# BasPBR Browser for Cinema 4D (Redshift)

Port of the WhateverWall Unity material browser to Cinema 4D. Browse **ambientCG**,
**Poly Haven** and **FreePBR** inside C4D, then double-click a material to download
it and get a ready **Redshift node material**:

- Standard Material with colour, roughness (or glossiness), metalness, opacity and emission;
- normal map through a Bump Map node (DirectX normals are flipped automatically);
- optional displacement.

It can be assigned to the selected objects straight away.

Downloads go into a library folder, by default `Documents\Redshift Material Library`.
Point it at a **network share** and the whole team shares one library:
- maps are named one way, whatever the source called them;
- a material is only ever downloaded once, by anyone;
- the **Installed** tab builds any material in the library instantly, without downloading.

## Install

Upgrading from the old *Material Browser (Redshift)*: delete the old `MaterialBrowserRS` plugin folder
first. Both use the same plugin ID, so only one can be installed. Your settings, including the library
folder, carry over.


1. Copy `plugins/BasPBRBrowser` into a Cinema 4D plugins folder. Either:
   - `%APPDATA%\Maxon\Maxon Cinema 4D <version>\plugins\`, or
   - any folder listed under Preferences ▸ Plugins ▸ Search Paths.
2. Restart Cinema 4D. Open it from **Extensions ▸ BasPBR Browser**. It's dockable.
3. The first time: open the window's **Library ▸ Check Redshift node IDs**. It prints to the
   Console whether every Redshift node and port the plugin uses exists in your Redshift
   version. "all ids resolve" means you're good.

Requires Cinema 4D 2023 or later with Redshift. Tested target: C4D 2024–2026 (Python 3.11).

## Using it

| | |
|---|---|
| Tabs | ambientCG (CC0), Poly Haven (CC0), FreePBR (free for **non-commercial** use; 2K only), **Installed** (the library folder) |
| Categories (left) | Each source's own categories, from its API, with counts. ambientCG filters on its server; Poly Haven and FreePBR filter locally. Installed groups by source (ambientCG, Poly Haven, FreePBR, Local) and by category. |
| Search | Searches as you type, within the chosen category. ambientCG searches on its server; Poly Haven and FreePBR load their catalogue once and filter locally. |
| Library | The folder materials download to. Type a path, or use **…** to pick one. **Open** shows it in Explorer. **Rescan** picks up what other PCs added. |
| Sort / Resolution | 1K–8K. If a material doesn't come in the size you ask for, you get the largest smaller one. |
| Click / Ctrl-click | Select one / several |
| Double-click, or **Create material** | Download (queued, in the background) and build the material |
| Right-click | Create, open the web page, show in the library folder |
| Assign to selected objects | Adds a UVW texture tag to whatever was selected when you asked |
| Bump from height | If a material has no normal map, its height map drives the bump instead |
| Displacement | Adds a Displacement node. It only renders once displacement is enabled in the object's **Redshift Object** tag. |

Tile markers:
- green corner: already in the library;
- orange bar: downloading;
- blue bar: another PC is downloading it right now;
- red frame: it failed.

## A shared library on the network

1. On one PC, set **Library** to a folder on the server. Prefer the network path
   (`\\server\share\Materials`). If you pick a mapped drive letter, the plugin offers to
   switch to its network path. Texture paths are written into the materials, so the
   network path keeps them working on PCs that map the drive to another letter, or not at all.
2. On every other PC, set **Library** to the same folder. The **Installed** tab now shows
   everything anyone has downloaded, and builds it with no download.

How sharing stays safe:

| | |
|---|---|
| Half-finished downloads | A material is assembled in `<library>\_incoming`, on the server itself, then renamed into place in one step. Nobody ever sees half a material. |
| Same material, same time | A lock file in `_incoming` says who's downloading. A second PC waits for that download and uses it (blue bar, "X is downloading … right now"). A lock older than 15 minutes, from a crashed PC, is taken over. |
| Different mount paths | `asset.json` stores file names only. Each PC finds the maps through its own path to the folder. |
| Read-only for some users | They can browse and build everything in the library. Downloads need write access. Their thumbnails are cached locally instead. |
| Your own folders | Any folder in the library with a colour map plus at least one other recognisable map shows up under **Installed ▸ Local**. Its thumbnail is drawn from the colour map. |

Every downloaded material keeps an `asset.json`: source, ID, name, resolution, licence,
categories, who downloaded it and when. It also keeps a `preview.png`, which the Installed
tab uses as its thumbnail.

## How it's built

```
plugins/BasPBRBrowser/
  BasPBRBrowser.pyp   registers the command (Extensions menu)
  baspbr/net.py             HTTP, cookies, curl fallback if Python's SSL has no certificates
  baspbr/sources.py         ambientCG / Poly Haven / FreePBR (+ categories) and Installed
  baspbr/library.py         library folders, map-role detection, asset.json, staging, locks, scan
  baspbr/jobs.py            worker threads: searches, categories, scans, thumbnails, downloads
  baspbr/rsmat.py           Redshift node graph (by node/port ID, not UI labels)
  baspbr/ui.py              the dialog and the thumbnail grid
  baspbr/settings.py        settings in C4D's prefs folder
tests/
  smoke_test.py           end-to-end run on fake c4d/maxon modules, live downloads
  fakes/                  the fake modules
```

`python tests/smoke_test.py` runs the real plugin code against fake `c4d` and `maxon`
modules. It searches each source live, double-clicks a tile, downloads the material and
checks the Redshift graph that gets built. It can't stand in for Cinema 4D itself:
- drawing and layout in the real UI;
- Redshift's actual port IDs. That's what the in-app ID check is for.

### Redshift IDs used

| Node | ID | Ports |
|---|---|---|
| Output | `com.redshift3d.redshift4c4d.node.output` | `.surface`, `.displacement` |
| Standard Material | `….nodes.core.standardmaterial` | `base_color`, `refl_roughness`, `refl_isglossiness`, `metalness`, `opacity_color`, `emission_color`, `emission_weight`, `bump_input`, out `outcolor` |
| Texture | `….nodes.core.texturesampler` | `tex0` → `path`, `colorspace` (`RS_INPUT_COLORSPACE_RAW` for data maps), out `outcolor` |
| Bump Map | `….nodes.core.bumpmap` | `input`, `inputtype` (1 = tangent-space normal, 0 = height), `scale`, `flipy`, out `out` |
| Displacement | `….nodes.core.displacement` | `texmap`, `scale`, out `out` |

The Output, Standard Material and Texture IDs come from Maxon's SDK examples and the
Custom_Redshift_API library. The Bump Map and Displacement port names follow Redshift's
core parameter names. Ports are also found by name suffix, and a missing port is skipped
with a Console note rather than failing the whole material.

## Plugin ID

`1000005` comes from Maxon's range for plugins in development. That's fine in-house. Before
sharing the plugin, register a free permanent ID at developers.maxon.net and change
`PLUGIN_ID` in `BasPBRBrowser.pyp`.

## Licences

ambientCG and Poly Haven assets are CC0. FreePBR's free downloads are for non-commercial
use; commercial use needs their paid licence, and the window says so when FreePBR is
selected. Each material's `asset.json` records where it came from and its licence.
