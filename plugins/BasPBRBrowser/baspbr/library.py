"""The local material library and texture-map sorting — plain Python.

Every downloaded material lives in its own folder:

    <library>/<Source>/<Name>_<RES>/
        <Name>_<RES>_Color.jpg, ..._NormalGL.jpg, ..._Roughness.jpg, ...
        asset.json        source, id, name, licence, resolution, maps

Maps are renamed to one naming scheme whatever the source called them, so the
Redshift builder only ever deals with roles. A material that's already in the
library is reused instead of downloaded again. When a material graph said more
than file names can (AMD's MaterialX: channels, constants, tiling), asset.json
keeps that too, under "graph".
"""
import collections
import json
import os
import re
import shutil
import tempfile
import time

# Roles the Redshift builder understands, and the file-name tokens that mean
# them. Order matters: a file is given the first role whose tokens match.
ROLES = ("color", "normal_gl", "normal_dx", "roughness", "gloss", "metalness",
         "ao", "height", "opacity", "emission", "orm")

_ROLE_TOKENS = [
    # packed: R = ambient occlusion, G = roughness, B = metalness (AMD's "ORM", Poly Haven's "arm")
    ("orm", ("orm", "arm", "occlusionroughnessmetallic", "occlusion_roughness_metallic")),
    ("normal_dx", ("normaldx", "nor_dx", "normal_dx", "normal-dx", "nrm_dx", "ndx")),
    ("normal_gl", ("normalgl", "nor_gl", "normal_gl", "normal-ogl", "normal_ogl", "normal-gl",
                   "normal", "nrm", "nor", "norm")),
    ("roughness", ("roughness", "rough", "rgh")),
    ("gloss", ("glossiness", "gloss")),
    ("metalness", ("metalness", "metallic", "metal", "mtl")),
    ("ao", ("ambientocclusion", "ambient_occlusion", "occlusion", "ao")),
    ("height", ("displacement", "height", "disp", "displace", "bump")),
    ("opacity", ("opacity", "alpha", "transparency")),
    ("emission", ("emission", "emissive", "emit")),
    ("color", ("basecolor", "base_color", "base-color", "albedo", "diffuse", "color", "colour",
               "diff", "col", "basecol")),
]
_SKIP = ("preview", "thumb", "thumbnail", "sphere", "cube", "rough_ao", "packed")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".exr", ".tga", ".webp")

# canonical suffix written into file names
SUFFIX = {"color": "Color", "normal_gl": "NormalGL", "normal_dx": "NormalDX", "roughness": "Roughness",
          "gloss": "Glossiness", "metalness": "Metalness", "ao": "AmbientOcclusion",
          "height": "Displacement", "opacity": "Opacity", "emission": "Emission", "orm": "ORM"}


def classify(filename):
    """Role of a texture file from its name, or None (previews, packed maps, other files)."""
    stem, ext = os.path.splitext(os.path.basename(filename))
    if ext.lower() not in IMAGE_EXT:
        return None
    s = stem.lower()
    tokens = [t for t in re.split(r"[\s_.\-]+", s) if t]
    if not tokens:
        return None
    # look at the end of the name first ("..._NormalGL", "..._nor_gl_4k"), skipping
    # resolution / format tokens such as "4k", "8k", "jpg", "png"
    while len(tokens) > 1 and re.fullmatch(r"\d+k|\d+|jpg|png|exr|tif|lq|hq", tokens[-1]):
        tokens.pop()
    tail1 = tokens[-1]
    tail2 = "_".join(tokens[-2:]) if len(tokens) > 1 else tail1
    if tail1 in _SKIP or tail2 in _SKIP:
        return None
    for candidate in (tail2, tail2.replace("_", "-"), tail2.replace("_", ""), tail1):
        for role, words in _ROLE_TOKENS:
            if candidate in words:
                return role
    # anywhere in the name, as a last resort
    joined = "_".join(tokens)
    for role, words in _ROLE_TOKENS:
        for w in words:
            if len(w) >= 4 and w in joined:
                return role
    return None


