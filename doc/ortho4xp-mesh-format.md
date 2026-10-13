# The Ortho4XP `.mesh` file format

`Data+27+089.mesh` is Ortho4XP's **intermediate** triangle mesh for one 1×1° scenery tile.
It is a MEDITS-format ASCII triangle mesh — one element per line, no compression, no
header block, about 280 MB for a ZL17-ish tile.

**X-Plane never reads this file.** Ortho4XP writes it in step 2 and reads it back in step 3
to build the `.dsf` that X-Plane actually loads. That is why it is the right file to edit
when a tile's water cuts, coastlines or airport flattening need fixing: you change the
geometry before the DSF is built, without touching Ortho4XP's Python or the tile config.

Two tools, two steps, one file. X-Plane only ever sees the `.dsf` on the right:

```
Ortho4XP step 1–2 ──▶ Data+27+089.mesh ──▶ Ortho4XP step 3 ──▶ Earth nav data/+27+089/+27+089.dsf
   create scenery        (this file)         build imagery+DSF      (what X-Plane reads)
                              ▲    │
                              └────┴──── Blender: import → edit → export, same file
```

---

## 1. Where the file comes from, and where it goes

All function names below are from the Ortho4XP 1.4 fork (v1.31); the 1.30 release used the
same layout, with the formatting differences listed in §4.

| Step | Code | What it does with the `.mesh` |
|---|---|---|
| 1–2 | `O4_Vector_Map.include_*`, `O4_Vector_Utils.Vector_Map.write_node_file()/.write_poly_file()` | writes the vector map: `Data+27+089.node`, `.poly`, plus the DEM raster `.alt` and the airport dump `.apt` |
| 1–2 | Triangle4XP (`Utils/src/Triangle4XP.c`) | triangulates the vector map into the `.ele` file; the 4th column of every triangle is its region attribute |
| 2 | `O4_Mesh_Utils.post_process_nodes_altitudes()` | reads the `.ele`, levels the marked regions, replaces `z` and normals for the marked triangles |
| 2 | `O4_Mesh_Utils.write_mesh_file()` | **writes** `Data+27+089.mesh` |
| 3 | `O4_Tile_Utils.py` | downloads imagery into `textures/`, writes `terrain/*.ter` |
| 3 | `O4_DSF_Utils.build_dsf()` → `O4_Mesh_Utils.read_mesh_file()` (called at `O4_DSF_Utils.py:473`) | **reads** the `.mesh` back, cuts water, builds the DSF |

The writer copies the triangle lines straight out of the `.ele` file (`" ".join(f_ele.readline().split()[1:])`),
which is why the attribute column is a Triangle4XP value and not something Ortho4XP invents
at write time.

## 2. One file per tile

The name is `Data+LL+LLL.mesh`, where `LL`/`LLL` are the **south-west corner** of the 1×1°
tile, zero-padded and signed: `Data+27+089.mesh` covers lat 27…28, lon 89…90 (VQPR,
Paro). Coordinates inside the file are absolute WGS84, not tile-relative, so any number of
tiles can be imported into one Blender scene without clashing.

## 3. Structure

```
MeshVersionFormatted 2                        header: which flavour this is
Dimension 3
                                              blank line
Vertices
2250375                                       vertex count
89.420056431852743 27.410980324026070 0.022366140136719 0
...                                           one line per vertex: lon lat z 0
                                              blank line
Normals
2250375                                       normal count (== vertex count)
0.00 0.00 0
...                                           one line per normal: nx ny 0
                                              blank line
Triangles
4491416                                       triangle count
1 2 3 0
...                                           one line per triangle: v1 v2 v3 attr
```

There is **no trailing `End`** and there are **no `#` comments** in files Ortho4XP writes.

### 3.1 `MeshVersionFormatted <version>`

A single token, `2` or `1.30`. Ortho4XP parses it as a float
(`float(f.readline().strip().split()[-1])`).

### 3.2 `Dimension 3`

Fixed. Coordinates are always 3D; the third is elevation, not a dummy.

### 3.3 `Vertices`

`<count>` then one line per vertex:

