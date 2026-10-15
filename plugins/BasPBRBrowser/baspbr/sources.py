"""Material sources — ambientCG, Poly Haven, FreePBR, and the library itself
("Installed"). Plain Python.

Ported from the WhateverWall Unity browser (AmbientCgApi / PolyHavenApi /
FreePbrApi), same endpoints:

  ambientCG   /api/v2/full_json?type=Material&include=imageData,downloadData
              server-side search, category filter and paging; one ZIP per
              material and resolution. Categories: /api/v2/categories_json.
  Poly Haven  /assets?t=textures once (filtered locally), /files/<id> per asset;
              individual map files per resolution. Categories:
              /categories/textures. Thumbnails are asked for as PNG — the CDN
              defaults to WebP, which Cinema 4D can't read.
  FreePBR     WooCommerce Store API for the catalogue and its categories; the
              download is a form POST on the product page that returns a ZIP.
              Fixed resolution (mostly 2K). NOT CC0: free downloads are for
              non-commercial use.
  Installed   whatever is in the library folder (which may be a network share
              other PCs fill too) — nothing to download.

Each online source returns Asset objects for the grid and, for a download, a
dict of {role: file} that library.Library.store() files away.
"""
import html
import os
import re
import shutil
import time
import zipfile

from . import net
from .library import classify, image_kind, pick_maps

RESOLUTIONS = ("1K", "2K", "4K", "8K")
ALL = ""                       # the "All" category


class Asset(object):
    __slots__ = ("source", "id", "name", "thumb_url", "tags", "categories", "page_url", "max_res", "extra")

    def __init__(self, source, id, name, thumb_url="", tags=(), page_url="", max_res="", extra=None,
                 categories=()):
        self.source = source            # "ambientcg" | "polyhaven" | "freepbr" | "installed"
        self.id = id
        self.name = name
        self.thumb_url = thumb_url
        self.tags = list(tags)
        self.categories = list(categories)
        self.page_url = page_url
        self.max_res = max_res
        self.extra = extra or {}

    @property
    def key(self):
        return "%s:%s" % (self.source, self.id)

    @property
    def source_label(self):
        return SOURCES[self.source].label

    def matches(self, q):
        if not q:
            return True
        hay = [self.id, self.name] + self.tags + self.categories
        return all(any(word in h.lower() for h in hay if h) for word in q.lower().split())


def _res_value(r):
    m = re.match(r"(\d+)", r or "")
    return int(m.group(1)) if m else 0


class Source(object):
    id = ""
    label = ""
    licence = ""
    server_search = False       # True: search/paging happen on the server
    sorts = ()
    fixed_resolution = ""      # set when the source offers one resolution only

    def categories(self):
        """[(key, label, count)] for the sidebar (cached by the source)."""
        return []

    def search(self, query, sort, page, page_size, category=ALL):
        """(assets, total) for one page of results."""
        raise NotImplementedError

    def download(self, asset, res, work, progress, cancel):
        """Download the maps into the folder `work`. Returns {role: path}."""
        raise NotImplementedError

    def refresh(self):
        """Forget cached catalogues and categories."""
        for attr in ("_all", "_cats"):
            if hasattr(self, attr):
                setattr(self, attr, None)


def _page(items, page, page_size):
    return items[page * page_size:(page + 1) * page_size], len(items)


