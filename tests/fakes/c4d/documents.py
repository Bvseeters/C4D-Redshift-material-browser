class FakeObject(object):
    def __init__(self, name):
        self.name, self.tags = name, []

    def IsAlive(self): return True

    def MakeTag(self, kind):
        t = {}
        self.tags.append(t)
        return t


class FakeDoc(object):
    def __init__(self):
        self.materials, self.undo, self.selection = [], [], [FakeObject("Cube"), FakeObject("Plane")]

    def InsertMaterial(self, m): self.materials.append(m)
    def AddUndo(self, kind, x): self.undo.append(x)
    def StartUndo(self): self.undo.append("start")
    def EndUndo(self): self.undo.append("end")
    def GetActiveObjects(self, flags): return list(self.selection)


DOC = FakeDoc()

def GetActiveDocument(): return DOC