| field | meaning |
|---|---|
| 1 | longitude, absolute WGS84 degrees |
| 2 | latitude, absolute WGS84 degrees |
| 3 | **elevation in units of 100000 m** — `alt_m = z * 1e5` |
| 4 | MEDITS "reference" tag; Ortho4XP always writes `0` |

The third field is the classic trap: a real tile reading `0.022366140136719` is at
**2236.614 m**, not 2 cm. `read_mesh_file()` multiplies by 100000 right after parsing.

### 3.4 `Normals`

Same count as the vertices, one line each, and only the **two horizontal components are
real**: `nx, ny` of an upward normal. X-Plane's DSF stores two components and implies +up,
so the true normal is `(nx, ny, sqrt(1 - nx² - ny²))`. The optional third column is a
literal `0` placeholder, *not* a usable Z component — do not normalize the triplet as
stored. Values are formatted with 2 decimals.

### 3.5 `Triangles`

`<count>` then `v1 v2 v3 attr`, with **1-based** vertex indices and a `0`-defaulted
attribute column. Ortho4XP's reader subtracts 1 from all four fields in one list
comprehension and then adds 1 back onto the attribute:

```python
(v1, v2, v3, attr) = [int(x) - 1 for x in line.split()[:4]]   # attr loses 1 here
tri_types[i] = attr + 1                                       # ...and gets it back
```

So the returned `tri_types` equals the file's attribute column exactly (verified on a real
tile: `array_equal(tri_types, mesh.attr) == True`). The `-1/+1` looks like an off-by-one and
is not — "fixing" it would shift every region attribute by one.

## 4. The two flavours in the wild

Both are read and written byte-exactly by this addon. The flavour is detected on import
(header text, coordinate precision, normal column count), stored on the object and
reproduced on export.

|  | Ortho4XP 1.30 (2023) | 1.4 fork / v1.31 ("v2") |
|---|---|---|
| header | `MeshVersionFormatted 1.30` (from `O4_Version.version`) | `MeshVersionFormatted 2` |
| coordinates | `'{:.7f}'` | `'{:.15f}'` |
| normals | `'{:.2f} {:.2f}'` — 2 columns | `'{:.2f} {:.2f} 0'` — 3 columns |
| elevation | `z = alt_m / 1e5` | identical |
| triangles | `.ele` line copied minus its first column, 4 columns | identical |
| line endings | whatever Python text mode produces — **CRLF on Windows** | CRLF on Windows |
| trailing `End` | absent | absent |
| same tile as a file | 220,135,767 bytes | 278,645,514 bytes (measured, same geometry) |

Everything else — absolute lon/lat, 1-based indices, the attribute bit mask, the `1e5`
elevation scale — is identical between them. The larger v2 file is purely the extra decimals.

**Why keep the source's flavour instead of normalising?** For a byte-exact round trip: an
untouched tile re-exports sha256-identical to its input, so a diff tells you exactly what
you changed and nothing else. The header text also decides one downstream branch —
`has_water = 7 if (mesh_version >= 1.3) else 3` at `O4_DSF_Utils.py:476` — but note that
both `1.30` (→ `1.3`) and `2` land on the same side of it, so that branch only separates
pre-1.3 files from both current flavours. Writing a file's header *down* to `1.30` or *up*
to `2` therefore does not change how step 3 treats the water bits, but it does change the
bytes.

## 5. The triangle attribute is a bit mask

The 4th triangle field is the Triangle4XP region attribute, whose names and values come
from `O4_Vector_Utils.py:44` (`Vector_Map.dico_attributes`):

| value | name | meaning |
|---|---|---|
| 0 | `DUMMY` | plain terrain, no marker |
| 1 | `WATER` | inland water / mask water |
| 2 | `SEA` | sea |
| 4 | `SEA_EQUIV` | water that X-Plane should treat as sea |
| 8 | `INTERP_ALT` | altitude interpolated from the ring (roads, levelled ribbons) |
| 16 | `RUNWAY` | runway surface, flattened |
| 32 | `TAXIWAY` | taxiway surface, flattened |
| 64 | `APRON` | apron surface, flattened |
| 128 | `HANGAR` | airport/hangar area, flattened |

