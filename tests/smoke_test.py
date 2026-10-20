"""End-to-end check of the plugin outside Cinema 4D.

Runs the real plugin code against fake c4d/maxon modules (tests/fakes; the fake
bitmap loader refuses WebP, like Cinema 4D). Live network. Checks:

  * the plugin registers, the browser opens, the update popup shows once per session
  * Everything: every site in one list, sites mixed near the top, merged categories,
    one search across all sites; ticking sites off and on (Ctrl-click: only that
    one) changes both the results and the merged categories
  * settings from an older version: displacement turned on, resolution Highest
  * the grid scrolls itself (wheel, scroll bar, Page/Home/End) over the whole list,
    fetching thumbnails only for the tiles in or near view
  * Ctrl + mouse wheel and the Size slider resize the tiles (one step per notch),
    Ctrl read from the event or, failing that, from the keyboard
  * Up / Down step through the categories (wrapping), from the sidebar, the grid
    or the dialog, one step per key press
  * the Resolution menu lists only the sizes a site offers, Highest first; each
    material downloads at the size it really has and is named after it
  * per site: categories arrive, filtering by one works, thumbnails load in a format
    C4D reads (including repairing a WebP thumbnail an old version cached), a
    double-clicked material downloads and becomes a correctly wired Redshift
    material assigned to the selection
  * AMD: the .mtlx decides, not the file names - roughness from the channel it names
    (Color Splitter -> Change Range for its remap), constant metalness / colour as
    port values, the authored tiling on every Texture's Scale, kept in asset.json;
    an AMD folder an older version made without the graph is downloaded again
  * Installed tab: lists the library (plugin downloads + a hand-made folder),
    categories by source, double-click builds WITHOUT any network access
  * shared library: two "PCs" fetching the same new material download it once
    (the second waits on the first's lock); a stale lock is taken over
  * switching the library folder rescans

    python tests/smoke_test.py
"""
import json
import os
import runpy
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "..", "plugins", "BasPBRBrowser")
sys.path.insert(0, os.path.join(HERE, "fakes"))
sys.path.insert(0, PLUGIN)

import c4d      # noqa: E402  the fakes
import maxon    # noqa: E402

LIB = tempfile.mkdtemp(prefix="baspbr_lib_")
with open(os.path.join(c4d.storage.PREFS, "baspbr_browser.json"), "w") as f:
    json.dump({"library": LIB, "resolution": "1K", "source": "everything", "query": "",   # a 3.1 settings file
               "assign": True, "displacement": False}, f)

failures = []


