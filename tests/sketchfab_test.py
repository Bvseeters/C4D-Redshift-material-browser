"""Sketchfab models, end to end outside Cinema 4D (fake c4d / maxon, as smoke_test.py).

Live: Sketchfab's public search, categories and paging. Downloading needs a user's
token, so for the import a fake Sketchfab on localhost answers /models/{uid} and
/models/{uid}/download (checking the token) and serves Khronos' glTF sample models
(downloaded once into the local cache).

  * glTF reader: a hand-made two-material quad - z mirrored, triangles reversed,
    UVs kept, a UV seam welded but a hard edge kept, node transforms, constants;
    Draco-compressed geometry refused with a clear message
  * the Sketchfab tab: Max faces menu, filter boxes, categories, licence badges,
    face counts, paging as you scroll, commercial-only, max faces, a link typed
    into the search on another tab
  * the models folder: separate from the materials library, set on the tab
  * Paste link: clipboard -> token asked once -> download (token only to the API,
    never to the file link) -> objects with Redshift materials; the folder keeps
    model.glb, textures, preview, ATTRIBUTION.txt, asset.json
  * the same model again: from the folder, no network; Installed lists it and builds it
  * non-commercial: asked first; a refused token: said so

    python tests/sketchfab_test.py
"""
import base64
import http.server
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "..", "plugins", "BasPBRBrowser")
sys.path.insert(0, os.path.join(HERE, "fakes"))
sys.path.insert(0, PLUGIN)

import c4d      # noqa: E402  the fakes
import maxon    # noqa: E402

LIB = tempfile.mkdtemp(prefix="baspbr_lib_")
MODELS = tempfile.mkdtemp(prefix="baspbr_models_")
with open(os.path.join(c4d.storage.PREFS, "baspbr_browser.json"), "w") as f:
    json.dump({"library": LIB, "model_library": MODELS, "source": "ambientcg", "query": "", "settings_version": 2,
               "assign": True}, f)

failures = []


def check(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        failures.append(what)


from baspbr import gltf, c4dscene, library as libmod, net, sketchfab, sources, ui, updater  # noqa: E402

# ---- the glTF reader on a hand-made model ------------------------------------------------------
print("glTF reader:")


def gltf_doc():
    """A quad as two triangles in two primitives (two materials), every vertex its own
    (as exporters do at UV seams), plus a second mesh whose two faces meet at a hard edge."""
    pos = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),        # quad, z = 0
           (0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 0), (1, 0, 0), (0, 0, -1)]      # hard edge 0-1
    nrm = [(0, 0, 1)] * 6 + [(0, 0, 1)] * 3 + [(0, 1, 0)] * 3
    uv = [(0, 1), (1, 1), (0, 0), (0.5, 1), (1, 0), (0.5, 0)] + [(0, 0)] * 6
    blob = b"".join(struct.pack("<3f", *p) for p in pos) + b"".join(struct.pack("<3f", *n) for n in nrm) + \
        b"".join(struct.pack("<2f", *t) for t in uv)
    acc = lambda off, n, t, c=5126: {"bufferView": 0, "byteOffset": off, "count": n, "componentType": c, "type": t}  # noqa: E731
    return {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(blob), "uri": "data:application/octet-stream;base64," + base64.b64encode(blob).decode()}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(blob)}],
        "accessors": [acc(0, 3, "VEC3"), acc(36, 3, "VEC3"), acc(144, 3, "VEC3"), acc(180, 3, "VEC3"),
                      acc(288, 3, "VEC2"), acc(312, 3, "VEC2"),
                      acc(72, 6, "VEC3"), acc(216, 6, "VEC3")],
        "materials": [{"name": "Red", "pbrMetallicRoughness": {"baseColorFactor": [1, 0, 0, 1], "metallicFactor": 0,
                                                              "roughnessFactor": 0.4}},
                      {"name": "Gold", "pbrMetallicRoughness": {"baseColorFactor": [1, 0.8, 0.3, 1],
                                                               "metallicFactor": 1, "roughnessFactor": 0.2}}],
        "meshes": [{"name": "Quad", "primitives": [
            {"attributes": {"POSITION": 0, "NORMAL": 2, "TEXCOORD_0": 4}, "material": 0},
            {"attributes": {"POSITION": 1, "NORMAL": 3, "TEXCOORD_0": 5}, "material": 1}]},
            {"name": "Edge", "primitives": [{"attributes": {"POSITION": 6, "NORMAL": 7}}]}],
        "nodes": [{"name": "Root", "children": [1, 2], "translation": [1, 2, 3], "scale": [2, 2, 2]},
                  {"name": "QuadNode", "mesh": 0}, {"name": "EdgeNode", "mesh": 1},
                  {"name": "AgainNode", "mesh": 0}],
        "scenes": [{"nodes": [0, 3]}], "scene": 0}


