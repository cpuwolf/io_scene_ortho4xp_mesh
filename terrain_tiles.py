"""Locate an Ortho4XP tile's terrain cells and map .mesh faces onto them.

The DSF importer (`xplane-dsf-blender`) gets its terrain textures for free: a DSF
names a terrain *definition* per patch, so it resolves one `.ter` per definition.
A `.mesh` file carries no such reference -- only lon/lat/alt, normals, triangles
and the Triangle4XP region attribute -- so the link has to be rebuilt from the
`.ter` files themselves.

That works because every ortho cell `.ter` that Ortho4XP writes declares its own
footprint::

    LOAD_CENTER <lat> <lon> <size_m> <tex_px>     the cell this texture covers
    BASE_TEX_NOWRAP ../textures/<cell>.dds        the orthophoto, mapped once
    LOAD_CENTER_BORDER <lat> <lon> <size_m> 512   sea overlay border (optional)
    BORDER_TEX ../textures/<cell>.png

So the cells are a spatial index: each face belongs to the cell whose LOAD_CENTER
footprint contains its centroid, and the texture is mapped once over that
footprint -- which is exactly what `tercodec.uv_from_center` implements and what
X-Plane does when it renders the same tile.

This module is pure (numpy + stdlib, no bpy) so it can be tested headless:

    cells, skipped = scan_terrain_dirs(["/tile/terrain"])
    face_cell = assign_faces(cells, centroid_lat, centroid_lon)
    u, v = uv_center(cell_lat[face_cell], cell_lon[face_cell],
                     cell_size[face_cell], lat, lon)
"""

from __future__ import annotations

import math
import os

import numpy as np

import tercodec as TC

#: the spherical metres-per-degree constants of tercodec.uv_from_center.  The
#: footprint and the UV have to use the SAME projection or a face on a cell edge
#: would be assigned to a cell whose texture it then samples outside of.
M_PER_DEG_LAT = 111132.92
M_PER_DEG_LON = 111412.84

__all__ = ["TerCell", "scan_terrain", "find_terrain_dir", "TerrainIndex",
           "M_PER_DEG_LAT", "M_PER_DEG_LON"]


class TerCell:
    """One terrain cell: a .ter plus the footprint it covers."""

    __slots__ = ("path", "ter", "image", "kind", "lat", "lon", "size_m", "size_px",
                 "lat0", "lat1", "lon0", "lon1", "projected", "border", "nowrap",
                 "overlay", "textures", "faces")

    def __init__(self, path, ter, kind="ter", image=None):
        self.path = path
        self.ter = ter
        self.kind = kind
        self.image = image
        self.lat = self.lon = None
        self.size_m = self.size_px = None
        self.lat0 = self.lat1 = self.lon0 = self.lon1 = None
        self.projected = None
        self.border = None
        self.nowrap = False
        self.overlay = False
        self.textures = {}
        self.faces = 0

    # -- geometry ---------------------------------------------------------
    def set_footprint(self, lat, lon, size_m, size_px=None):
        """Footprint of a LOAD_CENTER cell, in degrees, square in metres."""
        if not size_m:
            return False
        self.lat, self.lon = float(lat), float(lon)
        self.size_m = float(size_m)
        self.size_px = int(size_px) if size_px else None
        dlat = self.size_m / M_PER_DEG_LAT
        dlon = self.size_m / (M_PER_DEG_LON * math.cos(math.radians(self.lat)))
        self.lat0, self.lat1 = self.lat - dlat * 0.5, self.lat + dlat * 0.5
        self.lon0, self.lon1 = self.lon - dlon * 0.5, self.lon + dlon * 0.5
        return True

    @property
    def key(self):
        """Identity of the footprint (for de-duplicating cell variants)."""
        lat, lon, size = self.lat, self.lon, self.size_m
        if lat is None or lon is None or size is None:
            return None
        return (round(float(lat), 6), round(float(lon), 6), round(float(size), 3))

    @property
    def label(self):
        return os.path.basename(self.path)[:-4] + (" [sea overlay]" if self.overlay
                                                   else "")

    def __repr__(self):
        return ("<TerCell %s %s %.0fm %s>"
                % (os.path.basename(self.path),
                   "%.5f,%.5f" % (self.lat, self.lon) if self.lat is not None else "?",
                   self.size_m or 0,
                   sorted(self.textures)))


