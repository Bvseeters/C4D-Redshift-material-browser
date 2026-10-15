"""Material sources — ambientCG, Poly Haven, FreePBR, ShareTextures, the merged
"Everything" view, and the library itself ("Installed"). Plain Python.

Every online source loads its WHOLE catalogue once (a few seconds the first
time, then from a disk cache for 12 hours), and searching, sorting and
category filtering happen locally. That's what makes the grid scroll endlessly
without paging, and what lets "Everything" search all sites at once.

  ambientCG     /api/v2/full_json?type=Material&limit=5000&include=imageData
                (~2000 materials). Download links per material on demand:
                full_json?id=<id>&include=downloadData. One ZIP per resolution.
  Poly Haven    /assets?t=textures (filtered locally), /files/<id> per asset;
                separate map files. Thumbnails asked for as PNG (the CDN's
                default WebP can't be read by Cinema 4D).
  FreePBR       WooCommerce Store API catalogue; the download is a form POST on
                the product page that returns a ZIP. 2K only. NOT CC0: the free
                downloads are for non-commercial use.
  ShareTextures api2.sharetextures.com/api/v0/for-frontend/items (~1800 textures),
                item/<slug> for its ZIP links (1K/2K/4K). Its images are WebP
                only, so thumbnails come through the site's own image resizer,
                which hands out JPEG to clients that don't take WebP. CC0.
  Everything    all of the above in one list; categories merged by name.
  Installed     what's in the library folder (maybe a share other PCs fill too).
"""
import html
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import zipfile
from datetime import datetime

from . import net
from .library import classify, image_kind, local_cache_dir, pick_maps

RESOLUTIONS = ("1K", "2K", "4K", "8K")
ALL = ""                       # the "All" category
CACHE_TTL = 12 * 3600          # catalogues are re-read from the sites twice a day


class Asset(object):
    __slots__ = ("source", "id", "name", "thumb_url", "tags", "categories", "page_url", "max_res", "extra")

    def __init__(self, source, id, name, thumb_url="", tags=(), page_url="", max_res="", extra=None,
                 categories=()):
        self.source = source            # a SOURCES key
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

    def to_dict(self):
        return {"s": self.source, "i": self.id, "n": self.name, "t": self.thumb_url, "g": self.tags,
                "c": self.categories, "p": self.page_url, "m": self.max_res, "x": self.extra}

    @classmethod
    def from_dict(cls, d):
        return cls(d["s"], d["i"], d["n"], d.get("t", ""), d.get("g", ()), d.get("p", ""), d.get("m", ""),
                   d.get("x") or {}, d.get("c", ()))


def _res_value(r):
    m = re.match(r"(\d+)", r or "")
    return int(m.group(1)) if m else 0


