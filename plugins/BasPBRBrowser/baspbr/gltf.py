"""A glTF 2.0 reader (.glb, or .gltf with its .bin and images) - plain Python, so
it runs on worker threads. It turns a model into a plain description that
c4dscene.py builds Cinema 4D objects and Redshift materials from.

Converted to Cinema 4D's conventions on the way in:
  * metres -> centimetres (x 100)
  * glTF is right-handed, Cinema 4D left-handed: z is mirrored, and every
    triangle is reversed so it keeps facing outward
  * UVs stay as they are (both put v = 0 at the top of the image)
  * vertices sharing a position and a normal are welded: glTF splits vertices
    at every UV seam, and Cinema 4D would shade those splits as creases. Hard
    edges (different normals) stay split, so a Phong tag without an angle limit
    reproduces the model's own smoothing.

Materials: metallic-roughness (and the older specular-glossiness extension) as
a library.MapSet-like description - which image feeds which role, which channel
of it (glTF packs roughness in green, metalness in blue), the factors as remaps
or constants, alpha as opacity, KHR_texture_transform's scale as tiling.

Not supported, said so in Scene.warnings or GltfError: Draco / meshopt
compressed geometry, KTX2 textures, skinning and animation (the model comes
in in its rest pose), morph targets, points and lines.
"""
import base64
import json
import math
import os
import struct
import urllib.parse

GLB_MAGIC = 0x46546C67
COMPONENT = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2), 5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
NORMALIZE = {5120: 127.0, 5121: 255.0, 5122: 32767.0, 5123: 65535.0}
COMPRESSED = {"KHR_draco_mesh_compression": "Draco", "EXT_meshopt_compression": "meshopt"}
EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/ktx2": ".ktx2"}
UNITS = 100.0                      # metres -> centimetres


class GltfError(Exception):
    pass


class Mesh(object):
    def __init__(self, name):
        self.name = name
        self.points = []           # [(x, y, z)], Cinema 4D space
        self.polys = []            # [(a, b, c)]
        self.uvs = []              # per polygon ((u, v), (u, v), (u, v)), or [] when the mesh has none
        self.materials = []        # per polygon: material index, -1 = none
        self.smooth = True         # False when the file has no normals (glTF then means flat shading)


class Node(object):
    def __init__(self, name):
        self.name = name
        self.matrix = None         # (off, v1, v2, v3) in Cinema 4D space; None = identity
        self.mesh = None
        self.children = []


class Material(object):
    def __init__(self, name):
        self.name = name
        self.maps = {}             # role -> image index
        self.channels = {}         # role -> (channel index, min, max): the value is that channel remapped
        self.base_color = None     # [r, g, b] linear, when there's no colour texture
        self.roughness = None      # constants, when there's no texture
        self.metalness = None
        self.tiling = 1.0


class Scene(object):
    def __init__(self):
        self.nodes, self.roots, self.meshes, self.materials = [], [], [], []
        self.images = []           # [(name, extension, bytes or None, path or None)]
        self.textures = []         # texture index -> image index (or None)
        self.warnings = []

    def counts(self):
        return {"objects": sum(1 for n in self.nodes if n.mesh is not None),
                "polygons": sum(len(m.polys) for m in self.meshes),
                "points": sum(len(m.points) for m in self.meshes),
                "materials": len(self.materials), "images": len(self.images)}


