# Distributed as a Blender *extension*: the metadata that used to live in the
# legacy header dict (name, author, version, minimum Blender version, license)
# is now in blender_manifest.toml.  The legacy dict is deliberately gone --
# Blender treats a package as a legacy add-on, ignoring the manifest, as soon as
# any .py in the zip still carries that key name, so it must not come back here
# or in any other shipped module.

"""
Ortho4XP tile mesh (.mesh) importer/exporter for Blender.

What the file is
----------------
The .mesh file is Ortho4XP's *intermediate* triangle mesh for one 1x1 degree tile:
`O4_Mesh_Utils.write_mesh_file()` writes it from the Triangle4XP output, and
`O4_Mesh_Utils.read_mesh_file()` (used by `O4_DSF_Utils.build_dsf`) turns it into
the DSF that X-Plane actually loads.  X-Plane never reads the .mesh itself, so
this is the format to edit when a tile's water cuts, coastlines or airport
flattening need fixing before rebuilding the DSF.

Format (MEDITS, ASCII, one element per line)
--------------------------------------------
    MeshVersionFormatted 2
    Dimension 3
    <blank>
    Vertices
    <N>
    <lon> <lat> <z> 0
    <blank>
    Normals
    <N>
    <nx> <ny> 0
    <blank>
    Triangles
    <T>
    <v1> <v2> <v3> <attr>

Two flavours of that format are in the wild, and this addon reads and writes both
byte-exactly:

    Ortho4XP 1.30 (2023)                1.4 fork ("v2")
    MeshVersionFormatted 1.30           MeshVersionFormatted 2
    '{:.7f}' coordinates                '{:.15f}' coordinates
    '%.2f %.2f'   normals (2 columns)   '%.2f %.2f 0' normals (3 columns)

The flavour, line endings and coordinate precision are detected on import, stored
on the object and reproduced on export by default ("File flavour: same as source").

Quirks that this addon knows about (all verified against real tiles):
  * `lon`/`lat` are absolute WGS84 degrees, `z` is the elevation in units of
    100000 m (`alt_m = z * 1e5`), and triangle indices are 1-based.
  * the 4th vertex field is a MEDITS "reference": always 0 in Ortho4XP output.
  * the 3rd normal field is a literal `0` placeholder -- only `nx`, `ny` are real
    (X-Plane stores two normal components and implies +up), so the true normal is
    (nx, ny, sqrt(1-nx^2-ny^2)).  Ortho4XP 1.30 omits the column entirely.
  * `attr` is the Triangle4XP region bit mask copied from the .ele file:
    1 WATER, 2 SEA, 4 SEA_EQUIV, 8 INTERP_ALT, 16 RUNWAY, 32 TAXIWAY, 64 APRON,
    128 HANGAR, 0 DUMMY.
  * `MeshVersionFormatted` is not free-form: `build_dsf` branches on it
    (`has_water = 7 if version >= 1.3 else 3`).
  * a Windows build writes CRLF line endings, and there is no trailing `End`.

Geometry handling
-----------------
Longitude/latitude/elevation are projected into a local tangent plane about the
tile centre, in metres, giving Blender coordinates

    X = east, Y = north, Z = up

(the .mesh triangles are wound counter-clockwise seen from above, so Blender's
face normals come out pointing up -- no flipping is needed, unlike the DSF format).
The projection is stored on the object as `mesh_georef`, using the same keys as the
X-Plane DSF addon, so a .dsf tile and the .mesh it came from import to the same
place and line up in one scene.  Vertex positions live in float32, so the round trip is
exact to ~3e-8 degrees (measured 1.95 mm horizontal / 0.12 mm vertical on a full
1x1 degree tile) rather than bit-exact; the DSF point pools quantize to 0.2 m, so
this is far below what the pipeline can represent.

Terrain textures (.ter)
-----------------------
A .mesh carries no terrain reference, so the orthophotos are rebuilt from the tile:
Ortho4XP writes one `.ter` per ortho cell in the tile's `terrain/` folder, and each
one declares the footprint its texture covers:

    LOAD_CENTER <lat> <lon> <size_m> <tex_px>
    BASE_TEX_NOWRAP ../textures/<cell>.dds

Those footprints are a spatial index: every face is assigned to the cell whose
footprint contains its centroid, gets that cell's material, and is UV-mapped once
over that footprint (the cell centre lands on 0.5, 0.5) -- which is what X-Plane
does when it renders the same tile.  The tiling itself comes from the *observed*
lattice rather than from `size_m`: measured on a real tile the declared size is
~0.1% narrow in longitude and ~0.6% tall in latitude versus the actual centre
spacing, which would leave bands of untextured faces along the borders of every
patch of a finer zoom level.  A tile that mixes zoom levels (ZL17 with ZL19
patches) is handled by letting the finer cells win, and a `_sea_overlay.ter` wins
over the plain `.ter` of the same footprint because that is the definition
Ortho4XP's own DSF uses for those cells.

Note the consequence for materials: a face has exactly one material slot, so
"Material slot per triangle attribute" is skipped when textures are imported (the
`mesh_attr` attribute is written regardless, and still drives the export).

Texture projection (why the texture does not stretch)
----------------------------------------------------
Each cell's mapping is an affine map from position to UV, and it is written down
*rather than* baked into the mesh -- `uv = position * scale + offset`, with the
position in the object's local metres:

    u = a_x * x + b_x        v = a_y * y + b_y        (terrain_tiles.cell_uv_affine)

Two things consume that map:

  * the material's Z-axis projection: `Texture Coordinate (Object)` -> `Vector Math
    (Multiply Add)` -> the Vector input of every image texture.  Because the UV is
    computed from where the surface *is*, dragging a vertex slides the geometry
    under a texture that stays nailed to the map -- the texture does not stretch,
    and the view matches what X-Plane renders from the same tile.  Object space,
    so moving the tile as a whole moves its map with it.
  * the baked `XPlaneProjected` UV layer, written once at import: the same numbers,
    for Workbench's solid view, the UV editor and anything else that reads UVs.
    That layer goes stale when vertices move; the *Projected UVs* button recomputes
    it from the current positions.

Toggle with the *Fix to map* / *Use UVs* buttons in the sidebar panel, or with
"Project textures along Z" on import (off = just the baked layer, the pre-1.2
behaviour).  A rendered view ignores the UV layer entirely, so an edited tile can
be inspected with the projection before the layer is refreshed.

Round-trip
----------
The triangle attributes and vertex normals are stored on the import (a `mesh_attr`
face attribute and a `mesh_normals` point attribute) and written back verbatim, so
exporting an untouched tile reproduces the original file: the codec itself is
byte-exact (verified on a 288 MB / 2.29 M-vertex / 4.58 M-triangle v2 tile and a
119 MB / 1.25 M-vertex / 2.48 M-triangle 1.30 tile, both sha256-identical), and the
Blender round trip differs only by float32 vertex rounding.  Tick "Recompute
normals" on export after editing geometry, otherwise the tile keeps the normals it
was imported with.
"""

import json
import math
import os
import traceback

import bpy
import numpy as np
from mathutils import Vector
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import aptcodec as AC
from . import dsfcodec as DC
from . import meshcodec as MC
from . import osmcodec as OSM
from . import tercodec as TC
from . import terrain_tiles as TT

R_EARTH = 6378137.0          # WGS84 semi-major axis, metres (matches O4_Geo_Utils)
DEG = math.pi / 180.0

#: node names used by the Z-axis projection, so it can be found and removed again
ZPROJ_NODES = ("zproj_position", "zproj_uv")

#: name of the UV layer, shared with the DSF addon so both imports look the same
UV_LAYER = "XPlaneProjected"

#: viewport colours per triangle attribute, for the material slots
ATTR_COLORS = {
    0: (0.62, 0.60, 0.56, 1.0),      # DUMMY     - normal terrain
    1: (0.25, 0.55, 0.90, 1.0),      # WATER     - inland water
    2: (0.07, 0.25, 0.65, 1.0),      # SEA       - open sea
    4: (0.10, 0.70, 0.75, 1.0),      # SEA_EQUIV
    8: (1.00, 0.55, 0.10, 1.0),      # INTERP_ALT - airport/road/patches
    16: (0.78, 0.16, 0.16, 1.0),     # RUNWAY
    32: (0.85, 0.45, 0.15, 1.0),     # TAXIWAY
    64: (0.80, 0.70, 0.20, 1.0),     # APRON
    128: (0.55, 0.35, 0.65, 1.0),    # HANGAR
}
_ATTR_DOMINANT = (128, 64, 32, 16, 8, 4, 2, 1)

#: flavour -> (header version text, coordinate format, normal columns)
FLAVOURS = {
    MC.FLAVOUR_V2: ("2", "{:.15f}", 3),
    MC.FLAVOUR_V13: ("1.30", "{:.7f}", 2),
}

#: viewport clip end set on import.  A tile spans a whole degree (~111 km) and the
#: default 1000 m cuts all of it away, so the far plane is parked out of reach.
#: Blender stores clip_end as float32: reads back as 99999997952.0.
VIEW_CLIP_END = 1.0e11


def _far_viewport_clip(value=VIEW_CLIP_END):
    """Set clip_end on every 3D view of every screen, return how many were set.

    clip_end lives on the viewport (SpaceView3D), not on the scene, so it is one
    value per workspace screen: the 3D views you are not looking at keep their own
    and would snap back to 1000 when you switch to them.  Cameras have a separate
    clip_end and are left alone.

    Each area is tagged for redraw afterwards, otherwise the view in front of you
    keeps drawing with the old far plane until something else forces a redraw.
    """
    count = 0
    for screen in bpy.data.screens:
        for area in screen.areas:
            hit = False
            for space in area.spaces:
                if space.type == "VIEW_3D":
                    try:
                        space.clip_end = value
                        count += 1
                        hit = True
                    except Exception as ex:                  # noqa: BLE001
                        print("[Ortho4XP mesh] clip_end on %s: %s" % (area.type, ex))
            if hit:
                try:
                    area.tag_redraw()
                except Exception:                            # noqa: BLE001
                    pass
    return count


# --------------------------------------------------------------- helpers
def _georef_from_bounds(west, east, south, north):
    """Local tangent plane about the tile centre (same struct as io_scene_dsf)."""
    lon0 = (west + east) * 0.5
    lat0 = (south + north) * 0.5
    return {
        "lon0": lon0,
        "lat0": lat0,
        "m_per_deg_lat": DEG * R_EARTH,
        "m_per_deg_lon": DEG * R_EARTH * math.cos(lat0 * DEG),
        "west": west, "east": east, "south": south, "north": north,
    }


def _geo_to_blender(georef, lon, lat, alt):
    """Vectorised WGS84 -> Blender (X east, Y north, Z up), metres."""
    x = (np.asarray(lon, dtype=np.float64) - georef["lon0"]) * georef["m_per_deg_lon"]
    y = (np.asarray(lat, dtype=np.float64) - georef["lat0"]) * georef["m_per_deg_lat"]
    z = np.asarray(alt, dtype=np.float64)
    return np.column_stack((x, y, z))


def _blender_to_geo(georef, co):
    return (georef["lon0"] + co[:, 0] / georef["m_per_deg_lon"],
            georef["lat0"] + co[:, 1] / georef["m_per_deg_lat"],
            co[:, 2])


def _attr_color(value):
    v = int(value)
    if v in ATTR_COLORS:
        return ATTR_COLORS[v]
    for bit in _ATTR_DOMINANT:
        if v & bit:
            return ATTR_COLORS[bit]
    return ATTR_COLORS[0]


def _object_matrix(o):
    """4x4 world matrix as float64, plus whether it is the identity."""
    mw = o.matrix_world
    m = np.array([[mw[i][j] for j in range(4)] for i in range(4)], dtype=np.float64)
    ident = np.allclose(m, np.eye(4), atol=0.0, rtol=0.0)
    return m, ident


def _tile_objects(context, use_selection):
    """Mesh objects to export.

    Default (use_selection=False) exports every mesh carrying a `mesh_georef`,
    i.e. the tiles that were imported, so an unrelated default Cube or a prop in
    the scene is never swept into a .mesh file.
    """
    if use_selection:
        cand = list(context.selected_objects)
    else:
        cand = [o for o in context.view_layer.objects if "mesh_georef" in o]
    return [o for o in cand
            if o.type == "MESH" and o.data and len(o.data.polygons)]


def _add_face_attr(me, name, values):
    try:
        a = me.attributes.new(name=name, type="INT", domain="FACE")
        a.data.foreach_set("value", np.asarray(values, dtype=np.int32))
        return a
    except Exception as ex:                                    # noqa: BLE001
        print("[Ortho4XP mesh] could not store the '%s' attribute: %s" % (name, ex))
        return None


def _new_vertex_mesh(name, co, tris):
    """Build a triangle mesh from numpy arrays, fast path, with a safe fallback."""
    n_v, n_t = co.shape[0], tris.shape[0]
    me = bpy.data.meshes.new(name)
    ok = False
    try:
        me.vertices.add(n_v)
        me.vertices.foreach_set("co", np.ascontiguousarray(co, dtype=np.float32).ravel())
        me.loops.add(3 * n_t)
        me.loops.foreach_set("vertex_index",
                             np.ascontiguousarray(tris, dtype=np.int32).ravel())
        me.polygons.add(n_t)
        me.polygons.foreach_set("loop_start",
                                np.arange(0, 3 * n_t, 3, dtype=np.int32))
        try:
            me.polygons.foreach_set("loop_total", np.full(n_t, 3, dtype=np.int32))
        except Exception:                                      # noqa: BLE001
            pass
        me.update(calc_edges=False)
        lt = np.empty(n_t, dtype=np.int32)
        me.polygons.foreach_get("loop_total", lt)
        ok = (len(me.polygons) == n_t and lt.size == n_t
              and int(lt.min()) == 3 and int(lt.max()) == 3)
    except Exception as ex:                                    # noqa: BLE001
        print("[Ortho4XP mesh] fast mesh build failed (%s); using from_pydata" % ex)
        ok = False
    if ok:
        return me
    # slow but bullet-proof path
    bpy.data.meshes.remove(me)
    me = bpy.data.meshes.new(name)
    me.from_pydata([tuple(v) for v in np.asarray(co, dtype=np.float64)],
                   [], [tuple(int(i) for i in t) for t in tris])
    me.update()
    return me


