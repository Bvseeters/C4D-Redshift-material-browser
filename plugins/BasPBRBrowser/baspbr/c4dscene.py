"""Builds a model read by gltf.py into the Cinema 4D document (main thread only).

One Null named after the model holds the glTF node tree: a polygon object per
mesh node (an Instance when the same mesh is used again), Nulls for the rest,
each with its own transform. A mesh with several materials gets a polygon
selection per material and a texture tag restricted to it. Smoothing: a Phong
tag without an angle limit, since the reader kept hard edges as split points
(models without normals are meant to look faceted and get none).

Materials are Redshift node materials made by rsmat from the glTF material:
colour, roughness / metalness (one channel each of glTF's packed texture,
times the factors), normal, emission, alpha as opacity; constants where there's
no texture. One material per glTF material that's actually used.
"""
import c4d

from . import rsmat
from .library import Channel, MapSet


def mapset(material, images):
    """The glTF material as a library.MapSet over the saved image files."""
    maps = MapSet({role: images[i] for role, i in material.maps.items() if i in images})
    maps.channels = {role: Channel(*ch) for role, ch in material.channels.items() if role in maps}
    maps.base_color = material.base_color
    maps.roughness = material.roughness
    maps.metalness = material.metalness
    maps.tiling = material.tiling
    return maps


def _matrix(m):
    if m is None:
        return c4d.Matrix()
    off, v1, v2, v3 = m
    return c4d.Matrix(c4d.Vector(*off), c4d.Vector(*v1), c4d.Vector(*v2), c4d.Vector(*v3))


def _polygon_object(mesh, name):
    obj = c4d.PolygonObject(len(mesh.points), len(mesh.polys))
    obj.SetName(name)
    obj.SetAllPoints([c4d.Vector(x, y, z) for x, y, z in mesh.points])
    for i, (a, b, c) in enumerate(mesh.polys):
        obj.SetPolygon(i, c4d.CPolygon(a, b, c))
    if mesh.uvs:
        uvw = c4d.UVWTag(len(mesh.polys))
        for i, (p, q, r) in enumerate(mesh.uvs):
            a, b, c = c4d.Vector(p[0], p[1], 0), c4d.Vector(q[0], q[1], 0), c4d.Vector(r[0], r[1], 0)
            uvw.SetSlow(i, a, b, c, c)
        obj.InsertTag(uvw)
    if mesh.smooth:
        phong = obj.MakeTag(c4d.Tphong)
        phong[c4d.PHONGTAG_PHONG_ANGLELIMIT] = False
    obj.Message(c4d.MSG_UPDATE)
    return obj


def _texture_tag(obj, mat, restriction=""):
    tag = obj.MakeTag(c4d.Ttexture)
    tag[c4d.TEXTURETAG_MATERIAL] = mat
    tag[c4d.TEXTURETAG_PROJECTION] = c4d.TEXTURETAG_PROJECTION_UVW
    if restriction:
        tag[c4d.TEXTURETAG_RESTRICTION] = restriction
    return tag


def build(doc, scene, name, images, opts=None, progress=None):
    """Insert the model into doc (with undo; call between StartUndo / EndUndo).
    images: {image index: file path}. Returns (root object, {"objects", "materials",
    "polygons", "problems"})."""
    opts = opts or rsmat.Options()
    opts.displacement = False             # models carry no height maps for that
    opts.bump_from_height = False
    made, problems = {}, []

    def material(i):
        if i not in made:
            m = scene.materials[i]
            mat, probs = rsmat.build(doc, "%s - %s" % (name, m.name), mapset(m, images), opts)
            problems.extend(probs)
            made[i] = mat
        return made[i]

    root = c4d.BaseObject(c4d.Onull)
    root.SetName(name)
    built_meshes = {}                     # mesh index -> the first object made from it
    stats = {"objects": 0, "polygons": 0}
    total = max(1, sum(len(m.polys) for m in scene.meshes))

    def make(index, parent, depth):
        node = scene.nodes[index]
        if node.mesh is not None and node.mesh < len(scene.meshes):
            mesh = scene.meshes[node.mesh]
            if node.mesh in built_meshes:             # the same mesh again: an Instance of the first
                obj = c4d.BaseObject(c4d.Oinstance)
                obj.SetName(node.name)
                obj[c4d.INSTANCEOBJECT_LINK] = built_meshes[node.mesh]
            else:
                obj = _polygon_object(mesh, node.name)
                built_meshes[node.mesh] = obj
                used = sorted({m for m in mesh.materials if 0 <= m < len(scene.materials)})
                if len(used) == 1:
                    _texture_tag(obj, material(used[0]))
                for mi in (used if len(used) > 1 else []):
                    sel = c4d.SelectionTag(c4d.Tpolygonselection)
                    label = "%d %s" % (mi, scene.materials[mi].name)
                    sel.SetName(label)
                    bs = sel.GetBaseSelect()
                    for p, pm in enumerate(mesh.materials):
                        if pm == mi:
                            bs.Select(p)
                    obj.InsertTag(sel)
                    _texture_tag(obj, material(mi), label)
                stats["objects"] += 1
                stats["polygons"] += len(mesh.polys)
                if progress:
                    progress(stats["polygons"] / float(total))
        else:
            obj = c4d.BaseObject(c4d.Onull)
            obj.SetName(node.name)
        obj.SetMl(_matrix(node.matrix))
        obj.InsertUnderLast(parent)
        if depth < 64:
            for c in node.children:
                if 0 <= c < len(scene.nodes):
                    make(c, obj, depth + 1)

    for r in scene.roots:
        if 0 <= r < len(scene.nodes):
            make(r, root, 0)
    doc.InsertObject(root)
    doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, root)
    stats["materials"] = len(made)
    stats["problems"] = problems
    return root, stats