# ---- reading the file ---------------------------------------------------------------------
def _read(path):
    """(json, binary chunk or None, folder for relative uris)."""
    with open(path, "rb") as f:
        data = f.read()
    base = os.path.dirname(os.path.abspath(path))
    if len(data) >= 12 and struct.unpack_from("<I", data, 0)[0] == GLB_MAGIC:
        version, length = struct.unpack_from("<II", data, 4)
        if version != 2:
            raise GltfError("glTF version %d - only version 2 is supported" % version)
        pos, doc, binary = 12, None, None
        while pos + 8 <= min(length, len(data)):
            size, kind = struct.unpack_from("<II", data, pos)
            chunk = data[pos + 8:pos + 8 + size]
            if kind == 0x4E4F534A:                     # "JSON"
                doc = json.loads(chunk.decode("utf-8"))
            elif kind == 0x004E4942 and binary is None:  # "BIN"
                binary = chunk
            pos += 8 + size + (-size % 4)
        if doc is None:
            raise GltfError("no JSON chunk in the .glb")
        return doc, binary, base
    try:
        return json.loads(data.decode("utf-8-sig")), None, base
    except ValueError:
        raise GltfError("not a glTF file")


def _uri_bytes(uri, base):
    if uri.startswith("data:"):
        return base64.b64decode(uri.split(",", 1)[1])
    path = os.path.normpath(os.path.join(base, urllib.parse.unquote(uri)))
    if not path.startswith(os.path.normpath(base)):
        raise GltfError("a file reference points outside the model's folder: " + uri)
    with open(path, "rb") as f:
        return f.read()


class _Reader(object):
    def __init__(self, doc, binary, base):
        self.doc, self.binary, self.base = doc, binary, base
        self._buffers = {}

    def buffer(self, i):
        if i not in self._buffers:
            b = self.doc["buffers"][i]
            if "uri" in b:
                self._buffers[i] = _uri_bytes(b["uri"], self.base)
            elif self.binary is not None:
                self._buffers[i] = self.binary
            else:
                raise GltfError("buffer %d has no data" % i)
        return self._buffers[i]

    def view(self, i):
        v = self.doc["bufferViews"][i]
        data = self.buffer(v["buffer"])
        start = v.get("byteOffset", 0)
        return data, start, v.get("byteLength", len(data) - start), v.get("byteStride")

    def accessor(self, i):
        """The accessor's elements as tuples (scalars as plain numbers)."""
        a = self.doc["accessors"][i]
        fmt, size = COMPONENT[a["componentType"]]
        width = WIDTH[a["type"]]
        count = a["count"]
        if "bufferView" in a:
            data, start, length, stride = self.view(a["bufferView"])
            start += a.get("byteOffset", 0)
            elem = size * width
            if not stride or stride == elem:
                flat = struct.unpack_from("<%d%s" % (count * width, fmt), data, start)
            else:
                one = struct.Struct("<%d%s" % (width, fmt))
                flat = []
                for k in range(count):
                    flat.extend(one.unpack_from(data, start + k * stride))
        else:
            flat = [0] * (count * width)
        if a.get("normalized") and a["componentType"] in NORMALIZE:
            d = NORMALIZE[a["componentType"]]
            flat = [max(-1.0, v / d) for v in flat]
        out = [tuple(flat[k * width:(k + 1) * width]) for k in range(count)] if width > 1 else list(flat)
        sparse = a.get("sparse")
        if sparse:
            out = list(out)
            ic = sparse["indices"]
            idx_fmt, idx_size = COMPONENT[ic["componentType"]]
            data, start, _, _ = self.view(ic["bufferView"])
            idx = struct.unpack_from("<%d%s" % (sparse["count"], idx_fmt), data, start + ic.get("byteOffset", 0))
            vc = sparse["values"]
            data, start, _, _ = self.view(vc["bufferView"])
            vals = struct.unpack_from("<%d%s" % (sparse["count"] * width, fmt), data, start + vc.get("byteOffset", 0))
            for k, j in enumerate(idx):
                out[j] = tuple(vals[k * width:(k + 1) * width]) if width > 1 else vals[k]
        return out


