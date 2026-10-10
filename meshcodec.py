"""Reader/writer for Ortho4XP `.mesh` files (both flavours it ever wrote).

The `.mesh` file is Ortho4XP's *intermediate* terrain mesh: `O4_Mesh_Utils.
write_mesh_file()` produces it from the Triangle4XP output and
`O4_Mesh_Utils.read_mesh_file()` (used by `O4_DSF_Utils.build_dsf`) consumes it to
build the DSF that X-Plane actually loads.  X-Plane itself never reads it.

It is a MEDIT-format ASCII triangle mesh::

    MeshVersionFormatted 2
    Dimension 3
    <blank>
    Vertices
    <N>
    <lon> <lat> <z> 0            N times
    <blank>
    Normals
    <N>
    <nx> <ny> 0                  N times
    <blank>
    Triangles
    <T>
    <v1> <v2> <v3> <attr>        T times, 1-based vertex indices

Two flavours exist, and this module reads *and writes* both byte-exactly
----------------------------------------------------------------------------
Measured on real tiles (every field of every line re-formatted and compared):

                        Ortho4XP 1.30 (2023)         1.4 fork ("v2")
  header                `MeshVersionFormatted 1.30`  `MeshVersionFormatted 2`
  vertex fields         `{:.7f} {:.7f} {:.7f} 0`     `{:.15f} {:.15f} {:.15f} 0`
  normal fields         `{:.2f} {:.2f}` (2 columns)  `{:.2f} {:.2f} 0` (3 columns)
  elevation scale       `alt_m = z * 1e5`            identical
  line endings          CRLF                         CRLF
  trailing `End`        absent                       absent

Everything else is identical: absolute WGS84 lon/lat, 1-based indices, one
element per line, a triangle attribute column.  Flavour, line ending and the
per-coordinate precision are detected on read and reproduced on write, so an
untouched tile round-trips byte for byte (both flavours verified: 288 MB v2 tile
and 119 MB 1.30 tile, sha256 equal).

Field notes
-----------
* ``lon``/``lat`` are absolute WGS84 degrees; the writer adds the tile origin.
* ``z`` is the elevation in units of 100000 m: ``alt_m = z * 1e5``.
* the 4th vertex field is a MEDIT "reference"; Ortho4XP always writes ``0``.
* the 3rd normal field: Ortho4XP 1.4 writes a literal ``0``, and 1.30 omits the
  column.  A .mesh normal really only carries the horizontal components ``nx, ny``
  of a mostly-upward normal (X-Plane's DSF stores two components and implies +up);
  the stored third value is *not* a usable 3D normal, so the true one is
  ``(nx, ny, sqrt(1-nx^2-ny^2))`` (see :attr:`OrthoMesh.normal_xyz`).
* ``attr`` is the Triangle4XP region attribute, copied unchanged from the ``.ele``
  file.  It is a bit mask (see :data:`ATTR_NAMES`) and it is *not* off by one: bit
  1 is ``WATER``.  `read_mesh_file` returns it as ``tri_types``.
* ``MeshVersionFormatted`` is not free-form: `build_dsf` branches on it
  (``has_water = 7 if version >= 1.3 else 3``), so 1.30 and 2 differ downstream
  even though the geometry is the same.  Preserving the source version text
  matters when a tile is rebuilt.

Reading is tolerant: sections may appear in any order, wrapped or padded lines,
``End``, blank lines and ``#`` comments are accepted, unknown sections are skipped
and reported, and 2- or 3-column normals are both accepted.
"""

from __future__ import annotations

import io
import os

import numpy as np

__all__ = ["MeshError", "OrthoMesh", "ATTR_NAMES", "attribute_name",
           "read_mesh", "write_mesh", "ALT_SCALE", "FLAVOUR_V2", "FLAVOUR_V13"]

#: divider between the elevation stored in a .mesh file and metres
ALT_SCALE = 100000.0