def pick_maps(paths):
    """{role: path} from a list of files; the first file wins per role."""
    maps = {}
    for p in sorted(paths):
        role = classify(p)
        if role and role not in maps:
            maps[role] = p
    return maps


# one channel of a texture: index 0..3 = R, G, B, A; value = min + (max - min) * sample
Channel = collections.namedtuple("Channel", "index min max")


class MapSet(dict):
    """{role: path}, plus what a material graph says that file names can't: which
    channel of a file a value sits in and how it's remapped, constants instead of
    textures, and the authored tiling. Sources without a graph leave these empty,
    and a plain {role: path} dict works wherever a MapSet does."""

    def __init__(self, maps=(), graph=None):
        dict.__init__(self, maps)
        self.channels = {}           # role -> Channel
        self.base_color = None       # [r, g, b] linear, when the colour is a constant
        self.roughness = None        # constants, when there's no texture
        self.metalness = None
        self.tiling = 1.0
        if graph:
            try:
                self.channels = {r: Channel(int(c[0]), float(c[1]), float(c[2]))
                                 for r, c in (graph.get("channels") or {}).items()}
                if graph.get("base_color"):
                    self.base_color = [float(v) for v in graph["base_color"]][:3]
                for k in ("roughness", "metalness"):
                    if graph.get(k) is not None:
                        setattr(self, k, float(graph[k]))
                self.tiling = float(graph.get("tiling") or 1.0)
            except (TypeError, ValueError, IndexError, AttributeError):
                pass                 # a damaged asset.json: just the files

    def graph(self):
        """What to keep in asset.json; {} when there's nothing beyond the files."""
        g = {}
        if self.channels:
            g["channels"] = {r: list(c) for r, c in self.channels.items()}
        if self.base_color is not None:
            g["base_color"] = list(self.base_color)
        for k in ("roughness", "metalness"):
            if getattr(self, k) is not None:
                g[k] = getattr(self, k)
        if self.tiling != 1.0:
            g["tiling"] = self.tiling
        return g


# Sources whose downloads carry a material graph. Their folders made by a version
# that guessed the maps from file names (no "graph" in asset.json) are fetched again.
GRAPH_SOURCES = ("amd",)


def safe_name(name):
    s = re.sub(r"[^A-Za-z0-9]+", " ", name or "").strip()
    s = "".join(w[:1].upper() + w[1:] for w in s.split(" ") if w)
    return s or "Material"


