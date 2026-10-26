"""
dsfcodec.py -- Pure-Python reader for X-Plane DSF scenery files (terrain-mesh subset).

Verified against Laminar's xptools (src/DSF/DSFLib.cpp, src/Utils/XChunkyFileUtils.cpp)
and against real DSF files shipped with X-Plane 12.

Terrain-mesh point-pool plane layout (confirmed empirically on real files):
    plane 0  longitude   scale=1/D  offset=cell_west      (D = grid divisions per degree)
    plane 1  latitude    scale=1/D  offset=cell_south
    plane 2  elevation   scale=65535 offset=-32768        (integer metres == raw-32768)
    plane 3  normal X    scale=2     offset=-1            (range -1..1)
    plane 4  normal Z    scale=2     offset=-1
    plane 5+ pass-through scale=1    offset=0

Coordinate decode:  value = raw * scale * (1/65535) + offset      (POOL/SCAL, 16-bit)
                    value = raw * scale * (1/4294967295) + offset (PO32/SC32, 32-bit)
RLE code byte: high bit set -> run of (code & 0x7F) copies of one value;
               high bit clear -> (code & 0x7F) literal values.

Command payload layouts follow xptools exactly (note: NestedPolygonRange carries an
extra uint16 before its index loop; "commands 19-22 were removed during DSF spec
development" per DSFDefs.h).

No third-party dependencies and no subprocess: 7Z-compressed DSFs (global scenery)
are decoded by this package's own `sevenzip` module (stdlib `lzma`).  Writing a DSF
is deliberately out of scope here -- see the sibling project xplane-dsf-blender.
"""

from __future__ import annotations

import hashlib
import math
import os
import struct
from dataclasses import dataclass, field

try:                                        # loaded as a Blender extension package
    from . import sevenzip as SZ
except ImportError:                         # run headless as a plain script
    import sevenzip as SZ

COOKIE = b"XPLNEDSF"

#: Elevation floor. Terrain planes store elevation as raw-32768 metres, so a raw 0
#: decodes to -32768 m: X-Plane leaves whole LOD layers like that (no height, zero
#: normals) and takes their height from the tile's raster DEM instead.
NO_ELEVATION_M = -30000.0
MASTER_VERSION = 1
RECIP16 = 1.0 / 65535.0
RECIP32 = 1.0 / 4294967295.0

# ---------------------------------------------------------------- atom ids
A_HEAD = "HEAD"
A_PROP = "PROP"
A_DEFN = "DEFN"
A_TERT = "TERT"
A_OBJT = "OBJT"
A_POLY = "POLY"
A_NETW = "NETW"
A_DEMN = "DEMN"
A_GEOD = "GEOD"
A_POOL = "POOL"
A_SCAL = "SCAL"
A_PO32 = "PO32"
A_SC32 = "SC32"
A_CMDS = "CMDS"
A_DEMS = "DEMS"

# ---------------------------------------------------------------- commands
C_POOL_SELECT = 1
C_JUNCTION_OFFSET = 2
C_SETDEF8 = 3
C_SETDEF16 = 4
C_SETDEF32 = 5
C_ROAD_SUBTYPE8 = 6
C_OBJECT = 7
C_OBJECT_RANGE = 8
C_NETWORK_CHAIN = 9
C_NETWORK_CHAIN_RANGE = 10
C_NETWORK_CHAIN32 = 11
C_POLYGON = 12
C_POLYGON_RANGE = 13
C_NESTED_POLYGON = 14
C_NESTED_POLYGON_RANGE = 15
C_TERRAIN_PATCH = 16
C_TERRAIN_PATCH_FLAGS = 17
C_TERRAIN_PATCH_FLAGS_LOD = 18
C_TRIANGLE = 23
C_TRIANGLE_CROSSPOOL = 24
C_TRIANGLE_RANGE = 25
C_TRIANGLE_STRIP = 26
C_TRIANGLE_STRIP_CROSSPOOL = 27
C_TRIANGLE_STRIP_RANGE = 28
C_TRIANGLE_FAN = 29
C_TRIANGLE_FAN_CROSSPOOL = 30
C_TRIANGLE_FAN_RANGE = 31
C_COMMENT8 = 32
C_COMMENT16 = 33
C_COMMENT32 = 34

