"""Check `_mark_seams_from_islands` against Blender's own UV > Seams from Islands.

    blender --background --factory-startup --python verify/seams_test.py -- [tile.mesh]

Part 1 builds small meshes that pin the operator's rule down (two faces agreeing,
two faces disagreeing by 1e-7, three and four faces on one edge, unwelded twins,
a lone border edge) and asserts the addon's function marks exactly the same edge
set as the operator -- and that the operator is the one being trusted, by running
it too.

Part 2, when a tile path is given, imports the tile through the addon and compares
its seams with the operator's on a whole 4.5M triangle tile, then reports what each
route cost.
"""

import os
import sys
import time

import numpy as np

ADDON_DIR = os.path.expanduser(
    "~/Library/Application Support/Blender/5.2/scripts/addons/io_scene_ortho4xp_mesh")
sys.path.insert(0, ADDON_DIR)

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
TILE = argv[0] if argv else None

import bpy                                                     # noqa: E402

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="io_scene_ortho4xp_mesh")
import io_scene_ortho4xp_mesh as ADDON                         # noqa: E402

FAILS = []


def check(tag, got, want):
    ok = got == want
    print("  %-46s %s" % (tag, "ok" if ok else "FAIL got %s want %s" % (got, want)))
    if not ok:
        FAILS.append(tag)


def build(name, verts, faces, uvs):
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.update()
    layer = me.uv_layers.new(name=ADDON.UV_LAYER)
    li = 0
    for f in faces:
        for k in range(len(f)):
            layer.data[li].uv = uvs[f[k]]
            li += 1
    obj = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(obj)
    for o in bpy.context.view_layer.objects:
        o.select_set(o is obj)
    bpy.context.view_layer.objects.active = obj
    return obj


def edge_pairs(obj, flags):
    ev = np.empty(len(obj.data.edges) * 2, dtype=np.int32)
    obj.data.edges.foreach_get("vertices", ev)
    ev = ev.reshape(-1, 2)
    idx = np.flatnonzero(flags)
    return {(int(min(a, b)), int(max(a, b))) for a, b in ev[idx]}


def operator_seams(obj):
    me = obj.data
    me.edges.foreach_set("use_seam", np.zeros(len(me.edges), dtype=bool))
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.uv.seams_from_islands()
    bpy.ops.object.mode_set(mode="OBJECT")
    flags = np.zeros(len(me.edges), dtype=bool)
    me.edges.foreach_get("use_seam", flags)
    return edge_pairs(obj, flags)


def addon_seams(obj):
    me = obj.data
    me.edges.foreach_set("use_seam", np.zeros(len(me.edges), dtype=bool))
    n = ADDON._mark_seams_from_islands(me)
    flags = np.zeros(len(me.edges), dtype=bool)
    me.edges.foreach_get("use_seam", flags)
    assert n == int(flags.sum()), "reported %d seams, set %d" % (n, flags.sum())
    return edge_pairs(obj, flags)


print("== part 1: the synthetic cases ==")

# 1. two faces sharing 1-2, both giving that edge the same UVs
V = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0)]
F = [(0, 1, 2), (1, 3, 2)]
U = {0: (0.0, 0.0), 1: (1.0, 0.0), 2: (0.0, 1.0), 3: (1.0, 1.0)}
obj = build("contiguous", V, F, U)
check("two faces agreeing", addon_seams(obj), operator_seams(obj))

# 2. the same two, with the shared edge's UV off by 1e-7 (operator is exact)
for tag, delta in (("off by 1e-7", 1e-7), ("off by 0.25", 0.25), ("off by -0.5", -0.5)):
    obj = build("split", V, F, U)
    obj.data.uv_layers.active.data[1].uv[0] += delta
    want = operator_seams(obj)
    check("two faces, shared edge %s" % tag, addon_seams(obj), want)
    if delta != 1e-7 and want != {(1, 2)}:
        FAILS.append("case %s should split edge (1, 2)" % tag)

