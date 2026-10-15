import c4d


class GeUserArea(object):
    def __init__(self):
        self.drawn = []

    def GetWidth(self): return 700
    def LayoutChanged(self): pass
    def Redraw(self): self.drawn = []; self.DrawMsg(0, 0, 700, 3000, None)
    def OffScreenOn(self): pass
    def SetClippingRegion(self, *a): pass
    def GetColorRGB(self, cid): return {"r": 51, "g": 51, "b": 51}
    def DrawSetPen(self, c): pass
    def DrawRectangle(self, *a): self.drawn.append(("rect",) + a)
    def DrawSetTextCol(self, *a): pass
    def DrawText(self, t, x, y, *a): self.drawn.append(("text", t, x, y))
    def DrawGetTextWidth(self, t): return 7 * len(t)
    def DrawBitmap(self, *a): self.drawn.append(("bitmap",) + a[1:5])
    def Global2Local(self): return {"x": 0, "y": 0}


class GeDialog(object):
    def __init__(self):
        self.v, self.children, self.enabled, self.log = {}, {}, {}, []

    def __getattr__(self, name):          # layout calls just get logged
        def call(*a, **k):
            self.log.append(name)
            return True
        return call

    def AddChild(self, gid, sub, s): self.children.setdefault(gid, []).append((sub, s))
    def FreeChildren(self, gid): self.children[gid] = []
    def SetInt32(self, gid, v, *a): self.v[gid] = v
    def GetInt32(self, gid): return self.v.get(gid, 0)
    def SetString(self, gid, v): self.v[gid] = v
    def GetString(self, gid): return self.v.get(gid, "")
    def SetBool(self, gid, v): self.v[gid] = v
    def GetBool(self, gid): return self.v.get(gid, False)
    def SetFloat(self, gid, v, *a): self.v[gid] = v
    def GetFloat(self, gid): return self.v.get(gid, 0.0)
    def Enable(self, gid, on): self.enabled[gid] = on
    def Message(self, msg, result): return False

    # the scroll group: what's in view, in the grid's coordinates (tests move it)
    visible = None
    def GetVisibleArea(self, gid):
        return dict(self.visible or {"x1": 0, "y1": 0, "x2": 700, "y2": 600})
    def SetVisibleArea(self, gid, x1, y1, x2, y2):
        self.visible = {"x1": 0, "y1": 0, "x2": 700, "y2": 600}

    def AddCustomGui(self, gid, kind, name, flags, w, h, bc):
        self.quicktab = QuickTab()
        return self.quicktab


class QuickTab(object):
    def __init__(self): self.tabs, self.sel = [], set()
    def ClearStrings(self): self.tabs, self.sel = [], set()
    def AppendString(self, i, s, selected):
        self.tabs.append((i, s))
        if selected: self.sel = {i}
    def IsSelected(self, i): return i in self.sel
    def Select(self, i, on): self.sel = {i} if on else self.sel - {i}


POPUP_CHOICE = [0]
MESSAGES = []
INPUT_ANSWER = [None]


def MessageDialog(text, type=0):
    MESSAGES.append(text)
    return True


def InputDialog(title, preset=""):
    return INPUT_ANSWER[0]

QUESTION_ANSWER = [False]
QUESTIONS = []


def QuestionDialog(text):
    QUESTIONS.append(text)
    return QUESTION_ANSWER[0]


def ShowPopupDialog(cd=None, bc=None, x=0, y=0):
    return POPUP_CHOICE[0]
