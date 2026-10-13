# Midnight Club 3 City Importer for Blender

A Blender add-on that imports **Midnight Club 3: DUB Edition Remix (PS2)** city maps directly into your scene, with placed models, UVs, textures, baked vertex colours and glowing windows.

Point it at your game ISO, `ASSETS.DAT`, or an extracted city `.pck`, pick a city, and click **Import city**.

> **No game data is included.** You need to supply your own copy of the game.

## Features

- Reads cities straight from a **game ISO**, an **`ASSETS.DAT`** ("Dave" archive), or an extracted `resources/city/<city>.pck`
- Built-in ISO 9660 reader, so there's no need to extract anything first
- Scans the archive and lists every city with its size, so you can pick one from a list
- Decodes the PS2 VIF geometry (positions, UVs, triangles) and places every model using the city's object graph
- Optional **full-size textures**: city files only contain 16x16 thumbnails, so the add-on can match them against the full images in `TEXTURE.DAT` (or the ISO)
- Baked per-vertex colour tinting, with adjustable gain for the PS2 "modulate x2" behaviour
- Emissive lit windows with adjustable strength
- Two import modes: separate objects (repeated models share mesh data) or one merged mesh
- Y-up to Z-up conversion and a scale multiplier
- Optional LOD variants and unreachable meshes
- Available from both the **3D Viewport sidebar** and **File > Import**

## Requirements

- Blender **2.93 or newer** (developed for modern Blender; tested versions are listed under [Status](#status))
- Your own copy of Midnight Club 3: DUB Edition Remix (PS2), as one of:
  - the game ISO (2048-byte sectors, `.iso` / `.bin`)
  - `ASSETS.DAT`
  - an extracted `resources/city/<city>.pck`
- NumPy (bundled with Blender)

## Installation

1. Download `mc3_city_importer.py`.
2. In Blender: **Edit > Preferences > Add-ons > Install...** and select the file. (In Blender 4.2+, use **Install from Disk...** from the dropdown in the top-right of the Add-ons tab.)
3. Enable **Import-Export: Midnight Club 3 City Importer**.

You can also run it without installing: open the file in Blender's Text Editor and press **Run Script**.

## Usage

### Sidebar (recommended)

1. In the 3D Viewport, press **N** and open the **MC3** tab.
2. Set **File** to your ISO, `ASSETS.DAT` or a city `.pck`.
3. If you chose an ISO or `ASSETS.DAT`, click **Scan ISO / ASSETS.DAT** and select a city from the list.
4. *(Optional)* Set **Textures** to `TEXTURE.DAT`, a `.ppf` file, or the ISO to get full-size textures instead of thumbnails.
5. Adjust the options and click **Import city**.

The city is added as a new collection named `MC3_<city>`.

### File > Import

**File > Import > Midnight Club 3 City (ISO / ASSETS.DAT / .pck)**

- Choosing a `.pck` imports it right away.
- Choosing an ISO or `ASSETS.DAT` scans it; then pick the city in the MC3 sidebar tab and press **Import city**.

## Options

| Option | Default | What it does |
|---|---|---|
| **File** | | Game ISO, `ASSETS.DAT`, or an extracted city `.pck` |
| **Textures** | | Optional. `TEXTURE.DAT`, a `.ppf`, or the ISO. Replaces 16x16 thumbnails with full-size textures |
| **Texture match tolerance** | 0.12 | How different a full-size texture may look from the city's thumbnail and still be used. Higher matches more textures but can pick wrong ones |
| **Convert Y-up to Z-up** | On | The game is Y-up, Blender is Z-up |
| **Scale** | 1.0 | Multiplier on game units (the game's cull grid cell is 40 units) |
| **Mode** | Separate objects | *Separate objects*: one object per placement, repeats share mesh data. *Single mesh*: bakes the whole city into one object (fast to handle, heavy in memory) |
| **Baked vertex colours** | On | Tints models with the city's per-vertex palette |
| **Vertex colour gain** | 2.0 | PS2 vertex colours are "modulate x2", so 0.5 is neutral |
| **Window glow** | 2.0 | Emission strength of lit windows |
| **Include LOD variants** | Off | Includes models stored in LOD slots 1-9 that have no level-0 mesh |
| **Include unreachable meshes** | Off | Meshes the object graph doesn't reach are given the position of their nearest neighbour. This is a guess, not file data |

## About textures

City `.pck` files only store **16x16 thumbnails** of their textures, with no names. When you provide a **Textures** file, the add-on shrinks every full-size texture to 16x16 and compares it against each thumbnail (upright and flipped). A thumbnail is replaced only when a close enough match is found. After an import, the status bar reports how many matched (for example `Full-size textures: 3100 of 3600 matched`), and extra details are printed to Blender's system console.

Tips:

- `TEXTURE.DAT` holds one `.ppf` per city and weather. The add-on prefers the pack for the city you're importing, and adds the city's largest pack when the dawn/dusk packs only contain the differences.
- `STREAMS.DAT` and `BANKS.DAT` are audio archives and contain no textures.
- If textures are missing or wrong, try a slightly higher or lower **Texture match tolerance**.

## Troubleshooting

| Message | Likely cause |
|---|---|
| `Not a 2048-byte-sector ISO 9660 image` | The ISO is in a different sector format. Convert it to a standard 2048-byte-sector ISO |
| `Pick the .iso/.bin itself, not the .cue sheet.` | Select the image file, not the `.cue` |
| `ASSETS.DAT not found in the ISO` | The image isn't the right game or is damaged |
| `No city .pck files found in this archive` | Wrong ISO / `ASSETS.DAT` |
| `No city meshes found ... header type 67` | The `.pck` isn't a city file |
| `That is a Hash (audio) archive` | You selected `STREAMS.DAT` or `BANKS.DAT` as the file or textures; they hold sound only |
| `No placeable geometry found in this city` | Nothing in the city is reachable through the object graph. Try enabling *Include unreachable meshes* |

If large cities look cut off in the viewport, the add-on raises the viewport clip end to 100,000 automatically.

## Status

This is a reverse-engineered importer for a specific game and its data format. Cities import with geometry, textures and lighting, but expect rough edges. Bug reports with the console output and the city name are welcome.

## Credits and licence

The city and level reading code is a port of `parse_ps2_city` from `tools/mc3_view.py`, plus `tools/mc3_vifpos.py` and `tools/mc3_cityobj.py`, from [znxee/mc3-znx-tools](https://github.com/znxee/mc3-znx-tools) (GPL-3.0). The `ASSETS.DAT` ("Dave") reader follows the format notes of the MC3 ASSETS.DAT Explorer.

Because of this, the add-on is released under the **GNU General Public License v3.0 or later** (`GPL-3.0-or-later`). Add a copy of the licence text as `LICENSE` in the repo.

Midnight Club 3: DUB Edition Remix is a trademark of Rockstar Games. This project is not affiliated with or endorsed by Rockstar Games or Take-Two Interactive. No game assets are distributed with this project.
