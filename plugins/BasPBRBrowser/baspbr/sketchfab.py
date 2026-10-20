"""Sketchfab models - plain Python (search, licences, links, downloads).

Searching is public: GET /v3/search?type=models&downloadable=true, 24 a page,
paged with the "next" link it returns. Downloading needs the user's own API
token (sketchfab.com/settings/password, "API Token"): /v3/models/{uid}/download
with "Authorization: Token ..." answers with short-lived, pre-signed file links;
those are fetched WITHOUT the token (the storage rejects a second credential).

Always the .glb: Sketchfab's own conversion, one self-contained file with its
textures inside - never the "source" archive, where the original upload sits
in whatever formats and nested zips its author used. The .gltf zip is the
fallback, unpacked defensively (nested zips up to three deep, nothing written
outside the folder).

Licences: every model carries one. Tiles get a badge - CC0 (green), CREDIT
(Attribution: name the author), NON-COMM (not for commercial work, confirmed
before importing), CHECK (anything else). Each import writes ATTRIBUTION.txt.
"""
import os
import re
import shutil
import zipfile

from . import net
from .sources import ALL, SOURCES, Asset, Source, _timestamp

API = "https://api.sketchfab.com/v3/"
TOKEN_PAGE = "https://sketchfab.com/settings/password"
PAGE = 24                       # Sketchfab's largest page
FACES = (("Any", None), ("10K", 10000), ("50K", 50000), ("100K", 100000), ("250K", 250000),
         ("500K", 500000), ("1M", 1000000))
DEFAULT_FACES = "50K"
SORTS = {"Most liked": "-likeCount", "Relevance": None, "Newest": "-publishedAt", "Most viewed": "-viewCount"}
UID = re.compile(r"(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])")


class SketchfabError(Exception):
    pass


def parse_uid(text):
    """The model's uid from a Sketchfab link (sketchfab.com/3d-models/<slug>-<uid>,
    /models/<uid>, embed links...) or a bare uid; None if there isn't one."""
    text = (text or "").strip()
    if not text:
        return None
    if "sketchfab" not in text.lower() and not re.fullmatch(r"[0-9a-fA-F]{32}", text):
        return None
    path = re.split(r"[?#]", text, 1)[0].lower()
    found = UID.findall(path)
    return found[-1] if found else None


def licence_kind(label):
    """"cc0" | "by" (credit the author) | "nc" (not for commercial work) | "other"."""
    l = (label or "").lower()
    if "noncommercial" in l or "non-commercial" in l or "editorial" in l:
        return "nc"
    if "cc0" in l or "public domain" in l:
        return "cc0"
    if "attribution" in l:
        return "by"
    return "other"


BADGES = {"cc0": "CC0", "by": "CREDIT", "nc": "NON-COMM", "other": "CHECK"}


def faces_text(n):
    if not n:
        return ""
    if n >= 1000000:
        return "%.1fM" % (n / 1e6)
    return "%dK" % round(n / 1000.0) if n >= 1000 else str(n)


def _thumb(model, width=256):
    images = ((model.get("thumbnails") or {}).get("images")) or []
    if not images:
        return ""
    best = min(images, key=lambda i: abs((i.get("width") or 0) - width))
    return best.get("url") or ""


def asset_from(model):
    """An Asset for a search result or a /models/{uid} answer."""
    user = model.get("user") or {}
    lic = model.get("license") or {}
    label = lic.get("label") or lic.get("fullName") or ""
    archives = model.get("archives") or {}
    glb = archives.get("glb") or archives.get("gltf") or {}
    cats = [c.get("name") or c.get("slug") for c in model.get("categories") or [] if c.get("name") or c.get("slug")]
    return Asset("sketchfab", model["uid"], (model.get("name") or model["uid"]).strip(), _thumb(model),
                 tags=[t.get("name", "") for t in model.get("tags") or []], categories=cats,
                 page_url=model.get("viewerUrl") or "https://sketchfab.com/3d-models/" + model["uid"],
                 extra={"faces": model.get("faceCount") or glb.get("faceCount") or 0,
                        "licence": label, "kind": licence_kind(label),
                        "author": user.get("displayName") or user.get("username") or "",
                        "author_url": user.get("profileUrl") or "",
                        "size": glb.get("size") or 0, "textures": glb.get("textureCount"),
                        "texture_res": glb.get("textureMaxResolution"),
                        "pop": float(model.get("likeCount") or 0),
                        "date": _timestamp(model.get("publishedAt"))})


class Page(list):
    """One page of results, and the link to the next (None at the end)."""
    next = None


