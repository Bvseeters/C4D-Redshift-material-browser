"""Builds Redshift node materials from a set of texture maps (Cinema 4D only).

Graph, using Redshift's own node and port ids (verified against Maxon's SDK
examples and the Redshift node reference), so it works whatever the UI language
and from C4D 2023 onwards:

    Texture (Color, sRGB)        -> Standard Material . base_color
    Texture (Roughness, raw)     -> Standard Material . refl_roughness
      (Glossiness instead)       -> refl_roughness + refl_isglossiness
    Texture (Metalness, raw)     -> Standard Material . metalness
    Texture (Opacity, raw)       -> Standard Material . opacity_color
    Texture (Emission, sRGB)     -> Standard Material . emission_color
    Texture (Normal, raw) -> Bump Map (tangent-space normal) -> bump_input
      (no normal map: height -> Bump Map (height field), when asked for)
    Texture (Height, raw) -> Displacement -> Output . displacement (optional)
    Standard Material . outcolor -> Output . surface

Ports are looked up by full id first and by name suffix second; a port that
can't be found is reported on the console and skipped instead of failing the
whole material. "Check Redshift node IDs" in the browser's menu prints which
ids resolve, for diagnosing a Redshift version that renamed something.
"""
import os

import c4d
import maxon

RS_NODESPACE = "com.redshift3d.redshift4c4d.class.nodespace"
OUTPUT = "com.redshift3d.redshift4c4d.node.output"
CORE = "com.redshift3d.redshift4c4d.nodes.core."
STANDARD = CORE + "standardmaterial"
TEXTURE = CORE + "texturesampler"
BUMPMAP = CORE + "bumpmap"
DISPLACEMENT = CORE + "displacement"
RAW = "RS_INPUT_COLORSPACE_RAW"          # "" = Auto (sRGB for 8-bit colour images)

NAME_ATTR = "net.maxon.node.base.name"

# which maps are data (raw) rather than colour
DATA_ROLES = ("roughness", "gloss", "metalness", "opacity", "normal_gl", "normal_dx", "height", "ao")


class Options(object):
    def __init__(self, displacement=False, displacement_scale=1.0, bump_from_height=True,
                 bump_strength=1.0, height_bump_scale=0.05):
        self.displacement = displacement
        self.displacement_scale = displacement_scale    # scene units (cm by default)
        self.bump_from_height = bump_from_height
        self.bump_strength = bump_strength
        self.height_bump_scale = height_bump_scale


# ---- small node helpers -------------------------------------------------------------
def _valid(node):
    try:
        return node is not None and node.IsValid()
    except Exception:
        return node is not None


def _children(ports):
    try:
        return list(ports.GetChildren())
    except Exception:
        return []


def _port(node, port_id, output=False, problems=None):
    """Find a port by full id, then by its last id segment; None (and a note) if missing."""
    ports = node.GetOutputs() if output else node.GetInputs()
    p = ports.FindChild(port_id)
    if _valid(p):
        return p
    tail = port_id.split(".")[-1]
    for child in _children(ports):
        cid = str(child.GetId())
        if cid == tail or cid.endswith("." + tail):
            return child
    if output:                       # most nodes have a single output
        outs = _children(ports)
        if len(outs) == 1:
            return outs[0]
    if problems is not None:
        problems.append(port_id)
    return None


def _set(port, value):
    if not _valid(port):
        return False
    for setter in ("SetPortValue", "SetDefaultValue"):
        fn = getattr(port, setter, None)
        if fn is None:
            continue
        try:
            fn(value)
            return True
        except Exception:
            if isinstance(value, int) and not isinstance(value, bool):
                try:
                    fn(maxon.Int32(value))
                    return True
                except Exception:
                    pass
    return False


def _connect(src, dst):
    if _valid(src) and _valid(dst):
        src.Connect(dst)
        return True
    return False


def _name(node, text):
    for attr in (NAME_ATTR,):
        try:
            node.SetValue(attr, maxon.String(text))
            return
        except Exception:
            pass


