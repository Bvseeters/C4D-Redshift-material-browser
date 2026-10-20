"""The BasPBR Browser window (Cinema 4D only).

Tabs: Everything (every site at once, or the ticked ones), ambientCG, Poly
Haven, ShareTextures, AMD GPUOpen, FreePBR, and Installed (the library folder -
possibly a share several PCs use) - plus Sketchfab, for whole models: search
there, or copy a model's link in the browser and press Paste link; it comes in
as objects with Redshift materials, its licence credited in ATTRIBUTION.txt.
Under the tabs: search, sort, resolution, the
library folder and material options; on the left the categories (Up / Down step
through them), on the right the thumbnails, which load as they scroll into view
(Ctrl + mouse wheel or the Size slider resizes them).

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
from . import c4dscene, rsmat, settings, sketchfab, sources, updater, version

# gadget ids
ID_TABS = 1000
ID_QUERY, ID_SEARCH, ID_SORT, ID_RES, ID_PASTE, ID_RES_LABEL = 1002, 1003, 1004, 1005, 1006, 1007
ID_ASSIGN, ID_DISP, ID_DISP_SCALE, ID_BUMP_HEIGHT = 1010, 1011, 1012, 1013
ID_SCROLL, ID_GRID, ID_CAT_SCROLL, ID_CATS = 1020, 1021, 1022, 1023
ID_COUNT, ID_SIZE, ID_STATUS, ID_CANCEL, ID_CREATE = 1031, 1032, 1033, 1034, 1035
ID_LICENCE = 1036
ID_LIBRARY, ID_LIB_BROWSE, ID_LIB_OPEN, ID_LIB_RESCAN, ID_LIB_INFO, ID_LIB_LABEL = 1040, 1041, 1042, 1043, 1044, 1045
ID_MENU_FOLDER, ID_MENU_OPEN, ID_MENU_CHECK, ID_MENU_REFRESH, ID_MENU_MODELS = 1100, 1101, 1102, 1103, 1104
ID_MENU_UPDATE, ID_MENU_TOKEN, ID_MENU_ABOUT, ID_MENU_LOG, ID_MENU_SFTOKEN = 1110, 1111, 1112, 1113, 1114

# popup menu ids (must be >= FIRST_POPUP_ID)
POP_CREATE = c4d.FIRST_POPUP_ID + 1
POP_PAGE = c4d.FIRST_POPUP_ID + 2
POP_FOLDER = c4d.FIRST_POPUP_ID + 3

TILE_MIN, TILE_MAX, TILE_STEP = 64, 320, 16
CAPTION, GAP = 18, 8
SIDEBAR_W, ROW_H = 190, 20

BADGE_PEN = {"cc0": c4d.Vector(0.18, 0.55, 0.25), "by": c4d.Vector(0.75, 0.5, 0.1),
             "nc": c4d.Vector(0.7, 0.15, 0.15), "other": c4d.Vector(0.35)}

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


def _get(msg, key, default=0):
    try:
        return msg[key]
    except Exception:
        return default


def _ctrl(msg):
    """Ctrl held, from the event or (if the event doesn't say) the keyboard itself."""
    if _get(msg, c4d.BFM_INPUT_QUALIFIER) & c4d.QCTRL:
        return True
    try:
        state = c4d.BaseContainer()
        if gui.GetInputState(c4d.BFM_INPUT_KEYBOARD, c4d.BFM_INPUT_CHANNEL, state):
            return bool(state.GetInt32(c4d.BFM_INPUT_QUALIFIER) & c4d.QCTRL)
    except Exception:
        pass
    return False


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
        self.dialog.log_input("sidebar", msg)
        if msg[c4d.BFM_INPUT_DEVICE] == c4d.BFM_INPUT_KEYBOARD:
            return self.dialog.key(msg[c4d.BFM_INPUT_CHANNEL])
        if msg[c4d.BFM_INPUT_DEVICE] != c4d.BFM_INPUT_MOUSE or msg[c4d.BFM_INPUT_CHANNEL] != c4d.BFM_INPUT_MOUSELEFT:
            return False
        local = self.Global2Local()
        y = msg[c4d.BFM_INPUT_Y] + local["y"]
        if self.toggles and y < self._top():
            i = int((y - 3) // ROW_H)
            if 0 <= i < len(self.toggles):
                self.dialog.site_toggled(self.toggles[i][0], _ctrl(msg))
            return True
        i = int((y - self._top()) // ROW_H)
        if 0 <= i < len(self.rows) and self.rows[i][0] != self.selected:
            self.selected = self.rows[i][0]
            self.Redraw()
            self.dialog.category_changed(self.selected)
        return True


# =====================================================================================
class Grid(gui.GeUserArea):
    """The thumbnails. The grid scrolls itself - mouse wheel, its scroll bar, Page Up /
    Page Down / Home / End - instead of sitting in a scroll group: that way the wheel
    (and Ctrl + wheel, for the size) always reaches it, and however long the list,
    only the tiles in view are drawn and have their thumbnails fetched."""
    BAR = 12                  # scroll bar width

    def __init__(self, dialog, tile=128):
        super(Grid, self).__init__()
        self.dialog = dialog
        self.items = []
        self.selected = set()
        self.tile = tile
        self.cols = 1
        self.scroll = 0           # pixels scrolled down

    @property
    def cell_w(self):
        return self.tile + GAP

    @property
    def cell_h(self):
        return self.tile + CAPTION + GAP

    # ---- layout and scrolling ---------------------------------------------------------
    def _w(self):
        return self.GetWidth() or 700

    def _h(self):
        return self.GetHeight() or 500

    def _columns(self, width):
        return max(1, (max(width - self.BAR, self.cell_w) - GAP) // self.cell_w)

    def content_height(self):
        rows = (len(self.items) + self.cols - 1) // self.cols
        return rows * self.cell_h + GAP

    def max_scroll(self):
        return max(0, self.content_height() - self._h())

    def scroll_to(self, y):
        y = int(max(0, min(self.max_scroll(), y)))
        if y != self.scroll:
            self.scroll = y
            self.Redraw()

    def scroll_by(self, dy):
        self.scroll_to(self.scroll + dy)

    def GetMinSize(self):
        return self.cell_w + GAP + self.BAR, 200

    def Sized(self, w, h):
        self.cols = self._columns(w)
        self.scroll = max(0, min(self.max_scroll(), self.scroll))
        self.Redraw()

    def set_items(self, items):
        self.items = list(items)
        self.selected = set()
        self.scroll = 0
        self.cols = self._columns(self._w())
        self.Redraw()

    def add_items(self, items):
        """More results at the end (a search that pages); the view stays where it is."""
        self.items.extend(items)
        self.Redraw()

    def near_end(self, rows=2):
        return bool(self.items) and self.visible_range(extra_rows=rows)[1] >= len(self.items)

    def set_tile(self, size):
        first = (self.scroll // self.cell_h) * self.cols       # keep the top row in view
        self.tile = size
        self.cols = self._columns(self._w())
        self.scroll = max(0, min(self.max_scroll(), (first // self.cols) * self.cell_h))
        self.Redraw()

    def tile_rect(self, i):
        """Top-left of tile i, in list coordinates (scroll not applied)."""
        return GAP + (i % self.cols) * self.cell_w, GAP + (i // self.cols) * self.cell_h

    def index_at(self, x, y):
        """Tile under list coordinates x, y, or -1."""
        col, row = (x - GAP) // self.cell_w, (y - GAP) // self.cell_h
        if col < 0 or row < 0 or col >= self.cols:
            return -1
        i = int(row * self.cols + col)
        tx, ty = self.tile_rect(i)
        if i >= len(self.items) or x > tx + self.tile or y > ty + self.tile + CAPTION:
            return -1
        return i

    def visible_range(self, extra_rows=0):
        top, bottom = self.scroll, self.scroll + self._h() + extra_rows * self.cell_h
        first = max(0, int((top - GAP) // self.cell_h)) * self.cols
        last = min(len(self.items), (int((bottom - GAP) // self.cell_h) + 1) * self.cols)
        return first, max(first, last)

    def _bar(self):
        """(thumb top, thumb height) of the scroll bar, or None when everything fits."""
        h, total = self._h(), self.content_height()
        if total <= h:
            return None
        thumb = max(24, int(h * h / float(total)))
        return int((h - thumb) * self.scroll / float(self.max_scroll() or 1)), thumb

    # ---- drawing -------------------------------------------------------------------------
    def DrawMsg(self, x1, y1, x2, y2, msg):
        self.OffScreenOn()
        self.SetClippingRegion(x1, y1, x2, y2)
        bg = _rgb(self, c4d.COLOR_BG, c4d.Vector(0.2))
        self.DrawSetPen(bg)
        self.DrawRectangle(x1, y1, x2, y2)
        d = self.dialog
        if not self.items:
            self.DrawSetTextCol(c4d.COLOR_TEXT, c4d.COLOR_TRANS)
            self.DrawText(d.empty_text, GAP * 2, GAP * 2)
            return
        T = self.tile
        tile_bg = bg * 0.75
        accent = c4d.Vector(0.98, 0.62, 0.16)
        show_local = d.tab != "installed"
        show_source = d.tab == "everything"
        first, last = self.visible_range()
        # thumbnails: the next screen first, then what's in view (served newest-first)
        ahead = self.visible_range(extra_rows=self._h() // self.cell_h + 1)
        d.jobs.wanted = {a.key for a in self.items[ahead[0]:ahead[1]]}
        for asset in self.items[last:ahead[1]]:
            if asset.key not in _bitmaps:
                d.jobs.thumb(asset)
        for i in range(first, last):
            asset = self.items[i]
            x, y = self.tile_rect(i)
            y -= self.scroll
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
            if asset.source in sources.MODELS and T >= 72:          # licence badge + face count
                kind = asset.extra.get("kind", "other")
                tag = sketchfab.BADGES.get(kind, "CHECK")
                tw = self.DrawGetTextWidth(tag) + 8
                self.DrawSetPen(BADGE_PEN.get(kind, BADGE_PEN["other"]))
                self.DrawRectangle(x + 3, y + 3, x + 3 + tw, y + 3 + 15)
                self.DrawSetTextCol(c4d.Vector(1.0), c4d.COLOR_TRANS)
                self.DrawText(tag, x + 7, y + 3)
                faces = sketchfab.faces_text(asset.extra.get("faces"))
                if faces:
                    faces += " faces" if T >= 112 else ""
                    fw = self.DrawGetTextWidth(faces) + 8
                    self.DrawSetPen(c4d.Vector(0.08))
                    self.DrawRectangle(x + 3, y + T - 19, x + 3 + fw, y + T - 4)
                    self.DrawSetTextCol(c4d.COLOR_TEXT, c4d.COLOR_TRANS)
                    self.DrawText(faces, x + 7, y + T - 19)
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
        bar = self._bar()
        if bar:
            w, h = self._w(), self._h()
            self.DrawSetPen(bg * 0.8)
            self.DrawRectangle(w - self.BAR, 0, w, h)
            self.DrawSetPen(bg * 1.9)
            self.DrawRectangle(w - self.BAR + 2, bar[0], w - 3, bar[0] + bar[1])

    # ---- input ---------------------------------------------------------------------------
    def InputEvent(self, msg):
        self.dialog.log_input("grid", msg)
        device, channel = msg[c4d.BFM_INPUT_DEVICE], msg[c4d.BFM_INPUT_CHANNEL]
        if device == c4d.BFM_INPUT_KEYBOARD:
            page = self._h() - self.cell_h
            moves = {c4d.KEY_PGDOWN: page, c4d.KEY_PGUP: -page,
                     c4d.KEY_END: 10 ** 9, c4d.KEY_HOME: -10 ** 9}
            if channel in moves:
                self.scroll_by(moves[channel])
                return True
            return self.dialog.key(channel)
        if device != c4d.BFM_INPUT_MOUSE:
            return False
        if channel == c4d.BFM_INPUT_MOUSEWHEEL:
            value = _get(msg, c4d.BFM_INPUT_VALUE)
            if _ctrl(msg):                                       # Ctrl + wheel: thumbnail size
                self.dialog.wheel_resize(value)
            else:                                                # 120 = one notch, about 3/4 of a row
                self.scroll_by(-value / 120.0 * max(48, self.cell_h * 0.75))
            return True
        local = self.Global2Local()
        x = msg[c4d.BFM_INPUT_X] + local["x"]
        y = msg[c4d.BFM_INPUT_Y] + local["y"]
        if channel == c4d.BFM_INPUT_MOUSELEFT and x >= self._w() - self.BAR and self._bar():
            self._scroll_bar_click(y, msg)
            return True
        i = self.index_at(x, y + self.scroll)
        if channel == c4d.BFM_INPUT_MOUSELEFT:
            if i < 0:
                self.selected = set()
            elif _get(msg, c4d.BFM_INPUT_QUALIFIER) & c4d.QCTRL:
                self.selected ^= {i}
            else:
                self.selected = {i}
            self.Redraw()
            self.dialog.selection_changed()
            if i >= 0 and _get(msg, c4d.BFM_INPUT_DOUBLECLICK):
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

    def _scroll_bar_click(self, y, msg):
        top, thumb = self._bar()
        if not top <= y <= top + thumb:                      # in the track: a page up / down
            self.scroll_by((self._h() - self.cell_h) * (1 if y > top else -1))
            return
        start, moved = self.scroll, 0.0                      # on the thumb: drag it
        per_px = self.max_scroll() / float(max(1, self._h() - thumb))
        try:
            self.MouseDragStart(c4d.KEY_MLEFT, msg[c4d.BFM_INPUT_X], msg[c4d.BFM_INPUT_Y],
                                c4d.MOUSEDRAGFLAGS_DONTHIDEMOUSE | c4d.MOUSEDRAGFLAGS_NOMOVE)
            while True:
                result, dx, dy, channels = self.MouseDrag()
                if result != c4d.MOUSEDRAGRESULT_CONTINUE:
                    break
                moved += dy
                self.scroll_to(start + moved * per_px)
        finally:
            try:
                self.MouseDragEnd()
            except Exception:
                pass

    def selected_assets(self):
        return [self.items[i] for i in sorted(self.selected) if i < len(self.items)]


# =====================================================================================
class BrowserDialog(gui.GeDialog):
    def __init__(self):
        super(BrowserDialog, self).__init__()
        self.cfg = settings.load()
        self.library = libmod.Library(self.cfg["library"])
        self.model_library = libmod.Library(self.cfg.get("model_library") or settings.DEFAULTS["model_library"])
        sources.SOURCES["installed"].library = self.library
        sources.SOURCES["installed"].model_library = self.model_library
        self.jobs = jobsmod.Jobs(self.library, model_library=self.model_library)
        self.lib_info = {}          # library root -> its status text (count, network, read-only)
        self.library_due_models = False
        tile = int(self.cfg.get("tile", 128))
        self.grid = Grid(self, max(TILE_MIN, min(TILE_MAX, tile)))
        self.cats = Categories(self)
        self.tabs = None
        self.tab = self.cfg.get("source") if self.cfg.get("source") in sources.TABS else sources.TABS[0]
        self.state = {}             # asset key -> float progress | "queued" | "waiting" | "error"
        self.local = set()          # source:id keys already in the library
        self.cat_cache = {}         # tab -> [(key, label, count)]
        self.results = []           # every match of the current search (all in the grid)
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
        self._key = (0.0, 0)        # last arrow-key step (time, direction)
        self.res_options = []       # the Resolution menu's entries on this tab
        self.sizes = {}             # asset key -> sizes it really comes in (asked on selection)
        self.log_inputs = False     # Help > Log mouse wheel and keys
        self.more_url = None        # the next page of a search that pages (Sketchfab)
        self.loading_more = False
        self.import_after_lookup = None   # uid of a pasted link: import it once it's found
        self.search_typed = False   # the pending search_due came from typing
        if not isinstance(self.cfg.get("res"), dict):
            self.cfg["res"] = {}

    # ---- layout --------------------------------------------------------------------------
    def CreateLayout(self):
        self.SetTitle("BasPBR Browser")
        self.MenuFlushAll()
        self.MenuSubBegin("Library")
        self.MenuAddString(ID_MENU_FOLDER, "Choose library folder…")
        self.MenuAddString(ID_MENU_OPEN, "Open library folder")
        self.MenuAddString(ID_MENU_MODELS, "Choose Sketchfab models folder…")
        self.MenuAddString(ID_MENU_REFRESH, "Reload the sites' catalogues and rescan the library")
        self.MenuAddSeparator()
        self.MenuAddString(ID_MENU_CHECK, "Check Redshift node IDs (prints to the Console)")
        self.MenuSubEnd()
        self.MenuSubBegin("Help")
        self.MenuAddString(ID_MENU_UPDATE, "Check for updates")
        self.MenuAddString(ID_MENU_SFTOKEN, "Sketchfab API token…")
        self.MenuAddString(ID_MENU_TOKEN, "GitHub token (private repository)…")
        self.MenuAddString(ID_MENU_LOG, "Log mouse wheel and keys to the Console")
        self.MenuAddSeparator()
        self.MenuAddString(ID_MENU_ABOUT, "About BasPBR Browser")
        self.MenuSubEnd()
        self.MenuFinished()

        bc = c4d.BaseContainer()
        bc.SetBool(c4d.QUICKTAB_BAR, False)
        bc.SetBool(c4d.QUICKTAB_SHOWSINGLE, True)
        bc.SetBool(c4d.QUICKTAB_NOMULTISELECT, True)
        self.tabs = self.AddCustomGui(ID_TABS, c4d.CUSTOMGUI_QUICKTAB, "", c4d.BFH_SCALEFIT, 0, 0, bc)

        if self.GroupBegin(0, c4d.BFH_SCALEFIT, cols=8, rows=1):
            self.GroupBorderSpace(6, 4, 6, 2)
            self.AddEditText(ID_QUERY, c4d.BFH_SCALEFIT, 200)
            self.AddButton(ID_SEARCH, c4d.BFH_LEFT, name="Search")
            self.AddButton(ID_PASTE, c4d.BFH_LEFT, name="Paste link")
            self.AddStaticText(0, c4d.BFH_LEFT, name=" Sort")
            self.AddComboBox(ID_SORT, c4d.BFH_LEFT, 100)
            self.AddStaticText(ID_RES_LABEL, c4d.BFH_LEFT, 70, name=" Resolution")
            self.AddComboBox(ID_RES, c4d.BFH_LEFT, 90)
        self.GroupEnd()

        if self.GroupBegin(0, c4d.BFH_SCALEFIT, cols=6, rows=1):
            self.GroupBorderSpace(6, 0, 6, 2)
            self.AddStaticText(ID_LIB_LABEL, c4d.BFH_LEFT, 50, name="Library")
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
            self.AddUserArea(ID_GRID, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 600, 420)
            self.AttachUserArea(self.grid, ID_GRID)
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
        self.SetBool(ID_ASSIGN, cfg.get("assign", True))
        self.SetBool(ID_DISP, cfg.get("displacement", True))
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
        """The Resolution menu's choice: "Highest" or a size such as "4K"."""
        i = self.GetInt32(ID_RES)
        return self.res_options[i] if 0 <= i < len(self.res_options) else sources.HIGHEST

    def _fill_resolutions(self):
        """Only the sizes this tab's site(s) offer, largest first, after "Highest".
        On a model tab the same menu is Max faces."""
        self.SetString(ID_RES_LABEL, " Max faces" if self.tab in sources.MODELS else " Resolution")
        if self.tab in sources.MODELS:
            self.res_options = [label for label, n in sketchfab.FACES]
            self.FreeChildren(ID_RES)
            for n, label in enumerate(self.res_options):
                self.AddChild(ID_RES, n, label)
            saved = self.cfg.get("faces") or sketchfab.DEFAULT_FACES
            self.SetInt32(ID_RES, self.res_options.index(saved) if saved in self.res_options else 0)
            self.Enable(ID_RES, True)
            return
        if self.tab == "installed":
            sizes = ()
        elif self.tab == "everything":
            sizes = sources.Everything.resolutions_of(self.sites)
        else:
            sizes = self.source.resolutions
        sizes = sorted(sizes, key=sources._res_value, reverse=True)
        self.res_options = sizes if len(sizes) <= 1 else [sources.HIGHEST] + sizes
        self.FreeChildren(ID_RES)
        for n, r in enumerate(self.res_options or ["—"]):
            self.AddChild(ID_RES, n, r)
        saved = self.cfg["res"].get(self.tab, sources.HIGHEST)
        self.SetInt32(ID_RES, self.res_options.index(saved) if saved in self.res_options else 0)
        self.Enable(ID_RES, len(self.res_options) > 1)

    @property
    def sites(self):
        """The sites the Everything tab searches (the ticked ones); None on other tabs."""
        if self.tab != "everything":
            return None
        off = self.cfg.get("sites_off") or []
        return tuple(s for s in sources.Everything.MEMBERS if s not in off)

    def _cat_key(self):
        return self.tab if self.tab != "everything" else "everything:" + ",".join(self.sites)

    @property
    def max_faces(self):
        return dict(sketchfab.FACES).get(self.cfg.get("faces") or sketchfab.DEFAULT_FACES)

    def _model_toggles(self):
        return [("commercial", "Commercial use only", None, bool(self.cfg.get("sf_commercial"))),
                ("staffpicked", "Staff picks only", None, bool(self.cfg.get("sf_staffpicked")))]

    def _switch_tab(self, tid):
        if tid == self.tab:
            return
        if self.tabs is not None:
            for n, t in enumerate(sources.TABS):
                self.tabs.Select(n, t == tid)
        self.tab = tid
        self._tab_changed(search=False)

    def _site_toggles(self):
        counts = sources.SOURCES["everything"].site_counts()
        on = self.sites
        return [(s, sources.SOURCES[s].label, counts.get(s), s in on) for s in sources.Everything.MEMBERS]

    def site_toggled(self, sid, solo=False):
        """A site's tick box on the Everything tab. Ctrl-click: only that site (again: all).
        On Sketchfab the boxes are its filters."""
        if self.tab in sources.MODELS:
            key = {"commercial": "sf_commercial", "staffpicked": "sf_staffpicked"}.get(sid)
            if key:
                self.cfg[key] = not self.cfg.get(key)
                self.cats.set_toggles(self._model_toggles())
                self.search()
            return
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
        self._fill_resolutions()
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
        self._fill_resolutions()
        self.SetString(ID_CREATE, "Import model" if self.tab in sources.MODELS else "Create material")
        self._show_library_row()
        if self.tab not in sources.MODELS and sketchfab.parse_uid(self.GetString(ID_QUERY)):
            self.SetString(ID_QUERY, "")          # a Sketchfab link means nothing to the other tabs
        if installed:
            self.SetString(ID_LICENCE, "In the library folder — no download needed")
        elif self.tab in sources.MODELS:
            self.SetString(ID_LICENCE, src.licence)
        else:
            extra = ""
            if src.fixed_resolution:
                extra = "  ·  %s only" % src.fixed_resolution
            elif self.tab != "everything":
                extra = "  ·  up to %s" % src.resolutions[-1]
            self.SetString(ID_LICENCE, src.licence + extra)
        self.cats.set_toggles(self._site_toggles() if self.tab == "everything" else
                              self._model_toggles() if self.tab in sources.MODELS else [])
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
        if self.tab in sources.MODELS:
            cfg["faces"] = self.resolution
        elif self.res_options:
            cfg["res"][self.tab] = self.resolution
        cfg["assign"] = self.GetBool(ID_ASSIGN)
        cfg["displacement"] = self.GetBool(ID_DISP)
        cfg["displacement_scale"] = self.GetFloat(ID_DISP_SCALE)
        cfg["bump_from_height"] = self.GetBool(ID_BUMP_HEIGHT)
        cfg["library"] = self.library.root
        cfg["model_library"] = self.model_library.root
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
    def _lib(self, models=None):
        """The folder the Library row is about: Sketchfab's models folder on its tab."""
        models = self.tab in sources.MODELS if models is None else models
        return self.model_library if models else self.library

    def _show_library_row(self):
        lib = self._lib()
        self.SetString(ID_LIB_LABEL, "Models" if lib is self.model_library and self.tab in sources.MODELS else "Library")
        self.SetString(ID_LIBRARY, lib.root)
        self.SetString(ID_LIB_INFO, self.lib_info.get(lib.root, "reading…"))

    def set_library(self, path, ask_unc=True, models=None):
        path = (path or "").strip().strip('"')
        if not path:
            return
        models = self.tab in sources.MODELS if models is None else models
        if models:
            unc = libmod.unc_path(path)
            if unc and ask_unc and gui.QuestionDialog(
                    "%s is the network share\n%s\n\nUse the network path, so the models' texture paths "
                    "work on every PC (whatever drive letter they use)?" % (path[:2], unc[:len(unc) - len(path) + 2])):
                path = unc
            if os.path.normcase(path) != os.path.normcase(self.model_library.root):
                self.model_library = libmod.Library(path)
                self.jobs.model_library = self.model_library
                sources.SOURCES["installed"].model_library = self.model_library
                sources.SOURCES["installed"]._all = None
                self.save()
                self.jobs.scan()
                self.status("Sketchfab models folder: " + path)
            self._show_library_row()
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
        for lib, models in ((self.library, False), (self.model_library, True)):
            if error or not os.path.isdir(lib.root):
                info = "not reachable" if not os.path.isdir(lib.root) else error
            else:
                root = os.path.normcase(os.path.abspath(lib.root))
                mine = [e for e in entries if os.path.normcase(os.path.abspath(e["folder"])).startswith(root)
                        and (e.get("kind") == "model") == models]
                what = "model" if models else "material"
                shared = lib.root.startswith("\\\\") or bool(libmod.unc_path(lib.root))
                info = "%d %s%s%s%s" % (len(mine), what, "" if len(mine) == 1 else "s",
                                        " · network" if shared else "",
                                        "" if lib.writable() else " · read-only")
            self.lib_info[lib.root] = info
        self._show_library_row()
        if self.tab == "installed":
            self._installed_categories()
            self.search()
        self.grid.Redraw()

    # ---- searching and endless scrolling -------------------------------------------------------
    def search(self, typed=False):
        """typed: the user searched (Search, or typing) - a Sketchfab link then goes
        to the Sketchfab tab, whichever tab it was typed on."""
        query = self.GetString(ID_QUERY).strip()
        if typed and sketchfab.parse_uid(query) and self.tab not in sources.MODELS:
            self._switch_tab("sketchfab")
        self.token += 1
        self.search_due = 0.0
        self.results = []
        self.searching = True
        self.more_url, self.loading_more = None, False
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
        elif self.tab in sources.MODELS:
            self.empty_text = ("Looking the model up on Sketchfab…" if sketchfab.parse_uid(query)
                               else "Searching Sketchfab…")
        else:
            self.empty_text = "Searching…"
        self.grid.set_items([])
        self.SetString(ID_COUNT, "")
        extra = {}
        if self.tab in sources.MODELS:
            extra = {"max_faces": self.max_faces, "commercial": bool(self.cfg.get("sf_commercial")),
                     "staffpicked": bool(self.cfg.get("sf_staffpicked"))}
        self.jobs.search(self.token, self.tab, query, self._sort(), self.category, self.sites, **extra)

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
        for a in assets:
            self.assets[a.key] = a
        self.more_url = getattr(assets, "next", None)
        self.grid.set_items(assets)              # thumbnails load as tiles come into view
        self._count()
        uid = self.import_after_lookup
        if uid and self.tab in sources.MODELS:   # Paste link: import what the link points at
            self.import_after_lookup = None
            hit = next((i for i, a in enumerate(assets) if a.id == uid), None)
            if hit is not None:
                self.grid.selected = {hit}
                self.grid.Redraw()
                self.create([assets[hit]])
        elif self.tab in sources.MODELS and len(assets) == 1 and sketchfab.parse_uid(self.GetString(ID_QUERY)):
            self.grid.selected = {0}             # a link typed into the search: that model, selected
            self.grid.Redraw()
            self.selection_changed()
        if self._cat_key() not in self.cat_cache and self.tab != "installed":
            self._load_categories()               # (again, if it failed) - instant now the catalogue is loaded

    def _count(self):
        n = len(self.results)
        what = "model" if self.tab in sources.MODELS else "material"
        self.SetString(ID_COUNT, "%d%s %s%s" % (n, "+" if self.more_url else "", what, "" if n == 1 else "s"))

    def _more_result(self, token, page, error):
        if token != self.token:
            return
        self.loading_more = False
        if error:
            self.status("Couldn't load more: " + error)
            self.more_url = None
            return
        for a in page:
            self.assets[a.key] = a
        self.results.extend(page)
        self.more_url = getattr(page, "next", None)
        self.grid.add_items(page)
        self._count()

    def _maybe_more(self):
        """Near the end of a search that pages: fetch the next page."""
        if self.more_url and not self.loading_more and not self.searching and self.grid.near_end():
            self.loading_more = True
            self.jobs.more(self.token, self.tab, self.more_url, commercial=bool(self.cfg.get("sf_commercial")))

    # ---- Sketchfab models -------------------------------------------------------------------------
    def paste_link(self):
        """Paste link: the Sketchfab model whose link is on the clipboard - found and imported."""
        try:
            text = c4d.GetStringFromClipboard() or ""
        except Exception:
            text = ""
        uid = sketchfab.parse_uid(text)
        if not uid:
            self.status("No Sketchfab link on the clipboard - copy a model's address in your browser first.")
            return
        self.import_after_lookup = uid
        self.SetString(ID_QUERY, text.strip())
        self._switch_tab("sketchfab")
        self.search()

    def _ask_sketchfab_token(self, needed=False):
        text = gui.InputDialog(("Downloading from Sketchfab needs your own API token.\n" if needed else "") +
                               "Find it on sketchfab.com/settings/password (\"API Token\") and paste it here.\n"
                               "It's kept in this PC's BasPBR Browser settings. Leave empty to remove it.",
                               self.cfg.get("sketchfab_token", ""))
        if text is None:
            return None
        self.cfg["sketchfab_token"] = text.strip()
        self.save()
        self.status("Sketchfab token %s." % ("saved" if text.strip() else "removed"))
        return text.strip() or None

    def _import_models(self, assets, payload):
        token = self.cfg.get("sketchfab_token") or self._ask_sketchfab_token(needed=True)
        if not token:
            self.status("No Sketchfab token - Help > Sketchfab API token to add yours.")
            return
        nc = [a for a in assets if a.extra.get("kind") == "nc" and a.key not in self.local]
        if nc and not gui.QuestionDialog(
                "%s\n\n%s NOT for commercial work (%s).\nImport anyway?" % (
                    "\n".join("  \u2022 " + a.name for a in nc[:8]), "This model is" if len(nc) == 1 else "These are",
                    nc[0].extra.get("licence") or "non-commercial licence")):
            assets = [a for a in assets if a not in nc]
        queued = 0
        for a in assets:
            if self.jobs.download(a, None, dict(payload, token=token)):
                self.state[a.key] = "queued"
                queued += 1
        self.grid.Redraw()
        if queued:
            self.status("Downloading %d model%s from Sketchfab…" % (queued, "" if queued == 1 else "s"))

    def _model_ready(self, key, folder, info, scene, images, reused, asset, payload):
        """A model on disk and read: build it into the scene (main thread)."""
        self.state.pop(key, None)
        name = info.get("name") or asset.name
        doc = c4d.documents.GetActiveDocument()
        doc.StartUndo()
        try:
            root, stats = c4dscene.build(doc, scene, name, images, (payload or {}).get("options"))
            doc.SetActiveObject(root, c4d.SELECTION_NEW)
        except Exception as exc:
            traceback.print_exc()
            self.state[key] = "error"
            self.status("Couldn't build %s: %s" % (name, exc))
            return
        finally:
            doc.EndUndo()
            c4d.EventAdd()
        if asset.source != "installed":
            self.local.add(key)
            sources.SOURCES["installed"]._all = None
        bits = ["%s imported%s: %d object%s, %d material%s, %s polygons" % (
            name, " from the library" if reused else "", stats["objects"], "" if stats["objects"] == 1 else "s",
            stats["materials"], "" if stats["materials"] == 1 else "s", sketchfab.faces_text(stats["polygons"]) or "0")]
        kind = sketchfab.licence_kind(info.get("licence"))
        if kind == "by":
            bits.append("credit %s (ATTRIBUTION.txt in the library)" % (info.get("author") or "the author"))
        elif kind == "nc":
            bits.append("NOT for commercial work")
        if scene.warnings:
            bits.append("; ".join(scene.warnings))
        if stats["problems"]:
            bits.append("%d Redshift port(s) skipped - see Console" % len(stats["problems"]))
        self.status("  ·  ".join(bits))

    def category_changed(self, key):
        self.cfg.setdefault("category", {})[self.tab] = key
        self.search()

    # ---- keys and the mouse wheel ------------------------------------------------------------
    def key(self, channel):
        """A key pressed in the sidebar or the grid (or anywhere, via Message).
        Up / Down step through the categories, wrapping around."""
        if channel not in (c4d.KEY_UP, c4d.KEY_DOWN):
            return False
        step = -1 if channel == c4d.KEY_UP else 1
        now = time.time()
        if now - self._key[0] < 0.01 and self._key[1] == step:
            return True                          # the same key press, seen by a gadget and the dialog
        self._key = (now, step)
        keys = [r[0] for r in self.cats.rows]
        if len(keys) > 1:
            i = (keys.index(self.cats.selected) + step) % len(keys) if self.cats.selected in keys else 0
            self.cats.selected = keys[i]
            self.cats.Redraw()
            self._reveal_category(i)
            self.cfg.setdefault("category", {})[self.tab] = keys[i]
            self.search_due = now + 0.15         # the results follow once the key rests
        return True

    def _reveal_category(self, i):
        """Scroll the sidebar so category row i is in view."""
        y = self.cats.row_y(i)
        try:
            area = self.GetVisibleArea(ID_CAT_SCROLL)
            h = area["y2"] - area["y1"]
            top = y - ROW_H if y - ROW_H < area["y1"] else (y + 2 * ROW_H - h if y + 2 * ROW_H > area["y2"] else None)
            if top is not None:
                top = max(0, top)
                self.SetVisibleArea(ID_CAT_SCROLL, 0, top, SIDEBAR_W, top + h)
        except Exception:
            pass

    def log_input(self, where, msg):
        if not self.log_inputs:
            return
        device, channel = _get(msg, c4d.BFM_INPUT_DEVICE), _get(msg, c4d.BFM_INPUT_CHANNEL)
        if device == c4d.BFM_INPUT_KEYBOARD or channel == c4d.BFM_INPUT_MOUSEWHEEL:
            print("[BasPBR Browser] input in %s: %s channel=%s value=%s qualifier=%s ctrl=%s" % (
                where, "keyboard" if device == c4d.BFM_INPUT_KEYBOARD else "wheel", channel,
                _get(msg, c4d.BFM_INPUT_VALUE), _get(msg, c4d.BFM_INPUT_QUALIFIER), _ctrl(msg)))

    # ---- creating materials ---------------------------------------------------------------------
    def selection_changed(self):
        sel = self.grid.selected_assets()
        if len(sel) == 1:
            a = sel[0]
            if a.source in sources.MODELS:
                self._describe_model(a)
            elif a.source == "installed":
                e = a.extra["entry"]
                who = (" by " + e["downloaded_by"]) if e.get("downloaded_by") else ""
                when = ("  ·  downloaded %s%s" % (e["downloaded"], who)) if e.get("downloaded") else ""
                self.status("%s  (%s)%s" % (a.name, a.extra["origin"], when))
            else:
                self._describe(a)
                if a.key not in self.sizes:
                    self.jobs.details(a)             # the sizes it really comes in
        elif sel:
            self.status("%d selected" % len(sel))

    def _describe_model(self, a):
        e = a.extra
        bits = [a.name + ("  by " + e["author"] if e.get("author") else "")]
        verdict = {"cc0": "no credit needed", "by": "credit the author", "nc": "not for commercial work"}
        lic = e.get("licence") or "licence unknown"
        bits.append(lic + (" (%s)" % verdict[e.get("kind")] if e.get("kind") in verdict else ""))
        if e.get("faces"):
            bits.append(sketchfab.faces_text(e["faces"]) + " faces")
        if e.get("size"):
            tex = (", %d textures up to %dK" % (e["textures"], (e.get("texture_res") or 0) // 1024)
                   if e.get("textures") and e.get("texture_res") else "")
            bits.append("%d MB%s" % (round(e["size"] / 1e6), tex))
        if a.key in self.local:
            bits.append("in library")
        self.status("  ·  ".join(bits))

    def _describe(self, a):
        here = "  ·  in library" if a.key in self.local else ""
        cats = ", ".join(a.categories[:3])
        sizes = self.sizes.get(a.key)
        src = sources.SOURCES[a.source]
        if sizes:
            get = sources.choose_res(sizes, src.fixed_resolution or self.resolution)
            size = ("%s–%s, downloads %s" % (sizes[0], sizes[-1], get)) if len(sizes) > 1 else "%s only" % sizes[0]
        elif a.key in self.sizes:
            size = "sizes unknown"
        else:
            size = "checking sizes…"
        self.status("%s  (%s%s)  %s%s" % (a.name, a.source_label, (" · " + cats) if cats else "", size, here))

    def _details(self, key, sizes, error):
        self.sizes[key] = sizes
        sel = self.grid.selected_assets()
        if len(sel) == 1 and sel[0].key == key:
            self._describe(sel[0])

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
        models = [a for a in assets if a.source in sources.MODELS]
        if models:
            return self._import_models(models, payload)
        stored = [a for a in assets if a.source == "installed" and a.extra["entry"].get("kind") == "model"]
        for a in stored:                              # models in the library: read, then built
            self.state[a.key] = "queued"
            self.jobs.load_model(a, payload)
        assets = [a for a in assets if a not in stored]
        if not assets:
            self.grid.Redraw()
            return
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
        model = asset.source in sources.MODELS or (asset.source == "installed" and
                                                   asset.extra["entry"].get("kind") == "model")
        bc.SetString(POP_CREATE, "Import model" if model else "Create material")
        if asset.page_url:
            bc.SetString(POP_PAGE, "Open on Sketchfab" if model else "Open web page")
        folder = None
        if asset.source == "installed":
            folder = asset.extra["entry"]["folder"]
        elif asset.source in sources.MODELS:
            found = self.model_library.find_model(asset) if asset.key in self.local else None
            folder = found[0] if found else None
        elif asset.key in self.local:
            have = sorted(self.library.resolutions(asset), key=sources._res_value)
            folder = (self.library.asset_dir(asset, have[-1]) if have
                      else os.path.join(self.library.root, asset.source_label))
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
            self.search(typed=True)
        elif cid == ID_PASTE:
            self.paste_link()
        elif cid == ID_QUERY:
            self.search_due = time.time() + 0.4      # search as you type, once typing pauses
            self.search_typed = True
        elif cid == ID_SORT:
            self.cfg["sort"][self.tab] = self._sort()
            self.search()
        elif cid == ID_RES and self.tab in sources.MODELS:
            self.cfg["faces"] = self.resolution
            self.search()
        elif cid == ID_RES:
            self.cfg["res"][self.tab] = self.resolution
            sel = self.grid.selected_assets()
            if len(sel) == 1 and sel[0].source != "installed":
                self._describe(sel[0])
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
            self.library_due_models = self.tab in sources.MODELS
        elif cid in (ID_LIB_BROWSE, ID_MENU_FOLDER, ID_MENU_MODELS):
            models = cid == ID_MENU_MODELS or (cid == ID_LIB_BROWSE and self.tab in sources.MODELS)
            path = c4d.storage.LoadDialog(
                title="Sketchfab models folder (a network share works)" if models else
                "Material library folder (a network share works)",
                flags=c4d.FILESELECT_DIRECTORY, def_path=self._lib(models).root)
            if path:
                self.set_library(path, models=models)
        elif cid in (ID_LIB_OPEN, ID_MENU_OPEN):
            lib = self._lib(models=cid == ID_LIB_OPEN and self.tab in sources.MODELS)
            try:
                os.makedirs(lib.root, exist_ok=True)
                c4d.storage.ShowInFinder(lib.root, True)
            except OSError as exc:
                self.status("Can't open %s: %s" % (lib.root, exc))
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
        elif cid == ID_MENU_SFTOKEN:
            self._ask_sketchfab_token()
        elif cid == ID_MENU_LOG:
            self.log_inputs = not self.log_inputs
            print("[BasPBR Browser] logging mouse wheel and keys: %s" % ("on" if self.log_inputs else "off"))
            self.status("Logging mouse wheel and keys to the Console: %s" % ("on" if self.log_inputs else "off"))
            if self.log_inputs:
                c4d.CallCommand(12305)
        elif cid == ID_MENU_ABOUT:
            gui.MessageDialog("BasPBR Browser %s\n\nambientCG, Poly Haven, ShareTextures, AMD GPUOpen and FreePBR "
                              "as Redshift materials.\nUpdates: github.com/%s (%s)" % (version.VERSION, version.REPO, version.BRANCH))
        return True

    def Timer(self, msg):
        now = time.time()
        if self.search_due and now >= self.search_due:
            typed, self.search_typed = self.search_typed, False
            self.search(typed=typed)
        if self.library_due and now >= self.library_due:
            self.library_due = 0.0
            typed = self.GetString(ID_LIBRARY).strip()
            if typed and os.path.isdir(typed):
                self.set_library(typed, ask_unc=False, models=self.library_due_models)
            elif typed:
                self.SetString(ID_LIB_INFO, "folder not found")
        if self.make_thumbs:
            self._make_thumb(*self.make_thumbs.pop(0))
        self._maybe_more()
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
            elif kind == "details":
                self._details(*item[1:])
            elif kind == "more":
                self._more_result(*item[1:])
            elif kind == "model":
                self._model_ready(*item[1:])
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
        if msg.GetId() == c4d.BFM_INPUT:
            self.log_input("dialog", msg)
            device, channel = msg.GetInt32(c4d.BFM_INPUT_DEVICE), msg.GetInt32(c4d.BFM_INPUT_CHANNEL)
            if device == c4d.BFM_INPUT_KEYBOARD and channel in (c4d.KEY_UP, c4d.KEY_DOWN) and not self._typing():
                return self.key(channel)
            if channel == c4d.BFM_INPUT_MOUSEWHEEL and _ctrl(msg):
                self.wheel_resize(msg.GetInt32(c4d.BFM_INPUT_VALUE))
                return True
        return gui.GeDialog.Message(self, msg, result)

    def _typing(self):
        """True while the library path or the height field has the keyboard."""
        for gid in (ID_LIBRARY, ID_DISP_SCALE):
            try:
                if self.IsActive(gid):
                    return True
            except Exception:
                pass
        return False

    def AskClose(self):
        self.save()
        return False