def _timestamp(value):
    """Seconds since 1970 from a unix time or an ISO / 'YYYY-MM-DD hh:mm:ss' date."""
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return 0.0
    try:
        return datetime.strptime(str(value)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").timestamp()
        except ValueError:
            return 0.0


# ---- merged categories ----------------------------------------------------------------------
# Different sites call the same thing differently ("Metals", "Basic Metals", "metal";
# "Rock & Stone"; "plaster-concrete"). Each category is split into parts, singular-
# ised and mapped through these synonyms, so the Everything sidebar shows one entry.
_SYNONYMS = {
    "cloth": "fabric", "textile": "fabric", "fabric": "fabric", "carpet": "carpet",
    "path": "road", "road": "road", "street": "road", "asphalt": "asphalt",
    "paving stone": "paving", "pavingstone": "paving", "pavement": "paving", "paving": "paving",
    "tree": "plants", "plant": "plants", "foliage": "plants", "leaf": "plants", "grass": "grass",
    "roofing": "roof", "roof": "roof", "roof tile": "roof", "rooftile": "roof",
    "basic metal": "metal", "metal": "metal", "metal plate": "metal", "diamond plate": "metal",
    "concrete etc": "concrete", "concrete": "concrete", "plaster": "plaster",
    "wood floor": "wood floor", "woodfloor": "wood floor", "parquet": "wood floor", "floor": "floor",
    "wood": "wood", "plank": "wood", "bark": "wood", "wood siding": "wood",
    "rock": "rock", "stone": "stone", "brick": "brick", "tile": "tiles", "marble": "marble",
    "wall": "wall", "ground": "ground", "terrain": "ground", "soil": "ground", "dirt": "ground",
    "sci fi": "sci-fi", "scifi": "sci-fi", "sci-fi": "sci-fi",
    "man made": "man-made", "manmade": "man-made",
    "paper product": "paper", "paper": "paper", "countertop": "countertop",
    "surface imperfection": "imperfections", "imperfection": "imperfections",
    "gem": "gems", "animal": "animals", "leather": "leather", "gravel": "gravel",
}
_LABELS = {"sci-fi": "Sci-Fi", "man-made": "Man-made", "wood floor": "Wood Floor"}


def _singular(word):
    if len(word) <= 3 or word.endswith(("ss", "ves", "us")):
        return word
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "xes")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def canon_categories(cats):
    """Merged category labels for a list of source categories."""
    out = []
    for cat in cats:
        if not cat:
            continue
        spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", cat)          # "PavingStones" -> "Paving Stones"
        for part in re.split(r"\s*(?:&|/|,|\band\b|(?<=[a-z])-(?=[a-z]{4,}))\s*", spaced.lower()):
            part = re.sub(r"[^a-z0-9\- ]+", " ", part).strip()
            part = re.sub(r"\s+etc$", "", part)
            if not part:
                continue
            words = part.split()
            words[-1] = _singular(words[-1])
            key = " ".join(words)
            key = _SYNONYMS.get(key, _SYNONYMS.get(key.replace(" ", ""), key))
            label = _LABELS.get(key) or key.title()
            if label not in out:
                out.append(label)
    return out


