"""Zip the plugin for a release: dist/BasPBRBrowser.zip holds the BasPBRBrowser
folder, ready to unzip into a Cinema 4D plugins folder.

    python build.py
"""
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, "plugins", "BasPBRBrowser")
sys.path.insert(0, PLUGIN)

from baspbr import version  # noqa: E402

out = os.path.join(HERE, "dist", "BasPBRBrowser.zip")
os.makedirs(os.path.dirname(out), exist_ok=True)
count = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(PLUGIN):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for fn in sorted(files):
            if fn.endswith(".pyc"):
                continue
            path = os.path.join(root, fn)
            z.write(path, os.path.join("BasPBRBrowser", os.path.relpath(path, PLUGIN)))
            count += 1
print("BasPBR Browser %s: %s (%d files, %d KB)" % (version.VERSION, out, count, os.path.getsize(out) // 1024))
