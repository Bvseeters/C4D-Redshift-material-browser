"""BasPBR Browser version and where updates come from.

To release an update: raise VERSION, add a line or two under NOTES, commit and
push to BRANCH. Every PC with the plugin offers it the next time someone opens
the browser after starting Cinema 4D.
"""
VERSION = "3.0.0"

REPO = "Bvseeters/C4D-Redshift-material-browser"
BRANCH = "main"
PLUGIN_DIR = "plugins/BasPBRBrowser"        # the plugin's folder inside the repository

NOTES = {
    "3.0.0": [
        "Endless scrolling instead of pages",
        "Ctrl + mouse wheel (or the Size slider) resizes thumbnails",
        "ShareTextures added",
        "Everything tab: all sites in one search, categories merged by name",
        "Catalogues cached for 12 hours, so tabs open instantly",
        "Updates from GitHub, offered when the browser first opens",
    ],
    "2.0.0": [
        "Library folder can be a network share; Installed tab; category sidebar",
    ],
}