#: named output flavours for write_mesh(flavour=...) and the Blender exporter
FLAVOUR_V2 = "v2"        # Ortho4XP 1.4 fork: version "2", 15 decimals, 3-col normals
FLAVOUR_V13 = "v13"      # Ortho4XP 1.30: version "1.30", 7 decimals, 2-col normals

#: Triangle4XP / Vector_Map region attributes (O4_Vector_Utils.dico_attributes)
ATTR_NAMES = {
    0: "DUMMY",
    1: "WATER",
    2: "SEA",
    4: "SEA_EQUIV",
    8: "INTERP_ALT",
    16: "RUNWAY",
    32: "TAXIWAY",
    64: "APRON",
    128: "HANGAR",
}

#: order used to pick names for a combined bit mask (most specific first)
_ATTR_ORDER = (128, 64, 32, 16, 8, 4, 2, 1)

#: the per-coordinate format strings seen in the wild, most precise first
_VERT_FMTS = ("{:.15f}", "{:.7f}")


class MeshError(Exception):
    """Raised on a malformed .mesh file."""


def attribute_name(value):
    """Human-readable name for a triangle attribute bit mask."""
    v = int(value)
    if v == 0:
        return "DUMMY"
    parts = [ATTR_NAMES[b] for b in _ATTR_ORDER if v & b]
    return "|".join(parts) if parts else "attr_%d" % v


class OrthoMesh:
    """A parsed .mesh file: geometry in WGS84 plus the extras the format carries.

    All arrays are float64/int64 as parsed, so a read->write cycle is lossless.
    ``tris`` holds 0-based vertex indices (the file stores 1-based).

    Flavour fidelity: ``version_text``, ``vert_fmt`` (per-coordinate format),
    ``normal_cols`` (2 or 3) and ``crlf`` are detected on read and reproduced by
    :func:`write_mesh` unless explicitly overridden.
    """

    __slots__ = ("version", "version_text", "vert_fmt", "normal_cols",
                 "lon", "lat", "alt_m", "nx", "ny", "nz",
                 "tris", "attr", "vtag", "crlf", "unknown_sections", "path")

    def __init__(self, lon, lat, alt_m, tris, attr, version=2.0,
                 nx=None, ny=None, nz=None, vtag=None, crlf=True,
                 unknown_sections=None, path=None, version_text=None,
                 vert_fmt=None, normal_cols=None):
        self.version = float(version)
        self.version_text = (str(version_text) if version_text is not None
                             else "%g" % self.version)
        self.vert_fmt = vert_fmt if vert_fmt in _VERT_FMTS else "{:.15f}"
        self.normal_cols = 3 if normal_cols is None or int(normal_cols) >= 3 else 2
        self.lon = np.asarray(lon, dtype=np.float64)
        self.lat = np.asarray(lat, dtype=np.float64)
        self.alt_m = np.asarray(alt_m, dtype=np.float64)
        self.nx = None if nx is None else np.asarray(nx, dtype=np.float64)
        self.ny = None if ny is None else np.asarray(ny, dtype=np.float64)
        self.nz = None if nz is None else np.asarray(nz, dtype=np.float64)
        self.tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
        self.attr = np.asarray(attr, dtype=np.int64).reshape(-1)
        self.vtag = None if vtag is None else np.asarray(vtag, dtype=np.int64)
        self.crlf = bool(crlf)
        self.unknown_sections = list(unknown_sections or [])
        self.path = path

    # ---------------------------------------------------------------- info
    @property
    def n_vert(self):
        return int(self.lon.size)

    @property
    def n_tri(self):
        return int(self.tris.shape[0])

    @property
    def flavour(self):
        """FLAVOUR_V2 / FLAVOUR_V13 for this file's detected formatting."""
        return (FLAVOUR_V2 if (self.vert_fmt == "{:.15f}" and self.normal_cols == 3)
                else FLAVOUR_V13)

    @property
    def bounds(self):
        """(west, east, south, north) in degrees."""
        return (float(self.lon.min()), float(self.lon.max()),
                float(self.lat.min()), float(self.lat.max()))

    @property
    def normal_xyz(self):
        """Full unit normals (n, 3) reconstructed from nx, ny (or None).

        The file stores only the horizontal components and implies ``+up``: the
        third column (when present at all) is a literal 0 placeholder, so whenever
        it carries no information -- which is what Ortho4XP writes -- the vertical
        component is derived as ``sqrt(1 - nx^2 - ny^2)``.
        """
        if self.nx is None:
            return None
        if self.nz is not None and np.any(self.nz != 0):
            nz = self.nz
        else:
            nz = np.sqrt(np.clip(1.0 - self.nx ** 2 - self.ny ** 2, 0.0, 1.0))
        return np.column_stack((self.nx, self.ny, nz))

    def attribute_counts(self):
        """{attr value: face count}, most frequent first."""
        vals, counts = np.unique(self.attr, return_counts=True)
        order = np.argsort(-counts)
        return {int(vals[i]): int(counts[i]) for i in order}

    def summary(self):
        w, e, s, n = self.bounds
        counts = ", ".join("%s(%d)" % (attribute_name(v), c)
                           for v, c in self.attribute_counts().items())
        norm = ("%d-col normals" % self.normal_cols if self.nx is not None
                else "no normals")
        return ("mesh v%s [%s]: %d vertices, %d triangles, %s, lon %.6f..%.6f "
                "lat %.6f..%.6f, alt %.2f..%.2f m, %s, attrs: %s"
                % (self.version_text, self.flavour, self.n_vert, self.n_tri,
                   norm, w, e, s, n, float(self.alt_m.min()),
                   float(self.alt_m.max()), "CRLF" if self.crlf else "LF",
                   counts))


