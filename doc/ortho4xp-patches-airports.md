# Patches and airport mesh density

What a `Patches/…/*.patch.osm` file can and cannot do for an airport, and what actually
controls how dense the mesh gets around one. Written for Ortho4XP users — no Blender needed,
though the last section uses this add-on to check the result.

Verified against the **Ortho4XP 1.40** code base (`src/O4_*.py`, the layout with a `src/`
folder). Function names below are stable across the 1.30/1.40 releases; line numbers are not,
so they are not quoted.

## TL;DR

| You want | Mechanism | Where |
|---|---|---|
| Denser vertices **around** an airport | `apt_curv_tol` / `apt_curv_ext` in the tile cfg — an airport must exist in the `.apt` file (i.e. in OSM) | step 2, `O4_Mesh_Utils.build_curv_tol_weight_map` |
| Exact **height** for a runway/apron the auto-encoder gets wrong | a `*.patch.osm` quad with `altitude_high` / `altitude_low` | step 1, `O4_Vector_Map.include_patches` |
| A **whole airport** flattened by hand, auto-encoder off | same patch file, named exactly like the airport key | step 1, `O4_Airport_Utils.encode_runways_taxiways_and_aprons` |
| An airport that **OSM does not have** | edit the cached `…_airports.osm.bz2` (or add it to OSM) | step 1, `O4_OSM_Utils.OSM_queries_to_OSM_layer` |

A patch **never** adds an airport and **never** refines terrain by itself. Those are two
different stages.

---

## 1. The two stages

Step 1 (`O4_Vector_Map.build_poly_file`) builds the vector map and writes
`Data+27+089.node` / `.poly` / `.alt` / `.apt`. Step 2 (`O4_Mesh_Utils.build_mesh`) hands
those to `Triangle4XP` to make `Data+27+089.mesh`.

Order inside step 1, for airport data:

```
OSM query aeroway=*  →  dico_airports  →  boundaries  →  Data+27+089.apt  (pickled)
                                     ├─ include_patches()          ← your .patch.osm files
                                     └─ encode_runways/taxiways/aprons()
```

So:

* `.apt` is written **before** patches are read, and step 2 refines only from `.apt`. A patch
  has no influence on where the mesh is refined.
* patches exist to fight the auto-encoder's heights, not its geometry — or to switch it off
  entirely and take over.

If you tick *Erase intermediate files* (or delete `Data+27+089.apt`) between steps 1 and 2,
step 2 cannot know where the airports are and prints
`WARNING: File … .apt is missing (erased after Step 1?), cannot check airport info`. Keep it.

## 2. Where patch files live

The directory is **two levels**, derived from the tile's integer lat/lon — not one folder per
`+LL+LLL`:

```
Patches/<lat rounded down to 10><lon rounded down to 10>/<+lat><+lon>/
```

| Tile | Directory |
|---|---|
| +27+089 (Paro) | `Patches/+20+080/+27+089/` |
| +39-078 (US east coast) | `Patches/+30-080/+39-078/` |
| -33+151 (Sydney) | `Patches/-40+150/-33+151/` |

`Patches/+39-078/…` is therefore **never read** — a common way to lose an afternoon. The
sign of the latitude comes first and both halves are zero-padded to a fixed width (`+27`,
`+089`).

Files in that folder:

| Entry | Meaning |
|---|---|
| `<key>.patch.osm` | an OSM XML file with patch ways (see §4). `<key>` must be the airport key, see §5 |
| `<subdir>/` | OBJ8 static objects; each `.obj` must start with `ANCHOR <lon> <lat> <alt> <heading>` (`<alt>` optional). The subdir name is also treated as an airport key — name it after the airport, not `objects` |

Everything else in the folder is ignored.

## 3. How airport neighbourhood density is actually decided

Step 2 builds a weight raster and passes it to `Triangle4XP`:

```python
# O4_Mesh_Utils.build_curv_tol_weight_map — only runs if
#   apt_curv_tol != curvature_tol  and  apt_curv_tol > 0
(xmin, ymin, xmax, ymax) = dico_airports[airport]["boundary"].bounds
x_shift = 1000 * tile.apt_curv_ext * m_to_lon(tile.lat)   # km → 1/1000 degree cells
...
weight_array[rowmin:rowmax+1, colmin:colmax+1] = tile.curvature_tol / tile.apt_curv_tol
```

Five facts follow, and each one explains a complaint you have probably had:

1. **It is the boundary's bounding box**, not the airport polygon. A long diagonal airport
   makes a big rectangle, and the terrain in the corners of that rectangle gets densified too.