# ---- geometry -----------------------------------------------------------------------------------
def _triangles(indices, mode):
    if mode == 4:
        return [(indices[k], indices[k + 1], indices[k + 2]) for k in range(0, len(indices) - 2, 3)]
    if mode == 5:                                   # strip: every other one flipped
        return [(indices[k], indices[k + 1], indices[k + 2]) if k % 2 == 0 else
                (indices[k + 1], indices[k], indices[k + 2]) for k in range(len(indices) - 2)]
    if mode == 6:                                   # fan
        return [(indices[0], indices[k], indices[k + 1]) for k in range(1, len(indices) - 1)]
    return []


def _mesh(reader, m, index, scene):
    mesh = Mesh(m.get("name") or "Mesh %d" % index)
    weld = {}
    for prim in m.get("primitives", []):
        exts = prim.get("extensions") or {}
        for ext, what in COMPRESSED.items():
            if ext in exts:
                raise GltfError("this model's geometry is %s-compressed, which isn't supported - "
                                "Sketchfab's own .glb downloads aren't" % what)
        mode = prim.get("mode", 4)
        if mode not in (4, 5, 6):
            scene.warnings.append("%s: points / lines skipped" % mesh.name)
            continue
        attrs = prim.get("attributes", {})
        if "POSITION" not in attrs:
            continue
        pos = reader.accessor(attrs["POSITION"])
        nrm = reader.accessor(attrs["NORMAL"]) if "NORMAL" in attrs else None
        uv = reader.accessor(attrs["TEXCOORD_0"]) if "TEXCOORD_0" in attrs else None
        if nrm is None:
            mesh.smooth = False
        indices = reader.accessor(prim["indices"]) if "indices" in prim else list(range(len(pos)))
        # weld: same position (and normal) -> one point
        remap = []
        for k, p in enumerate(pos):
            key = (round(p[0], 6), round(p[1], 6), round(p[2], 6))
            if nrm is not None:
                n = nrm[k]
                key += (round(n[0], 3), round(n[1], 3), round(n[2], 3))
            j = weld.get(key)
            if j is None:
                j = weld[key] = len(mesh.points)
                mesh.points.append((p[0] * UNITS, p[1] * UNITS, -p[2] * UNITS))
            remap.append(j)
        mat = prim.get("material", -1)
        if uv is not None and not mesh.uvs and mesh.polys:
            mesh.uvs = [((0.0, 0.0),) * 3] * len(mesh.polys)    # an earlier primitive had none
        for a, b, c in _triangles(indices, mode):
            if a >= len(pos) or b >= len(pos) or c >= len(pos):
                continue
            pa, pb, pc = remap[a], remap[b], remap[c]
            if pa == pb or pb == pc or pa == pc:
                continue                                       # degenerate after welding
            mesh.polys.append((pa, pc, pb))                    # reversed: z is mirrored
            mesh.materials.append(mat)
            if uv is not None:
                mesh.uvs.append(((uv[a][0], uv[a][1]), (uv[c][0], uv[c][1]), (uv[b][0], uv[b][1])))
            elif mesh.uvs:
                mesh.uvs.append(((0.0, 0.0),) * 3)
    return mesh


# ---- transforms -------------------------------------------------------------------------------------
def _matrix(n):
    """The node's local transform as (off, v1, v2, v3) in Cinema 4D space, or None."""
    if "matrix" in n:
        m = n["matrix"]                                # column-major
        cols = [(m[0], m[1], m[2]), (m[4], m[5], m[6]), (m[8], m[9], m[10])]
        t = (m[12], m[13], m[14])
    elif any(k in n for k in ("translation", "rotation", "scale")):
        x, y, z, w = n.get("rotation", (0.0, 0.0, 0.0, 1.0))
        sx, sy, sz = n.get("scale", (1.0, 1.0, 1.0))
        cols = [((1 - 2 * (y * y + z * z)) * sx, (2 * (x * y + z * w)) * sx, (2 * (x * z - y * w)) * sx),
                ((2 * (x * y - z * w)) * sy, (1 - 2 * (x * x + z * z)) * sy, (2 * (y * z + x * w)) * sy),
                ((2 * (x * z + y * w)) * sz, (2 * (y * z - x * w)) * sz, (1 - 2 * (x * x + y * y)) * sz)]
        t = tuple(n.get("translation", (0.0, 0.0, 0.0)))
    else:
        return None
    # mirror z on both sides: S M S with S = diag(1, 1, -1)
    v1 = (cols[0][0], cols[0][1], -cols[0][2])
    v2 = (cols[1][0], cols[1][1], -cols[1][2])
    v3 = (-cols[2][0], -cols[2][1], cols[2][2])
    off = (t[0] * UNITS, t[1] * UNITS, -t[2] * UNITS)
    return off, v1, v2, v3