# --------------------------------------------------------------- reading
class _Scanner:
    """Line/token scanner over an open binary file.

    Line-oriented for the header, batching-friendly for the number blocks, and it
    pushes back anything it reads past the end of a section so the next section
    scan continues where it left off.
    """

    __slots__ = ("f", "toks", "crlf")

    def __init__(self, f):
        self.f = f
        self.toks = []          # tokens read but not consumed (push-back)
        self.crlf = False

    def raw_line(self):
        """Next raw line (bytes), or b'' at EOF."""
        raw = self.f.readline()
        if raw.endswith(b"\r\n"):
            self.crlf = True
        return raw

    def line_tokens(self):
        """Tokens of the next non-empty line, or None at EOF."""
        if self.toks:
            toks, self.toks = self.toks, []
            return toks
        while True:
            raw = self.f.readline()
            if not raw:
                return None
            if raw.endswith(b"\r\n"):
                self.crlf = True
            toks = raw.split()
            if toks:
                return toks

    def count(self, what):
        toks = self.line_tokens()
        if toks is None:
            raise MeshError("unexpected end of file, expected a count after %r"
                            % what)
        try:
            n = int(toks[0])
        except ValueError:
            raise MeshError("expected an element count after %r, got %r"
                            % (what, toks[0]))
        if len(toks) > 1:                    # data sharing the count line
            self.toks.extend(toks[1:])
        return n

    def skip_lines(self, n):
        for _ in range(n):
            if not self.f.readline():
                break

    def numbers(self, n_elem, per, dtype, what):
        """Read exactly ``n_elem`` rows of ``per`` numbers."""
        out = np.empty((n_elem, per), dtype=dtype)
        if n_elem == 0:
            return out
        got = 0
        if self.toks:                        # non-standard input: slow but exact
            got = self._slow(out, n_elem, per, dtype, what, got)
        while got < n_elem:
            want = min(8192, n_elem - got)
            lines = []
            for _ in range(want):
                raw = self.f.readline()
                if not raw:
                    break
                lines.append(raw)
            if not lines:
                raise MeshError("unexpected end of file in the %r section: "
                                "%d of %d elements read" % (what, got, n_elem))
            fields = [ln.split() for ln in lines]
            if len(lines) == want and all(len(f) == per for f in fields):
                # one element per line: convert the whole batch in one go
                flat = b" ".join(lines)
                try:
                    out[got:got + want] = np.array(
                        flat.split(), dtype=dtype).reshape(want, per)
                except ValueError as ex:
                    raise MeshError("bad number in the %r section: %s" % (what, ex))
                got += want
                continue
            # wrapped or padded lines: hand everything to the token path
            for f in fields:
                self.toks.extend(f)
            got = self._slow(out, n_elem, per, dtype, what, got)
        return out

    def numbers_auto(self, n_elem, choices, dtype, what):
        """Read a block whose column count varies by writer (2- or 3-col normals).

        Returns ``(per, first_raw_line, array)``.  The first data line decides
        ``per`` and is handed back so the caller can detect the numeric format from
        the file's own text, then it is consumed as element 0 -- the rest of the
        block still goes through the batched path.
        """
        while True:                          # first non-empty data line
            raw = self.raw_line()
            if not raw:
                raise MeshError("unexpected end of file in the %r section" % what)
            toks = raw.split()
            if toks:
                break
        per = len(toks)
        if per not in choices:
            # wrapped/padded line: fall back to a token-stream read.  The column
            # count is taken from the file when it is at all plausible.
            per = 3 if per > 3 else (choices[-1] if per > 1 else choices[0])
            self.toks = list(toks)
            return per, b"", self.numbers(n_elem, per, dtype, what)
        out = np.empty((n_elem, per), dtype=dtype)
        if n_elem:
            try:
                out[0] = np.array(toks, dtype=dtype)
            except ValueError as ex:
                raise MeshError("bad number in the %r section: %s" % (what, ex))
        if n_elem > 1:
            out[1:] = self.numbers(n_elem - 1, per, dtype, what)
        return per, raw, out

    def _slow(self, out, n_elem, per, dtype, what, got):
        toks = self.toks
        while got < n_elem:
            while len(toks) < per:
                raw = self.f.readline()
                if not raw:
                    raise MeshError("unexpected end of file in the %r section: "
                                    "%d of %d elements read"
                                    % (what, got, n_elem))
                toks.extend(raw.split())
            try:
                out[got] = np.array(toks[:per], dtype=dtype)
            except ValueError as ex:
                raise MeshError("bad number in the %r section: %s" % (what, ex))
            del toks[:per]
            got += 1
        return got