def check(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        failures.append(what)


# ---- the plugin registers ---------------------------------------------------------
ns = runpy.run_path(os.path.join(PLUGIN, "BasPBRBrowser.pyp"), run_name="__main__")
print("registration:")
check(len(c4d.plugins.REGISTERED) == 1 and c4d.plugins.REGISTERED[0]["id"] == 1000005, "command plugin registered")

from baspbr import library as libmod, net, sources, ui, updater  # noqa: E402

# an old version cached Poly Haven thumbnails as WebP under a .png name; plant one to see it repaired
ph_first = sorted(sources.SOURCES["polyhaven"].catalogue(), key=lambda a: a.name.lower())[0]
webp = os.path.join(LIB, "_thumbs", "Poly Haven", ph_first.id + ".png")
os.makedirs(os.path.dirname(webp), exist_ok=True)
net.download("https://cdn.polyhaven.com/asset_img/thumbs/%s.png?width=256&height=256&format=webp" % ph_first.id, webp)
check(libmod.image_kind(webp) == "webp", "planted a WebP thumbnail like v1 left behind")

# GitHub is asked once per session; pretend it has something newer
asked = []
real_check = updater.check


def fake_check(token=None):
    asked.append(token)
    return {"version": "9.9.9", "current": "3.0.0", "notes": ["9.9.9: something new"]}


updater.check = fake_check
c4d.gui.QUESTION_ANSWER[0] = False          # "not now"

dlg = ns["BrowserCommand"]()._dialog()
dlg.CreateLayout()
dlg.InitValues()
check([t[1] for t in dlg.tabs.tabs] == ["Everything", "ambientCG", "Poly Haven", "ShareTextures", "AMD GPUOpen",
                                       "FreePBR", "Installed"], "tabs: Everything, 5 sites, Installed")


print("\nsettings from 3.1:")
check(dlg.GetBool(ui.ID_DISP) and dlg.options().displacement, "displacement is on")
check(dlg.resolution == sources.HIGHEST and dlg.res_options[0] == sources.HIGHEST, "resolution: Highest")


def pump(until, timeout=60):
    t = time.time()
    while time.time() - t < timeout:
        dlg.Timer(None)
        if until():
            return True
        time.sleep(0.05)
    return False


print("\nupdates:")
check(pump(lambda: any("9.9.9" in q for q in c4d.gui.QUESTIONS), 10), "first open: 'BasPBR Browser 9.9.9 is available' popup")
dlg2 = ui.BrowserDialog()
dlg2.CreateLayout()
dlg2.InitValues()
time.sleep(0.3)
check(len(asked) == 1, "opening it again in the same session doesn't ask GitHub again")
dlg2.jobs.cancel_all()
updater.check = real_check
c4d.gui.QUESTIONS.clear()


def click(i, double=False):
    x, y = dlg.grid.tile_rect(i)
    msg = {c4d.BFM_INPUT_DEVICE: c4d.BFM_INPUT_MOUSE, c4d.BFM_INPUT_CHANNEL: c4d.BFM_INPUT_MOUSELEFT,
           c4d.BFM_INPUT_X: x + 30, c4d.BFM_INPUT_Y: y + 30, c4d.BFM_INPUT_QUALIFIER: 0,
           c4d.BFM_INPUT_DOUBLECLICK: double}
    return dlg.grid.InputEvent(msg)


def wheel(value, ctrl=True):
    msg = {c4d.BFM_INPUT_DEVICE: c4d.BFM_INPUT_MOUSE, c4d.BFM_INPUT_CHANNEL: c4d.BFM_INPUT_MOUSEWHEEL,
           c4d.BFM_INPUT_VALUE: value, c4d.BFM_INPUT_QUALIFIER: c4d.QCTRL if ctrl else 0,
           c4d.BFM_INPUT_X: 50, c4d.BFM_INPUT_Y: 50}
    return dlg.grid.InputEvent(msg)


def click_category(label):
    rows = dlg.cats.rows
    i = next((n for n, r in enumerate(rows) if r[1].lower() == label.lower()), None)
    if i is None:
        return None
    msg = {c4d.BFM_INPUT_DEVICE: c4d.BFM_INPUT_MOUSE, c4d.BFM_INPUT_CHANNEL: c4d.BFM_INPUT_MOUSELEFT,
           c4d.BFM_INPUT_X: 20, c4d.BFM_INPUT_Y: dlg.cats.row_y(i) + 5, c4d.BFM_INPUT_QUALIFIER: 0}
    dlg.cats.InputEvent(msg)
    return rows[i]


def click_site(sid, ctrl=False):
    i = [t[0] for t in dlg.cats.toggles].index(sid)
    msg = {c4d.BFM_INPUT_DEVICE: c4d.BFM_INPUT_MOUSE, c4d.BFM_INPUT_CHANNEL: c4d.BFM_INPUT_MOUSELEFT,
           c4d.BFM_INPUT_X: 12, c4d.BFM_INPUT_Y: dlg.cats.toggle_y(i) + 5,
           c4d.BFM_INPUT_QUALIFIER: c4d.QCTRL if ctrl else 0}
    dlg.cats.InputEvent(msg)


def ticked():
    return {t[0] for t in dlg.cats.toggles if t[3]}


def switch(tab):
    dlg.tabs.Select(sources.TABS.index(tab), True)
    dlg.Command(ui.ID_TABS, None)


def searched():
    return not dlg.searching and dlg.grid.items


def check_material(graph, mat):
    ends = {(c.split("<")[0], d) for a, b, c, d in graph.wires}
    ends |= {(l.split("<")[0], p) for l, p, v in graph.values}      # or a constant (AMD's graphs)
    check(("output", "surface") in ends and ("standardmaterial", "base_color") in ends and
          ("standardmaterial", "refl_roughness") in ends and ("standardmaterial", "bump_input") in ends,
          "graph wired: colour, roughness, normal -> bump, surface")
    tags = [t for o in c4d.documents.DOC.selection for t in o.tags]
    check(any(t.get(c4d.TEXTURETAG_MATERIAL) is mat for t in tags), "assigned to the selected objects")


def key(k, where="dialog"):
    if where == "dialog":
        bc = c4d.BaseContainer(c4d.BFM_INPUT)
        bc.update({c4d.BFM_INPUT_DEVICE: c4d.BFM_INPUT_KEYBOARD, c4d.BFM_INPUT_CHANNEL: k})
        return dlg.Message(bc, None)
    msg = {c4d.BFM_INPUT_DEVICE: c4d.BFM_INPUT_KEYBOARD, c4d.BFM_INPUT_CHANNEL: k, c4d.BFM_INPUT_QUALIFIER: 0}
    return (dlg.cats if where == "sidebar" else dlg.grid).InputEvent(msg)


def thumbs_shown(n=6):
    return sum(1 for a in dlg.grid.items[:30] if a.key in ui._bitmaps) >= min(n, len(dlg.grid.items))


# ---- Everything ---------------------------------------------------------------------------
print("\nEverything:")
t0 = time.time()
check(pump(searched, 120), "every site's catalogue loaded in %.1fs: %d materials" % (time.time() - t0, len(dlg.results)))
check(len(dlg.results) > 5000 and not dlg.source.failed, "more than 5000, no site failed %s" % (dlg.source.failed,))
top = {a.source for a in dlg.results[:10]}
check(top == set(sources.ONLINE), "'Mixed' deals the sites out in turn: top 10 come from %s" % sorted(top))
check(pump(lambda: len(dlg.cats.rows) > 20, 60), "merged categories: %d" % (len(dlg.cats.rows) - 1))
check([t[1] for t in dlg.cats.toggles] == ["ambientCG", "Poly Haven", "ShareTextures", "AMD GPUOpen", "FreePBR"]
      and ticked() == set(sources.ONLINE) and all(t[2] for t in dlg.cats.toggles),
      "a ticked box per site above the categories, with counts: %s" % [(t[1], t[2]) for t in dlg.cats.toggles])
check(pump(lambda: thumbs_shown(), 60), "thumbnails load (all sites, C4D-readable)")
check(not c4d.bitmaps.REJECTED, "no thumbnail C4D couldn't read")

print("\nscrolling:")
g = dlg.grid
keys = {a.key for a in dlg.results}
check(len(g.items) == len(dlg.results) and g.scroll == 0, "the whole list is in the grid: %d tiles" % len(g.items))
check(g.content_height() > 100000, "%d px tall - drawn a screen at a time" % g.content_height())
asked = keys & dlg.jobs._thumb_seen
per_screen = g.cols * (g._h() // g.cell_h + 2)
check(0 < len(asked) <= 2 * per_screen, "thumbnails asked for the tiles in or near view only: %d" % len(asked))
check(dlg.GetString(ui.ID_COUNT) == "%d materials" % len(dlg.results), "count: '%s'" % dlg.GetString(ui.ID_COUNT))
check(wheel(-120, ctrl=False) and g.scroll > 0 and g.tile == 128, "mouse wheel down scrolls %d px" % g.scroll)
wheel(120, ctrl=False)
check(g.scroll == 0, "and back up")
key(c4d.KEY_PGDOWN, "grid")
check(g.scroll == g._h() - g.cell_h, "Page Down: %d px" % g.scroll)
key(c4d.KEY_END, "grid")
check(g.scroll == g.max_scroll(), "End: the bottom (%d px)" % g.scroll)
last = g.items[-1]
check(pump(lambda: last.key in ui._bitmaps, 30), "the last tile's thumbnail arrives")
key(c4d.KEY_HOME, "grid")
check(g.scroll == 0, "Home: the top")
top, thumb = g._bar()
c4d.gui.DRAG_SCRIPT[:] = [(c4d.MOUSEDRAGRESULT_CONTINUE, 0, 40), (c4d.MOUSEDRAGRESULT_CONTINUE, 0, 60)]
bar_click = {c4d.BFM_INPUT_DEVICE: c4d.BFM_INPUT_MOUSE, c4d.BFM_INPUT_CHANNEL: c4d.BFM_INPUT_MOUSELEFT,
             c4d.BFM_INPUT_X: g._w() - 5, c4d.BFM_INPUT_Y: top + 5, c4d.BFM_INPUT_QUALIFIER: 0}
g.InputEvent(bar_click)
expect = int(100 * g.max_scroll() / float(g._h() - thumb))
check(abs(g.scroll - expect) <= 1, "dragging the scroll bar 100 px: %d px down the list" % g.scroll)
g.scroll_to(0)
bar_click[c4d.BFM_INPUT_Y] = g._h() - 5
g.InputEvent(bar_click)
check(g.scroll == g._h() - g.cell_h, "a click in the track below: a page down")
g.scroll_to(0)

print("\nthumbnail size:")
t = dlg.grid.tile
wheel(120)
check(dlg.grid.tile == t + ui.TILE_STEP and dlg.GetInt32(ui.ID_SIZE) == t + ui.TILE_STEP,
      "Ctrl + wheel up: %d -> %d px, slider follows" % (t, dlg.grid.tile))
time.sleep(0.05)
bc = c4d.BaseContainer(c4d.BFM_INPUT)
bc.update({c4d.BFM_INPUT_CHANNEL: c4d.BFM_INPUT_MOUSEWHEEL, c4d.BFM_INPUT_QUALIFIER: c4d.QCTRL, c4d.BFM_INPUT_VALUE: -120})
wheel(-120)
dlg.Message(bc, None)                     # the same notch reaching the dialog too
check(dlg.grid.tile == t, "one notch down seen by grid and dialog: one step (%d px)" % dlg.grid.tile)
check(wheel(-120, ctrl=False) and dlg.grid.tile == t and dlg.grid.scroll > 0, "a plain wheel scrolls instead")
dlg.grid.scroll_to(0)
time.sleep(0.02)
c4d.gui.CTRL_HELD[0] = True
wheel(120, ctrl=False)
c4d.gui.CTRL_HELD[0] = False
check(dlg.grid.tile == t + ui.TILE_STEP, "Ctrl seen on the keyboard when the event doesn't carry it")
dlg.set_tile_size(t)
dlg.SetInt32(ui.ID_SIZE, 300)
dlg.Command(ui.ID_SIZE, None)
check(dlg.grid.tile == 300 and dlg.grid.cols == 2, "Size slider: 300 px, %d columns" % dlg.grid.cols)
for _ in range(30):
    time.sleep(0.011)
    wheel(-120)
check(dlg.grid.tile == ui.TILE_MIN, "never smaller than %d px" % ui.TILE_MIN)
dlg.set_tile_size(128)

print("\narrow keys:")
rows = [r[0] for r in dlg.cats.rows]
check(dlg.cats.selected == sources.ALL, "starts on All")
key(c4d.KEY_DOWN)
check(dlg.cats.selected == rows[1] and dlg.category == rows[1], "Down (dialog): %s" % rows[1])
check(pump(lambda: searched() and all(rows[1] in a.extra["canon"] for a in dlg.results), 5),
      "the results follow once the key rests: %d" % len(dlg.results))
time.sleep(0.02)
key(c4d.KEY_DOWN, "sidebar")
key(c4d.KEY_DOWN)                          # the same press reaching the dialog too
check(dlg.cats.selected == rows[2], "Down in the sidebar: %s (one step, not two)" % dlg.cats.selected)
time.sleep(0.02)
key(c4d.KEY_DOWN, "grid")
check(dlg.cats.selected == rows[3], "Down with the grid focused: %s" % dlg.cats.selected)
for _ in range(3):
    time.sleep(0.02)
    key(c4d.KEY_UP)
check(dlg.cats.selected == sources.ALL, "Up x3: back on All")
time.sleep(0.02)
key(c4d.KEY_UP)
check(dlg.cats.selected == rows[-1], "Up from All wraps to the last: %s" % rows[-1])
time.sleep(0.02)
key(c4d.KEY_DOWN)
check(dlg.cats.selected == sources.ALL, "and Down wraps back to All")
pump(searched, 10)

print("\nresolutions:")
ph = sources.SOURCES["polyhaven"]
a8 = next(a for a in ph.catalogue() if a.max_res == "8K")
a16 = next(a for a in ph.catalogue() if a.max_res == "16K")
check(ph.available(a8)[-1] == "8K" and ph.available(a16)[-1] == "16K", "Poly Haven per material: %s has %s, %s has %s"
      % (a8.id, ph.available(a8), a16.id, ph.available(a16)))
check(sources.choose_res(ph.available(a8), "16K") == "8K" and sources.choose_res(ph.available(a8), sources.HIGHEST) == "8K",
      "asking 16K or Highest of an 8K material gets 8K")
check(sources.choose_res(["1K", "2K", "4K"], "2K") == "2K" and sources.choose_res(["4K", "8K"], "2K") == "4K",
      "a size it has is used as is; below its smallest, its smallest")
EXPECT = {"ambientcg": ["Highest", "16K", "12K", "8K", "4K", "2K", "1K"],
          "polyhaven": ["Highest", "16K", "8K", "4K", "2K", "1K"],
          "sharetextures": ["Highest", "4K", "2K", "1K"], "amd": ["Highest", "8K", "4K", "2K", "1K"],
          "freepbr": ["2K"], "installed": []}
for tid, want in EXPECT.items():
    switch(tid)
    check(dlg.res_options == want and dlg.enabled.get(ui.ID_RES) == (len(want) > 1),
          "%s menu: %s" % (tid, dlg.res_options or "(off)"))
switch("everything")
check(dlg.res_options == EXPECT["ambientcg"], "Everything: every size any ticked site has")
click_site("ambientcg")
click_site("polyhaven")
check(dlg.res_options == ["Highest", "8K", "4K", "2K", "1K"], "without ambientCG and Poly Haven: %s" % dlg.res_options)
click_site("ambientcg")
click_site("polyhaven")
pump(searched, 20)

# the size decides the library folder and the material's name (fake site: no big downloads)
amd = sources.SOURCES["amd"]
fake = sources.Asset("amd", "fake-material", "Fake Material", max_res="")
real_available, real_download = sources.AMDMatLib.available, sources.AMDMatLib.download
asked_for = []


def fake_download(self, asset, res, work, progress, cancel):
    asked_for.append(res)
    path = os.path.join(work, "fake_basecolor.jpg")
    with open(path, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0" + b"0" * 64)
    return libmod.MapSet({"color": path})          # AMD downloads always carry their graph


sources.AMDMatLib.available = lambda self, asset: ["1K", "2K"]
sources.AMDMatLib.download = fake_download
folder = sources.fetch(libmod.Library(LIB), fake, sources.HIGHEST)[0]
check(asked_for == ["2K"] and os.path.basename(folder) == libmod.safe_name(fake.name) + "_2K", "Highest of a 1K/2K material: 2K, folder %s" % os.path.basename(folder))
folder, maps, reused = sources.fetch(libmod.Library(LIB), fake, "8K")
check(reused and asked_for == ["2K"], "asking 8K of it reuses the 2K already in the library")
folder = sources.fetch(libmod.Library(LIB), fake, "1K")[0]
check(asked_for == ["2K", "1K"] and folder.endswith("_1K"), "asking 1K downloads 1K")
sources.AMDMatLib.available, sources.AMDMatLib.download = real_available, real_download
shutil.rmtree(os.path.join(LIB, "AMD GPUOpen"), ignore_errors=True)

print("\nsearch and categories across sites:")
dlg.SetString(ui.ID_QUERY, "marble")
dlg.Command(ui.ID_QUERY, None)
check(pump(lambda: searched() and all(a.matches("marble") for a in dlg.results), 10), "typing searches every site: %d results for 'marble'" % len(dlg.results))
check(len({a.source for a in dlg.results}) >= 4, "from %s" % sorted({a.source for a in dlg.results}))
dlg.SetString(ui.ID_QUERY, "")
dlg.search()
pump(searched, 10)
row = click_category("Wood")
check(row is not None and row[2] > 300, "merged 'Wood' category: %s" % (row,))
pump(lambda: searched() and len(dlg.results) == row[2], 10)
check(len(dlg.results) == row[2] and len({a.source for a in dlg.results}) == 5,
      "Wood shows %d materials from all five sites" % len(dlg.results))
wood_all = row[2]

print("\nchoosing sites:")
click_site("sharetextures")
check(ticked() == set(sources.ONLINE) - {"sharetextures"}, "untick ShareTextures")
check(pump(lambda: searched() and "sharetextures" not in {a.source for a in dlg.results}
           and dlg.cats.rows[0][0] == sources.ALL and len(dlg.cats.rows) > 5, 20), "it's left out of the results")
row = next((r for r in dlg.cats.rows if r[0] == "Wood"), None)
check(row is not None and row[2] < wood_all and dlg.cats.selected == "Wood",
      "merged categories recount: Wood %s -> %s, still selected" % (wood_all, row and row[2]))
check(pump(lambda: searched() and len(dlg.results) == row[2], 10), "and the results follow: %d" % len(dlg.results))
dlg.category_changed(sources.ALL)
pump(searched, 10)
click_site("amd", ctrl=True)
check(ticked() == {"amd"}, "Ctrl-click AMD GPUOpen: only that site")
check(pump(lambda: searched() and {a.source for a in dlg.results} == {"amd"} and "Wallpaper" in [r[0] for r in dlg.cats.rows], 30),
      "%d AMD materials, categories just AMD's (%d)" % (len(dlg.results), len(dlg.cats.rows) - 1))
click_site("amd")
check(ticked() == {"amd"} and "stay ticked" in dlg.GetString(ui.ID_STATUS), "the last ticked site can't be unticked")
click_site("amd", ctrl=True)
check(ticked() == set(sources.ONLINE), "Ctrl-click it again: every site")
pump(lambda: searched() and len({a.source for a in dlg.results}) == 5, 20)
dlg.AskClose()
with open(os.path.join(c4d.storage.PREFS, "baspbr_browser.json")) as f:
    check(json.load(f).get("sites_off") == [], "remembered (none left out)")
click_site("freepbr")
check(dlg.cfg["sites_off"] == ["freepbr"], "leaving FreePBR out is remembered as sites_off")
click_site("freepbr")

# ---- each site ------------------------------------------------------------------------------
CATEGORY = {"ambientcg": "Marble", "polyhaven": "Wood", "sharetextures": "Wood", "amd": "Wood", "freepbr": "Wood"}
downloaded = []
for sid in sources.ONLINE:
    print("\n%s:" % sid)
    switch(sid)
    check(pump(lambda: len(dlg.cats.rows) > 5, 60), "categories loaded (%d)" % (len(dlg.cats.rows) - 1))
    check(pump(searched, 90), "unfiltered: %d results" % len(dlg.results))
    row = click_category(CATEGORY[sid])
    check(row is not None, "category '%s' is in the sidebar" % CATEGORY[sid])
    if row is None:
        continue
    pump(lambda: searched() and len(dlg.results) == row[2], 30)
    in_cat = all(any(CATEGORY[sid].lower() in c.lower() for c in a.categories) for a in dlg.results)
    check(len(dlg.results) == row[2], "filtered to %s: %d results (sidebar said %s)" % (row[1], len(dlg.results), row[2]))
    check(in_cat, "every result is in that category")
    pump(lambda: thumbs_shown(), 40)
    check(thumbs_shown(), "thumbnails load in a C4D-readable format")
    if "1K" in dlg.res_options:
        dlg.SetInt32(ui.ID_RES, dlg.res_options.index("1K"))
        dlg.Command(ui.ID_RES, None)
    before = len(maxon.GRAPHS)
    click(0)
    pump(lambda: dlg.grid.items[0].key in dlg.sizes, 20)
    check(("2K only" if sid == "freepbr" else "downloads 1K") in dlg.GetString(ui.ID_STATUS),
          "selected: '%s'" % dlg.GetString(ui.ID_STATUS))
    click(0, double=True)
    asset = dlg.grid.items[0]
    pump(lambda: len(maxon.GRAPHS) > before or dlg.state.get(asset.key) == "error", 300)
    check(len(maxon.GRAPHS) > before, "double-click downloaded '%s' and built it" % asset.name)
    if len(maxon.GRAPHS) > before:
        check_material(maxon.GRAPHS[-1], c4d.documents.DOC.materials[-1])
        downloaded.append(asset)
        name = c4d.documents.DOC.materials[-1].name
        check(name.endswith(" 2K" if sid == "freepbr" else " 1K"), "named after the size it has: '%s'" % name)
    dlg.category_changed(sources.ALL)

# ---- AMD: the MaterialX graph decides, not the file names ------------------------------------
print("\nAMD MaterialX graphs:")


def feeding(g, label, port):
    """What feeds label.port, walking back through Change Range and Color Splitter
    nodes to the texture: [(node, its output port), ...]."""
    chain = []
    while True:
        src = next(((a, b) for a, b, c, d in g.wires if c == label and d == port), None)
        if src is None:
            return chain
        chain.append(src)
        if not src[0].startswith(("rsmathrange", "rscolorsplitter")):
            return chain
        label, port = src[0], "input"


def values(g, label):
    return {p: v for l, p, v in g.values if l == label}


def near(a, b):
    return a is not None and abs(a - b) < 1e-3


# name: roughness (splitter output, remap or None for 0..1), metalness (a constant, or a
# splitter output of the same file as roughness), tiling, constant base colour (or None)
AMD_CASES = [
    ("Grey Chateau Solid Granite", ("outr", (0.0, 0.915)), 0.0, 2.0, None),        # R of _Mask.png
    ("Sandstone", ("outg", None), 0.0, 2.0, None),                                    # G of _mask.png
    ("Aluminum Brushed", ("outr", (0.0978, 0.6088)), 1.0, 10.0, None),                # base layer of 4
    ("Gold Parametric Imperfections", ("outr", None), 1.0, 1.0, (1.0, 0.7605, 0.3278)),  # no colour map
    ("Copper Brushed", ("outr", None), 1.0, 2.0, None),
    ("Indigo Palm Wallpaper", ("outg", (0.25, 1.0)), "outb", 2.0, None),             # ORM: G and B
]
lib = libmod.Library(LIB)
amd_by_name = {a.name: a for a in sources.SOURCES["amd"].catalogue()}
for name, (rport, rrange), metal, tiling, colour in AMD_CASES:
    asset = amd_by_name.get(name)
    check(asset is not None, "%s is in AMD's catalogue" % name)
    if asset is None:
        continue
    folder, maps, reused = sources.fetch(lib, asset, "1K")
    mat, problems = ui.rsmat.build(c4d.documents.DOC, name, maps)
    g = maxon.GRAPHS[-1]
    std = next(n.label() for n in g.nodes if n.type.endswith("standardmaterial"))
    sv = values(g, std)
    print("  %s:" % name)

    rough = feeding(g, std, "refl_roughness")
    kinds = [l.split("<")[0] for l, p in rough]
    want = (["rsmathrange"] if rrange else []) + ["rscolorsplitter", "texturesampler"]
    split_port = next((p for l, p in rough if l.startswith("rscolorsplitter")), None)
    check(kinds == want and split_port == rport, "  roughness: %s" % " <- ".join("%s.%s" % (l, p) for l, p in rough))
    if rrange:
        rv = values(g, rough[0][0])
        check(near(rv.get("old_min"), 0) and near(rv.get("old_max"), 1) and near(rv.get("new_min"), rrange[0])
              and near(rv.get("new_max"), rrange[1]), "  remapped 0..1 -> %.3f..%.3f: %s" % (rrange + (rv,)))

    if isinstance(metal, str):
        m = feeding(g, std, "metalness")
        check([p for l, p in m] == [metal, "outcolor"] and m[0][0] == rough[-2][0],
              "  metalness: %s, the same file and Color Splitter as roughness"
              % " <- ".join("%s.%s" % (l, p) for l, p in m))
        texs = [n for n in g.nodes if n.type.endswith("texturesampler")]
        check(len(texs) == len({values(g, n.label()).get("path") for n in texs}), "  one Texture node per file")
    else:
        check(near(sv.get("metalness"), metal) and not feeding(g, std, "metalness"),
              "  metalness: the constant %s" % sv.get("metalness"))

    if colour:
        bc = sv.get("base_color")
        check(not feeding(g, std, "base_color") and bc is not None and all(near(a, b) for a, b in zip(bc, colour)),
              "  base colour: the constant %s (linear), no texture" % (bc,))
    else:
        check([l.split("<")[0] for l, p in feeding(g, std, "base_color")] == ["texturesampler"],
              "  base colour: a texture")

    scales = [values(g, n.label()).get("scale") for n in g.nodes if n.type.endswith("texturesampler")]
    if tiling == 1.0:
        check(scales and all(s is None for s in scales), "  tiling 1: no Scale set")
    else:
        check(scales and all(s == maxon.Vector2d(tiling, tiling) for s in scales),
              "  tiling %gx on all %d Texture nodes" % (tiling, len(scales)))
    check(not problems, "  no Redshift ports skipped %s" % (problems or ""))
    with open(os.path.join(folder, "asset.json")) as f:
        stored = json.load(f).get("graph")
    check(stored == maps.graph() and stored, "  asset.json keeps the graph: %s" % stored)
    if name == "Indigo Palm Wallpaper":
        check(maps["roughness"] == maps["metalness"] and maps["roughness"].endswith("_ORM.png") and "height" in maps,
              "  the shared ORM file is stored once; height added by name: %s"
              % sorted("%s=%s" % (r, os.path.basename(p)) for r, p in maps.items()))

granite = amd_by_name["Grey Chateau Solid Granite"]
folder, maps, reused = sources.fetch(lib, granite, "1K")
ch = maps.channels.get("roughness")
check(reused and ch is not None and ch.index == 0 and near(ch.max, 0.915) and maps.metalness == 0.0 and maps.tiling == 2.0,
      "fetched again: from the library, graph and all (%s)" % (ch,))
sand = amd_by_name["Sandstone"]
info_path = os.path.join(lib.asset_dir(sand, "1K"), "asset.json")
with open(info_path) as f:
    info = json.load(f)
info.pop("graph")                            # as 3.2 wrote it: maps guessed from names
with open(info_path, "w") as f:
    json.dump(info, f)
folder, maps, reused = sources.fetch(lib, sand, "1K")
check(not reused and maps.channels.get("roughness") and maps.channels["roughness"].index == 1,
      "an AMD material an older version downloaded (no graph) is downloaded again")

print("\nPoly Haven thumbnail repair:")
switch("polyhaven")
dlg.SetString(ui.ID_QUERY, ph_first.name)
dlg.search()
pump(lambda: any(a.id == ph_first.id and a.key in ui._bitmaps for a in dlg.grid.items), 40)
check(libmod.image_kind(webp) == "png", "the WebP thumbnail was replaced by a PNG")
check(any(a.id == ph_first.id and a.key in ui._bitmaps for a in dlg.grid.items), "and it shows")
dlg.SetString(ui.ID_QUERY, "")

print("\nthumbnail memory:")
cap, ui.MAX_BITMAPS = ui.MAX_BITMAPS, 5
forgot = []
real_forget, dlg.jobs.forget_thumb = dlg.jobs.forget_thumb, forgot.append
ui._remember("x:1", ui._bitmaps[next(iter(ui._bitmaps))], dlg.jobs)
check(len(ui._bitmaps) == 5 and len(forgot) > 0, "past the cap the oldest go, to reload if seen again (%d dropped)" % len(forgot))
ui.MAX_BITMAPS, dlg.jobs.forget_thumb = cap, real_forget
ui._bitmaps.pop("x:1", None)

# ---- Installed ---------------------------------------------------------------------------
print("\nInstalled tab:")
manual = os.path.join(LIB, "Studio scans", "Oak_Wall_4K")
os.makedirs(manual)
first_entry = libmod.Library(LIB).scan()[0]
for role, fn in (("color", "oak_wall_albedo.jpg"), ("normal_gl", "oak_wall_normal.jpg"), ("roughness", "oak_wall_rough.jpg")):
    src = first_entry["maps"].get(role) or first_entry["maps"]["color"]
    shutil.copy(src, os.path.join(manual, fn))
switch("installed")
check(pump(lambda: dlg.scanned and len(dlg.grid.items) >= len(downloaded) + 1, 30),
      "lists %d materials (%d downloaded + 1 hand-made folder)" % (len(dlg.grid.items), len(downloaded)))
labels = [r[1] for r in dlg.cats.rows]
check(all(s in labels for s in ("ambientCG", "Poly Haven", "ShareTextures", "AMD GPUOpen", "FreePBR", "Local")),
      "sidebar groups by source: %s" % labels[:7])
click_category("Local")
pump(lambda: len(dlg.grid.items) == 1, 10)
check(len(dlg.grid.items) == 1 and "Oak Wall" in dlg.grid.items[0].name, "Local category shows the hand-made folder")
pump(lambda: dlg.grid.items and dlg.grid.items[0].key in ui._bitmaps, 10)
check(dlg.grid.items and dlg.grid.items[0].key in ui._bitmaps, "a thumbnail was drawn from its colour map")

real_download, real_get = net.download, net.get_bytes


def no_network(*a, **k):
    raise AssertionError("network used while building from the library")


net.download, net.get_bytes = no_network, no_network
before = len(maxon.GRAPHS)
click(0, double=True)
check(len(maxon.GRAPHS) == before + 1, "double-click built it instantly, no network")
if len(maxon.GRAPHS) > before:
    check_material(maxon.GRAPHS[-1], c4d.documents.DOC.materials[-1])
dlg.category_changed(sources.ALL)
pump(lambda: len(dlg.grid.items) >= len(downloaded) + 1, 10)
before = len(maxon.GRAPHS)
dlg.grid.selected = set(range(len(dlg.grid.items)))
dlg.create()
check(len(maxon.GRAPHS) == before + len(dlg.grid.items), "multi-select builds all %d, still no network" % len(dlg.grid.items))
net.download, net.get_bytes = real_download, real_get

# ---- shared library: two PCs, one download ------------------------------------------------
print("\nshared library:")
lib = libmod.Library(LIB)
fresh_ones = [a for a in sources.SOURCES["ambientcg"].search("", "Popular")[:40] if not lib.find(a, "1K")]
fresh, stale = fresh_ones[0], fresh_ones[1]
calls = []
orig = sources.AmbientCG.download


def counted(self, asset, res, work, progress, cancel):
    calls.append(threading.current_thread().name)
    time.sleep(1.0)                        # long enough for the other "PC" to collide
    return orig(self, asset, res, work, progress, cancel)


sources.AmbientCG.download = counted
results, waits = {}, []
threads = [threading.Thread(name=n, target=lambda n=n: results.__setitem__(
    n, sources.fetch(lib, fresh, "1K", waiting=lambda h: waits.append(h)))) for n in ("pcA", "pcB")]
[t.start() for t in threads]
[t.join(120) for t in threads]
sources.AmbientCG.download = orig
check(len(calls) == 1, "two PCs asking for %s at once: downloaded once (%d)" % (fresh.name, len(calls)))
check(len(waits) == 1, "the other one waited for it")
check(len(results) == 2 and results["pcA"][0] == results["pcB"][0], "both got the same library folder")
check(os.listdir(os.path.join(LIB, "_incoming")) == [], "no locks or half-made folders left in _incoming")

lockp = lib._lock_path(stale, "1K")
with open(lockp, "w") as f:
    f.write("CRASHED-PC\n")
old = time.time() - libmod.LOCK_STALE_SECONDS - 60
os.utime(lockp, (old, old))
folder, maps, reused = sources.fetch(lib, stale, "1K")
check(not reused and maps.get("color"), "a stale lock (crashed PC) is taken over")

print("\nlibrary folders:")
check(libmod.unc_path("Y:\\") is not None or os.name != "nt" or not os.path.isdir("Y:\\"),
      "mapped drive Y: resolves to %s" % libmod.unc_path("Y:\\"))
other = tempfile.mkdtemp(prefix="baspbr_lib2_")
dlg.set_library(other, ask_unc=False)
pump(lambda: dlg.scanned, 20)
check(dlg.local == set() and dlg.GetString(ui.ID_LIB_INFO).startswith("0 material"),
      "switching to an empty folder rescans: '%s'" % dlg.GetString(ui.ID_LIB_INFO))
dlg.set_library(LIB, ask_unc=False)
pump(lambda: dlg.scanned and dlg.local, 20)
check(len(dlg.local) >= len(downloaded), "and back: %s" % dlg.GetString(ui.ID_LIB_INFO))

print("\nsettings:")
dlg.set_tile_size(176)
dlg.AskClose()
with open(os.path.join(c4d.storage.PREFS, "baspbr_browser.json")) as f:
    saved = json.load(f)
check(saved.get("tile") == 176 and saved.get("source") == "installed", "tile size and tab are remembered")

print("\nRedshift id check (fake node space):")
check(ui.rsmat.check_ids(), "all ids resolve")

shutil.rmtree(other, ignore_errors=True)
print("\n%s" % ("ALL PASSED" if not failures else "%d FAILED:\n  %s" % (len(failures), "\n  ".join(failures))))
print("(test library left at %s)" % LIB)
sys.exit(1 if failures else 0)
