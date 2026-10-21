# X-Plane DSF import (terrain)

`File → Import → X-Plane DSF (terrain, .dsf)` reads the terrain mesh out of a scenery
`.dsf` — the file X-Plane actually loads — so a shipped tile can be inspected next to (or
instead of) the Ortho4XP `.mesh` it was built from. There is **no DSF export**: a DSF carries
layers this add-on does not model (objects, polygons, vector networks, raster DEM), so
writing one back would silently drop them.

## Compressed (Global Scenery) files

Global Scenery ships `.dsf` files as bare 7z archives. They are decoded by the add-on's own
`sevenzip` module — pure Python on top of the stdlib `lzma` module, no `bsdtar`, no 7-Zip, no
third-party package, so it works the same on macOS, Windows and Linux. Uncompressed DSFs
(Global Airports, most add-on sceneries) are read directly.

The decoder is verified against `bsdtar` byte-for-byte: 250 of the 894 compressed DSFs in a
full X-Plane 12 install, plus every archive sampled in the batch runs (README has the
numbers).

## What it imports

| DSF content | Status |
|---|---|
| Terrain patches: triangles, triangle strips, triangle fans (incl. cross-pool and range forms) | imported |
| Terrain type table (`TERT`) → `.ter` → base + normal texture | imported as materials |
| Objects, objects-with-ranges, network chains/ranges, polygons, nested polygons | parsed, counted, **not** imported (reported at the end) |
| Raster DEM layers | parsed, skipped |

Per terrain definition you get one material slot, one UV island, and (with *Project textures
along Z*, on by default) the same fixed-to-the-map projection the `.mesh` importer offers:
`uv = position * scale + offset` in the tile's local metres, evaluated in the shader, so the
ground texture stays put while you drag vertices.

The `.ter` mapping is read exactly as the format defines it:

* `PROJECTED <x> <y>` — the texture covers that many metres; `u = (x + w/2 − offset_x) / x`.
  `PROJECT_OFFSET` is applied; `PROJECT_ANGLE` (never used by the shipped library) is
  recorded on the material but not applied.
* `LOAD_CENTER <lat> <lon> <m>` — the texture covers a footprint of that size centred there,
  so `u = 0.5 + east/size`.
* `BASE_TEX_NOWRAP` / `LOAD_CENTER` switch the image nodes to `EXTEND`.

Textures are resolved through X-Plane's terrain library (`lib/g10/...`), so *X-Plane root* is
auto-detected by walking up from the `.dsf`; scenery packages that ship their own textures
can be added in *Extra texture roots* (separated by `:` — `;` on Windows).

## Vertices without an elevation come from the tile's own raster DEM

X-Plane 12 writes a tile's heights **twice**: in the point pools (the mesh) and in a
raster DEM (`DEMS` container — `DEMI` header: width, height, bytes/pixel, format, scale,
offset; `DEMD`: the pixels). Global Scenery leaves whole LOD layers of the mesh without
heights — raw elevation `0`, which the format's `scale 65535 / offset -32768` decodes to
**-32768 m** — and without normals. Imported as-is those layers become a 32 km deep sheet
under the real landscape ("flat water with the mountains floating in the sky").

So the importer reads the raster too:

1. every raster layer in `DEMS` is parsed (int/uint/float, 1-4 bytes per pixel);
2. the layer that really is this tile's elevation grid is **identified by fitting it to
   the heights the mesh already carries** — both row orders and a pixel-offset search,
   best median error wins, and only a fit under 120 m is accepted;
3. vertices at the `-32768 m` floor are then sampled bilinearly from that grid, with
   edge-extend at the tile border.

Measured on real tiles (`median error` = raster vs the mesh's own heights):

| tile | raster used | fit median error | triangles | kept | z range after |
|---|---|---|---|---|---|
| `+45-111` (Yellowstone) | 1201×1201 int16 | **1.0 m** | 461,347 | all | 1233…3061 m |
| `+28-097` (Texas coast) | 1201×1201 int16 | **0.0 m** | 266,689 | all | 0…47 m |
| `+27+089` (Ortho4XP-built) | not needed | — | 4,577,322 | all | 445…7295 m |

A file with no usable raster (or none at all, like an Ortho4XP-built DSF) still imports;
triangles that end up without any height are skipped and reported rather than placed at
-32768 m. The import reports both numbers: `N vertices … taken from the tile's own raster
DEM`, and `M triangle(s) skipped` when even the raster is silent.

## Known limits

* **Terrain only.** Objects, polygons and vector networks are counted in the report and
  dropped — a DSF tile's autogen, facades, forests and roads are not imported.
* The `.ter` layers `LIT_TEX` / `COMPOSITE_TEX` / `BORDER_TEX` are imported as labelled,
  unconnected nodes: blending them needs the per-vertex factor in DSF pool planes 5+, which
  a Blender mesh does not carry.
* Terrain types rendered internally by X-Plane (`terrain_Water`, `terrain_*_builtin`) have no
  file on disk; their materials say `dsf_ter_kind = "builtin"` and get no image. The import
  report counts them separately from genuinely unresolved ones.
* `Max texture size` defaults to 2048 px here (unlike the `.mesh` importer's 0): one DSF tile
  references dozens of terrain types at 1024–4096 px each.
* Normals are recomputed by Blender; the DSF's own normal planes are not used (they are
  two-component and only meaningful to the sim's shading).