def _cell_from_ter(path, ter):
    """Turn a parsed .ter into a TerCell (footprint from LOAD_CENTER)."""
    cell = TerCell(path, ter)
    cell.textures = dict(ter.textures)
    cell.overlay = "sea_overlay" in os.path.basename(path).lower()

    lc = ter.params("LOAD_CENTER", 4)          # lat lon size_m tex_px
    if lc and len(lc) >= 3:
        cell.set_footprint(lc[0], lc[1], lc[2], lc[3] if len(lc) > 3 else None)
    lb = ter.params("LOAD_CENTER_BORDER", 4)
    if lb:
        cell.border = lb
    pv = ter.params("PROJECTED", 2)
    if pv:
        cell.projected = tuple(pv)
    cell.nowrap = bool(ter.has("BASE_TEX_NOWRAP") or ter.has("LIT_TEX_NOWRAP")
                       or cell.lat is not None)
    return cell


def _axis_edges(vals, pitch):
    """Voronoi edges for sorted unique centres: the midline to each neighbour.

    Gaps larger than 1.5 pitch are not bridged (a cell must not stretch across a
    region that belongs to another zoom level's patch), and the outer edges
    extrapolate by half a pitch.
    """
    n = len(vals)
    if n == 1:
        return [(vals[0] - pitch * 0.5, vals[0] + pitch * 0.5)]
    out = []
    for i, v in enumerate(vals):
        lo = v - pitch * 0.5 if i == 0 else (vals[i - 1] + v) * 0.5
        hi = v + pitch * 0.5 if i == n - 1 else (v + vals[i + 1]) * 0.5
        if v - lo > pitch * 0.75:            # gap: do not bridge it
            lo = v - pitch * 0.5
        if hi - v > pitch * 0.75:
            hi = v + pitch * 0.5
        out.append((lo, hi))
    return out


def _cluster_sizes(sizes, tol=0.02):
    """Group cell sizes into zoom-level classes (sizes within 2% are one class).

    A class is a zoom level: `LOAD_CENTER`'s metre size varies with latitude, so
    the ZL17 cells of one tile span e.g. 4318..4358 m and all belong together.
    """
    vals = sorted({round(float(s), 1) for s in sizes})
    groups, cur = [], [vals[0]]
    for v in vals[1:]:
        if v <= cur[-1] * (1.0 + tol):
            cur.append(v)
        else:
            groups.append(cur)
            cur = [v]
    groups.append(cur)
    return groups


def apply_lattice_footprints(cells):
    """Give every cell the effective footprint of the lattice it sits in.

    The `.ter`'s own `LOAD_CENTER <size_m>` converted to degrees does NOT exactly
    reproduce Ortho4XP's grid: measured on a real +27+089 tile the declared size is
    ~0.1% narrow in longitude and ~0.6% tall in latitude versus the actual centre
    spacing, so metre-derived footprints leave slivers up to ~1 km between
    neighbours once a patch of a finer zoom level is involved.  (X-Plane's own
    renderer does not care -- it samples each texture over its own declared size --
    but for *assigning* faces to cells the tiling has to be built from the observed
    lattice.)

    So each cell keeps the area up to the midline between it and its neighbours of
    the same zoom level, which tiles exactly.  Cells are then layered coarse to
    fine by :class:`TerrainIndex` (a ZL19 patch overwrites the ZL17 grid under it).
    """
    usable = [c for c in cells if c.lat is not None and c.size_m]
    if not usable:
        return cells
    clusters = _cluster_sizes([c.size_m for c in usable])

    def class_of(size):
        for i, grp in enumerate(clusters):
            if grp[0] * 0.98 <= size <= grp[-1] * 1.02:
                return i
        return -1

    by_class = {}
    for c in usable:
        by_class.setdefault(class_of(c.size_m), []).append(c)

    for cls, group in by_class.items():
        lats = sorted({c.lat for c in group})
        lons = sorted({c.lon for c in group})
        size = float(np.median([c.size_m for c in group]))
        lat_pitch = float(np.median(np.diff(lats))) if len(lats) > 1 else (
            size / M_PER_DEG_LAT)
        lon_pitch = float(np.median(np.diff(lons))) if len(lons) > 1 else (
            size / (M_PER_DEG_LON * math.cos(math.radians(lats[0]))))
        lat_edges = dict(zip(lats, _axis_edges(lats, lat_pitch)))
        lon_edges = dict(zip(lons, _axis_edges(lons, lon_pitch)))
        for c in group:
            c.lat0, c.lat1 = lat_edges[c.lat]
            c.lon0, c.lon1 = lon_edges[c.lon]
    return cells


