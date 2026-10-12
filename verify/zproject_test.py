"""Headless Blender test for the Z-axis texture projection of the addon.

    blender --background --factory-startup --python verify/zproject_test.py -- [outdir]

It builds a synthetic tile in `outdir` (a 1.2 km grid with two 2 km `LOAD_CENTER`
cells and a checker texture each), imports it through the addon, then checks:

  1. every image texture is fed from the Z projection (Texture Coordinate ->
     Vector Math Multiply Add), and its four numbers are the cell's affine map;
  2. the baked `XPlaneProjected` layer equals `uv = position * scale + offset`, and
     equals `terrain_tiles.uv_center` (the documented LOAD_CENTER mapping);
  3. rendering *reads* the projection: rewiring each material to Emission of the
     projection vector makes the rendered pixel at a known world point report the
     UV the shader computed there.  Compared against the affine of the triangle the
     camera ray actually hits, the same run reports the UV layer's own value.
  4. dragging vertices leaves the projection readout unchanged (the map is a
     function of position -- this is the "texture stays put" claim), while the baked
     UV layer goes stale, and the *Projected UVs* operator puts it back.

Two PNGs (projection on / off, same dragged geometry) are written to `outdir` as a
visual: with the projection the checker lines stay straight and aligned to the
world, without it they kink at the moved patch.
"""

import json
import math
import os
import sys
import time

import numpy as np

ADDON_DIR = os.path.expanduser(
    "~/Library/Application Support/Blender/5.2/scripts/addons/io_scene_ortho4xp_mesh")
sys.path.insert(0, ADDON_DIR)

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = os.path.abspath(argv[0]) if argv else os.path.join(
    os.environ.get("TMPDIR", "/tmp"), "ortho4xp_zproj_test")

import bpy                                                    # noqa: E402

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="io_scene_ortho4xp_mesh")
import io_scene_ortho4xp_mesh as ADDON                        # noqa: E402
import meshcodec as MC                                        # noqa: E402
import terrain_tiles as TT                                    # noqa: E402

assert hasattr(ADDON.TT, "cell_uv_affine"), \
    "the loaded addon has no cell_uv_affine: is the symlinked folder stale?"

# ---------------------------------------------------------------- synthetic tile
LAT0, LON0 = 47.5, 8.5
HALF = 600.0                 # mesh extent: +/- 600 m
NGRID = 12                   # 12x12 quads = 100 m spacing
CELL_M = 2000.0              # declared LOAD_CENTER footprint (m)
CELL_OFFSET_M = 750.0        # cell centres, either side of the tile centre
CHECKER = 16                 # checker squares across the texture (125 m each)
DRAG = (80.0, 50.0, 20.0)    # vertex drag (east, north, up), metres
FLAVOUR = MC.FLAVOUR_V2

M_LAT = TT.M_PER_DEG_LAT
M_LON = TT.M_PER_DEG_LON * math.cos(math.radians(LAT0))


