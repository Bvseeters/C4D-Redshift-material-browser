"""Reads a MaterialX (.mtlx) standard_surface to find out how its textures are
really used - plain Python. File names can't tell you that: AMD's materials put
roughness in a different channel of a "_mask.png" per material, remapped between
a min and a max, and many have NO metalness (or even colour) texture - the value
is a constant in the graph (every AMD metal: metalness 1.0).

For each input the graph is followed back to the texture file, the channel and a
linear remap (value = min + (max - min) * channel), or to a constant, through
    image, extract, constant, mix, clamp, max / min, multiply, add, subtract, normalmap.
Layered parametric graphs (several channels at different scales, combined with
max) are reduced to their base layer - close, not exact. The tiling is the UV
scale of the colour texture (or, without one, of the first other texture).

The same reading as the Unity version of BasPBR Browser (Mtlx.cs).
"""
import os
import xml.etree.ElementTree as ET

from .library import Channel, MapSet


class _Signal(object):
    """A texture channel remapped to min..max, or a constant (file None, value min)."""
    __slots__ = ("file", "channel", "min", "max", "scale")

    def __init__(self, file=None, channel=0, lo=0.0, hi=1.0, scale=1.0):
        self.file, self.channel, self.min, self.max, self.scale = file, channel, lo, hi, scale

    @classmethod
    def const(cls, v):
        return cls(None, 0, v, v)

    @property
    def is_const(self):
        return self.file is None

    @property
    def value(self):
        return self.min


def read(path, files):
    """A MapSet from the .mtlx at path, against the unpacked images ({file name: path}).
    None when there's no standard_surface to read."""
    with open(path, "rb") as f:
        data = f.read()
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return None                              # no entity tricks from a download
    root = ET.fromstring(data)
    surface = next((e for e in root.iter() if _tag(e) == "standard_surface"), None)
    if surface is None:
        return None
    r = _Reader(root, files)
    ms = MapSet()
    scales = []

    # colour: a texture, or a constant
    color, constant, scale = r.color(r.input(surface, "base_color"))
    if color:
        ms["color"] = color
        scales.append(scale)
    elif constant:
        ms.base_color = constant

    # roughness and metalness: a channel of some texture (remapped), or a constant
    for name, role in (("specular_roughness", "roughness"), ("metalness", "metalness")):
        s = r.scalar(r.input(surface, name))
        if s is None:
            continue
        if s.is_const:
            setattr(ms, role, _01(s.value))
            continue
        ms[role] = s.file
        ms.channels[role] = Channel(s.channel, _01(s.min), _01(s.max))
        scales.append(s.scale)

    normal, scale = r.normal(r.input(surface, "normal"))
    if normal:
        ms["normal_gl"] = normal                 # MaterialX normal maps are +Y (OpenGL)
        scales.append(scale)

    # emission / opacity when they're textures
    if r.number(r.input(surface, "emission")) > 0:
        emission = r.color(r.input(surface, "emission_color"))[0]
        if emission:
            ms["emission"] = emission
    opacity = r.color(r.input(surface, "opacity"))[0]
    if opacity:
        ms["opacity"] = opacity

    ms.tiling = scales[0] if scales else 1.0
    return ms


def _01(v):
    return 0.0 if v < 0 else 1.0 if v > 1 else v


def _tag(el):
    tag = el.tag if isinstance(el.tag, str) else ""
    return tag.rsplit("}", 1)[-1]                # without a namespace


def _num(v):
    try:
        return float((v or "").split(",")[0].strip())
    except ValueError:
        return 0.0


def _rgb(v):
    parts = [_num(p) for p in (v or "").split(",")]
    while len(parts) < 3:
        parts.append(parts[0] if parts else 1.0)
    return parts[:3]


def _plain(inp):
    """An input holding a value of its own rather than a connection."""
    return "value" in inp.attrib and "nodename" not in inp.attrib and "nodegraph" not in inp.attrib