class Sketchfab(Source):
    id = "sketchfab"
    label = "Sketchfab"
    licence = "Licence per model: CC0, CREDIT (name the author), NON-COMM (not for commercial work)"
    sorts = tuple(SORTS)
    resolutions = ()
    models = True               # a model tab: Import instead of Create material

    def __init__(self):
        Source.__init__(self)
        self._categories = None

    def loaded(self, *a):
        return True                                   # nothing to load up front: it searches online

    def refresh(self):
        self._categories = None

    def categories(self, **kw):
        if self._categories is None:
            data = net.get_json(API + "categories")
            self._categories = [(c["slug"], c.get("name") or c["slug"], None)
                                for c in data.get("results") or [] if c.get("slug")]
        return self._categories

    def search_url(self, query, sort, category=ALL, max_faces=None, staffpicked=False):
        params = [("type", "models"), ("downloadable", "true"), ("count", str(PAGE))]
        if query:
            params.append(("q", query))
        if SORTS.get(sort):
            params.append(("sort_by", SORTS[sort]))
        if category:
            params.append(("categories", category))
        if max_faces:
            params.append(("max_face_count", str(int(max_faces))))
        if staffpicked:
            params.append(("staffpicked", "true"))
        return API + "search?" + net.urllib.parse.urlencode(params)

    def page(self, url, commercial=False, want=PAGE, max_pages=4):
        """Results from url on, following "next" until want of them pass the filter."""
        out = Page()
        while url and max_pages > 0 and len(out) < want:
            data = net.get_json(url)
            for m in data.get("results") or []:
                if not m.get("uid"):
                    continue
                a = asset_from(m)
                if commercial and a.extra["kind"] == "nc":
                    continue
                out.append(a)
            url = data.get("next")
            max_pages -= 1
        out.next = url
        return out

    def search(self, query, sort, category=ALL, max_faces=None, commercial=False, staffpicked=False, **kw):
        uid = parse_uid(query)
        if uid:                                       # a pasted link: that model
            out = Page([self.model(uid)])
            return out
        return self.page(self.search_url(query, sort, category, max_faces, staffpicked), commercial)

    def model(self, uid):
        try:
            return asset_from(net.get_json(API + "models/" + uid))
        except net.urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise SketchfabError("there's no Sketchfab model %s (deleted, or private)" % uid)
            raise

    # ---- downloading ---------------------------------------------------------------------
    def download_links(self, uid, token):
        if not token:
            raise SketchfabError("downloading from Sketchfab needs your API token (Help > Sketchfab API token)")
        try:
            return net.get_json(API + "models/%s/download" % uid, headers={"Authorization": "Token " + token})
        except net.urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise SketchfabError("Sketchfab didn't accept the API token - check it under Help > Sketchfab API token")
            if exc.code == 403:
                raise SketchfabError("this model can't be downloaded with your account")
            if exc.code == 404:
                raise SketchfabError("this model isn't downloadable (any more)")
            if exc.code == 429:
                raise SketchfabError("Sketchfab says: too many downloads for now - try again in a while")
            raise

    def download_model(self, asset, token, work, progress, cancel):
        """Download the model into work; returns the path of its .glb / .gltf."""
        links = self.download_links(asset.id, token)
        glb, gltf = links.get("glb") or {}, links.get("gltf") or {}
        if glb.get("url"):
            path = os.path.join(work, "model.glb")
            total = float(glb.get("size") or asset.extra.get("size") or 0)
            net.download(glb["url"], path, lambda d, t: progress(d / float(t or total or 5e7)), cancel)
            return path
        if gltf.get("url"):
            zpath = os.path.join(work, "gltf.zip")
            total = float(gltf.get("size") or 0)
            net.download(gltf["url"], zpath, lambda d, t: progress(d / float(t or total or 5e7)), cancel)
            return unpack_gltf(zpath, os.path.join(work, "gltf"))
        raise SketchfabError("Sketchfab offers no glTF / glb download for this model")


def unpack_gltf(zpath, dest, depth=3):
    """Unzip a glTF archive (and zips inside it, a few levels deep) into dest;
    the path of the .gltf in it."""
    os.makedirs(dest, exist_ok=True)
    root = os.path.realpath(dest)
    pending = [zpath]
    for _ in range(depth):
        inner = []
        for z in pending:
            with zipfile.ZipFile(z) as zf:
                for info in zf.infolist():
                    target = os.path.realpath(os.path.join(dest, info.filename))
                    if not target.startswith(root + os.sep) and target != root:
                        continue                      # a path that climbs out: skipped
                    if info.is_dir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as f:
                        shutil.copyfileobj(src, f)
                    if target.lower().endswith(".zip"):
                        inner.append(target)
        if not inner:
            break
        pending = inner
    found = []
    for folder, dirs, files in os.walk(dest):
        found += [os.path.join(folder, f) for f in files if f.lower().endswith((".gltf", ".glb"))]
    if not found:
        raise SketchfabError("no .gltf in Sketchfab's download")
    return sorted(found, key=lambda p: (p.count(os.sep), p))[0]


def attribution(asset, when):
    e = asset.extra
    kind = e.get("kind")
    verdict = {"cc0": "Commercial use OK, no credit needed.",
               "by": "Commercial use OK - credit the author (see the licence).",
               "nc": "NOT for commercial use.",
               "other": "Check the licence before using this commercially."}.get(kind, "")
    lines = ["%s" % asset.name,
             "by %s%s" % (e.get("author") or "unknown", (" (%s)" % e["author_url"]) if e.get("author_url") else ""),
             "Licence: %s" % (e.get("licence") or "unknown"),
             verdict,
             "Source: %s" % asset.page_url,
             "Downloaded: %s" % when,
             "",
             "Credit line: \"%s\" by %s, licensed under %s, via Sketchfab (%s)" % (
                 asset.name, e.get("author") or "unknown", e.get("licence") or "its licence", asset.page_url)]
    return "\n".join(lines) + "\n"


SOURCES.setdefault("sketchfab", Sketchfab())   # whichever of the two modules is imported first