def build_tile(root):
    """A tiny .mesh plus two .ter cells and their checker textures."""
    terrain = os.path.join(root, "terrain")
    textures = os.path.join(root, "textures")
    os.makedirs(terrain, exist_ok=True)
    os.makedirs(textures, exist_ok=True)

    # grid in metres -> WGS84 (the same tangent-plane projection as the addon)
    xs = np.linspace(-HALF, HALF, NGRID + 1)
    ys = np.linspace(-HALF, HALF, NGRID + 1)
    gx, gy = np.meshgrid(xs, ys, indexing="xy")
    lon = LON0 + gx.ravel() / M_LON
    lat = LAT0 + gy.ravel() / M_LAT
    alt = np.zeros(lon.size)
    tris = []
    for j in range(NGRID):
        for i in range(NGRID):
            v00 = j * (NGRID + 1) + i
            v10 = v00 + 1
            v01 = v00 + (NGRID + 1)
            v11 = v01 + 1
            # counter-clockwise seen from above, like Ortho4XP's own triangles
            tris.append((v00, v10, v11))
            tris.append((v00, v11, v01))
    tris = np.array(tris, dtype=np.int64)
    mesh = MC.OrthoMesh(lon, lat, alt, tris, np.zeros(tris.shape[0], dtype=np.int64),
                        version=2.0, version_text="2", nx=np.zeros(lon.size),
                        ny=np.zeros(lon.size), nz=np.zeros(lon.size))
    mesh_path = os.path.join(root, "Data+47+008.mesh")
    MC.write_mesh(mesh_path, mesh, flavour=FLAVOUR)

    dlon = CELL_OFFSET_M / M_LON
    cells = [("A", LON0 - dlon, (1.0, 0.1, 0.1)), ("B", LON0 + dlon, (0.1, 0.3, 1.0))]
    for name, clon, col in cells:
        with open(os.path.join(terrain, "%s.ter" % name), "w") as fh:
            fh.write("A\n800\nTERRAIN\n"
                     "LOAD_CENTER %.7f %.7f %.1f 4096\n"
                     "BASE_TEX_NOWRAP ../textures/%s.dds\n" % (LAT0, clon, CELL_M, name))
        write_checker(os.path.join(textures, "%s.png" % name), col)
    return mesh_path, terrain


def write_checker(path, col):
    """A dark/light checker with a coloured tint, `CHECKER` squares a side."""
    img = bpy.data.images.new(os.path.basename(path), width=CHECKER, height=CHECKER,
                              alpha=False)
    px = np.zeros((CHECKER, CHECKER, 4), dtype=np.float32)
    for j in range(CHECKER):
        for i in range(CHECKER):
            k = 1.0 if (i + j) % 2 else 0.25
            px[j, i, :3] = np.array(col) * k
            px[j, i, 3] = 1.0
    img.pixels.foreach_set(px.ravel())
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()
    bpy.data.images.remove(img)


# ---------------------------------------------------------------- scene helpers
RES = 64
SCALE = 2 * HALF


def setup_scene():
    sc = bpy.context.scene
    cam_data = bpy.data.cameras.new("probe")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = SCALE
    cam_data.clip_start = 0.1
    cam_data.clip_end = 10000.0
    cam = bpy.data.objects.new("probe", cam_data)
    cam.location = (0.0, 0.0, 500.0)
    sc.collection.objects.link(cam)
    sc.camera = cam
    sun_data = bpy.data.lights.new("sun", type="SUN")
    sun_data.energy = 4.0
    sun = bpy.data.objects.new("sun", sun_data)
    sun.location = (0.0, 0.0, 900.0)
    sc.collection.objects.link(sun)
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.use_denoising = False
    sc.render.resolution_x = sc.render.resolution_y = RES
    sc.render.resolution_percentage = 100
    sc.render.film_transparent = False
    try:
        sc.view_settings.view_transform = "Raw"
    except Exception as ex:                                    # noqa: BLE001
        print("view transform not set to Raw: %s" % ex)
    return sc


def render_exr(sc, path):
    """One sample, sub-pixel filter: the value IS the shading point, not an average."""
    sc.cycles.samples = 1
    sc.cycles.pixel_filter_type = "BOX"
    sc.cycles.filter_width = 0.01
    sc.render.image_settings.file_format = "OPEN_EXR"
    sc.render.image_settings.color_depth = "32"
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)
    assert os.path.isfile(path), "no render written to %s" % path
    img = bpy.data.images.load(path)
    buf = np.empty(RES * RES * 4, dtype=np.float32)
    img.pixels.foreach_get(buf)
    bpy.data.images.remove(img)
    return buf.reshape(RES, RES, 4)


def render_png(sc, path, samples=32, res=512):
    """The picture to look at: bigger than the readout, so it can be judged."""
    sc.render.resolution_x = sc.render.resolution_y = res
    sc.cycles.samples = samples
    sc.cycles.pixel_filter_type = "GAUSSIAN"
    sc.cycles.filter_width = 1.5
    sc.render.image_settings.file_format = "PNG"
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)
    assert os.path.isfile(path), "no render written to %s" % path
    sc.render.resolution_x = sc.render.resolution_y = RES


