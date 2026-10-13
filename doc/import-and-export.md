# Import and export

Two operators, plus a sidebar panel for the things you need after the import.

* `File → Import → Ortho4XP Mesh (.mesh)` — `import_scene.ortho4xp_mesh`
* `File → Export → Ortho4XP Mesh (.mesh)` — `export_scene.ortho4xp_mesh`
* 3D viewport sidebar (`N`) → tab **Ortho4XP** → `MESH_PT_ortho4xp_panel`

A failed parse reports *why* (missing `Vertices`/`Triangles` section, non-1-based index,
count mismatch, …) and cancels instead of importing a half mesh. A failure while attaching
terrain textures is a warning, not an error: you keep the geometry.

## Import

### Options

| Option | Default | Effect |
|---|---|---|
| *Vertex normals* | Keep as attribute (fast) | `Keep as attribute` stores `nx, ny` in a `mesh_normals` attribute and leaves shading to Blender's geometric normals; `Custom split normals` additionally applies them so the shading matches the tile; `Ignore` drops them and export recomputes |
| *Material slot per triangle attribute* | on | Gives every triangle attribute (water, sea, runway, …) its own material slot with a viewport colour, so water cuts and flattening are visible at a glance. Skipped when terrain textures are imported — a face has one material slot and the orthophoto wins |
| *Terrain textures (.ter)* | on | Read the tile's `.ter` cells, give every cell a material and UV-map each face over its own cell. Turn off for a geometry-only import |
| *Terrain folder* | empty | Where the `.ter` files are; empty = the `terrain` folder next to the `.mesh` |
| *Max textures* | 0 (all) | Load images for at most N cells, biggest first. Materials, metadata and UVs are still created for every cell — only the image is skipped |
| *Max texture size* | 0 (native) | Decode and downscale each image to at most N px. The whole-tile memory knob |
| *Smooth shading* | on | Shade the tile smooth; faceted otherwise |
| *Project textures along Z (fixed map)* | on | Feed every texture from a Z-axis projection of the vertex positions instead of the baked UV layer, so the orthophoto stays nailed to the map while you drag vertices |
| *Seams from UV islands* | on | Mark the borders between UV islands as seams (needs the UV layer, so skipped on a geometry-only import) |
| *Report non-standard input* | on | Report skipped sections, non-zero vertex references and line endings |

