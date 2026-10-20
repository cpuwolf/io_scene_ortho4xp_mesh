# Listing text for extensions.blender.org

Copy-paste for the submission form at <https://extensions.blender.org/submit/>.
Everything here matches `blender_manifest.toml` -- keep them in sync when the
version changes.

| field | value |
|---|---|
| Name | Ortho4XP Mesh Import/Export |
| Tagline (max 64 chars) | Edit Ortho4XP tile meshes with their orthophotos in Blender |
| Type | Add-on |
| Tags | Import-Export, UV, Mesh |
| License | GNU GPL v3.0 or later (`SPDX:GPL-3.0-or-later`) |
| Blender version min | 4.2.0 |
| Website | https://github.com/cpuwolf/io_scene_ortho4xp_mesh |
| Maintainer | Wei Shuai |
| Zip | `dist/io_scene_ortho4xp_mesh-<version>.zip` from `blender --command extension build` |
| Preview images | `doc/previews/hero_tile.png`, `doc/previews/closeup_airport.png` (first one is the thumbnail) |

## Description (markdown)

```markdown
Read and write **Ortho4XP tile meshes** (`.mesh`) inside Blender, with the tile's
orthophotos attached.

A `.mesh` is Ortho4XP's intermediate triangle mesh for one 1x1 degree tile — the file to
edit when a tile's water cuts, coastlines or airport levelling need fixing before it is
rebuilt into the DSF that X-Plane reads. This add-on brings that file into Blender with
its real georeferencing and its textures.

### What it does

- **Faithful import.** Both Ortho4XP flavours (1.30 and the 1.4 "v2" format), both line
  endings, altitudes from the file's 1e5-scaled z. Lon/lat becomes local metres (X east,
  Y north, Z up) about the tile centre.
- **The tile's orthophotos.** The `.ter` files next to the `.mesh` are read, each triangle
  is assigned to the ortho cell that contains its centroid, and every cell gets its own
  material and images (base colour, normal map wired; lit / composite / border layers
  imported as labelled nodes). Optional *Max texture size* / *Max textures* keep a ZL18
  tile (about 1000 images) affordable in memory.
- **UVs that agree with the render.** One `XPlaneProjected` UV layer, `uv = position *
  scale + offset` per cell — the same four numbers the shader projection uses, so the UV
  editor, Workbench and every tool that reads UVs match what you see.
- **Project textures along Z.** The images can be fed from the object-space position
  instead of baked UVs, so the orthophoto stays nailed to the map while you drag
  vertices — what X-Plane itself does with the same tile. *Projected UVs* re-bakes the
  layer once the geometry is edited.
- **Cell borders as seams.** Marks the borders between ortho cells as UV seams — the same
  edge set as *UV -> Seams from Islands*, computed on the datablock (7 s instead of a
  10.7 s edit-mode round trip on a 4.5M-triangle tile).
- **Attribute slots.** Optional material slot per triangle attribute (water, sea, runway,
  taxiway, apron, hangar) with viewport colours, so water cuts and airport levelling are
  visible at a glance — automatically skipped when the orthophotos win the slot.
- **Export back** to `.mesh`, byte-identical by default (same flavour, precision, line
  endings and normals as the source file).

### Worth knowing

- Textures only exist where `.ter` files do; triangles outside every footprint get a red
  *uncovered* slot rather than a neighbour's texture, so gaps show up instead of lying.
- A `.mesh` carries no per-vertex blend factor, so the extra `.ter` layers (lit,
  composite, border) are imported labelled but not blended.
- Tested on Blender 5.2 LTS (macOS); requires 4.2 or newer, where extensions live.

Source, docs and the verification scripts (mesh format against Ortho4XP's own reader,
seams against Blender's operator, texture assignment on real tiles):
https://github.com/cpuwolf/io_scene_ortho4xp_mesh
```

## Before uploading

1. `blender --command extension validate dist/io_scene_ortho4xp_mesh-<version>.zip`
2. Screenshots must be your own render/screenshot of *your* add-on — `doc/previews/`
   holds the two that ship with this repo.
3. Review happens at <https://extensions.blender.org/approval-queue/>; expect a
   moderator to ask about anything not obvious from the description.