# ---- materials ---------------------------------------------------------------------------------------
def _image_of(scene, ref):
    if not ref or "index" not in ref:
        return None
    t = ref["index"]
    return scene.textures[t] if 0 <= t < len(scene.textures) else None


def _tiling(ref):
    tt = ((ref or {}).get("extensions") or {}).get("KHR_texture_transform") or {}
    s = tt.get("scale")
    return float(s[0]) if s else 1.0


def _material(scene, m, index):
    out = Material(m.get("name") or "Material %d" % index)
    exts = m.get("extensions") or {}
    sg = exts.get("KHR_materials_pbrSpecularGlossiness")
    blend = m.get("alphaMode", "OPAQUE") in ("BLEND", "MASK")
    if sg is not None:
        color_ref, factor = sg.get("diffuseTexture"), sg.get("diffuseFactor", [1, 1, 1, 1])
        img = _image_of(scene, sg.get("specularGlossinessTexture"))
        gloss = float(sg.get("glossinessFactor", 1.0))
        if img is not None:                    # roughness = 1 - glossiness (alpha)
            out.maps["roughness"] = img
            out.channels["roughness"] = (3, 1.0, 1.0 - gloss)
        else:
            out.roughness = 1.0 - gloss
        out.metalness = 0.0
    else:
        pbr = m.get("pbrMetallicRoughness") or {}
        color_ref, factor = pbr.get("baseColorTexture"), pbr.get("baseColorFactor", [1, 1, 1, 1])
        rough, metal = float(pbr.get("roughnessFactor", 1.0)), float(pbr.get("metallicFactor", 1.0))
        img = _image_of(scene, pbr.get("metallicRoughnessTexture"))
        if img is not None:                    # green = roughness, blue = metalness, times the factors
            out.maps["roughness"] = out.maps["metalness"] = img
            out.channels["roughness"] = (1, 0.0, rough)
            out.channels["metalness"] = (2, 0.0, metal)
        else:
            out.roughness, out.metalness = rough, metal
    color = _image_of(scene, color_ref)
    if color is not None:
        out.maps["color"] = color
        out.tiling = _tiling(color_ref)
        if blend:
            out.maps["opacity"] = color
            out.channels["opacity"] = (3, 0.0, float(factor[3]) if len(factor) > 3 else 1.0)
    else:
        out.base_color = [float(v) for v in factor[:3]]
    normal = _image_of(scene, m.get("normalTexture"))
    if normal is not None:
        out.maps["normal_gl"] = normal
    emissive = _image_of(scene, m.get("emissiveTexture"))
    if emissive is not None:
        out.maps["emission"] = emissive
    return out