# =====================================================================================
class AmbientCG(Source):
    id = "ambientcg"
    label = "ambientCG"
    licence = "CC0 (public domain)"
    server_search = True
    sorts = ("Popular", "Latest", "Alphabet", "Downloads")
    API = "https://ambientcg.com/api/v2/"

    def __init__(self):
        self._cats = None

    def categories(self):
        if self._cats is None:
            data = net.get_json(self.API + "categories_json")
            cats = [(c["categoryName"], c.get("categoryDisplayName") or c["categoryName"], c.get("numberOfAssets", 0))
                    for c in data if c.get("defaultDataTypeId") == "Material" and c.get("numberOfAssets")]
            self._cats = sorted(cats, key=lambda c: c[1].lower())
        return self._cats

    def search(self, query, sort, page, page_size, category=ALL):
        params = {"type": "Material", "limit": page_size, "offset": page * page_size,
                  "include": "imageData,downloadData", "sort": sort or "Popular"}
        if query:
            params["q"] = query.strip()
        if category:
            params["category"] = category
        data = net.get_json(self.API + "full_json?" + net.urllib.parse.urlencode(params))
        assets = []
        for a in data.get("foundAssets", []):
            aid = a.get("assetId")
            if not aid:
                continue
            prev = a.get("previewImage") or {}
            thumb = prev.get("256-PNG") or prev.get("256-JPG-FFFFFF") or next(
                (v for v in prev.values() if isinstance(v, str)), "")
            downloads = []
            try:
                for d in a["downloadFolders"]["default"]["downloadFiletypeCategories"]["zip"]["downloads"]:
                    if d.get("attribute") and (d.get("downloadLink") or d.get("fullDownloadPath")):
                        downloads.append({"attribute": d["attribute"],
                                          "url": d.get("downloadLink") or d.get("fullDownloadPath"),
                                          "size": d.get("size", 0)})
            except (KeyError, TypeError):
                pass
            best = max((_res_value(d["attribute"]) for d in downloads), default=0)
            cat = a.get("displayCategory") or a.get("category")
            assets.append(Asset(self.id, aid, a.get("displayName") or aid, thumb,
                                tags=a.get("tags") or [], categories=[cat] if cat else [],
                                page_url="https://ambientcg.com/view?id=" + aid,
                                max_res="%dK" % best if best else "",
                                extra={"downloads": downloads}))
        return assets, int(data.get("numberOfResults") or len(assets))

    @staticmethod
    def pick(downloads, res):
        """The zip for res (JPG preferred), else the largest smaller one, else the smallest."""
        want = _res_value(res)
        jpg = [d for d in downloads if d["attribute"].upper().endswith("-JPG")] or downloads
        jpg = sorted(jpg, key=lambda d: _res_value(d["attribute"]), reverse=True)
        for d in jpg:
            if _res_value(d["attribute"]) <= want:
                return d
        return jpg[-1] if jpg else None

    def download(self, asset, res, work, progress, cancel):
        dl = self.pick(asset.extra.get("downloads", []), res)
        if not dl:
            raise RuntimeError("no downloads listed for " + asset.id)
        zpath = os.path.join(work, "download.zip")
        net.download(dl["url"], zpath, lambda d, t: progress(d / float(t or dl.get("size") or 1)), cancel)
        return _extract_maps(zpath, work)