def image_kind(path):
    """'png' / 'jpg' / 'webp' / None from the file's first bytes (not its name)."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return None
    if head.startswith(b"\x89PNG"):
        return "png"
    if head.startswith(b"\xff\xd8"):
        return "jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def unc_path(path):
    r"""The \\server\share form of a path on a mapped network drive (Windows), else None."""
    if os.name != "nt" or not path:
        return None
    drive, rest = os.path.splitdrive(path)
    if len(drive) != 2:
        return None                       # already UNC, or relative
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        if ctypes.windll.mpr.WNetGetConnectionW(drive, buf, ctypes.byref(size)) == 0 and buf.value:
            return buf.value.rstrip("\\") + rest
    except Exception:
        pass
    return None


def local_cache_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "BasPBRBrowser")


_SKIP_DIRS = ("_thumbs", "_incoming")
LOCK_STALE_SECONDS = 15 * 60


class Library(object):
    """A material library folder — local or on a network share several PCs use.

    Sharing rules: a material appears only once it's complete (it's assembled in
    <library>/_incoming on the same drive, then renamed into place in one step);
    two PCs asking for the same material don't both download it (a lock file in
    _incoming makes the second one wait for the first); asset.json stores file
    names only, so it reads the same however each PC reaches the folder.
    """

    def __init__(self, root):
        self.root = root
        self._writable = None

    # ---- paths ----------------------------------------------------------------
    def asset_dir(self, asset, res):
        return os.path.join(self.root, asset.source_label, "%s_%s" % (safe_name(asset.name), res.upper()))

    # ---- models (Sketchfab) -------------------------------------------------------
    def model_dir(self, asset):
        """<library>/<Source>/<Name>_<the uid's first 6>: models sharing a name stay apart."""
        return self.asset_dir(asset, asset.id[:6])

    def find_model(self, asset):
        """(folder, info) when the model is in the library already, else None."""
        d = self.model_dir(asset)
        info = _read_json(os.path.join(d, "asset.json"))
        if info and info.get("kind") == "model" and info.get("model") and \
                os.path.isfile(os.path.join(d, info["model"])):
            return d, info
        return None

    def store_model(self, asset, staged, info):
        """Write asset.json into the assembled folder staged and rename it into place
        in one go (as store() does for materials). Returns (folder, info)."""
        info = dict(info, source=asset.source, id=asset.id, name=asset.name, kind="model",
                    page=asset.page_url, thumb_url=asset.thumb_url,
                    categories=list(asset.categories or []), tags=list(asset.tags or []),
                    downloaded=time.strftime("%Y-%m-%d %H:%M"), downloaded_by=_host())
        with open(os.path.join(staged, "asset.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, indent=1)
        dest = self.model_dir(asset)
        if os.path.isdir(dest):
            found = self.find_model(asset)        # another PC finished first: use theirs
            if found:
                return found
            shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(staged, dest)
        return dest, info

    def writable(self):
        if self._writable is None:
            try:
                d = os.path.join(self.root, "_incoming")
                os.makedirs(d, exist_ok=True)
                probe = os.path.join(d, "probe_%s_%d" % (re.sub(r"\W", "", _host()), os.getpid()))
                with open(probe, "w") as f:
                    f.write("ok")
                os.remove(probe)
                self._writable = True
            except Exception:
                self._writable = False
        return self._writable

    def thumb_path(self, asset):
        url = (asset.thumb_url or "").lower()
        ext = ".png" if ("png" in url and "webp" not in url) else ".jpg"
        key = re.sub(r"[^A-Za-z0-9_\-]+", "_", asset.id)
        base = os.path.join(self.root, "_thumbs") if self.writable() else os.path.join(local_cache_dir(), "thumbs")
        return os.path.join(base, asset.source_label, key + ext)

    # ---- what's already downloaded ---------------------------------------------
    def resolutions(self, asset):
        """The sizes of asset that are in the library already, e.g. ["4K", "8K"]."""
        folder = os.path.join(self.root, asset.source_label)
        prefix = safe_name(asset.name) + "_"
        try:
            names = os.listdir(folder)
        except OSError:
            return []
        return [n[len(prefix):] for n in names
                if n.startswith(prefix) and re.fullmatch(r"\d+K", n[len(prefix):])
                and os.path.isfile(os.path.join(folder, n, "asset.json"))]

    def find(self, asset, res):
        """(folder, maps) if this asset at this resolution is in the library, else None."""
        d = self.asset_dir(asset, res)
        info = _read_json(os.path.join(d, "asset.json"))
        if info is None:
            return None
        if asset.source in GRAPH_SOURCES and "graph" not in info:
            return None                  # its maps were guessed from file names: fetch it again
        maps = _maps_of(d, info)
        return (d, maps) if len(maps) or maps.graph() else None

    def scan(self):
        """Every material in the library, as dicts — the plugin's own downloads
        (folders with asset.json) and any other folder holding recognisable maps."""
        out = []
        if not os.path.isdir(self.root):
            return out

        def walk(folder, rel, depth):
            try:
                names = os.listdir(folder)
            except OSError:
                return
            if "asset.json" in names:
                info = _read_json(os.path.join(folder, "asset.json"))
                if info:
                    out.append(_entry_from_info(folder, rel, info))
                return
            images = [n for n in names if os.path.splitext(n)[1].lower() in IMAGE_EXT]
            maps = pick_maps([os.path.join(folder, n) for n in images])
            if "color" in maps and len(maps) >= 2:
                out.append(_entry_from_maps(folder, rel, maps, images))
                return
            if depth < 4:
                for n in sorted(names):
                    if n.startswith(".") or (depth == 0 and n in _SKIP_DIRS):
                        continue
                    sub = os.path.join(folder, n)
                    if os.path.isdir(sub):
                        walk(sub, (rel + "/" + n) if rel else n, depth + 1)

        walk(self.root, "", 0)
        return out

    def index(self, entries=None):
        """{"<source>:<id>"} of everything downloaded through the plugin."""
        return {"%s:%s" % (e["source"], e["id"]) for e in (entries if entries is not None else self.scan())
                if e.get("managed")}

    # ---- downloading into the library -----------------------------------------------
    def staging(self):
        """A fresh working folder on the library's own drive."""
        if not self.writable():
            raise RuntimeError("the library folder isn't writable from this PC: " + self.root)
        return tempfile.mkdtemp(prefix="%s_" % re.sub(r"\W", "", _host()), dir=os.path.join(self.root, "_incoming"))

    def lock(self, asset, res):
        """Claim the download of asset@res. Returns (True, None) when we have it,
        (False, holder) when another PC is downloading it right now."""
        path = self._lock_path(asset, res)
        for _ in range(2):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as f:
                    f.write("%s\n%s\n" % (_host(), time.strftime("%Y-%m-%d %H:%M:%S")))
                return True, None
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(path)
                    holder = open(path).read().split("\n")[0] or "another PC"
                except OSError:
                    continue                      # it just went away; try again
                if age > LOCK_STALE_SECONDS:
                    try:
                        os.remove(path)           # a crashed download; take it over
                    except OSError:
                        pass
                    continue
                return False, holder
            except OSError:
                return True, None                 # can't lock (odd share) - just download
        return True, None

    def unlock(self, asset, res):
        try:
            os.remove(self._lock_path(asset, res))
        except OSError:
            pass

    def _lock_path(self, asset, res):
        key = re.sub(r"[^A-Za-z0-9_\-]+", "_", "%s_%s_%s" % (asset.source, asset.id, res.upper()))
        return os.path.join(self.root, "_incoming", key + ".lock")

    def store(self, asset, res, files, licence, staging, extra=None):
        """Assemble the asset folder in staging, then rename it into place in one go.

        files: {role: path} or a MapSet (several roles may share one file, e.g.
        roughness and metalness in two channels of a mask); extra may carry
        "preview" (a thumbnail file to keep with the material).
        Returns (folder, MapSet).
        """
        final = os.path.join(staging, "asset")
        os.makedirs(final, exist_ok=True)
        base = "%s_%s" % (safe_name(asset.name), res.upper())
        names, moved = {}, {}
        for role, src in files.items():
            if src not in moved:
                shared = sum(1 for s in files.values() if s == src) > 1
                suffix = SUFFIX[role] if not shared else ("ORM" if classify(src) == "orm" else "Mask")
                fn = "%s_%s%s" % (base, suffix, os.path.splitext(src)[1].lower())
                n = 2
                while fn in moved.values():
                    fn = "%s_%s%d%s" % (base, suffix, n, os.path.splitext(src)[1].lower())
                    n += 1
                shutil.move(src, os.path.join(final, fn))
                moved[src] = fn
            names[role] = moved[src]
        extra = dict(extra or {})
        preview = extra.pop("preview", None)
        kind = image_kind(preview) if preview else None
        if kind in ("png", "jpg"):
            shutil.copyfile(preview, os.path.join(final, "preview." + kind))
            extra["preview"] = "preview." + kind
        info = {"source": asset.source, "id": asset.id, "name": asset.name, "resolution": res.upper(),
                "licence": licence, "page": asset.page_url, "maps": names,
                "categories": list(getattr(asset, "categories", []) or []), "tags": list(asset.tags or []),
                "thumb_url": asset.thumb_url, "downloaded": time.strftime("%Y-%m-%d %H:%M"),
                "downloaded_by": _host()}
        graph = files.graph() if isinstance(files, MapSet) else None
        if graph is not None:
            info["graph"] = graph
        info.update(extra)
        with open(os.path.join(final, "asset.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, indent=1)

        dest = self.asset_dir(asset, res)
        if os.path.isdir(dest):
            if os.path.isfile(os.path.join(dest, "asset.json")):
                found = self.find(asset, res)    # another PC finished first: use theirs
                if found:
                    return found
            shutil.rmtree(dest, ignore_errors=True)   # an incomplete leftover
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(final, dest)
        return dest, MapSet({r: os.path.join(dest, fn) for r, fn in names.items()}, graph)


# ---- helpers -------------------------------------------------------------------------------
def scan_all(libraries):
    """Every entry of several libraries. A folder that is - or lies inside - one
    already read isn't read again."""
    def norm(lib):
        return os.path.normcase(os.path.abspath(lib.root)).rstrip("\\/") + os.sep
    read, out = [], []
    for lib in sorted((l for l in libraries if l is not None), key=lambda l: len(norm(l))):
        r = norm(lib)
        if any(r.startswith(done) for done in read):
            continue
        read.append(r)
        out.extend(lib.scan())
    return out


def _host():
    try:
        import socket
        return socket.gethostname() or "PC"
    except Exception:
        return "PC"


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _maps_of(folder, info):
    """The MapSet an asset.json describes (files that are really there, plus its graph)."""
    return MapSet({r: os.path.join(folder, fn) for r, fn in (info.get("maps") or {}).items()
                   if os.path.isfile(os.path.join(folder, fn))}, info.get("graph"))


def _entry_from_info(folder, rel, info):
    maps = _maps_of(folder, info)
    preview = info.get("preview")
    return {"folder": folder, "rel": rel, "managed": True, "source": info.get("source", "local"),
            "id": info.get("id", rel), "name": info.get("name") or os.path.basename(folder),
            "resolution": info.get("resolution", ""), "maps": maps,
            "categories": info.get("categories") or [], "tags": info.get("tags") or [],
            "thumb": os.path.join(folder, preview) if preview and os.path.isfile(os.path.join(folder, preview)) else None,
            "thumb_url": info.get("thumb_url", ""), "page": info.get("page", ""),
            "licence": info.get("licence", ""), "downloaded": info.get("downloaded", ""),
            "downloaded_by": info.get("downloaded_by", ""),
            "kind": info.get("kind", "material"),
            "model": os.path.join(folder, info["model"]) if info.get("model") else None,
            "images": {int(k): os.path.join(folder, v) for k, v in (info.get("images") or {}).items()},
            "author": info.get("author", "")}


def _entry_from_maps(folder, rel, maps, images):
    name = os.path.basename(folder)
    res = re.search(r"(\d+)[kK]\b", name)
    preview = next((os.path.join(folder, n) for n in images
                    if re.search(r"preview|thumb", n, re.I) and image_kind(os.path.join(folder, n)) in ("png", "jpg")), None)
    return {"folder": folder, "rel": rel, "managed": False, "source": "local", "id": rel,
            "name": re.sub(r"[_\-]+", " ", name), "resolution": (res.group(1) + "K") if res else "",
            "maps": maps, "categories": [rel.split("/")[0]] if "/" in rel else [], "tags": [],
            "thumb": preview, "thumb_url": "", "page": "", "licence": "", "downloaded": "",
            "downloaded_by": ""}