# =====================================================================================
class Source(object):
    id = ""
    label = ""
    licence = ""
    sorts = ("Name",)
    fixed_resolution = ""      # set when the source offers one resolution only
    best_resolution = "8K"

    def __init__(self):
        self._all = None
        self._lock = threading.Lock()

    # ---- the catalogue -------------------------------------------------------------------
    def fetch_catalogue(self):
        """Every asset this source has, from the network."""
        raise NotImplementedError

    def _cache_path(self):
        return os.path.join(local_cache_dir(), "catalogues", self.id + ".json")

    def catalogue(self, refresh=False):
        with self._lock:
            if self._all is not None and not refresh:
                return self._all
            path = self._cache_path()
            fresh = os.path.isfile(path) and time.time() - os.path.getmtime(path) < CACHE_TTL
            if fresh and not refresh:
                cached = self._read_cache(path)
                if cached is not None:
                    self._all = cached
                    return cached
            try:
                items = self.fetch_catalogue()
            except Exception:
                cached = self._read_cache(path) if os.path.isfile(path) else None
                if cached is None:
                    raise
                items = cached                      # offline: the last good copy
            else:
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path + ".tmp", "w", encoding="utf-8") as f:
                        json.dump([a.to_dict() for a in items], f)
                    os.replace(path + ".tmp", path)
                except OSError:
                    pass
            self._all = items
            return items

    @staticmethod
    def _read_cache(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return [Asset.from_dict(d) for d in json.load(f)]
        except Exception:
            return None

    def loaded(self):
        return self._all is not None

    def refresh(self):
        self._all = None
        try:
            os.remove(self._cache_path())
        except OSError:
            pass

    # ---- browsing ----------------------------------------------------------------------------
    def categories(self):
        """[(key, label, count)] for the sidebar."""
        counts = {}
        for a in self.catalogue():
            for c in a.categories:
                counts[c] = counts.get(c, 0) + 1
        return sorted(((c, c.replace("-", " / ").title() if c.islower() else c, n) for c, n in counts.items()),
                      key=lambda r: r[1].lower())

    def search(self, query, sort, category=ALL):
        """Every matching asset, sorted (the grid shows them as you scroll)."""
        items = [a for a in self.catalogue() if (not category or category in a.categories) and a.matches(query)]
        return self.sort(items, sort)

    def sort(self, items, sort):
        if sort in ("Popular", "Most downloaded"):
            items.sort(key=lambda a: -a.extra.get("pop", 0))
        elif sort in ("Newest", "Latest"):
            items.sort(key=lambda a: -a.extra.get("date", 0))
        else:
            items.sort(key=lambda a: a.name.lower())
        return items

    def download(self, asset, res, work, progress, cancel):
        """Download the maps into the folder `work`. Returns {role: path}."""
        raise NotImplementedError


# =====================================================================================
class AmbientCG(Source):
    id = "ambientcg"
    label = "ambientCG"
    licence = "CC0 (public domain)"
    sorts = ("Popular", "Latest", "Name")
    API = "https://ambientcg.com/api/v2/"

    def fetch_catalogue(self):
        data = net.get_json(self.API + "full_json?type=Material&limit=5000&offset=0&include=imageData")
        out = []
        for a in data.get("foundAssets", []):
            aid = a.get("assetId")
            if not aid:
                continue
            prev = a.get("previewImage") or {}
            thumb = prev.get("256-PNG") or prev.get("256-JPG-FFFFFF") or next(
                (v for v in prev.values() if isinstance(v, str)), "")
            cat = a.get("displayCategory") or a.get("category")
            out.append(Asset(self.id, aid, a.get("displayName") or aid, thumb,
                             tags=a.get("tags") or [], categories=[cat] if cat else [],
                             page_url="https://ambientcg.com/view?id=" + aid, max_res="8K",
                             extra={"pop": float(a.get("popularityScore") or a.get("downloadCount") or 0),
                                    "date": _timestamp(a.get("releaseDate"))}))
        if not out:
            raise RuntimeError("ambientCG returned no materials")
        return out

    def downloads(self, asset_id):
        data = net.get_json(self.API + "full_json?id=%s&include=downloadData" % urllib.parse.quote(asset_id))
        out = []
        for a in data.get("foundAssets", []):
            try:
                for d in a["downloadFolders"]["default"]["downloadFiletypeCategories"]["zip"]["downloads"]:
                    if d.get("attribute") and (d.get("downloadLink") or d.get("fullDownloadPath")):
                        out.append({"attribute": d["attribute"], "url": d.get("downloadLink") or d.get("fullDownloadPath"),
                                    "size": d.get("size", 0)})
            except (KeyError, TypeError):
                pass
        return out

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
        dl = self.pick(asset.extra.get("downloads") or self.downloads(asset.id), res)
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
    sorts = ("Popular", "Newest", "Name")
    API = "https://api.polyhaven.com"
    # Poly Haven file keys -> our roles. OpenGL normals (Redshift's convention).
    MAPS = (("Diffuse", "color"), ("nor_gl", "normal_gl"), ("Rough", "roughness"), ("metal", "metalness"),
            ("AO", "ao"), ("Displacement", "height"), ("Opacity", "opacity"), ("alpha", "opacity"))

    @staticmethod
    def png_thumb(url, aid):
        """Poly Haven's CDN serves WebP unless asked otherwise; Cinema 4D can't read WebP."""
        url = url or "https://cdn.polyhaven.com/asset_img/thumbs/%s.png?width=256&height=256" % aid
        if "format=" in url:
            return re.sub(r"format=[a-z]+", "format=png", url)
        return url + ("&" if "?" in url else "?") + "format=png"

    def fetch_catalogue(self):
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
                             extra={"pop": float(a.get("download_count") or 0),
                                    "date": _timestamp(a.get("date_published"))}))
        return out

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
    sorts = ("Newest", "Name")
    fixed_resolution = "2K"
    best_resolution = "2K"
    ROOT = "https://freepbr.com"

    def fetch_catalogue(self):
        out = []
        for page in range(1, 21):              # ~620 products today; hard stop either way
            try:
                arr = net.get_json("%s/wp-json/wc/store/v1/products?per_page=100&page=%d" % (self.ROOT, page))
            except net.urllib.error.HTTPError:
                break                          # a page past the end answers 400
            if not isinstance(arr, list) or not arr:
                break
            for p in arr:
                price = (p.get("prices") or {}).get("price")
                slug = p.get("slug") or ""
                if (price not in (None, "0")) or not slug or "license" in slug or "download-all" in slug:
                    continue               # the paid bundle licences
                imgs = p.get("images") or [{}]
                out.append(Asset(self.id, str(p.get("id")), html.unescape(p.get("name") or slug),
                                 imgs[0].get("thumbnail") or imgs[0].get("src") or "",
                                 tags=[html.unescape(t.get("name", "")) for t in (p.get("tags") or [])],
                                 categories=[html.unescape(c.get("name", "")) for c in (p.get("categories") or [])
                                             if c.get("slug") not in ("all", "uncategorized")],
                                 page_url=p.get("permalink") or "%s/product/%s/" % (self.ROOT, slug),
                                 max_res="2K", extra={"slug": slug, "date": float(p.get("id") or 0)}))
            if len(arr) < 100:
                break
        return out

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
class ShareTextures(Source):
    id = "sharetextures"
    label = "ShareTextures"
    licence = "CC0 (public domain)"
    sorts = ("Popular", "Newest", "Name")
    best_resolution = "4K"
    API = "https://api2.sharetextures.com/api/v0/for-frontend/"
    SITE = "https://www.sharetextures.com"
    IMAGES = "https://images.sharetextures.com/"

    @classmethod
    def thumb(cls, object_key):
        """The site's image resizer: JPEG for clients that don't accept WebP."""
        if not object_key:
            return ""
        return "%s/_next/image?url=%s&w=256&q=80" % (cls.SITE, urllib.parse.quote(cls.IMAGES + object_key, safe=""))

    def fetch_catalogue(self):
        data = net.get_json(self.API + "items?itemType=textures&page=1&perPage=5000&sortBy=most_recent")
        out = []
        for it in (data.get("data") or {}).get("items", []):
            slug = it.get("slug")
            cat = (it.get("category") or {})
            if not slug or it.get("isEarlyAccess") or cat.get("slug") == "sbsar":
                continue                   # Substance files only, no texture maps
            img = it.get("previewImage1") or {}
            out.append(Asset(self.id, slug, it.get("title") or slug,
                             self.thumb(img.get("thumbObjectKey") or img.get("originalObjectKey")),
                             tags=[t.get("name", "") for t in (it.get("tagDetails") or [])],
                             categories=[cat["name"]] if cat.get("name") else [],
                             page_url="%s/textures/%s/%s" % (self.SITE, cat.get("slug", "texture"), slug),
                             max_res="4K",
                             extra={"pop": float(it.get("downloadCount") or 0),
                                    "date": _timestamp(it.get("publishDate"))}))
        return out

    def download(self, asset, res, work, progress, cancel):
        detail = net.get_json(self.API + "item/" + urllib.parse.quote(asset.id))
        links = [(m.group(1).upper(), l.get("value")) for l in (detail.get("data") or {}).get("downloadLinks") or []
                 for m in [re.search(r"(\d+K)", (l.get("title") or "") + " " + (l.get("value") or ""), re.I)]
                 if m and (l.get("value") or "").lower().endswith(".zip")]
        if not links:
            raise RuntimeError("ShareTextures has no texture download for " + asset.name)
        want = _res_value(res)
        links.sort(key=lambda r: _res_value(r[0]), reverse=True)
        url = next((u for r, u in links if _res_value(r) <= want), links[-1][1])
        zpath = os.path.join(work, "download.zip")
        net.download(url, zpath, lambda d, t: progress(d / float(t or 50e6)), cancel)
        return _extract_maps(zpath, work)


