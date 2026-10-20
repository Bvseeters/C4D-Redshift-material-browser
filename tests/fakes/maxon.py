"""A tiny stand-in for Cinema 4D's maxon module: just enough of the Nodes API for
mbrs.rsmat, with Redshift's node/port ids, so graph building can be checked
outside Cinema 4D. Connections and values are recorded on GRAPHS."""

GRAPHS = []
CORE = "com.redshift3d.redshift4c4d.nodes.core."
OUT = "com.redshift3d.redshift4c4d.node.output"

# ports each node type has. Std/texture/output from Maxon's SDK examples and the
# Redshift-API library; bump/displacement use Redshift's core parameter names.
NODES = {
    OUT: (["surface", "displacement", "volume"], []),
    CORE + "standardmaterial": (["base_color", "base_color_weight", "metalness", "refl_roughness",
                                 "refl_isglossiness", "opacity_color", "emission_color",
                                 "emission_weight", "bump_input"], ["outcolor"]),
    CORE + "texturesampler": (["tex0", "scale", "offset"], ["outcolor", "outalpha"]),
    CORE + "bumpmap": (["input", "inputtype", "scale", "flipy"], ["out"]),
    CORE + "displacement": (["texmap", "scale"], ["out"]),
    CORE + "rscolorsplitter": (["input"], ["outr", "outg", "outb", "outa"]),
    # Math Float "Change Range": ids from Maxon's Cinema 4D node reference
    CORE + "rsmathrange": (["input", "old_min", "old_max", "new_min", "new_max", "clamp"], ["out"]),
}


class Id(str):
    def __new__(cls, s=""):
        return str.__new__(cls, s)


class Url(str):
    pass


class String(str):
    pass


class Int32(int):
    pass


class Color(tuple):
    def __new__(cls, r=0.0, g=0.0, b=0.0):
        return tuple.__new__(cls, (r, g, b))


class Vector2d(tuple):
    def __new__(cls, x=0.0, y=0.0):
        return tuple.__new__(cls, (x, y))


class DataDictionary(dict):
    def Set(self, k, v):
        self[k] = v


class _Nodes(object):
    UndoMode = "undomode"

    class UNDO_MODE(object):
        ADD = 1


nodes = _Nodes()


class Port(object):
    def __init__(self, node, pid, valid=True, output=False):
        self.node, self.pid, self.valid, self.output = node, pid, valid, output
        self.value = None
        self.subs = {}
        if pid.endswith(".tex0"):
            self.subs = {"path": Port(node, "path"), "colorspace": Port(node, "colorspace")}

    def IsValid(self):
        return self.valid

    def GetId(self):
        return Id(self.pid)

    def FindChild(self, name):
        return self.subs.get(name, Port(self.node, name, valid=False))

    def SetPortValue(self, v):
        if not self.valid:
            raise RuntimeError("set on invalid port " + self.pid)
        self.value = v
        self.node.graph.values.append((self.node.label(), self.pid.split(".")[-1], v))

    def Connect(self, other):
        assert self.valid and other.valid, "connect invalid port"
        assert self.output and not other.output, "must connect output -> input"
        self.node.graph.wires.append((self.node.label(), self.pid.split(".")[-1],
                                      other.node.label(), other.pid.split(".")[-1]))


class PortList(object):
    def __init__(self, ports):
        self.ports = ports

    def FindChild(self, pid):
        for p in self.ports:
            if p.pid == str(pid):
                return p
        return Port(None, str(pid), valid=False)

    def GetChildren(self):
        return list(self.ports)


class Node(object):
    def __init__(self, graph, type_id):
        self.graph, self.type = graph, type_id
        ins, outs = NODES[type_id]
        self.name = type_id.split(".")[-1]
        self.inputs = PortList([Port(self, type_id + "." + p) for p in ins])
        self.outputs = PortList([Port(self, type_id + "." + p, output=True) for p in outs])

    def label(self):
        return self.name

    def IsValid(self):
        return True

    def GetInputs(self):
        return self.inputs

    def GetOutputs(self):
        return self.outputs

    def SetValue(self, attr, v):
        if attr == "net.maxon.node.base.name":
            self.name = "%s<%s>" % (self.type.split(".")[-1], v)


class Transaction(object):
    def __init__(self, g):
        self.g = g

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def Commit(self):
        self.g.committed = True


class Graph(object):
    def __init__(self):
        self.nodes, self.wires, self.values, self.committed = [], [], [], False
        GRAPHS.append(self)

    def IsNullValue(self):
        return False

    def BeginTransaction(self, user=None):
        return Transaction(self)

    def AddChild(self, child_id, node_id, args=None):
        if str(node_id) not in NODES:
            raise RuntimeError("unknown node " + node_id)
        n = Node(self, str(node_id))
        self.nodes.append(n)
        return n