def hijack(mat, kind):
    """Point the material's surface at an Emission node reporting a UV vector."""
    nt = mat.node_tree
    out = next(nd for nd in nt.nodes if nd.type == "OUTPUT_MATERIAL")
    link = out.inputs["Surface"].links[0] if out.inputs["Surface"].is_linked else None
    em = nt.nodes.new("ShaderNodeEmission")
    em.name = "probe_emission"
    em.location = (300, -800)
    tc = None
    if kind == "proj":
        src = nt.nodes["zproj_uv"].outputs["Vector"]
    else:
        tc = nt.nodes.new("ShaderNodeTexCoord")
        tc.name = "probe_uv"
        tc.location = (-300, -800)
        src = tc.outputs["UV"]
    nt.links.new(src, em.inputs["Color"])
    nt.links.new(em.outputs["Emission"], out.inputs["Surface"])
    return (nt, link.from_socket if link else None, em, tc)


def restore(save):
    nt, prev, em, tc = save
    if prev is not None:
        out = next(nd for nd in nt.nodes if nd.type == "OUTPUT_MATERIAL")
        nt.links.new(prev, out.inputs["Surface"])
    nt.nodes.remove(em)
    if tc is not None:
        nt.nodes.remove(tc)


def readout(sc, mats, kind, path):
    saves = [hijack(m, kind) for m in mats]
    arr = render_exr(sc, path)
    for s in saves:
        restore(s)
    return arr


# ---------------------------------------------------------------- geometry maths
def vertex_coords(obj):
    co = np.empty(len(obj.data.vertices) * 3, dtype=np.float64)
    obj.data.vertices.foreach_get("co", co)
    return co.reshape(-1, 3)


def hit_slot(obj, x, y, co):
    """Material slot of the topmost triangle over (x, y), from the current mesh."""
    best, best_z = None, -1e18
    for poly in obj.data.polygons:
        p = co[list(poly.vertices)]
        x0, y0 = p[0, 0], p[0, 1]
        d = [(p[1, 0] - x0, p[1, 1] - y0), (p[2, 0] - x0, p[2, 1] - y0)]
        w0 = (x - x0, y - y0)
        det = d[0][0] * d[1][1] - d[1][0] * d[0][1]
        if abs(det) < 1e-12:
            continue
        a = (w0[0] * d[1][1] - d[1][0] * w0[1]) / det
        b = (d[0][0] * w0[1] - w0[0] * d[0][1]) / det
        if a < -1e-9 or b < -1e-9 or a + b > 1 + 1e-9:
            continue
        z = p[0, 2] + a * (p[1, 2] - p[0, 2]) + b * (p[2, 2] - p[0, 2])
        if z > best_z:
            best, best_z = int(poly.material_index), z
    return best


def pixel_world(i, j):
    return ((i + 0.5) / RES * SCALE - SCALE / 2.0,
            (j + 0.5) / RES * SCALE - SCALE / 2.0)


def samples():
    out = []
    for j in range(0, RES):
        y = pixel_world(0, j)[1]
        if abs(y) > 500.0:
            continue
        for i in range(0, RES):
            x = pixel_world(i, 0)[0]
            if abs(x) > 500.0 or abs(x) < 150.0:
                continue
            if (i + j) % 3:
                continue
            out.append((i, j, x, y))
    return out


def expected_uv(obj, affines, x, y, co):
    slot = hit_slot(obj, x, y, co)
    if slot is None or slot >= len(affines) or affines[slot] is None:
        return None
    ax, bx, ay, by = affines[slot]
    return (ax * x + bx, ay * y + by)


# ---------------------------------------------------------------- the checks
FAILS = []


