# Verification

Five scripts in `verify/`. Three of them are plain Python (they exercise the `bpy`-free
modules `meshcodec.py` / `terrain_tiles.py` headless); two need Blender.

## `verify/verify_mesh_format.py` — a file against Ortho4XP's own code

It loads `src/O4_Mesh_Utils.py` with its heavy imports stubbed, then does three independent
checks:

1. calls Ortho4XP's `read_mesh_file()` on the file and prints what it decodes;
2. walks the file byte by byte and checks every field against the exact format strings
   `write_mesh_file()` emits — each parsed number is re-formatted with `'{:.15f}'` /
   `'{:.2f}'` and compared as text, so a layout *or* float-formatting mismatch is caught;
3. re-writes the parsed data with this addon's `meshcodec` and compares the sha256 of the
   result with the original file.

```sh
python3 verify/verify_mesh_format.py [mesh_file] [--repo /path/to/Ortho4XP]
```

## `verify/blender_roundtrip_test.py` — the headless Blender round trip

Enables the addon, imports, exports, then re-reads both files with `meshcodec` to report how
much the round trip changed.

```sh
blender --background --factory-startup --python verify/blender_roundtrip_test.py -- \
        in.mesh out.mesh [ATTR|SPLIT|NONE]
```

The third argument selects the *Vertex normals* import mode (`ATTR` = attribute only,
`SPLIT` = custom split normals, `NONE` = ignore).

## `verify/seams_test.py` — *Seams from UV islands* against Blender's own operator

First the nine hand-built cases that pin the rule down (two faces agreeing, two disagreeing
by 1e-7, three and four faces on one edge, unwelded twins, a lone border edge, a mesh with no
UV layer), then a whole `.mesh` tile, comparing the two edge sets — plus the sidebar button
and the seam count it stores on the object, against the same set.

```sh
blender --background --factory-startup --python verify/seams_test.py -- [tile.mesh]
```

Without a tile argument part 1 still runs and part 2 is reported as skipped.

## `verify/zproject_test.py` — the Z-axis texture projection, including a render

Builds a synthetic tile (a 1.2 km grid with two 2 km `LOAD_CENTER` cells and a checker
texture each), imports it through the addon, then checks:

1. every image texture is fed from the Z projection (Texture Coordinate → Vector Math
   Multiply Add) and its four numbers are the cell's affine map;
2. the baked `XPlaneProjected` layer equals `uv = position * scale + offset` and equals
   `terrain_tiles.uv_center` (the documented `LOAD_CENTER` mapping);
3. rendering *reads* the projection: rewiring each material to Emission of the projection
   vector makes the rendered pixel at a known world point report the UV the shader computed
   there — compared against the affine of the triangle the camera ray actually hits, and
   against the UV layer's own value;
4. dragging vertices leaves the projection readout unchanged (the map is a function of
   position — the "texture stays put" claim), while the baked UV layer goes stale, and
   *Projected UVs* puts it back.

Two PNGs (projection on / off, same dragged geometry) are written to the output folder as a
visual: with the projection the checker lines stay straight and aligned to the world, without
it they kink at the moved patch.

```sh
blender --background --factory-startup --python verify/zproject_test.py -- [outdir]
```

## `verify/terrain_lookup_test.py` — face → ortho cell assignment, with holes

`terrain_tiles.TerrainIndex.lookup` walks the faces in chunks of 65536 points, and a tile
whose `.ter` set covers every face never exercises the miss path. This one builds a
synthetic lattice *with gaps*, a finer cell inside it and a sea overlay twin of one plain
cell, then compares the chunked answer against brute force: cell centres resolve to their
own cell (finest first, overlay first), gap and off-lattice points come back `-1`, 150k
random points (>2 chunks, so full chunks containing misses exist) match element for
element, and the answer is the same for chunk sizes 65536 / 1000 / 7.

```sh
python3 verify/terrain_lookup_test.py
```

## Measured results on the two reference tiles

|  | `sample/Data+50-125.mesh` | `zOrtho4XP_+27+089/Data+27+089.mesh` (Google imagery, ZL13/15/16/17/18/19) |
|---|---|---|
| flavour, size, line endings | v2, 288,705,257 bytes, CRLF | v2, 278,645,514 bytes, CRLF |
| content | 2,292,352 verts / 4,575,096 tris | 2,250,375 verts / 4,491,416 tris |
| codec `read_mesh` → `write_mesh` | **sha256-identical** | **sha256-identical** |
| byte conformance to `write_mesh_file()` | 0 errors | v2 templates (see the note) |
| Blender round trip | indices/attributes/normals identical, positions within 1.95 mm / 0.12 mm, ~50 s, ~3.5 GB peak RSS | indices/attributes/`nx, ny` identical, positions exact, **export byte-identical** (sha256 match through Blender) |

> **Evidence note.** The 1.30 flavour was verified against a Bing/ZL17-19 build of
> `+27+089` (1,246,225 verts, 118,855,686 bytes) that the data owner has since replaced:
> codec round trip sha256-identical, `verify_mesh_format.py`'s byte check run with the 1.30
> templates, and a Blender export that was byte-identical because 7 decimals is coarser than
> float32's rounding of local metres. No Ortho4XP-written 1.30 file remains in the local
> sample set, so those numbers are history rather than a re-runnable result; the code path is
> still exercised by flavour round trips on a generated 1.30 file.

Rewriting the current `+27+089` tile as the 1.30 flavour (same geometry, 2-column normals,
7 decimals) gives a 220,135,767 byte file that Ortho4XP 1.4's own `read_mesh_file()` reads
back without complaint — both flavours are accepted by the current reader, which takes only
the first two tokens of each normal line.

## Terrain assignment on the current `+27+089` build

183 cells from 231 `.ter` files, 48 of them sea overlays, six zoom levels from ZL13's 69 km
cell down to ZL19's 1.08 km:

* **100.000 %** of faces covered (4,491,416 / 4,491,416);
* all 183 cells used (101 to 1,265,906 faces each — none left without a texture);
* every assigned face centroid inside its cell, and every cell's face centroids inside its
  own texture's 0…1 range;
* `terrain_tiles.uv_center` matches `tercodec.uv_from_center` bit for bit (max delta 0.0).

Importing the geometry plus textures for the 64 biggest cells (capped at 1024 px) takes
~25 s on a 4.49 M-triangle tile, and a Workbench render of the result shows the orthophotos
with no untextured patch anywhere.

## Repository layout

```
__init__.py       operators, georeference, material slots, .ter textures, panel
meshcodec.py      dependency-free (numpy only) .mesh reader/writer + OrthoMesh
terrain_tiles.py  .ter cell scan, lattice footprints, face assignment, UV maths
tercodec.py       .ter parser + uv_from_center (from xplane-dsf-blender)
doc/              this documentation (+ two standalone workflow diagrams as HTML)
verify/           verification scripts (format, lookup, Blender round trip, seams, z projection)
```

`meshcodec.py` and `terrain_tiles.py` import no `bpy`, so both can be exercised headless —
that is how the format and the face assignment were verified — while all Blender-side wiring
lives in `__init__.py`.