def file_url(path):
    r"""A file Url for a local or network path (C:\..., \\server\share\..., /Volumes/...)."""
    path = os.path.abspath(path)
    if path.startswith("\\\\"):
        try:
            url = maxon.Url()
            url.SetSystemPath(path)      # maxon's own conversion knows UNC paths
            if str(url):
                return url
        except Exception:
            pass
    p = path.replace("\\", "/")
    if p.startswith("//"):
        return maxon.Url("file:" + p)    # file://server/share/...
    if not p.startswith("/"):
        p = "/" + p                      # file:///C:/... on Windows
    return maxon.Url("file://" + p)


def _transaction(graph):
    try:
        user = maxon.DataDictionary()
        user.Set(maxon.nodes.UndoMode, maxon.nodes.UNDO_MODE.ADD)
        return graph.BeginTransaction(user)
    except Exception:
        return graph.BeginTransaction()


# ---- the material -------------------------------------------------------------------
def build(doc, name, maps, opts=None):
    """Create a Redshift node material from {role: path}, insert it into doc (with
    undo; call inside doc.StartUndo/EndUndo) and return (material, problems)."""
    opts = opts or Options()
    problems = []

    mat = c4d.BaseMaterial(c4d.Mmaterial)
    if mat is None:
        raise MemoryError("couldn't allocate a material")
    mat.SetName(name)
    graph = mat.GetNodeMaterialReference().CreateEmptyGraph(maxon.Id(RS_NODESPACE))
    if graph is None or graph.IsNullValue():
        raise RuntimeError("Redshift isn't available in this Cinema 4D (no Redshift node space)")

    # insert before editing the graph, so the viewport picks the textures up
    doc.InsertMaterial(mat)
    doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, mat)

    with _transaction(graph) as tr:
        out = graph.AddChild(maxon.Id(), maxon.Id(OUTPUT))
        std = graph.AddChild(maxon.Id(), maxon.Id(STANDARD))
        _name(std, name)
        _connect(_port(std, STANDARD + ".outcolor", True, problems), _port(out, OUTPUT + ".surface", False, problems))

        def texture(role, label):
            tex = graph.AddChild(maxon.Id(), maxon.Id(TEXTURE))
            _name(tex, label)
            bundle = _port(tex, TEXTURE + ".tex0", False, problems)
            if _valid(bundle):
                _set(bundle.FindChild("path"), file_url(maps[role]))
                if role in DATA_ROLES:
                    _set(bundle.FindChild("colorspace"), RAW)
            return _port(tex, TEXTURE + ".outcolor", True, problems)

        def to_std(role, label, port):
            if role in maps:
                return _connect(texture(role, label), _port(std, STANDARD + "." + port, False, problems))
            return False

        to_std("color", "Color", "base_color")
        if not to_std("roughness", "Roughness", "refl_roughness"):
            if to_std("gloss", "Glossiness", "refl_roughness"):
                _set(_port(std, STANDARD + ".refl_isglossiness", False, problems), True)
        to_std("metalness", "Metalness", "metalness")
        to_std("opacity", "Opacity", "opacity_color")
        if to_std("emission", "Emission", "emission_color"):
            _set(_port(std, STANDARD + ".emission_weight", False, problems), 1.0)

        # normal map (preferred) or height as bump
        normal_role = "normal_gl" if "normal_gl" in maps else ("normal_dx" if "normal_dx" in maps else None)
        if normal_role or (opts.bump_from_height and "height" in maps):
            bump = graph.AddChild(maxon.Id(), maxon.Id(BUMPMAP))
            if normal_role:
                _name(bump, "Normal")
                _set(_port(bump, BUMPMAP + ".inputtype", False, problems), 1)     # tangent-space normal
                _set(_port(bump, BUMPMAP + ".scale", False, problems), float(opts.bump_strength))
                if normal_role == "normal_dx":
                    _set(_port(bump, BUMPMAP + ".flipy", False, None), True)        # DirectX -> OpenGL
                src = texture(normal_role, "Normal")
            else:
                _name(bump, "Bump (height)")
                _set(_port(bump, BUMPMAP + ".inputtype", False, problems), 0)     # height field
                _set(_port(bump, BUMPMAP + ".scale", False, problems), float(opts.height_bump_scale))
                src = texture("height", "Height (bump)")
            _connect(src, _port(bump, BUMPMAP + ".input", False, problems))
            _connect(_port(bump, BUMPMAP + ".out", True, problems), _port(std, STANDARD + ".bump_input", False, problems))

        # displacement (needs displacement enabled on the object's Redshift Object tag to render)
        if opts.displacement and "height" in maps:
            disp = graph.AddChild(maxon.Id(), maxon.Id(DISPLACEMENT))
            _name(disp, "Displacement")
            _connect(texture("height", "Height"), _port(disp, DISPLACEMENT + ".texmap", False, problems))
            _set(_port(disp, DISPLACEMENT + ".scale", False, problems), float(opts.displacement_scale))
            _connect(_port(disp, DISPLACEMENT + ".out", True, problems), _port(out, OUTPUT + ".displacement", False, problems))

        tr.Commit()

    for p in problems:
        print("[BasPBR Browser] Redshift port not found, skipped: %s" % p)
    return mat, problems


