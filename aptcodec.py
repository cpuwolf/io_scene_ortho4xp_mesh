"""apt.dat (1200) reader: airport pavement boundaries, as polygons in lat/lon.

Only what the importer needs, and only for the airports inside a tile:

    row 1/16/17   airport header      -> elevation (ft), identifier, name
    row 1302      datum_lat/datum_lon -> the position used to decide "in this tile"
    row 110       pavement header     -> surface type, smoothness, texture orientation
    row 111/113   pavement node       -> one boundary vertex
    row 112       node + control pt   -> the edge leaving that node is a quadratic Bezier

`112` repeats the coordinates of the node it belongs to and adds one control point, so
the edge from that node to the next one curves through it.

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
    """One row-110 chunk: a closed boundary with optional Bezier control points."""

    __slots__ = ("airport", "airport_name", "surface", "smoothness", "orientation",
                 "name", "ring", "ctrl")

    def __init__(self, airport, airport_name, surface, smoothness, orientation, name):
        self.airport = airport
        self.airport_name = airport_name
        self.surface = surface
        self.smoothness = smoothness
        self.orientation = orientation
        self.name = name
        self.ring = []          # [(lat, lon), ...]   first point is not repeated
        self.ctrl = []          # per vertex: (lat, lon) control point or None

    def boundary_m(self, georef, step_m=20.0):
        """Closed boundary densified to ~step_m, in the tile's local metres.

        Straight edges are subdivided; an edge whose start vertex carries a control
        point is a quadratic Bezier, so the boundary follows the curve the airport
        designer drew rather than a chord.
        """
        out = []
        n = len(self.ring)
        for i in range(n):
            lat0, lon0 = self.ring[i]
            lat1, lon1 = self.ring[(i + 1) % n]
            p0 = _to_m(georef, lat0, lon0)
            p1 = _to_m(georef, lat1, lon1)
            ctrl = self.ctrl[i]
            if ctrl is not None:
                c = _to_m(georef, ctrl[0], ctrl[1])
                # curve length: chord + 2x handle-to-chord distance, good enough to
                # pick a subdivision count
                approx = (math.dist(p0, c) + math.dist(c, p1) + math.dist(p0, p1)) * 0.5
                steps = max(2, min(64, int(math.ceil(approx / step_m))))
                for s in range(steps):
                    t = s / steps
                    mt = 1.0 - t
                    x = mt * mt * p0[0] + 2 * mt * t * c[0] + t * t * p1[0]
                    y = mt * mt * p0[1] + 2 * mt * t * c[1] + t * t * p1[1]
                    out.append((x, y))
            else:
                steps = max(1, min(256, int(math.ceil(math.dist(p0, p1) / step_m))))
                for s in range(steps):
                    t = s / steps
                    out.append((p0[0] + (p1[0] - p0[0]) * t,
                                p0[1] + (p1[1] - p0[1]) * t))
        return out


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
        if cur_pav is not None and len(cur_pav.ring) >= 3 and cur_apt is not None:
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
        keep = bool(ids) and cur_apt["id"].upper() in ids
        if lat is not None and lon is not None and (
                west - margin <= lon <= east + margin
                and south - margin <= lat <= north + margin):
            keep = True
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
            if code in ("111", "113") and len(f) >= 3:
                lat, lon = _f(f[1]), _f(f[2])
                cur_pav.ring.append((lat, lon))
                cur_pav.ctrl.append(None)
                if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                    cur_apt["pts"].append((lat, lon))
                if code == "113":
                    close_pavement()
                continue
            if code == "112" and len(f) >= 5:
                lat, lon = _f(f[1]), _f(f[2])
                pt = (_f(f[3]), _f(f[4]))
                # `112` repeats the node it belongs to and adds its control point;
                # a node we have not seen yet is inserted first
                if cur_pav.ring and abs(cur_pav.ring[-1][0] - lat) < 1e-7 \
                        and abs(cur_pav.ring[-1][1] - lon) < 1e-7:
                    cur_pav.ctrl[-1] = pt
                else:
                    cur_pav.ring.append((lat, lon))
                    cur_pav.ctrl.append(pt)
                continue
    close_airport()
    return out, airports


def ear_clip(points):
    """Triangulate a simple polygon given as [(x, y), ...] (local metres).

    Returns index triples. O(n^2), which is nothing for an airport pavement
    boundary (tens of vertices). Self-intersecting or duplicate-vertex rings fall
    back to a fan over the remaining points instead of losing the pavement.
    """
    n = len(points)
    if n < 3:
        return []
    area = 0.0
    for i in range(n):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % n]
        area += x0 * y1 - x1 * y0
    order = list(range(n)) if area > 0 else list(range(n))[::-1]

    def cross(o, a, b):
        return ((points[a][0] - points[o][0]) * (points[b][1] - points[o][1])
                - (points[a][1] - points[o][1]) * (points[b][0] - points[o][0]))

    def inside(p, a, b, c):
        d1 = cross_p(p, points[a], points[b])
        d2 = cross_p(p, points[b], points[c])
        d3 = cross_p(p, points[c], points[a])
        neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
        pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
        return not (neg and pos)

    def cross_p(p, a, b):
        return ((p[0] - a[0]) * (b[1] - a[1]) - (p[1] - a[1]) * (b[0] - a[0]))

    tris = []
    guard = 0
    while len(order) > 3 and guard < n * n:
        guard += 1
        clipped = False
        for k in range(len(order)):
            i0 = order[(k - 1) % len(order)]
            i1 = order[k]
            i2 = order[(k + 1) % len(order)]
            if cross(i0, i1, i2) <= 1e-9:
                continue
            ok = True
            for j in order:
                if j in (i0, i1, i2):
                    continue
                if inside(points[j], i0, i1, i2):
                    ok = False
                    break
            if ok:
                tris.append((i0, i1, i2))
                order.pop(k)
                clipped = True
                break
        if not clipped:
            break
    if len(order) == 3:
        tris.append((order[0], order[1], order[2]))
    elif len(order) > 3:                     # degenerate ring: fan, keep the shape
        for k in range(1, len(order) - 1):
            tris.append((order[0], order[k], order[k + 1]))
    return tris