# 3. three / four faces on one edge, all agreeing and then one breaking rank
V4 = [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
F3 = [(0, 1, 2), (0, 1, 3), (0, 1, 4)]
F4 = F3 + [(0, 1, 5)]
U6 = {0: (0.0, 0.0), 1: (1.0, 0.0), 2: (0.0, 1.0), 3: (0.0, -1.0),
      4: (1.0, 1.0), 5: (1.0, -1.0)}
obj = build("nonmanifold3", V4, F3, U6)
check("three faces on an edge, all agreeing", addon_seams(obj), operator_seams(obj))
obj = build("nonmanifold4", V4, F4, U6)
check("four faces on an edge, all agreeing", addon_seams(obj), operator_seams(obj))
obj = build("nonmanifold4b", V4, F4, U6)
obj.data.uv_layers.active.data[9].uv = (9.0, 9.0)
check("four faces, the last one disagreeing", addon_seams(obj), operator_seams(obj))

# 4. two faces whose vertices are not welded: one face per edge, nothing to split
obj = build("unwelded", V + [(0, 0, 0), (1, 0, 0), (0, 1, 0)],
            [(0, 1, 2), (4, 5, 6)],
            {0: (0, 0), 1: (1, 0), 2: (0, 1), 4: (0, 0), 5: (1, 0), 6: (0, 1)})
check("unwelded twins, no shared edge", addon_seams(obj), operator_seams(obj))

# 5. no UV layer at all: the function has to say 0, not raise
me = bpy.data.meshes.new("nouv")
me.from_pydata(V, [], F)
me.update()
check("mesh without a UV layer", ADDON._mark_seams_from_islands(me), 0)

# 6. the sidebar button with nothing to act on
for obj in bpy.context.view_layer.objects:
    obj.select_set(False)
bpy.context.view_layer.objects.active = None
check("button with no tile selected",
      bpy.ops.mesh.ortho4xp_seams_from_islands(), {"CANCELLED"})

if TILE:
    print("== part 2: %s ==" % TILE)
    t = time.time()
    bpy.ops.import_scene.ortho4xp_mesh(filepath=TILE, tex_limit=1, tex_max_px=64)
    t_import = time.time() - t
    obj = bpy.context.object
    mine = edge_pairs(obj, np.array(
        [e.use_seam for e in obj.data.edges], dtype=bool))
    print("  import %.1fs, addon seams %d" % (t_import, len(mine)))

    t = time.time()
    theirs = operator_seams(obj)
    t_op = time.time() - t
    print("  operator %.1fs (edit mode round trip included), seams %d"
          % (t_op, len(theirs)))
    check("whole tile: same seam set", mine, theirs)

    obj.data.edges.foreach_set("use_seam", np.zeros(len(obj.data.edges), dtype=bool))
    t = time.time()
    n = ADDON._mark_seams_from_islands(obj.data)
    print("  addon function alone: %.1fs, seams %d" % (time.time() - t, n))
    check("whole tile: same count on a second run", n, len(theirs))

    print("  -- and the sidebar button")
    obj.data.edges.foreach_set("use_seam", np.zeros(len(obj.data.edges), dtype=bool))
    t = time.time()
    res = bpy.ops.mesh.ortho4xp_seams_from_islands()
    flags = np.zeros(len(obj.data.edges), dtype=bool)
    obj.data.edges.foreach_get("use_seam", flags)
    print("  button: %.1fs, seams %d, result %s"
          % (time.time() - t, int(flags.sum()), res))
    check("button: same seam set", edge_pairs(obj, flags), theirs)
    check("button: FINISHED", res, {"FINISHED"})
    check("button: count stored on the object",
          obj.get("mesh_seams"), len(theirs))
else:
    print("(no .mesh given: part 2 skipped)")

print("\n%s" % ("FAILED: " + ", ".join(FAILS) if FAILS else "all checks passed"))