*Max textures* / *Max texture size* are the memory knobs — see
[terrain-textures.md](terrain-textures.md#memory-tex_limit-tex_max_px) for the measured numbers.

### What lands in the scene

* **Geometry** is projected into a local tangent plane about the tile centre, in metres:
  `X = east, Y = north, Z = up`. The projection is stored on the object as `mesh_georef`,
  with the same keys the `io_scene_dsf` addon uses, so a `.dsf` tile and the `.mesh` it came
  from import to the same place and line up in one scene. The object is named after the file
  stem (`Data+27+089`).
* **`mesh_attr`** (INT, FACE) keeps every triangle's region attribute and **`mesh_normals`**
  (FLOAT_VECTOR, POINT) keeps `nx, ny` — both are written back verbatim on export. Optional
  *Custom split normals* also makes Blender shade with them.
* **UVs** land in `XPlaneProjected` (same layer name as the DSF addon). They are computed per
  loop from the cell that owns the face, so neighbouring cells sample their own texture;
  `BASE_TEX_NOWRAP` cells use `EXTEND`, so a single vertex that strays up to one mesh edge
  outside its cell clamps instead of wrapping.
* **Seams from UV islands** marks the cell borders as seams, which is what
  `UV > Seams from Islands` does by hand: two neighbouring faces of different cells hand
  their shared edge different UVs, so the seams fall exactly on the cell borders and
  `Select Linked` / `Rip` / the UV editor's island tools work a whole cell at a time. The
  add-on computes it on the mesh datablock instead of entering edit mode — on the 4.49 M
  triangle tile the menu item costs an 8.6 s edit-mode round trip and a 3.2 GB BMesh copy,
  this costs 7.3 s and no second copy of the mesh. Skipped without the UV layer, i.e. on a
  geometry-only import. `verify/seams_test.py` checks it against the operator, edge for edge.
* **Tile metadata** for the panel: `mesh_meta` (flavour, version text, line endings,
  attribute counts, source path), `mesh_tex_stats` and `mesh_seams` / `mesh_zproj`.
* **Precision:** vertex positions land in Blender's float32 vertices, so the round trip is
  exact to ~3e-8 degrees. On a v2 tile (15 decimals) that shows up as 1.95 mm horizontally /
  0.12 mm vertically; on a 1.30 tile (7 decimals) it is below the stored precision and the
  exported file is **byte-identical** to the source.
* **Viewport far plane:** a tile spans a whole degree (~111 km) while Blender's viewport far
  plane defaults to 1000 m, which clips all of it away. Import therefore sets *Clip End* to
  `1e11` on **every 3D view of every workspace screen** — clip end lives on the viewport, not
  on the scene, so only fixing the one in front of you would make the others snap back to
  1000 m on the next workspace switch. Blender stores it as float32, whose spacing out there
  is ~8 km, so the panel reads back `99999997952` — far enough past the ~111 km of a tile
  either way. Cameras keep their own *Clip End* (100 m default), so a camera render still
  needs it raised by hand.

## Export

### Options

| Option | Default | Effect |
|---|---|---|
| *Selected objects only* | off | Otherwise every mesh object carrying a `mesh_georef` is written |
| *Georeference* | From the object (`mesh_georef`) | `Custom origin` uses *Tile west longitude* / *Tile south latitude* below, for a mesh modelled from scratch in Blender |
| *Tile west longitude* / *Tile south latitude* | −1 / 0 | South-west corner of the 1×1° tile, when *Custom origin* is chosen |
| *File flavour* | Same as the source file | `Ortho4XP 1.4 (v2)` or `Ortho4XP 1.30` force the other flavour. See [ortho4xp-mesh-format.md](ortho4xp-mesh-format.md#4-the-two-flavours-in-the-wild) |
| *Line endings* | Same as the source file | CRLF (what a Windows Ortho4XP produces) or LF |
| *Recompute normals* | off | Derive normals from the edited geometry instead of writing the imported `nx, ny` back. Tick after moving/adding vertices — the DSF builder bakes these two components into the tile's normal planes |
| *Zero third normal component* | on | Write the literal `0` third normal column, as 1.4 does. Only affects the v2 flavour |

### Behaviour

* Writes `mesh_attr` back as the 4th triangle column and `nx, ny` back as the normal
  components, by default keeping the source file's line endings and flavour.
* N-gons and quads are triangulated on the fly via `calc_loop_triangles()`.
* One `.mesh` = one 1×1° tile. Objects from different tiles are refused; several objects of
  the *same* tile are merged (vertices are not welded).
* The file is written to `path + ".tmp"` and then `os.replace()`d over the target, so a
  crash mid-write cannot leave a truncated tile behind.
* Compared to the imported tile, a round trip is byte-identical when nothing was edited —
  that is the fastest way to confirm your edits are the only difference.

## Sidebar panel

`N` → **Ortho4XP**. With no tile selected it only offers the two operators; with one
selected it shows:

* tile name, `mesh_georef` bounds and vertex/face counts;
* source flavour and line endings, and the triangle attribute counts by name;
* terrain texture stats: `cells used by faces`, `faces textured` / `without a cell`,
  `texture(s) loaded` (with the cap), `sea overlay cell(s)`, the UV layer and how many loops
  it covers, and the `.ter` folder that was scanned;
* the number of seam edges marked from the UV islands;
* **Seams from Islands** — the same computation as the import option, for after the UVs have
  been edited. It acts on every imported tile, like the buttons next to it;
* **Texture projection** — the current mode (*Z axis, fixed on the map* / *baked UV layer*)
  with **Fix to map** / **Use UVs** to switch, and **Projected UVs** to re-anchor the baked
  `XPlaneProjected` layer to the current geometry;
* a reminder that vertex coordinates are metres: X east, Y north, Z up.

`verify/zproject_test.py` covers that section end to end, including a render that proves the
texture really stays put while vertices move — see [verification.md](verification.md).