2. The raster is 1001×1001 cells over the 1×1° tile (≈100 m per cell at mid latitudes), and
   `apt_curv_ext` (in **km**) grows the rectangle in every direction.
3. The stored value is the ratio `w = curvature_tol / apt_curv_tol`. Internally `Triangle4XP`
   multiplies the local curvature by `w` and refines while

   ```
   ratio = maxedge² · scalx² · maxcurv² / curv_tol²  > 1
   ```

   so the largest allowed edge length scales like `1/w`: **edges ≈ `curvature_tol /
   apt_curv_tol` times shorter**, i.e. triangle count grows with the square of that ratio.
   With the defaults (`curvature_tol=2`, `apt_curv_tol=0.5`) `w=4` → roughly 4× shorter edges
   and ~16× more triangles in the airport box. This is a ratio read off the code's own
   criterion, not a measured triangle count — the actual count also depends on the DEM, on
   `limit_tris` and on the mesh being capped by the DEM pixel size.
4. `apt_curv_tol >= curvature_tol` is legal and gives `w ≤ 1`: **coarser** terrain around the
   airport. Setting it "to make it safe" is how people lose airport detail.
5. Refinement stops when the triangle's largest edge is about one DEM pixel
   (`if (!outside && maxedge2 > pix_x * pix_y)`) — with SRTM-3 (~90 m) a much smaller
   `apt_curv_tol` buys nothing. `limit_tris` (millions of triangles) caps the whole tile.

### Why the pavement itself does not get denser

`Triangle4XP` bails out of the quality test for any triangle carrying region attribute ≥ 8:

```c
/* Refinement in INTERP_ALT tris is useless */
if (attribute >= 8) return;
```

Runway (16), taxiway (32), apron (64), hangar (128) interiors and anything inside a patch
quad (`INTERP_ALT` = 8) are in that class. Their vertex spacing comes from step 1 instead:

| Surface | Ring vertex spacing |
|---|---|
| taxiway | `refine_way(..., 20)` → one vertex every 20 m |
| apron (only with an `include` tag) | 15 m |
| runway | vertices where other ways cross the ring, no fixed spacing |
| patch quad | one vertex every `cell_size` (default 10 m) along its **long** sides |

So "the airport is one dense blob but the interior has huge triangles" is expected, not a bug.

## 4. Writing a patch quad

A patch is a plain OSM XML file. For levelling, a way must be a **closed 5-node quad**:

```xml
<?xml version='1.0' encoding='UTF-8'?>
<osm version='0.6' upload='false'>
  <node id='-1' lat='27.41331' lon='89.41676'/>
  <node id='-2' lat='27.41154' lon='89.41789'/>
  <node id='-3' lat='27.41300' lon='89.42020'/>
  <node id='-4' lat='27.41477' lon='89.41907'/>
  <node id='-5' lat='27.41331' lon='89.41676'/>   <!-- same as node -1 -->

  <way id='-100'>
    <nd ref='-1'/><nd ref='-2'/><nd ref='-3'/><nd ref='-4'/><nd ref='-1'/>
    <tag k='altitude_high' v='2244.479'/>
    <tag k='altitude_low'  v='2243.100'/>
    <tag k='cell_size'     v='10'/>
    <tag k='profile'       v='plane'/>
  </way>
</osm>
```

Node order is a hard requirement, not a convention:

| Node | Role |
|---|---|
| `node0` and `node3` | the **high** end (`short_high = way[-2:]`) |
| `node1` and `node2` | the **low** end (`short_low = way[1:3]`) |

Swap them and the slope runs the wrong way. Any node count other than 5, or a way that is not
closed, is skipped with a single level-1 message (`Wrong number of nodes or non closed way …`).

Tags:

| Tag | Effect |
|---|---|
| `altitude_high`, `altitude_low` | metres. Filled across the quad as `high - profile(t)·(high-low)`, so `high == low` is perfectly flat. **Both** are parsed inside one `try`: if `altitude_low` is missing or not a number, both fall back to the smoothed DEM means and the patch looks like it did nothing |
| `cell_size` | metres between ring vertices along the long sides, default `10`. Small values flood the `.node` file; constrained vertices are never removed later, `limit_tris` only limits new ones |
| `profile` | `plane` (default, linear), `spline` (smoothstep), `tanh` (with `steepness`, default 2) |
| `steepness` | only for `tanh` |

Way-level alternatives to a quad, for surfaces that are simply constant:

