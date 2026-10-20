"""Check `terrain_tiles.TerrainIndex.lookup` -- chunking, gaps and cell priority.

    python3 verify/terrain_lookup_test.py

`lookup` runs in chunks of 65536 points; a chunk is the unit that goes wrong when
the hit/miss scatter is written by hand, and a tile where every face is covered
never shows it.  So this test builds a synthetic lattice *with holes* and a
fine-patch cell on top, and compares the chunked answer with a brute-force one:

1. cell centres resolve to their own cell (finest first, sea overlay first);
2. points in the gaps between cells -- and outside the lattice -- come back -1;
3. over 150k random points (>2 chunks, so full chunks with misses exist) the
   result equals the brute-force answer element for element;
4. the answer does not depend on the chunk size (65536 / 1000 / 7).
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import terrain_tiles as TT                                        # noqa: E402


def make_cells():
    """Coarse grid with gaps, one finer cell inside it, one sea overlay twin."""
    cells = []
    base_lat, base_lon = 45.0, 7.0
    pitch = 0.03                                   # 0.03 deg > 2000 m: real gaps
    for row in range(4):
        for col in range(5):
            c = TT.TerCell("%d_%d.ter" % (row, col), None)
            c.set_footprint(base_lat + row * pitch, base_lon + col * pitch, 2000.0)
            cells.append(c)
    # a finer cell covering the middle of cell (1,2)
    fine = TT.TerCell("fine.ter", None)
    fine.set_footprint(base_lat + pitch, base_lon + 2 * pitch, 800.0)
    cells.append(fine)
    # a sea overlay sharing the footprint of cell (2,3): same size, overlay wins
    sea = TT.TerCell("sea_overlay.ter", None)
    sea.set_footprint(base_lat + 2 * pitch, base_lon + 3 * pitch, 2000.0)
    sea.overlay = True
    cells.append(sea)
    return cells


def brute(cells, lat, lon):
    """The documented rule: first containing cell, finest first, overlay first."""
    order = sorted(range(len(cells)),
                   key=lambda i: (cells[i].size_m, 0 if cells[i].overlay else 1, i))
    out = np.full(lat.size, -1, dtype=np.int32)
    for n in range(lat.size):
        for i in order:
            c = cells[i]
            if c.lat0 <= lat[n] <= c.lat1 and c.lon0 <= lon[n] <= c.lon1:
                out[n] = i
                break
    return out


def main():
    cells = make_cells()
    index = TT.TerrainIndex(cells)
    ok = True

    # -- 1. every cell finds itself at its own centre, overlays and fine cells win
    clat = np.array([c.lat for c in cells])
    clon = np.array([c.lon for c in cells])
    got = index.lookup(clat, clon)
    want = brute(cells, clat, clon)
    print("centres -> %s" % ("ok" if (got == want).all() else "MISMATCH %s vs %s"
                             % (got.tolist(), want.tolist())))
    ok &= bool((got == want).all())
    fine_i = len(cells) - 2
    sea_i = len(cells) - 1
    print("fine cell owns its own centre:", int(got[fine_i]) == fine_i,
          "| sea overlay beats its plain twin:",
          bool(cells[got[sea_i]].overlay))
    ok &= int(got[fine_i]) == fine_i and bool(cells[got[sea_i]].overlay)

    # -- 2. gaps and everything outside the lattice stay -1
    gap_lat, gap_lon = np.array([45.015, 45.015, 60.0]), np.array([7.015, 7.045, 7.0])
    g = index.lookup(gap_lat, gap_lon)
    print("gap / outside -> %s (want [-1, -1, -1])" % g.tolist())
    ok &= bool((g == -1).all())

    # -- 3. 150k random points across and past the lattice against brute force
    rng = np.random.default_rng(7)
    n = 150000
    lat = rng.uniform(44.90, 45.20, n)
    lon = rng.uniform(6.90, 7.20, n)
    got = index.lookup(lat, lon)
    ref = brute(cells, lat, lon)
    hits = int((got >= 0).sum())
    print("150k points: %d inside a cell, %d in the holes, matches brute force: %s"
          % (hits, n - hits, bool((got == ref).all())))
    ok &= bool((got == ref).all()) and 0 < hits < n

    # -- 4. chunk size must not change the answer
    same = all(bool((index.lookup(lat, lon, chunk=k) == got).all())
               for k in (1000, 7))
    print("chunk-size invariance (65536 / 1000 / 7):", same)
    ok &= same

    print("RESULT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
