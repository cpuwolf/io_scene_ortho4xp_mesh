"""apt.dat (1200) reader: airport pavement boundaries, as polygons in lat/lon.

Only what the importer needs, and only for the airports inside a tile:

    row 1/16/17   airport header      -> elevation (ft), identifier, name
    row 1302      datum_lat/datum_lon -> the position used to decide "in this tile"
    row 110       pavement header     -> surface type, smoothness, texture orientation
    row 111       node                -> one boundary vertex
    row 112       node + control pt   -> the edge leaving that node is a quadratic Bezier
    row 113       node, closes        -> closes this loop
    row 114       node + control pt, closes
    row 115/116   node ending a string (line features only)

Every node row carries its own coordinates; `112`/`114`/`116` add the control point of
the edge that leaves that node. `113`/`114` end a loop, and a pavement may hold more than
one loop: the first is its outline, the rest are holes cut out of it.

The file is 300+ MB in a full install, so `read_pavements` streams it once, keeps only
the airports whose datum falls inside the requested bounds, and never builds a global
index.
"""

import math

M_PER_DEG_LAT = 111132.0
M_PER_DEG_LON = 111412.84

#: apt.dat surface type codes -> the colour a pavement gets in Blender (linear RGB).
SURFACE_COLORS = {
    1: (0.055, 0.055, 0.060),      # asphalt
    2: (0.30, 0.30, 0.29),         # concrete
    3: (0.10, 0.16, 0.06),         # turf / grass
    4: (0.13, 0.09, 0.06),         # dirt
    5: (0.16, 0.15, 0.13),         # gravel
    6: (0.10, 0.13, 0.10),         # dry lakebed
    12: (0.06, 0.09, 0.12),        # snow / ice
    13: (0.03, 0.08, 0.14),        # water
}
SURFACE_NAMES = {1: "asphalt", 2: "concrete", 3: "turf", 4: "dirt", 5: "gravel",
                 6: "lakebed", 12: "snow", 13: "water"}


def surface_name(code):
    """Readable name for an apt.dat surface type code.

    X-Plane 12 added codes the published table does not document (20, 23, ... appear
    in WED 2.5 output and mean "a paved surface"); those are reported by number and
    drawn with the asphalt colour rather than dropped or guessed at.
    """
    return SURFACE_NAMES.get(code) or "surface %d" % code


def surface_color(code):
    return SURFACE_COLORS.get(code, SURFACE_COLORS[1])


class Pavement:
    """One row-110 chunk: an outline plus holes, with optional Bezier control points."""

    __slots__ = ("airport", "airport_name", "surface", "smoothness", "orientation",
                 "name", "rings", "ctrls", "closed")

    def __init__(self, airport, airport_name, surface, smoothness, orientation, name):
        self.airport = airport
        self.airport_name = airport_name
        self.surface = surface
        self.smoothness = smoothness
        self.orientation = orientation
        self.name = name
        self.rings = []         # [[(lat, lon), ...], ...]  first = outline, rest = holes
        self.ctrls = []         # per ring: per vertex (lat, lon) control point or None
        self.closed = []        # per ring: did a 113/114 (or a 115/116 string end) end it

    def add_node(self, lat, lon, ctrl=None):
        # Writers repeat a node and give it its control point on the 112/114/116 row.
        # Appending would leave a zero-length edge and hang the curve on the wrong
        # segment, so the control point goes back onto the node it repeats.
        if self.rings and not self.closed[-1] and self.rings[-1]:
            last = self.rings[-1][-1]
            if ctrl is not None and last == (lat, lon) \
                    and self.ctrls[-1][-1] is None:
                self.ctrls[-1][-1] = ctrl
                return
        if not self.rings or self.closed[-1]:
            self.rings.append([])
            self.ctrls.append([])
            self.closed.append(False)
        self.rings[-1].append((lat, lon))
        self.ctrls[-1].append(ctrl)

    def end_ring(self):
        if self.rings:
            self.closed[-1] = True

    @property
    def ring(self):
        """The outline, for callers that only care about the outer boundary."""
        return self.rings[0] if self.rings else []

    def rings_m(self, georef, step_m=20.0):
        """Every loop densified to ~step_m, in the tile's local metres (outline first)."""
        out = []
        for ring, ctrls in zip(self.rings, self.ctrls):
            pts = _densify(georef, ring, ctrls, step_m)
            if len(pts) >= 3:
                out.append(pts)
        return out

    def boundary_m(self, georef, step_m=20.0):
        """The outline only, densified; holes are not cut out (see `outline_m`)."""
        rings = self.rings_m(georef, step_m)
        return rings[0] if rings else []

    def outline_m(self, georef, step_m=20.0):
        """One triangulatable ring: the outline with its holes bridged into it."""
        rings = self.rings_m(georef, step_m)
        if not rings:
            return []
        return _bridge_holes(rings[0], rings[1:])


