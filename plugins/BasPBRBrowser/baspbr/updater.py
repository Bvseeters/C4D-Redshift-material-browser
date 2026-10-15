"""Updates from GitHub. Plain Python.

check()   reads version.py on the repository's branch and returns what's new
          when its VERSION is higher than the installed one.
install() downloads the branch as a zip, takes the plugin folder out of it,
          backs the installed copy up (outside Cinema 4D's plugin folders, so
          the backup never loads as a second plugin) and replaces it.
          Cinema 4D has to be restarted to run the new code.

A public repository needs nothing. A private one needs a GitHub token with
read access to it (Help > GitHub token…); without one GitHub answers
"not found".

The installed copy is never overwritten when it sits inside a git working
folder - that's a developer's checkout, where pulling is the way to update.
"""
import ast
import os
import shutil
import tempfile
import time
import zipfile

from . import net, version
from .library import local_cache_dir

CHECKED_THIS_SESSION = False       # the automatic check runs once per Cinema 4D session

RAW_URL = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"
API_FILE = "https://api.github.com/repos/{repo}/contents/{path}?ref={branch}"
ZIP_URL = "https://codeload.github.com/{repo}/zip/refs/heads/{branch}"
API_ZIP = "https://api.github.com/repos/{repo}/zipball/{branch}"


def plugin_dir():
    """The installed plugin folder (the one holding BasPBRBrowser.pyp)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def as_tuple(v):
    out = []
    for part in str(v).split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out + [0] * (3 - len(out)))


def _auth(token):
    return {"Authorization": "Bearer " + token, "X-GitHub-Api-Version": "2022-11-28"} if token else {}


def read_version_file(text):
    """VERSION and NOTES from version.py's source, without running it."""
    found = {}
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ("VERSION", "NOTES", "BRANCH", "REPO"):
                try:
                    found[name] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    if "VERSION" not in found:
        raise RuntimeError("no VERSION in the repository's version.py")
    return found


def remote_version(token=None, repo=None, branch=None):
    repo, branch = repo or version.REPO, branch or version.BRANCH
    path = version.PLUGIN_DIR + "/baspbr/version.py"
    try:
        text = net.get_text(RAW_URL.format(repo=repo, branch=branch, path=path))
    except net.urllib.error.HTTPError as exc:
        if exc.code != 404 or not token:
            if exc.code == 404:
                raise RuntimeError("GitHub says %s doesn't exist - is the repository private? "
                                   "Then set a GitHub token (Help menu)." % repo)
            raise
        text = net.get_text(API_FILE.format(repo=repo, branch=branch, path=path),
                            headers=dict(_auth(token), Accept="application/vnd.github.raw"))
    return read_version_file(text)


def check(token=None):
    """None when up to date, else {"version", "current", "notes"}."""
    remote = remote_version(token)
    new, cur = remote["VERSION"], version.VERSION
    if as_tuple(new) <= as_tuple(cur):
        return None
    notes = []
    for v in sorted((remote.get("NOTES") or {}).keys(), key=as_tuple, reverse=True):
        if as_tuple(v) > as_tuple(cur):
            notes += ["%s: %s" % (v, line) for line in remote["NOTES"][v]]
    return {"version": new, "current": cur, "notes": notes}


def git_checkout(path):
    """True when path lies inside a git working folder (a developer's copy)."""
    p = os.path.abspath(path)
    for _ in range(6):
        if os.path.exists(os.path.join(p, ".git")):
            return True
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return False


def install(token=None, target=None, repo=None, branch=None, progress=lambda f: None):
    """Download the branch and replace the installed plugin. Returns the new version."""
    target = target or plugin_dir()
    repo, branch = repo or version.REPO, branch or version.BRANCH
    if git_checkout(target):
        raise RuntimeError("this copy of the plugin lives in a git working folder (%s) - "
                           "update it with git pull instead" % target)
    work = tempfile.mkdtemp(prefix="baspbr_update_")
    try:
        zpath = os.path.join(work, "update.zip")
        try:
            net.download(ZIP_URL.format(repo=repo, branch=branch), zpath, lambda d, t: progress(d / float(t or 3e5)))
        except net.urllib.error.HTTPError as exc:
            if exc.code != 404 or not token:
                raise
            net.download(API_ZIP.format(repo=repo, branch=branch), zpath, headers=_auth(token))
        if not zipfile.is_zipfile(zpath):
            raise RuntimeError("GitHub didn't send a zip")

        # find <anything>/plugins/BasPBRBrowser/ inside the archive
        marker = "/" + version.PLUGIN_DIR.strip("/") + "/"
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
            prefix = next((n[:n.index(marker) + len(marker)] for n in names if marker in "/" + n), None)
            if prefix is None:
                raise RuntimeError("the repository has no %s folder" % version.PLUGIN_DIR)
            new_dir = os.path.join(work, "new")
            for n in names:
                if not n.startswith(prefix) or n.endswith("/") or "__pycache__" in n:
                    continue
                dst = os.path.join(new_dir, *n[len(prefix):].split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with z.open(n) as src, open(dst, "wb") as f:
                    shutil.copyfileobj(src, f)
        pyp = [f for f in os.listdir(new_dir) if f.endswith(".pyp")]
        vfile = os.path.join(new_dir, "baspbr", "version.py")
        if not pyp or not os.path.isfile(vfile):
            raise RuntimeError("the downloaded plugin looks incomplete - nothing was changed")
        with open(vfile, "r", encoding="utf-8") as f:
            new_version = read_version_file(f.read())["VERSION"]

        # back up what's installed, outside every plugin folder
        backup = os.path.join(local_cache_dir(), "backups", "BasPBRBrowser-%s-%s" % (
            version.VERSION, time.strftime("%Y%m%d-%H%M%S")))
        shutil.copytree(target, backup, ignore=shutil.ignore_patterns("__pycache__"))

        # replace: write every new file, then drop files the new version no longer has
        keep = set()
        for root, dirs, files in os.walk(new_dir):
            for fn in files:
                src = os.path.join(root, fn)
                rel = os.path.relpath(src, new_dir)
                keep.add(os.path.normcase(rel))
                dst = os.path.join(target, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fn in files:
                path = os.path.join(root, fn)
                if os.path.normcase(os.path.relpath(path, target)) not in keep:
                    os.remove(path)
        return new_version
    finally:
        shutil.rmtree(work, ignore_errors=True)