tmp = tempfile.mkdtemp(prefix="baspbr_gltf_")
with open(os.path.join(tmp, "quad.gltf"), "w") as f:
    json.dump(gltf_doc(), f)
sc = gltf.load(os.path.join(tmp, "quad.gltf"))
quad, edge = sc.meshes
check(len(quad.points) == 4 and len(quad.polys) == 2, "UV seam welded: 6 vertices -> %d points, %d triangles"
      % (len(quad.points), len(quad.polys)))
check(len(edge.points) == 6, "hard edge kept split: %d points" % len(edge.points))
check(quad.points[1] == (100.0, 0.0, 0.0) and quad.points[2] == (0.0, 100.0, 0.0), "metres -> cm: %s" % quad.points[:3])
check(edge.points[-1] == (0.0, 0.0, 100.0), "z mirrored: (0, 0, -1) m -> %s" % (edge.points[-1],))
check(quad.polys[0] == (0, 2, 1), "triangles reversed (they keep facing out): %s" % (quad.polys[0],))
check(quad.uvs[0] == ((0, 1), (0, 0), (1, 1)), "UVs follow their corners, unflipped: %s" % (quad.uvs[0],))
check(quad.materials == [0, 1], "a material per primitive")
root_m = sc.nodes[0].matrix
check(root_m[0] == (100.0, 200.0, -300.0) and root_m[1] == (2, 0, 0) and root_m[3] == (0, 0, 2),
      "node transform: translation in cm, z mirrored, scale kept: %s" % (root_m,))
check(sc.materials[0].base_color == [1.0, 0.0, 0.0] and sc.materials[0].roughness == 0.4 and
      sc.materials[1].metalness == 1.0, "material constants (no textures)")
try:
    gltf.load(os.path.expandvars(r"%LOCALAPPDATA%\BasPBRBrowser\testdata\gltf\draco\Duck.gltf")
              if os.path.isdir(os.path.expandvars(r"%LOCALAPPDATA%\BasPBRBrowser\testdata\gltf\draco")) else "missing")
    check(False, "Draco geometry refused")
except gltf.GltfError as exc:
    check("Draco" in str(exc), "Draco geometry refused: '%s'" % exc)
except OSError:
    pass

before = len(maxon.GRAPHS)
root, stats = c4dscene.build(c4d.documents.DOC, sc, "Test quad", {})
objs = list(root.walk())
names = [o.name for o in objs]
check(names == ["Test quad", "Root", "QuadNode", "EdgeNode", "AgainNode"], "tree: %s" % names)
qn = objs[2]
sels = [t for t in qn.tags if isinstance(t, c4d.SelectionTag)]
texs = [t for t in qn.tags if t.kind == c4d.Ttexture]
check(len(sels) == 2 and [sorted(t.GetBaseSelect().sel) for t in sels] == [[0], [1]] and
      [t[c4d.TEXTURETAG_RESTRICTION] for t in texs] == [s.GetName() for s in sels],
      "two materials on one mesh: a polygon selection each, texture tags restricted to them")
check(any(t.kind == c4d.Tphong and t[c4d.PHONGTAG_PHONG_ANGLELIMIT] is False for t in qn.tags), "Phong without angle limit")
check(objs[4].kind == c4d.Oinstance and objs[4][c4d.INSTANCEOBJECT_LINK] is qn, "a mesh used twice: an Instance")
check(stats["materials"] == 2 and len(maxon.GRAPHS) == before + 2, "one Redshift material per glTF material")
vals = {(n.split("<")[0], p) for n, p, v in maxon.GRAPHS[-1].values} if hasattr(maxon.GRAPHS[-1], "values") else set()
check(("standardmaterial", "base_color") in {(a, b.split(".")[-1]) for a, b in vals} or not vals,
      "constant colour set on the material")