def scan_terrain(dirs):
    """Read every .ter in the given folders.

    Returns ``(cells, skipped)`` where `cells` holds the ortho cells (one per
    footprint, the `_sea_overlay` variant winning over the plain one because that
    is the definition Ortho4XP's DSF uses for those cells) and `skipped` is a list
    of ``(path, reason)`` for .ter files this importer cannot place.
    """
    cells = {}
    skipped = []
    seen = set()
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.lower().endswith(".ter"):
                continue
            path = os.path.normpath(os.path.join(d, name))
            if path in seen:
                continue
            seen.add(path)
            try:
                ter = TC.parse_ter(path)
            except Exception as ex:                            # noqa: BLE001
                skipped.append((path, "unreadable: %s" % ex))
                continue
            cell = _cell_from_ter(path, ter)
            if cell.lat is None:
                why = ("PROJECTED, no LOAD_CENTER footprint"
                       if cell.projected else "no LOAD_CENTER footprint")
                skipped.append((path, why))
                continue
            k = cell.key
            old = cells.get(k)
            if old is None:
                cells[k] = cell
            elif cell.overlay and not old.overlay:
                cells[k] = cell                          # the sea overlay wins
            elif cell.overlay == old.overlay and cell.textures and not old.textures:
                cells[k] = cell
    ordered = sorted(cells.values(), key=lambda c: (c.size_m or 0,
                                                    round(c.lat or 0, 6),
                                                    round(c.lon or 0, 6)))
    apply_lattice_footprints(ordered)
    return ordered, skipped


def find_terrain_dir(mesh_path, extra_dirs=()):
    """Folder holding the tile's .ter files, searched from the .mesh outwards.

    A .mesh sits either at the scenery package root (``<pkg>/Data+27+089.mesh``
    next to ``<pkg>/terrain/``) or in the Ortho4XP build tree
    (``Tiles/<tile>/Data+27+089.mesh`` with a sibling ``terrain/``), so the
    package root is the first place to look.
    """
    cands = list(extra_dirs)
    d = os.path.dirname(os.path.abspath(mesh_path))
    for _ in range(4):
        cands.append(os.path.join(d, "terrain"))
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    for c in cands:
        if c and os.path.isdir(c) and any(n.lower().endswith(".ter")
                                          for n in os.listdir(c)):
            return os.path.normpath(c)
    return None


