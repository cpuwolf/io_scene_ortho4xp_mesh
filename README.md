# Ortho4XP Mesh Import/Export (Blender addon)

Read and write Ortho4XP tile meshes — `Data+50-125.mesh`, `Data+27+089.mesh` etc. —
in Blender 4.0+ (tested on Blender 5.2.2 LTS, macOS), with the tile's `.ter`
orthophotos attached on import.

Install: this folder *is* the addon. Copy it into
`<blender config>/scripts/addons/` (macOS: `~/Library/Application Support/Blender/<ver>/scripts/addons/`),
symlink it from there, and enable **Ortho4XP Mesh Import/Export** in
*Preferences → Add-ons*, or enable it from the ZIP via *Install from Disk…*.

* `File → Import → Ortho4XP Mesh (.mesh)`
* `File → Export → Ortho4XP Mesh (.mesh)`
* 3D viewport sidebar (`N`) → tab **Ortho4XP**

---

## What the file is

`.mesh` is Ortho4XP's **intermediate** triangle mesh for one 1×1 degree tile, not a
format X-Plane reads. `src/O4_Mesh_Utils.py`:

* `write_mesh_file()` writes it from the Triangle4XP output (`step 2`),
* `read_mesh_file()` reads it in `O4_DSF_Utils.build_dsf()` (`step 3`) to build the
  DSF that X-Plane actually loads.

So this is the file to edit when a tile's water cuts, coastlines or airport
flattening need fixing before rebuilding the DSF.

## Format

MEDITS, ASCII, one element per line:

```
MeshVersionFormatted 2
Dimension 3
<blank>
Vertices
2292352
<lon> <lat> <z> 0            '{:.15f} {:.15f} {:.15f} 0'
<blank>
Normals
2292352
<nx> <ny> 0                  '{:.2f} {:.2f} 0'
<blank>
Triangles
4575096
<v1> <v2> <v3> <attr>        1-based vertex indices
```

### Two flavours

Both are in the wild; both are read and written **byte-exactly**. The flavour is
detected on import (header text, coordinate precision, normal column count), stored
on the object, and reproduced on export.

| | Ortho4XP 1.30 (2023) | 1.4 fork ("v2") |
|---|---|---|
| header | `MeshVersionFormatted 1.30` | `MeshVersionFormatted 2` |
| coordinates | `{:.7f}` | `{:.15f}` |
| normals | `%.2f %.2f` (2 columns) | `%.2f %.2f 0` (3 columns) |
| elevation | `alt_m = z * 1e5` | identical |
| line endings | CRLF | CRLF |
| trailing `End` | absent | absent |

`MeshVersionFormatted` is not free-form: `build_dsf` branches on it
(`has_water = 7 if version >= 1.3 else 3`), so keeping the source's exact text
matters when a tile is rebuilt. Ortho4XP 1.4's own `read_mesh_file()` cannot read a
1.30 file (it expects the third normal column), which is why the flavour is
preserved instead of normalised.

Quirks the addon knows about (all verified against real tiles, see *Verification*):

| item | reality |
|---|---|
| elevation | `z` is in units of **100000 m**: `alt_m = z * 1e5` (`read_mesh_file` multiplies, `write_mesh_file` divides) |
| vertex 4th field | MEDITS "reference"; Ortho4XP always writes `0` |
| normal fields | only `nx, ny` are real (X-Plane's DSF stores two components and implies +up), so the true normal is `(nx, ny, sqrt(1-nx²-ny²))`. 1.4 writes a literal `0` as the third field, 1.30 omits it |
| triangle 4th field | Triangle4XP region **bit mask**, not off by one: `1` WATER, `2` SEA, `4` SEA_EQUIV, `8` INTERP_ALT, `16` RUNWAY, `32` TAXIWAY, `64` APRON, `128` HANGAR, `0` DUMMY. `read_mesh_file` calls it `tri_types` |
| line endings | the file is written in Python text mode, so a Windows build emits **CRLF** (this is what tiles on disk have) |
| trailing `End` | not written by Ortho4XP (MEDITS allows it; the reader tolerates it) |
| winding | triangles are **counter-clockwise seen from above**, so Blender's face normals come out pointing up — no flipping needed (unlike DSF, which is clockwise) |

Reading is deliberately tolerant: sections in any order, wrapped or padded lines,
`End`, blank lines and `#` comments are accepted, unknown sections are skipped and
reported, and 2- as well as 3-column normals are accepted.

## Terrain textures (.ter)

A `.mesh` carries **no** terrain reference — unlike a DSF, which names a terrain
definition per patch — so the orthophotos are rebuilt from the tile itself. Every
ortho cell `.ter` that Ortho4XP writes declares its own footprint:

```
LOAD_CENTER <lat> <lon> <size_m> <tex_px>     the cell this texture covers
BASE_TEX_NOWRAP ../textures/<cell>.dds        the orthophoto, mapped once
LOAD_CENTER_BORDER <lat> <lon> <size_m> 512   sea overlay border (optional)
BORDER_TEX ../textures/<cell>.png
```

So the cells are a spatial index: each face is assigned to the cell whose footprint
contains its centroid, gets that cell's material, and is UV-mapped once over that
footprint (cell centre at 0.5, 0.5, footprint edges at 0 and 1) — which is what
X-Plane does when it renders the same tile. The tile's `terrain/` folder is found
next to the `.mesh` automatically; override it with *Terrain folder*.

Details that matter on real tiles:

* **The tiling comes from the observed lattice, not from `size_m`.** Measured on
  `+27+089`, the declared size converted to degrees is ~0.1% narrow in longitude and
  ~0.6% tall in latitude versus the actual cell-centre spacing, so metre-derived
  footprints leave accumulating bands of untextured faces along the borders of every
  finer-zoom patch (first attempt: only 83% of faces covered). Each cell therefore
  keeps the area up to the midline between it and its same-zoom-level neighbours,
  which tiles exactly, and the cells are layered coarse → fine so a ZL19 patch
  overwrites the ZL17 grid beneath it.
* **Each bin keeps every cell whose footprint touches it**, and the assignment takes
  the finest of those that *really* contains the point. Keeping only the finest cell
  per bin (the first version) silently drops the coarse cell underneath a fine
  patch: on `+27+089` that stranded 637 faces in bins owned by ZL19 cells that did
  not contain them, and left the 5 ZL18 cells underneath used by no face at all, so
  their textures never reached Blender.
* **`_sea_overlay.ter` wins** over the plain `.ter` with the same footprint, because
  that is the definition Ortho4XP's own DSF uses for those cells (the overlay adds
  `WET`, `NO_SHADOW` and a border mask).
