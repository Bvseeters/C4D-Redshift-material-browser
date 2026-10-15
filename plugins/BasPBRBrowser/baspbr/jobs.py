"""Background work for the browser: searches, categories, library scans,
thumbnails and downloads run on worker threads so Cinema 4D never freezes (a
network library can be slow to list). Plain Python.

Workers never touch the c4d API. They put results on one queue; the dialog's
timer drains it on the main thread (where creating materials is allowed):

    ("search",     token, assets, error)          every match, sorted; the grid shows them as you scroll
    ("categories", source_id, [(key, label, count)], error)
    ("scanned",    entries, error)                 the library was (re)read
    ("thumb",      key, path)                      thumbnail on disk, ready to draw
    ("thumb_make", key, color_map_path)            no thumbnail anywhere: draw one from the colour map
    ("progress",   key, fraction)                  download progress 0..1
    ("waiting",    key, holder)                    another PC is downloading this material
    ("done",       key, folder, maps, reused, asset, payload)
    ("error",      key, message)
"""
import os
import queue
import re
import threading
import traceback

from . import net, sources
from .library import image_kind, local_cache_dir


class Jobs(object):
    def __init__(self, library, thumb_threads=4):
        self.library = library
        self.results = queue.Queue()
        self._thumbs = queue.LifoQueue()        # newest first: what's in view right now
        self._downloads = queue.Queue()
        self._thumb_seen = set()
        self._cancel = threading.Event()
        self.pending = []               # asset keys queued or downloading, in order
        self.busy_key = None
        for _ in range(thumb_threads):
            threading.Thread(target=self._thumb_loop, daemon=True).start()
        threading.Thread(target=self._download_loop, daemon=True).start()

    def _run(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    # ---- searches, categories, the library ---------------------------------------------
    def search(self, token, source_id, query, sort, category=sources.ALL):
        def run():
            try:
                assets = sources.SOURCES[source_id].search(query, sort, category)
                self.results.put(("search", token, assets, None))
            except Exception as exc:
                traceback.print_exc()
                self.results.put(("search", token, [], message(exc)))
        self._run(run)

    def categories(self, source_id):
        def run():
            try:
                self.results.put(("categories", source_id, sources.SOURCES[source_id].categories(), None))
            except Exception as exc:
                self.results.put(("categories", source_id, [], message(exc)))
        self._run(run)

    def scan(self):
        """Re-read the library folder (it may have been filled from other PCs)."""
        lib = self.library

        def run():
            try:
                lib.writable()          # touches the share once, here rather than on C4D's thread
                entries = lib.scan()
                inst = sources.SOURCES["installed"]
                inst.library, inst._all = lib, entries
                self.results.put(("scanned", entries, None))
            except Exception as exc:
                self.results.put(("scanned", [], message(exc)))
        self._run(run)

    # ---- thumbnails --------------------------------------------------------------------
    def forget_thumbs(self):
        self._thumb_seen = set()

    def forget_thumb(self, key):
        self._thumb_seen.discard(key)

    def thumb(self, asset):
        """Make sure asset's thumbnail is on disk; posts ("thumb", key, path) when it is.
        Cheap: the grid calls it for every tile it draws. The newest requests are
        served first, so the tiles in view load before ones scrolled past."""
        if asset.key in self._thumb_seen:
            return
        self._thumb_seen.add(asset.key)
        self._thumbs.put(asset)

    def _find_thumb(self, asset):
        """(path on disk, None) or (None, (url, path to download to)) or (None, None).
        Worker thread: the library may be a slow share."""
        if asset.source == "installed":
            return self._installed_thumb(asset)
        if not asset.thumb_url:
            return None, None
        path = self.library.thumb_path(asset)
        if os.path.isfile(path) and image_kind(path) in ("png", "jpg"):
            return path, None
        return None, (asset.thumb_url, path)      # missing, or a WebP C4D can't read

    def _installed_thumb(self, asset):
        e = asset.extra["entry"]
        candidates = [e.get("thumb"),
                      os.path.join(local_cache_dir(), "thumbs", "installed", _safe(asset.key) + ".png")]
        if e.get("managed"):
            label = sources.SOURCES[e["source"]].label if e["source"] in sources.SOURCES else e["source"]
            key = re.sub(r"[^A-Za-z0-9_\-]+", "_", e["id"])
            for base in (os.path.join(self.library.root, "_thumbs"), os.path.join(local_cache_dir(), "thumbs")):
                candidates += [os.path.join(base, label, key + ext) for ext in (".png", ".jpg")]
        for c in candidates:
            if c and os.path.isfile(c) and image_kind(c) in ("png", "jpg"):
                return c, None
        if e.get("thumb_url"):
            url = e["thumb_url"]
            if "polyhaven" in url:
                url = sources.PolyHaven.png_thumb(url, e["id"])
            path = os.path.join(local_cache_dir(), "thumbs", "installed", _safe(asset.id) + ".png")
            if os.path.isfile(path) and image_kind(path) in ("png", "jpg"):
                return path, None
            return None, (url, path)
        if e.get("maps", {}).get("color"):
            self.results.put(("thumb_make", asset.key, e["maps"]["color"]))
        return None, None

    def _thumb_loop(self):
        while True:
            asset = self._thumbs.get()
            key = asset.key
            try:
                found, fetch = self._find_thumb(asset)
                if found:
                    self.results.put(("thumb", key, found))
                    continue
                if not fetch:
                    continue
                url, path = fetch
                if os.path.isfile(path):
                    os.remove(path)
                net.download(url, path, headers=sources.THUMB_HEADERS)
                if image_kind(path) in ("png", "jpg"):
                    self.results.put(("thumb", key, path))
                else:
                    os.remove(path)     # a format Cinema 4D can't read; leave the placeholder
            except Exception:
                pass

    # ---- downloads ------------------------------------------------------------------------
    def download(self, asset, res, payload=None):
        """Queue asset for download; payload comes back with the result (what to do with it)."""
        if asset.key in self.pending:
            return False
        self.pending.append(asset.key)
        self._downloads.put((asset, res, payload))
        return True

    def cancel_all(self):
        """Drop everything queued and stop the current download."""
        while True:
            try:
                self._downloads.get_nowait()
            except queue.Empty:
                break
        self.pending = [k for k in self.pending if k == self.busy_key]
        self._cancel.set()

    def _download_loop(self):
        while True:
            asset, res, payload = self._downloads.get()
            self._cancel.clear()
            self.busy_key = asset.key
            key = asset.key
            try:
                folder, maps, reused = sources.fetch(
                    self.library, asset, res,
                    lambda f: self.results.put(("progress", key, max(0.0, min(1.0, f)))),
                    self._cancel,
                    lambda holder: self.results.put(("waiting", key, holder)))
                self.results.put(("done", key, folder, maps, reused, asset, payload))
            except net.Cancelled:
                self.results.put(("error", key, "cancelled"))
            except Exception as exc:
                traceback.print_exc()
                self.results.put(("error", key, message(exc)))
            finally:
                self.busy_key = None
                if key in self.pending:
                    self.pending.remove(key)


def _safe(s):
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", s)


def message(exc):
    text = str(exc) or type(exc).__name__
    if isinstance(exc, net.urllib.error.HTTPError):
        text = "HTTP %d from %s" % (exc.code, exc.url.split("/")[2] if exc.url else "server")
    elif isinstance(exc, net.urllib.error.URLError):
        text = "can't reach the server (%s)" % exc.reason
    return text
