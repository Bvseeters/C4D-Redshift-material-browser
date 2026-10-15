"""The BasPBR Browser window (Cinema 4D only).

Tabs: Everything (every site at once), ambientCG, Poly Haven, ShareTextures,
FreePBR, and Installed (the library folder - possibly a share several PCs use).
Under them: search, sort, resolution, the library folder and material options;
on the left the categories, on the right a thumbnail grid that keeps loading as
you scroll (Ctrl + mouse wheel or the Size slider resizes the thumbnails).

Click selects (Ctrl-click adds), double-click or "Create material" builds the
Redshift material - downloading it first unless it's in the library already.
Right-click for more. Network work runs in baspbr.jobs on worker threads; the
dialog's timer picks the results up on the main thread, where materials get
created. The first time the browser opens in a Cinema 4D session it also asks
GitHub whether there's a newer version.
"""
import collections
import os
import threading
import time
import traceback
import webbrowser

import c4d
from c4d import gui

from . import library as libmod
from . import jobs as jobsmod
from . import rsmat, settings, sources, updater, version

CHUNK = 60                      # thumbnails added each time the grid nears its end

# gadget ids
ID_TABS = 1000
ID_QUERY, ID_SEARCH, ID_SORT, ID_RES = 1002, 1003, 1004, 1005
ID_ASSIGN, ID_DISP, ID_DISP_SCALE, ID_BUMP_HEIGHT = 1010, 1011, 1012, 1013
ID_SCROLL, ID_GRID, ID_CAT_SCROLL, ID_CATS = 1020, 1021, 1022, 1023
ID_COUNT, ID_SIZE, ID_STATUS, ID_CANCEL, ID_CREATE = 1031, 1032, 1033, 1034, 1035
ID_LICENCE = 1036
ID_LIBRARY, ID_LIB_BROWSE, ID_LIB_OPEN, ID_LIB_RESCAN, ID_LIB_INFO = 1040, 1041, 1042, 1043, 1044
ID_MENU_FOLDER, ID_MENU_OPEN, ID_MENU_CHECK, ID_MENU_REFRESH = 1100, 1101, 1102, 1103
ID_MENU_UPDATE, ID_MENU_TOKEN, ID_MENU_ABOUT = 1110, 1111, 1112

# popup menu ids (must be >= FIRST_POPUP_ID)
POP_CREATE = c4d.FIRST_POPUP_ID + 1
POP_PAGE = c4d.FIRST_POPUP_ID + 2
POP_FOLDER = c4d.FIRST_POPUP_ID + 3

TILE_MIN, TILE_MAX, TILE_STEP = 64, 320, 16
CAPTION, GAP = 18, 8
SIDEBAR_W, ROW_H = 190, 20

_bitmaps = collections.OrderedDict()   # asset key -> BaseBitmap, most recently drawn last
MAX_BITMAPS = 1500                      # ~300 MB of 256px thumbnails; older ones reload when seen again


def _rgb(area, color_id, fallback):
    try:
        d = area.GetColorRGB(color_id)
        return c4d.Vector(d["r"], d["g"], d["b"]) / 255.0
    except Exception:
        return fallback


def _remember(key, bmp, jobs):
    _bitmaps[key] = bmp
    _bitmaps.move_to_end(key)
    while len(_bitmaps) > MAX_BITMAPS:
        old, _ = _bitmaps.popitem(last=False)
        jobs.forget_thumb(old)                  # asked for again if it scrolls back into view


def _fit(area, text, width):
    while text and area.DrawGetTextWidth(text) > width:
        text = text[:-2] + "…" if len(text) > 2 else ""
    return text