# ---- a fake Sketchfab for downloads ----------------------------------------------------------------
SAMPLES = os.path.expandvars(r"%LOCALAPPDATA%\BasPBRBrowser\testdata\gltf")
KHRONOS = "https://raw.githubusercontent.com/KhronosGroup/glTF-Sample-Assets/main/Models/"
for fn, url in (("DamagedHelmet.glb", "DamagedHelmet/glTF-Binary/DamagedHelmet.glb"),
                ("SpecGlossVsMetalRough.glb", "SpecGlossVsMetalRough/glTF-Binary/SpecGlossVsMetalRough.glb")):
    if not os.path.isfile(os.path.join(SAMPLES, fn)):
        os.makedirs(SAMPLES, exist_ok=True)
        net.download(KHRONOS + url, os.path.join(SAMPLES, fn))
UID_HELMET, UID_BOTTLE, UID_NC = "a" * 31 + "1", "b" * 31 + "2", "c" * 31 + "3"
FILES = {UID_HELMET: "DamagedHelmet.glb", UID_BOTTLE: "SpecGlossVsMetalRough.glb", UID_NC: "SpecGlossVsMetalRough.glb"}
NAMES = {UID_HELMET: ("Damaged Helmet", "CC Attribution"), UID_BOTTLE: ("Water Bottle", "CC0 Public Domain"),
         UID_NC: ("Bottle (no commercial use)", "CC Attribution-NonCommercial")}
helmet_scene = gltf.load(os.path.join(SAMPLES, "DamagedHelmet.glb"))
THUMB = next(d for n, e, d, p in helmet_scene.images if e == ".jpg")
SEEN = {"download_auth": [], "file_auth": []}


class FakeSketchfab(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = self.path.split("?")[0].strip("/").split("/")
        auth = self.headers.get("Authorization")
        if parts[:2] == ["v3", "models"] and len(parts) == 4 and parts[3] == "download":
            SEEN["download_auth"].append(auth)
            if auth != "Token test-token":
                return self.send(401, b'{"detail": "Invalid token."}')
            size = os.path.getsize(os.path.join(SAMPLES, FILES[parts[2]]))
            return self.send(200, json.dumps({"glb": {"url": BASE + "/files/" + parts[2] + ".glb", "size": size}}).encode())
        if parts[:2] == ["v3", "models"] and len(parts) == 3 and parts[2] in FILES:
            name, lic = NAMES[parts[2]]
            return self.send(200, json.dumps({
                "uid": parts[2], "name": name, "license": {"label": lic}, "faceCount": 15452,
                "user": {"displayName": "Khronos sample", "profileUrl": "https://github.com/KhronosGroup"},
                "viewerUrl": "https://sketchfab.com/3d-models/x-" + parts[2],
                "thumbnails": {"images": [{"width": 256, "url": BASE + "/files/thumb.jpg"}]},
                "archives": {"glb": {"size": 3773916, "textureCount": 5, "textureMaxResolution": 2048}}}).encode())
        if parts[:1] == ["files"]:
            SEEN["file_auth"].append(auth)
            if parts[1] == "thumb.jpg":
                return self.send(200, THUMB, "image/jpeg")
            uid = parts[1].split(".")[0]
            with open(os.path.join(SAMPLES, FILES[uid]), "rb") as f:
                return self.send(200, f.read(), "model/gltf-binary")
        self.send(404, b"{}")


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeSketchfab)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % server.server_address[1]
LIVE_API = sketchfab.API

# ---- the dialog ------------------------------------------------------------------------------------
updater.CHECKED_THIS_SESSION = True                    # no update popup in this test
dlg = ui.BrowserDialog()
dlg.CreateLayout()
dlg.InitValues()


def pump(until, timeout=60):
    t = time.time()
    while time.time() - t < timeout:
        dlg.Timer(None)
        if until():
            return True
        time.sleep(0.05)
    return False


def switch(tab):
    dlg.tabs.Select(sources.TABS.index(tab), True)
    dlg.Command(ui.ID_TABS, None)


def searched():
    return not dlg.searching and bool(dlg.grid.items)


