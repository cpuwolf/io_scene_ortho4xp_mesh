bl_info = {
    "name": "Ortho4XP Mesh Import/Export",
    "author": "built for rourou",
    "version": (1, 1, 0),
    "blender": (4, 0, 0),
    "location": "File > Import/Export > Ortho4XP Mesh; View3D > Sidebar > Ortho4XP",
    "description": "Read and write Ortho4XP tile meshes (.mesh) with the tile's "
                   ".ter orthophotos",
    "category": "Import-Export",
}

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

import bpy
import numpy as np
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
from bpy_extras.io_utils import ExportHelper, ImportHelper

from . import meshcodec as MC
from . import terrain_tiles as TT

R_EARTH = 6378137.0          # WGS84 semi-major axis, metres (matches O4_Geo_Utils)
DEG = math.pi / 180.0

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


def _store_ter_metadata(mat, cell):
    """Record what the cell's .ter said, so the material stays self-describing.

    Kept even when the texture itself is not loaded (see `tex_limit`): the .ter
    path, its texture roles and the mapping parameters are always on the material.
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


def _apply_terrain_texture(mat, cell, max_px=0):
    """Wire one cell's .ter into a material; returns how many images were loaded.

    Same policy as the DSF importer: the base texture drives Base Color, a
    NORMAL_TEX gets a normal-map node, and the remaining layers (lit, composite,
    border) are imported as labelled but unconnected nodes -- blending them needs
    the per-vertex factor that lives in the DSF, which a .mesh does not carry, so
    inventing a factor would be a lie.
    """
    _store_ter_metadata(mat, cell)
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
    # cap the images LAST: the colorspace writes above make Blender re-read each
    # file at its native size, which silently discards an earlier Image.scale()
    if max_px:
        for n in nt.nodes:
            if n.type == "TEX_IMAGE" and n.image is not None:
                _cap_image(n.image, max_px)
    return loaded


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
    slot_of = {}
    for i in order:
        cell = cells[int(i)]
        mat = bpy.data.materials.new("ter_%s" % cell.label)
        if int(i) in load_set:
            n_img = _apply_terrain_texture(mat, cell, int(op.tex_max_px))
            stats["images_loaded"] += n_img
            if n_img:
                stats["cells_with_image"] += 1
        else:
            _store_ter_metadata(mat, cell)
        slot_of[int(i)] = len(obj.data.materials)
        obj.data.materials.append(mat)

    # faces with no cell get their own slot, so a gap shows up instead of being
    # silently painted with a neighbour's texture
    if stats["faces_uncovered"]:
        gap = bpy.data.materials.new("ter_none_uncovered")
        gap.diffuse_color = (1.0, 0.15, 0.15, 1.0)
        gap["mesh_ter_kind"] = "none"
        slot_of[-1] = len(obj.data.materials)
        obj.data.materials.append(gap)
    face_slot = np.zeros(face_cell.size, dtype=np.int32)
    for i in np.unique(face_cell):
        face_slot[face_cell == i] = slot_of[int(i)]
    me.polygons.foreach_set("material_index", face_slot)
    stats["materials"] = len(obj.data.materials)

    # ---- UVs: per loop, computed from the cell that owns the face
    loop_total = np.empty(len(me.polygons), dtype=np.int32)
    me.polygons.foreach_get("loop_total", loop_total)
    loop_vert = np.empty(len(me.loops), dtype=np.int32)
    me.loops.foreach_get("vertex_index", loop_vert)
    loop_cell = np.repeat(face_cell, loop_total)
    co = np.empty(len(me.vertices) * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    lat_l = georef["lat0"] + co[loop_vert, 1] / georef["m_per_deg_lat"]
    lon_l = georef["lon0"] + co[loop_vert, 0] / georef["m_per_deg_lon"]
    u = np.zeros(lat_l.size)
    v = np.zeros(lat_l.size)

    valid = loop_cell >= 0
    lc = loop_cell[valid]
    if lc.size:
        n_cells = len(cells)
        c_lat = np.zeros(n_cells)
        c_lon = np.zeros(n_cells)
        c_size = np.ones(n_cells)
        c_center = np.zeros(n_cells, dtype=bool)
        c_xs = np.ones(n_cells)
        c_ys = np.ones(n_cells)
        c_ox = np.zeros(n_cells)
        c_oy = np.zeros(n_cells)
        c_proj = np.zeros(n_cells, dtype=bool)
        for i, c in enumerate(cells):
            if c.lat is not None:
                c_lat[i], c_lon[i] = c.lat, c.lon
                c_size[i] = c.size_m or 1.0
                c_center[i] = True
            if c.projected:
                c_proj[i] = True
                c_xs[i], c_ys[i] = c.projected[0], c.projected[1]
        hw = (georef["east"] - georef["west"]) * 0.5 * georef["m_per_deg_lon"]
        hh = (georef["north"] - georef["south"]) * 0.5 * georef["m_per_deg_lat"]
        llat = lat_l[valid]
        llon = lon_l[valid]
        uu = np.zeros(lc.size)
        vv = np.zeros(lc.size)
        center = c_center[lc]
        if np.any(center):
            uu[center], vv[center] = TT.uv_center(
                c_lat[lc[center]], c_lon[lc[center]], c_size[lc[center]],
                llat[center], llon[center])
        proj = c_proj[lc] & ~center
        if np.any(proj):
            lv = loop_vert[valid][proj]
            uu[proj], vv[proj] = TT.uv_projected(
                co[lv, 0], co[lv, 1], c_xs[lc[proj]], c_ys[lc[proj]],
                c_ox[lc[proj]], c_oy[lc[proj]], hw, hh)
        uv_skip = int(np.count_nonzero(~(center | proj)))
        if uv_skip:
            print("[Ortho4XP mesh import] %d loop(s) on faces whose .ter has neither "
                  "LOAD_CENTER nor PROJECTED: UVs left at 0" % uv_skip)
        u[valid] = uu
        v[valid] = vv

    flat = np.empty(lat_l.size * 2, dtype=np.float32)
    flat[0::2] = u
    flat[1::2] = v
    layer = me.uv_layers.get(UV_LAYER) or me.uv_layers.new(name=UV_LAYER)
    layer.data.foreach_set("uv", flat)
    obj["mesh_uv_layer"] = layer.name
    stats["uv_layer"] = layer.name
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
                col.label(text="  UVs in %s" % t["uv_layer"])
            if t.get("terrain_dir"):
                col.label(text=t["terrain_dir"], icon="FILE_FOLDER")
        sub = lay.column(align=True)
        sub.label(text="vertex coords are metres:", icon="INFO")
        sub.label(text="X east, Y north, Z up")


# --------------------------------------------------------------- registration
CLASSES = (IMPORT_OT_ortho4xp_mesh, EXPORT_OT_ortho4xp_mesh,
           MESH_PT_ortho4xp_panel)


def _menu_import(self, context):
    self.layout.operator(IMPORT_OT_ortho4xp_mesh.bl_idname,
                         text="Ortho4XP Mesh (.mesh)")


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