def check(name, ok, detail=""):
    print("%-4s %s%s" % ("PASS" if ok else "FAIL", name, (" -- " + detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def main():
    os.makedirs(OUT, exist_ok=True)
    print("Blender %s, out %s" % (bpy.app.version_string, OUT))
    t0 = time.time()
    mesh_path, terrain = build_tile(OUT)
    sc = setup_scene()

    t = time.time()
    res = bpy.ops.import_scene.ortho4xp_mesh(filepath=mesh_path, terrain_dir=terrain,
                                            import_textures=True, zproj=True,
                                            tex_max_px=0, normals_mode="ATTR")
    assert res == {"FINISHED"}, res
    obj = bpy.context.view_layer.objects.active
    me = obj.data
    stats = json.loads(obj["mesh_tex_stats"])
    print("import %.1fs: %d verts, %d faces, %d cells used, %d images, uv layer %s"
          % (time.time() - t, len(me.vertices), len(me.polygons), stats["cells_used"],
             stats["images_loaded"], stats["uv_layer"]))
    print("stats:", json.dumps(stats))
    check("import covered every face", stats["faces_uncovered"] == 0,
          "%d uncovered" % stats["faces_uncovered"])
    check("two cells", stats["cells_used"] == 2)

    # ---- 1. the node trees
    mats = [s.material for s in obj.material_slots if s.material]
    affines = [ADDON._material_affine(m, json.loads(obj["mesh_georef"])) for m in mats]
    tex_mats = [m for m in mats if m.node_tree and
                any(nd.type == "TEX_IMAGE" and nd.image for nd in m.node_tree.nodes)]
    check("materials carry the affine map", all(a is not None for a in affines[:2]),
          "%d/%d slots" % (sum(a is not None for a in affines), len(affines)))
    bad_link = bad_const = 0
    for m in tex_mats:
        nt = m.node_tree
        vm = nt.nodes.get("zproj_uv")
        geo = nt.nodes.get("zproj_position")
        if vm is None or geo is None:
            bad_link += 1
            continue
        tex = [nd for nd in nt.nodes if nd.type == "TEX_IMAGE" and nd.image]
        for nd in tex:
            # bpy returns a fresh wrapper per access, so compare with == not is
            if not (nd.inputs["Vector"].is_linked
                    and nd.inputs["Vector"].links[0].from_node == vm):
                bad_link += 1
        if not (geo.outputs["Object"].is_linked
                and geo.outputs["Object"].links[0].to_node == vm):
            bad_link += 1
        aff = ADDON._material_affine(m)
        scale = tuple(vm.inputs[1].default_value)[:2]
        offset = tuple(vm.inputs[2].default_value)[:2]
        if aff is None or np.abs(np.array(scale) - np.array([aff[0], aff[2]])).max() > 1e-6 \
                or np.abs(np.array(offset) - np.array([aff[1], aff[3]])).max() > 1e-3:
            bad_const += 1
    check("every image texture is fed by the projection", bad_link == 0,
          "%d bad link(s) over %d material(s)" % (bad_link, len(tex_mats)))
    check("node constants are the cell's affine", bad_const == 0,
          "%d mismatch(es)" % bad_const)

    # ---- 2. the baked UV layer == the affine, and == uv_center
    co = vertex_coords(obj)
    uv = np.empty(len(me.loops) * 2, dtype=np.float32)
    me.uv_layers[stats["uv_layer"]].data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2).astype(np.float64)
    loop_vert = np.empty(len(me.loops), dtype=np.int32)
    me.loops.foreach_get("vertex_index", loop_vert)
    loop_total = np.empty(len(me.polygons), dtype=np.int32)
    me.polygons.foreach_get("loop_total", loop_total)
    slot_of = np.empty(len(me.polygons), dtype=np.int32)
    me.polygons.foreach_get("material_index", slot_of)
    loop_slot = np.repeat(slot_of, loop_total)
    u_exp = np.zeros(len(me.loops))
    v_exp = np.zeros(len(me.loops))
    for slot, aff in enumerate(affines):
        sel = loop_slot == slot
        if aff is None or not sel.any():
            continue
        lv = loop_vert[sel]
        u_exp[sel] = aff[0] * co[lv, 0] + aff[1]
        v_exp[sel] = aff[2] * co[lv, 1] + aff[3]
    d_uv = np.abs(np.column_stack((uv[:, 0] - u_exp, uv[:, 1] - v_exp))).max()
    check("UV layer == position * scale + offset", d_uv < 1e-6, "max delta %.3e" % d_uv)

    g = json.loads(obj["mesh_georef"])
    lat_l = g["lat0"] + co[loop_vert, 1] / g["m_per_deg_lat"]
    lon_l = g["lon0"] + co[loop_vert, 0] / g["m_per_deg_lon"]
    cells, _skipped = TT.scan_terrain([terrain])
    cell_aff = [TT.center_uv_affine(c.lat, c.lon, c.size_m, g) for c in cells]
    # same cell? match on the affine, which is what both sides are built from
    slot_cell = [None] * len(affines)
    for slot, aff in enumerate(affines):
        if aff is None:
            continue
        best, best_d = None, 1e-9
        for k, ca in enumerate(cell_aff):
            if ca is None:
                continue
            d = abs(ca[0] - aff[0]) + abs(ca[1] - aff[1])
            if d < best_d:
                best, best_d = k, d
        slot_cell[slot] = best
    u_c, v_c = np.zeros(len(me.loops)), np.zeros(len(me.loops))
    for slot, k in enumerate(slot_cell):
        if k is None:
            continue
        cell = cells[k]
        sel = loop_slot == slot
        uu, vv = TT.uv_center(cell.lat, cell.lon, cell.size_m, lat_l[sel], lon_l[sel])
        u_c[sel], v_c[sel] = uu, vv
    check("every cell was matched by its affine", all(k is not None for k in slot_cell))
    d_c = np.abs(np.column_stack((uv[:, 0] - u_c, uv[:, 1] - v_c))).max()
    # 1e-6, not 0: the layer is stored as float32 (the two formulas are the same
    # float64 arithmetic, so this is storage precision, not a mapping difference)
    check("UV layer == terrain_tiles.uv_center", d_c < 1e-6, "max delta %.3e" % d_c)

    # ---- 3. rendering reads that map
    pts = samples()
    print("sampling %d pixels" % len(pts))
    arr_proj0 = readout(sc, mats, "proj", os.path.join(OUT, "readout_proj_0.exr"))
    arr_uv0 = readout(sc, mats, "uv", os.path.join(OUT, "readout_uv_0.exr"))

    def compare(arr, co_now, label, expect_stale=False):
        deltas = []
        for (i, j, x, y) in pts:
            exp = expected_uv(obj, affines, x, y, co_now)
            if exp is None:
                continue
            deltas.append(abs(arr[j, i, 0] - exp[0]) + abs(arr[j, i, 1] - exp[1]))
        deltas = np.array(deltas)
        print("  %s: n=%d  max %.2e  p95 %.2e  >0.02: %d"
              % (label, deltas.size, deltas.max(), np.percentile(deltas, 95),
                 int((deltas > 0.02).sum())))
        return deltas

    p0 = compare(arr_proj0, co, "projection readout at import")
    u0 = compare(arr_uv0, co, "UV-layer readout at import")
    check("render reads the projection (at import)", p0.max() < 1e-3,
          "max %.2e" % p0.max())
    check("UV layer agrees at import (render)", u0.max() < 1e-3, "max %.2e" % u0.max())

    # ---- 4. drag vertices: the projection must not move
    sel = [(abs(v.co[0] - 0.0) >= 100.0 - 1e-6 and abs(v.co[0]) <= 300.0 + 1e-6 and
            v.co[1] >= 100.0 - 1e-6 and v.co[1] <= 300.0 + 1e-6)
           for v in me.vertices]
    moved = 0
    for v, take in zip(me.vertices, sel):
        if not take:
            continue
        v.co[0] += DRAG[0]
        v.co[1] += DRAG[1]
        v.co[2] += DRAG[2]
        moved += 1
    me.update()
    print("dragged %d vertices by %s m" % (moved, DRAG))
    check("dragged a patch", moved >= 4, "%d vertices" % moved)
    co2 = vertex_coords(obj)
    arr_proj1 = readout(sc, mats, "proj", os.path.join(OUT, "readout_proj_1.exr"))
    arr_uv1 = readout(sc, mats, "uv", os.path.join(OUT, "readout_uv_1.exr"))
    p1 = compare(arr_proj1, co2, "projection readout after the drag")
    u1 = compare(arr_uv1, co2, "UV-layer readout after the drag")
    check("projection readout unchanged by the drag", p1.max() < 1e-3,
          "max %.2e" % p1.max())
    same = np.abs(arr_proj1 - arr_proj0).max()
    check("projection pixels identical before/after the drag", same < 1e-3,
          "max pixel delta %.2e" % same)
    check("baked UV layer is stale after the drag (that is the stretch)",
          int((u1 > 0.02).sum()) >= 5,
          "%d of %d sampled pixels moved by >0.02 uv" % (int((u1 > 0.02).sum()),
                                                         u1.size))

    # ---- 5. the operator re-anchors the layer
    assert bpy.ops.mesh.ortho4xp_reproject_uvs() == {"FINISHED"}
    uv2 = np.empty(len(me.loops) * 2, dtype=np.float32)
    me.uv_layers[stats["uv_layer"]].data.foreach_get("uv", uv2)
    uv2 = uv2.reshape(-1, 2).astype(np.float64)
    u_exp2 = np.zeros(len(me.loops))
    v_exp2 = np.zeros(len(me.loops))
    for slot, aff in enumerate(affines):
        sel2 = loop_slot == slot
        if aff is None or not sel2.any():
            continue
        lv = loop_vert[sel2]
        u_exp2[sel2] = aff[0] * co2[lv, 0] + aff[1]
        v_exp2[sel2] = aff[2] * co2[lv, 1] + aff[3]
    d2 = np.abs(np.column_stack((uv2[:, 0] - u_exp2, uv2[:, 1] - v_exp2))).max()
    check("Projected UVs re-anchors the layer", d2 < 1e-6, "max delta %.3e" % d2)
    arr_uv2 = readout(sc, mats, "uv", os.path.join(OUT, "readout_uv_2.exr"))
    u2 = compare(arr_uv2, co2, "UV-layer readout after Projected UVs")
    check("re-anchored layer renders the fixed map", u2.max() < 1e-3,
          "max %.2e" % u2.max())

    # ---- 6. the toggle, and the two pictures
    assert bpy.ops.mesh.ortho4xp_zproject(enable=False) == {"FINISHED"}
    off = [m for m in mats if not (m.node_tree and m.node_tree.nodes.get("zproj_uv"))]
    check("Use UVs removes the projection", len(off) == len(mats),
          "%d of %d material(s) still wired" % (len(mats) - len(off), len(mats)))
    render_png(sc, os.path.join(OUT, "demo_uv_layer.png"))
    assert bpy.ops.mesh.ortho4xp_zproject(enable=True) == {"FINISHED"}
    on = [m for m in mats if m.node_tree and m.node_tree.nodes.get("zproj_uv")]
    check("Fix to map re-wires the projection", len(on) == len(mats))
    render_png(sc, os.path.join(OUT, "demo_z_projection.png"))
    print("pictures: %s" % ", ".join(sorted(
        os.path.join(OUT, f) for f in os.listdir(OUT) if f.startswith("demo_"))))

    print("\n%s (%d check(s) failed) in %.1fs"
          % ("RESULT: PASS" if not FAILS else "RESULT: FAIL " + ", ".join(FAILS),
             len(FAILS), time.time() - t0))
    return 1 if FAILS else 0


sys.exit(main())