def _densify(georef, ring, ctrls, step_m):
    """Sample a closed loop: straight edges subdivided, Bezier edges followed."""
    out = []
    n = len(ring)
    if n < 3:
        return out
    for i in range(n):
        lat0, lon0 = ring[i]
        lat1, lon1 = ring[(i + 1) % n]
        p0 = _to_m(georef, lat0, lon0)
        p1 = _to_m(georef, lat1, lon1)
        ctrl = ctrls[i] if i < len(ctrls) else None
        if ctrl is not None:
            c = _to_m(georef, ctrl[0], ctrl[1])
            approx = (math.dist(p0, c) + math.dist(c, p1) + math.dist(p0, p1)) * 0.5
            steps = max(2, min(64, int(math.ceil(approx / step_m))))
            for s in range(steps):
                t = s / steps
                mt = 1.0 - t
                out.append((mt * mt * p0[0] + 2 * mt * t * c[0] + t * t * p1[0],
                            mt * mt * p0[1] + 2 * mt * t * c[1] + t * t * p1[1]))
        else:
            steps = max(1, min(256, int(math.ceil(math.dist(p0, p1) / step_m))))
            for s in range(steps):
                t = s / steps
                out.append((p0[0] + (p1[0] - p0[0]) * t,
                            p0[1] + (p1[1] - p0[1]) * t))
    return out


def _area(ring):
    tot = 0.0
    for i in range(len(ring)):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % len(ring)]
        tot += x0 * y1 - x1 * y0
    return tot * 0.5


def _bridge_holes(outer, holes):
    """Cut `holes` out of `outer` by bridging each one to the outline.

    Each hole is joined to the nearest pair of vertices with a zero-width slit (the
    standard way to make a polygon-with-holes ear-clippable), largest hole first so the
    shorter bridges stay inside the area already cut.
    """
    if not holes:
        return list(outer)
    ring = list(outer)
    for hole in sorted(holes, key=lambda h: -abs(_area(h))):
        best = None
        for i, a in enumerate(ring):
            ax, ay = a
            for j, b in enumerate(hole):
                d = (ax - b[0]) ** 2 + (ay - b[1]) ** 2
                if best is None or d < best[0]:
                    best = (d, i, j)
        if best is None:
            continue
        _, i, j = best
        h = hole[j:] + hole[:j]                 # start the hole at its nearest vertex
        ring = ring[:i + 1] + h + [h[0], ring[i]] + ring[i + 1:]
    return ring


def _to_m(georef, lat, lon):
    """lat/lon -> the tile's local metres (same projection as the mesh importer)."""
    return ((lon - georef["lon0"]) * georef["m_per_deg_lon"],
            (lat - georef["lat0"]) * georef["m_per_deg_lat"])


def _f(tok, default=0.0):
    try:
        return float(tok)
    except (TypeError, ValueError):
        return default