* A tile that mixes zoom levels is the normal case: `+27+089` has 963 `.ter` files
  → 707 distinct footprints (618 at ZL17, 27 at ZL18, 62 at ZL19; 256 of them sea
  overlays).
* Faces with no cell get a red `ter_none_uncovered` slot instead of being silently
  painted with a neighbour's texture (637 of 2.48 M faces on `+27+089`, 0.03%, all
  at the tile edge).
* **Material slots:** a face has exactly one material index, so *Material slot per
  triangle attribute* is skipped when textures are imported — the orthophoto wins.
  `mesh_attr` is still written and still drives the export.

### Memory (`tex_limit`, `tex_max_px`)

There is no cheap way to display 1000 orthophotos at once, so both knobs are
explicit:

* **Blender loads lazily.** `bpy.data.images.load()` alone costs ~0.4 MB per image
  (measured); pixels are decoded only when something displays them, so importing all
  707 cells is cheap. Displaying the *whole* tile is what costs: a Workbench render
  of every cell's orthophoto measured **11.9 GB peak RSS** (macOS
  `maximum resident set size`), while a single full-resolution decode of one
  4096×2560 DXT image costs ~248 MB on its own — Blender evidently keeps a reduced
  copy per displayed texture rather than 707 full decodes.
* **`tex_limit`** (default 0 = all) loads images for at most N cells, the ones
  owning the most faces first. Materials, `.ter` metadata and UVs are created for
  *every* used cell either way; only the image is skipped, so nothing is lost.
* **`tex_max_px`** decodes and downscales each image to at most N pixels: a
  4096×2560 orthophoto dropped to 1024×640 costs ~5 MB instead of ~248 MB
  (measured), which is what makes a whole-tile view practical. It is a quality
  trade-off, not free: at 1024 px a 1085 m ZL19 cell is ~1 m/px, and a close-up
  rendered at that scale aliases badly (measured: the same 1200×1200 view of a
  4096 px cell is crisp at native size and shows heavy horizontal moiré at 1024).
  Use 0 — or a higher cap — when inspecting detail, a small cap when viewing a
  whole tile.

> **Why the cap needs care:** writing `img.colorspace_settings.name` — *even to the
> value it already has* — invalidates Blender's ImBuf and makes it re-read the file
> at native size, silently discarding an earlier `Image.scale()` (measured: a 4096²
> DDS → scale to 1024 → set sRGB → back to 4096). The cap is therefore applied
> **after** the material wiring and then verified: the importer reports
> `images_over_cap` in its stats and warns if an image could not be brought down, and
> a whole-tile Workbench render leaves every image at or below the cap.

`base` textures drive Base Color, a `NORMAL_TEX` gets a normal-map node, and the
remaining layers (`lit`, `composite`, `border`) are imported as labelled but
**unconnected** nodes: blending them needs the per-vertex factor that lives in the
DSF, which a `.mesh` does not carry, so wiring them to an invented factor would be a
lie.

## Import

* Geometry is projected into a local tangent plane about the tile centre, in metres:
  `X = east, Y = north, Z = up`. The projection is stored on the object as
  `mesh_georef`, with the same keys the `io_scene_dsf` addon uses, so a `.dsf` tile
  and the `.mesh` it came from import to the same place and line up in one scene.
* `mesh_attr` (INT, FACE) keeps every triangle's region attribute and
  `mesh_normals` (FLOAT_VECTOR, POINT) keeps `nx, ny` — both are written back
  verbatim on export. Optional *Custom split normals* also makes Blender shade with
  them.