# =====================================================================================
class Everything(Source):
    """Every online source in one list. Categories merged by name."""
    id = "everything"
    label = "Everything"
    licence = "ambientCG, Poly Haven and ShareTextures are CC0; FreePBR is for non-commercial use"
    sorts = ("Mixed", "Newest", "Name")
    MEMBERS = ("ambientcg", "polyhaven", "sharetextures", "freepbr")

    def catalogue(self, refresh=False):
        """All member catalogues, loaded side by side. Sources that fail are left out
        (and listed in self.failed) rather than sinking the whole view."""
        results, self.failed = {}, []

        def load(sid):
            try:
                results[sid] = SOURCES[sid].catalogue(refresh)
            except Exception as exc:
                self.failed.append("%s (%s)" % (SOURCES[sid].label, exc))

        threads = [threading.Thread(target=load, args=(sid,), daemon=True) for sid in self.MEMBERS]
        [t.start() for t in threads]
        [t.join(120) for t in threads]
        out = []
        for sid in self.MEMBERS:
            for a in results.get(sid, []):
                if "canon" not in a.extra:
                    a.extra["canon"] = canon_categories(a.categories)
                out.append(a)
        return out

    def loaded(self):
        return all(SOURCES[s].loaded() for s in self.MEMBERS)

    def refresh(self):
        for s in self.MEMBERS:
            SOURCES[s].refresh()

    def categories(self):
        assets = self.catalogue()
        by_source = {}
        counts = {}
        for a in assets:
            by_source[a.source] = by_source.get(a.source, 0) + 1
            for c in a.extra["canon"]:
                counts[c] = counts.get(c, 0) + 1
        rows = [("src:" + s, SOURCES[s].label, by_source[s]) for s in self.MEMBERS if s in by_source]
        return rows + sorted(((c, c, n) for c, n in counts.items() if n >= 2), key=lambda r: r[1].lower())

    def search(self, query, sort, category=ALL):
        items = []
        for a in self.catalogue():
            if category.startswith("src:"):
                if a.source != category[4:]:
                    continue
            elif category and category not in a.extra["canon"]:
                continue
            if a.matches(query):
                items.append(a)
        if sort == "Mixed":
            # each site in its own popularity order, then dealt out in turn, so every
            # site shows up near the top
            lanes = [sorted((a for a in items if a.source == s), key=lambda a: -a.extra.get("pop", a.extra.get("date", 0)))
                     for s in self.MEMBERS]
            items = [a for row in _interleave(lanes) for a in row]
            return items
        return Source.sort(self, items, sort)