The bits combine: `9` = `WATER|INTERP_ALT` (water that is also levelled), which really does
occur (50 triangles on the reference tile). Measured attribute histogram of
`zOrtho4XP_+27+089/Data+27+089.mesh` (2,250,375 verts / 4,491,416 tris):

| attr | meaning | triangles |
|---|---|---|
| 0 | `DUMMY` | 4,377,167 |
| 8 | `INTERP_ALT` | 61,349 |
| 1 | `WATER` | 52,185 |
| 32 | `TAXIWAY` | 419 |
| 16 | `RUNWAY` | 238 |
| 9 | `WATER\|INTERP_ALT` | 50 |
| 128 | `HANGAR` | 8 |

Two consequences worth knowing:

* **`attr >= 8` is not "the roads"** — `post_process_nodes_altitudes()` treats every
  triangle at or above `INTERP_ALT` as altitude-interpolated, so runways, taxiways, aprons
  and hangars share that class. Filter on the exact bit (`attr & 8`) when the question is
  about roads only, and remember a levelled water triangle also carries bit 8.
* The water test in step 3 is a mask, not an equality: `t = tri_types[i] & has_water`
  (`7` for both current flavours, i.e. `WATER|SEA|SEA_EQUIV`), and the result is remapped
  into X-Plane's 0/1/2 water codes.

## 6. Units, conventions, and what must stay put

| item | reality |
|---|---|
| coordinates | absolute WGS84 degrees, lon first |
| elevation | `alt_m = z * 1e5`; float32 storage in Blender gives ~0.1 mm of vertical rounding |
| indices | 1-based in the file, 0-based once parsed |
| winding | triangles are **counter-clockwise seen from above**, so Blender's face normals come out pointing up — no flipping needed (unlike DSF, which is clockwise) |
| normals | `nx, ny` only, `+z` implied; the third column is a placeholder zero |
| vertex 4th field | always `0` |
| line endings | CRLF (the file is written in Python text mode on a Windows build) |
| trailing `End` | never written; the reader tolerates it if you add one |
| empty sections | not emitted — a file with no normals simply has no `Normals` block |

Reading is deliberately tolerant: sections in any order, wrapped or padded lines, extra
blank lines, `#` comments, `End`, and both 2- and 3-column normals are accepted. Unknown
sections are skipped and reported rather than failing the import. Structural errors that do
fail: a missing `Vertices`/`Triangles` section or header, a non-1-based index, a triangle
referencing a vertex that does not exist, and count mismatches between normals/attributes
and their blocks.

## 7. Round-trip precision

Vertex positions land in Blender's **float32** vertices, so a round trip through Blender is
exact to about `3e-8` degrees:

* on a v2 tile (15 decimals), that shows up as 1.95 mm horizontally / 0.12 mm vertically;
* on a 1.30 tile (7 decimals), float32 is finer than the stored precision, so the exported
  file is **byte-identical** to the source.

Triangle indices, the attribute column and `nx, ny` are stored verbatim as mesh attributes
and survive untouched. Use *Recompute normals* on export after moving vertices — stale
normals are real data here and the DSF bakes them into the tile's normal planes.

## 8. Inspecting a `.mesh` by hand

`meshcodec.py` is numpy-only (no `bpy`), so it runs in any Python with numpy:

```python
import meshcodec as MC

m = MC.read_mesh("Data+27+089.mesh")
print(m.summary())
print(m.attribute_counts())
print(m.flavour, m.version_text, m.vert_fmt, m.normal_cols, m.crlf)
```

On the reference tile:

```
mesh v2 [v2]: 2250375 vertices, 4491416 triangles, 3-col normals,
lon 89.000000..90.000000 lat 27.000000..28.000000, alt 445.14..7294.66 m, CRLF,
attrs: DUMMY(4377167), INTERP_ALT(61349), WATER(52185), TAXIWAY(419), RUNWAY(238),
WATER|INTERP_ALT(50), HANGAR(8)
v2 2 {:.15f} 3 True
```

