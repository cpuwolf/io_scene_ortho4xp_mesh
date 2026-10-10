"""Headless Blender round-trip test for the Ortho4XP Mesh addon.

    blender --background --factory-startup --python blender_roundtrip_test.py -- \
            <input.mesh> <output.mesh> [ATTR|SPLIT|NONE]

It enables the addon, imports the file, exports it again and then re-reads both
files with meshcodec to report how much the round trip changed: triangle indices
and attributes must be identical, vertex normals must be identical, and vertex
positions differ only by Blender's float32 vertex storage (sub-millimetre).
"""
import hashlib
import json
import os
import sys
import time

import numpy as np

ADDON_ROOT = os.path.expanduser(
    "~/Library/Application Support/Blender/5.2/scripts/addons")
sys.path.insert(0, os.path.join(ADDON_ROOT, "io_scene_ortho4xp_mesh"))

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if len(argv) < 2:
    print(__doc__)
    sys.exit(2)
SRC, DST = argv[0], argv[1]
MODE = argv[2] if len(argv) > 2 else "ATTR"

import bpy                                                    # noqa: E402
import meshcodec as MC                                        # noqa: E402

print("Blender", bpy.app.version_string)
t0 = time.time()
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.preferences.addon_enable(module="io_scene_ortho4xp_mesh")

t = time.time()
assert bpy.ops.import_scene.ortho4xp_mesh(filepath=SRC,
                                          normals_mode=MODE) == {"FINISHED"}
print("import %.1fs" % (time.time() - t))
obj = bpy.context.view_layer.objects.active
me = obj.data
print("object %s: %d verts, %d polys, attributes %s"
      % (obj.name, len(me.vertices), len(me.polygons),
         [a.name for a in me.attributes]))
print("material slots:", [m.name for m in me.materials])
print("meta:", json.dumps(json.loads(obj["mesh_meta"]))[:400])
print("non-triangle polygons:", sum(1 for p in me.polygons if p.loop_total != 3))

t = time.time()
assert bpy.ops.export_scene.ortho4xp_mesh(filepath=DST) == {"FINISHED"}
print("export %.1fs" % (time.time() - t))


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


print("\nsource   %d bytes %s" % (os.path.getsize(SRC), sha(SRC)))
print("exported %d bytes %s" % (os.path.getsize(DST), sha(DST)))
print("BYTE IDENTICAL:", sha(SRC) == sha(DST))

a, b = MC.read_mesh(SRC), MC.read_mesh(DST)
print("counts: %d/%d verts, %d/%d tris" % (a.n_vert, b.n_vert, a.n_tri, b.n_tri))
print("triangle indices identical:", np.array_equal(a.tris, b.tris))
print("triangle attributes identical:", np.array_equal(a.attr, b.attr))
print("version %g -> %g, CRLF %s -> %s"
      % (a.version, b.version, a.crlf, b.crlf))
if a.lon.shape == b.lon.shape:
    mplon = 111319.49079327357 * np.cos(np.radians((a.lat.min() + a.lat.max()) / 2))
    mplat = 111319.49079327357
    print("max |dlon| %.3e deg (%.4f mm)  max |dlat| %.3e deg (%.4f mm)"
          % (np.abs(a.lon - b.lon).max(),
             np.abs(a.lon - b.lon).max() * mplon * 1000,
             np.abs(a.lat - b.lat).max(),
             np.abs(a.lat - b.lat).max() * mplat * 1000))
    print("max |dz|   %.3e m (%.4f mm)"
          % (np.abs(a.alt_m - b.alt_m).max(),
             np.abs(a.alt_m - b.alt_m).max() * 1000))
if a.nx is not None and b.nx is not None:
    print("normals identical:", np.array_equal(a.nx, b.nx)
          and np.array_equal(a.ny, b.ny),
          " (max |dnx| %.3f, max |dny| %.3f)"
          % (np.abs(a.nx - b.nx).max(), np.abs(a.ny - b.ny).max()))
print("total %.1fs" % (time.time() - t0))