def _probe_vert_fmt(raw, row):
    """Which per-coordinate format string reproduces this vertex line?

    The templates differ in the number of decimals, so any real line picks exactly
    one of them.
    """
    if not raw:
        return None
    fields = raw.decode("latin-1").rstrip("\r\n").split()
    if len(fields) < 3:
        return None
    for fmt in _VERT_FMTS:
        if all(fields[i] == fmt.format(row[i]) for i in range(3)):
            return fmt
    return None


def _first_vertex_line(path):
    """Raw text of the first line of the Vertices block, or b''."""
    with open(path, "rb") as f:
        for _ in range(4):                   # header, dimension, blank, 'Vertices'
            f.readline()
        count = f.readline()
        if not count:
            return b""
        return f.readline()


def read_mesh(path, want_normals=True):
    """Parse an Ortho4XP .mesh file into an :class:`OrthoMesh`."""
    if not os.path.isfile(path):
        raise MeshError("no such file: %s" % path)
    version = None
    version_text = None
    normal_cols = None
    lon = lat = alt = vtag = None
    nx = ny = nz = None
    tris = attr = None
    unknown = []

    with open(path, "rb") as f:
        sc = _Scanner(f)
        first = True
        while True:
            toks = sc.line_tokens()
            if toks is None:
                break
            key = toks[0].decode("latin-1")
            kl = key.lower()
            if kl.startswith("meshversionformatted"):
                if len(toks) < 2:
                    raise MeshError("bad MeshVersionFormatted line")
                version_text = toks[1].decode("latin-1")
                try:
                    version = float(version_text)
                except ValueError:
                    raise MeshError("bad MeshVersionFormatted value %r"
                                    % version_text)
                first = False
                continue
            if first:
                raise MeshError("not a MEDIT mesh file (first line is %r)" % key)
            if kl == "end" or kl.startswith("#"):
                continue
            if kl in ("vertices", "vertex"):
                n = sc.count(kl)
                data = sc.numbers(n, 4, np.float64, kl)
                lon, lat = data[:, 0], data[:, 1]
                alt = data[:, 2] * ALT_SCALE
                vtag = data[:, 3].astype(np.int64)
            elif kl == "normals":
                n = sc.count(kl)
                per, _raw, data = sc.numbers_auto(n, (2, 3), np.float64, kl)
                normal_cols = per
                nx, ny = data[:, 0], data[:, 1]
                if per >= 3:
                    nz = data[:, 2]
            elif kl in ("triangles", "triangle"):
                n = sc.count(kl)
                data = sc.numbers(n, 4, np.int64, kl)
                tri = data[:, :3]
                if tri.size and tri.min() < 1:
                    raise MeshError("triangle indices are not 1-based "
                                    "(minimum is %d)" % tri.min())
                tris = tri - 1
                attr = data[:, 3]
            elif kl == "dimension":
                continue
            else:
                unknown.append(key)
                sc.skip_lines(sc.count(kl))

    if version is None:
        raise MeshError("missing MeshVersionFormatted line")
    if lon is None:
        raise MeshError("missing Vertices section")
    if tris is None:
        raise MeshError("missing Triangles section")
    if tris.size and tris.max() >= lon.size:
        raise MeshError("triangle references vertex %d but the file has %d"
                        % (tris.max() + 1, lon.size))
    if nx is not None and nx.size != lon.size:
        raise MeshError("normals count %d != vertex count %d"
                        % (nx.size, lon.size))
    if vtag is not None and vtag.size != lon.size:
        raise MeshError("vertex tag count %d != vertex count %d"
                        % (vtag.size, lon.size))
    if attr is None:
        attr = np.zeros(tris.shape[0], dtype=np.int64)
    if attr.size != tris.shape[0]:
        raise MeshError("triangle attribute count %d != triangle count %d"
                        % (attr.size, tris.shape[0]))
    if not want_normals:
        nx = ny = nz = None

    # per-coordinate precision, probed from the file's own text
    vert_fmt = None
    if lon.size:
        vert_fmt = _probe_vert_fmt(_first_vertex_line(path),
                                   (lon[0], lat[0], alt[0] / ALT_SCALE))
    if vert_fmt is None:
        vert_fmt = "{:.15f}" if (version or 0) >= 1.4 else "{:.7f}"
    if normal_cols is None:
        normal_cols = 3

    return OrthoMesh(lon, lat, alt, tris, attr, version=version,
                     nx=nx, ny=ny, nz=nz, vtag=vtag, crlf=sc.crlf,
                     unknown_sections=unknown, path=path,
                     version_text=version_text, vert_fmt=vert_fmt,
                     normal_cols=normal_cols)


