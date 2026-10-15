"""End-to-end check of the plugin outside Cinema 4D.

Runs the real plugin code against fake c4d/maxon modules (tests/fakes; the fake
bitmap loader refuses WebP, like Cinema 4D). Live network. Checks:

  * the plugin registers and the browser opens
  * per online source: categories arrive, filtering by one works, thumbnails
    load (including repairing a WebP thumbnail left in the cache by v1), a
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
    json.dump({"library": LIB, "resolution": "1K", "source": "ambientcg", "query": "",
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

from baspbr import library as libmod, net, sources, ui  # noqa: E402

# v1 cached Poly Haven thumbnails as WebP under a .png name; plant one to see it repaired
ph_first = sorted(sources.PolyHaven()._catalogue(), key=lambda a: a.name.lower())[0]
webp = os.path.join(LIB, "_thumbs", "Poly Haven", ph_first.id + ".png")
os.makedirs(os.path.dirname(webp), exist_ok=True)
net.download("https://cdn.polyhaven.com/asset_img/thumbs/%s.png?width=256&height=256&format=webp" % ph_first.id, webp)
check(libmod.image_kind(webp) == "webp", "planted a WebP thumbnail like v1 left behind")

dlg = ns["BrowserCommand"]()._dialog()
dlg.CreateLayout()
dlg.InitValues()
check([t[1] for t in dlg.tabs.tabs] == ["ambientCG", "Poly Haven", "FreePBR", "Installed"], "tabs: 3 sources + Installed")


def pump(until, timeout=60):
    t = time.time()
    while time.time() - t < timeout:
        dlg.Timer(None)
        if until():
            return True
        time.sleep(0.05)
    return False


def click(i, double=False):
    x, y = dlg.grid.tile_rect(i)
    msg = {c4d.BFM_INPUT_DEVICE: c4d.BFM_INPUT_MOUSE, c4d.BFM_INPUT_CHANNEL: c4d.BFM_INPUT_MOUSELEFT,
           c4d.BFM_INPUT_X: x + 30, c4d.BFM_INPUT_Y: y + 30, c4d.BFM_INPUT_QUALIFIER: 0,
           c4d.BFM_INPUT_DOUBLECLICK: double}
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


def check_material(graph, mat):
    ends = {(c.split("<")[0], d) for a, b, c, d in graph.wires}
    check(("output", "surface") in ends and ("standardmaterial", "base_color") in ends and
          ("standardmaterial", "refl_roughness") in ends and ("standardmaterial", "bump_input") in ends,
          "graph wired: colour, roughness, normal -> bump, surface")
    tags = [t for o in c4d.documents.DOC.selection for t in o.tags]
    check(any(t.get(c4d.TEXTURETAG_MATERIAL) is mat for t in tags), "assigned to the selected objects")


CATEGORY = {"ambientcg": "Marble", "polyhaven": "Wood", "freepbr": "Wood"}
downloaded = []
for sid in sources.ORDER:
    print("\n%s:" % sid)
    switch(sid)
    check(pump(lambda: len(dlg.cats.rows) > 5, 60), "categories loaded (%d)" % (len(dlg.cats.rows) - 1))
    check(pump(lambda: len(dlg.grid.items) > 0, 90), "unfiltered search: %d results" % dlg.total)
    row = click_category(CATEGORY[sid])
    check(row is not None, "category '%s' is in the sidebar" % CATEGORY[sid])
    if row is None:
        continue
    pump(lambda: len(dlg.grid.items) > 0 and dlg.total <= (row[2] or 10 ** 6), 60)
    in_cat = all(any(CATEGORY[sid].lower() in c.lower() for c in a.categories) for a in dlg.grid.items)
    check(dlg.total == row[2] or (sid != "ambientcg" and dlg.total <= row[2]),
          "filtered to %s: %d results (sidebar said %s)" % (row[1], dlg.total, row[2]))
    check(in_cat, "every result is in that category")
    pump(lambda: sum(1 for a in dlg.grid.items if a.key in ui._bitmaps) >= min(6, len(dlg.grid.items)), 40)
    check(sum(1 for a in dlg.grid.items if a.key in ui._bitmaps) >= min(6, len(dlg.grid.items)),
          "thumbnails load in a C4D-readable format")
    before = len(maxon.GRAPHS)
    target = 0
    click(target)
    click(target, double=True)
    asset = dlg.grid.items[target]
    pump(lambda: len(maxon.GRAPHS) > before or dlg.state.get(asset.key) == "error", 300)
    check(len(maxon.GRAPHS) > before, "double-click downloaded '%s' and built it" % asset.name)
    if len(maxon.GRAPHS) > before:
        check_material(maxon.GRAPHS[-1], c4d.documents.DOC.materials[-1])
        downloaded.append(asset)
    dlg.cats.selected = sources.ALL
    dlg.category_changed(sources.ALL)

print("\nPoly Haven thumbnail repair:")
switch("polyhaven")
dlg.SetString(ui.ID_QUERY, ph_first.name)
dlg.search(0)
pump(lambda: any(a.id == ph_first.id and a.key in ui._bitmaps for a in dlg.grid.items), 40)
check(libmod.image_kind(webp) == "png", "the WebP thumbnail was replaced by a PNG")
check(any(a.id == ph_first.id and a.key in ui._bitmaps for a in dlg.grid.items), "and it shows")
dlg.SetString(ui.ID_QUERY, "")

# ---- Installed ---------------------------------------------------------------------------
print("\nInstalled tab:")
manual = os.path.join(LIB, "Studio scans", "Oak_Wall_4K")
os.makedirs(manual)
for role, fn in (("color", "oak_wall_albedo.jpg"), ("normal_gl", "oak_wall_normal.jpg"), ("roughness", "oak_wall_rough.jpg")):
    shutil.copy(libmod.Library(LIB).scan()[0]["maps"].get(role) or libmod.Library(LIB).scan()[0]["maps"]["color"],
                os.path.join(manual, fn))
switch("installed")
check(pump(lambda: dlg.scanned and len(dlg.grid.items) >= len(downloaded) + 1, 30),
      "lists %d materials (%d downloaded + 1 hand-made folder)" % (len(dlg.grid.items), len(downloaded)))
labels = [r[1] for r in dlg.cats.rows]
check(all(s in labels for s in ("ambientCG", "Poly Haven", "FreePBR", "Local")), "sidebar groups by source: %s" % labels[:6])
local_row = click_category("Local")
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
fresh = sources.SOURCES["ambientcg"].search("", "Popular", 3, 2)[0][-1]
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

stale = sources.SOURCES["ambientcg"].search("", "Popular", 4, 2)[0][-1]
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
check(dlg.local == set() and dlg.GetString(ui.ID_LIB_INFO).startswith("0 material"), "switching to an empty folder rescans: '%s'" % dlg.GetString(ui.ID_LIB_INFO))
dlg.set_library(LIB, ask_unc=False)
pump(lambda: dlg.scanned and dlg.local, 20)
check(len(dlg.local) >= len(downloaded), "and back: %s" % dlg.GetString(ui.ID_LIB_INFO))

print("\nRedshift id check (fake node space):")
check(ui.rsmat.check_ids(), "all ids resolve")

shutil.rmtree(other, ignore_errors=True)
print("\n%s" % ("ALL PASSED" if not failures else "%d FAILED:\n  %s" % (len(failures), "\n  ".join(failures))))
print("(test library left at %s)" % LIB)
sys.exit(1 if failures else 0)