def click(i, double=False):
    x, y = dlg.grid.tile_rect(i)
    dlg.grid.InputEvent({c4d.BFM_INPUT_DEVICE: c4d.BFM_INPUT_MOUSE, c4d.BFM_INPUT_CHANNEL: c4d.BFM_INPUT_MOUSELEFT,
                         c4d.BFM_INPUT_X: x + 30, c4d.BFM_INPUT_Y: y + 30 - dlg.grid.scroll,
                         c4d.BFM_INPUT_QUALIFIER: 0, c4d.BFM_INPUT_DOUBLECLICK: double})


def toggle(key):
    i = [t[0] for t in dlg.cats.toggles].index(key)
    dlg.cats.InputEvent({c4d.BFM_INPUT_DEVICE: c4d.BFM_INPUT_MOUSE, c4d.BFM_INPUT_CHANNEL: c4d.BFM_INPUT_MOUSELEFT,
                         c4d.BFM_INPUT_X: 12, c4d.BFM_INPUT_Y: dlg.cats.toggle_y(i) + 5, c4d.BFM_INPUT_QUALIFIER: 0})


def click_category(label):
    i = next(n for n, r in enumerate(dlg.cats.rows) if r[1] == label)
    dlg.cats.InputEvent({c4d.BFM_INPUT_DEVICE: c4d.BFM_INPUT_MOUSE, c4d.BFM_INPUT_CHANNEL: c4d.BFM_INPUT_MOUSELEFT,
                         c4d.BFM_INPUT_X: 20, c4d.BFM_INPUT_Y: dlg.cats.row_y(i) + 5, c4d.BFM_INPUT_QUALIFIER: 0})
    return dlg.cats.rows[i]


def set_faces(label):
    dlg.SetInt32(ui.ID_RES, dlg.res_options.index(label))
    dlg.Command(ui.ID_RES, None)


print("\nthe Sketchfab tab:")
check("Sketchfab" in [t[1] for t in dlg.tabs.tabs], "a Sketchfab tab")
switch("sketchfab")
check(dlg.GetString(ui.ID_RES_LABEL).strip() == "Max faces" and dlg.res_options[0] == "Any" and
      dlg.resolution == sketchfab.DEFAULT_FACES, "Resolution becomes Max faces (default %s)" % dlg.resolution)
check([t[1] for t in dlg.cats.toggles] == ["Commercial use only", "Staff picks only"], "filter boxes in the sidebar")
check(dlg.GetString(ui.ID_CREATE) == "Import model", "the button says Import model")
check(dlg.GetString(ui.ID_LIB_LABEL) == "Models" and dlg.GetString(ui.ID_LIBRARY) == MODELS,
      "the folder row shows the models folder: %s" % dlg.GetString(ui.ID_LIBRARY))
check(pump(searched, 60), "live search: %s" % dlg.GetString(ui.ID_COUNT))
check(dlg.GetString(ui.ID_COUNT).endswith("+ models") and len(dlg.results) >= 24, "count says there's more")
check(pump(lambda: len(dlg.cats.rows) > 10, 30), "Sketchfab's categories: %d" % (len(dlg.cats.rows) - 1))
check(all(a.extra["faces"] <= 50000 for a in dlg.results), "all within 50K faces")
texts = [d[1] for d in dlg.grid.drawn if d[0] == "text"]
check(any(t in ("CC0", "CREDIT", "NON-COMM", "CHECK") for t in texts) and any(t.endswith("faces") for t in texts),
      "tiles show a licence badge and the face count")
n0 = len(dlg.results)
dlg.grid.scroll_to(10 ** 9)
check(pump(lambda: len(dlg.results) > n0 and not dlg.loading_more, 30), "scrolled to the end: the next page (%d -> %d)"
      % (n0, len(dlg.results)))
set_faces("10K")
check(pump(lambda: searched() and all(a.extra["faces"] <= 10000 for a in dlg.results), 30), "Max faces 10K")
toggle("commercial")
check(pump(lambda: searched() and all(a.extra["kind"] != "nc" for a in dlg.results), 30),
      "Commercial use only: no NON-COMM (%d)" % len(dlg.results))
toggle("commercial")
row = click_category("Furniture & Home")
check(pump(lambda: searched() and all("furniture-home" in a.categories for a in dlg.results), 30),
      "category %s" % row[1])