# --------------------------------------------------------------- writing
def _pct(fmt):
    """numpy.savetxt needs %-style when a format list mixes text and integers."""
    if fmt.startswith("{"):
        return fmt.replace("{:", "%").replace("f}", "f")
    return fmt


def _fmt_block(cols, fmts, lf, chunk=250000):
    """Format columns as text lines with the given line ending."""
    n = cols[0].size
    fmts = [_pct(f) for f in fmts]
    buf = io.StringIO()
    for start in range(0, n, chunk):
        stop = min(start + chunk, n)
        part = np.column_stack([np.asarray(c, dtype=np.float64)[start:stop]
                                for c in cols])
        np.savetxt(buf, part, fmt=fmts, delimiter=" ")
    text = buf.getvalue()
    return text if lf == "\n" else text.replace("\n", lf)


def _fmt_normals(nx, ny, nz, lf, cols):
    """'%.2f %.2f' lines (1.30) or '%.2f %.2f 0' lines (1.4 writes a literal 0)."""
    n = nx.size
    if n == 0:
        return ""
    if cols == 2:
        return _fmt_block((nx, ny), ["%.2f", "%.2f"], lf)
    if np.all(nz == 0):
        return _fmt_block((nx, ny, np.zeros(n)), ["%.2f", "%.2f", "%d"], lf)
    buf = io.StringIO()                      # rare: real third components
    for start in range(0, n, 250000):
        stop = min(start + 250000, n)
        for i in range(start, stop):
            z = nz[i]
            buf.write("%.2f %.2f %s%s"
                      % (nx[i], ny[i], "0" if z == 0 else "%.2f" % z, lf))
    return buf.getvalue()


