"""End-to-end check of the plugin outside Cinema 4D.

Runs the real plugin code against fake c4d/maxon modules (tests/fakes; the fake
bitmap loader refuses WebP, like Cinema 4D). Live network. Checks:

  * the plugin registers, the browser opens, the update popup shows once per session
  * Everything: every site in one list, sites mixed near the top, merged categories,
    one search across all sites
  * endless scrolling: more tiles arrive as the view nears the end
  * Ctrl + mouse wheel and the Size slider resize the tiles (one step per notch)
  * per site: categories arrive, filtering by one works, thumbnails load in a format
    C4D reads (including repairing a WebP thumbnail an old version cached), a
    double-clicked material downloads and becomes a correctly wired Redshift
    material assigned to the selection
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
    json.dump({"library": LIB, "resolution": "1K", "source": "everything", "query": "",
               "assign": True, "displacement": True}, f)

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
check([t[1] for t in dlg.tabs.tabs] == ["Everything", "ambientCG", "Poly Haven", "ShareTextures", "FreePBR", "Installed"],
      "tabs: Everything, 4 sites, Installed")


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
           c4d.BFM_INPUT_X: 20, c4d.BFM_INPUT_Y: 3 + i * ui.ROW_H + 5}
    dlg.cats.InputEvent(msg)
    return rows[i]


def switch(tab):
    dlg.tabs.Select(sources.TABS.index(tab), True)
    dlg.Command(ui.ID_TABS, None)


def searched():
    return not dlg.searching and dlg.grid.items


def check_material(graph, mat):
    ends = {(c.split("<")[0], d) for a, b, c, d in graph.wires}
    check(("output", "surface") in ends and ("standardmaterial", "base_color") in ends and
          ("standardmaterial", "refl_roughness") in ends and ("standardmaterial", "bump_input") in ends,
          "graph wired: colour, roughness, normal -> bump, surface")
    tags = [t for o in c4d.documents.DOC.selection for t in o.tags]
    check(any(t.get(c4d.TEXTURETAG_MATERIAL) is mat for t in tags), "assigned to the selected objects")


def thumbs_shown(n=6):
    return sum(1 for a in dlg.grid.items[:30] if a.key in ui._bitmaps) >= min(n, len(dlg.grid.items))


# ---- Everything ---------------------------------------------------------------------------
print("\nEverything:")
t0 = time.time()
check(pump(searched, 120), "every site's catalogue loaded in %.1fs: %d materials" % (time.time() - t0, len(dlg.results)))
check(len(dlg.results) > 4000 and not dlg.source.failed, "more than 4000, no site failed %s" % (dlg.source.failed,))
top = {a.source for a in dlg.results[:8]}
check(top == set(sources.ONLINE), "'Mixed' deals the sites out in turn: top 8 come from %s" % sorted(top))
check(pump(lambda: len(dlg.cats.rows) > 20, 60), "merged categories: %d" % (len(dlg.cats.rows) - 1))
labels = [r[1] for r in dlg.cats.rows]
check(labels[1:5] == ["ambientCG", "Poly Haven", "ShareTextures", "FreePBR"], "sites listed first in the sidebar")
check(pump(lambda: thumbs_shown(), 60), "thumbnails load (all sites, C4D-readable)")
check(not c4d.bitmaps.REJECTED, "no thumbnail C4D couldn't read")

print("\nendless scrolling:")
first = len(dlg.grid.items)
check(first == ui.CHUNK and first < len(dlg.results), "starts with %d tiles of %d" % (first, len(dlg.results)))
pump(lambda: False, 0.3)
check(len(dlg.grid.items) == first, "nothing more while the view is at the top")
dlg.visible = {"x1": 0, "y1": dlg.grid.content_height() - 600, "x2": 700, "y2": dlg.grid.content_height()}
check(pump(lambda: len(dlg.grid.items) > first, 5), "scrolled to the end: %d more tiles arrive" % (len(dlg.grid.items) - first))
check(dlg.GetString(ui.ID_COUNT).startswith("%d materials" % len(dlg.results)), "count: '%s'" % dlg.GetString(ui.ID_COUNT))

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
check(wheel(120, ctrl=False) is False and dlg.grid.tile == t, "a plain wheel scrolls instead")
dlg.SetInt32(ui.ID_SIZE, 300)
dlg.Command(ui.ID_SIZE, None)
check(dlg.grid.tile == 300 and dlg.grid.cols == 2, "Size slider: 300 px, %d columns" % dlg.grid.cols)
for _ in range(30):
    time.sleep(0.011)
    wheel(-120)
check(dlg.grid.tile == ui.TILE_MIN, "never smaller than %d px" % ui.TILE_MIN)
dlg.set_tile_size(128)

print("\nsearch and categories across sites:")
dlg.SetString(ui.ID_QUERY, "marble")
dlg.Command(ui.ID_QUERY, None)
check(pump(lambda: searched() and all(a.matches("marble") for a in dlg.results), 10), "typing searches every site: %d results for 'marble'" % len(dlg.results))
check(len({a.source for a in dlg.results}) >= 3, "from %s" % sorted({a.source for a in dlg.results}))
dlg.SetString(ui.ID_QUERY, "")
dlg.search()
pump(searched, 10)
row = click_category("Wood")
check(row is not None and row[2] > 300, "merged 'Wood' category: %s" % (row,))
pump(lambda: searched() and len(dlg.results) == row[2], 10)
check(len(dlg.results) == row[2] and len({a.source for a in dlg.results}) == 4,
      "Wood shows %d materials from all four sites" % len(dlg.results))
row = click_category("Poly Haven")
pump(lambda: searched() and {a.source for a in dlg.results} == {"polyhaven"}, 10)
check({a.source for a in dlg.results} == {"polyhaven"} and len(dlg.results) == row[2], "site row filters to one site")
dlg.category_changed(sources.ALL)

# ---- each site ------------------------------------------------------------------------------
CATEGORY = {"ambientcg": "Marble", "polyhaven": "Wood", "sharetextures": "Wood", "freepbr": "Wood"}
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
    before = len(maxon.GRAPHS)
    click(0)
    click(0, double=True)
    asset = dlg.grid.items[0]
    pump(lambda: len(maxon.GRAPHS) > before or dlg.state.get(asset.key) == "error", 300)
    check(len(maxon.GRAPHS) > before, "double-click downloaded '%s' and built it" % asset.name)
    if len(maxon.GRAPHS) > before:
        check_material(maxon.GRAPHS[-1], c4d.documents.DOC.materials[-1])
        downloaded.append(asset)
    dlg.category_changed(sources.ALL)

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
check(all(s in labels for s in ("ambientCG", "Poly Haven", "ShareTextures", "FreePBR", "Local")),
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
