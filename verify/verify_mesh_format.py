"""Verify a .mesh file against Ortho4XP's own reader/writer code.

Two independent checks, both driven by the Ortho4XP source tree:

 1. call the repository's ``O4_Mesh_Utils.read_mesh_file()`` on the file and print
    what it decodes (the heavy imports -- rasterio/shapely/rtree/requests -- are
    stubbed out so the module can be loaded outside a full Ortho4XP install);
 2. walk the file byte by byte and check every field against the exact format
    strings ``O4_Mesh_Utils.write_mesh_file()`` emits, including re-formatting
    each parsed number back with ``'{:.15f}'`` / ``'{:.2f}'`` and comparing the
    text, so a mismatch in either the layout or the float formatting is caught;
 3. re-write the parsed data with this addon's ``meshcodec`` and compare the
    sha256 of the result with the original file.

Usage:
    python3 verify_mesh_format.py [mesh_file] [--repo /path/to/Ortho4XP]
"""
import argparse
import hashlib
import importlib.util
import os
import sys
import types

import numpy as np


def load_repo_module(repo):
    """Load src/O4_Mesh_Utils.py standalone, with its heavy imports stubbed."""
    src = os.path.join(repo, "src")

    class _Any:
        def __getattr__(self, name):
            return _Any()

        def __call__(self, *a, **k):
            return _Any()

    for name in ("O4_DEM_Utils", "O4_UI_Utils", "O4_Geo_Utils", "O4_Vector_Utils",
                 "O4_OSM_Utils", "O4_Version", "requests"):
        mod = types.ModuleType(name)
        mod.__getattr__ = lambda n: _Any()
        sys.modules[name] = mod
    fn = types.ModuleType("O4_File_Names")
    fn.Utils_dir = os.path.join(repo, "Utils")
    fn.Ortho4XP_dir = repo
    sys.modules["O4_File_Names"] = fn
    sys.path.insert(0, src)

    spec = importlib.util.spec_from_file_location(
        "O4_Mesh_Utils", os.path.join(src, "O4_Mesh_Utils.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mesh", nargs="?",
                    default="/Users/rourou/GitHub/Ortho4XP/sample/Data+50-125.mesh")
    ap.add_argument("--repo", default="/Users/rourou/GitHub/Ortho4XP")
    ap.add_argument("--no-rewrite", action="store_true",
                    help="skip the sha256 re-write comparison (saves ~15 s and "
                         "300 MB of disk on a full tile)")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import meshcodec as MC

    MESH = load_repo_module(args.repo)
    print("repo module :", MESH.__file__)
    print("mesh file   :", args.mesh, "(%d bytes)" % os.path.getsize(args.mesh))

    # ---------------------------------------------------------- 1. repo reader
    vers, n_nodes, coords, n_tris, tri_idx, tri_types = MESH.read_mesh_file(args.mesh)
    print("\n=== O4_Mesh_Utils.read_mesh_file() ===")
    print("mesh_version :", vers)
    print("nbr_nodes    :", n_nodes)
    print("nbr_tris     :", n_tris)
    print("coords       :", coords.shape, "(lon, lat, z_m, nx, ny per node)")
    print("lon range    : %.9f .. %.9f" % (coords[0::5].min(), coords[0::5].max()))
    print("lat range    : %.9f .. %.9f" % (coords[1::5].min(), coords[1::5].max()))
    print("alt (m)      : %.3f .. %.3f" % (coords[2::5].min(), coords[2::5].max()))
    print("nx, ny       : %.2f..%.2f, %.2f..%.2f"
          % (coords[3::5].min(), coords[3::5].max(),
             coords[4::5].min(), coords[4::5].max()))
    print("tri idx      : min %d max %d (0-based), nodes-1 = %d"
          % (tri_idx.min(), tri_idx.max(), n_nodes - 1))
    print("tri_types    :", np.unique(tri_types), "(has_water mask %s for v%s)"
          % (7 if vers >= 1.3 else 3, vers))

    # ------------------------------------------------- 2. strict byte-level walk
    print("\n=== byte-level conformance to write_mesh_file() ===")
    f15, f2 = "{:.15f}".format, "{:.2f}".format
    errors = []
    n_ok = {"v": 0, "n": 0, "t": 0}
    tags, nz_vals, attrs = set(), set(), {}
    with open(args.mesh, "r", newline="") as f:
        line = f.readline()
        if line != "MeshVersionFormatted 2\r\n" and line != "MeshVersionFormatted 2\n":
            errors.append("line 1 is %r" % line)
        if f.readline() not in ("Dimension 3\r\n", "Dimension 3\n"):
            errors.append("line 2 is not 'Dimension 3'")
        if f.readline() not in ("\r\n", "\n"):
            errors.append("line 3 is not blank")
        if f.readline() not in ("Vertices\r\n", "Vertices\n"):
            errors.append("line 4 is not 'Vertices'")
        n_v = int(f.readline())
        if n_v != n_nodes:
            errors.append("vertex count %d != reader %d" % (n_v, n_nodes))
        lon = np.zeros(n_v); lat = np.zeros(n_v); z = np.zeros(n_v)
        for i in range(n_v):
            p = f.readline().rstrip("\r\n").split(" ")
            if len(p) != 4:
                errors.append("vertex line %d has %d fields" % (i + 6, len(p)))
                break
            tags.add(p[3])
            lon[i], lat[i], z[i] = float(p[0]), float(p[1]), float(p[2])
            if (p[0] != f15(lon[i]) or p[1] != f15(lat[i]) or p[2] != f15(z[i])):
                errors.append("vertex line %d: float formatting mismatch" % (i + 6))
                break
            n_ok["v"] += 1
        if f.readline() not in ("\r\n", "\n"):
            errors.append("no blank line after the vertices")
        if f.readline() not in ("Normals\r\n", "Normals\n"):
            errors.append("no 'Normals' section")
        n_n = int(f.readline())
        if n_n != n_v:
            errors.append("normals count %d != vertex count %d" % (n_n, n_v))
        nx = np.zeros(n_n); ny = np.zeros(n_n)
        for i in range(n_n):
            p = f.readline().rstrip("\r\n").split(" ")
            if len(p) != 3:
                errors.append("normal line %d has %d fields" % (i, len(p)))
                break
            nz_vals.add(p[2])
            nx[i], ny[i] = float(p[0]), float(p[1])
            if p[0] != f2(nx[i]) or p[1] != f2(ny[i]):
                errors.append("normal line %d: float formatting mismatch" % i)
                break
            n_ok["n"] += 1
        if f.readline() not in ("\r\n", "\n"):
            errors.append("no blank line after the normals")
        if f.readline() not in ("Triangles\r\n", "Triangles\n"):
            errors.append("no 'Triangles' section")
        n_t = int(f.readline())
        if n_t != n_tris:
            errors.append("triangle count %d != reader %d" % (n_t, n_tris))
        tris = np.zeros((n_t, 3), dtype=np.int64)
        tri_attr = np.zeros(n_t, dtype=np.int64)
        for i in range(n_t):
            p = f.readline().rstrip("\r\n").split(" ")
            if len(p) != 4:
                errors.append("triangle line %d has %d fields" % (i, len(p)))
                break
            v = [int(x) for x in p]
            tris[i] = v[:3]
            tri_attr[i] = v[3]
            attrs[v[3]] = attrs.get(v[3], 0) + 1
            n_ok["t"] += 1
        trailing = f.readline()
        if trailing != "":
            errors.append("trailing content after the triangles: %r" % trailing)

    print("vertices / normals / triangles checked: %d / %d / %d"
          % (n_ok["v"], n_ok["n"], n_ok["t"]))
    print("vertex 4th field values :", tags)
    print("normal 3rd field values :", nz_vals)
    print("triangle attributes     :", dict(sorted(attrs.items())))
    print("errors                  :", len(errors))
    for e in errors[:20]:
        print("  !", e)

    if not errors:
        print("cross-check reader == strict parse: lon %s lat %s z %s"
              % (np.array_equal(coords[0::5], lon),
                 np.array_equal(coords[1::5], lat),
                 np.array_equal(coords[2::5], z * MC.ALT_SCALE)))

    # ----------------------------------------------------- 3. codec round-trip
    if not args.no_rewrite:
        m = MC.read_mesh(args.mesh)
        out = args.mesh + ".rewrite"
        nbytes = MC.write_mesh(out, m)
        same = sha256(args.mesh) == sha256(out)
        print("\n=== meshcodec re-write ===")
        print("source  %d bytes %s" % (os.path.getsize(args.mesh), sha256(args.mesh)))
        print("rewrite %d bytes %s" % (nbytes, sha256(out)))
        print("BYTE IDENTICAL:", same)
        os.remove(out)

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())