PATCH_FLAG_PHYSICAL = 1
PATCH_FLAG_OVERLAY = 2

P_LON, P_LAT, P_ELEV, P_NX, P_NZ = 0, 1, 2, 3, 4
MAX_POOL_ITEMS = 65535
ELEV_OFFSET = -32768.0
ELEV_SCALE = 65535.0


class DSFError(Exception):
    pass


# ==================================================================== reading
def _id_bytes(name: str) -> bytes:
    """Atom id as stored on disk: the logical name with its bytes reversed."""
    return name[::-1].encode("latin1")


def _u8(b, o):
    return b[o], o + 1


def _u16(b, o):
    return struct.unpack_from("<H", b, o)[0], o + 2


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0], o + 4


def _f32(b, o):
    return struct.unpack_from("<f", b, o)[0], o + 4


def iter_atoms(buf, start, end):
    """Yield (name, payload_start, payload_end) for atoms in [start,end)."""
    o = start
    while o < end:
        if o + 8 > end:
            raise DSFError(f"trailing {end - o} bytes in atom container")
        aid, size = struct.unpack_from("<II", buf, o)
        if size < 8 or o + size > end:
            raise DSFError(f"bad atom size {size} at offset {o}")
        name = struct.pack("<I", aid).decode("latin1")[::-1]
        yield name, o + 8, o + size
        o += size


def _read_string_table(buf, s, e):
    raw = buf[s:e]
    if not raw:
        return []
    parts = raw.split(b"\x00")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    return [p.decode("utf-8", "replace") for p in parts]


def _rle_expand(buf, p, count, wide):
    """Expand RLE-encoded values; returns (list_of_ints, new_pos)."""
    out = []
    fmt = "<I" if wide else "<H"
    sz = 4 if wide else 2
    while len(out) < count:
        if p >= len(buf):
            raise DSFError("RLE stream truncated")
        code = buf[p]
        p += 1
        n = code & 0x7F
        if n == 0:
            continue
        if code & 0x80:
            v = struct.unpack_from(fmt, buf, p)[0]
            p += sz
            out.extend([v] * n)
        else:
            for _ in range(n):
                out.append(struct.unpack_from(fmt, buf, p)[0])
                p += sz
    return out[:count], p


def _decode_plane(buf, p, count, mode, wide):
    """Decode one plane of a planar numeric atom. Returns (values, new_pos)."""
    sz = 4 if wide else 2
    mask = 0xFFFFFFFF if wide else 0xFFFF
    if mode == 0:  # raw
        vals = list(struct.unpack_from("<%d%s" % (count, "I" if wide else "H"), buf, p))
        return vals, p + count * sz
    if mode == 1:  # differenced
        raw = list(struct.unpack_from("<%d%s" % (count, "I" if wide else "H"), buf, p))
        p += count * sz
        out, last = [], 0
        for d in raw:
            last = (last + d) & mask
            out.append(last)
        return out, p
    if mode == 2:  # RLE
        return _rle_expand(buf, p, count, wide)
    if mode == 3:  # RLE + differenced
        raw, p = _rle_expand(buf, p, count, wide)
        out, last = [], 0
        for d in raw:
            last = (last + d) & mask
            out.append(last)
        return out, p
    raise DSFError(f"unknown planar encoding mode {mode}")


def _decode_pool(buf, s, e, wide):
    """Decode POOL/PO32 payload -> (n_items, planes[list_of_lists_of_raw_ints])."""
    n, = struct.unpack_from("<I", buf, s)
    nplanes = buf[s + 4]
    p = s + 5
    planes = []
    for _ in range(nplanes):
        mode = buf[p]
        p += 1
        vals, p = _decode_plane(buf, p, n, mode, wide)
        planes.append(vals)
    return n, planes