dlg.category_changed(sources.ALL)
pump(searched, 30)
a0 = dlg.results[0]
click(0)
st = dlg.GetString(ui.ID_STATUS)
check(a0.extra["licence"] in st and ("faces" in st or not a0.extra["faces"]), "selected: '%s'" % st)
link = a0.page_url
switch("ambientcg")
dlg.SetString(ui.ID_QUERY, link)
dlg.Command(ui.ID_SEARCH, None)
check(dlg.tab == "sketchfab" and pump(lambda: searched() and len(dlg.results) == 1 and dlg.results[0].id == a0.id, 30),
      "a Sketchfab link searched on another tab: that model, on the Sketchfab tab")
check(dlg.grid.selected == {0}, "and it's selected")
switch("polyhaven")
check(dlg.tab == "polyhaven" and dlg.GetString(ui.ID_QUERY) == "", "switching away clears the link (no bounce back)")
switch("sketchfab")

print("\nthe models folder:")
MODELS2 = tempfile.mkdtemp(prefix="baspbr_models2_")
dlg.set_library(MODELS2, ask_unc=False)
check(dlg.model_library.root == MODELS2 and dlg.library.root == LIB, "set on the Sketchfab tab: only the models folder changes")
switch("ambientcg")
check(dlg.GetString(ui.ID_LIB_LABEL) == "Library" and dlg.GetString(ui.ID_LIBRARY) == LIB, "other tabs show the library")
switch("sketchfab")
check(dlg.GetString(ui.ID_LIBRARY) == MODELS2, "back on Sketchfab: the models folder")

print("\nPaste link (fake Sketchfab):")
sketchfab.API = BASE + "/v3/"
c4d.CLIPBOARD[0] = "nothing useful"
dlg.Command(ui.ID_PASTE, None)
check("No Sketchfab link" in dlg.GetString(ui.ID_STATUS), "no link on the clipboard: said so")
c4d.CLIPBOARD[0] = "https://sketchfab.com/3d-models/damaged-helmet-%s?utm=x" % UID_HELMET
c4d.gui.INPUT_ANSWER[0] = "test-token"
objects_before = len(c4d.documents.DOC.objects)
dlg.Command(ui.ID_PASTE, None)
check(pump(lambda: len(c4d.documents.DOC.objects) > objects_before or "error" in dlg.state.values(), 60),
      "pasted link -> looked up -> downloaded -> imported")
check(dlg.cfg.get("sketchfab_token") == "test-token", "the token was asked for once and kept")
check(SEEN["download_auth"] == ["Token test-token"] and not any(SEEN["file_auth"]),
      "token sent to the API only, never with the file download")
root = c4d.documents.DOC.objects[-1] if c4d.documents.DOC.objects else None
objs = list(root.walk()) if root else []
polys = [o for o in objs if isinstance(o, c4d.PolygonObject)]
check(root is not None and root.name == "Damaged Helmet" and len(polys) == 1 and
      polys[0].GetPolygonCount() == 15452, "a Null 'Damaged Helmet' with the mesh (%s polygons)"
      % (polys[0].GetPolygonCount() if polys else 0))
tags = polys[0].tags if polys else []
check(any(isinstance(t, c4d.UVWTag) for t in tags) and any(t.kind == c4d.Ttexture for t in tags),
      "UVs and a texture tag")
w = {(a.split("<")[0], b, c.split("<")[0], d) for a, b, c, d in maxon.GRAPHS[-1].wires}
check(("rscolorsplitter", "outg", "standardmaterial", "refl_roughness") in w and
      ("rscolorsplitter", "outb", "standardmaterial", "metalness") in w and
      ("texturesampler", "outcolor", "standardmaterial", "base_color") in w and
      ("bumpmap", "out", "standardmaterial", "bump_input") in w and
      ("texturesampler", "outcolor", "standardmaterial", "emission_color") in w,
      "Redshift material: colour, roughness G / metalness B, normal, emission")
