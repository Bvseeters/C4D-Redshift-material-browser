"""BasPBR Browser version and where updates come from.

To release an update: raise VERSION, add a line or two under NOTES, commit and
push to BRANCH. Every PC with the plugin offers it the next time someone opens
the browser after starting Cinema 4D.
"""
VERSION = "3.4.0"

REPO = "Bvseeters/C4D-Redshift-material-browser"
BRANCH = "main"
PLUGIN_DIR = "plugins/BasPBRBrowser"        # the plugin's folder inside the repository

NOTES = {
    "3.4.0": [
        "Sketchfab tab: search Sketchfab's downloadable models, with licence badges (CC0 / CREDIT / NON-COMM) and face counts",
        "Paste link: copy a model's address in your browser, press Paste link - it's downloaded and imported",
        "Models come in as objects with Redshift materials (colour, roughness, metalness, normal, emission, opacity)",
        "Sketchfab models have their own folder (Library row on the Sketchfab tab), separate from the materials",
        "Each model keeps ATTRIBUTION.txt with its author and licence; non-commercial models ask first",
        "Your Sketchfab API token: Help > Sketchfab API token (asked for the first time you import)",
    ],
    "3.3.0": [
        "AMD GPUOpen materials follow their MaterialX file instead of guessing from file names: "
        "roughness comes from the right channel of the mask map, remapped as AMD set it",
        "AMD metals get their metalness, and materials without a colour map get their colour",
        "AMD materials keep their authored tiling (the Texture nodes' Scale)",
        "AMD materials downloaded by an older version are downloaded again the next time you use them",
    ],
    "3.2.0": [
        "Resolution menu shows only the sizes a site really has; Highest (the default) gets each material's largest",
        "Materials download at - and are named after - the size they really come in (no more '8K' labels on 4K maps)",
        "AMD GPUOpen materials up to 8K; ambientCG up to 16K; Poly Haven up to 16K",
        "Up / Down arrow keys step through the categories",
        "The thumbnail grid scrolls itself: mouse wheel, Ctrl + wheel for the size, scroll bar, Page Up / Down, Home / End",
        "Displacement is on by default",
    ],
    "3.1.0": [
        "AMD GPUOpen MaterialX Library added (454 materials, free for commercial use)",
        "Everything tab: tick the sites to search - Ctrl-click searches just one",
        "Packed ORM maps (occlusion / roughness / metalness) are wired through a Color Splitter",
    ],
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