# =====================================================================================
class PolyHaven(Source):
    id = "polyhaven"
    label = "Poly Haven"
    licence = "CC0 (public domain)"
    sorts = ("Name", "Newest", "Popular")
    API = "https://api.polyhaven.com"
    # Poly Haven file keys -> our roles. OpenGL normals (Redshift's convention).
    MAPS = (("Diffuse", "color"), ("nor_gl", "normal_gl"), ("Rough", "roughness"), ("metal", "metalness"),
            ("AO", "ao"), ("Displacement", "height"), ("Opacity", "opacity"), ("alpha", "opacity"))

    def __init__(self):
        self._all = None
        self._cats = None

    @staticmethod
    def png_thumb(url, aid):
        """Poly Haven's CDN serves WebP unless asked otherwise; Cinema 4D can't read WebP."""
        url = url or "https://cdn.polyhaven.com/asset_img/thumbs/%s.png?width=256&height=256" % aid
        if "format=" in url:
            return re.sub(r"format=[a-z]+", "format=png", url)
        return url + ("&" if "?" in url else "?") + "format=png"

    def _catalogue(self):
        if self._all is None:
            data = net.get_json(self.API + "/assets?t=textures")
            out = []
            for aid, a in data.items():
                if not isinstance(a, dict):
                    continue
                mr = a.get("max_resolution") or [0]
                out.append(Asset(self.id, aid, a.get("name") or aid, self.png_thumb(a.get("thumbnail_url"), aid),
                                 tags=a.get("tags") or [], categories=a.get("categories") or [],
                                 page_url="https://polyhaven.com/a/" + aid,
                                 max_res="%dK" % (int(mr[0]) // 1024) if mr and mr[0] else "",
                                 extra={"published": a.get("date_published", 0),
                                        "downloads": a.get("download_count", 0)}))
            self._all = out
        return self._all

    def categories(self):
        if self._cats is None:
            data = net.get_json(self.API + "/categories/textures")
            self._cats = sorted(((k, k.replace("-", " / ").title(), n) for k, n in data.items() if k != "all" and n),
                                key=lambda c: c[1].lower())
        return self._cats

    def search(self, query, sort, page, page_size, category=ALL):
        items = [a for a in self._catalogue()
                 if (not category or category in a.categories) and a.matches(query)]
        if sort == "Newest":
            items.sort(key=lambda a: -a.extra.get("published", 0))
        elif sort == "Popular":
            items.sort(key=lambda a: -a.extra.get("downloads", 0))
        else:
            items.sort(key=lambda a: a.name.lower())
        return _page(items, page, page_size)

    def files(self, asset_id, res):
        """[(role, url, ext, size)] for the maps of one asset at res (or the nearest smaller)."""
        data = net.get_json(self.API + "/files/" + asset_id)
        ladder = ["16k", "8k", "4k", "2k", "1k"]
        want = res.lower()
        start = ladder.index(want) if want in ladder else 1
        out, seen = [], set()
        for key, role in self.MAPS:
            by_res = data.get(key)
            if not isinstance(by_res, dict) or role in seen:
                continue
            for r in ladder[start:]:
                by_fmt = by_res.get(r)
                if not isinstance(by_fmt, dict):
                    continue
                fmt = next((f for f in ("jpg", "png") if isinstance(by_fmt.get(f), dict) and by_fmt[f].get("url")), None)
                if fmt:
                    out.append((role, by_fmt[fmt]["url"], fmt, by_fmt[fmt].get("size", 0)))
                    seen.add(role)
                    break
        if not any(o[0] == "color" for o in out):
            raise RuntimeError("Poly Haven lists no colour map for " + asset_id)
        return out

    def download(self, asset, res, work, progress, cancel):
        files = self.files(asset.id, res)
        total = float(sum(f[3] for f in files) or len(files))
        done = 0.0
        maps = {}
        for role, url, ext, size in files:
            path = os.path.join(work, "%s_%s.%s" % (asset.id, role, ext))
            try:
                net.download(url, path, lambda d, t, b=done: progress((b + d) / total), cancel)
            except net.Cancelled:
                raise
            except Exception:
                if role == "color":
                    raise
                continue            # a missing optional map shouldn't sink the material
            done += size or 1
            maps[role] = path
        return maps


# =====================================================================================
class FreePBR(Source):
    id = "freepbr"
    label = "FreePBR"
    licence = "Free for non-commercial use (commercial use needs the paid FreePBR licence)"
    sorts = ("Name",)
    fixed_resolution = "2K"
    ROOT = "https://freepbr.com"

    def __init__(self):
        self._all = None
        self._cats = None

    def _catalogue(self):
        if self._all is None:
            out = []
            for page in range(1, 11):          # ~620 products today; hard stop either way
                try:
                    arr = net.get_json("%s/wp-json/wc/store/v1/products?per_page=100&page=%d" % (self.ROOT, page))
                except net.urllib.error.HTTPError:
                    break                      # a page past the end answers 400
                if not isinstance(arr, list) or not arr:
                    break
                for p in arr:
                    price = (p.get("prices") or {}).get("price")
                    slug = p.get("slug") or ""
                    if (price not in (None, "0")) or not slug or "license" in slug or "download-all" in slug:
                        continue           # the paid bundle licences
                    imgs = p.get("images") or [{}]
                    out.append(Asset(self.id, str(p.get("id")), html.unescape(p.get("name") or slug),
                                     imgs[0].get("thumbnail") or imgs[0].get("src") or "",
                                     tags=[html.unescape(t.get("name", "")) for t in (p.get("tags") or [])],
                                     categories=[html.unescape(c.get("name", "")) for c in (p.get("categories") or [])],
                                     page_url=p.get("permalink") or "%s/product/%s/" % (self.ROOT, slug),
                                     max_res="2K", extra={"slug": slug}))
                if len(arr) < 100:
                    break
            self._all = out
        return self._all

    def categories(self):
        if self._cats is None:
            data = net.get_json(self.ROOT + "/wp-json/wc/store/v1/products/categories?per_page=100")
            cats = [(html.unescape(c["name"]), html.unescape(c["name"]), c.get("count", 0)) for c in data
                    if c.get("count", 0) > 1 and c.get("slug") not in ("all", "uncategorized")]
            self._cats = sorted(cats, key=lambda c: c[1].lower())
        return self._cats

    def search(self, query, sort, page, page_size, category=ALL):
        items = sorted((a for a in self._catalogue()
                        if (not category or category in a.categories) and a.matches(query)),
                       key=lambda a: a.name.lower())
        return _page(items, page, page_size)

    @staticmethod
    def pick_file(page_html):
        """somdn_productfile index of the '*-bl.zip' pack (separate PNG maps, roughness included)."""
        names = re.findall(r"([A-Za-z0-9_\-]+\.zip)", page_html)
        idx = re.findall(r'name="somdn_productfile"\s+value="(\d+)"', page_html)
        for n, i in zip(names, idx):
            if n.lower().endswith("-bl.zip"):
                return i
        return idx[-1] if idx else "3"

    def download(self, asset, res, work, progress, cancel):
        page = asset.page_url
        page_html = net.get_text(page)          # the key is fresh on every page load
        key = re.search(r'name="somdn_download_key"\s+value="([^"]+)"', page_html)
        pid = re.search(r'name="somdn_product"\s+value="(\d+)"', page_html)
        if not key or not pid:
            raise RuntimeError("no download form on " + page)
        form = {"somdn_download_key": key.group(1), "action": "somdn_download_multi_single",
                "somdn_product": pid.group(1), "somdn_productfile": self.pick_file(page_html)}
        zpath = os.path.join(work, "download.zip")
        ctype = net.download(page, zpath, lambda d, t: progress(d / float(t or 60e6)), cancel,
                             data=form, headers={"Referer": page})
        if not zipfile.is_zipfile(zpath):
            raise RuntimeError("FreePBR didn't return a zip (%s)" % (ctype or "unknown type"))
        return _extract_maps(zpath, work)


# =====================================================================================
class Installed(Source):
    """The library folder itself: every material in it, ready to build, no network."""
    id = "installed"
    label = "Installed"
    licence = ""
    sorts = ("Name", "Newest", "Source")

    def __init__(self):
        self.library = None          # set by the browser
        self._all = None

    def entries(self):
        if self._all is None:
            self._all = self.library.scan() if self.library else []
        return self._all

    def assets(self):
        out = []
        for e in self.entries():
            src = SOURCES.get(e["source"])
            src_label = src.label if src and e["source"] != self.id else "Local"
            res = e["resolution"]
            out.append(Asset(self.id, e["rel"] or e["name"],
                             "%s %s" % (e["name"], res) if res else e["name"],
                             thumb_url=e["thumb_url"], tags=e["tags"] + [src_label],
                             categories=e["categories"], page_url=e["page"], max_res=res,
                             extra={"entry": e, "origin": src_label}))
        return out

    def categories(self):
        assets = self.assets()
        counts = {}
        for a in assets:
            counts[("src:" + a.extra["origin"], a.extra["origin"])] = counts.get(("src:" + a.extra["origin"], a.extra["origin"]), 0) + 1
        cats = {}
        for a in assets:
            for c in a.categories:
                cats[c] = cats.get(c, 0) + 1
        by_source = sorted(((k, label, n) for (k, label), n in counts.items()), key=lambda c: c[1].lower())
        return by_source + sorted((("cat:" + c, c.replace("-", " / ").title() if c.islower() else c, n)
                                   for c, n in cats.items()), key=lambda c: c[1].lower())

    def search(self, query, sort, page, page_size, category=ALL):
        items = []
        for a in self.assets():
            if category.startswith("src:") and a.extra["origin"] != category[4:]:
                continue
            if category.startswith("cat:") and category[4:] not in a.categories:
                continue
            if a.matches(query):
                items.append(a)
        if sort == "Newest":
            items.sort(key=lambda a: a.extra["entry"].get("downloaded", ""), reverse=True)
        elif sort == "Source":
            items.sort(key=lambda a: (a.extra["origin"].lower(), a.name.lower()))
        else:
            items.sort(key=lambda a: a.name.lower())
        return _page(items, page, page_size)


# =====================================================================================
def _extract_maps(zpath, work):
    """Unpack the image files of a material zip (flattened) and sort them into roles."""
    out_dir = os.path.join(work, "maps")
    os.makedirs(out_dir, exist_ok=True)
    paths = []
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = os.path.basename(info.filename)
            if not classify(name):
                continue
            dst = os.path.join(out_dir, name)
            with z.open(info) as src, open(dst, "wb") as f:
                shutil.copyfileobj(src, f)
            paths.append(dst)
    maps = pick_maps(paths)
    if not maps:
        raise RuntimeError("no recognisable texture maps in the download")
    return maps


SOURCES = {s.id: s for s in (AmbientCG(), PolyHaven(), FreePBR(), Installed())}
ORDER = ("ambientcg", "polyhaven", "freepbr")          # online sources, in tab order
TABS = ORDER + ("installed",)


def fetch(library, asset, res, progress=lambda f: None, cancel=None, waiting=lambda holder: None):
    """Download asset at res into the library, or reuse it. Returns (folder, maps, reused).

    On a shared library another PC may be downloading the same material right
    now: then we wait for it (waiting(holder) is called) and use theirs.
    """
    src = SOURCES[asset.source]
    res = (src.fixed_resolution or res).upper()
    found = library.find(asset, res)
    if found:
        return found[0], found[1], True

    deadline = time.time() + 20 * 60
    while True:
        have, holder = library.lock(asset, res)
        if have:
            break
        waiting(holder)
        while time.time() < deadline:
            if cancel is not None and cancel.is_set():
                raise net.Cancelled()
            time.sleep(2.0)
            found = library.find(asset, res)
            if found:
                return found[0], found[1], True
            if not os.path.exists(library._lock_path(asset, res)):
                break                                  # they gave up: our turn
        else:
            raise RuntimeError("%s has been downloading this for 20 minutes - try again later" % holder)

    work = library.staging()
    try:
        files = src.download(asset, res, work, progress, cancel)
        preview = _preview(library, asset, work)
        folder, maps = library.store(asset, res, files, src.licence, work, {"preview": preview} if preview else None)
        return folder, maps, False
    finally:
        library.unlock(asset, res)
        shutil.rmtree(work, ignore_errors=True)


def _preview(library, asset, work):
    """A PNG/JPG thumbnail to keep with the material (for the Installed tab)."""
    cached = library.thumb_path(asset)
    if os.path.isfile(cached) and image_kind(cached) in ("png", "jpg"):
        return cached
    if not asset.thumb_url:
        return None
    path = os.path.join(work, "preview")
    try:
        net.download(asset.thumb_url, path)
        return path if image_kind(path) in ("png", "jpg") else None
    except Exception:
        return None
