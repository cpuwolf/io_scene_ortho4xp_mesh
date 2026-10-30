# OBJ8 mesh patches

An OBJ8 file in a tile's patch folder turns its triangles into **terrain**. Each `VT` becomes a
mesh vertex at a fixed altitude, each triangle becomes a levelled, non-refined patch — the object
contributes geometry and height, nothing else. No texture, no UV, no model.

Read `ortho4xp-patches-airports.md` first if the terminology is new; this page is only the OBJ8
route.

## Where the file goes

```
Patches/<lat floored to 10><lon floored to 10>/<+LL+LLL>/<any folder>/<any name>.obj
Patches/+20+080/+27+089/pad/pad.obj          ← tile +27+089
```

* The tile folder is **two levels** deep (`O4_File_Names.long_latlon`). Tile `+27+089` lives in
  `+20+080/`.
* The object must be in a **subfolder**. A loose `*.obj` in the tile folder is ignored silently.
* The subfolder name is recorded as a patch name: if it equals an airport key (ICAO/IATA/`name`),
  the automatic runway/taxiway/apron encoder is skipped for that airport — the OBJ then owns the
  whole area.

## The file

* **First line must contain `ANCHOR`.** A normal OBJ8 export starts with `I / 800 / OBJ` and is
  skipped with `is missing and ANCHOR in first line, skipping it` — move the `ANCHOR` up, or add
  one as line 1.
* `ANCHOR <lon> <lat> <alt> <heading>` — `alt` is absolute metres (MSL). The shorter
  `ANCHOR <lon> <lat> <heading>` takes the altitude from the DEM instead.
* Parsed lines: `VT x y z …`, `IDX`, `IDX10`, `TRIS <offset> <count>`. Everything else
  (`TEXTURE`, `ATTR_*`, `POINT_COUNTS`) is ignored.
* Metres, local X-Plane axes: `x` east, `y` up, `z` south. Heading 0 puts `+x` due east; heading
  rotates in the x/z plane. Vertex altitude = `VT y + ANCHOR alt`, so author the object at real
  heights — or use the 3-value `ANCHOR` (base altitude from the DEM) and keep `y` relative.

A 100 × 100 m flat pad at 1500 m:

```
ANCHOR 89.4000000 27.4000000 1500.0 0.0
VT 0 0 0 0 1 0 0 0
VT 100 0 0 0 1 0 0 0
VT 100 0 100 0 1 0 0 0
VT 0 0 100 0 1 0 0 0
IDX 0 1 2
IDX 0 2 3
TRIS 0 6
```

## The anchor's four numbers

The anchor is an arbitrary local origin — Ortho4XP only uses it to *place* the geometry, so any
point works as long as every `VT` is measured from it. What matters is that the four numbers and
the `VT` coordinates agree:

```
vertex altitude = VT y + ANCHOR alt      (metres MSL)
vertex position = anchor lon/lat + (VT x east, VT z south), rotated by ANCHOR heading
```

So `alt` does not have to be the real ground height; only the **sum** is the altitude. Two
consistent styles:

| style | `ANCHOR alt` | `VT y` |
|---|---|---|
| absolute | `0` | the vertex's own MSL altitude |
| relative | the anchor's ground altitude | height above the anchor |

### Taking it from a tile imported in Blender

An imported `.mesh` carries the projection it was built with in the object's `mesh_georef`
custom property — tile **centre** and the two metres-per-degree factors (measured on `+27+089`:

```
lon0 89.5    lat0 27.5
m_per_deg_lat 111319.49079327357      m_per_deg_lon 98741.59427751697
```

The imported coordinates are metres from that centre, **not** degrees, so:

```
lon = lon0 + X / m_per_deg_lon
lat = lat0 + Y / m_per_deg_lat
```

The simplest consistent anchor is the tile centre itself with altitude 0, because then the
local coordinates are the Blender ones unchanged:

```
ANCHOR  lon0  lat0  0  0
VT      x = vertex X            (east)
        y = vertex Z            (MSL altitude)
        z = −vertex Y           (obj z is south-positive, Blender Y is north-positive)
```

Measured on a real vertex of `+27+089`: `X −7893.755, Y −9909.625, Z 2236.61` with
`ANCHOR 89.5 27.5 0 0` gives `lon 89.420056400 lat 27.410980300 z 2236.61` against
`89.420056432 / 27.410980324 / 2236.61` in the `.mesh` — 3e-8°, i.e. the 7-decimal rounding
`keep_obj8` applies itself (~3 mm).

Keep the anchor at the tile centre if you can: `keep_obj8` scales longitudes by
`cos(anchor lat)` while the imported frame used `cos(lat0)`, so an off-centre anchor drifts by
~(1 − cos(lat_anchor)/cos(lat0)) × distance — centimetres for a patch a few hundred metres
across, metres when the offset runs into kilometres.

## What Ortho4XP does with it

1. `O4_Vector_Map.keep_obj8` maps every `VT` to a node at the tile-relative lon/lat and the
   absolute altitude, then turns every triangle into three `INTERP_ALT` edges plus a seed.
2. `Triangle4XP` fills those triangles with attribute bit 8 and **never refines** them, so the
   triangle count is exactly the object's.
3. `post_process_nodes_altitudes` sets those vertices to the altitude they were inserted with,
   zeroes their normals: flat shading.
4. The triangles get the tile's orthophoto like any other terrain — the object's own texture is
   dead weight.

## Rules that decide whether it works

| Rule | Consequence |
|---|---|
| Height field only: nodes are keyed on `(x, y)` | a vertical face (same x/y, different z) collapses to the first z. Roofs, pads, ramps — no walls, no overhangs |
| First z wins per coordinate | a `.patch.osm` way, or an object loaded earlier, keeps its altitude for a shared vertex |
| `TRIS` is mandatory | a file with `ANCHOR` but no `TRIS` raises `UnboundLocalError` and kills step 1 (no try/except there) |
| No clipping to the tile | vertices outside the 1° box land outside the unit square in the `.node` file |
| Rebuild required | step 1 (`.node`/`.poly`) and step 2 (`.mesh`); a patch does nothing to an existing mesh |

## Check the result

Import the rebuilt `Data+27+089.mesh` with this add-on and turn on *Material slot per triangle
attribute*: the patch shows up as `INTERP_ALT` faces, and its vertices sit at the altitudes you
typed. If nothing shows up, the file was never read — check the step-1 log for
`Including OBJ8 objects from …`.