class _Reader(object):
    def __init__(self, root, files):
        self.root = root
        self.files = files
        self.parent = {c: p for p in root.iter() for c in p}
        self.graphs = {g.get("name"): g for g in root.iter() if _tag(g) == "nodegraph"}

    @staticmethod
    def input(node, name):
        return next((c for c in node if _tag(c) == "input" and c.get("name") == name), None)

    @staticmethod
    def _find(scope, name):
        if scope is None:
            return None
        return next((c for c in scope if _tag(c) not in ("input", "output") and c.get("name") == name), None)

    def _target(self, inp):
        """The node an input points at (in its own graph by nodename, or a nodegraph's output)."""
        graph, output, name = inp.get("nodegraph", ""), inp.get("output", ""), inp.get("nodename", "")
        g = self.graphs.get(graph) if graph else None
        if g is not None:
            outs = [c for c in g if _tag(c) == "output"]
            o = next((c for c in outs if c.get("name") == output), None)
            if o is None and not output and len(outs) == 1:
                o = outs[0]                      # a graph with one output may leave it unnamed
            return self._find(g, o.get("nodename", "")) if o is not None else None
        if not name:
            return None
        scope = self.parent.get(self.parent.get(inp))   # the graph (or document) holding this node
        found = self._find(scope, name)
        return found if found is not None else self._find(self.root, name)

    def number(self, inp):
        if inp is None:
            return 0.0
        s = self.scalar(inp)
        return s.value if s is not None and s.is_const else 1.0

    def scalar(self, inp, depth=0):
        if inp is None or depth > 24:
            return None
        if _plain(inp):
            return _Signal.const(_num(inp.get("value")))
        n = self._target(inp)
        return None if n is None else self._node(n, depth + 1)

    def _node(self, n, depth):
        kind = _tag(n)
        get = lambda name: self.scalar(self.input(n, name), depth)    # noqa: E731
        if kind == "constant":
            return get("value")
        if kind in ("image", "tiledimage"):
            f = self._file(n)
            return None if f is None else _Signal(f, scale=self._scale(n))
        if kind == "extract":
            src = get("in")
            if src is None or src.is_const:
                return src
            index = self.input(n, "index")
            src.channel = int(_num(index.get("value") if index is not None else "0"))
            return src
        if kind == "clamp":
            s = get("in")
            if s is None:
                return None
            low = get("low") if self.input(n, "low") is not None else None
            high = get("high") if self.input(n, "high") is not None else None
            lo = low.value if low is not None else 0.0
            hi = high.value if high is not None else 1.0
            s.min = max(lo, min(hi, s.min))
            s.max = max(lo, min(hi, s.max))
            return s
        if kind == "mix":
            fg, bg, m = get("fg"), get("bg"), get("mix")
            if m is None:
                return fg if fg is not None else bg
            if m.is_const:
                k = m.value
                if k <= 0.001 or fg is None:
                    return bg if bg is not None else fg
                if k >= 0.999 or bg is None:
                    return fg
                if fg.is_const and bg.is_const:
                    return _Signal.const(bg.value + k * (fg.value - bg.value))
                if not fg.is_const and bg.is_const:
                    fg.min = bg.value + k * (fg.min - bg.value)
                    fg.max = bg.value + k * (fg.max - bg.value)
                    return fg
                if fg.is_const and not bg.is_const:
                    bg.min += k * (fg.value - bg.min)
                    bg.max += k * (fg.value - bg.max)
                    return bg
                return fg if k >= 0.5 else bg
            # the texture drives the blend between two values: a remap of that texture
            if fg is not None and bg is not None and fg.is_const and bg.is_const:
                a, b = bg.value, fg.value
                m.min, m.max = a + m.min * (b - a), a + m.max * (b - a)
                return m
            if fg is not None and not fg.is_const:
                return fg
            if bg is not None and not bg.is_const:
                return bg
            return m
        if kind in ("max", "min"):
            # layered graphs: keep the first textured layer (the base)
            a, b = get("in1"), get("in2")
            if a is not None and b is not None and a.is_const and b.is_const:
                return _Signal.const(max(a.value, b.value) if kind == "max" else min(a.value, b.value))
            if a is not None and not a.is_const:
                return a
            return b if b is not None else a
        if kind in ("multiply", "add", "subtract"):
            a, b = get("in1"), get("in2")
            if a is None or b is None:
                return a if a is not None else b
            op = {"multiply": lambda x, y: x * y, "add": lambda x, y: x + y,
                  "subtract": lambda x, y: x - y}[kind]
            if a.is_const and b.is_const:
                return _Signal.const(op(a.value, b.value))
            tex, c = (b, a) if a.is_const else (a, b)
            if not c.is_const:
                return tex
            if kind == "subtract" and a.is_const:
                tex.min, tex.max = c.value - tex.min, c.value - tex.max
                return tex
            tex.min, tex.max = op(tex.min, c.value), op(tex.max, c.value)
            return tex
        if kind in ("convert", "dot", "swizzle"):
            return get("in")
        return None

    def color(self, inp, guard=0):
        """A colour input: (texture file, None, its UV scale), or (None, linear RGB, 1)
        for a constant, or (None, None, 1)."""
        if inp is None or guard > 16:
            return None, None, 1.0
        if _plain(inp):
            return None, _rgb(inp.get("value")), 1.0
        n = self._target(inp)
        if n is None:
            return None, None, 1.0
        kind = _tag(n)
        if kind in ("image", "tiledimage"):
            return self._file(n), None, self._scale(n)
        if kind == "constant":
            return self.color(self.input(n, "value"), guard + 1)
        if kind == "mix":
            k = self.scalar(self.input(n, "mix"))
            side = self.input(n, "bg" if k is not None and k.is_const and k.value < 0.5 else "fg")
            return self.color(side if side is not None else self.input(n, "fg"), guard + 1)
        if kind in ("multiply", "convert", "srgb_texture_to_lin_rec709"):
            first = self.input(n, "in1")
            return self.color(first if first is not None else self.input(n, "in"), guard + 1)
        return None, None, 1.0

    def normal(self, inp):
        """(normal map file, its UV scale), through normalmap and the like; (None, 1) if none."""
        n = self._target(inp) if inp is not None else None
        for _ in range(8):
            if n is None:
                break
            if _tag(n) in ("image", "tiledimage"):
                return self._file(n), self._scale(n)
            nxt = self.input(n, "in")
            n = self._target(nxt) if nxt is not None else None
        return None, 1.0

    def _file(self, image):
        inp = self.input(image, "file")
        value = inp.get("value") if inp is not None else None
        if not value:
            return None
        name = os.path.basename(value.replace("\\", "/"))
        if name in self.files:
            return self.files[name]
        low = name.lower()                       # names differ in case sometimes
        return next((p for k, p in self.files.items() if k.lower() == low), None)

    def _scale(self, image):
        """The UV scale of an image: texcoord multiplied by a constant (or a tiledimage's uvtiling)."""
        if _tag(image) == "tiledimage":
            tiling = self.input(image, "uvtiling")
            if tiling is not None and _plain(tiling) and _num(tiling.get("value")) > 0:
                return _num(tiling.get("value"))
        tc = self.input(image, "texcoord")
        n = self._target(tc) if tc is not None else None
        if n is None or _tag(n) != "multiply":
            return 1.0
        s = self.scalar(self.input(n, "in2"))
        return s.value if s is not None and s.is_const and s.value > 0 else 1.0