def assign(doc, mat, objects):
    """Put mat on each object with a UVW texture tag (with undo). Returns how many."""
    n = 0
    for obj in objects:
        try:
            if obj is None or not obj.IsAlive():
                continue
        except Exception:
            continue
        tag = obj.MakeTag(c4d.Ttexture)
        if tag is None:
            continue
        tag[c4d.TEXTURETAG_MATERIAL] = mat
        tag[c4d.TEXTURETAG_PROJECTION] = c4d.TEXTURETAG_PROJECTION_UVW
        doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, tag)
        n += 1
    return n


# ---- diagnostics ------------------------------------------------------------------------
CHECK = {
    OUTPUT: (("surface", False), ("displacement", False)),
    STANDARD: (("outcolor", True), ("base_color", False), ("refl_roughness", False), ("refl_isglossiness", False),
               ("metalness", False), ("opacity_color", False), ("emission_color", False),
               ("emission_weight", False), ("bump_input", False)),
    TEXTURE: (("tex0", False), ("outcolor", True)),
    BUMPMAP: (("input", False), ("inputtype", False), ("scale", False), ("flipy", False), ("out", True)),
    DISPLACEMENT: (("texmap", False), ("scale", False), ("out", True)),
}


def check_ids():
    """Print which node and port ids resolve in this Redshift, plus every port each
    node really has. Uses a throwaway material that's never inserted."""
    lines = ["[BasPBR Browser] Redshift node/port check"]
    mat = c4d.BaseMaterial(c4d.Mmaterial)
    graph = mat.GetNodeMaterialReference().CreateEmptyGraph(maxon.Id(RS_NODESPACE))
    if graph is None or graph.IsNullValue():
        print(lines[0] + ": no Redshift node space - is Redshift installed?")
        return False
    ok = True
    with _transaction(graph) as tr:
        for node_id, ports in CHECK.items():
            node = graph.AddChild(maxon.Id(), maxon.Id(node_id))
            if not _valid(node):
                lines.append("  MISSING NODE  %s" % node_id)
                ok = False
                continue
            lines.append("  node %s" % node_id)
            for tail, output in ports:
                found = _port(node, node_id + "." + tail, output, None)
                lines.append("    %-4s %-3s %s" % ("ok" if found else "MISS", "out" if output else "in", tail))
                ok = ok and bool(found)
            for side, ps in (("in", node.GetInputs()), ("out", node.GetOutputs())):
                ids = [str(c.GetId()) for c in _children(ps)]
                if ids:
                    lines.append("    all %s: %s" % (side, ", ".join(i.split(".")[-1] for i in ids)))
            if node_id == TEXTURE:
                b = node.GetInputs().FindChild(TEXTURE + ".tex0")
                if _valid(b):
                    lines.append("    tex0 bundle: path=%s colorspace=%s" % (
                        "ok" if _valid(b.FindChild("path")) else "MISS",
                        "ok" if _valid(b.FindChild("colorspace")) else "MISS"))
        tr.Commit()                     # the material is never inserted, so this goes nowhere
    print("\n".join(lines))
    print("[BasPBR Browser] all ids resolve" if ok else "[BasPBR Browser] some ids are missing - see above")
    return ok