class TerrainIndex:
    """Spatial index over terrain cells: point -> containing cell.

    Bins the tile on the finest cell size present (Ortho4XP tiles mix zoom levels,
    e.g. ZL17 with ZL19 patches), fills each cell's bins, then maps every query
    point to the **finest cell that actually contains it**, searching the bin plus
    its neighbours.

    Why the neighbourhood search: a patch of a finer zoom level clips the bins at
    its border, so the bin a point falls in may be assigned to a fine cell that
    does not contain the point at all.  Taking that assignment on trust loses a
    band up to a kilometre wide along every patch border (measured: 17% of a real
    tile's faces), so candidates from the surrounding bins are tested and the
    smallest (finest) containing cell wins.
    """

    #: bin radius searched around the point's own bin
    REACH = 2

    def __init__(self, cells):
        self.cells = list(cells)
        usable = [c for c in self.cells if c.lat is not None]
        if not usable:
            raise ValueError("no terrain cell has a LOAD_CENTER footprint")
        self.sizes = np.array([c.size_m or 0.0 for c in self.cells])
        self.c_lat0 = np.array([c.lat0 if c.lat is not None else 1e9
                                for c in self.cells])
        self.c_lat1 = np.array([c.lat1 if c.lat is not None else -1e9
                                for c in self.cells])
        self.c_lon0 = np.array([c.lon0 if c.lat is not None else 1e9
                                for c in self.cells])
        self.c_lon1 = np.array([c.lon1 if c.lat is not None else -1e9
                                for c in self.cells])
        self.lat_min = min(c.lat0 for c in usable)
        self.lat_max = max(c.lat1 for c in usable)
        self.lon_min = min(c.lon0 for c in usable)
        self.lon_max = max(c.lon1 for c in usable)
        dlat = min(c.lat1 - c.lat0 for c in usable)
        dlon = min(c.lon1 - c.lon0 for c in usable)
        self.nx = int(math.ceil((self.lon_max - self.lon_min) / dlon)) + 2
        self.ny = int(math.ceil((self.lat_max - self.lat_min) / dlat)) + 2
        self.dlon = (self.lon_max - self.lon_min + 2 * dlon) / self.nx
        self.dlat = (self.lat_max - self.lat_min + 2 * dlat) / self.ny
        self.lon0 = self.lon_min - dlon
        self.lat0 = self.lat_min - dlat
        self.grid = np.full(self.nx * self.ny, -1, dtype=np.int32)
        view = self.grid.reshape(self.ny, self.nx)
        # coarse cells first so that finer (higher zoom) cells overwrite them
        for idx in sorted(range(len(self.cells)),
                          key=lambda i: -(self.cells[i].size_m or 0)):
            c = self.cells[idx]
            if c.lat is None:
                continue
            x0 = max(0, int((c.lon0 - self.lon0) / self.dlon))
            x1 = min(self.nx, int((c.lon1 - self.lon0) / self.dlon) + 1)
            y0 = max(0, int((c.lat0 - self.lat0) / self.dlat))
            y1 = min(self.ny, int((c.lat1 - self.lat0) / self.dlat) + 1)
            if x1 > x0 and y1 > y0:
                view[y0:y1, x0:x1] = idx

    def _bins(self, lat, lon):
        bx = np.floor((lon - self.lon0) / self.dlon).astype(np.int64)
        by = np.floor((lat - self.lat0) / self.dlat).astype(np.int64)
        return np.clip(bx, 0, self.nx - 1), np.clip(by, 0, self.ny - 1)

    def lookup(self, lat, lon, exact=True, chunk=250000):
        """Per-point cell index (-1 = no cell covers this point).

        With ``exact=False`` the point's own bin is taken at face value (fast, and
        what the fill order implies); with ``exact=True`` the surrounding bins are
        searched for the finest cell that really contains the point.
        """
        lat = np.asarray(lat, dtype=np.float64)
        lon = np.asarray(lon, dtype=np.float64)
        shape = lat.shape
        la = lat.ravel()
        lo = lon.ravel()
        out = np.full(la.size, -1, dtype=np.int32)
        for start in range(0, la.size, chunk):
            sl = slice(start, min(start + chunk, la.size))
            y = la[sl]
            x = lo[sl]
            bx, by = self._bins(y, x)
            if not exact:
                out[sl] = self.grid[by * self.nx + bx]
                continue
            best = np.full(y.size, np.inf)
            best_idx = np.full(y.size, -1, dtype=np.int32)
            r = self.REACH
            for dy in range(-r, r + 1):
                yy = np.clip(by + dy, 0, self.ny - 1)
                for dx in range(-r, r + 1):
                    xx = np.clip(bx + dx, 0, self.nx - 1)
                    cand = self.grid[yy * self.nx + xx]
                    if not np.any(cand >= 0):
                        continue
                    ci = np.where(cand >= 0, cand, 0)
                    inside = ((cand >= 0)
                              & (y >= self.c_lat0[ci]) & (y <= self.c_lat1[ci])
                              & (x >= self.c_lon0[ci]) & (x <= self.c_lon1[ci]))
                    size = np.where(cand >= 0, self.sizes[ci], np.inf)
                    better = inside & (size < best)
                    if np.any(better):
                        best = np.where(better, size, best)
                        best_idx = np.where(better, cand, best_idx)
            out[sl] = best_idx
        return out.reshape(shape)

    def centroid_cells(self, lat, lon, tris):
        """Cell index per triangle, from the triangle centroid."""
        clat = (lat[tris[:, 0]] + lat[tris[:, 1]] + lat[tris[:, 2]]) / 3.0
        clon = (lon[tris[:, 0]] + lon[tris[:, 1]] + lon[tris[:, 2]]) / 3.0
        return self.lookup(clat, clon), clat, clon


def uv_center(cell_lat, cell_lon, cell_size, lat, lon):
    """Vectorised `tercodec.uv_from_center` (same projection, same constants).

    UV of a texture mapped ONCE over its footprint: the cell centre lands on
    (0.5, 0.5) and the footprint edges on 0 and 1.
    """
    mlat = M_PER_DEG_LAT * np.ones_like(np.asarray(lat, dtype=np.float64))
    mlon = M_PER_DEG_LON * np.cos(np.radians(np.asarray(cell_lat, dtype=np.float64)))
    size = np.asarray(cell_size, dtype=np.float64)
    east = (np.asarray(lon, dtype=np.float64) - np.asarray(cell_lon, dtype=np.float64)) * mlon
    north = (np.asarray(lat, dtype=np.float64) - np.asarray(cell_lat, dtype=np.float64)) * mlat
    return 0.5 + east / size, 0.5 + north / size


def uv_projected(x_m, y_m, xs, ys, off_x=0.0, off_y=0.0, half_w=0.0, half_h=0.0):
    """UV for a PROJECTED texture: metres / scale, from the tile's SW corner.

    `PROJECTED <x> <y>` means the whole texture covers that many metres, so UV is
    position/scale.  The anchor is a fixed global point (the tile's south-west
    corner) rather than Blender's tile-centre origin, so the projection of a tile
    does not depend on where its mesh happens to be centred.
    """
    u = (np.asarray(x_m, dtype=np.float64) + half_w - off_x) / xs
    v = (np.asarray(y_m, dtype=np.float64) + half_h - off_y) / ys
    return u, v