# ------------------------------------------------- terrain textures (.ter)
def _file_dimensions(path):
    """(w, h) from the file header, for cases where Blender reports (0, 0).

    Blender can report size (0, 0) for a DDS/DXT file until its pixels are touched,
    which would silently defeat `tex_max_px` (a ``0 > n`` test never fires), so the
    header is read directly when that happens.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError:
        return None
    if head[:4] == b"DDS " and len(head) >= 20:
        h = int.from_bytes(head[12:16], "little")
        w = int.from_bytes(head[16:20], "little")
        return (w, h) if w and h else None
    if head[:8] == b"\x89PNG\r\n\x1a\n" and len(head) >= 24:
        w = int.from_bytes(head[16:20], "big")
        h = int.from_bytes(head[20:24], "big")
        return (w, h) if w and h else None
    return None


def _load_image(path, max_px=0):
    """Load (or reuse) an image, optionally downscaling it to cap memory.

    Blender decodes an image only when something displays it, so a plain load is
    cheap (measured +0.4 MB each).  A 4096x2560 orthophoto costs ~250 MB while
    decoded, and a ZL18 tile has ~1000 of them, so `max_px` decodes and resizes
    now (peak ~250 MB per image, transient) to keep viewing many cells affordable.
    """
    if not path:
        return None
    try:
        img = bpy.data.images.load(path, check_existing=True)
    except Exception as ex:                                    # noqa: BLE001
        print("[Ortho4XP mesh import] cannot load %s: %s"
              % (os.path.basename(path), ex))
        return None
    if max_px:
        _cap_image(img, max_px, path)
    return img


def _cap_image(img, max_px, path=None):
    """Enforce `max_px` on an image, and verify that it actually took.

    Writing to `img.colorspace_settings.name` -- even to the value it already has --
    invalidates the ImBuf and makes Blender re-read the file at its native size,
    which silently discards an earlier `Image.scale()`.  The cap therefore has to be
    applied *after* the material wiring and then checked.  A decode is tried as a
    second route, because `scale()` on a never-decoded image is a no-op on some
    builds.  Returns True when the image ends up within the cap.
    """
    if not max_px or img is None:
        return True
    if path is None:
        path = img.filepath
    for attempt in (1, 2):
        w, h = img.size
        if not (w and h):
            w, h = _file_dimensions(path) or (0, 0)
        if max(w, h) <= max_px:
            return True
        if attempt == 2:
            try:
                _ = img.pixels[0]                  # decode so scale() has a buffer
            except Exception as ex:                            # noqa: BLE001
                print("[Ortho4XP mesh import] cannot decode %s: %s"
                      % (os.path.basename(path), ex))
                return False
            w, h = img.size or (w, h)
        scale = max_px / float(max(w, h))
        try:
            img.scale(max(1, int(round(w * scale))),
                      max(1, int(round(h * scale))))
        except Exception as ex:                                # noqa: BLE001
            print("[Ortho4XP mesh import] cannot scale %s: %s"
                  % (os.path.basename(path), ex))
            return False
    print("[Ortho4XP mesh import] %s is still %s after downscaling: this Blender "
          "build will not hold the %d px cap for it"
          % (os.path.basename(path), tuple(img.size), max_px))
    return False


def _store_ter_metadata(mat, cell, affine=None):
    """Record what the cell's .ter said, so the material stays self-describing.

    Kept even when the texture itself is not loaded (see `tex_limit`): the .ter
    path, its texture roles and the mapping parameters are always on the material.
    `affine` is the cell's position -> UV map (a_x, b_x, a_y, b_y, see
    `terrain_tiles.cell_uv_affine`); both the Z-axis projection in the node tree
    and the baked UV layer are built from it, so it is stored as the single
    source of truth and read back by the *Z projection* / *Projected UVs* buttons.
    """
    mat["mesh_ter_kind"] = "ter"
    mat["mesh_ter_path"] = cell.path
    mat["mesh_ter_textures"] = json.dumps(cell.textures)
    if cell.lat is not None:
        mat["mesh_ter_center"] = json.dumps(
            [cell.lat, cell.lon, cell.size_m, cell.size_px])
    if cell.border:
        mat["mesh_ter_center_border"] = json.dumps(cell.border)
    if cell.projected:
        mat["mesh_ter_projected"] = json.dumps(list(cell.projected))
    mat["mesh_ter_nowrap"] = bool(cell.nowrap)
    if cell.overlay:
        mat["mesh_ter_overlay"] = True
    if affine is not None:
        mat["mesh_uv_zproj"] = json.dumps([float(v) for v in affine])


def _material_affine(mat, georef=None):
    """The cell's position -> UV map, or None.

    Read from the material (`mesh_uv_zproj`, written at import).  Materials from an
    import made before the projection existed fall back to `mesh_ter_center` plus
    the object's `mesh_georef`, which reproduces the same four numbers.
    """
    stored = mat.get("mesh_uv_zproj")
    if stored:
        try:
            vals = [float(v) for v in json.loads(stored)]
            if len(vals) == 4:
                return tuple(vals)
        except Exception:                                      # noqa: BLE001
            pass
    center = mat.get("mesh_ter_center")
    if center and georef:
        try:
            lat, lon, size = json.loads(center)[:3]
        except Exception:                                      # noqa: BLE001
            return None
        try:
            return TT.center_uv_affine(lat, lon, size, georef)
        except Exception:                                      # noqa: BLE001
            return None
    return None


def _wire_material_zproject(mat, affine):
    """Feed the material's image textures from a Z-axis projection.

    The texture coordinate is no longer a baked UV loop; it is the *position* of
    the shading point in the object's local space (metres, X east / Y north, the
    same coordinates `_geo_to_blender` writes into the vertices), mapped to 0..1
    over the cell's footprint by one multiply-add::

        uv = position * scale + offset        (a_x, a_y, b_x, b_y)

    Because the UV is a function of where the surface *is*, dragging a vertex in
    Blender slides the geometry under a texture that stays nailed to the map --
    which is what X-Plane does with the same tile, and what a baked UV layer
    cannot do (there the texture is carried along by the vertex and stretches).
    Object space rather than world space, so moving the tile as a whole moves its
    map with it.

    Returns True when a chain was wired (or was already there).
    """
    if affine is None or mat is None:
        return False
    if not mat.use_nodes or mat.node_tree is None:
        mat.use_nodes = True
    nt = mat.node_tree
    if nt is None:
        return False
    tex = [nd for nd in nt.nodes if nd.type == "TEX_IMAGE"]
    if not tex:
        return False
    ax, bx, ay, by = (float(v) for v in affine)
    if ax == 0.0 or ay == 0.0:
        return False
    geo = nt.nodes.get(ZPROJ_NODES[0])
    if geo is None:
        geo = nt.nodes.new("ShaderNodeTexCoord")
        geo.name = ZPROJ_NODES[0]
        geo.label = "Z projection: object position (m)"
        geo.location = (-1040, 260)
    # Vector Math "Multiply Add" is componentwise a*b+c in ONE node: scale in
    # input 1, offset in input 2 -- the whole projection is this multiplication.
    vm = nt.nodes.get(ZPROJ_NODES[1])
    if vm is None:
        vm = nt.nodes.new("ShaderNodeVectorMath")
        vm.name = ZPROJ_NODES[1]
        vm.operation = "MULTIPLY_ADD"
        vm.location = (-820, 260)
    vm.label = "metres -> UV (fixed map)"
    vm.inputs[1].default_value = (ax, ay, 0.0)
    vm.inputs[2].default_value = (bx, by, 0.0)
    nt.links.new(geo.outputs["Object"], vm.inputs[0])
    for nd in tex:
        nt.links.new(vm.outputs["Vector"], nd.inputs["Vector"])
    return True


def _unwire_material_zproject(mat):
    """Drop the Z projection, putting the image textures back on the UV layer."""
    if mat is None or not mat.node_tree:
        return False
    nt = mat.node_tree
    gone = [nd for nd in nt.nodes if nd.name in ZPROJ_NODES]
    for nd in gone:
        nt.nodes.remove(nd)          # removes its links, so Vector goes back to UV
    return bool(gone)


def _apply_terrain_texture(mat, cell, max_px=0, affine=None, zproj=True):
    """Wire one cell's .ter into a material; returns how many images were loaded.

    Same policy as the DSF importer: the base texture drives Base Color, a
    NORMAL_TEX gets a normal-map node, and the remaining layers (lit, composite,
    border) are imported as labelled but unconnected nodes -- blending them needs
    the per-vertex factor that lives in the DSF, which a .mesh does not carry, so
    inventing a factor would be a lie.
    """
    _store_ter_metadata(mat, cell, affine)
    ter = cell.ter
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (540, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (240, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    try:
        bsdf.inputs["Roughness"].default_value = 1.0
    except Exception:                                          # noqa: BLE001
        pass

    extension = "EXTEND" if cell.nowrap else "REPEAT"
    loaded = 0
    base = ter.texture_abs("base") or ter.texture_abs("other")
    img = _load_image(base, max_px)
    if img is not None:
        loaded += 1
        img.colorspace_settings.name = "sRGB"
        node = nt.nodes.new("ShaderNodeTexImage")
        node.location = (-560, 140)
        node.label = "BASE_TEX"
        node.image = img
        node.extension = extension
        nt.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])

    n_img = _load_image(ter.texture_abs("normal"), max_px)
    if n_img is not None:
        loaded += 1
        n_img.colorspace_settings.name = "Non-Color"
        node = nt.nodes.new("ShaderNodeTexImage")
        node.location = (-560, -160)
        node.label = "NORMAL_TEX"
        node.image = n_img
        node.extension = extension
        nmap = nt.nodes.new("ShaderNodeNormalMap")
        nmap.location = (-280, -160)
        nt.links.new(node.outputs["Color"], nmap.inputs["Color"])
        nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])

    for role, y in (("lit", -420), ("composite", -640), ("border", -860)):
        r_img = _load_image(ter.texture_abs(role), max_px)
        if r_img is None:
            continue
        loaded += 1
        r_img.colorspace_settings.name = "sRGB"
        node = nt.nodes.new("ShaderNodeTexImage")
        node.location = (-560, y)
        node.label = role.upper() + "_TEX (imported, not blended)"
        node.image = r_img
        node.extension = extension
    # ---- the projection that keeps the orthophoto nailed to the map
    if zproj and affine is not None:
        _wire_material_zproject(mat, affine)
    # cap the images LAST: the colorspace writes above make Blender re-read each
    # file at its native size, which silently discards an earlier Image.scale()
    if max_px:
        for n in nt.nodes:
            if n.type == "TEX_IMAGE" and n.image is not None:
                _cap_image(n.image, max_px)
    return loaded


def _write_uv_layer(me, slot_affine, layer_name=UV_LAYER):
    """(Re)write the UV layer from the vertices' current positions.

    `uv = position * scale + offset`, per face, taking the mapping from the face's
    *material slot*: a face keeps its material when its vertices move, so the same
    call is correct at import and after the tile has been edited -- which is what
    the *Projected UVs* button uses to re-anchor the layer on the moved geometry.

    This is the baked twin of the shader projection: same four numbers, same
    coordinates (the object's local metres).  Rendered views use the projection and
    ignore this layer; Workbench's *solid* view, the UV editor and anything that
    reads UVs use the layer, and go stale as soon as a vertex is dragged.

    Returns {"layer", "loops", "mapped"}; a missing mapping leaves those loops at 0.
    """
    n_poly = len(me.polygons)
    n_loops = len(me.loops)
    if not n_poly or not n_loops:
        return {"layer": None, "loops": 0, "mapped": 0}
    loop_total = np.empty(n_poly, dtype=np.int32)
    me.polygons.foreach_get("loop_total", loop_total)
    slot_of = np.empty(n_poly, dtype=np.int32)
    me.polygons.foreach_get("material_index", slot_of)
    loop_vert = np.empty(n_loops, dtype=np.int32)
    me.loops.foreach_get("vertex_index", loop_vert)
    co = np.empty(len(me.vertices) * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    loop_slot = np.repeat(slot_of, loop_total)
    u = np.zeros(n_loops)
    v = np.zeros(n_loops)
    mapped = 0
    for slot, affine in enumerate(slot_affine):
        if affine is None:
            continue
        sel = loop_slot == slot
        if not sel.any():
            continue
        ax, bx, ay, by = (float(x) for x in affine)
        lv = loop_vert[sel]
        u[sel] = ax * co[lv, 0] + bx
        v[sel] = ay * co[lv, 1] + by
        mapped += int(sel.sum())
    flat = np.empty(n_loops * 2, dtype=np.float32)
    flat[0::2] = u
    flat[1::2] = v
    layer = me.uv_layers.get(layer_name) or me.uv_layers.new(name=layer_name)
    layer.data.foreach_set("uv", flat)
    return {"layer": layer.name, "loops": int(n_loops), "mapped": mapped}


def _mark_seams_from_islands(me, layer_name=UV_LAYER, eps=0.0):
    """Mark the UV island borders as seams -- Blender's *UV > Seams from Islands*.

    Same answer as the built-in operator, computed on the datablock instead of
    through edit mode: that menu item on a 4.5M triangle tile costs an 8.6 s edit
    mode round trip and a 3.2 GB BMesh copy, this costs a sort of the face edges
    and no second copy of the mesh.

    The operator's rule is narrow, and it is what is reproduced here: *an edge is
    a seam when the faces sharing it hand that edge's vertices different UVs.*  A
    border edge (one face) keeps no seam -- it is already a border -- and a
    non-manifold edge is marked only when its sides actually disagree.  UVs are
    compared exactly, as the operator does.

    Returns the number of edges marked.
    """
    n_loops = len(me.loops)
    n_poly = len(me.polygons)
    layer = me.uv_layers.get(layer_name)
    if layer is None or not n_loops or not n_poly:
        return 0

    loop_vert = np.empty(n_loops, dtype=np.int32)
    me.loops.foreach_get("vertex_index", loop_vert)
    # the mesh already tells every loop which edge it runs along: no need to work
    # the edge out of the vertex pair, nor to find it again afterwards
    loop_edge = np.empty(n_loops, dtype=np.int32)
    me.loops.foreach_get("edge_index", loop_edge)
    uv = np.empty(n_loops * 2, dtype=np.float32)
    layer.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2)
    loop_start = np.empty(n_poly, dtype=np.int32)
    loop_total = np.empty(n_poly, dtype=np.int32)
    me.polygons.foreach_get("loop_start", loop_start)
    me.polygons.foreach_get("loop_total", loop_total)

    # the loop each one runs on to, inside its own face, the last one of a face
    # wrapping round; that wrap is what keeps the indexing in range, the final
    # loop of the mesh always being the last loop of a face
    nxt = np.arange(1, n_loops + 1, dtype=np.int32)
    nxt[loop_start + loop_total - 1] = loop_start

    other = loop_vert[nxt]
    # the UV each *side* of the edge hands to the edge's low and high vertex; two
    # neighbours walk the edge in opposite directions, so it is the vertex each
    # one is talking about -- not the loop order -- that makes them comparable
    low_first = (loop_vert <= other)[:, None]
    u_low = np.where(low_first, uv, uv[nxt])
    u_high = np.where(low_first, uv[nxt], uv)

    # gather the sides of every edge and compare each one with the first of its
    # group; the group's first side is arbitrary, and any disagreement is caught
    # against it whichever side it is, so the sort does not have to be stable
    order = np.argsort(loop_edge, kind="quicksort")
    sides = loop_edge[order]
    starts = np.flatnonzero(np.r_[True, sides[1:] != sides[:-1]])
    ref = np.repeat(starts, np.diff(np.r_[starts, sides.size]))
    lo_s, hi_s = u_low[order], u_high[order]
    if eps:
        bad = ((np.abs(lo_s - lo_s[ref]) > eps).any(axis=1)
               | (np.abs(hi_s - hi_s[ref]) > eps).any(axis=1))
    else:
        bad = (lo_s != lo_s[ref]).any(axis=1) | (hi_s != hi_s[ref]).any(axis=1)

    seams = np.zeros(len(me.edges), dtype=bool)
    seams[sides[starts][np.logical_or.reduceat(bad, starts)]] = True
    me.edges.foreach_set("use_seam", seams)
    return int(seams.sum())


def _import_terrain_textures(op, obj, me, m, georef):
    """Give the tile its orthophotos: a material per .ter cell + one UV layer.

    Returns a stats dict (also stored on the object as `mesh_tex_stats`).
    """
    tdir = (op.terrain_dir or "").strip() or TT.find_terrain_dir(op.filepath)
    stats = {"terrain_dir": tdir, "cells": 0, "cells_used": 0, "materials": 0,
             "faces_total": int(m.n_tri), "faces_textured": 0, "faces_uncovered": 0,
             "images_loaded": 0, "cells_with_image": 0, "uv_layer": None,
             "skipped_ter": 0, "overlay_cells": 0, "max_px": int(op.tex_max_px)}
    if not tdir:
        print("[Ortho4XP mesh import] no terrain folder found next to %s; "
              "no textures imported" % os.path.basename(op.filepath))
        return stats

    cells, skipped = TT.scan_terrain([tdir])
    stats["cells"] = len(cells)
    stats["skipped_ter"] = len(skipped)
    stats["overlay_cells"] = sum(1 for c in cells if c.overlay)
    if not cells:
        print("[Ortho4XP mesh import] %s holds %d .ter file(s) but none declares a "
              "LOAD_CENTER footprint; no textures imported"
              % (tdir, len(skipped)))
        return stats
    try:
        index = TT.TerrainIndex(cells)
    except ValueError as ex:
        print("[Ortho4XP mesh import] cannot index the terrain cells: %s" % ex)
        return stats

    face_cell, _clat, _clon = index.centroid_cells(m.lat, m.lon, m.tris)
    covered = int(np.count_nonzero(face_cell >= 0))
    stats["faces_textured"] = covered
    stats["faces_uncovered"] = int(face_cell.size - covered)

    # one material per used cell, the cells owning the most faces first
    used, counts = np.unique(face_cell[face_cell >= 0], return_counts=True)
    order = used[np.argsort(-counts)]
    stats["cells_used"] = int(order.size)
    n_load = order.size if not op.tex_limit else min(int(op.tex_limit), order.size)
    load_set = {int(i) for i in order[:n_load]}
    # position -> UV: one affine per cell, the single source of truth for both the
    # baked UV layer and the Z projection in the node trees
    aff = TT.cell_uv_affine(cells, georef)
    slot_of = {}
    slot_affine = []
    for i in order:
        cell = cells[int(i)]
        affine = None
        if aff[4][int(i)]:
            affine = (float(aff[0][int(i)]), float(aff[1][int(i)]),
                      float(aff[2][int(i)]), float(aff[3][int(i)]))
        mat = bpy.data.materials.new("ter_%s" % cell.label)
        if int(i) in load_set:
            n_img = _apply_terrain_texture(mat, cell, int(op.tex_max_px), affine,
                                           bool(op.zproj))
            stats["images_loaded"] += n_img
            if n_img:
                stats["cells_with_image"] += 1
        else:
            _store_ter_metadata(mat, cell, affine)
        slot_of[int(i)] = len(obj.data.materials)
        slot_affine.append(affine)
        obj.data.materials.append(mat)

    # faces with no cell get their own slot, so a gap shows up instead of being
    # silently painted with a neighbour's texture
    if stats["faces_uncovered"]:
        gap = bpy.data.materials.new("ter_none_uncovered")
        gap.diffuse_color = (1.0, 0.15, 0.15, 1.0)
        gap["mesh_ter_kind"] = "none"
        slot_of[-1] = len(obj.data.materials)
        slot_affine.append(None)
        obj.data.materials.append(gap)
    face_slot = np.zeros(face_cell.size, dtype=np.int32)
    for i in np.unique(face_cell):
        face_slot[face_cell == i] = slot_of[int(i)]
    me.polygons.foreach_set("material_index", face_slot)
    stats["materials"] = len(obj.data.materials)

    # ---- UVs: the same map, baked once from the vertices' positions
    uv_stats = _write_uv_layer(me, slot_affine)
    obj["mesh_uv_layer"] = uv_stats["layer"]
    stats["uv_layer"] = uv_stats["layer"]
    stats["uv_loops"] = uv_stats["loops"]
    stats["uv_mapped"] = uv_stats["mapped"]
    stats["zproj"] = bool(op.zproj)
    # verify the memory cap instead of trusting it: report any image still over it
    if int(op.tex_max_px or 0):
        over = [i.name for i in bpy.data.images
                if i.source == "FILE" and max(i.size) > int(op.tex_max_px)]
        stats["images_over_cap"] = len(over)
        if over:
            print("[Ortho4XP mesh import] WARNING: %d image(s) are still above the "
                  "%d px cap: %s" % (len(over), int(op.tex_max_px), over[:5]))
    else:
        stats["images_over_cap"] = 0
    return stats



# ------------------------------------------------------- X-Plane DSF import
def _dsf_bounds(dsf):
    """(west, east, south, north): the sim/* properties, else from the pools.

    The bounds drive the georeference, so a DSF without them is measured from its
    own vertices instead of being rejected.
    """
    b = dsf.bounds()
    if b:
        return b
    lons, lats = [], []
    for pool in dsf.pools:
        for v in pool:
            if len(v) > 1:
                lons.append(v[0])
                lats.append(v[1])
    if not lons or not lats:
        raise DC.DSFError("the DSF has no sim/* bounds and no terrain vertices")
    return (min(lons), max(lons), min(lats), max(lats))


def _dsf_texture_affine(mat, georef):
    """Position -> UV affine for one terrain definition's material, or None.

    Both .ter mappings are affine in the tile's local metres, which is what lets
    the DSF path reuse the mesh path's UV layer and Z projection instead of
    carrying a second implementation:

        PROJECTED <x> <y>   u = (x + half_width  - offset_x) / x
                            v = (y + half_height - offset_y) / y
        LOAD_CENTER <lat> <lon> <m>   u = 0.5 + east/size, v = 0.5 + north/size
    """
    pj = mat.get("dsf_ter_projected")
    if pj:
        try:
            xs, ys, ox, oy = (float(v) for v in json.loads(pj))
        except Exception:                                      # noqa: BLE001
            return None
        if xs == 0.0 or ys == 0.0:
            return None
        hw = (float(georef["east"]) - float(georef["west"])) * 0.5 * georef["m_per_deg_lon"]
        hh = (float(georef["north"]) - float(georef["south"])) * 0.5 * georef["m_per_deg_lat"]
        return (1.0 / xs, (hw - ox) / xs, 1.0 / ys, (hh - oy) / ys)
    lc = mat.get("dsf_ter_center")
    if lc:
        try:
            lat, lon, size = json.loads(lc)[:3]
        except Exception:                                      # noqa: BLE001
            return None
        try:
            return TT.center_uv_affine(lat, lon, size, georef)
        except Exception:                                      # noqa: BLE001
            return None
    return None


def _apply_dsf_texture(mat, resolver, entry, max_px=0, affine=None, zproj=True):
    """Wire the .ter (or bitmap) a terrain definition points at into a material.

    Returns True when a base texture was attached.  The .ter's other layers are
    loaded as labelled but unconnected nodes: compositing and night lighting need
    the per-vertex blend parameter that lives in DSF pool planes 5+, which a
    Blender mesh does not carry, so inventing a factor would be a lie.
    """
    ter, kind, img_path = resolver.load(entry)
    if kind == "builtin":
        # e.g. terrain_Water -- rendered internally by X-Plane, no file on disk
        mat["dsf_ter_kind"] = "builtin"
        return False
    if kind == "image":
        mat["dsf_ter_kind"] = "image"
        mat["dsf_ter_textures"] = json.dumps({"base": img_path})
        base = img_path
    elif kind == "ter" and ter is not None:
        mat["dsf_ter_kind"] = "ter"
        mat["dsf_ter_path"] = ter.path
        mat["dsf_ter_textures"] = json.dumps(ter.textures)
        proj = ter.params("PROJECTED", 2)
        if proj:
            ox = oy = 0.0
            ov = ter.directive("PROJECT_OFFSET")
            if ov and len(ov) >= 3:
                try:
                    ox = float(ov[1].replace(",", "."))
                    oy = float(ov[2].replace(",", "."))
                except ValueError:
                    ox = oy = 0.0
            mat["dsf_ter_projected"] = json.dumps([proj[0], proj[1], ox, oy])
        else:
            lc = ter.params("LOAD_CENTER", 4)
            if lc:
                mat["dsf_ter_center"] = json.dumps(lc)
            lb = ter.params("LOAD_CENTER_BORDER", 4)
            if lb:
                mat["dsf_ter_center_border"] = json.dumps(lb)
        if ter.directive("PROJECT_ANGLE"):
            mat["dsf_ter_angle"] = json.dumps(ter.directive("PROJECT_ANGLE")[1:])
        mat["dsf_ter_nowrap"] = bool(
            ter.has("BASE_TEX_NOWRAP") or ter.has("LIT_TEX_NOWRAP")
            or "dsf_ter_center" in mat)
        base = ter.texture_abs("base") or ter.texture_abs("other")
    else:
        mat["dsf_ter_kind"] = "unresolved"
        return False

    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (540, 0)
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    bsdf.location = (240, 0)
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    try:
        bsdf.inputs["Roughness"].default_value = 1.0
    except Exception:                                          # noqa: BLE001
        pass

    x = -560
    base_img = _load_image(base, max_px)
    if base_img is not None:
        base_img.colorspace_settings.name = "sRGB"
        node = nt.nodes.new("ShaderNodeTexImage")
        node.location = (x, 140)
        node.label = "BASE_TEX"
        node.image = base_img
        node.extension = "EXTEND" if mat.get("dsf_ter_nowrap") else "REPEAT"
        nt.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])

    if kind == "ter" and ter is not None:
        n_img = _load_image(ter.texture_abs("normal"), max_px)
        if n_img is not None:
            n_img.colorspace_settings.name = "Non-Color"
            node = nt.nodes.new("ShaderNodeTexImage")
            node.location = (x, -160)
            node.label = "NORMAL_TEX"
            node.image = n_img
            nmap = nt.nodes.new("ShaderNodeNormalMap")
            nmap.location = (x + 280, -160)
            nt.links.new(node.outputs["Color"], nmap.inputs["Color"])
            nt.links.new(nmap.outputs["Normal"], bsdf.inputs["Normal"])
        for role, y in (("lit", -420), ("composite", -640), ("border", -860)):
            r_img = _load_image(ter.texture_abs(role), max_px)
            if r_img is None:
                continue
            r_img.colorspace_settings.name = "sRGB"
            node = nt.nodes.new("ShaderNodeTexImage")
            node.location = (x, y)
            node.label = role.upper() + "_TEX (imported, not blended)"
            node.image = r_img

    if zproj and affine is not None:
        _wire_material_zproject(mat, affine)
    if max_px:
        for n in nt.nodes:
            if n.type == "TEX_IMAGE" and n.image is not None:
                _cap_image(n.image, max_px)
    return base_img is not None


def _write_polygon_osm(op, dsf, include_forests=False):
    """Auto-save the DSF's polygon boundaries as <dsf>.patch.osm; returns the path."""
    if not getattr(op, "osm_export", False):
        return None
    custom = (getattr(op, "osm_path", "") or "").strip()
    if custom:
        target = bpy.path.abspath(custom)
        if os.path.isdir(target):
            target = os.path.join(target, os.path.basename(
                os.path.splitext(op.filepath)[0]) + ".patch.osm")
    else:
        target = os.path.splitext(op.filepath)[0] + ".patch.osm"
    try:
        ways, nodes, nbytes = OSM.write_osm(
            target, dsf.polygons,
            note="X-Plane polygon boundaries from %s, written by the Blender importer"
                 % os.path.basename(op.filepath),
            include_forests=include_forests)
    except OSError as ex:
        msg = "could not write %s: %s" % (target, ex)
        print("[X-Plane DSF import] " + msg)
        op.report({"WARNING"}, msg)
        return None
    if not ways:
        return None
    msg = ("polygon boundaries saved to %s (%d way(s), %d node(s), %.0f kB)"
           % (target, ways, nodes, nbytes / 1024.0))
    print("[X-Plane DSF import] " + msg)
    op.report({"INFO"}, msg)
    return target


def _build_polygon_outlines(terrain, dsf, georef, include_forests, lift=0.05):
    """Edge-only mesh with the DSF's polygon boundaries, draped on the terrain.

    Nothing is filled and nothing is textured: a .pol / .fac / legacy class polygon
    becomes a closed outline, a .lin (line feature) an open polyline. Forest areas
    (.for) are left out unless asked for -- a Global Scenery tile holds thousands of
    them and their outlines drown the pavement the user is looking for.
    """
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree

    polys = [p for p in dsf.polygons
             if include_forests or not p.klass.lower().endswith(".for")]
    if not polys:
        return None, {"polygons": 0, "rings": 0, "points": 0, "shown": [], "dropped": 0}

    bvh = None
    if terrain is not None:
        sample = [v.co.z for v in terrain.data.vertices[:4000]]
        z_fallback = min(sample) if sample else 0.0
        z_top = max(sample) + 1000.0 if sample else 10000.0
        try:
            bvh = BVHTree.FromObject(terrain, bpy.context.evaluated_depsgraph_get())
        except Exception:                                      # noqa: BLE001
            bvh = None
    else:
        z_fallback = 0.0

    verts, edges = [], []
    counts, dropped = {}, 0
    for poly in polys:
        open_line = poly.klass.lower().endswith(".lin")
        for ring in poly.rings:
            base = len(verts)
            hits = 0
            for lon, lat in ring:
                xy = _geo_to_blender(georef, lon, lat, 0.0)[0]
                x, y = float(xy[0]), float(xy[1])
                z = None
                if bvh is not None:
                    loc, _nor, _idx, _dist = bvh.ray_cast(
                        Vector((x, y, z_top)), Vector((0.0, 0.0, -1.0)), 40000.0)
                    if loc is not None:
                        z = loc.z
                        hits += 1
                verts.append((x, y, (z if z is not None else z_fallback) + lift))
            if not hits:
                dropped += 1
            n = len(ring)
            edges.extend((base + i, base + i + 1) for i in range(n - 1))
            if not open_line and n > 2:
                edges.append((base + n - 1, base))
        name = poly.klass.split("/")[-1] or "polygon"
        counts[name] = counts.get(name, 0) + 1

    if not verts:
        return None, {"polygons": 0, "rings": 0, "points": 0, "shown": [],
                      "dropped": dropped}
    base_name = os.path.splitext(os.path.basename(dsf.source_path or "dsf"))[0]
    me = bpy.data.meshes.new(base_name + "_polygons")
    me.from_pydata(verts, edges, [])
    me.update()
    ob = bpy.data.objects.new(base_name + "_polygons", me)
    ob["dsf_polygons"] = json.dumps(counts, sort_keys=True)
    ob["dsf_georef"] = json.dumps(georef)
    context_collection = (terrain.users_collection[0] if terrain.users_collection
                          else bpy.context.scene.collection) if terrain is not None \
        else bpy.context.scene.collection
    context_collection.objects.link(ob)
    stats = {"polygons": len(polys), "rings": sum(len(p.rings) for p in polys),
             "points": len(verts), "shown": sorted(counts.items(),
                                                   key=lambda kv: -kv[1])[:6],
             "dropped": dropped}
    return ob, stats


def _import_dsf_terrain(op, context):
    """Build the tile's terrain mesh from a DSF (the .dsf import operator's work)."""
    dsf = DC.load_dsf(op.filepath)
    west, east, south, north = _dsf_bounds(dsf)
    georef = _georef_from_bounds(west, east, south, north)
    name = os.path.splitext(os.path.basename(op.filepath))[0]

    stats = {"file": op.filepath, "compressed": bool(dsf.was_compressed),
             "patches": len(dsf.patches), "triangles": 0, "verts": 0,
             "defs": len(dsf.terrain_defs()), "defs_used": 0, "materials": 0,
             "textured": 0, "builtin": 0, "unresolved": 0, "unresolved_list": [],
             "uv_layer": None, "uv_mapped": 0, "max_px": int(op.tex_max_px),
             "zproj": bool(op.zproj), "unhandled": dict(dsf.unhandled)}

    # ---- the tile's raster DEM is the file's own source of height for vertices that
    # carry none: Global Scenery writes raw elevation 0 (which decodes to -32768 m)
    # and zero normals for whole LOD layers, so the mesh alone cannot place them.
    # The raster is registered against the heights the mesh *does* carry, and only
    # used when it reproduces them closely -- a wrong layer is detected, not trusted.
    bounds = (west, east, south, north)
    known = []
    missing_seen = False
    for pa in dsf.patches:
        for tri in pa.triangles:
            for pool_i, vert_i in tri:
                v = dsf.pools[pool_i][vert_i]
                if v[2] <= DC.NO_ELEVATION_M:
                    missing_seen = True
                elif len(known) < 4000:
                    known.append(v)
        if missing_seen and len(known) >= 400:
            break
    fit = DC.pick_dem(dsf.rasters, bounds, known) if (missing_seen and dsf.rasters) else None
    if fit is not None:
        stats["raster"] = {"width": fit[0].width, "height": fit[0].height,
                           "flip_y": bool(fit[1]), "dx": float(fit[2]),
                           "dy": float(fit[3]), "fit_error_m": round(fit[4], 2),
                           "sharpness_m_per_px": round(fit[5], 2)}
        print("[X-Plane DSF import] DEM layer %dx%d picked for detail "
              "(%.1f m/px between neighbouring pixels), registered to the mesh: "
              "median error %.1f m"
              % (fit[0].width, fit[0].height, fit[5], fit[4]))

    # ---- flatten pools into one vertex array; DSF triangles are clockwise seen
    # from above, so the winding is reversed to get +Z normals in Blender
    vmap = {}
    co_list = []
    tri_list = []
    face_def = []
    face_patch = []
    filled = 0
    no_elev = 0
    for pi, patch in enumerate(dsf.patches):
        for tri in patch.triangles:
            corners = []
            drop = False
            for pool_i, vert_i in tri:
                v = dsf.pools[pool_i][vert_i]
                lon = v[0] if len(v) > 0 else 0.0
                lat = v[1] if len(v) > 1 else 0.0
                elev = v[2] if len(v) > 2 else 0.0
                if elev <= DC.NO_ELEVATION_M:
                    hv = None
                    if fit is not None:
                        hv = fit[0].height_at(lon, lat, bounds, fit[1], fit[2], fit[3])
                    if hv is None:
                        drop = True      # nothing in the file says how high this is
                        break
                    elev = hv
                    filled += 1
                corners.append((lon, lat, elev))
            if drop:
                no_elev += 1
                continue
            idx = []
            for lon, lat, elev in corners:
                key = (lon, lat, elev)
                vi = vmap.get(key)
                if vi is None:
                    vi = vmap[key] = len(co_list)
                    co_list.append(_geo_to_blender(georef, lon, lat, elev))
                idx.append(vi)
            tri_list.append((idx[2], idx[1], idx[0]))
            face_def.append(patch.definition)
            face_patch.append(pi)
    if not tri_list:
        raise DC.DSFError("no terrain mesh in this DSF (nothing to import)")

    co = np.asarray(co_list, dtype=np.float64).reshape(-1, 3)
    tris = np.asarray(tri_list, dtype=np.int32).reshape(-1, 3)
    me = _new_vertex_mesh(name, co, tris)
    obj = bpy.data.objects.new(name, me)
    (context.collection or context.scene.collection).objects.link(obj)
    stats["triangles"] = int(tris.shape[0])
    stats["verts"] = int(co.shape[0])
    stats["filled_from_raster"] = int(filled)
    stats["skipped_no_elevation"] = int(no_elev)

    # ---- one material per terrain definition, textures through the .ter chain
    defs = dsf.terrain_defs()
    resolver = None
    if op.import_materials and op.import_textures:
        extra = [p for p in op.extra_tex_roots.split(os.pathsep) if p.strip()]
        try:
            resolver = TC.TerResolver(op.filepath,
                                      xp_root=op.xp_root.strip() or None,
                                      extra_roots=extra)
            print("[X-Plane DSF import] x-plane root: %s; %d library prefixes"
                  % (resolver.xp_root, len(resolver.prefixes)))
        except Exception as ex:                                # noqa: BLE001
            resolver = None
            print("[X-Plane DSF import] cannot resolve terrain textures: %s" % ex)

    slot_of = {}
    slot_affine = []
    unresolved = []
    if op.import_materials:
        for di in sorted(set(face_def)):
            path = defs[di] if di < len(defs) else "<terrain %d>" % di
            mat = bpy.data.materials.new("dsf_terrain_%d" % di)
            mat["dsf_def_index"] = di
            mat["dsf_def_path"] = path
            if resolver is not None:
                if _apply_dsf_texture(mat, resolver, path, int(op.tex_max_px), None,
                                      False):
                    stats["textured"] += 1
                elif mat.get("dsf_ter_kind") == "builtin":
                    stats["builtin"] += 1
                else:
                    stats["unresolved"] += 1
                    unresolved.append(path)
            affine = _dsf_texture_affine(mat, georef)
            if affine is not None and bool(op.zproj):
                _wire_material_zproject(mat, affine)
            slot_of[di] = len(obj.data.materials)
            slot_affine.append(affine)
            obj.data.materials.append(mat)
        slot = np.zeros(len(me.polygons), dtype=np.int32)
        def_arr = np.asarray(face_def, dtype=np.int32)
        for di, idx in slot_of.items():
            slot[def_arr == di] = idx
        me.polygons.foreach_set("material_index", slot)
        stats["materials"] = len(obj.data.materials)
        stats["defs_used"] = len(slot_of)
        stats["unresolved_list"] = sorted(set(unresolved))[:40]

    # ---- UVs: the same affine machinery the .mesh importer writes and projects
    if op.import_uvs and slot_of:
        uv_stats = _write_uv_layer(me, slot_affine)
        obj["dsf_uv_layer"] = uv_stats["layer"]
        stats["uv_layer"] = uv_stats["layer"]
        stats["uv_mapped"] = uv_stats["mapped"]
        for m in obj.data.materials:
            if m is not None and m.get("dsf_ter_nowrap"):
                for n in m.node_tree.nodes:
                    if n.type == "TEX_IMAGE":
                        n.extension = "EXTEND"
        if op.mark_seams and uv_stats["layer"]:
            stats["seams"] = _mark_seams_from_islands(me, uv_stats["layer"])

    _add_face_attr(me, "dsf_patch", face_patch)
    if op.smooth:
        flags = np.ones(len(me.polygons), dtype=bool)
        me.polygons.foreach_set("use_smooth", flags)

    # ---- metadata: enough to know what was imported, nothing that pretends a
    # DSF can be written back from Blender (it cannot: there is no exporter)
    obj["dsf_georef"] = json.dumps(georef)
    obj["dsf_props"] = json.dumps([[k, v] for k, v in dsf.props
                                   if not k.startswith("_dsf/")])
    obj["dsf_defn"] = json.dumps(dsf.defn)
    obj["dsf_patches"] = json.dumps(
        [{"definition": p.definition, "flags": p.flags,
          "lod_near": p.lod_near, "lod_far": p.lod_far} for p in dsf.patches])
    obj["dsf_source"] = op.filepath
    obj["dsf_stats"] = json.dumps(stats)
    return obj, me, dsf, georef, stats


# ------------------------------------------------------- apt.dat pavements
def _apt_dat_path(op, dsf_path, xp_root):
    """Where apt.dat is: the operator's choice, next to the .dsf, or Global Airports."""
    if (op.apt_dat or "").strip():
        return os.path.expanduser(op.apt_dat.strip()), "chosen"
    here = os.path.dirname(os.path.abspath(dsf_path))
    for cand in (os.path.join(here, "apt.dat"),
                 os.path.join(os.path.dirname(here), "apt.dat")):
        if os.path.isfile(cand):
            return cand, "next to the .dsf"
    root = (xp_root or "").strip()
    if not root:
        # walk up looking for the X-Plane root (it has Resources/default scenery)
        d = here
        for _ in range(6):
            if os.path.isdir(os.path.join(d, "Resources", "default scenery")):
                root = d
                break
            d = os.path.dirname(d)
    if root:
        cand = os.path.join(root, "Global Scenery", "Global Airports",
                            "Earth nav data", "apt.dat")
        if os.path.isfile(cand):
            return cand, "Global Airports"
    return None, "not found"


def _subdivide(tri, max_edge, depth=0):
    """Split a triangle (three (x, y) points) until its longest edge is <= max_edge.

    The pavement has to follow the terrain's curvature, not just its outline: a
    boundary-only polygon would be one flat sheet across a hillside.
    """
    a, b, c = tri
    d_ab = math.dist(a, b)
    d_bc = math.dist(b, c)
    d_ca = math.dist(c, a)
    longest = max(d_ab, d_bc, d_ca)
    # stop on AREA, not on edge length: an ear of a long concave apron is a sliver
    # with a huge edge and a small area, and splitting it by edge length alone
    # produced tens of thousands of triangles for one grass strip.  The edge guard
    # keeps a leftover sliver from spanning a hillside.
    area = abs((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) * 0.5
    # stop early: an apron interior needs no 20 m grid, the densified boundary already
    # gives the edges that follow the shape, and the ray-cast drapes whatever is left
    if depth >= 4 or (area <= 16.0 * max_edge * max_edge and longest <= 8.0 * max_edge):
        return [tri]
    ab = ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)
    bc = ((b[0] + c[0]) * 0.5, (b[1] + c[1]) * 0.5)
    ca = ((c[0] + a[0]) * 0.5, (c[1] + a[1]) * 0.5)
    out = []
    out += _subdivide((a, ab, ca), max_edge, depth + 1)
    out += _subdivide((ab, b, bc), max_edge, depth + 1)
    out += _subdivide((ca, bc, c), max_edge, depth + 1)
    out += _subdivide((ab, bc, ca), max_edge, depth + 1)
    return out


def _terrain_tile_for_pavements(context):
    """The DSF-imported tile in the scene as (object, georef, bounds), or None."""
    best = None
    for o in context.scene.objects:
        if o.type != "MESH":
            continue
        # both importers store the same georeference struct, under their own key:
        # a DSF tile has dsf_georef, an Ortho4XP .mesh tile has mesh_georef
        stored = o.get("dsf_georef") or o.get("mesh_georef")
        if not stored:
            continue
        try:
            geo = json.loads(stored)
        except Exception:                                      # noqa: BLE001
            continue
        if best is None or len(o.data.polygons) > len(best[0].data.polygons):
            best = (o, geo, (geo["west"], geo["east"], geo["south"], geo["north"]))
    return best


def _build_pavement_mesh(name, pavs, elev_by_id, georef, terrain_obj, step, lift):
    """Drape the pavement boundaries, returning ``(object, stats)``.

    With a terrain object every vertex is ray-cast onto it, so the pavement follows
    the surface's curvature.  Without one (apt.dat imported on its own) the pavement
    is placed at the airport's own elevation from apt.dat row 1, i.e. flat, which is
    what the file itself says.
    """
    stats = {"pavements": 0, "triangles": 0, "verts": 0, "unplaced": 0, "holes": 0,
             "draped": terrain_obj is not None, "terrain": None}
    bvh = None
    if terrain_obj is not None:
        from mathutils.bvhtree import BVHTree
        me = terrain_obj.data
        co = np.empty(len(me.vertices) * 3, dtype=np.float32)
        me.vertices.foreach_get("co", co)
        verts = [tuple(co[i:i + 3]) for i in range(0, len(co), 3)]
        bvh = BVHTree.FromPolygons(verts, [tuple(p.vertices) for p in me.polygons],
                                  all_triangles=False, epsilon=0.0)
        stats["terrain"] = terrain_obj.name

    def surface_z(x, y, flat_z):
        if bvh is None:
            return flat_z
        hit, _n, _i, _d = bvh.ray_cast(Vector((x, y, 20000.0)),
                                       Vector((0.0, 0.0, -1.0)), 80000.0)
        return hit.z if hit is not None else None

    slot_of = {}
    all_verts, all_faces, face_slot = [], [], []
    for pav in pavs:
        boundary = pav.outline_m(georef, step)      # outline with its holes bridged
        if len(boundary) < 3:
            continue
        tris = AC.ear_clip(boundary)
        if not tris:
            continue
        flat_z = elev_by_id.get(pav.airport, 0.0)
        base = len(all_verts)
        index, local = {}, []

        def add(pt):
            key = (round(pt[0], 2), round(pt[1], 2))
            i = index.get(key)
            if i is None:
                z = surface_z(pt[0], pt[1], flat_z)
                if z is None:
                    return None
                i = index[key] = base + len(local)
                local.append((pt[0], pt[1], z + lift))
            return i

        for tri in tris:
            for piece in _subdivide((boundary[tri[0]], boundary[tri[1]],
                                    boundary[tri[2]]), step * 1.6):
                ia, ib, ic = (add(piece[0]), add(piece[1]), add(piece[2]))
                if ia is None or ib is None or ic is None:
                    stats["unplaced"] += 1
                    continue
                if ia == ib or ib == ic or ic == ia:
                    continue
                q0, q1, q2 = piece
                if abs((q1[0] - q0[0]) * (q2[1] - q0[1])
                       - (q2[0] - q0[0]) * (q1[1] - q0[1])) < 0.05:
                    continue                        # sliver, no surface to see
                if pav.surface not in slot_of:
                    slot_of[pav.surface] = len(slot_of)
                all_faces.append((ia, ib, ic))
                face_slot.append(slot_of[pav.surface])
        all_verts.extend(local)
        stats["pavements"] += 1

    if not all_faces:
        return None, stats

    me = bpy.data.meshes.new(name)
    me.from_pydata([tuple(v) for v in all_verts], [], [tuple(f) for f in all_faces])
    me.update()
    obj = bpy.data.objects.new(name, me)
    (bpy.context.collection or bpy.context.scene.collection).objects.link(obj)
    for surface, _slot in sorted(slot_of.items(), key=lambda kv: kv[1]):
        mat = bpy.data.materials.new("apt_%d_%s"
                                     % (surface,
                                        AC.surface_name(surface).replace(" ", "_")))
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        colour = AC.surface_color(surface)
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = (*colour, 1.0)
            try:
                bsdf.inputs["Roughness"].default_value = 0.85
            except Exception:                                  # noqa: BLE001
                pass
        mat.diffuse_color = (*colour, 1.0)
        me.materials.append(mat)
    me.polygons.foreach_set("material_index", np.asarray(face_slot, dtype=np.int32))
    me.polygons.foreach_set("use_smooth", np.ones(len(me.polygons), dtype=bool))
    stats["verts"] = len(all_verts)
    stats["triangles"] = len(all_faces)
    return obj, stats


def _tile_id_for(lat, lon):
    """X-Plane / Ortho4XP tile name a lat/lon falls in, e.g. +61-149."""
    return "%+03d%+04d" % (math.floor(lat), math.floor(lon))


def _pavement_bounds(pavs):
    """lon/lat box of the given pavements (used when there is no terrain tile)."""
    lons, lats = [], []
    for pav in pavs:
        for lat, lon in pav.ring:
            lons.append(lon)
            lats.append(lat)
    if not lons:
        return None
    return (min(lons), max(lons), min(lats), max(lats))


class IMPORT_OT_xplane_apt(bpy.types.Operator, ImportHelper):
    """Import airport pavement boundaries from an apt.dat file"""
    bl_idname = "import_scene.xplane_apt"
    bl_label = "Import X-Plane apt.dat (pavement)"
    bl_options = {"UNDO"}

    filename_ext = ".dat"
    filter_glob: StringProperty(default="*.dat", options={"HIDDEN"})

    airports: StringProperty(
        name="Airports", default="",
        description="Comma-separated airport identifiers to import (KLVM, 29S, ...). "
                    "Leave empty to take the airports inside the scene's tile (DSF or "
                    ".mesh), or every airport in the file when there is no tile",
    )
    use_tile: BoolProperty(
        name="Use the scene's tile", default=True,
        description="When a tile is in the scene (a DSF import or an Ortho4XP .mesh "
                    "import), take the airports inside it and drape the pavement onto "
                    "its terrain. Off, or with no tile, the pavement is placed flat at "
                    "each airport's own elevation — apt.dat does not need a terrain "
                    "import to work",
    )
    step_m: FloatProperty(
        name="Grid (m)", default=20.0, min=2.0, max=200.0,
        description="Target edge length: smaller follows the terrain more closely and "
                    "costs more triangles",
    )
    max_airports: IntProperty(
        name="Airport limit", default=200, min=1, max=100000,
        description="Safety valve: without identifiers and without a tile, refuse a "
                    "file that holds more airports than this instead of building "
                    "thousands of pavements (the full worldwide apt.dat has ~40,000)",
    )
    lift_m: FloatProperty(
        name="Lift (m)", default=0.12, min=0.0, max=5.0,
        description="Height above the surface so the pavement is visible instead of "
                    "z-fighting with it",
    )

    def execute(self, context):
        # The DSF tile is a convenience, never a requirement: with one in the scene the
        # pavements are draped on its terrain and its bounds pick the airports, without
        # one they are placed flat at each airport's own apt.dat elevation.
        tile = _terrain_tile_for_pavements(context)
        ids = {s.strip().upper() for s in self.airports.replace(";", ",").split(",")
               if s.strip()}
        if ids:
            bounds = (-180.0, 180.0, -90.0, 90.0)
        elif tile is not None and self.use_tile:
            bounds = tile[2]
        else:
            bounds = (-180.0, 180.0, -90.0, 90.0)
            print("[apt.dat import] no airport identifiers and no tile in the scene: "
                  "importing every airport in the file")
        try:
            pavs, airports = AC.read_pavements(self.filepath, bounds, ids=ids or None)
        except Exception as ex:                                # noqa: BLE001
            traceback.print_exc()
            self.report({"ERROR"}, "apt.dat: %s" % ex)
            return {"CANCELLED"}
        if ids:
            pavs = [p for p in pavs if p.airport.upper() in ids]
            airports = [a for a in airports if a[0].upper() in ids]
        if not ids and tile is None and len(airports) > int(self.max_airports):
            self.report({"ERROR"},
                        "%s holds %d airports and no identifier was given — list the "
                        "ones you want (e.g. KLVM) or import the tile first"
                        % (os.path.basename(self.filepath), len(airports)))
            return {"CANCELLED"}
        if not pavs:
            self.report({"WARNING"}, "no pavement for %s in %s"
                        % (sorted(ids) or "this tile", os.path.basename(self.filepath)))
            return {"CANCELLED"}
        elev = {a[0]: (a[2] or 0.0) * 0.3048 for a in airports}
        box = _pavement_bounds(pavs)
        if box is None:
            self.report({"WARNING"}, "the pavements have no boundary to place")
            return {"CANCELLED"}
        # drape only when the tile is in the scene *and* these pavements lie inside it;
        # otherwise the tile's coordinate frame would put them in the wrong place
        use_terrain = False
        if tile is not None and self.use_tile:
            tw, te, ts, tn = tile[2]
            use_terrain = (box[0] >= tw - 0.02 and box[1] <= te + 0.02
                           and box[2] >= ts - 0.02 and box[3] <= tn + 0.02)
            if not use_terrain:
                print("[apt.dat import] the pavement lies outside %s; placing it flat"
                      % tile[0].name)
        if use_terrain:
            georef = tile[1]
            terrain = tile[0]
            name = terrain.name + "_pavement"
        else:
            georef = _georef_from_bounds(box[0] - 0.001, box[1] + 0.001,
                                         box[2] - 0.001, box[3] + 0.001)
            terrain = None
            name = "apt_pavement"
        try:
            obj, stats = _build_pavement_mesh(name, pavs, elev, georef, terrain,
                                              max(2.0, float(self.step_m)),
                                              float(self.lift_m))
        except Exception as ex:                                # noqa: BLE001
            traceback.print_exc()
            self.report({"ERROR"}, "pavement: %s" % ex)
            return {"CANCELLED"}
        if obj is None:
            self.report({"WARNING"}, "pavement boundaries found but none could be "
                                     "placed (%d unplaced)" % stats["unplaced"])
            return {"CANCELLED"}
        obj["apt_dat"] = self.filepath
        # the frame the pavement was placed in: without it nothing can re-drape or
        # convert the mesh back to lat/lon afterwards
        obj["apt_georef"] = json.dumps(georef)
        obj["apt_airports"] = json.dumps(sorted({p.airport for p in pavs}))
        msg = ("Pavement: %d chunk(s) from %d airport(s) — %d triangles, %s"
               % (stats["pavements"], len({p.airport for p in pavs}),
                  stats["triangles"],
                  ("draped on %s" % stats["terrain"]) if stats["draped"]
                  else "flat at each airport's elevation"))
        print("[apt.dat import] " + msg)
        self.report({"INFO"}, msg)
        for o in context.view_layer.objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {"FINISHED"}


class IMPORT_OT_xplane_dsf(bpy.types.Operator, ImportHelper):
    """Import the terrain mesh of an X-Plane DSF scenery file"""
    bl_idname = "import_scene.xplane_dsf"
    bl_label = "Import X-Plane DSF"
    bl_options = {"UNDO"}

    filename_ext = ".dsf"
    filter_glob: StringProperty(default="*.dsf", options={"HIDDEN"})

    import_materials: BoolProperty(
        name="Material per terrain definition",
        description="Give every terrain definition (the DSF's TERT table) its own "
                    "material slot, so the ground the sim renders per type is "
                    "visible in Blender",
        default=True,
    )
    import_textures: BoolProperty(
        name="Terrain textures (.ter)",
        description="Resolve each terrain definition to its .ter (or bitmap) and "
                    "load its BASE_TEX / NORMAL_TEX into the material. Built-in "
                    "types such as terrain_Water have no file on disk and are "
                    "reported separately",
        default=True,
    )
    import_uvs: BoolProperty(
        name="UVs from the .ter mapping",
        description="Write one UV layer from each terrain type's .ter — the "
                    "PROJECTED metres-per-texture scale, or its LOAD_CENTER "
                    "footprint. Without it the textures have no UVs to sample",
        default=True,
    )
    xp_root: StringProperty(
        name="X-Plane root",
        description="Folder containing 'Resources/default scenery', used to resolve "
                    "the DSF's lib/... terrain references. Leave empty to walk up "
                    "from the .dsf",
        default="", subtype="DIR_PATH",
    )
    extra_tex_roots: StringProperty(
        name="Extra texture roots",
        description="Optional; separated by ':' (';' on Windows). Additional folders "
                    "searched for terrain textures, for scenery packages that ship "
                    "their own",
        default="",
    )
    tex_max_px: IntProperty(
        name="Max texture size", default=2048, min=0, max=8192,
        description="Decode each terrain texture and downscale it to at most this "
                    "many pixels (0 = native size). A DSF tile references dozens of "
                    "terrain types, and X-Plane's own textures are 1024-4096 px",
    )
    zproj: BoolProperty(
        name="Project textures along Z (fixed map)", default=True,
        description="Feed each texture from a Z projection of the vertex positions "
                    "instead of the baked UV layer, so the ground texture stays "
                    "nailed to the map while vertices move — the same behaviour the "
                    ".mesh importer offers",
    )
    osm_export: BoolProperty(
        name="Write .osm",
        description="Save the imported polygon boundaries as OSM XML next to the DSF "
                    "(<dsf>.patch.osm), one way per winding with the X-Plane class in "
                    "the xplane_class tag, so JOSM can open and edit them",
        default=True,
    )
    osm_path: StringProperty(
        name=".osm file", subtype="FILE_PATH", default="",
        description="Where to write the .osm; leave empty for <dsf>.patch.osm next to "
                    "the DSF",
    )
    smooth: BoolProperty(name="Smooth shading", default=True,
                         description="Shade the terrain smooth (it looks faceted "
                                     "otherwise)")
    polygons: BoolProperty(
        name="Polygon outlines",
        description="Also import the DSF's polygons as plain boundaries (edges, no "
                    "faces, no texture): .pol and .fac polygons and legacy class "
                    "names become closed outlines, .lin line features open polylines, "
                    "all draped on the terrain. Separate object, <tile>_polygons",
        default=True,
    )
    forests: BoolProperty(
        name="Include forest areas (.for)",
        description="Also draw forest boundaries. A Global Scenery tile holds "
                    "thousands of forest polygons (~9,000 on +45-111), so it is off "
                    "by default",
        default=False,
    )
    mark_seams: BoolProperty(
        name="Seams from UV islands", default=False,
        description="Mark the borders between terrain-type UV islands as seams. Off "
                    "by default: a DSF tile has hundreds of terrain types and the "
                    "edge sort costs seconds on a 500k-triangle tile",
    )
    report_unhandled: BoolProperty(
        name="Report skipped content", default=True,
        description="List the objects / polygons / vector networks / raster layers "
                    "that were parsed but not imported (out of scope: this add-on "
                    "imports the terrain mesh)",
    )

    def _import_overlay(self, context):
        """A DSF with polygons but no terrain: import the outlines on their own.

        Overlay sceneries (a scenery package's own .dsf, the Global Airports layer)
        carry no terrain patches. Their polygons still describe real places, so they
        are draped on any tile already in the scene and placed at the tile origin
        otherwise instead of the import dying with 'no terrain mesh'.
        """
        dsf = DC.load_dsf(self.filepath)
        west, east, south, north = _dsf_bounds(dsf)
        georef = _georef_from_bounds(west, east, south, north)
        tile = _terrain_tile_for_pavements(context)
        terrain = None
        if tile is not None:
            tw, te, ts, tn = tile[2]
            if (west >= tw - 0.01 and east <= te + 0.01
                    and south >= ts - 0.01 and north <= tn + 0.01):
                terrain = tile[0]
            else:
                print("[X-Plane DSF import] %s does not cover this DSF (tile %s, not %s)"
                      % (tile[0].name, _tile_id_for((south + north) * 0.5,
                                                    (west + east) * 0.5), tile[0].name))
        poly_obj, pstats = _build_polygon_outlines(terrain, dsf, georef, self.forests)
        if poly_obj is None:
            self.report({"WARNING"},
                        "DSF: no terrain and no polygon to import in %s"
                        % os.path.basename(self.filepath))
            return {"CANCELLED"}
        _write_polygon_osm(self, dsf, self.forests)
        msg = ("overlay DSF (no terrain): %d polygon(s), %d ring(s), %d point(s) in %s "
               "— %s" % (pstats["polygons"], pstats["rings"], pstats["points"],
                         poly_obj.name,
                         "draped on %s" % terrain.name if terrain is not None
                         else "no tile in the scene, placed from lat/lon 0 m up"))
        print("[X-Plane DSF import] " + msg)
        self.report({"INFO"}, msg)
        if pstats["shown"]:
            print("[X-Plane DSF import] polygon classes: "
                  + ", ".join("%s x%d" % kv for kv in pstats["shown"]))
        context.view_layer.objects.active = poly_obj
        poly_obj.select_set(True)
        return {"FINISHED"}

    def execute(self, context):
        try:
            obj, me, dsf, georef, stats = _import_dsf_terrain(self, context)
        except DC.DSFError as ex:
            if self.polygons and "no terrain mesh" in str(ex):
                return self._import_overlay(context)
            self.report({"ERROR"}, "DSF: %s" % ex)
            return {"CANCELLED"}
        except Exception as ex:                                # noqa: BLE001
            traceback.print_exc()
            self.report({"ERROR"}, "Failed to read %s: %s"
                        % (os.path.basename(self.filepath), ex))
            return {"CANCELLED"}

        for o in context.view_layer.objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj

        if self.polygons:
            poly_obj, pstats = _build_polygon_outlines(obj, dsf, georef, self.forests)
            if poly_obj is not None:
                _write_polygon_osm(self, dsf, self.forests)
                msg = ("Polygon outlines: %d polygon(s), %d ring(s), %d point(s) in %s%s"
                       % (pstats["polygons"], pstats["rings"], pstats["points"],
                          poly_obj.name,
                          (", " + ", ".join("%s x%d" % (k, v)
                                            for k, v in pstats["shown"]))
                          if pstats["shown"] else ""))
                print("[X-Plane DSF import] " + msg)
                self.report({"INFO"}, msg)
                if pstats["dropped"]:
                    msg = ("%d ring(s) fell outside the terrain and were left at the "
                           "tile's base height" % pstats["dropped"])
                    print("[X-Plane DSF import] " + msg)
                    self.report({"WARNING"}, msg)
            else:
                print("[X-Plane DSF import] no polygon to import%s"
                      % ("" if self.forests else
                         " (forest areas are skipped; tick Include forest areas)"))

        info = ("DSF %s%s: %d triangles, %d patches, %d terrain defs, lon %.6f..%.6f "
                "lat %.6f..%.6f" % (obj.name, " [7z]" if stats["compressed"] else "",
                                    stats["triangles"], stats["patches"],
                                    stats["defs"], georef["west"], georef["east"],
                                    georef["south"], georef["north"]))
        print("[X-Plane DSF import] " + info)
        self.report({"INFO"}, info)
        if stats.get("filled_from_raster"):
            msg = ("%d vertices carried no elevation in the mesh pools and were taken "
                   "from the tile's own raster DEM" % stats["filled_from_raster"])
            print("[X-Plane DSF import] " + msg)
            self.report({"INFO"}, msg)
        if stats.get("skipped_no_elevation"):
            msg = ("%d triangle(s) skipped: neither the pools nor a raster DEM give "
                   "those vertices a height" % stats["skipped_no_elevation"])
            print("[X-Plane DSF import] " + msg)
            self.report({"WARNING"}, msg)
        if stats["materials"]:
            msg = ("Terrain textures: %d materials textured, %d built-in (no file), "
                   "%d unresolved" % (stats["textured"], stats["builtin"],
                                      stats["unresolved"]))
            print("[X-Plane DSF import] " + msg)
            self.report({"INFO"}, msg)
            if stats["unresolved_list"]:
                print("[X-Plane DSF import] unresolved terrain types: "
                      + ", ".join(stats["unresolved_list"][:10]))
        if stats.get("uv_layer"):
            print("[X-Plane DSF import] UV layer '%s' on %d loops"
                  % (stats["uv_layer"], stats["uv_mapped"]))
        if self.report_unhandled and dsf.unhandled:
            names = {7: "objects", 8: "object-ranges", 9: "network chains",
                     10: "network ranges", 11: "network chains32",
                     12: "polygons", 13: "polygon-ranges", 14: "nested polygons",
                     15: "nested polygon ranges", 32: "comments", 33: "comments",
                     34: "comments"}
            parts = ["%d %s" % (dsf.unhandled[c], names.get(c, "cmd %d" % c))
                     for c in sorted(dsf.unhandled)]
            msg = "Not imported (terrain only): " + ", ".join(parts)
            print("[X-Plane DSF import] " + msg)
            self.report({"INFO"}, msg)
        return {"FINISHED"}


# --------------------------------------------------------------- import
class IMPORT_OT_ortho4xp_mesh(bpy.types.Operator, ImportHelper):
    """Import an Ortho4XP tile mesh (.mesh)"""
    bl_idname = "import_scene.ortho4xp_mesh"
    bl_label = "Import Ortho4XP Mesh"
    bl_options = {"UNDO"}

    filename_ext = ".mesh"
    filter_glob: StringProperty(default="*.mesh", options={"HIDDEN"})

    normals_mode: EnumProperty(
        name="Vertex normals",
        description="The file stores only the horizontal components (nx, ny) of "
                    "each normal, rounded to 2 decimals. 'Attribute only' keeps "
                    "them for a faithful export without changing the shading; "
                    "'Custom split normals' also makes Blender shade with them; "
                    "'Ignore' drops them (export then recomputes them)",
        items=[("ATTR", "Keep as attribute (fast)",
                "Store nx, ny in a 'mesh_normals' attribute, leave shading to "
                "Blender's geometric normals"),
               ("SPLIT", "Custom split normals",
                "Additionally apply them as custom split normals so the shading "
                "matches the tile"),
               ("NONE", "Ignore",
                "Do not store normals; export will recompute them from geometry")],
        default="ATTR",
    )
    attr_slots: BoolProperty(
        name="Material slot per triangle attribute",
        description="Give every triangle attribute (water, sea, runway, ...) its "
                    "own material slot with a viewport colour, so the water cuts "
                    "and airport flattening are visible at a glance. Skipped when "
                    "terrain textures are imported: a face has one material slot, "
                    "and the orthophoto wins",
        default=True,
    )
    import_textures: BoolProperty(
        name="Terrain textures (.ter)",
        description="Read the .ter files of the tile next to the .mesh, work out "
                    "which ortho cell each face belongs to from their LOAD_CENTER "
                    "footprints, give every cell its own material and UV-map each "
                    "face once over its own cell. Turn off for a geometry-only "
                    "import",
        default=True,
    )
    terrain_dir: StringProperty(
        name="Terrain folder", subtype="DIR_PATH", default="",
        description="Folder holding the tile's .ter files; leave empty to use the "
                    "'terrain' folder next to the .mesh",
    )
    tex_limit: IntProperty(
        name="Max textures", default=0, min=0,
        description="Load images for at most this many cells, the cells owning the "
                    "most faces first (0 = every cell). Materials, metadata and UVs "
                    "are created for all of them either way; only the image is "
                    "skipped",
    )
    tex_max_px: IntProperty(
        name="Max texture size", default=0, min=0, max=8192,
        description="Decode each texture and downscale it to at most this many "
                    "pixels (0 = leave at native size). A 4096x2560 orthophoto "
                    "costs ~250 MB once Blender decodes it and a ZL18 tile has "
                    "~1000 cells, so 1024-2048 keeps a whole tile viewable",
    )
    smooth: BoolProperty(
        name="Smooth shading",
        description="Shade the tile smooth (terrain looks faceted otherwise)",
        default=True,
    )
    zproj: BoolProperty(
        name="Project textures along Z (fixed map)",
        description="Feed every texture from a Z-axis projection of the vertex "
                    "positions instead of the baked UV layer, so the orthophoto "
                    "stays nailed to the map while you drag vertices around -- the "
                    "same thing X-Plane does with the same tile. The UV layer is "
                    "still written (Workbench's solid view and other tools read it) "
                    "and can be re-anchored with 'Projected UVs' after editing",
        default=True,
    )
    mark_seams: BoolProperty(
        name="Seams from UV islands",
        description="After the terrain UVs are written, mark the borders between "
                    "UV islands as seams -- Blender's UV > Seams from Islands, so "
                    "the seams show up in the 3D viewport and 'Select Linked' / "
                    "'Rip' pick a whole ortho cell. The cell borders are adjacent "
                    "faces that sample different orthophotos, so this is what tells "
                    "the cells apart. Needs the UV layer, so it is skipped when "
                    "'Terrain textures (.ter)' is off",
        default=True,
    )
    report_extra: BoolProperty(
        name="Report non-standard input",
        description="Report skipped sections, non-zero vertex references and "
                    "line endings",
        default=True,
    )

    def execute(self, context):
        try:
            m = MC.read_mesh(self.filepath)
        except MC.MeshError as ex:
            self.report({"ERROR"}, "mesh: %s" % ex)
            return {"CANCELLED"}
        except Exception as ex:                                # noqa: BLE001
            self.report({"ERROR"}, "Failed to read %s: %s"
                        % (os.path.basename(self.filepath), ex))
            return {"CANCELLED"}

        west, east, south, north = m.bounds
        georef = _georef_from_bounds(west, east, south, north)
        name = os.path.splitext(os.path.basename(self.filepath))[0]

        co = _geo_to_blender(georef, m.lon, m.lat, m.alt_m)
        me = _new_vertex_mesh(name, co, m.tris)

        obj = bpy.data.objects.new(name, me)
        (context.collection or context.scene.collection).objects.link(obj)
        try:
            for o in context.view_layer.objects:
                o.select_set(False)
            obj.select_set(True)
            context.view_layer.objects.active = obj
        except Exception:                                      # noqa: BLE001
            pass

        # ---- triangle attributes (survive export verbatim)
        _add_face_attr(me, "mesh_attr", m.attr)

        # ---- vertex normals
        have_normals = m.nx is not None and self.normals_mode != "NONE"
        if have_normals:
            vec = np.ascontiguousarray(m.normal_xyz, dtype=np.float32)
            try:
                a = me.attributes.new(name="mesh_normals", type="FLOAT_VECTOR",
                                      domain="POINT")
                a.data.foreach_set("vector", vec.ravel())
            except Exception as ex:                            # noqa: BLE001
                print("[Ortho4XP mesh] could not store normals: %s" % ex)
            if self.normals_mode == "SPLIT":
                try:
                    me.normals_split_custom_set_from_vertices(vec)
                except Exception:                              # noqa: BLE001
                    try:
                        me.normals_split_custom_set_from_vertices(
                            [tuple(float(x) for x in v) for v in vec])
                    except Exception as ex:                    # noqa: BLE001
                        print("[Ortho4XP mesh] custom split normals not "
                              "supported by this Blender build: %s" % ex)

        if self.smooth:
            flags = np.ones(len(me.polygons), dtype=bool)
            me.polygons.foreach_set("use_smooth", flags)

        # ---- terrain textures from the tile's .ter cells
        tex_stats = None
        if self.import_textures:
            try:
                tex_stats = _import_terrain_textures(self, obj, me, m, georef)
            except Exception as ex:                            # noqa: BLE001
                # the tile itself imported fine; a traceback here is the only way to
                # find which cell/face made the texture pass give up
                traceback.print_exc()
                print("[Ortho4XP mesh import] terrain textures failed: %s" % ex)
                self.report({"WARNING"}, "Terrain textures failed: %s" % ex)

        # ---- material slots per triangle attribute
        # (only without textures: a face has one material index, so the two
        # assignments cannot coexist -- the orthophoto wins)
        slots = {}
        counts = m.attribute_counts()
        if self.attr_slots and not (tex_stats and tex_stats.get("materials")):
            for value in counts:
                label = MC.attribute_name(value)
                mat = bpy.data.materials.new("mesh_attr_%d_%s" % (value, label))
                mat.diffuse_color = _attr_color(value)
                mat.roughness = 1.0
                slots[value] = len(obj.data.materials)
                obj.data.materials.append(mat)
            if slots:
                face_slot = np.zeros(m.n_tri, dtype=np.int32)
                for value, slot in slots.items():
                    face_slot[m.attr == value] = slot
                me.polygons.foreach_set("material_index", face_slot)

        # ---- UV island borders as seams (Blender's UV > *Seams from Islands*)
        if self.mark_seams:
            n_seams = _mark_seams_from_islands(me)
            obj["mesh_seams"] = n_seams
            if n_seams:
                print("[Ortho4XP mesh import] %d edge(s) marked as seams from "
                      "the UV islands (one island = one ortho cell)" % n_seams)
            elif me.uv_layers:
                print("[Ortho4XP mesh import] every neighbouring face agrees on "
                      "its UVs: no island border to mark as a seam")
            else:
                print("[Ortho4XP mesh import] no UV layer to read (no terrain "
                      "textures on this import), so no island seam to mark")

        # ---- storable metadata (drives the export and the sidebar panel)
        obj["mesh_georef"] = json.dumps(georef)
        obj["mesh_meta"] = json.dumps({
            "version": m.version,
            "version_text": m.version_text,
            "flavour": m.flavour,
            "vert_fmt": m.vert_fmt,
            "normal_cols": m.normal_cols,
            "crlf": m.crlf,
            "vertices": m.n_vert,
            "triangles": m.n_tri,
            "attributes": [MC.attribute_name(v) for v in counts],
            "attribute_counts": {str(k): v for k, v in counts.items()},
            "normals": have_normals,
            "unknown_sections": m.unknown_sections,
            "vtag_nonzero": (None if m.vtag is None
                             else int(np.count_nonzero(m.vtag))),
            "source": os.path.basename(self.filepath),
        })
        if tex_stats is not None:
            obj["mesh_tex_stats"] = json.dumps(tex_stats)
        obj["mesh_zproj"] = bool(tex_stats and self.zproj)
        obj["mesh_source"] = self.filepath
        obj["mesh_unit_scale"] = 1.0        # Blender units are metres here

        info = ("Ortho4XP mesh '%s' (v%s): %d vertices, %d triangles, attrs %s"
                % (name, m.version_text, m.n_vert, m.n_tri, ", ".join(
                    "%s(%d)" % (MC.attribute_name(v), c)
                    for v, c in counts.items())))
        print("[Ortho4XP mesh import] " + m.summary())
        print("[Ortho4XP mesh import] lon %.6f..%.6f lat %.6f..%.6f, tile origin "
              "%.1f/%.1f, local metres, %s, flavour %s"
              % (west, east, south, north, georef["lon0"], georef["lat0"],
                 "CRLF" if m.crlf else "LF", m.flavour))
        if tex_stats is not None:
            print("[Ortho4XP mesh import] terrain: %d cell(s) in %s, %d used by "
                  "faces, %d material(s), %d image(s) loaded, %d face(s) without "
                  "a cell"
                  % (tex_stats["cells"], tex_stats["terrain_dir"],
                     tex_stats["cells_used"], tex_stats["materials"],
                     tex_stats["images_loaded"], tex_stats["faces_uncovered"]))
            if tex_stats["faces_uncovered"]:
                frac = 100.0 * tex_stats["faces_uncovered"] / max(1, m.n_tri)
                self.report({"INFO"}, "%d face(s) (%.2f%%) have no ortho cell and "
                            "got the 'ter_none_uncovered' slot"
                            % (tex_stats["faces_uncovered"], frac))
            if tex_stats["skipped_ter"]:
                print("[Ortho4XP mesh import] %d .ter file(s) skipped (no "
                      "LOAD_CENTER footprint)" % tex_stats["skipped_ter"])
        if self.report_extra:
            if m.unknown_sections:
                print("[Ortho4XP mesh import] skipped sections: %s"
                      % ", ".join(m.unknown_sections))
            if m.vtag is not None and np.count_nonzero(m.vtag):
                print("[Ortho4XP mesh import] %d vertices carry a non-zero "
                      "reference (written back as 0)"
                      % int(np.count_nonzero(m.vtag)))
            if m.nz is not None and np.any(m.nz != 0):
                print("[Ortho4XP mesh import] the file has non-zero third normal "
                      "components; they are preserved on export unless 'Zero "
                      "third component' is ticked")

        # ---- the tile is ~111 km across, so the default 1000 m viewport clip end
        # cuts most of it away; park the far plane out of reach on every screen
        views = _far_viewport_clip()
        if views:
            print("[Ortho4XP mesh import] viewport clip end -> %.0f in %d 3D "
                  "view(s)" % (VIEW_CLIP_END, views))
        self.report({"INFO"}, info)
        return {"FINISHED"}


# --------------------------------------------------------------- export
class EXPORT_OT_ortho4xp_mesh(bpy.types.Operator, ExportHelper):
    """Export the selected/all tiles as an Ortho4XP .mesh file"""
    bl_idname = "export_scene.ortho4xp_mesh"
    bl_label = "Export Ortho4XP Mesh"
    bl_options = {"UNDO"}

    filename_ext = ".mesh"
    filter_glob: StringProperty(default="*.mesh", options={"HIDDEN"})

    use_selection: BoolProperty(
        name="Selected objects only",
        description="Export only the selected mesh objects (otherwise every mesh "
                    "carrying a mesh_georef)",
        default=False,
    )
    georef_mode: EnumProperty(
        name="Georeference",
        description="Where the local->WGS84 projection comes from",
        items=[("STORED", "From the object (mesh_georef)",
                "Inverse of the projection stored when the tile was imported"),
               ("CUSTOM", "Custom origin",
                "Use the tile origin entered below (for meshes modelled from "
                "scratch in Blender)")],
        default="STORED",
    )
    origin_lon: FloatProperty(
        name="Tile west longitude", default=-1.0, precision=6,
        description="Longitude of the south-west corner of the 1x1 degree tile",
    )
    origin_lat: FloatProperty(
        name="Tile south latitude", default=0.0, precision=6,
        description="Latitude of the south-west corner of the 1x1 degree tile",
    )
    flavour: EnumProperty(
        name="File flavour",
        description="Which writer's formatting to reproduce. Both flavours hold "
                    "identical geometry; they differ in the header version string, "
                    "the decimals on coordinates and whether the normal block has "
                    "a (literal 0) third column",
        items=[("SOURCE", "Same as the source file",
                "Keep the flavour of the imported tile"),
               ("V2", "Ortho4XP 1.4 (v2)",
                "'MeshVersionFormatted 2', '{:.15f}' coordinates, "
                "3-column normals -- what a current 1.4 build writes"),
               ("V13", "Ortho4XP 1.30",
                "'MeshVersionFormatted 1.30', '{:.7f}' coordinates, "
                "2-column normals -- what 1.30 wrote")],
        default="SOURCE",
    )
    line_ending: EnumProperty(
        name="Line endings",
        description="Ortho4XP writes the file in text mode, so a Windows build "
                    "produces CRLF",
        items=[("SOURCE", "Same as the source file",
                "Keep whatever the imported file used"),
               ("CRLF", "CRLF (Windows)", "Carriage return + line feed"),
               ("LF", "LF (Unix/macOS)", "Line feed only")],
        default="SOURCE",
    )
    recompute_normals: BoolProperty(
        name="Recompute normals",
        description="Derive the normals from the edited geometry instead of "
                    "writing back the nx, ny imported from the file. Needed after "
                    "moving vertices -- the DSF builder bakes these normals into "
                    "the tile's normal map",
        default=False,
    )
    zero_nz: BoolProperty(
        name="Zero third normal component",
        description="Write a literal 0 as the third normal component, as Ortho4XP "
                    "1.4 does. Only affects the v2 flavour: the 1.30 flavour has "
                    "no third column",
        default=True,
    )

    def execute(self, context):
        objs = _tile_objects(context, self.use_selection)
        if not objs:
            self.report({"ERROR"},
                        "No mesh objects to export. Import a .mesh first, or tick "
                        "'Selected objects only' and select the meshes to write.")
            return {"CANCELLED"}

        # ---- georeference (one tile per .mesh file)
        georef = None
        crlf_from_source = None
        if self.georef_mode == "CUSTOM":
            georef = _georef_from_bounds(self.origin_lon, self.origin_lon + 1.0,
                                         self.origin_lat, self.origin_lat + 1.0)
        else:
            for o in objs:
                if "mesh_georef" in o:
                    try:
                        georef = json.loads(o["mesh_georef"])
                        break
                    except Exception:                          # noqa: BLE001
                        continue
            if georef is None:
                self.report({"ERROR"},
                            "No 'mesh_georef' on the selected meshes. Import a "
                            ".mesh first, or switch Georeference to 'Custom "
                            "origin' and enter the tile corner.")
                return {"CANCELLED"}
            for o in objs:
                if "mesh_georef" not in o:
                    continue
                g = json.loads(o["mesh_georef"])
                if (abs(g["lon0"] - georef["lon0"]) > 1e-6
                        or abs(g["lat0"] - georef["lat0"]) > 1e-6):
                    self.report({"ERROR"},
                                "The selected meshes belong to different tiles "
                                "(lon0/lat0 differ). One .mesh covers a single 1x1 "
                                "degree tile -- export one tile at a time.")
                    return {"CANCELLED"}

        src_flavour = None
        for o in objs:
            meta = o.get("mesh_meta")
            if meta:
                try:
                    d = json.loads(meta)
                    crlf_from_source = d.get("crlf")
                    src_flavour = d.get("flavour")
                    break
                except Exception:                              # noqa: BLE001
                    pass

        # ---- flavour: the source's own formatting unless overridden
        if self.flavour == "V2":
            flavour = MC.FLAVOUR_V2
        elif self.flavour == "V13":
            flavour = MC.FLAVOUR_V13
        else:
            flavour = {MC.FLAVOUR_V2: MC.FLAVOUR_V2,
                       MC.FLAVOUR_V13: MC.FLAVOUR_V13}.get(src_flavour, MC.FLAVOUR_V2)
            if src_flavour is None:
                print("[Ortho4XP mesh export] no source flavour recorded; writing "
                      "the Ortho4XP 1.4 (v2) flavour")
        ver_txt, vert_fmt, normal_cols = FLAVOURS[flavour]

        # ---- gather geometry
        lon_parts, lat_parts, alt_parts = [], [], []
        nx_parts, ny_parts, nz_parts = [], [], []
        tri_parts, attr_parts = [], []
        offset = 0
        n_gons = n_gon_tris = 0
        no_attr = 0
        for o in objs:
            me = o.data
            mw, ident = _object_matrix(o)
            n_v = len(me.vertices)
            co = np.empty(n_v * 3, dtype=np.float64)
            me.vertices.foreach_get("co", co)
            co = co.reshape(-1, 3)
            if not ident:
                co = co @ mw[:3, :3].T + mw[:3, 3]
            lon, lat, alt = _blender_to_geo(georef, co)

            # normals: imported ones (exact) or derived from the geometry
            a = me.attributes.get("mesh_normals") if me.attributes else None
            if not self.recompute_normals and a is not None:
                vec = np.empty(n_v * 3, dtype=np.float32)
                a.data.foreach_get("vector", vec)
                nrm = vec.reshape(-1, 3).astype(np.float64)
                if not ident:
                    nrm = nrm @ np.linalg.inv(mw[:3, :3]).T
                ln = np.linalg.norm(nrm, axis=1, keepdims=True)
                nrm = nrm / np.where(ln > 1e-12, ln, 1.0)
            else:
                vec = np.empty(n_v * 3, dtype=np.float32)
                try:
                    me.vertex_normals.foreach_get("vector", vec)
                    nrm = vec.reshape(-1, 3).astype(np.float64)
                except Exception:                              # noqa: BLE001
                    nrm = np.tile((0.0, 0.0, 1.0), (n_v, 1))
                if not ident:
                    nrm = nrm @ np.linalg.inv(mw[:3, :3]).T

            # faces -> loop triangles (handles n-gons and quads)
            me.calc_loop_triangles()
            lt = me.loop_triangles
            n_t = len(lt)
            tvi = np.empty(n_t * 3, dtype=np.int32)
            tpi = np.empty(n_t, dtype=np.int32)
            lt.foreach_get("vertices", tvi)
            lt.foreach_get("polygon_index", tpi)
            tri = tvi.reshape(-1, 3).astype(np.int64) + offset

            n_poly = len(me.polygons)
            if n_t != n_poly:
                n_gons += n_poly - n_t
                n_gon_tris += n_t
            fa = me.attributes.get("mesh_attr") if me.attributes else None
            if fa is not None:
                fv = np.empty(n_poly, dtype=np.int32)
                fa.data.foreach_get("value", fv)
                attr = fv[tpi].astype(np.int64)
            else:
                attr = np.zeros(n_t, dtype=np.int64)
                no_attr += n_t

            lon_parts.append(lon)
            lat_parts.append(lat)
            alt_parts.append(alt)
            nx_parts.append(nrm[:, 0])
            ny_parts.append(nrm[:, 1])
            nz_parts.append(np.zeros(n_v) if self.zero_nz else nrm[:, 2])
            tri_parts.append(tri)
            attr_parts.append(attr)
            offset += n_v

        lon = np.concatenate(lon_parts)
        lat = np.concatenate(lat_parts)
        alt = np.concatenate(alt_parts)
        mesh = MC.OrthoMesh(
            lon, lat, alt,
            np.concatenate(tri_parts), np.concatenate(attr_parts),
            version=float(ver_txt), version_text=ver_txt,
            vert_fmt=vert_fmt, normal_cols=normal_cols,
            nx=np.concatenate(nx_parts), ny=np.concatenate(ny_parts),
            nz=np.concatenate(nz_parts),
            crlf=(crlf_from_source if (self.line_ending == "SOURCE"
                                       and crlf_from_source is not None)
                  else self.line_ending != "LF"),
        )

        try:
            nbytes = MC.write_mesh(self.filepath, mesh, flavour=flavour)
        except Exception as ex:                                # noqa: BLE001
            self.report({"ERROR"}, "Export failed: %s" % ex)
            return {"CANCELLED"}

        counts = mesh.attribute_counts()
        msg = ("Wrote %s (%d bytes): %d vertices, %d triangles from %d object(s), "
               "lon %.6f..%.6f lat %.6f..%.6f, flavour %s, attrs %s"
               % (os.path.basename(self.filepath), nbytes, mesh.n_vert,
                  mesh.n_tri, len(objs), lon.min(), lon.max(), lat.min(),
                  lat.max(), flavour, ", ".join("%s(%d)" % (MC.attribute_name(v), c)
                                                for v, c in counts.items())))
        print("[Ortho4XP mesh export] " + msg)
        if n_gons:
            self.report({"INFO"},
                        "%d polygon(s) were split into %d triangles on export"
                        % (n_gons, n_gon_tris))
            print("[Ortho4XP mesh export] triangulated %d n-gon/quad face(s) "
                  "-> %d triangles" % (n_gons, n_gon_tris))
        if no_attr:
            print("[Ortho4XP mesh export] %d face(s) had no 'mesh_attr' "
                  "attribute and were written as DUMMY (0)" % no_attr)
        if self.line_ending == "SOURCE" and crlf_from_source is None:
            print("[Ortho4XP mesh export] no source line ending recorded; used CRLF")
        self.report({"INFO"}, msg)
        return {"FINISHED"}


# --------------------------------------------------------------- projection
def _georef_of(obj):
    raw = obj.get("mesh_georef")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:                                          # noqa: BLE001
        return None


def _tile_targets(context):
    """Tile objects the projection buttons act on: imported tiles, else selection."""
    objs = _tile_objects(context, False)
    if objs:
        return objs
    return [o for o in context.selected_objects
            if o.type == "MESH" and o.data and len(o.data.polygons)]


def _slot_affines(obj):
    """One (a_x, b_x, a_y, b_y) or None per material slot of the object."""
    g = _georef_of(obj)
    return [(_material_affine(s.material, g) if s.material else None)
            for s in obj.material_slots]


class MESH_OT_zproject(bpy.types.Operator):
    """Add or remove the Z-axis texture projection in the tile's materials"""
    bl_idname = "mesh.ortho4xp_zproject"
    bl_label = "Z-axis projection"
    bl_description = ("Feed the tile's textures from a Z-axis projection of the "
                      "vertex positions (fixed map) or put them back on the baked "
                      "UV layer")
    bl_options = {"UNDO"}

    enable: BoolProperty(name="Enable", default=True)

    def execute(self, context):
        objs = _tile_targets(context)
        if not objs:
            self.report({"WARNING"}, "No Ortho4XP tile mesh to project")
            return {"CANCELLED"}
        mats = 0
        for obj in objs:
            aff = _slot_affines(obj)
            for slot, s in enumerate(obj.material_slots):
                mat = s.material
                if mat is None:
                    continue
                if self.enable:
                    if _wire_material_zproject(mat, aff[slot]):
                        mats += 1
                elif _unwire_material_zproject(mat):
                    mats += 1
            obj["mesh_zproj"] = bool(self.enable)
        self.report({"INFO"}, "%s the Z projection on %d material(s) of %d tile(s)"
                    % ("Enabled" if self.enable else "Removed", mats, len(objs)))
        return {"FINISHED"}


class MESH_OT_reproject_uvs(bpy.types.Operator):
    """Re-anchor the UV layer on the vertices' current positions"""
    bl_idname = "mesh.ortho4xp_reproject_uvs"
    bl_label = "Projected UVs"
    bl_description = ("Recompute the XPlaneProjected UV layer from where the "
                      "vertices are now (uv = position * scale + offset, per cell). "
                      "Rendered views use the Z projection and do not need this; "
                      "Workbench's solid view, the UV editor and anything else "
                      "reading the UV layer do, after geometry has been moved")
    bl_options = {"UNDO"}

    def execute(self, context):
        objs = _tile_targets(context)
        if not objs:
            self.report({"WARNING"}, "No Ortho4XP tile mesh to reproject")
            return {"CANCELLED"}
        loops = 0
        for obj in objs:
            stats = _write_uv_layer(obj.data, _slot_affines(obj))
            loops += stats["mapped"]
            if stats["layer"]:
                obj["mesh_uv_layer"] = stats["layer"]
        self.report({"INFO"}, "Reprojected %d UV loop(s) on %d tile(s)"
                    % (loops, len(objs)))
        return {"FINISHED"}


# --------------------------------------------------------------- seams
class MESH_OT_seams_from_islands(bpy.types.Operator):
    """Mark the UV island borders as seams, in place"""
    bl_idname = "mesh.ortho4xp_seams_from_islands"
    bl_label = "Seams from Islands"
    bl_description = ("Mark every edge whose two faces hand it different UVs -- "
                      "what Blender's UV > Seams from Islands does, which on a "
                      "tile are the borders between ortho cells. Computed on the "
                      "datablock, so unlike the menu item it needs no edit mode "
                      "round trip and no second copy of the mesh")
    bl_options = {"UNDO"}

    def execute(self, context):
        objs = _tile_targets(context)
        if not objs:
            self.report({"WARNING"}, "No Ortho4XP tile mesh to mark")
            return {"CANCELLED"}
        edges = 0
        bare = [o.name for o in objs if not o.data.uv_layers]
        for obj in objs:
            seams = _mark_seams_from_islands(obj.data)
            obj["mesh_seams"] = seams
            edges += seams
        for name in bare:
            self.report({"WARNING"}, "%s has no UV layer: import the terrain "
                        "textures first" % name)
        self.report({"INFO"}, "%d seam edge(s) marked on %d tile(s)"
                    % (edges, len(objs)))
        return {"FINISHED"}


# --------------------------------------------------------------- panel
class MESH_PT_ortho4xp_panel(bpy.types.Panel):
    bl_label = "Ortho4XP Mesh"
    bl_idname = "MESH_PT_ortho4xp_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Ortho4XP"

    def draw(self, context):
        lay = self.layout
        lay.operator(IMPORT_OT_ortho4xp_mesh.bl_idname, icon="IMPORT")
        lay.operator(EXPORT_OT_ortho4xp_mesh.bl_idname, icon="EXPORT")
        obj = context.object
        if obj is None or "mesh_georef" not in obj:
            lay.separator()
            lay.label(text="No Ortho4XP tile selected", icon="INFO")
            return
        box = lay.box()
        box.label(text="Tile %s" % obj.name, icon="MESH_DATA")
        g = json.loads(obj["mesh_georef"])
        box.label(text="lon %.0f..%.0f  lat %.0f..%.0f"
                      % (g["west"], g["east"], g["south"], g["north"]))
        box.label(text="%d verts, %d faces"
                      % (len(obj.data.vertices), len(obj.data.polygons)))
        meta = obj.get("mesh_meta")
        if meta:
            try:
                d = json.loads(meta)
            except Exception:                                  # noqa: BLE001
                d = {}
            box.label(text="source v%s [%s], %s"
                          % (d.get("version_text", d.get("version", "?")),
                             d.get("flavour", "?"),
                             "CRLF" if d.get("crlf") else "LF"))
            cnt = d.get("attribute_counts") or {}
            if cnt:
                col = box.column(align=True)
                col.label(text="triangle attributes:")
                for value, n in sorted(cnt.items(), key=lambda kv: -kv[1]):
                    col.label(text="  %s: %d" % (MC.attribute_name(int(value)), n))
            if d.get("source"):
                box.label(text=d["source"], icon="FILE")
        tex = obj.get("mesh_tex_stats")
        if tex:
            try:
                t = json.loads(tex)
            except Exception:                                  # noqa: BLE001
                t = {}
            col = box.column(align=True)
            col.label(text="terrain textures:", icon="TEXTURE")
            col.label(text="  %d of %d cells used by faces"
                          % (t.get("cells_used", 0), t.get("cells", 0)))
            col.label(text="  %d faces textured, %d without a cell"
                          % (t.get("faces_textured", 0), t.get("faces_uncovered", 0)))
            if t.get("images_loaded"):
                col.label(text="  %d texture(s) loaded%s"
                              % (t["images_loaded"],
                                 (", capped at %d px" % t["max_px"])
                                 if t.get("max_px") else ""))
            else:
                col.label(text="  no texture image loaded", icon="INFO")
            if t.get("overlay_cells"):
                col.label(text="  %d sea overlay cell(s)" % t["overlay_cells"])
            if t.get("uv_layer"):
                col.label(text="  UVs in %s (%s of %s loop(s))"
                              % (t["uv_layer"], "{:,}".format(t.get("uv_mapped", 0)),
                                 "{:,}".format(t.get("uv_loops", 0))))
            if t.get("terrain_dir"):
                col.label(text=t["terrain_dir"], icon="FILE_FOLDER")
        seams = obj.get("mesh_seams")
        if seams is not None:
            box.label(text="%s seam edge(s) from the UV islands"
                          % "{:,}".format(int(seams)),
                      icon="EDGESEL")
        box.operator(MESH_OT_seams_from_islands.bl_idname, icon="EDGESEL")
        proj = box.column(align=True)
        proj.label(text="texture projection:", icon="TEXTURE")
        proj.label(text="  %s"
                   % ("Z axis, fixed on the map" if obj.get("mesh_zproj")
                      else "baked UV layer"))
        row = proj.row(align=True)
        row.operator(MESH_OT_zproject.bl_idname, text="Fix to map").enable = True
        row.operator(MESH_OT_zproject.bl_idname, text="Use UVs").enable = False
        proj.operator(MESH_OT_reproject_uvs.bl_idname, icon="UV")
        sub = lay.column(align=True)
        sub.label(text="vertex coords are metres:", icon="INFO")
        sub.label(text="X east, Y north, Z up")


# --------------------------------------------------------------- registration
CLASSES = (IMPORT_OT_ortho4xp_mesh, EXPORT_OT_ortho4xp_mesh,
           IMPORT_OT_xplane_dsf, IMPORT_OT_xplane_apt,
           MESH_OT_zproject, MESH_OT_reproject_uvs,
           MESH_OT_seams_from_islands, MESH_PT_ortho4xp_panel)


def _menu_import(self, context):
    self.layout.operator(IMPORT_OT_ortho4xp_mesh.bl_idname,
                         text="Ortho4XP Mesh (.mesh)")
    self.layout.operator(IMPORT_OT_xplane_dsf.bl_idname,
                         text="X-Plane DSF (terrain, .dsf)")
    self.layout.operator(IMPORT_OT_xplane_apt.bl_idname,
                         text="X-Plane apt.dat (pavement, .dat)")


def _menu_export(self, context):
    self.layout.operator(EXPORT_OT_ortho4xp_mesh.bl_idname,
                         text="Ortho4XP Mesh (.mesh)")


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)
    bpy.types.TOPBAR_MT_file_export.append(_menu_export)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(_menu_export)
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()