| Tag | Height |
|---|---|
| `cst_alt_abs=<m>` | that altitude |
| `cst_alt_rel=<m>` | mean DEM of the way + that offset |
| `var_alt_rel=<m>` | DEM per node + that offset |
| `altitude=<m>` | deprecated, same as `cst_alt_abs` |

And per node (any way in the patch file): `alt_abs=<m>` sets a vertex height, `alt_rel=<m>`
offsets it from the DEM. These are applied only to ways that did **not** take the quad branch.

A closed way with no altitude tags is still encoded: it becomes an `INTERP_ALT` ring flattened
to the DEM, i.e. a "level it but do not change its height" patch. A way that is **not** closed
becomes a `DUMMY` constraint line — useful to force a crease/ditch line into the mesh without
touching heights.

Geometry is not clipped to the tile (unlike OSM data, which goes through `cut_to_tile`). Keep
patch nodes inside the 1×1° tile: a node placed outside lands in the `.node` file outside the
unit square.

Patches are read **only in step 1**. Editing a patch after a tile has been meshed does nothing
until steps 1–2 are re-run (and step 3 for the DSF, or import/export in this add-on).

## 5. The file name decides much more than the name

```python
patches_list.append(pfile_name[:-10])          # "VQPR.patch.osm" → "VQPR"
...
for airport in dico_airports:
    if airport in patches_list:
        continue                                # whole airport skipped by the auto-encoder
```

`<name>.patch.osm` must equal the airport key **exactly**, and the key is produced in this
order (`O4_Airport_Utils.discover_airport_names`):

| Priority | Key | Notes |
|---|---|---|
| 1 | `icao` tag, first 4 characters | most common |
| 2 | `iata` tag, first 3 characters | only if no `icao` |
| 3 | `local_ref` tag | verbatim |
| 4 | `name` / `name:en` / `name:alt` | verbatim, truncated to 60 characters |
| 5 | a coordinate tuple | **no file name can match this** — see §6 |

Two outcomes:

* **Name matches** → the auto-encoder skips that airport completely: no runway/taxiway/apron
  rings, no least-square height fits, no `RUNWAY`/`TAXIWAY`/`APRON` attribute bits. Your patch
  is then the only source of pavement geometry and height. The airport keeps its boundary in
  `.apt`, so `apt_curv_tol` still densifies the terrain around it.
* **Name does not match** (`2W2_runways`, `VQPR_patch`, `VQPR.v2`, …) → both run. The patch ring
  keeps its `altitude_high/low` and the auto runway ring keeps its own fitted DEM height, and
  the two are stitched by one band of triangles: a ramp plus a crease at the runway edge. If you
  see a step along a runway edge, check the file name first.

## 6. An airport that OSM does not have

Without an entry in the airport data there is no `.apt` boundary, so no `apt_curv_tol`
refinement — no patch will change that. Two ways in:

1. **Add it to OSM** (proper way — it also helps everyone else). Needs `aeroway=aerodrome` on a
   closed way and `icao` / `name`, plus `aeroway=runway` ways inside it.
2. **Add it to the local cache** and let Ortho4XP recycle it. Step 1 reuses the cached answer
   instead of querying Overpass:

   ```
   OSM_data/<lat rounded to 10><lon rounded to 10>/<+lat><+lon>/<+lat><+lon>_airports.osm.bz2
   e.g. OSM_data/+20+080/+27+089/+27+089_airports.osm.bz2
   ```

   Delete that file (or leave it absent) and the next step 1 downloads a fresh copy from the
   configured Overpass server, overwriting your edit. Minimal content — only `node`, `way`,
   `nd` and `tag` lines are parsed:

   ```xml
   <?xml version='1.0' encoding='UTF-8'?>
   <osm version='0.6'>
     <node id='-1' lat='27.40000' lon='89.40000'/>
     <node id='-2' lat='27.41000' lon='89.40000'/>
     <node id='-10' lat='27.39900' lon='89.39800'/>
     <node id='-11' lat='27.41200' lon='89.39800'/>
     <node id='-12' lat='27.41200' lon='89.40400'/>
     <node id='-13' lat='27.39900' lon='89.40400'/>

     <way id='-100'>
       <nd ref='-1'/><nd ref='-2'/>
       <tag k='aeroway' v='runway'/>
       <tag k='name' v='My Strip'/>
       <tag k='width' v='30'/>
     </way>

     <way id='-200'>
       <nd ref='-10'/><nd ref='-11'/><nd ref='-12'/><nd ref='-13'/><nd ref='-10'/>
       <tag k='aeroway' v='aerodrome'/>
       <tag k='icao' v='XXXX'/>
       <tag k='name' v='My Strip'/>
     </way>
   </osm>
   ```

   How surfaces attach to an airport (`O4_Airport_Utils.attach_surfaces_to_airports`), in order:
   the `aeroway=runway|taxiway|apron|hangar` way is tested against every aerodrome polygon that
   exists (`intersects`) → take the first hit; otherwise the nearest airport within **3500 m**
   of the way's centre; otherwise the way's own `name` becomes a new airport key. That last
   branch is the reason a runway-only entry still works — and the reason to **always give a
   runway a `name`**, because the final fallback keys the airport by a coordinate tuple that no
   patch file name can ever match.

   Two silent size limits (`discard_unwanted_airports`): an aerodrome boundary smaller than
   **5000 m²** is dropped, and an airport without a boundary whose runway face is smaller than
   **2500 m²** is dropped. A 800×30 m grass strip (=24 000 m²) is fine; a 60×20 m helipad is not.