def read_pavements(path, bounds, margin=0.02, ids=None):
    """Pavements of every airport whose datum lies inside `bounds`.

    `ids` (a set of upper-case identifiers) keeps those airports even when their
    datum is outside `bounds`, which is what the standalone apt.dat importer needs.

    Returns ``(pavements, airports)``: a list of Pavement objects and a list of
    ``(id, name, elev_ft, lat, lon)`` for the airports that were kept.
    """
    west, east, south, north = bounds
    out, airports = [], []
    cur_apt = None          # dict while inside one airport
    cur_pav = None          # Pavement being filled

    def close_pavement():
        nonlocal cur_pav
        if cur_pav is not None and cur_apt is not None:
            # keep every loop with three distinct points; a writer may repeat the first
            # point of a loop, and holes are loops too
            keep_rings, keep_ctrls, keep_closed = [], [], []
            for ring, ctrls, closed in zip(cur_pav.rings, cur_pav.ctrls, cur_pav.closed):
                pts = list(ring)
                cts = list(ctrls)
                while len(pts) > 1 and pts[0] == pts[-1]:
                    pts.pop()
                    cts.pop()
                if len(pts) >= 3:
                    keep_rings.append(pts)
                    keep_ctrls.append(cts)
                    keep_closed.append(closed)
            if keep_rings:
                cur_pav.rings, cur_pav.ctrls, cur_pav.closed = \
                    keep_rings, keep_ctrls, keep_closed
                cur_apt["pav"].append(cur_pav)
        cur_pav = None

    def close_airport():
        """Keep the airport only if its datum falls inside the tile."""
        close_pavement()
        if cur_apt is None:
            return
        lat, lon = cur_apt["lat"], cur_apt["lon"]
        if lat is None and cur_apt["pts"]:
            # apt.dat 1200 does not require a 1302 datum; without one, place the
            # airport at the mean of its own geometry
            lat = sum(pt[0] for pt in cur_apt["pts"]) / len(cur_apt["pts"])
            lon = sum(pt[1] for pt in cur_apt["pts"]) / len(cur_apt["pts"])
        if ids:
            # identifiers win over the bounds: naming an airport must not depend on
            # which tile happens to be open, and a world-sized bounds must not drag
            # the other 37,000 airports of the file in with it
            keep = cur_apt["id"].upper() in ids
        else:
            keep = lat is not None and lon is not None and (
                west - margin <= lon <= east + margin
                and south - margin <= lat <= north + margin)
        if keep:
            airports.append((cur_apt["id"], cur_apt["name"], cur_apt["elev"],
                             lat, lon))
            out.extend(cur_apt["pav"])

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line or line[0] not in "01123459":
                continue
            f = line.split()
            code = f[0]
            if code in ("1", "16", "17"):
                close_airport()
                cur_apt = {"id": f[4] if len(f) > 4 else "?",
                           "name": " ".join(f[5:]) if len(f) > 5 else "",
                           "elev": _f(f[1]), "lat": None, "lon": None, "pav": [],
                           "pts": []}
                continue
            if cur_apt is None:
                continue
            if code == "1302" and len(f) >= 3:
                if f[1] == "datum_lat":
                    cur_apt["lat"] = _f(f[2])
                elif f[1] == "datum_lon":
                    cur_apt["lon"] = _f(f[2])
                continue
            if code in ("120", "130"):
                # painted lines and the airport boundary use the same 111/112/113
                # nodes; without closing the pavement here their vertices would be
                # appended to it and stretch the apron across the whole airport
                close_pavement()
                continue
            if code in ("102", "14", "15", "18", "19", "1400"):
                # airport furniture carries a position: collect it in case this
                # airport has no 1302 datum (apt.dat 1200 does not require one)
                a = _f(f[1])
                b = _f(f[2]) if len(f) > 2 else 999.0
                if code == "102" and len(f) > 3:          # helipad: designator first
                    a, b = _f(f[2]), _f(f[3])
                if -90.0 <= a <= 90.0 and -180.0 <= b <= 180.0:
                    cur_apt["pts"].append((a, b))
                continue
            if code == "110":
                close_pavement()
                cur_pav = Pavement(cur_apt["id"], cur_apt["name"],
                                   int(_f(f[1], 1)), _f(f[2], 0.25), _f(f[3]),
                                   " ".join(f[4:]))
                continue
            if cur_pav is None:
                continue
            if code in ("111", "112", "113", "114", "115", "116") and len(f) >= 3:
                lat, lon = _f(f[1]), _f(f[2])
                ctrl = None
                if code in ("112", "114", "116") and len(f) >= 5:
                    ctrl = (_f(f[3]), _f(f[4]))
                cur_pav.add_node(lat, lon, ctrl)
                if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                    cur_apt["pts"].append((lat, lon))
                if code in ("113", "114", "115", "116"):
                    # 113/114 close this loop; 115/116 end a string. Either way the
                    # next node row starts a new loop -- a hole, or a new feature
                    cur_pav.end_ring()
                continue
    close_airport()
    return out, airports


def _point_in_ring(pt, ring):
    """Even-odd containment test for a ring given in metres."""
    x, y = pt
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y):
            xcross = xi + (y - yi) * (xj - xi) / (yj - yi)
            if x < xcross:
                inside = not inside
        j = i
    return inside


