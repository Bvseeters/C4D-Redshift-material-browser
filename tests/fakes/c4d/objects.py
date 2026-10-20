"""Fake scene objects: just enough of BaseObject / PolygonObject / tags for c4dscene."""


class Tag(dict):
    """Parameters by id (tag[...] = ...), plus a name."""

    def __init__(self, kind=0):
        dict.__init__(self)
        self.kind, self.name = kind, ""

    def SetName(self, n): self.name = n
    def GetName(self): return self.name


class UVWTag(Tag):
    def __init__(self, count):
        Tag.__init__(self, "uvw")
        self.uvs = [None] * count

    def SetSlow(self, i, a, b, c, d): self.uvs[i] = (a, b, c, d)
    def GetSlow(self, i):
        a, b, c, d = self.uvs[i]
        return {"a": a, "b": b, "c": c, "d": d}


class BaseSelect(object):
    def __init__(self): self.sel = set()
    def Select(self, i): self.sel.add(i); return True
    def IsSelected(self, i): return i in self.sel
    def GetCount(self): return len(self.sel)


class SelectionTag(Tag):
    def __init__(self, kind):
        Tag.__init__(self, kind)
        self.bs = BaseSelect()

    def GetBaseSelect(self): return self.bs


class CPolygon(object):
    def __init__(self, a, b, c, d=None):
        self.a, self.b, self.c, self.d = a, b, c, c if d is None else d


class BaseObject(object):
    def __init__(self, kind):
        from . import Matrix
        self.kind, self.name, self.tags, self.children, self.parent = kind, "", [], [], None
        self.ml, self.data = Matrix(), {}

    def SetName(self, n): self.name = n
    def GetName(self): return self.name
    def IsAlive(self): return True
    def Message(self, *a): return True
    def InsertTag(self, t): self.tags.append(t)
    def MakeTag(self, kind):
        t = Tag(kind)
        self.tags.append(t)
        return t
    def GetTags(self): return list(self.tags)
    def SetMl(self, m): self.ml = m
    def GetMl(self): return self.ml
    def InsertUnderLast(self, parent):
        self.parent = parent
        parent.children.append(self)
    def GetChildren(self): return list(self.children)
    def __setitem__(self, k, v): self.data[k] = v
    def __getitem__(self, k): return self.data.get(k)

    def walk(self):
        yield self
        for c in self.children:
            for o in c.walk():
                yield o


class PolygonObject(BaseObject):
    def __init__(self, pcnt, vcnt):
        from . import Opolygon
        BaseObject.__init__(self, Opolygon)
        self.points, self.polys = [None] * pcnt, [None] * vcnt

    def SetAllPoints(self, pts):
        assert len(pts) == len(self.points), "point count"
        self.points = list(pts)
    def GetAllPoints(self): return list(self.points)
    def SetPolygon(self, i, p): self.polys[i] = p
    def GetPolygon(self, i): return self.polys[i]
    def GetPointCount(self): return len(self.points)
    def GetPolygonCount(self): return len(self.polys)