## 7. Checking that it worked

Step 1 log:

```
-> Dealing with airports
   Patching VQPR.patch.osm           ← the patch was found and read
   Auto-patched 2 runways and 14 pieces of taxiway
```

If the airport you patched is missing from the *Auto-patched* tally (or it reads
`Auto-patched 0 runways and 0 pieces of taxiway`), the file name matched and the auto-encoder
was suppressed — that is the intended effect of a full takeover.

Step 2 log:

```
-> Modifying curv_tol weight map according to runway locations.
```

Present = `apt_curv_tol` really is being applied. Absent with `apt_curv_tol` set = it equals
`curvature_tol`, or the `.apt` file is gone.

Airport keys the tile knows about:

```sh
strings Tiles/zOrtho4XP_+27+089/Data+27+089.apt | grep -x VQPR
```

Mesh contents: the `.mesh` triangle attribute is the same bit mask that step 1 wrote, OR-
accumulated, so test bits rather than comparing values:

| Bit | Region |
|---|---|
| 1 / 2 / 4 | water / sea / sea-equivalent |
| 8 | `INTERP_ALT` (patch interior, roads) |
| 16 / 32 / 64 / 128 | runway / taxiway / apron / hangar |

This add-on is the cheapest way to look: import `Data+…​.mesh`, and the sidebar (*N* → Ortho4XP)
shows the triangle attributes and the per-tile vertex/triangle counts. To compare density
before/after, count vertices inside a fixed box (e.g. the airport boundary bounding box) on two
imports of the same tile.

## 8. Pitfalls

1. Patch folder is two levels deep (`Patches/+20+080/+27+089/`); one level is silently dead.
2. The file name must equal the airport key — suffixing it (`_runways`, `_v2`) keeps the
   auto-encoder alive and gives you a ramp plus a crease.
3. A quad must have exactly 5 nodes with `node0 == node4`, high end first-and-fourth. Anything
   else is skipped with one line of log.
4. `altitude_high` without a numeric `altitude_low` disables the patch's heights entirely
   (silent DEM fallback).
5. Patches do not densify anything: interiors of quads and pavement carry attribute ≥ 8 and are
   never refined. Density comes from `apt_curv_tol` (terrain around the airport) and from the
   10–20 m ring spacing (the pavement outline).
6. The `apt_curv_tol` mask is the boundary **bounding box** plus `apt_curv_ext` km — off-airport
   terrain in that rectangle gets denser too, and a diagonal airport makes a large rectangle.
7. `apt_curv_tol >= curvature_tol` makes the mesh **coarser** there, it is not a no-op.
8. Losing `Data+….apt` (erase-intermediates, manual cleanup) loses all airport refinement in
   step 2, even though the patch polygons themselves still apply.
9. A tiny `cell_size` (1–2 m) explodes the `.node` file size; those vertices are constraints and
   `limit_tris` will not remove them.
10. Airport data is cached: an edit to `…_airports.osm.bz2` is used verbatim, but an *absent*
    file is re-downloaded, and a stale file hides upstream OSM changes indefinitely.

## See also

* [ortho4xp-mesh-format.md](ortho4xp-mesh-format.md) — the `.node` / `.poly` / `.alt` / `.apt`
  files, the triangle attribute mask, and the `.mesh` format itself.
* [import-and-export.md](import-and-export.md) — what the sidebar reports about attributes and
  texture stats after an import.
* [verification.md](verification.md) — how the numbers in this repository are checked.
* Ortho4XP itself: <https://github.com/oscarpilote/Ortho4XP>
