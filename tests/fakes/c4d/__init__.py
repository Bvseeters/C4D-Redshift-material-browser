"""Minimal stand-in for Cinema 4D's c4d package (only what the plugin touches)."""
import maxon as _maxon
from . import gui, bitmaps, storage, documents, plugins  # noqa: F401

Mmaterial, Ttexture, UNDOTYPE_NEWOBJ = 5703, 5616, 1
TEXTURETAG_MATERIAL, TEXTURETAG_PROJECTION, TEXTURETAG_PROJECTION_UVW = 1010, 1011, 6
FIRST_POPUP_ID, MOUSEPOS = 900000, -1
IMAGERESULT_OK = 1
EVENTS = []


class Vector(object):
    def __init__(self, x=0.0, y=None, z=None):
        self.x, self.y, self.z = x, x if y is None else y, x if z is None else z

    def __mul__(self, s):
        return Vector(self.x * s, self.y * s, self.z * s)

    def __truediv__(self, s):
        return Vector(self.x / s, self.y / s, self.z / s)


class BaseContainer(dict):
    def SetString(self, k, v):
        self[k] = v

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