def _interleave(lanes):
    longest = max((len(l) for l in lanes), default=0)
    for i in range(longest):
        yield [l[i] for l in lanes if i < len(l)]


# =====================================================================================
class Installed(Source):
    """The library folder itself: every material in it, ready to build, no network."""
    id = "installed"
    label = "Installed"
    licence = ""
    sorts = ("Name", "Newest", "Source")

    def __init__(self):
        Source.__init__(self)
        self.library = None          # set by the browser

    def entries(self):
        if self._all is None:
            self._all = self.library.scan() if self.library else []
        return self._all

    def catalogue(self, refresh=False):
        if refresh:
            self._all = None
        return self.assets()

    def refresh(self):
        self._all = None

    def loaded(self):
        return self._all is not None

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
        counts, cats = {}, {}
        for a in assets:
            counts[a.extra["origin"]] = counts.get(a.extra["origin"], 0) + 1
            for c in a.categories:
                cats[c] = cats.get(c, 0) + 1
        by_source = sorted((("src:" + k, k, n) for k, n in counts.items()), key=lambda c: c[1].lower())
        return by_source + sorted((("cat:" + c, c.replace("-", " / ").title() if c.islower() else c, n)
                                   for c, n in cats.items()), key=lambda c: c[1].lower())

    def search(self, query, sort, category=ALL):
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
        return items


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


SOURCES = {s.id: s for s in (Everything(), AmbientCG(), PolyHaven(), FreePBR(), ShareTextures(), Installed())}
ONLINE = ("ambientcg", "polyhaven", "sharetextures", "freepbr")
TABS = ("everything",) + ONLINE + ("installed",)


def fetch(library, asset, res, progress=lambda f: None, cancel=None, waiting=lambda holder: None):
    """Download asset at res into the library, or reuse it. Returns (folder, maps, reused).

    On a shared library another PC may be downloading the same material right
    now: then we wait for it (waiting(holder) is called) and use theirs.
    """
    src = SOURCES[asset.source]
    res = (src.fixed_resolution or res).upper()
    if _res_value(res) > _res_value(src.best_resolution):
        res = src.best_resolution                 # e.g. ShareTextures stops at 4K
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


THUMB_HEADERS = {"Accept": "image/png,image/jpeg;q=0.9,*/*;q=0.1"}   # never WebP: C4D can't read it


def _preview(library, asset, work):
    """A PNG/JPG thumbnail to keep with the material (for the Installed tab)."""
    cached = library.thumb_path(asset)
    if os.path.isfile(cached) and image_kind(cached) in ("png", "jpg"):
        return cached
    if not asset.thumb_url:
        return None
    path = os.path.join(work, "preview")
    try:
        net.download(asset.thumb_url, path, headers=THUMB_HEADERS)
        return path if image_kind(path) in ("png", "jpg") else None
    except Exception:
        return None