# =====================================================================================
class Categories(gui.GeUserArea):
    """The sidebar. On the Everything tab it starts with a tick box per site (which
    sites to search); then All and the categories with their counts."""
    SEP = 10                  # gap between the sites and the categories

    def __init__(self, dialog):
        super(Categories, self).__init__()
        self.dialog = dialog
        self.rows = [(sources.ALL, "All", None)]
        self.toggles = []         # [(site id, label, count or None, ticked)]
        self.selected = sources.ALL
        self.note = ""

    def set_rows(self, cats, selected, note="", toggles=None):
        self.rows = [(sources.ALL, "All", None)] + list(cats)
        if toggles is not None:
            self.toggles = list(toggles)
        keys = [r[0] for r in self.rows]
        self.selected = selected if selected in keys else sources.ALL
        self.note = note
        self.LayoutChanged()
        self.Redraw()

    def set_toggles(self, toggles):
        self.toggles = list(toggles)
        self.LayoutChanged()
        self.Redraw()

    # row positions
    def _top(self):
        return 3 + (len(self.toggles) * ROW_H + self.SEP if self.toggles else 0)

    def toggle_y(self, i):
        return 3 + i * ROW_H

    def row_y(self, i):
        return self._top() + i * ROW_H

    def GetMinSize(self):
        return SIDEBAR_W, self._top() + ROW_H * (len(self.rows) + (2 if self.note else 0)) + 6

    def DrawMsg(self, x1, y1, x2, y2, msg):
        self.OffScreenOn()
        self.SetClippingRegion(x1, y1, x2, y2)
        bg = _rgb(self, c4d.COLOR_BG, c4d.Vector(0.2))
        self.DrawSetPen(bg * 0.85)
        self.DrawRectangle(x1, y1, x2, y2)
        w = self.GetWidth() or SIDEBAR_W
        accent = c4d.Vector(0.98, 0.62, 0.16)

        def count_text(count, y):
            num = "" if count is None else str(count)
            if num:
                self.DrawSetTextCol(c4d.COLOR_TEXT_DISABLED, c4d.COLOR_TRANS)
                self.DrawText(num, w - self.DrawGetTextWidth(num) - 8, y + 2)
            return self.DrawGetTextWidth(num) if num else 0

        if self.toggles:
            self.DrawSetPen(bg * 0.72)                          # the sites, set apart
            self.DrawRectangle(0, 0, w, self._top() - self.SEP // 2)
        for i, (sid, label, count, on) in enumerate(self.toggles):
            y = self.toggle_y(i)
            self.DrawSetPen(c4d.Vector(0.12))                   # the tick box
            self.DrawRectangle(8, y + 4, 19, y + 15)
            self.DrawSetPen(accent if on else bg * 0.9)
            self.DrawRectangle(10, y + 6, 17, y + 13)
            numw = count_text(count, y)
            self.DrawSetTextCol(c4d.COLOR_TEXT if on else c4d.COLOR_TEXT_DISABLED, c4d.COLOR_TRANS)
            self.DrawText(_fit(self, label, w - numw - 40), 26, y + 2)
        for i, (key, label, count) in enumerate(self.rows):
            y = self.row_y(i)
            if y > y2 or y + ROW_H < y1:
                continue
            if key == self.selected:
                self.DrawSetPen(accent * 0.55)
                self.DrawRectangle(0, y, w, y + ROW_H - 1)
            elif key.startswith("src:"):
                self.DrawSetPen(bg * 0.72)                     # the source rows, set apart
                self.DrawRectangle(0, y, w, y + ROW_H - 1)
            numw = count_text(count, y)
            self.DrawSetTextCol(c4d.COLOR_TEXT, c4d.COLOR_TRANS)
            self.DrawText(_fit(self, label, w - numw - 22), 8, y + 2)
        if self.note:
            self.DrawSetTextCol(c4d.COLOR_TEXT_DISABLED, c4d.COLOR_TRANS)
            self.DrawText(_fit(self, self.note, w - 12), 8, self.row_y(len(self.rows) + 1))

    def InputEvent(self, msg):
        if msg[c4d.BFM_INPUT_DEVICE] != c4d.BFM_INPUT_MOUSE or msg[c4d.BFM_INPUT_CHANNEL] != c4d.BFM_INPUT_MOUSELEFT:
            return False
        local = self.Global2Local()
        y = msg[c4d.BFM_INPUT_Y] + local["y"]
        if self.toggles and y < self._top():
            i = int((y - 3) // ROW_H)
            if 0 <= i < len(self.toggles):
                solo = bool(msg.get(c4d.BFM_INPUT_QUALIFIER, 0) & c4d.QCTRL) if hasattr(msg, "get") \
                    else bool(msg[c4d.BFM_INPUT_QUALIFIER] & c4d.QCTRL)
                self.dialog.site_toggled(self.toggles[i][0], solo)
            return True
        i = int((y - self._top()) // ROW_H)
        if 0 <= i < len(self.rows) and self.rows[i][0] != self.selected:
            self.selected = self.rows[i][0]
            self.Redraw()
            self.dialog.category_changed(self.selected)
        return True


# =====================================================================================
class Grid(gui.GeUserArea):
    def __init__(self, dialog, tile=128):
        super(Grid, self).__init__()
        self.dialog = dialog
        self.items = []
        self.selected = set()
        self.tile = tile
        self.cols = 1

    @property
    def cell_w(self):
        return self.tile + GAP

    @property
    def cell_h(self):
        return self.tile + CAPTION + GAP

    # ---- layout ----------------------------------------------------------------------
    def _columns(self, width):
        return max(1, (max(width, self.cell_w) - GAP) // self.cell_w)

    def content_height(self):
        rows = (len(self.items) + self.cols - 1) // self.cols
        return rows * self.cell_h + GAP

    def GetMinSize(self):
        self.cols = self._columns(self.GetWidth() or 700)
        return self.cell_w + GAP, max(self.cell_h, self.content_height())

    def Sized(self, w, h):
        cols = self._columns(w)
        if cols != self.cols:
            self.cols = cols
            self.LayoutChanged()

    def set_items(self, items):
        self.items = list(items)
        self.selected = set()
        self.cols = self._columns(self.GetWidth() or 700)
        self.LayoutChanged()
        self.Redraw()

    def add_items(self, items):
        self.items.extend(items)
        self.LayoutChanged()
        self.Redraw()

    def set_tile(self, size):
        self.tile = size
        self.cols = self._columns(self.GetWidth() or 700)
        self.LayoutChanged()
        self.Redraw()

    def tile_rect(self, i):
        return GAP + (i % self.cols) * self.cell_w, GAP + (i // self.cols) * self.cell_h

    def index_at(self, x, y):
        col, row = (x - GAP) // self.cell_w, (y - GAP) // self.cell_h
        if col < 0 or row < 0 or col >= self.cols:
            return -1
        i = int(row * self.cols + col)
        tx, ty = self.tile_rect(i)
        if i >= len(self.items) or x > tx + self.tile or y > ty + self.tile + CAPTION:
            return -1
        return i

    # ---- drawing -------------------------------------------------------------------------
    def DrawMsg(self, x1, y1, x2, y2, msg):
        self.OffScreenOn()
        self.SetClippingRegion(x1, y1, x2, y2)
        bg = _rgb(self, c4d.COLOR_BG, c4d.Vector(0.2))
        self.DrawSetPen(bg)
        self.DrawRectangle(x1, y1, x2, y2)
        if not self.items:
            self.DrawSetTextCol(c4d.COLOR_TEXT, c4d.COLOR_TRANS)
            self.DrawText(self.dialog.empty_text, GAP * 2, GAP * 2)
            return
        T = self.tile
        tile_bg = bg * 0.75
        accent = c4d.Vector(0.98, 0.62, 0.16)
        d = self.dialog
        show_local = d.tab != "installed"
        show_source = d.tab == "everything"
        first = max(0, int((y1 - GAP) // self.cell_h) * self.cols)
        last = min(len(self.items), (int((y2 - GAP) // self.cell_h) + 1) * self.cols)
        for i in range(first, last):
            asset = self.items[i]
            x, y = self.tile_rect(i)
            self.DrawSetPen(tile_bg)
            self.DrawRectangle(x, y, x + T - 1, y + T - 1)
            bmp = _bitmaps.get(asset.key)
            if bmp is None:
                d.jobs.thumb(asset)                                 # loads in the background
            else:
                _bitmaps.move_to_end(asset.key)
                bw, bh = bmp.GetBw(), bmp.GetBh()
                s = min(T / float(bw or 1), T / float(bh or 1))
                w, h = int(bw * s), int(bh * s)
                self.DrawBitmap(bmp, x + (T - w) // 2, y + (T - h) // 2, w, h, 0, 0, bw, bh,
                                c4d.BMP_NORMALSCALED | c4d.BMP_ALLOWALPHA)
            state = d.state.get(asset.key)
            if show_local and asset.key in d.local:
                self.DrawSetPen(c4d.Vector(0.3, 0.8, 0.4))         # already in the library
                self.DrawRectangle(x + T - 12, y + 4, x + T - 5, y + 11)
            if show_source and T >= 96:                            # which site, in Everything
                tag = sources.SOURCES[asset.source].label
                tw = self.DrawGetTextWidth(tag) + 8
                self.DrawSetPen(c4d.Vector(0.08))
                self.DrawRectangle(x + 3, y + 3, x + 3 + tw, y + 3 + 15)
                self.DrawSetTextCol(c4d.COLOR_TEXT, c4d.COLOR_TRANS)
                self.DrawText(tag, x + 7, y + 3)
            if isinstance(state, float):
                self.DrawSetPen(c4d.Vector(0.1))
                self.DrawRectangle(x + 4, y + T - 10, x + T - 5, y + T - 5)
                self.DrawSetPen(accent)
                self.DrawRectangle(x + 4, y + T - 10, x + 4 + int((T - 9) * state), y + T - 5)
            elif state in ("queued", "waiting"):
                self.DrawSetPen(c4d.Vector(0.1) if state == "queued" else c4d.Vector(0.25, 0.45, 0.8))
                self.DrawRectangle(x + 4, y + T - 10, x + T - 5, y + T - 5)
            if i in self.selected or state == "error":
                self.DrawSetPen(accent if i in self.selected else c4d.Vector(0.9, 0.25, 0.2))
                for k in range(2):
                    self.DrawRectangle(x - 2 + k, y - 2 + k, x + T + 1 - k, y - 1 + k)
                    self.DrawRectangle(x - 2 + k, y + T + k, x + T + 1 - k, y + T + 1 + k)
                    self.DrawRectangle(x - 2 + k, y - 2 + k, x - 1 + k, y + T + 1 - k)
                    self.DrawRectangle(x + T + k, y - 2 + k, x + T + 1 + k, y + T + 1 - k)
            self.DrawSetTextCol(c4d.COLOR_TEXT, c4d.COLOR_TRANS)
            self.DrawText(_fit(self, asset.name, T), x, y + T + 2)

    # ---- input ---------------------------------------------------------------------------
    def InputEvent(self, msg):
        if msg[c4d.BFM_INPUT_DEVICE] != c4d.BFM_INPUT_MOUSE:
            return False
        channel = msg[c4d.BFM_INPUT_CHANNEL]
        if channel == c4d.BFM_INPUT_MOUSEWHEEL:
            if msg[c4d.BFM_INPUT_QUALIFIER] & c4d.QCTRL:           # Ctrl + wheel: thumbnail size
                self.dialog.wheel_resize(msg[c4d.BFM_INPUT_VALUE])
                return True
            return False                                        # plain wheel: let the view scroll
        local = self.Global2Local()
        x = msg[c4d.BFM_INPUT_X] + local["x"]
        y = msg[c4d.BFM_INPUT_Y] + local["y"]
        i = self.index_at(x, y)
        if channel == c4d.BFM_INPUT_MOUSELEFT:
            if i < 0:
                self.selected = set()
            elif msg[c4d.BFM_INPUT_QUALIFIER] & c4d.QCTRL:
                self.selected ^= {i}
            else:
                self.selected = {i}
            self.Redraw()
            self.dialog.selection_changed()
            if i >= 0 and msg[c4d.BFM_INPUT_DOUBLECLICK]:
                self.dialog.create([self.items[i]])
            return True
        if channel == c4d.BFM_INPUT_MOUSERIGHT and i >= 0:
            if i not in self.selected:
                self.selected = {i}
                self.Redraw()
                self.dialog.selection_changed()
            self.dialog.context_menu(self.items[i])
            return True
        return False

    def selected_assets(self):
        return [self.items[i] for i in sorted(self.selected) if i < len(self.items)]


# =====================================================================================
class BrowserDialog(gui.GeDialog):
    def __init__(self):
        super(BrowserDialog, self).__init__()
        self.cfg = settings.load()
        self.library = libmod.Library(self.cfg["library"])
        sources.SOURCES["installed"].library = self.library
        self.jobs = jobsmod.Jobs(self.library)
        tile = int(self.cfg.get("tile", 128))
        self.grid = Grid(self, max(TILE_MIN, min(TILE_MAX, tile)))
        self.cats = Categories(self)
        self.tabs = None
        self.tab = self.cfg.get("source") if self.cfg.get("source") in sources.TABS else sources.TABS[0]
        self.state = {}             # asset key -> float progress | "queued" | "waiting" | "error"
        self.local = set()          # source:id keys already in the library
        self.cat_cache = {}         # tab -> [(key, label, count)]
        self.results = []           # every match of the current search; the grid shows a growing part
        self.token = 0
        self.searching = False
        self.search_due = 0.0
        self.library_due = 0.0
        self.scanned = False
        self.make_thumbs = []       # (key, colour map) waiting for a thumbnail to be drawn
        self.empty_text = "Searching…"
        self.assets = {}            # key -> Asset (for results arriving later)
        self.manual_update_check = False
        self.cat_pending = set()    # tabs whose categories are being fetched
        self._wheel = (0.0, 0)      # last Ctrl+wheel step (time, direction)

    # ---- layout --------------------------------------------------------------------------
    def CreateLayout(self):
        self.SetTitle("BasPBR Browser")
        self.MenuFlushAll()
        self.MenuSubBegin("Library")
        self.MenuAddString(ID_MENU_FOLDER, "Choose library folder…")
        self.MenuAddString(ID_MENU_OPEN, "Open library folder")
        self.MenuAddString(ID_MENU_REFRESH, "Reload the sites' catalogues and rescan the library")
        self.MenuAddSeparator()
        self.MenuAddString(ID_MENU_CHECK, "Check Redshift node IDs (prints to the Console)")
        self.MenuSubEnd()
        self.MenuSubBegin("Help")
        self.MenuAddString(ID_MENU_UPDATE, "Check for updates")
        self.MenuAddString(ID_MENU_TOKEN, "GitHub token (private repository)…")
        self.MenuAddSeparator()
        self.MenuAddString(ID_MENU_ABOUT, "About BasPBR Browser")
        self.MenuSubEnd()
        self.MenuFinished()

        bc = c4d.BaseContainer()
        bc.SetBool(c4d.QUICKTAB_BAR, False)
        bc.SetBool(c4d.QUICKTAB_SHOWSINGLE, True)
        bc.SetBool(c4d.QUICKTAB_NOMULTISELECT, True)
        self.tabs = self.AddCustomGui(ID_TABS, c4d.CUSTOMGUI_QUICKTAB, "", c4d.BFH_SCALEFIT, 0, 0, bc)

        if self.GroupBegin(0, c4d.BFH_SCALEFIT, cols=7, rows=1):
            self.GroupBorderSpace(6, 4, 6, 2)
            self.AddEditText(ID_QUERY, c4d.BFH_SCALEFIT, 200)
            self.AddButton(ID_SEARCH, c4d.BFH_LEFT, name="Search")
            self.AddStaticText(0, c4d.BFH_LEFT, name=" Sort")
            self.AddComboBox(ID_SORT, c4d.BFH_LEFT, 100)
            self.AddStaticText(0, c4d.BFH_LEFT, name=" Resolution")
            self.AddComboBox(ID_RES, c4d.BFH_LEFT, 70)
            for n, r in enumerate(sources.RESOLUTIONS):
                self.AddChild(ID_RES, n, r)
        self.GroupEnd()

        if self.GroupBegin(0, c4d.BFH_SCALEFIT, cols=6, rows=1):
            self.GroupBorderSpace(6, 0, 6, 2)
            self.AddStaticText(0, c4d.BFH_LEFT, name="Library")
            self.AddEditText(ID_LIBRARY, c4d.BFH_SCALEFIT, 300)
            self.AddButton(ID_LIB_BROWSE, c4d.BFH_LEFT, 30, name="…")
            self.AddButton(ID_LIB_OPEN, c4d.BFH_LEFT, name="Open")
            self.AddButton(ID_LIB_RESCAN, c4d.BFH_LEFT, name="Rescan")
            self.AddStaticText(ID_LIB_INFO, c4d.BFH_LEFT, 220, name="")
        self.GroupEnd()

        if self.GroupBegin(0, c4d.BFH_SCALEFIT, cols=6, rows=1):
            self.GroupBorderSpace(6, 0, 6, 4)
            self.AddCheckbox(ID_ASSIGN, c4d.BFH_LEFT, 0, 0, "Assign to selected objects")
            self.AddCheckbox(ID_BUMP_HEIGHT, c4d.BFH_LEFT, 0, 0, "Bump from height if no normal map")
            self.AddCheckbox(ID_DISP, c4d.BFH_LEFT, 0, 0, "Displacement")
            self.AddStaticText(0, c4d.BFH_LEFT, name="Height")
            self.AddEditNumberArrows(ID_DISP_SCALE, c4d.BFH_LEFT, 80)
            self.AddStaticText(ID_LICENCE, c4d.BFH_SCALEFIT | c4d.BFH_RIGHT, name="")
        self.GroupEnd()

        if self.GroupBegin(0, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, cols=2, rows=1):
            self.ScrollGroupBegin(ID_CAT_SCROLL, c4d.BFH_LEFT | c4d.BFV_SCALEFIT,
                                  c4d.SCROLLGROUP_VERT | c4d.SCROLLGROUP_AUTOVERT, SIDEBAR_W + 16, 420)
            self.AddUserArea(ID_CATS, c4d.BFH_SCALEFIT | c4d.BFV_TOP, SIDEBAR_W)
            self.AttachUserArea(self.cats, ID_CATS)
            self.GroupEnd()
            self.ScrollGroupBegin(ID_SCROLL, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT,
                                  c4d.SCROLLGROUP_VERT | c4d.SCROLLGROUP_AUTOVERT, 600, 420)
            self.AddUserArea(ID_GRID, c4d.BFH_SCALEFIT | c4d.BFV_TOP)
            self.AttachUserArea(self.grid, ID_GRID)
            self.GroupEnd()
        self.GroupEnd()

        if self.GroupBegin(0, c4d.BFH_SCALEFIT, cols=6, rows=1):
            self.GroupBorderSpace(6, 4, 6, 6)
            self.AddStaticText(ID_COUNT, c4d.BFH_LEFT, 150, name="")
            self.AddStaticText(0, c4d.BFH_LEFT, name="Size")
            self.AddEditSlider(ID_SIZE, c4d.BFH_LEFT, 110)
            self.AddStaticText(ID_STATUS, c4d.BFH_SCALEFIT, name="")
            self.AddButton(ID_CANCEL, c4d.BFH_RIGHT, name="Cancel downloads")
            self.AddButton(ID_CREATE, c4d.BFH_RIGHT, 160, name="Create material")
        self.GroupEnd()
        return True

    def InitValues(self):
        cfg = self.cfg
        if self.tabs is not None:
            self.tabs.ClearStrings()
            for n, tid in enumerate(sources.TABS):
                self.tabs.AppendString(n, sources.SOURCES[tid].label, tid == self.tab)
        self.SetString(ID_QUERY, cfg.get("query", ""))
        self.SetString(ID_LIBRARY, self.library.root)
        res = cfg.get("resolution", "4K")
        self.SetInt32(ID_RES, sources.RESOLUTIONS.index(res) if res in sources.RESOLUTIONS else 2)
        self.SetBool(ID_ASSIGN, cfg.get("assign", True))
        self.SetBool(ID_DISP, cfg.get("displacement", False))
        self.SetBool(ID_BUMP_HEIGHT, cfg.get("bump_from_height", True))
        self.SetFloat(ID_DISP_SCALE, cfg.get("displacement_scale", 1.0), 0.0, 1000.0, 0.1, c4d.FORMAT_METER)
        self.SetInt32(ID_SIZE, self.grid.tile, TILE_MIN, TILE_MAX, TILE_STEP)
        self.SetString(ID_LIB_INFO, "reading the library…")
        self.jobs.scan()
        self._tab_changed(search=True)
        self.SetTimer(100)
        if not updater.CHECKED_THIS_SESSION:
            updater.CHECKED_THIS_SESSION = True
            self.check_updates(manual=False)
        return True

    # ---- state helpers ------------------------------------------------------------------------
    @property
    def source(self):
        return sources.SOURCES[self.tab]

    @property
    def resolution(self):
        return sources.RESOLUTIONS[self.GetInt32(ID_RES)]

    @property
    def sites(self):
        """The sites the Everything tab searches (the ticked ones); None on other tabs."""
        if self.tab != "everything":
            return None
        off = self.cfg.get("sites_off") or []
        return tuple(s for s in sources.Everything.MEMBERS if s not in off)

    def _cat_key(self):
        return self.tab if self.tab != "everything" else "everything:" + ",".join(self.sites)

    def _site_toggles(self):
        counts = sources.SOURCES["everything"].site_counts()
        on = self.sites
        return [(s, sources.SOURCES[s].label, counts.get(s), s in on) for s in sources.Everything.MEMBERS]

    def site_toggled(self, sid, solo=False):
        """A site's tick box on the Everything tab. Ctrl-click: only that site (again: all)."""
        members = sources.Everything.MEMBERS
        on = list(self.sites)
        if solo:
            on = list(members) if on == [sid] else [sid]
        elif sid in on:
            if len(on) == 1:
                self.status("At least one site has to stay ticked.")
                return
            on.remove(sid)
        else:
            on.append(sid)
        self.cfg["sites_off"] = [s for s in members if s not in on]
        self.cats.set_toggles(self._site_toggles())
        self._show_categories()
        self.search()

    def _show_categories(self):
        key = self._cat_key()
        if key in self.cat_cache:
            self.cats.set_rows(self.cat_cache[key], self.category)
        else:
            self.cats.set_rows([], self.category, "loading categories…")
            self._load_categories()

    @property
    def category(self):
        if not isinstance(self.cfg.get("category"), dict):
            self.cfg["category"] = {}
        return self.cfg["category"].get(self.tab, sources.ALL)

    def _current_tab(self):
        if self.tabs is None:
            return self.tab
        for n, tid in enumerate(sources.TABS):
            if self.tabs.IsSelected(n):
                return tid
        return self.tab

    def _tab_changed(self, search=True):
        src = self.source
        self.FreeChildren(ID_SORT)
        for n, s in enumerate(src.sorts):
            self.AddChild(ID_SORT, n, s)
        if not isinstance(self.cfg.get("sort"), dict):         # a v1 settings file
            self.cfg["sort"] = {}
        sort = self.cfg["sort"].get(src.id)
        self.SetInt32(ID_SORT, src.sorts.index(sort) if sort in src.sorts else 0)
        self.Enable(ID_SORT, len(src.sorts) > 1)
        installed = self.tab == "installed"
        self.Enable(ID_RES, not src.fixed_resolution and not installed)
        if installed:
            self.SetString(ID_LICENCE, "In the library folder — no download needed")
        else:
            extra = ""
            if src.fixed_resolution:
                extra = "  ·  %s only" % src.fixed_resolution
            elif src.best_resolution != "8K" and src.id != "everything":
                extra = "  ·  up to %s" % src.best_resolution
            self.SetString(ID_LICENCE, src.licence + extra)
        self.cats.set_toggles(self._site_toggles() if self.tab == "everything" else [])
        if installed:
            self._installed_categories()
            if self.scanned:
                self.jobs.scan()             # others may have added materials since
        else:
            self._show_categories()
        if search:
            self.search()

    def _load_categories(self):
        key = self._cat_key()
        if key not in self.cat_pending:
            self.cat_pending.add(key)
            self.jobs.categories(self.tab, self.sites, key)

    def _categories_result(self, key, cats, error):
        self.cat_pending.discard(key)
        if not error:
            self.cat_cache[key] = cats
        if key != self._cat_key():
            return
        if self.tab == "everything":
            self.cats.set_toggles(self._site_toggles())      # the counts are known now
        self.cats.set_rows(cats, self.category, ("categories unavailable: " + error) if error else "")
        if not error and self.category != sources.ALL and self.category not in [c[0] for c in cats]:
            self.cfg["category"][self.tab] = sources.ALL       # a category this site no longer has
            self.search()

    def _installed_categories(self):
        try:
            rows = self.source.categories()
        except Exception:
            rows = []
        self.cats.set_rows(rows, self.category, "" if self.scanned else "reading the library…")

    def _sort(self):
        sorts = self.source.sorts
        i = self.GetInt32(ID_SORT)
        return sorts[i] if 0 <= i < len(sorts) else sorts[0]

    def status(self, text):
        self.SetString(ID_STATUS, text)

    def options(self):
        return rsmat.Options(displacement=self.GetBool(ID_DISP),
                             displacement_scale=self.GetFloat(ID_DISP_SCALE),
                             bump_from_height=self.GetBool(ID_BUMP_HEIGHT))

    def save(self):
        cfg = self.cfg
        cfg["source"] = self.tab
        cfg["query"] = self.GetString(ID_QUERY)
        cfg["sort"][self.tab] = self._sort()
        cfg["resolution"] = self.resolution
        cfg["assign"] = self.GetBool(ID_ASSIGN)
        cfg["displacement"] = self.GetBool(ID_DISP)
        cfg["displacement_scale"] = self.GetFloat(ID_DISP_SCALE)
        cfg["bump_from_height"] = self.GetBool(ID_BUMP_HEIGHT)
        cfg["library"] = self.library.root
        cfg["tile"] = self.grid.tile
        settings.save(cfg)

    def wheel_resize(self, delta):
        """Ctrl + mouse wheel. The grid and the dialog may both see the same turn of the
        wheel; one step per notch either way."""
        step = TILE_STEP if delta > 0 else -TILE_STEP
        now = time.time()
        if now - self._wheel[0] < 0.01 and self._wheel[1] == step:
            return
        self._wheel = (now, step)
        self.set_tile_size(self.grid.tile + step)

    def set_tile_size(self, size):
        size = max(TILE_MIN, min(TILE_MAX, int(size)))
        if size == self.grid.tile:
            return
        self.grid.set_tile(size)
        self.SetInt32(ID_SIZE, size, TILE_MIN, TILE_MAX, TILE_STEP)
        self.cfg["tile"] = size

    # ---- the library folder ----------------------------------------------------------------------
    def set_library(self, path, ask_unc=True):
        path = (path or "").strip().strip('"')
        if not path:
            return
        unc = libmod.unc_path(path)
        if unc and ask_unc and gui.QuestionDialog(
                "%s is the network share\n%s\n\nUse the network path, so the materials' texture paths "
                "work on every PC (whatever drive letter they use)?" % (path[:2], unc[:len(unc) - len(path) + 2])):
            path = unc
        if os.path.normcase(path) == os.path.normcase(self.library.root):
            self.SetString(ID_LIBRARY, path)
            return
        self.library = libmod.Library(path)
        self.jobs.library = self.library
        sources.SOURCES["installed"].library = self.library
        sources.SOURCES["installed"]._all = None
        self.jobs.forget_thumbs()
        self.local = set()
        self.scanned = False
        self.SetString(ID_LIBRARY, path)
        self.SetString(ID_LIB_INFO, "reading the library…")
        self.save()
        self.jobs.scan()
        if self.tab == "installed":
            self._tab_changed()
        self.grid.Redraw()
        self.status("Library: " + path)

    def _scanned(self, entries, error):
        self.scanned = True
        self.local = self.library.index(entries)
        lib = self.library
        if error or not os.path.isdir(lib.root):
            info = "not reachable" if not os.path.isdir(lib.root) else error
        else:
            shared = lib.root.startswith("\\\\") or bool(libmod.unc_path(lib.root))
            info = "%d material%s%s%s" % (len(entries), "" if len(entries) == 1 else "s",
                                          " · network" if shared else "",
                                          "" if lib.writable() else " · read-only")
        self.SetString(ID_LIB_INFO, info)
        if self.tab == "installed":
            self._installed_categories()
            self.search()
        self.grid.Redraw()

    # ---- searching and endless scrolling -------------------------------------------------------
    def search(self):
        self.token += 1
        self.search_due = 0.0
        self.results = []
        self.searching = True
        src = self.source
        if self.tab == "installed":
            self.empty_text = ("Nothing in the library yet — download something, or point Library at a "
                               "folder that has materials.") if self.scanned else "Reading the library…"
            if not self.scanned:
                self.grid.set_items([])
                return
        elif not (src.loaded(self.sites) if self.tab == "everything" else src.loaded()):
            self.empty_text = ("Loading every site's catalogue (first time today, a few seconds)…"
                               if self.tab == "everything" else
                               "Loading the %s catalogue (first time today)…" % src.label)
        else:
            self.empty_text = "Searching…"
        self.grid.set_items([])
        self.SetString(ID_COUNT, "")
        self.jobs.search(self.token, self.tab, self.GetString(ID_QUERY).strip(), self._sort(), self.category,
                         self.sites)

    def _search_result(self, token, assets, error):
        if token != self.token:
            return                       # an older search finishing late
        self.searching = False
        self.results = assets
        if error:
            self.empty_text = "Search failed: " + error
            self.status("Search failed: " + error)
        elif self.tab != "installed":
            self.empty_text = "Nothing found."
        if self.tab == "everything":
            failed = getattr(self.source, "failed", None)
            if failed:
                self.status("Couldn't reach: " + ", ".join(failed))
        self.grid.set_items([])
        self.SetVisibleArea(ID_SCROLL, 0, 0, 10, 10)
        self._show_more()
        if self._cat_key() not in self.cat_cache and self.tab != "installed":
            self._load_categories()               # (again, if it failed) - instant now the catalogue is loaded

    def _show_more(self):
        """Hand the grid the next stretch of results (the endless scroll)."""
        start = len(self.grid.items)
        if start >= len(self.results):
            self._count()
            return
        more = self.results[start:start + max(CHUNK, self.grid.cols * 6)]
        for a in more:
            self.assets[a.key] = a
        self.grid.add_items(more)                 # thumbnails load as the tiles get drawn
        self._count()

    def _count(self):
        n, shown = len(self.results), len(self.grid.items)
        text = "%d material%s" % (n, "" if n == 1 else "s")
        if 0 < shown < n:
            text += "  (%d shown)" % shown
        self.SetString(ID_COUNT, text)

    def _near_the_end(self):
        if len(self.grid.items) >= len(self.results):
            return False
        try:
            area = self.GetVisibleArea(ID_SCROLL)
            bottom = area["y2"]
        except Exception:
            return True
        return bottom >= self.grid.content_height() - 3 * self.grid.cell_h

    def category_changed(self, key):
        self.cfg.setdefault("category", {})[self.tab] = key
        self.search()

    # ---- creating materials ---------------------------------------------------------------------
    def selection_changed(self):
        sel = self.grid.selected_assets()
        if len(sel) == 1:
            a = sel[0]
            if a.source == "installed":
                e = a.extra["entry"]
                who = (" by " + e["downloaded_by"]) if e.get("downloaded_by") else ""
                when = ("  ·  downloaded %s%s" % (e["downloaded"], who)) if e.get("downloaded") else ""
                self.status("%s  (%s)%s" % (a.name, a.extra["origin"], when))
            else:
                here = " · in library" if a.key in self.local else ""
                cats = ", ".join(a.categories[:3])
                self.status("%s  (%s%s, up to %s)%s" % (a.name, a.source_label, (" · " + cats) if cats else "",
                                                       a.max_res or "?", here))
        elif sel:
            self.status("%d selected" % len(sel))

    def _payload(self):
        doc = c4d.documents.GetActiveDocument()
        objects = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_NONE) if self.GetBool(ID_ASSIGN) else []
        return {"objects": objects, "options": self.options(), "resolution": self.resolution}

    def create(self, assets=None):
        assets = assets or self.grid.selected_assets()
        if not assets:
            self.status("Select a material first (double-click creates it straight away).")
            return
        payload = self._payload()
        if assets[0].source == "installed":          # already on disk: build right away
            built = 0
            for a in assets:
                if self._build(a.key, a.name, a.extra["entry"]["maps"], payload):
                    built += 1
            if len(assets) > 1:
                self.status("Created %d materials from the library." % built)
            return
        queued = 0
        for a in assets:
            if self.jobs.download(a, self.resolution, payload):
                self.state[a.key] = "queued"
                queued += 1
        self.grid.Redraw()
        self.status("Queued %d material%s…" % (queued, "" if queued == 1 else "s") if queued
                    else "Already queued.")

    def _build(self, key, name, maps, payload, note=""):
        """Make the Redshift material on the main thread. Returns True when it worked."""
        doc = c4d.documents.GetActiveDocument()
        doc.StartUndo()
        try:
            mat, problems = rsmat.build(doc, name, maps, payload.get("options"))
            n = rsmat.assign(doc, mat, payload.get("objects") or [])
        except Exception as exc:
            traceback.print_exc()
            self.state[key] = "error"
            self.status("Couldn't build %s: %s" % (name, exc))
            return False
        finally:
            doc.EndUndo()
            c4d.EventAdd()
        bits = ["%s created" % name]
        if note:
            bits.append(note)
        if n:
            bits.append("assigned to %d object%s" % (n, "" if n == 1 else "s"))
        if problems:
            bits.append("%d Redshift port(s) skipped - see Console" % len(problems))
        left = len(self.jobs.pending)
        if left:
            bits.append("%d still queued" % left)
        self.status(", ".join(bits))
        return True

    def _finished(self, key, folder, maps, reused, asset, payload):
        self.state.pop(key, None)
        self.local.add(key)
        res = os.path.basename(folder).rsplit("_", 1)[-1]
        self._build(key, "%s %s" % (asset.name, res), maps, payload, "from the library" if reused else "")
        if not reused:
            sources.SOURCES["installed"]._all = None   # the Installed tab rescans next time

    # ---- updates ---------------------------------------------------------------------------------
    def check_updates(self, manual):
        self.manual_update_check = manual
        token = self.cfg.get("github_token") or None

        def run():
            try:
                self.jobs.results.put(("update", updater.check(token), None))
            except Exception as exc:
                self.jobs.results.put(("update", None, jobsmod.message(exc)))
        threading.Thread(target=run, daemon=True).start()
        if manual:
            self.status("Checking GitHub for updates…")

    def _update_result(self, info, error):
        if error:
            if self.manual_update_check:
                gui.MessageDialog("Couldn't check for updates:\n%s" % error)
            else:
                print("[BasPBR Browser] update check failed: %s" % error)
            return
        if not info:
            if self.manual_update_check:
                gui.MessageDialog("BasPBR Browser %s is the latest version." % version.VERSION)
                self.status("")
            return
        notes = "\n".join("  • " + n for n in info["notes"][:12])
        if not gui.QuestionDialog(
                "BasPBR Browser %s is available (you have %s).\n\n%s\n\nDownload and install it now?\n"
                "Restart Cinema 4D afterwards to use it." % (info["version"], info["current"], notes)):
            self.status("Update %s skipped for now - Help > Check for updates installs it later." % info["version"])
            return
        token = self.cfg.get("github_token") or None
        self.status("Downloading BasPBR Browser %s…" % info["version"])

        def run():
            try:
                self.jobs.results.put(("updated", updater.install(token), None))
            except Exception as exc:
                traceback.print_exc()
                self.jobs.results.put(("updated", None, jobsmod.message(exc)))
        threading.Thread(target=run, daemon=True).start()

    def _updated(self, new_version, error):
        if error:
            gui.MessageDialog("The update didn't install - nothing was changed:\n%s" % error)
            self.status("Update failed: " + error)
            return
        self.status("BasPBR Browser %s installed - restart Cinema 4D to use it." % new_version)
        gui.MessageDialog("BasPBR Browser %s is installed.\n\nRestart Cinema 4D to use it." % new_version)

    # ---- menus ---------------------------------------------------------------------------------
    def context_menu(self, asset):
        bc = c4d.BaseContainer()
        bc.SetString(POP_CREATE, "Create material")
        if asset.page_url:
            bc.SetString(POP_PAGE, "Open web page")
        folder = None
        if asset.source == "installed":
            folder = asset.extra["entry"]["folder"]
        elif asset.key in self.local:
            src = sources.SOURCES[asset.source]
            folder = self.library.asset_dir(asset, src.fixed_resolution or self.resolution)
            if not os.path.isdir(folder):
                folder = os.path.join(self.library.root, asset.source_label)
        if folder:
            bc.SetString(POP_FOLDER, "Show in library folder")
        choice = gui.ShowPopupDialog(cd=None, bc=bc, x=c4d.MOUSEPOS, y=c4d.MOUSEPOS)
        if choice == POP_CREATE:
            self.create()
        elif choice == POP_PAGE:
            webbrowser.open(asset.page_url)
        elif choice == POP_FOLDER and folder:
            c4d.storage.ShowInFinder(folder, True)

    # ---- events ----------------------------------------------------------------------------------
    def Command(self, cid, msg):
        if cid == ID_TABS:
            tab = self._current_tab()
            if tab != self.tab:
                self.tab = tab
                self._tab_changed()
        elif cid == ID_SEARCH:
            self.search()
        elif cid == ID_QUERY:
            self.search_due = time.time() + 0.4      # search as you type, once typing pauses
        elif cid == ID_SORT:
            self.cfg["sort"][self.tab] = self._sort()
            self.search()
        elif cid == ID_SIZE:
            self.set_tile_size(self.GetInt32(ID_SIZE))
        elif cid == ID_CREATE:
            self.create()
        elif cid == ID_CANCEL:
            self.jobs.cancel_all()
            for k, v in list(self.state.items()):
                if v in ("queued", "waiting"):
                    self.state.pop(k)
            self.grid.Redraw()
            self.status("Cancelled.")
        elif cid == ID_LIBRARY:
            self.library_due = time.time() + 1.2     # typed path: apply once typing pauses
        elif cid in (ID_LIB_BROWSE, ID_MENU_FOLDER):
            path = c4d.storage.LoadDialog(title="Material library folder (a network share works)",
                                          flags=c4d.FILESELECT_DIRECTORY, def_path=self.library.root)
            if path:
                self.set_library(path)
        elif cid in (ID_LIB_OPEN, ID_MENU_OPEN):
            try:
                os.makedirs(self.library.root, exist_ok=True)
                c4d.storage.ShowInFinder(self.library.root, True)
            except OSError as exc:
                self.status("Can't open the library: %s" % exc)
        elif cid == ID_LIB_RESCAN:
            self.SetString(ID_LIB_INFO, "reading the library…")
            self.jobs.forget_thumbs()
            self.jobs.scan()
        elif cid == ID_MENU_REFRESH:
            for s in sources.SOURCES.values():
                s.refresh()
            self.cat_cache = {}
            self.jobs.forget_thumbs()
            self.jobs.scan()
            self._tab_changed()
        elif cid == ID_MENU_CHECK:
            ok = rsmat.check_ids()
            self.status("Redshift ids all resolve." if ok else "Some Redshift ids are missing - see the Console.")
            c4d.CallCommand(12305)       # open the Console
        elif cid == ID_MENU_UPDATE:
            self.check_updates(manual=True)
        elif cid == ID_MENU_TOKEN:
            text = gui.InputDialog("GitHub token, only needed while the repository %s is private.\n"
                                   "Use a fine-grained token with read-only access to that repository. "
                                   "Leave empty to remove it." % version.REPO, self.cfg.get("github_token", ""))
            if text is not None:
                self.cfg["github_token"] = text.strip()
                self.save()
                self.status("GitHub token %s." % ("saved" if text.strip() else "removed"))
        elif cid == ID_MENU_ABOUT:
            gui.MessageDialog("BasPBR Browser %s\n\nambientCG, Poly Haven, ShareTextures, AMD GPUOpen and FreePBR "
                              "as Redshift materials.\nUpdates: github.com/%s (%s)" % (version.VERSION, version.REPO, version.BRANCH))
        return True

    def Timer(self, msg):
        now = time.time()
        if self.search_due and now >= self.search_due:
            self.search()
        if self.library_due and now >= self.library_due:
            self.library_due = 0.0
            typed = self.GetString(ID_LIBRARY).strip()
            if typed and os.path.isdir(typed):
                self.set_library(typed, ask_unc=False)
            elif typed:
                self.SetString(ID_LIB_INFO, "folder not found")
        if self.make_thumbs:
            self._make_thumb(*self.make_thumbs.pop(0))
        if self._near_the_end():
            self._show_more()
        redraw = False
        for _ in range(200):
            try:
                item = self.jobs.results.get_nowait()
            except Exception:
                break
            kind = item[0]
            if kind == "search":
                self._search_result(*item[1:])
            elif kind == "categories":
                self._categories_result(*item[1:])
            elif kind == "scanned":
                self._scanned(*item[1:])
            elif kind == "thumb":
                bmp = c4d.bitmaps.BaseBitmap()
                if bmp.InitWith(item[2])[0] == c4d.IMAGERESULT_OK:
                    _remember(item[1], bmp, self.jobs)
                    redraw = True
            elif kind == "thumb_make":
                self.make_thumbs.append(item[1:])
            elif kind == "progress":
                self.state[item[1]] = item[2]
                a = self.assets.get(item[1])
                if a is not None:
                    self.status("Downloading %s… %d%%" % (a.name, int(item[2] * 100)))
                redraw = True
            elif kind == "waiting":
                self.state[item[1]] = "waiting"
                a = self.assets.get(item[1])
                self.status("%s is downloading %s right now — waiting for it to land in the library…"
                            % (item[2], a.name if a else item[1]))
                redraw = True
            elif kind == "done":
                self._finished(*item[1:])
                redraw = True
            elif kind == "error":
                self.state[item[1]] = "error" if item[2] != "cancelled" else None
                a = self.assets.get(item[1])
                self.status("%s: %s" % (a.name if a else item[1], item[2]))
                redraw = True
            elif kind == "update":
                self._update_result(*item[1:])
            elif kind == "updated":
                self._updated(*item[1:])
        if redraw:
            self.grid.Redraw()

    def _make_thumb(self, key, color_path):
        """A thumbnail for a library folder that came without one: the colour map, scaled
        down (main thread, one per timer tick), cached locally."""
        try:
            src = c4d.bitmaps.BaseBitmap()
            if src.InitWith(color_path)[0] != c4d.IMAGERESULT_OK:
                return
            bw, bh = src.GetBw(), src.GetBh()
            s = 256.0 / max(bw, bh, 1)
            dst = c4d.bitmaps.BaseBitmap()
            dst.Init(max(1, int(bw * s)), max(1, int(bh * s)))
            src.ScaleIt(dst, 256, True, False)
            _remember(key, dst, self.jobs)
            path = os.path.join(libmod.local_cache_dir(), "thumbs", "installed", jobsmod._safe(key) + ".png")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            dst.Save(path, c4d.FILTER_PNG)
            self.grid.Redraw()
        except Exception:
            traceback.print_exc()

    def Message(self, msg, result):
        if msg.GetId() == c4d.BFM_INPUT and msg.GetInt32(c4d.BFM_INPUT_CHANNEL) == c4d.BFM_INPUT_MOUSEWHEEL \
                and msg.GetInt32(c4d.BFM_INPUT_QUALIFIER) & c4d.QCTRL:
            self.wheel_resize(msg.GetInt32(c4d.BFM_INPUT_VALUE))
            return True
        return gui.GeDialog.Message(self, msg, result)

    def AskClose(self):
        self.save()
        return False
