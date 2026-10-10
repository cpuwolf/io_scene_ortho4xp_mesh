# Ortho4XP Mesh Import/Export (Blender addon)

Read and write Ortho4XP tile meshes — `Data+50-125.mesh` etc. — in Blender 4.0+
(tested on Blender 5.2.2 LTS, macOS).

Install: this folder *is* the addon. Either copy it into
`<blender config>/scripts/addons/` (macOS: `~/Library/Application Support/Blender/<ver>/scripts/addons/`)
and enable **Ortho4XP Mesh Import/Export** in *Preferences → Add-ons*, or enable it
from the ZIP via *Install from Disk…*.

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

Quirks the addon knows about (all verified against a real tile, see *Verification*):

| item | reality |
|---|---|
| elevation | `z` is in units of **100000 m**: `alt_m = z * 1e5` (`read_mesh_file` multiplies, `write_mesh_file` divides) |
| vertex 4th field | MEDITS "reference"; Ortho4XP always writes `0` |
| 3rd normal field | a literal `0` placeholder — only `nx, ny` are real (X-Plane's DSF stores two components and implies +up), so the true normal is `(nx, ny, sqrt(1-nx²-ny²))` |
| triangle 4th field | Triangle4XP region **bit mask**, not off by one: `1` WATER, `2` SEA, `4` SEA_EQUIV, `8` INTERP_ALT, `16` RUNWAY, `32` TAXIWAY, `64` APRON, `128` HANGAR, `0` DUMMY. `read_mesh_file` calls it `tri_types` |
| version | hard-coded to `2` by the writer, while `build_dsf` branches on it: `has_water = 7 if version >= 1.3 else 3` |
| line endings | the file is written in Python text mode, so a Windows build emits **CRLF** (this is what tiles on disk have) |
| trailing `End` | not written by Ortho4XP (MEDITS allows it; the reader tolerates it) |
| winding | triangles are **counter-clockwise seen from above**, so Blender's face normals come out pointing up — no flipping needed (unlike DSF, which is clockwise) |

## Import

* Geometry is projected into a local tangent plane about the tile centre, in metres:
  `X = east, Y = north, Z = up`. The projection is stored on the object as
  `mesh_georef` with the same keys the `io_scene_dsf` addon uses, so a `.dsf` tile
  and the `.mesh` it came from import to the same place and line up in one scene.
* `mesh_attr` (INT, FACE) keeps every triangle's region attribute, and
  `mesh_normals` (FLOAT_VECTOR, POINT) keeps `nx, ny` — both are written back
  verbatim on export. Optional *Custom split normals* also makes Blender shade with
  them; *Material slot per triangle attribute* gives water/sea/runway/… their own
  viewport-coloured slot so the cuts are visible at a glance.
* Vertex positions land in Blender's float32 vertices, so the round trip is exact to
  ~3e-8 degrees — measured 1.95 mm horizontally / 0.12 mm vertically on a full
  1×1 degree tile — not bit-exact. (The DSF point pools quantize to 0.2 m, so this
  is far below what the pipeline can represent.)

## Export

* Writes `mesh_attr` back as the 4th triangle column, `nx, ny` back as the normal
  components, preserves `MeshVersionFormatted`, and by default keeps the source
  file's line endings, so an untouched tile comes back byte-for-byte the same size
  and identical in everything except the float32 vertex rounding.
* N-gons and quads are triangulated on the fly via `calc_loop_triangles()`.
* **Recompute normals**: tick after moving/adding vertices. The DSF builder bakes
  these two components into the tile's normal planes, so stale normals survive into
  the DSF otherwise.
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

Results on the reference tile `sample/Data+50-125.mesh`
(2,292,352 vertices / 4,575,096 triangles, 288,705,257 bytes, CRLF):

* codec: `read_mesh` → `write_mesh` reproduces the file **sha256-identical**
* byte-level conformance to `write_mesh_file()`: 0 errors
* repo `read_mesh_file()` agrees with the codec on every vertex/normal/triangle
* Blender round trip: triangle indices identical, attributes identical, normals
  identical, positions within 2 mm horizontally / 0.12 mm vertically on a full
  1×1 degree tile (float32 vertex storage only);
  2.3 M vertices + 4.6 M triangles import+export in ~50 s, ~3.5 GB peak RSS

## Layout

```
__init__.py     operators, georeference, material slots, sidebar panel
meshcodec.py    dependency-free (numpy only) reader/writer + OrthoMesh
verify/         verification scripts (format + Blender round trip)
```