`m.alt_m` is already in metres, `m.tris` is 0-based, `m.normal_xyz` rebuilds full unit
normals, and `m.bounds` gives `(west, east, south, north)`. Writing it back unchanged with
`MC.write_mesh(path, m)` reproduces the source byte for byte (sha256 equal) — that is the
cheapest sanity check that your tooling did not disturb the file.

## 9. The other files in a tile folder

The `.mesh` is one of five intermediates. The other four are the *inputs* to the mesh build
and are not needed to edit a tile in Blender, but the shared conventions explain a lot of
what you see in a `.mesh` (notably where the attribute bits come from). All of this is from
the vector-map writer, `O4_Vector_Utils.py`.

| file | content | gotchas |
|---|---|---|
| `Data+27+089.node` | header `<n_nodes> 2 1 0`, then `id x y z` per node (`'{:.9f}'` ×3) | `x`/`y` are lon/lat **offsets from the tile's south-west corner** (0…1), `z` in **metres** — unlike the `.mesh`. Triangle4XP writes another `.node` of its own with more node attributes, same name, different columns (6) |
| `Data+27+089.poly` | header `0 2 1 0`; `<n_edges> 1` then `id n0 n1 marker`; then holes (`id x y`); then seeds (`id x y marker`) | `marker` is exactly the bit mask of §5, and it is what Triangle4XP turns into the `.ele`/`.mesh` attribute column |
| `Data+27+089.alt` | raw `float32` DEM raster, **no header**, grid size fixed by the DEM source chosen in Ortho4XP | for the SRTM/View DEM this is 3673×3673, covering −0.01…1.01 tile-relative lon/lat, i.e. a 1 % overlap all round (the reference file is 53,963,716 bytes = 3673² × 4). **Row 0 is the northernmost row**, nodata is −32768. Verified by comparing 118,402 `.node` altitudes against the raster: north-up gives a median error of 2 m, south-up 1669 m |
| `Data+27+089.apt` | Python `pickle` (not ASCII) holding the airport data: `icao`, `name`, `runway` (shapely polygon WKB), `runway_as_rel`, `taxiway`, plus numpy arrays | `pickle.load()` with shapely and numpy available; it is a cache for the vector map, not something the DSF build reads |

The `.node` altitude and the `.mesh` altitude of the same point are *close but not equal*
(2236.81 vs 2236.61 m at vertex 1 of the reference tile) — the vector map samples the DEM
with its own interpolator and Triangle4XP has another. Do not treat one as a check of the
other at metre precision.

## 10. Editing pitfalls

* **`z` is scaled by 1e5.** The `.mesh` is 1e5-scaled, the `.node` is not. Mixing them gives
  "terrain at 0.02 m".
* **`1` is `WATER`.** The attribute column is not off by one, despite `read_mesh_file`'s
  `-1`/`+1`.
* **Do not renumber vertices** unless you rewrite the triangle block — indices are 1-based
  positions in the vertex block, and the normal at index *i* belongs to vertex *i*.
* **Keep the normal block in sync.** Deleting or adding vertices without updating `Normals`
  (or leaving *Recompute normals* off after moving them) writes stale normals into the DSF.
* **The `.mesh` carries no terrain reference.** Materials and UVs on import come from the
  tile's `.ter` files, which the DSF build re-derives anyway — editing textures in Blender
  does not change the rebuilt tile. See [terrain-textures.md](terrain-textures.md).
* **One `.mesh` = one tile.** Exporting several tiles' objects into one file is refused;
  several objects of the *same* tile are merged (vertices are not welded).
* **N-gons are triangulated on export** via `calc_loop_triangles()`, so adding faces is safe.
* **Back the file up.** Re-running step 1–2 regenerates the `.mesh` and discards your edits.

## 11. Verifying a file

`verify/verify_mesh_format.py` checks a `.mesh` against Ortho4XP's own reader/writer
(byte-level field check plus a sha256 round trip), and
`verify/blender_roundtrip_test.py` runs the import/export through Blender headless. Usage,
arguments and measured results are in [verification.md](verification.md).