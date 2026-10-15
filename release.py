"""Create (or update) a GitHub release for the current version and attach dist/BasPBRBrowser.zip.
Auth: the GitHub credential git already has (Git Credential Manager). Never prints it.

    python release.py . <branch>      (after build.py and git push)
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

repo_dir, target = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(repo_dir, "plugins", "BasPBRBrowser"))
from baspbr import version  # noqa: E402

cred = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                      capture_output=True, text=True, cwd=repo_dir, timeout=120).stdout
token = dict(l.split("=", 1) for l in cred.splitlines() if "=" in l).get("password")
if not token:
    sys.exit("no GitHub credential available")
sha = subprocess.run(["git", "rev-parse", target], capture_output=True, text=True, cwd=repo_dir).stdout.strip()
H = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "BasPBRBrowser-release"}
API = "https://api.github.com/repos/" + version.REPO


def call(method, url, body=None, headers=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method, headers=dict(H, **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            text = r.read()
            return r.status, (json.loads(text) if text else None)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


tag = "v" + version.VERSION
notes = "\n".join("- " + n for n in version.NOTES.get(version.VERSION, []))
body = ("%s\n\n**Install:** unzip `BasPBRBrowser.zip` into a Cinema 4D plugins folder "
        "(delete an older `BasPBRBrowser` folder first) and restart Cinema 4D." % notes)
status, rel = call("GET", API + "/releases/tags/" + tag)
if status == 200:
    print("release %s exists, updating" % tag)
    status, rel = call("PATCH", API + "/releases/%d" % rel["id"], {"body": body, "name": "BasPBR Browser " + version.VERSION})
else:
    status, rel = call("POST", API + "/releases", {"tag_name": tag, "target_commitish": sha,
                                                   "name": "BasPBR Browser " + version.VERSION,
                                                   "body": body, "draft": False, "prerelease": False})
if status not in (200, 201):
    sys.exit("release failed: HTTP %s %s" % (status, rel))
for a in rel.get("assets", []):
    if a["name"] == "BasPBRBrowser.zip":
        call("DELETE", API + "/releases/assets/%d" % a["id"])
zpath = os.path.join(repo_dir, "dist", "BasPBRBrowser.zip")
with open(zpath, "rb") as f:
    data = f.read()
upload = rel["upload_url"].split("{")[0] + "?name=BasPBRBrowser.zip"
status, asset = call("POST", upload, raw=data, headers={"Content-Type": "application/zip"})
if status not in (200, 201):
    sys.exit("upload failed: HTTP %s %s" % (status, asset))
print("release:", rel["html_url"])
print("asset:  ", asset["browser_download_url"], asset["size"], "bytes")
print("tag %s -> %s" % (tag, sha[:7]))