* UVs land in `XPlaneProjected` (same layer name as the DSF addon). They are
  computed per loop from the cell that owns the face, so neighbouring cells sample
  their own texture; `BASE_TEX_NOWRAP` cells use `EXTEND`, so a single vertex that
  strays up to one mesh edge outside its cell clamps instead of wrapping.
* Vertex positions land in Blender's float32 vertices, so the round trip is exact to
  ~3e-8 degrees. On a v2 tile (15 decimals) that shows up as 1.95 mm horizontally /
  0.12 mm vertically; on a 1.30 tile (7 decimals) it is below the stored precision
  and the exported file is **byte-identical** to the source.

## Export

* *File flavour*: **Same as the source file** (default), **Ortho4XP 1.4 (v2)** or
  **Ortho4XP 1.30**.
* Writes `mesh_attr` back as the 4th triangle column, `nx, ny` back as the normal
  components, and by default keeps the source file's line endings.
* N-gons and quads are triangulated on the fly via `calc_loop_triangles()`.
* **Recompute normals**: tick after moving/adding vertices. The DSF builder bakes
  these two components into the tile's normal planes, so stale normals otherwise
  survive into the DSF.
* One `.mesh` = one 1×1 degree tile. Objects from different tiles are refused;
  several objects of the *same* tile are merged (vertices are not welded).

## Verification

`verify/verify_mesh_format.py` checks a file against Ortho4XP's own code (it loads
`src/O4_Mesh_Utils.py` with its heavy imports stubbed, calls `read_mesh_file()`, then
walks the file byte by byte re-formatting every number with the exact format strings
`write_mesh_file()` uses, and finally re-writes the parsed data and compares sha256):

```
python3 verify/verify_mesh_format.py [mesh_file] [--repo /path/to/Ortho4XP]
```

`verify/blender_roundtrip_test.py` is the headless Blender round trip:

```
blender --background --factory-startup --python verify/blender_roundtrip_test.py -- in.mesh out.mesh [ATTR|SPLIT|NONE]
```

Results on the two reference tiles (the second was rebuilt since the 1.30 numbers
were taken — see the note below the table):

| | `sample/Data+50-125.mesh` | `zOrtho4XP_+27+089/Data+27+089.mesh` (Google imagery, ZL13/15/16/17/18/19) |
|---|---|---|
| flavour, size, CRLF | v2, 288,705,257 bytes | v2, 278,645,514 bytes |
| content | 2,292,352 verts / 4,575,096 tris | 2,250,375 verts / 4,491,416 tris |
| codec `read_mesh` → `write_mesh` | **sha256-identical** | **sha256-identical** |
| byte conformance to `write_mesh_file()` | 0 errors | v2 templates (see note) |
| Blender round trip | indices/attributes/normals identical, positions within 1.95 mm / 0.12 mm, ~50 s, ~3.5 GB peak RSS | indices/attributes/`nx, ny` identical, positions exact, **export byte-identical** (sha256 match through Blender) |

> **Evidence note.** The 1.30 flavour was verified against a Bing/ZL17-19 build of
> `+27+089` (1,246,225 verts, 118,855,686 bytes) that the data owner has since
> replaced: codec round trip sha256-identical, `verify_mesh_format.py`'s byte check
> run with the 1.30 templates, and a Blender export that was byte-identical because
> 7 decimals is coarser than float32's rounding of local metres. No Ortho4XP-written
> 1.30 file remains in the local sample set, so those numbers are history rather than
> a re-runnable result; the code path is still exercised by
> `test_flavours.py`-style round trips on a generated 1.30 file.

Terrain assignment on the current `+27+089` build (183 cells from 231 `.ter` files,
48 of them sea overlays, six zoom levels from ZL13's 69 km cell down to ZL19's
1.08 km): **100.000%** of faces covered (4,491,416 / 4,491,416), all 183 cells used
(101 to 1,265,906 faces each — none left without a texture), every assigned face
centroid inside its cell, every cell's face centroids inside its own texture's 0..1
range, and `terrain_tiles.uv_center` matches `tercodec.uv_from_center` bit for bit
(max delta 0.0). Importing the geometry plus textures for the 64 biggest cells
(capped at 1024 px) takes ~25 s on a 4.49 M-triangle tile, and a Workbench render of
the result shows the orthophotos with no untextured patch anywhere.

## Layout

```
__init__.py       operators, georeference, material slots, .ter textures, panel
meshcodec.py      dependency-free (numpy only) .mesh reader/writer + OrthoMesh
terrain_tiles.py  .ter cell scan, lattice footprints, face assignment, UV maths
tercodec.py       .ter parser + uv_from_center (from xplane-dsf-blender)
verify/           verification scripts (format + Blender round trip)
```

`meshcodec.py` and `terrain_tiles.py` import no `bpy`, so both can be exercised
headless — that is how the format and the face assignment were verified — while all
Blender-side wiring lives in `__init__.py`.