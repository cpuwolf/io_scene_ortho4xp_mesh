# apt.dat pavement and DSF polygons

Two separate importers, both edge/pavement oriented: one reads an airport's pavement out
of `apt.dat`, the other reads the polygons a DSF draws over its terrain. Neither needs the
other, and neither needs the DSF terrain import to work.

## apt.dat pavement — `File → Import → X-Plane apt.dat (pavement, .dat)`

Reads apt.dat 12.00 (`110` pavement chunks, `111` / `112` / `113` nodes, quadratic Bezier
edges sampled), triangulates the rings and drapes them on the tile that is already in the
scene. 8.50/10.50-era files without `110` rows simply contribute nothing.

| option | meaning |
|---|---|
| `Airports` | comma-separated identifiers (`KLVM, 29S`). Empty = the airports inside the scene's tile, or every airport in the file when there is no tile |
| `Use the scene's tile` | when a tile is in the scene, take the airports inside it and drape on its terrain |
| `Airport limit` | safety valve: without identifiers and without a tile, refuse a file holding more airports than this (the worldwide apt.dat has ~38,000) |
| `Grid (m)` | boundary densification step; also the size the ear-clipped triangles are subdivided towards |
| `Lift (m)` | how far above the terrain the pavement sits (default 0.12 m) |

Where the pavement ends up:

- **A tile in the scene covers these airports** (a DSF import or an Ortho4XP `.mesh` import
  — both store the georeference the importer needs) → each vertex is ray-cast onto that
  mesh and lifted, so the pavement follows the curved surface.
- **No tile, or a tile that does not cover them** → flat at each airport's own apt.dat
  elevation. The console names the tile the pavement actually belongs to, e.g.
  `the pavement is at 61.3837, -148.3670 (tile +61-149) — but the scene holds …`.

Airports with no `1302 datum_lat/lon` (legal in 12.00, and what VSL Base-8 AU's apt.dat
does) are located from the mean of their own geometry instead of being dropped.

Surface types are named and coloured per airport surface code; codes X-Plane 12 writes but
never documented (20, 23) are reported by number and drawn as asphalt rather than dropped.

## DSF polygons — part of `File → Import → X-Plane DSF (.dsf)`

The DSF `POLY` atom and the four polygon commands (`12` POLYGON, `13` POLYGON RANGE,
`14` NESTED POLYGON, `15` NESTED POLYGON RANGE) are read; the class comes from the
`SETDEF` command that precedes each one, looked up in the `DEFN/POLY` definition table.

- `.pol`, `.fac` and legacy class names → **closed outlines**
- `.lin` line features → **open polylines**
- forest areas (`.for`) → skipped unless *Include forest areas* is ticked

The result is a separate `<dsf>_polygons` object with vertices and edges only: no faces, no
material, no texture. It is draped on the terrain by ray-cast (0.05 m lift); a ring that
hits nothing stays at the tile base instead of floating.

A DSF with polygons but no terrain (a scenery package's own overlay, the Global Airports
layer) imports its outlines too — draped on a tile in the scene only when that tile
actually covers the DSF.

### `<dsf>.patch.osm`

Every import that brings polygons also writes OSM XML next to the DSF (unless *Write .osm*
is off):

- one `<way>` per winding, closed by repeating the first node for area polygons, open for
  `.lin`
- tags: `xplane_class` (the path exactly as the DSF stores it), `xplane_param`,
  `xplane_linear=yes` on line features, and for nested polygons `xplane_winding=outer|hole`
  plus `xplane_polygon=<group>`
- coordinates are WGS84 degrees at 7 decimals, and a `<bounds>` element leads the file so
  JOSM zooms to the tile

Measured: `+45-111` gives 916 ways / 11,197 nodes (912 closed, 4 linear), 1.0 MB; the VSL
Base-8 AU overlay `+61-149` gives 19 ways / 517 nodes, 42 kB.