check(c4d.documents.DOC.active is root, "the model is selected")
folder = os.path.join(MODELS2, "Sketchfab", "DamagedHelmet_" + UID_HELMET[:6].upper())
have = sorted(os.listdir(folder)) if os.path.isdir(folder) else []
check(have == ["ATTRIBUTION.txt", "asset.json", "model.glb", "preview.jpg", "textures"], "in the models folder: %s" % have)
check(os.path.isdir(folder) and len(os.listdir(os.path.join(folder, "textures"))) == 5, "its 5 textures as files")
check(not os.path.exists(os.path.join(LIB, "Sketchfab")), "nothing in the materials library")
if os.path.isdir(folder):
    with open(os.path.join(folder, "ATTRIBUTION.txt"), encoding="utf-8") as f:
        att = f.read()
    check("Khronos sample" in att and "CC Attribution" in att and "Credit line" in att, "ATTRIBUTION.txt credits the author")
check("credit Khronos sample" in dlg.GetString(ui.ID_STATUS), "status: '%s'" % dlg.GetString(ui.ID_STATUS))
paths = [v for n, p, v in maxon.GRAPHS[-1].values if p.endswith("path")] if hasattr(maxon.GRAPHS[-1], "values") else []
check(not paths or all(MODELS2.replace("\\", "/") in str(v).replace("\\", "/") for v in paths),
      "the material's textures point into the models folder")

print("\nagain, and from Installed:")
real_download, real_get = net.download, net.get_bytes
calls = []
net.download = lambda *a, **k: calls.append(a[0]) or real_download(*a, **k)
n = len(c4d.documents.DOC.objects)
dlg.create([dlg.assets["sketchfab:" + UID_HELMET]])
check(pump(lambda: len(c4d.documents.DOC.objects) > n, 30) and not calls and
      "from the library" in dlg.GetString(ui.ID_STATUS), "the same model again: from the folder, not downloaded")
net.download = real_download
switch("installed")
check(pump(lambda: dlg.scanned and any(a.extra.get("origin") == "Sketchfab" for a in dlg.grid.items), 20),
      "Installed lists the model (origin Sketchfab)")
i = next((k for k, a in enumerate(dlg.grid.items) if a.extra.get("origin") == "Sketchfab"), 0)
net.download = net.get_bytes = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
n = len(c4d.documents.DOC.objects)
click(i, double=True)
check(pump(lambda: len(c4d.documents.DOC.objects) > n, 30), "double-click there builds it, no network")
net.download, net.get_bytes = real_download, real_get

print("\nlicences and tokens:")
switch("sketchfab")
c4d.gui.QUESTIONS.clear()
c4d.gui.QUESTION_ANSWER[0] = False
c4d.CLIPBOARD[0] = "https://sketchfab.com/models/" + UID_NC
n = len(c4d.documents.DOC.objects)
dlg.Command(ui.ID_PASTE, None)
pump(lambda: bool(c4d.gui.QUESTIONS), 20)
pump(lambda: False, 1.0)
check(any("NOT for commercial" in q for q in c4d.gui.QUESTIONS) and len(c4d.documents.DOC.objects) == n,
      "non-commercial: asked first; 'no' imports nothing")
c4d.gui.QUESTION_ANSWER[0] = True
dlg.create([dlg.assets["sketchfab:" + UID_NC]])
check(pump(lambda: len(c4d.documents.DOC.objects) > n, 60), "'yes': imported")
check("NOT for commercial work" in dlg.GetString(ui.ID_STATUS), "and the status says so")
bottle = c4d.documents.DOC.objects[-1]
check(len([o for o in bottle.walk() if isinstance(o, c4d.PolygonObject)]) == 4, "a model with 4 meshes: 4 objects")
dlg.cfg["sketchfab_token"] = "wrong"
c4d.CLIPBOARD[0] = "https://sketchfab.com/3d-models/bottle-" + UID_BOTTLE
dlg.Command(ui.ID_PASTE, None)
check(pump(lambda: "didn't accept the API token" in dlg.GetString(ui.ID_STATUS), 30),
      "a refused token: '%s'" % dlg.GetString(ui.ID_STATUS))

sketchfab.API = LIVE_API
server.shutdown()
for d in (LIB, MODELS, MODELS2, tmp):
    shutil.rmtree(d, ignore_errors=True)
print("\n%s" % ("ALL PASSED" if not failures else "%d FAILED:\n  %s" % (len(failures), "\n  ".join(failures))))
sys.exit(1 if failures else 0)
