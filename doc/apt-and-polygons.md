# apt.dat pavement and DSF polygons

Two separate importers, both edge/pavement oriented: one reads an airport's pavement out
of `apt.dat`, the other reads the polygons a DSF draws over its terrain. Neither needs the
other, and neither needs the DSF terrain import to work.

## apt.dat pavement — `File → Import → X-Plane apt.dat (pavement, .dat)`

Reads apt.dat 12.00, triangulates the boundaries and drapes them on the tile that is
already in the scene. 8.50/10.50-era files without `110` rows contribute nothing.

The row model, all six node types:

| row | meaning |
|---|---|
| `110` | pavement header: surface type, smoothness, texture orientation, name |
| `111` | node |
| `112` | node + the control point of the edge leaving it (quadratic Bezier) |
| `113` | node, closes this loop |
| `114` | node + control point, closes this loop |
| `115` / `116` | node ending a string (line features); `116` also has a control point |

A pavement is **one or more loops**: the first is its outline, the rest are holes cut out
of it (grass islands, buildings, parked stands). Global Airports has 9,821 chunks with
holes and 35,938 holes in total, so a reader that closes the chunk on the first `113` and
ignores what follows paints pavement over every one of them.

| option | meaning |
|---|---|
| `Airports` | comma-separated identifiers (`KLVM, 29S`). Empty = the airports inside the scene's tile, or every airport in the file when there is no tile |
| `Use the scene's tile` | when a tile is in the scene, take the airports inside it and drape on its terrain |
| `Airport limit` | safety valve: without identifiers and without a tile, refuse a file holding more airports than this (the worldwide apt.dat has ~38,000) |
| `Grid (m)` | boundary densification step; triangles are only split while they are still much larger than this (16x its square), so an apron interior is not turned into a 20 m grid |
| `Lift (m)` | how far above the terrain the pavement sits (default 0.12 m) |

Loops are bridged into the outline and ear-clipped; a triangle whose centre falls inside
a hole, or outside the outline, is dropped, so the hole stays empty even when the bridge
slit leaves the clipper guessing. Checked by sampling 4,000 points per chunk: 0.00 %
of the inside points uncovered and 0.00 % of the outside points covered, on tiles with up
to 15 loops.

Where the pavement ends up:

- **A tile in the scene covers these airports** (a DSF import or an Ortho4XP `.mesh` import
  — both store the georeference the importer needs) → each vertex is ray-cast onto that
  mesh and lifted, so the pavement follows the curved surface.
- **Identifiers given** → those airports, whatever tile is open and whatever the file's
  other 37,000 airports are.
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