# ---- the whole file -----------------------------------------------------------------------------------
def load(path):
    """Read a .glb / .gltf into a Scene. Raises GltfError for what can't be read."""
    doc, binary, base = _read(path)
    if not str((doc.get("asset") or {}).get("version", "2")).startswith("2"):
        raise GltfError("glTF version %s - only version 2 is supported" % (doc.get("asset") or {}).get("version"))
    for ext in doc.get("extensionsRequired") or []:
        if ext in COMPRESSED:
            raise GltfError("this model's geometry is %s-compressed, which isn't supported" % COMPRESSED[ext])
    reader = _Reader(doc, binary, base)
    scene = Scene()

    for i, img in enumerate(doc.get("images") or []):
        ext = EXTENSIONS.get(img.get("mimeType", ""), "")
        if "uri" in img and not img["uri"].startswith("data:"):
            p = os.path.normpath(os.path.join(base, urllib.parse.unquote(img["uri"])))
            scene.images.append((img.get("name") or os.path.splitext(os.path.basename(p))[0],
                                 ext or os.path.splitext(p)[1].lower(), None, p))
            continue
        try:
            if "uri" in img:
                data = _uri_bytes(img["uri"], base)
                ext = ext or EXTENSIONS.get(img["uri"][5:].split(";")[0], ".png")
            else:
                d, start, length, _ = reader.view(img["bufferView"])
                data = d[start:start + length]
        except Exception:
            data = None
        if not ext and data:
            ext = ".png" if data[:4] == b"\x89PNG" else ".jpg" if data[:2] == b"\xff\xd8" else ".bin"
        scene.images.append((img.get("name") or "image%d" % i, ext, data, None))
    for t in doc.get("textures") or []:
        src = t.get("source")
        for ext in ("EXT_texture_webp", "KHR_texture_basisu", "MSFT_texture_dds"):
            if src is None and ext in (t.get("extensions") or {}):
                src = t["extensions"][ext].get("source")
        scene.textures.append(src)
    bad = {scene.images[i][1] for i in scene.textures if i is not None and i < len(scene.images)} & {".webp", ".ktx2", ".bin"}
    if bad:
        scene.warnings.append("some textures are %s, which Cinema 4D can't read" % ", ".join(sorted(bad)))

    scene.materials = [_material(scene, m, i) for i, m in enumerate(doc.get("materials") or [])]
    scene.meshes = [_mesh(reader, m, i, scene) for i, m in enumerate(doc.get("meshes") or [])]
    for i, n in enumerate(doc.get("nodes") or []):
        node = Node(n.get("name") or "Node %d" % i)
        node.matrix = _matrix(n)
        node.mesh = n.get("mesh")
        node.children = list(n.get("children") or [])
        if n.get("skin") is not None and "skin" not in " ".join(scene.warnings):
            scene.warnings.append("skinned: imported in its rest pose, without bones")
        scene.nodes.append(node)
    if doc.get("animations"):
        scene.warnings.append("animations aren't imported")
    scenes = doc.get("scenes") or []
    if scenes:
        scene.roots = list(scenes[doc.get("scene", 0)].get("nodes") or [])
    else:                                             # no scene list: every node nobody parents
        children = {c for n in scene.nodes for c in n.children}
        scene.roots = [i for i in range(len(scene.nodes)) if i not in children]
    if not scene.nodes and scene.meshes:              # meshes but no nodes at all
        for i, m in enumerate(scene.meshes):
            node = Node(m.name)
            node.mesh = i
            scene.nodes.append(node)
            scene.roots.append(len(scene.nodes) - 1)
    return scene


def save_images(scene, folder, prefix=""):
    """Write the images to folder (the library keeps them; Redshift reads them from there).
    Returns {image index: file name}."""
    os.makedirs(folder, exist_ok=True)
    out, used = {}, set()
    for i, (name, ext, data, path) in enumerate(scene.images):
        stem = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)[:60].strip("_") or "image"
        fn = "%s%02d_%s%s" % (prefix, i, stem, ext or ".png")
        while fn.lower() in used:
            fn = "_" + fn
        used.add(fn.lower())
        try:
            if data is not None:
                with open(os.path.join(folder, fn), "wb") as f:
                    f.write(data)
            elif path and os.path.isfile(path):
                with open(path, "rb") as src, open(os.path.join(folder, fn), "wb") as f:
                    f.write(src.read())
            else:
                continue
        except OSError:
            continue
        out[i] = fn
    return out
