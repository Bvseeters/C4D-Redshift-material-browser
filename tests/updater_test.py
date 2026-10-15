"""The GitHub updater against a fake GitHub on localhost (no network).

  * check(): newer version found, only the newer release notes listed; same
    version = nothing; a private repository without a token says so; with a
    token it reads through the API with the token
  * install(): replaces the installed plugin from the branch zip, removes files
    the new version dropped, backs the old one up; refuses an incomplete zip and
    a git working folder, leaving everything as it was

    python tests/updater_test.py
"""
import http.server
import io
import os
import shutil
import sys
import tempfile
import threading
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "..", "plugins", "BasPBRBrowser")
sys.path.insert(0, PLUGIN)

from baspbr import updater, version  # noqa: E402

failures = []


def check(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        failures.append(what)


def version_py(v):
    return ('VERSION = "%s"\nREPO = "%s"\nBRANCH = "main"\nPLUGIN_DIR = "plugins/BasPBRBrowser"\n'
            'NOTES = {"%s": ["shiny new thing"], "%s": ["the current one"], "1.0.0": ["ancient"]}\n'
            % (v, version.REPO, v, version.VERSION))


def branch_zip(v, complete=True):
    """A zip shaped like GitHub's: <repo>-main/plugins/BasPBRBrowser/..."""
    buf = io.BytesIO()
    root = "C4D-Redshift-material-browser-main/"
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(root + "README.md", "readme")
        for dirpath, dirs, files in os.walk(PLUGIN):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fn in files:
                rel = os.path.relpath(os.path.join(dirpath, fn), PLUGIN).replace("\\", "/")
                if rel == "baspbr/version.py":
                    z.writestr(root + "plugins/BasPBRBrowser/" + rel, version_py(v))
                elif rel.endswith(".pyp") and not complete:
                    continue
                else:
                    z.write(os.path.join(dirpath, fn), root + "plugins/BasPBRBrowser/" + rel)
        z.writestr(root + "plugins/BasPBRBrowser/baspbr/brand_new.py", "NEW = True\n")
    return buf.getvalue()


MAJ, MIN, PAT = updater.as_tuple(version.VERSION)[:3]
NEWER = "%d.%d.0" % (MAJ, MIN + 1)                 # what "GitHub" has
NEWER_PATCH = "%d.%d.%d" % (MAJ, MIN, PAT + 10)     # e.g. 3.1.10 vs 3.1.9: numbers, not text
OLDER = "%d.%d.%d" % (MAJ, MIN - 1, 99) if MIN else "%d.0.0" % (MAJ - 1)

SITE = {"private": False, "no_release": False, "version": NEWER, "zip": b"", "auth_seen": []}


class FakeGitHub(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        if auth:
            SITE["auth_seen"].append((self.path.split("/")[1], auth))
        if self.path.startswith("/api/repo/"):
            return self.send(404) if SITE["private"] else self.send(200, b'{"name": "repo"}', "application/json")
        if self.path.startswith("/raw/"):
            if SITE["private"] or SITE["no_release"]:
                return self.send(404, b"404: Not Found")
            return self.send(200, version_py(SITE["version"]).encode())
        if self.path.startswith("/api/contents/"):
            if auth != "Bearer test-token" or self.headers.get("Accept") != "application/vnd.github.raw":
                return self.send(404)
            return self.send(200, version_py(SITE["version"]).encode())
        if self.path.startswith("/zip/"):
            if SITE["private"]:
                return self.send(404)
            return self.send(200, SITE["zip"], "application/zip")
        if self.path.startswith("/api/zipball/"):
            if auth != "Bearer test-token":
                return self.send(404)
            return self.send(200, SITE["zip"], "application/zip")
        self.send(404)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeGitHub)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % server.server_address[1]
updater.RAW_URL = base + "/raw/{repo}/{branch}/{path}"
updater.API_FILE = base + "/api/contents/{repo}/{path}?ref={branch}"
updater.ZIP_URL = base + "/zip/{repo}/{branch}"
updater.API_ZIP = base + "/api/zipball/{repo}/{branch}"
updater.API_REPO = base + "/api/repo/{repo}"
CACHE = tempfile.mkdtemp(prefix="baspbr_upd_cache_")
updater.local_cache_dir = lambda: CACHE

print("check:")
info = updater.check()
check(info and info["version"] == NEWER and info["current"] == version.VERSION, "%s on GitHub is offered: %s" % (NEWER, info))
check(info and info["notes"] == ["%s: shiny new thing" % NEWER], "only notes newer than %s" % version.VERSION)
SITE["version"] = version.VERSION
check(updater.check() is None, "same version: nothing to do")
SITE["version"] = OLDER
check(updater.check() is None, "older version on GitHub: nothing to do")
SITE["version"] = NEWER_PATCH
check(updater.as_tuple("3.0.10") > updater.as_tuple("3.0.9") and updater.check() is not None,
      "%s counts as newer than %s" % (NEWER_PATCH, version.VERSION))

SITE["no_release"] = True
try:
    updater.check()
    check(False, "public repository, nothing on the branch yet: fails")
except RuntimeError as exc:
    check("no BasPBR Browser on it yet" in str(exc) and "private" not in str(exc),
          "public repository, nothing on the branch yet: '%s'" % exc)
SITE["no_release"] = False

SITE["private"], SITE["version"] = True, NEWER
try:
    updater.check()
    check(False, "private repository without token fails")
except RuntimeError as exc:
    check("private" in str(exc) and "token" in str(exc), "private repository without token: '%s'" % exc)
info = updater.check("test-token")
check(info and info["version"] == NEWER, "with a token it reads through the API")
check(("api", "Bearer test-token") in SITE["auth_seen"], "and sends the token only there: %s" % sorted({s for s, _ in SITE["auth_seen"]}))
SITE["private"] = False

print("\ninstall:")
installed = os.path.join(tempfile.mkdtemp(prefix="baspbr_upd_plugins_"), "BasPBRBrowser")
shutil.copytree(PLUGIN, installed, ignore=shutil.ignore_patterns("__pycache__"))
with open(os.path.join(installed, "baspbr", "dropped_in_new_version.py"), "w") as f:
    f.write("OLD = True\n")
os.makedirs(os.path.join(installed, "baspbr", "__pycache__"), exist_ok=True)
with open(os.path.join(installed, "baspbr", "__pycache__", "ui.cpython-311.pyc"), "wb") as f:
    f.write(b"x")

SITE["zip"] = branch_zip(NEWER, complete=False)
try:
    updater.install(target=installed)
    check(False, "an incomplete download is refused")
except RuntimeError as exc:
    check("incomplete" in str(exc), "an incomplete download is refused: '%s'" % exc)
check(os.path.isfile(os.path.join(installed, "baspbr", "dropped_in_new_version.py")) and
      not os.path.exists(os.path.join(CACHE, "backups")), "and nothing was touched")

SITE["zip"] = branch_zip(NEWER)
seen = []
new = updater.install(target=installed, progress=seen.append)
check(new == NEWER, "installed %s" % new)
with open(os.path.join(installed, "baspbr", "version.py")) as f:
    check('VERSION = "%s"' % NEWER in f.read(), "version.py is the new one")
check(os.path.isfile(os.path.join(installed, "baspbr", "brand_new.py")), "new files arrive")
check(not os.path.exists(os.path.join(installed, "baspbr", "dropped_in_new_version.py")), "files the new version dropped are removed")
check(os.path.isfile(os.path.join(installed, "BasPBRBrowser.pyp")) and os.path.isfile(os.path.join(installed, "res", "icon.png")),
      "the plugin is complete (.pyp, icon)")
check(not os.path.exists(os.path.join(installed, "README.md")), "nothing from outside the plugin folder")
backups = os.listdir(os.path.join(CACHE, "backups"))
check(len(backups) == 1 and backups[0].startswith("BasPBRBrowser-%s-" % version.VERSION), "backup: %s" % backups)
check(backups and os.path.isfile(os.path.join(CACHE, "backups", backups[0], "baspbr", "dropped_in_new_version.py")),
      "the backup is the old version as it was")
check(seen and max(seen) > 0, "progress reported")

print("\ngit working folder:")
try:
    updater.install(target=PLUGIN)
    check(False, "refuses to overwrite the git checkout")
except RuntimeError as exc:
    check("git" in str(exc), "refuses to overwrite the git checkout: '%s'" % exc)
check(len(os.listdir(os.path.join(CACHE, "backups"))) == 1, "and made no backup")

server.shutdown()
shutil.rmtree(CACHE, ignore_errors=True)
shutil.rmtree(os.path.dirname(installed), ignore_errors=True)
print("\n%s" % ("ALL PASSED" if not failures else "%d FAILED:\n  %s" % (len(failures), "\n  ".join(failures))))
sys.exit(1 if failures else 0)
