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


@dataclass
class Patch:
    """One terrain patch: triangles sharing terrain definition / LOD / flags."""
    definition: int = 0
    flags: int = 0
    lod_near: float = 0.0
    lod_far: float = 0.0
    triangles: list = field(default_factory=list)  # each: ((pool, idx) x3)


@dataclass
class DSF:
    props: list = field(default_factory=list)          # ordered [(k, v), ...]
    defn: dict = field(default_factory=dict)           # atom name -> [paths]
    pools: list = field(default_factory=list)          # decoded vertex tuples
    pool_scales: list = field(default_factory=list)    # per pool: [(scale,offset)]
    patches: list = field(default_factory=list)        # Patch objects
    unhandled: dict = field(default_factory=dict)      # cmd id -> count
    source_path: str | None = None
    was_compressed: bool = False

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

    geod = cmds = None
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
            n = raw[o + 2]
            o += 3 + 2 * n
            unhandled[cid] = unhandled.get(cid, 0) + 1
        elif cid == C_POLYGON_RANGE:
            o += 6
            unhandled[cid] = unhandled.get(cid, 0) + 1
        elif cid == C_NESTED_POLYGON:
            nw = raw[o + 2]
            o += 3
            for _ in range(nw):
                k = raw[o]
                o += 1 + 2 * k
            unhandled[cid] = unhandled.get(cid, 0) + 1
        elif cid == C_NESTED_POLYGON_RANGE:
            n = raw[o + 2]
            o += 5 + 2 * n           # u16 param, u8 count, u16 index1, N*u16
            unhandled[cid] = unhandled.get(cid, 0) + 1
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