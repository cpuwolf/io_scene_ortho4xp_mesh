# Listing text for extensions.blender.org

Copy-paste for the submission form at <https://extensions.blender.org/submit/>.
Everything here matches `blender_manifest.toml` -- keep them in sync when the
version changes.

| field | value |
|---|---|
| Name | X-Plane DSF and Ortho4XP Mesh |
| Tagline (max 64 chars) | Import X-Plane DSF terrain and Ortho4XP meshes, with textures |
| Type | Add-on |
| Tags | Import-Export |
| License | GNU GPL v3.0 or later (`SPDX:GPL-3.0-or-later`) |
| Blender version min | 4.2.0 |
| Website | https://github.com/cpuwolf/io_scene_ortho4xp_mesh |
| Maintainer | Wei Shuai |
| Zip | `dist/io_scene_ortho4xp_mesh-<version>.zip` from `blender --command extension build` |
| Preview images | `doc/previews/hero_tile.png`, `doc/previews/closeup_airport.png` (first one is the thumbnail) |

Tags are `Import-Export` only: this is a file reader/writer for X-Plane scenery
formats. It is not a general mesh or UV tool.

## Description (markdown)

```markdown
Read and write X-Plane scenery data in Blender: **Ortho4XP tile meshes** (`.mesh`), the
**DSF terrain** X-Plane actually loads, and the **airport pavement and polygon
boundaries** that sit on top of it.

A `.mesh` is Ortho4XP's intermediate triangle mesh for one 1x1 degree tile — the file to
edit when a tile's water cuts, coastlines or airport levelling need fixing before it is
rebuilt into the DSF. A `.dsf` is what X-Plane reads, with its own terrain, textures and
the polygons (draped polygons, facades, forests, line features) drawn over it.

### Ortho4XP meshes

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

### X-Plane DSF terrain

- **Read-only DSF import**, with the tile's terrain textures resolved against your
  X-Plane install (or extra texture folders you point it at), and the tile's own UVs, so
  the orthophoto lands where X-Plane puts it. `sim/overlay` sceneries and Global Scenery's
  7z-compressed DSFs are handled in pure Python — no 7-Zip, no external tools.
- **Heights that come from the file, not from guesswork.** Global Scenery writes whole
  LOD layers with a placeholder elevation; those vertices are filled from the DSF's own
  `DEMS` raster, registered against the heights the mesh does carry and only trusted
  when it reproduces them (median error 0-1 m on the tiles tested).
- **Polygon boundaries.** The DSF's polygons come in as a separate `<tile>_polygons`
  object: `.pol` / `.fac` polygons and legacy class names as closed outlines, `.lin` line
  features as open polylines, draped on the terrain, edges only — no faces, no textures.
  Forest areas are off by default (a Global Scenery tile holds thousands).
- **The same boundaries as `.osm`.** Every import also writes `<dsf>.patch.osm` next to
  the DSF: one OSM way per ring, the X-Plane class in the `xplane_class` tag, open ways
  flagged `xplane_linear`, so JOSM can open, compare against imagery and edit them.
- Exporting a DSF is deliberately not offered: a DSF holds objects, vector networks and
  raster layers this add-on does not model, and writing it back would silently drop
  them.

### Airport pavement from apt.dat

- **`File → Import → X-Plane apt.dat (pavement, .dat)`**: the taxiway and apron
  boundaries of apt.dat 12.00, including Bezier edges, triangulated and draped on the
  terrain that is already in the scene — a DSF tile or an Ortho4XP `.mesh` tile, either
  one. With no tile in the scene the pavement is placed flat at each airport's own
  elevation, so it never needs a terrain import to be useful.
- Airports are picked by identifier (`KLVM`, `29S`, ...), by the tile in the scene, or —
  for a small file — all of them. A 360 MB worldwide apt.dat is read in a single
  streaming pass.

### Worth knowing

- Textures only exist where `.ter` files (or DSF texture references) do; triangles
  without a texture get a red *uncovered* slot rather than a neighbour's image, so gaps
  show up instead of lying.
- A `.mesh` carries no per-vertex blend factor, so the extra `.ter` layers (lit,
  composite, border) are imported labelled but not blended.
- Tested on Blender 5.2 LTS (macOS); requires 4.2 or newer, where extensions live.

Source, docs and the verification scripts (mesh format against Ortho4XP's own reader,
DSF heights against the file's raster, seams against Blender's operator, texture
assignment on real tiles):
https://github.com/cpuwolf/io_scene_ortho4xp_mesh
```

## Before uploading

1. `blender --command extension validate dist/io_scene_ortho4xp_mesh-<version>.zip`
2. Screenshots must be your own render/screenshot of *your* add-on — `doc/previews/`
   holds the ones that ship with this repo.
3. Review happens at <https://extensions.blender.org/approval-queue/>; expect a
   moderator to ask about anything not obvious from the description.