def _read_scal(buf, s, e):
    """SCAL/SC32 payload -> list of (scale, offset) per plane."""
    n = (e - s) // 4
    floats = struct.unpack_from("<%df" % n, buf, s)
    return [(floats[i], floats[i + 1]) for i in range(0, n - 1, 2)]


# ------------------------------------------------------------------- rasters
#: X-Plane 12 DSF tiles carry their terrain heights twice: in the point pools (the
#: mesh) and in a raster DEM (`DEMS` container: `DEMI` header + `DEMD` pixels).  A
#: vertex whose pool elevation is 0 (which decodes to NO_ELEVATION_M) has no height
#: of its own -- Global Scenery leaves whole LOD layers that way -- so the raster is
#: the file's own source for it.
A_DEMS = "DEMS"
A_DEMI = "DEMI"
A_DEMD = "DEMD"


class Raster:
    """One DEM layer: a width x height grid of raw samples plus scale/offset."""

    __slots__ = ("width", "height", "bpp", "fmt", "flags", "scale", "offset", "vals")

    def __init__(self, width, height, bpp, fmt, flags, scale, offset, vals):
        self.width = width
        self.height = height
        self.bpp = bpp
        self.fmt = fmt
        self.flags = flags
        self.scale = scale
        self.offset = offset
        self.vals = vals

    def __repr__(self):
        return ("<Raster %dx%d bpp%d fmt%d scale %g offset %g>"
                % (self.width, self.height, self.bpp, self.fmt, self.scale,
                   self.offset))

    def height_at(self, lon, lat, bounds, flip_y=True, dx=0.0, dy=0.0):
        """Bilinear height in metres at lon/lat, or None outside the grid.

        The grid is registered to the tile's bounds; `flip_y`/`dx`/`dy` are the
        alignment `fit_raster` measured, because the writer's pixel-centre
        convention is not something the file states.
        """
        west, east, south, north = bounds
        w, h = self.width, self.height
        if w < 2 or h < 2 or east == west or north == south:
            return None
        fx = (lon - west) / (east - west) * (w - 1) + dx
        fy = (((lat - south) / (north - south)) if flip_y
              else ((north - lat) / (north - south))) * (h - 1) + dy
        # edge-extend rather than refuse: a vertex a few metres outside the grid
        # (patch overhang at the tile border) takes the border height, which is
        # what a raster read does everywhere else
        fx = min(max(fx, 0.0), w - 1.0)
        fy = min(max(fy, 0.0), h - 1.0)
        x0, y0 = int(fx), int(fy)
        if x0 >= w - 1:
            x0 = w - 2
            fx = float(x0)
        if y0 >= h - 1:
            y0 = h - 2
            fy = float(y0)
        tx, ty = fx - x0, fy - y0
        v = self.vals
        i = y0 * w + x0
        top = v[i] * (1.0 - tx) + v[i + 1] * tx
        bot = v[i + w] * (1.0 - tx) + v[i + w + 1] * tx
        return (top * (1.0 - ty) + bot * ty) * self.scale + self.offset


