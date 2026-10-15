"""Browser settings, kept in a small JSON file in Cinema 4D's preferences folder."""
import json
import os

DEFAULTS = {
    "library": os.path.join(os.path.expanduser("~"), "Documents", "Redshift Material Library"),
    "source": "everything",
    "query": "",
    "sort": {},                 # per source
    "resolution": "4K",
    "assign": True,
    "displacement": False,
    "displacement_scale": 1.0,
    "bump_from_height": True,
    "category": {},             # per tab
    "sites_off": [],            # sites left out of the Everything tab
    "tile": 128,                # thumbnail size in pixels (Ctrl + mouse wheel)
    "github_token": "",         # only while the GitHub repository is private
}


FILE = "baspbr_browser.json"
LEGACY = "material_browser_rs.json"       # the plugin's name before it became BasPBR Browser


def _path(name=FILE):
    try:
        import c4d
        folder = c4d.storage.GeGetC4DPath(c4d.C4D_PATH_PREFS)
    except Exception:
        folder = os.path.expanduser("~")
    return os.path.join(folder, name)


def load():
    data = dict(DEFAULTS)
    for path in (_path(), _path(LEGACY)):    # older settings carry over on first run
        try:
            with open(path, "r", encoding="utf-8") as f:
                data.update(json.load(f))
            break
        except Exception:
            continue
    return data


def save(data):
    try:
        with open(_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except Exception as exc:
        print("[BasPBR Browser] couldn't save settings: %s" % exc)