def triangulate(rings, filter_triangles=True):
    """Triangulate one pavement: rings[0] is the outline, the rest are holes.

    The holes are bridged into the outline and the result is ear-clipped; with
    `filter_triangles` every triangle whose centroid is not inside the outline, or is
    inside a hole, is dropped. That keeps a bridged slit from painting pavement across
    the hole the slit was cut for, which is what makes an apron look wrong even when
    the boundary itself is right.
    """
    if not rings:
        return [], []
    outer, holes = rings[0], rings[1:]
    bridged = _bridge_holes(outer, holes)
    tris = ear_clip(bridged)
    if not filter_triangles:
        return bridged, tris
    keep = []
    for t in tris:
        cx = (bridged[t[0]][0] + bridged[t[1]][0] + bridged[t[2]][0]) / 3.0
        cy = (bridged[t[0]][1] + bridged[t[1]][1] + bridged[t[2]][1]) / 3.0
        c = (cx, cy)
        if not _point_in_ring(c, outer):
            continue
        if any(_point_in_ring(c, h) for h in holes):
            continue
        keep.append(t)
    return bridged, keep


def ear_clip(points):
    """Triangulate a simple polygon given as [(x, y), ...] (local metres).

    Returns index triples. Densified pavements are full of collinear runs and of
    points that sit exactly on another ear's edge, so the containment test is strict
    (a point on an edge does not block an ear) and a second pass accepts collinear
    ears: without those two the clipper stalls on perfectly ordinary aprons and leaves
    a fifth of the pavement untriangulated.

    A ring the source file made self-intersecting still ends up with the triangles the
    clipper managed to produce rather than a fan spanning the whole chunk.
    """
    n = len(points)
    if n < 3:
        return []
    pts, idxmap = [], []                  # cleaned ring + where each point came from
    for k, p in enumerate(points):        # drop repeated neighbours: zero-length edges
        if not pts or abs(p[0] - pts[-1][0]) > 1e-6 or abs(p[1] - pts[-1][1]) > 1e-6:
            pts.append(p)
            idxmap.append(k)
    while len(pts) > 3 and abs(pts[0][0] - pts[-1][0]) < 1e-6 \
            and abs(pts[0][1] - pts[-1][1]) < 1e-6:
        pts.pop()
        idxmap.pop()
    n = len(pts)
    if n < 3:
        return []

    area = 0.0
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        area += x0 * y1 - x1 * y0
    order = list(range(n)) if area > 0 else list(range(n))[::-1]

    def cross(o, a, b):
        return ((pts[a][0] - pts[o][0]) * (pts[b][1] - pts[o][1])
                - (pts[a][1] - pts[o][1]) * (pts[b][0] - pts[o][0]))

    def strictly_inside(p, a, b, c):
        eps = 1e-9
        d1 = ((p[0] - pts[a][0]) * (pts[b][1] - pts[a][1])
              - (p[1] - pts[a][1]) * (pts[b][0] - pts[a][0]))
        d2 = ((p[0] - pts[b][0]) * (pts[c][1] - pts[b][1])
              - (p[1] - pts[b][1]) * (pts[c][0] - pts[b][0]))
        d3 = ((p[0] - pts[c][0]) * (pts[a][1] - pts[c][1])
              - (p[1] - pts[c][1]) * (pts[a][0] - pts[c][0]))
        return (d1 > eps and d2 > eps and d3 > eps) or \
               (d1 < -eps and d2 < -eps and d3 < -eps)

    tris = []
    stalled = 0
    for min_cross in (1e-9, -1e-9):        # strict first, then allow collinear ears
        while len(order) > 3 and stalled <= n:
            clipped = False
            best = None
            best_cross = 0.0
            for k in range(len(order)):
                i0 = order[(k - 1) % len(order)]
                i1 = order[k]
                i2 = order[(k + 1) % len(order)]
                c = cross(i0, i1, i2)
                if abs(c) < abs(best_cross) or best is None:
                    best, best_cross = k, c
                if c <= min_cross:
                    continue
                ok = True
                for j in order:
                    if j in (i0, i1, i2):
                        continue
                    if strictly_inside(pts[j], i0, i1, i2):
                        ok = False
                        break
                if ok:
                    tris.append((idxmap[i0], idxmap[i1], idxmap[i2]))
                    order.pop(k)
                    clipped = True
                    break
            if clipped:
                continue
            # No ear anywhere: the ring pinches itself at a repeated vertex or doubles
            # back on an edge. Collapsing the flattest vertex loses no measurable area
            # and lets the clipper finish, which beats fanning the remnant across the
            # chunk (that is how a 12,000 m2 apron became 14,700 m2 of pavement).
            if best is not None:
                order.pop(best)
            stalled += 1
        if len(order) <= 3:
            break
    if len(order) == 3:
        tris.append(tuple(idxmap[i] for i in order))
    return tris