def _read_dems(raw, s, e):
    """Decode a `DEMS` container into a list of Raster objects."""
    out = []
    headers, blocks = [], []
    for name, a, b in iter_atoms(raw, s, e):
        if name == A_DEMI:
            headers.append((a, b))
        elif name == A_DEMD:
            blocks.append((a, b))
    for (hs, he), (ds, de) in zip(headers, blocks):
        if he - hs < 20:
            continue
        ver, bpp, flags, w, h, scale, offset = struct.unpack_from("<BBHIIff", raw, hs)
        fmt = flags & 0x3
        if w == 0 or h == 0 or bpp not in (1, 2, 4):
            continue
        code = {0: "f", 1: {1: "b", 2: "h", 4: "i"},
                2: {1: "B", 2: "H", 4: "I"},
                3: {1: "B", 2: "H", 4: "I"}}[fmt][bpp]
        count = min(w * h, (de - ds) // bpp)
        if count < w * h:
            continue
        vals = struct.unpack_from("<%d%s" % (w * h, code), raw, ds)
        if fmt == 3:                       # normalised unsigned: 0..1 of the range
            top = float((1 << (8 * bpp)) - 1)
            vals = tuple(v / top for v in vals)
            scale, offset = (scale, offset) if scale else (1.0, 0.0)
        out.append(Raster(w, h, bpp, fmt, flags, scale, offset, vals))
    return out


def _sharpness(r, step=4, probe=200000):
    """Mean |elevation difference| between neighbouring pixels (metres per pixel).

    This is how the layers are told apart: the same landscape is shipped twice, and the
    smoothed variant (a lower LOD) is several times flatter than the real DEM.  Pixel
    count is capped so a 1201x1201 layer costs milliseconds.
    """
    v = r.vals
    w, h = r.width, r.height
    scale = r.scale or 1.0
    if w < 2:
        return 0.0
    total = 0.0
    n = 0
    rows = range(0, h, step)
    for y in rows:
        base = y * w
        for x in range(0, w - step, step):
            total += abs(v[base + x + step] - v[base + x]) * scale
            n += 1
            if n >= probe:
                return total / n
    return total / n if n else 0.0


def pick_dem(rasters, bounds, samples, min_width=512, max_points=200, accept_m=250.0):
    """Choose the tile's DEM layer and register it.

    Detail decides the layer, not agreement with the mesh: a smoothed LOD variant of
    the same DEM fits the mesh *better* (it is flatter, so less to disagree with) and
    is exactly the wrong choice -- it flattens the whole tile.  The sharpest plausible
    full-tile layer is therefore used, and only its registration (row order, pixel
    offset) is measured, against the heights the mesh already carries.

    Returns ``(raster, flip_y, dx, dy, median_error_m, sharpness)`` or None.
    """
    cands = []
    for r in rasters:
        if r.width < min_width or r.height < min_width:
            continue
        if r.fmt == 0 and r.bpp != 4:
            continue
        vals = r.vals
        step = max(1, len(vals) // 20000)
        scale = r.scale or 1.0
        lo = min(vals[::step]) * scale + (r.offset or 0.0)
        hi = max(vals[::step]) * scale + (r.offset or 0.0)
        if not (-900.0 <= lo <= 9000.0 and -900.0 <= hi <= 9000.0) or hi - lo < 20.0:
            continue                       # a mask/depth layer, not terrain
        # a layer is only a DEM candidate if it can reproduce heights the mesh already
        # has; masks, water depth and class rasters fail this even when their value
        # range happens to look like metres
        fit = fit_raster([r], samples, bounds, min_width=min_width,
                         max_points=max_points, accept_m=10.0)
        if fit is None:
            continue
        cands.append((_sharpness(r), r, fit))
    if not cands:
        return None
    # detail decides between the survivors: the sharp layer is the real DEM, the
    # smoothed one is a lower LOD of the same landscape
    cands.sort(key=lambda t: -t[0])
    sharp, r, fit = cands[0]
    return fit[0], fit[1], fit[2], fit[3], fit[4], sharp


def fit_raster(rasters, samples, bounds, min_width=64, max_points=200, accept_m=120.0):
    """Find the raster layer that really is this tile's elevation grid.

    `samples` are pool vertices the mesh already gives a height for (only their
    lon, lat and elevation are read), so the fit is measured against data, not assumed: every candidate raster
    is tried with both row orders and a small pixel-offset search, and the best
    one is accepted only if it reproduces those heights closely.

    Returns ``(raster, flip_y, dx, dy, median_error_m)`` or None.
    """
    if not rasters or not samples:
        return None
    pts = samples[::max(1, len(samples) // max_points)][:max_points]
    best = None
    for r in rasters:
        if r.width < min_width or r.height < min_width:
            continue
        for flip_y in (True, False):
            # coarse search, then refine around the winner
            for step, span in ((8.0, 24.0), (2.0, 6.0), (1.0, 2.0)):
                centre = (best[2], best[3]) if best and best[0] is r else (0.0, 0.0)
                offsets = [(centre[0] + sx * step, centre[1] + sy * step)
                           for sx in range(-int(span / step), int(span / step) + 1)
                           for sy in range(-int(span / step), int(span / step) + 1)]
                for dx, dy in offsets:
                    diffs = []
                    for p in pts:
                        lon, lat, z = p[0], p[1], p[2]
                        hv = r.height_at(lon, lat, bounds, flip_y, dx, dy)
                        if hv is not None:
                            diffs.append(abs(hv - z))
                    if len(diffs) < max(8, len(pts) // 2):
                        continue
                    diffs.sort()
                    med = diffs[len(diffs) // 2]
                    if best is None or med < best[4]:
                        best = (r, flip_y, dx, dy, med)
    if best is None or best[4] > accept_m:
        return None
    return best[0], best[1], best[2], best[3], best[4]


@dataclass
class Patch:
    """One terrain patch: triangles sharing terrain definition / LOD / flags."""
    definition: int = 0
    flags: int = 0
    lod_near: float = 0.0
    lod_far: float = 0.0
    triangles: list = field(default_factory=list)  # each: ((pool, idx) x3)


@dataclass
class Polygon:
    """One polygon / facade / forest / line primitive from the DSF.

    ``rings`` holds one list per winding, each a list of (lon, lat) in degrees; the
    first winding is the outline, the rest are holes. ``param`` is the per-polygon
    16-bit parameter, whose meaning belongs to the definition (class), not to us.
    """
    definition: int = 0
    klass: str = ""
    param: int = 0
    rings: list = field(default_factory=list)


@dataclass
class DSF:
    props: list = field(default_factory=list)          # ordered [(k, v), ...]
    defn: dict = field(default_factory=dict)           # atom name -> [paths]
    pools: list = field(default_factory=list)          # decoded vertex tuples
    pool_scales: list = field(default_factory=list)    # per pool: [(scale,offset)]
    patches: list = field(default_factory=list)        # Patch objects
    polygons: list = field(default_factory=list)       # Polygon objects
    unhandled: dict = field(default_factory=dict)      # cmd id -> count
    source_path: str | None = None
    was_compressed: bool = False
    rasters: list = field(default_factory=list)        # DEM layers (Raster objects)

    def prop(self, name, default=None):
        for k, v in self.props:
            if k == name:
                return v
        return default

    def bounds(self):
        """(west, east, south, north) as floats, from the sim/* properties."""
        out = []
        for k in ("sim/west", "sim/east", "sim/south", "sim/north"):
            v = self.prop(k)
            if v is None:
                return None
            out.append(float(v))
        return tuple(out)

    def terrain_defs(self):
        return self.defn.get(A_TERT, [])


def load_dsf(path):
    """Read a .dsf, transparently decoding the 7z wrapper Global Scenery uses."""
    dsf = DSF(source_path=path)
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:8] != COOKIE:
        if raw[:2] == b"7z":
            dsf.was_compressed = True
            try:
                _name, raw = SZ.load(path)
            except SZ.SevenZipError as ex:
                raise DSFError("7z: %s" % ex) from ex
        else:
            raise DSFError("not a DSF file (cookie %r)" % raw[:8])
    _parse(dsf, raw)
    return dsf


def _parse(dsf: DSF, raw: bytes):
    if raw[:8] != COOKIE:
        raise DSFError("bad cookie after decompression")
    ver, = struct.unpack_from("<I", raw, 8)
    if ver != MASTER_VERSION:
        raise DSFError(f"unsupported DSF master version {ver}")
    body_end = len(raw) - 16
    if hashlib.md5(raw[:body_end]).digest() != raw[body_end:]:
        dsf.props.append(("_dsf/md5_mismatch", "1"))

    geod = cmds = dems = None
    for name, s, e in iter_atoms(raw, 12, body_end):
        if name == A_HEAD:
            for n2, s2, e2 in iter_atoms(raw, s, e):
                if n2 == A_PROP:
                    strs = _read_string_table(raw, s2, e2)
                    dsf.props.extend(list(zip(strs[0::2], strs[1::2])))
        elif name == A_DEFN:
            for n2, s2, e2 in iter_atoms(raw, s, e):
                if n2 in (A_TERT, A_OBJT, A_POLY, A_NETW, A_DEMN):
                    dsf.defn[n2] = _read_string_table(raw, s2, e2)
        elif name == A_GEOD:
            geod = (s, e)
        elif name == A_CMDS:
            cmds = (s, e)
        elif name == A_DEMS:
            dems = (s, e)

    if geod is None or cmds is None:
        raise DSFError("DSF missing GEOD or CMDS atom")

    # pools and their scalings are matched by order of appearance of each kind
    pool_pairs, pool32_pairs = [], []
    scales, scales32 = [], []
    for name, s, e in iter_atoms(raw, geod[0], geod[1]):
        if name == A_POOL:
            pool_pairs.append((s, e))
        elif name == A_PO32:
            pool32_pairs.append((s, e))
        elif name == A_SCAL:
            scales.append(_read_scal(raw, s, e))
        elif name == A_SC32:
            scales32.append(_read_scal(raw, s, e))

    def build(pairs, scals, wide):
        out = []
        scal_out = []
        for i, (s, e) in enumerate(pairs):
            n, planes = _decode_pool(raw, s, e, wide)
            sc = scals[i] if i < len(scals) else [(1.0, 0.0)] * len(planes)
            recip = RECIP32 if wide else RECIP16
            verts = []
            for v in range(n):
                vert = []
                for p in range(len(planes)):
                    rv = planes[p][v]
                    if p < len(sc):
                        scl, off = sc[p]
                        vert.append(rv * scl * recip + off if scl else float(rv))
                    else:
                        vert.append(float(rv))
                verts.append(tuple(vert))
            out.append(verts)
            scal_out.append([(float(a), float(b)) for a, b in sc])
        return out, scal_out

    if dems is not None:
        dsf.rasters = _read_dems(raw, dems[0], dems[1])

    pools16, scal16 = build(pool_pairs, scales, False)
    pools32, scal32 = build(pool32_pairs, scales32, True)
    dsf.pools = pools16 + pools32
    # plane scaling is kept so the writer can re-encode non-standard planes
    # (terrain-type dependent planes 5+ use scales the format does not fix)
    dsf.pool_scales = scal16 + scal32
    _parse_commands(dsf, raw, cmds[0], cmds[1])


def _parse_commands(dsf: DSF, raw, s, e):
    o = s
    cur_pool = 0
    cur_def = 0
    patch_flags = 0
    lod_near = -1.0
    lod_far = -1.0
    cur_patch = None
    raw_polys = []
    unhandled = dsf.unhandled

    def close_patch():
        nonlocal cur_patch
        if cur_patch is not None and cur_patch.triangles:
            dsf.patches.append(cur_patch)
        cur_patch = None

    def add_prim(verts, kind):
        """verts: list of (pool, index); kind in tri/strip/fan."""
        if cur_patch is None:
            return
        if kind == "tri":
            for i in range(0, len(verts) - 2, 3):
                cur_patch.triangles.append((verts[i], verts[i + 1], verts[i + 2]))
        elif kind == "strip":
            for i in range(len(verts) - 2):
                if i % 2 == 0:
                    cur_patch.triangles.append((verts[i], verts[i + 1], verts[i + 2]))
                else:
                    cur_patch.triangles.append((verts[i], verts[i + 2], verts[i + 1]))
        elif kind == "fan":
            for i in range(1, len(verts) - 1):
                cur_patch.triangles.append((verts[0], verts[i], verts[i + 1]))

    PRIM = {C_TRIANGLE: "tri", C_TRIANGLE_STRIP: "strip", C_TRIANGLE_FAN: "fan",
            C_TRIANGLE_CROSSPOOL: "tri", C_TRIANGLE_STRIP_CROSSPOOL: "strip",
            C_TRIANGLE_FAN_CROSSPOOL: "fan", C_TRIANGLE_RANGE: "tri",
            C_TRIANGLE_STRIP_RANGE: "strip", C_TRIANGLE_FAN_RANGE: "fan"}
    SIMPLE = (C_TRIANGLE, C_TRIANGLE_STRIP, C_TRIANGLE_FAN)
    CROSS = (C_TRIANGLE_CROSSPOOL, C_TRIANGLE_STRIP_CROSSPOOL, C_TRIANGLE_FAN_CROSSPOOL)
    RANGE = (C_TRIANGLE_RANGE, C_TRIANGLE_STRIP_RANGE, C_TRIANGLE_FAN_RANGE)

    while o < e:
        cid = raw[o]
        o += 1
        if cid == C_POOL_SELECT:
            cur_pool, o = _u16(raw, o)
        elif cid == C_JUNCTION_OFFSET:
            _v, o = _u32(raw, o)
        elif cid in (C_SETDEF8, C_ROAD_SUBTYPE8):
            cur_def, o = _u8(raw, o)
        elif cid == C_SETDEF16:
            cur_def, o = _u16(raw, o)
        elif cid == C_SETDEF32:
            cur_def, o = _u32(raw, o)
        elif cid == C_OBJECT:
            _v, o = _u16(raw, o)
            unhandled[cid] = unhandled.get(cid, 0) + 1
        elif cid == C_OBJECT_RANGE:
            o += 4
            unhandled[cid] = unhandled.get(cid, 0) + 1
        elif cid == C_NETWORK_CHAIN:
            n, o = _u8(raw, o)
            o += 2 * n
            unhandled[cid] = unhandled.get(cid, 0) + 1
        elif cid == C_NETWORK_CHAIN_RANGE:
            o += 4
            unhandled[cid] = unhandled.get(cid, 0) + 1
        elif cid == C_NETWORK_CHAIN32:
            n, o = _u8(raw, o)
            o += 4 * n
            unhandled[cid] = unhandled.get(cid, 0) + 1
        elif cid == C_POLYGON:
            # u16 param, u8 count, count x u16 index
            param, o = _u16(raw, o)
            n, o = _u8(raw, o)
            ring = []
            for _ in range(n):
                idx, o = _u16(raw, o)
                ring.append((cur_pool, idx))
            raw_polys.append((cur_def, param, [ring]))
        elif cid == C_POLYGON_RANGE:
            # u16 param, u16 first index, u16 last index + 1
            param, o = _u16(raw, o)
            first, o = _u16(raw, o)
            after, o = _u16(raw, o)
            raw_polys.append((cur_def, param,
                              [[(cur_pool, i) for i in range(first, after)]]))
        elif cid == C_NESTED_POLYGON:
            # u16 param, u8 winding count, then per winding: u8 count + indices
            param, o = _u16(raw, o)
            nw, o = _u8(raw, o)
            rings = []
            for _ in range(nw):
                k, o = _u8(raw, o)
                ring = []
                for _ in range(k):
                    idx, o = _u16(raw, o)
                    ring.append((cur_pool, idx))
                rings.append(ring)
            raw_polys.append((cur_def, param, rings))
        elif cid == C_NESTED_POLYGON_RANGE:
            # u16 param, u8 number of windings, then (windings + 1) x u16 index: each
            # index starts a winding except the last, one past the last point.
            # Checked against the file: count 2 is followed by three indices.
            param, o = _u16(raw, o)
            nw, o = _u8(raw, o)
            starts = []
            for _ in range(nw + 1):
                idx, o = _u16(raw, o)
                starts.append(idx)
            rings = [[(cur_pool, i) for i in range(starts[k], starts[k + 1])]
                     for k in range(max(0, len(starts) - 1))]
            raw_polys.append((cur_def, param, rings))
        elif cid == C_TERRAIN_PATCH:
            close_patch()
            cur_patch = Patch(cur_def, patch_flags, lod_near, lod_far)
        elif cid == C_TERRAIN_PATCH_FLAGS:
            close_patch()
            patch_flags, o = _u8(raw, o)
            cur_patch = Patch(cur_def, patch_flags, lod_near, lod_far)
        elif cid == C_TERRAIN_PATCH_FLAGS_LOD:
            close_patch()
            patch_flags, o = _u8(raw, o)
            lod_near, o = _f32(raw, o)
            lod_far, o = _f32(raw, o)
            cur_patch = Patch(cur_def, patch_flags, lod_near, lod_far)
        elif cid in SIMPLE:
            n, o = _u8(raw, o)
            vs = []
            for _ in range(n):
                idx, o = _u16(raw, o)
                vs.append((cur_pool, idx))
            add_prim(vs, PRIM[cid])
        elif cid in CROSS:
            n, o = _u8(raw, o)
            vs = []
            for _ in range(n):
                pool, o = _u16(raw, o)
                idx, o = _u16(raw, o)
                vs.append((pool, idx))
            add_prim(vs, PRIM[cid])
        elif cid in RANGE:
            i1, o = _u16(raw, o)
            i2, o = _u16(raw, o)
            add_prim([(cur_pool, i) for i in range(i1, i2)], PRIM[cid])
        elif cid == C_COMMENT8:
            n, o = _u8(raw, o)
            o += n
            unhandled[cid] = unhandled.get(cid, 0) + 1
        elif cid == C_COMMENT16:
            n, o = _u16(raw, o)
            o += n
            unhandled[cid] = unhandled.get(cid, 0) + 1
        elif cid == C_COMMENT32:
            n, o = _u32(raw, o)
            o += n
            unhandled[cid] = unhandled.get(cid, 0) + 1
        else:
            raise DSFError(
                f"unknown/unsupported command id {cid} at CMDS offset {o - s - 1}"
            )
        if o > e:
            raise DSFError(f"command {cid} overran CMDS atom")
    close_patch()

    # ------------------------------------------------------------- polygons
    # The definition table holds (class, params) pairs, one per polygon def.
    defs = dsf.defn.get(A_POLY) or []
    for def_i, param, rings in raw_polys:
        # the POLY definition table holds one entry (a .pol/.for/.lin path, or a
        # legacy class name) per definition index
        klass = defs[def_i] if def_i < len(defs) else ""
        poly = Polygon(definition=def_i, klass=klass, param=param)
        for ring in rings:
            pts = []
            for pool_i, vert_i in ring:
                try:
                    v = dsf.pools[pool_i][vert_i]
                except IndexError:
                    continue
                pts.append((float(v[0]), float(v[1])))
            # WED repeats a vertex (a zero-length edge) in most of these polygons and
            # repeats the first point at the end of the ring; both are dropped so the
            # outline, the edges in Blender and the .osm way have no phantom vertices
            clean = []
            for q in pts:
                if not clean or q != clean[-1]:
                    clean.append(q)
            pts = clean
            if len(pts) > 3 and pts[0] == pts[-1]:
                pts.pop()
            if len(pts) >= 3:
                poly.rings.append(pts)
        if poly.rings:
            dsf.polygons.append(poly)


def summary(dsf: DSF):
    ntri = sum(len(p.triangles) for p in dsf.patches)
    return {
        "patches": len(dsf.patches),
        "triangles": ntri,
        "pools": len(dsf.pools),
        "terrain_defs": len(dsf.terrain_defs()),
        "bounds": dsf.bounds(),
        "unhandled": dict(dsf.unhandled),
        "compressed": dsf.was_compressed,
    }


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        d = load_dsf(p)
        print(p, summary(d))