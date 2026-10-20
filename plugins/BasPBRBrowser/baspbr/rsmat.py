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
    Texture (ORM, raw) -> Color Splitter . G -> refl_roughness, . B -> metalness
      (packed map, used when there's no separate roughness / metalness map)
    Texture (Normal, raw) -> Bump Map (tangent-space normal) -> bump_input
      (no normal map: height -> Bump Map (height field), when asked for)
    Texture (Height, raw) -> Displacement -> Output . displacement (optional)
    Standard Material . outcolor -> Output . surface

When a material graph said more (a MapSet from AMD's MaterialX):
    Texture (raw) -> Color Splitter . R/G/B/A -> Change Range (0..1 -> min..max)
      -> refl_roughness / metalness   (the channel the graph names, remapped;
                                       no Range when it's 0..1 already)
    a constant roughness / metalness / colour -> that port's value
    the authored tiling -> every Texture's Scale
Each file gets one Texture node (and one Color Splitter), however many ports it feeds.

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
SPLITTER = CORE + "rscolorsplitter"
RANGE = CORE + "rsmathrange"             # Math Float "Change Range"
RAW = "RS_INPUT_COLORSPACE_RAW"          # "" = Auto (sRGB for 8-bit colour images)
CHANNELS = ("outr", "outg", "outb", "outa")   # Color Splitter outputs by channel index

NAME_ATTR = "net.maxon.node.base.name"

# which maps are data (raw) rather than colour
DATA_ROLES = ("roughness", "gloss", "metalness", "opacity", "normal_gl", "normal_dx", "height", "ao", "orm")


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


def _set_first(port, values):
    """Set the first of values (built lazily) the port accepts: vector and colour
    types differ between Cinema 4D versions."""
    for make in values:
        try:
            value = make()
        except Exception:
            continue
        if _set(port, value):
            return True
    return False


def _color(rgb):
    r, g, b = (float(v) for v in rgb)
    return (lambda: maxon.Color(r, g, b), lambda: maxon.Color64(r, g, b), lambda: c4d.Vector(r, g, b))


def _vec2(v):
    v = float(v)
    return (lambda: maxon.Vector2d(v, v), lambda: maxon.Vector(v, v, 1.0), lambda: c4d.Vector(v, v, 1.0))


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
    """Create a Redshift node material from {role: path} (or a MapSet), insert it
    into doc (with undo; call inside doc.StartUndo/EndUndo) and return
    (material, problems)."""
    opts = opts or Options()
    problems = []
    # what a material graph adds (a plain dict has none of it)
    channels = getattr(maps, "channels", None) or {}
    constants = {"roughness": getattr(maps, "roughness", None), "metalness": getattr(maps, "metalness", None)}
    base_color = getattr(maps, "base_color", None)
    tiling = float(getattr(maps, "tiling", 1.0) or 1.0)

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

        samplers, splitters = {}, {}

        def texture(role, label):
            """The Texture node's output for role's file (one node per file and colour space)."""
            key = (maps[role], role in DATA_ROLES)
            if key not in samplers:
                tex = graph.AddChild(maxon.Id(), maxon.Id(TEXTURE))
                _name(tex, label)
                bundle = _port(tex, TEXTURE + ".tex0", False, problems)
                if _valid(bundle):
                    _set(bundle.FindChild("path"), file_url(maps[role]))
                    if role in DATA_ROLES:
                        _set(bundle.FindChild("colorspace"), RAW)
                if abs(tiling - 1.0) > 1e-6:            # the authored tiling, on every map
                    _set_first(_port(tex, TEXTURE + ".scale", False, problems), _vec2(tiling))
                samplers[key] = _port(tex, TEXTURE + ".outcolor", True, problems)
            return samplers[key]

        def channel(role, label, index, split_label=None):
            """One channel (0..3 = R, G, B, A) of role's file, through a Color Splitter (one per file)."""
            path = maps[role]
            if path not in splitters:
                split = graph.AddChild(maxon.Id(), maxon.Id(SPLITTER))
                _name(split, split_label or label + " channels")
                _connect(texture(role, label), _port(split, SPLITTER + ".input", False, problems))
                splitters[path] = split
            return _port(splitters[path], SPLITTER + "." + CHANNELS[index], True, problems)

        def remap(src, lo, hi, label):
            """src (0..1) remapped to lo..hi through a Change Range node; src itself if
            that's 0..1 already, or if this Redshift has no such node."""
            if abs(lo) < 1e-4 and abs(hi - 1.0) < 1e-4:
                return src
            try:
                rng = graph.AddChild(maxon.Id(), maxon.Id(RANGE))
            except Exception:
                rng = None
            if not _valid(rng):
                problems.append(RANGE)
                return src
            _name(rng, label + " range")
            for port, value in (("old_min", 0.0), ("old_max", 1.0), ("new_min", lo), ("new_max", hi)):
                _set(_port(rng, RANGE + "." + port, False, problems), float(value))
            _connect(src, _port(rng, RANGE + ".input", False, problems))
            return _port(rng, RANGE + ".out", True, problems)

        def from_graph(role, port):
            """roughness / metalness as the material graph gives it: one channel of a
            texture (remapped), or a constant. False when the graph says nothing."""
            ch = channels.get(role)
            if role in maps and ch is not None:
                sharing = [r.title() for r in ("roughness", "metalness")
                           if r in channels and maps.get(r) == maps[role]]
                label = " / ".join(sharing) or role.title()
                _connect(remap(channel(role, label, ch.index), ch.min, ch.max, role.title()),
                         _port(std, STANDARD + "." + port, False, problems))
                return True
            if constants[role] is not None:
                _set(_port(std, STANDARD + "." + port, False, problems), float(constants[role]))
                return True
            return False

        def to_std(role, label, port):
            if role in maps:
                return _connect(texture(role, label), _port(std, STANDARD + "." + port, False, problems))
            return False

        if not to_std("color", "Color", "base_color") and base_color is not None:
            _set_first(_port(std, STANDARD + ".base_color", False, problems), _color(base_color))
        if not from_graph("roughness", "refl_roughness") and not to_std("roughness", "Roughness", "refl_roughness"):
            if to_std("gloss", "Glossiness", "refl_roughness"):
                _set(_port(std, STANDARD + ".refl_isglossiness", False, problems), True)
        has_metal = from_graph("metalness", "metalness") or to_std("metalness", "Metalness", "metalness")
        has_rough = "roughness" in maps or "gloss" in maps or constants["roughness"] is not None
        if "orm" in maps and not (has_rough and has_metal):
            orm = "ORM (occlusion / roughness / metalness)"
            if not has_rough:
                _connect(channel("orm", orm, 1, "ORM channels"), _port(std, STANDARD + ".refl_roughness", False, problems))
            if not has_metal:
                _connect(channel("orm", orm, 2, "ORM channels"), _port(std, STANDARD + ".metalness", False, problems))
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
    TEXTURE: (("tex0", False), ("scale", False), ("outcolor", True)),
    BUMPMAP: (("input", False), ("inputtype", False), ("scale", False), ("flipy", False), ("out", True)),
    DISPLACEMENT: (("texmap", False), ("scale", False), ("out", True)),
    SPLITTER: (("input", False), ("outr", True), ("outg", True), ("outb", True), ("outa", True)),
    RANGE: (("input", False), ("old_min", False), ("old_max", False), ("new_min", False), ("new_max", False),
            ("out", True)),
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
