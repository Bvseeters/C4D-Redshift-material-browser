"""Minimal stand-in for Cinema 4D's c4d package (only what the plugin touches)."""
import maxon as _maxon
from . import gui, bitmaps, storage, documents, plugins  # noqa: F401

Mmaterial, Ttexture, UNDOTYPE_NEWOBJ = 5703, 5616, 1
TEXTURETAG_MATERIAL, TEXTURETAG_PROJECTION, TEXTURETAG_PROJECTION_UVW = 1010, 1011, 6
FIRST_POPUP_ID, MOUSEPOS = 900000, -1
IMAGERESULT_OK = 1
EVENTS = []
Onull, Opolygon, Oinstance, Tphong, Tpolygonselection = 5140, 5100, 5126, 5612, 5673
CLIPBOARD = [""]


def GetStringFromClipboard():
    return CLIPBOARD[0]


class Vector(object):
    def __init__(self, x=0.0, y=None, z=None):
        self.x, self.y, self.z = x, x if y is None else y, x if z is None else z

    def __mul__(self, s):
        return Vector(self.x * s, self.y * s, self.z * s)

    def __truediv__(self, s):
        return Vector(self.x / s, self.y / s, self.z / s)

    def __add__(self, o):
        return Vector(self.x + o.x, self.y + o.y, self.z + o.z)

    def __repr__(self):
        return "Vector(%g, %g, %g)" % (self.x, self.y, self.z)


class Matrix(object):
    def __init__(self, off=None, v1=None, v2=None, v3=None):
        self.off = off or Vector(0.0)
        self.v1 = v1 or Vector(1.0, 0.0, 0.0)
        self.v2 = v2 or Vector(0.0, 1.0, 0.0)
        self.v3 = v3 or Vector(0.0, 0.0, 1.0)


from .objects import BaseObject, BaseSelect, CPolygon, PolygonObject, SelectionTag, UVWTag  # noqa: E402,F401


class BaseContainer(dict):
    def __init__(self, id=0):
        dict.__init__(self)
        self.id = id

    def GetId(self):
        return self.id

    def SetString(self, k, v):
        self[k] = v

    def GetInt32(self, k, default=0):
        return self.get(k, default)

    SetBool = SetInt32 = SetString


class _NodeMaterial(object):
    def CreateEmptyGraph(self, space):
        assert str(space) == "com.redshift3d.redshift4c4d.class.nodespace"
        return _maxon.Graph()


class BaseMaterial(object):
    def __init__(self, kind):
        self.name, self.ref = "", _NodeMaterial()

    def SetName(self, n):
        self.name = n

    def GetNodeMaterialReference(self):
        return self.ref


def EventAdd(*a):
    EVENTS.append("EventAdd")


def CallCommand(cid):
    EVENTS.append(("CallCommand", cid))


def __getattr__(name):          # any other constant: a stable fake int
    if name.isupper() or name[:1].isupper():
        return abs(hash(name)) % 100000 + 1
    raise AttributeError(name)
