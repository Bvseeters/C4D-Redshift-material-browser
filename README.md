# BasPBR Browser

A Cinema 4D plugin for browsing free PBR materials and Sketchfab models, and bringing them in with **Redshift node materials** in a double-click.

Sources: **ambientCG**, **Poly Haven**, **ShareTextures** (all CC0), **AMD GPUOpen** (free, commercial use allowed) and **FreePBR** (non-commercial use only). The **Everything** tab searches all of them at once — tick the sites you want in the sidebar (Ctrl-click: just that one); categories with the same name are merged.

## Install

1. Download `BasPBRBrowser.zip` from [Releases](../../releases/latest).
2. Unzip it into a Cinema 4D plugins folder, for example `%APPDATA%\Maxon\Maxon Cinema 4D <version>\plugins\`.
3. Restart Cinema 4D and open **Extensions ▸ BasPBR Browser**.

Requires Cinema 4D 2023 or later with Redshift.

## Use

- **Find materials:** pick a site tab or **Everything**, then search or click a category on the left. **Up / Down** step through the categories. Thumbnails load as you scroll.
- **Resize thumbnails:** **Ctrl + mouse wheel**, or the **Size** slider.
- **Pick a resolution:** the **Resolution** menu lists only the sizes that site has. **Highest**, the default, downloads each material at the largest size it comes in: up to 16K on ambientCG and Poly Haven, 8K on AMD and 4K on ShareTextures.
- **Make a material:** double-click a thumbnail. It downloads and is assigned to the selected objects.
  - The material is a Standard Material with colour, roughness, metalness, normal/bump, opacity and emission.
  - AMD materials are built from their MaterialX file: the right channel of a packed map, its remap, constant metalness or colour, and the authored tiling.
  - Displacement is on by default.
- **More actions:** right-click a thumbnail to open its web page or show it in the library folder.

## Sketchfab models

The **Sketchfab** tab searches Sketchfab's downloadable models.
- **Badges:** each tile shows its licence (**CC0**, **CREDIT** = name the author, **NON-COMM** = not for commercial work) and its face count.
- **Filters:** **Max faces**, *Commercial use only* and *Staff picks only*.
- **Paste link:** found a model on sketchfab.com? Copy its address and press **Paste link**: it's downloaded and imported straight away.
- **Import:** a model comes in as objects under one Null, with Redshift materials (colour, roughness, metalness, normal, emission, opacity).
- **Models folder:** models go to their own folder, which is the **Library** row while the Sketchfab tab is open. Each model keeps its textures, an `ATTRIBUTION.txt` with author and licence, and is listed under **Installed**.
- **API token:** downloading needs your free Sketchfab API token, from sketchfab.com/settings/password. You're asked for it the first time; change it under **Help ▸ Sketchfab API token**.

## Shared library

Downloads go to the **Library** folder. Point every PC at the same network folder, preferably the `\\server\share\…` path.

- Each material is downloaded only once, by whoever needs it first.
- The **Installed** tab builds anything already in the library instantly.

## Updates

The first time the browser opens after Cinema 4D starts, it checks this repository's `main` branch. If there's a newer version it offers to install it; restart Cinema 4D afterwards. **Help ▸ Check for updates** checks on demand.

While this repository is private, every PC needs a read-only GitHub token, set under **Help ▸ GitHub token**.

## Releasing

1. Raise `VERSION` and add notes in `plugins/BasPBRBrowser/baspbr/version.py`.
2. Run `python tests/smoke_test.py`, `python tests/sketchfab_test.py` and `python tests/updater_test.py`.
3. Run `python build.py`, which writes `dist/BasPBRBrowser.zip`.
4. Push, then run `python release.py . <branch>`. It publishes the zip as a GitHub release, using git's GitHub sign-in.
5. Merge into `main`. Every PC is then offered the update.