def write_mesh(path, mesh, version=None, crlf=None, vtag=None, flavour=None):
    """Write an :class:`OrthoMesh` back out; returns the byte count.

    By default the source flavour (version string, coordinate precision, normal
    column count, line ending) is reproduced, which makes an untouched tile
    byte-identical to its input.  ``version``/``flavour``/``crlf`` override that.

    ``flavour`` is one of :data:`FLAVOUR_V2` (``MeshVersionFormatted 2``, 15
    decimals, 3-column normals) or :data:`FLAVOUR_V13` (``1.30``, 7 decimals,
    2-column normals).
    """
    lon = np.asarray(mesh.lon, dtype=np.float64)
    lat = np.asarray(mesh.lat, dtype=np.float64)
    alt = np.asarray(mesh.alt_m, dtype=np.float64)
    tris = np.asarray(mesh.tris, dtype=np.int64).reshape(-1, 3)
    attr = np.asarray(mesh.attr, dtype=np.int64).reshape(-1)
    n_v, n_t = lon.size, tris.shape[0]

    if flavour == FLAVOUR_V2:
        vert_fmt, normal_cols, ver_txt = "{:.15f}", 3, "2"
    elif flavour == FLAVOUR_V13:
        vert_fmt, normal_cols, ver_txt = "{:.7f}", 2, "1.30"
    else:
        vert_fmt = mesh.vert_fmt
        normal_cols = mesh.normal_cols
        ver_txt = mesh.version_text
    if version is not None:
        ver_txt = "%g" % float(version)

    lf = "\r\n" if (mesh.crlf if crlf is None else crlf) else "\n"
    tags = (np.zeros(n_v, dtype=np.int64) if vtag is None
            else np.asarray(vtag, dtype=np.int64).reshape(-1))

    out = io.StringIO()
    out.write("MeshVersionFormatted %s%s" % (ver_txt, lf))
    out.write("Dimension 3" + lf + lf)
    out.write("Vertices" + lf)
    out.write("%d%s" % (n_v, lf))
    out.write(_fmt_block((lon, lat, alt / ALT_SCALE, tags),
                         [vert_fmt, vert_fmt, vert_fmt, "%d"], lf))
    out.write(lf)

    if mesh.nx is not None:
        nx = np.asarray(mesh.nx, dtype=np.float64).reshape(-1)
        ny = np.asarray(mesh.ny, dtype=np.float64).reshape(-1)
        nz = (np.zeros(nx.size) if mesh.nz is None
              else np.asarray(mesh.nz, dtype=np.float64).reshape(-1))
        out.write("Normals" + lf)
        out.write("%d%s" % (nx.size, lf))
        out.write(_fmt_normals(nx, ny, nz, lf, normal_cols))
        out.write(lf)

    out.write("Triangles" + lf)
    out.write("%d%s" % (n_t, lf))
    if n_t:
        out.write(_fmt_block((tris[:, 0] + 1, tris[:, 1] + 1, tris[:, 2] + 1,
                              attr), ["%d", "%d", "%d", "%d"], lf))

    text = out.getvalue()
    out.close()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="ascii", newline="") as f:
        f.write(text)
    os.replace(tmp, path)
    return len(text)