"""
tercodec.py -- X-Plane terrain type (.ter) parsing and texture resolution.

Pure stdlib, no bpy dependency, so it can be unit-tested outside Blender.

Format
------
A .ter file is text: standard X-Plane 3-line header then one command per line.

    A
    800
    TERRAIN

    BASE_TEX ../textures10/soil/rock_vcld_rain_flat_c.dds
    BORDER_TEX ../textures10/border/med.png
    PROJECTED 2421 2421
    NO_ALPHA
    COMPOSITE_BORDERS

The published spec (developer.x-plane.com, last touched 2018) is incomplete and
in places wrong about real files shipped with X-Plane 12. Measured across the
9848 .ter files in Resources/default scenery/1000 world terrain:

  * directives that exist in files but are NOT in the spec: DECAL_LIB (7442
    files), SUPER_ROUGHNESS (6960), NORMAL_TEX (484), TEXTURE_TILE (730),
    TEXTURE_NORMAL, TEXTURE_LIT, LIT_FADE_PARAMS, LUMINANCE, AUTO_SLOPE /
    AUTO_SLOPE_HILL / AUTO_SLOPE_CLIFF / AUTO_SLOPE_HEADING, AUTO_HEADING,
    COMPOSITE_PARAMS, COMPOSITE_NOISE, COMPOSITE_PROJECTED, BUMP_LEVEL,
    NORMAL_METALNESS, FAR, AUTO_VARY
  * TEXTURE_LIT is used as well as the documented LIT_TEX, and TEXTURE_NORMAL as
    well as NORMAL_TEX
  * several directives put NUMBERS BEFORE the filename, with comma decimal
    separators:  NORMAL_TEX 20,000000 <file> / FAR 5000 20000 0.0625 <file> /
    AUTO_SLOPE_HILL 2762 1381 30 35 <file>
  * files are indented with TABS even though the spec says "spaces and not tabs"
  * `#` starts a comment

Consequence: never assume the filename is token[1]. Scan each line for the token
that carries a known image extension.

Textures are normally relative to the .ter file's own directory. X-Plane 12 ships
only .dds for many assets while the .ter still names .png, so resolution falls
back across extensions.
"""

import os

IMG_EXTS = (".png", ".dds", ".bmp", ".jpg", ".jpeg", ".tga", ".tif", ".tiff")

# directive -> role. Roles drive how the addon wires a Blender material.
ROLE_OF = {
    "BASE_TEX": "base",
    "BASE_TEX_NOWRAP": "base",
    "TEXTURE_BASE": "base",
    "LIT_TEX": "lit",
    "LIT_TEX_NOWRAP": "lit",
    "TEXTURE_LIT": "lit",
    "COMPOSITE_TEX": "composite",
    "COMPOSITE_NOWRAP": "composite",
    "TEXTURE_COMPOSITE": "composite",
    "BORDER_TEX": "border",
    "BORDER_TEX_WRAP": "border",
    "NORMAL_TEX": "normal",
    "TEXTURE_NORMAL": "normal",
    "COMPOSITE_NOISE": "noise",
    "AUTO_SLOPE_HILL": "slope_hill",
    "AUTO_SLOPE_CLIFF": "slope_cliff",
    "FAR": "far",
    "DECAL_LIB": "decal",
}

# terrain types X-Plane resolves internally; there is no file on disk for these
BUILTIN_TYPES = {"terrain_water"}

_HEADER_WORDS = ("TERRAIN",)


class TerError(Exception):
    pass


def _norm_seps(s):
    """X-Plane allows ':' '\\' or '/' as directory separators."""
    return s.replace("\\", "/").replace(":", "/")


def _is_image(tok):
    return os.path.splitext(tok)[1].lower() in IMG_EXTS


def _img_token(tokens):
    """First token that looks like an image path (never assume a position)."""
    for t in tokens:
        if _is_image(t):
            return t
    return None


class TerFile:
    """A parsed .ter file."""

    __slots__ = ("path", "letter", "version", "kind", "directives", "textures")

    def __init__(self, path, letter="", version="", kind="", directives=None,
                 textures=None):
        self.path = path
        self.letter = letter
        self.version = version
        self.kind = kind
        self.directives = directives or []      # [(name, [tokens...]), ...] raw
        self.textures = textures or {}          # role -> relative path

    # -- convenience -----------------------------------------------------
    @property
    def directory(self):
        return os.path.dirname(self.path)

    def directive(self, name):
        for n, toks in self.directives:
            if n == name:
                return toks
        return None

    def params(self, name, count):
        """Leading numeric params of a directive, comma decimals allowed."""
        toks = self.directive(name)
        if not toks:
            return None
        out = []
        for t in toks[1:count + 1]:
            try:
                out.append(float(t.replace(",", ".")))
            except ValueError:
                return None
        return out or None

    def has(self, name):
        return any(n == name for n, _ in self.directives)

    def texture_abs(self, role):
        rel = self.textures.get(role)
        return resolve_texture(self.directory, rel) if rel else None

    def __repr__(self):
        return (f"<TerFile {os.path.basename(self.path)} roles="
                f"{sorted(self.textures)} dirs={len(self.directives)}>")


def parse_ter(path):
    """Parse a .ter file. Raises TerError on a malformed header."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = fh.read()
    except OSError as ex:
        raise TerError(f"cannot read {path}: {ex}") from ex

    lines = raw.splitlines()
    head = []
    i = 0
    while i < len(lines) and len(head) < 3:
        s = lines[i].strip()
        i += 1
        if not s or s.startswith("#"):
            continue
        head.append(s)
    if len(head) < 3:
        raise TerError(f"{path}: truncated header")
    letter, version, kind = head[0], head[1], head[2].split()[0].upper()
    if kind not in _HEADER_WORDS:
        raise TerError(f"{path}: not a TERRAIN file (got {kind!r})")

    directives = []
    textures = {}
    for line in lines[i:]:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.split()
        name = tokens[0].upper()
        directives.append((name, tokens))
        img = _img_token(tokens)
        if img:
            role = ROLE_OF.get(name, "other")
            # keep the first occurrence for a role; specifics vary by file
            textures.setdefault(role, _norm_seps(img))

    return TerFile(path, letter, version, kind, directives, textures)


# --------------------------------------------------------------- resolution
def _candidate_names(rel):
    """The path as given, then the same stem in the other shipped formats."""
    stem, ext = os.path.splitext(rel)
    yield rel
    if ext.lower() != ".dds":
        yield stem + ".dds"
    if ext.lower() != ".png":
        yield stem + ".png"
    if ext.lower() not in (".bmp",):
        yield stem + ".bmp"


def resolve_texture(directory, rel):
    """Resolve a .ter texture reference to an absolute path, or None."""
    if not rel:
        return None
    rel = _norm_seps(rel)
    base = None
    if rel.lower().startswith("lib/"):
        # library reference: caller should have expanded it; try as-is below
        base = rel
    for cand in _candidate_names(rel):
        p = os.path.normpath(os.path.join(directory, cand))
        if os.path.isfile(p):
            return p
    return base if base else None


def find_default_scenery(xp_root):
    d = os.path.join(xp_root, "Resources", "default scenery")
    return d if os.path.isdir(d) else None


def xplane_root_from(path):
    """Walk up from a DSF/package path looking for an X-Plane install root."""
    cur = os.path.abspath(path)
    for _ in range(12):
        if os.path.isdir(os.path.join(cur, "Resources", "default scenery")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def library_prefixes(xp_root):
    """Map 'lib/<name>/' -> [candidate directories], by scanning library.txt files.

    Several library.txt files mention the same prefix (e.g. lib/g10/ appears in
    both '1000 autogen' and '1000 world terrain'), so a prefix maps to a LIST of
    candidates and resolution picks whichever one actually holds the file. Taking
    the first match would send lib/g10/... to 1000 autogen, where nothing exists.
    """
    out = {}
    d = find_default_scenery(xp_root) if xp_root else None
    if not d:
        return out
    for entry in sorted(os.listdir(d)):
        sub = os.path.join(d, entry)
        lib = os.path.join(sub, "library.txt")
        if not (os.path.isdir(sub) and os.path.isfile(lib)):
            continue
        try:
            with open(lib, encoding="utf-8", errors="replace") as fh:
                head = fh.read(262144)
        except OSError:
            continue
        for line in head.replace("\t", " ").replace("\\", "/").splitlines():
            for tok in line.split():
                low = tok.lower()
                if not low.startswith("lib/"):
                    continue
                # lib/g10/terrain10/foo.ter -> prefix 'lib/g10/'
                parts = low.split("/")
                if len(parts) >= 3 and parts[1]:
                    cands = out.setdefault("lib/" + parts[1] + "/", [])
                    if sub not in cands:
                        cands.append(sub)
    return out


def uv_from_center(lat, lon, center_lat, center_lon, size_m):
    """UV for a texture placed ONCE over its footprint (LOAD_CENTER, no PROJECTED).

    Third-party ortho scenery (Ortho4XP) ships one orthophoto per terrain type.
    LOAD_CENTER names the texture's centre plus the footprint it covers, and
    BASE_TEX_NOWRAP means it is mapped once rather than tiled -- so the centre
    lands on (0.5, 0.5) and the footprint edges on 0 and 1. The shipped X-Plane
    library never uses this; it always projects from position/scale instead.

    Uses a spherical metres-per-degree; over one tile the error is negligible
    next to an orthophoto's own ground resolution.
    """
    import math
    m_per_deg_lat = 111132.92
    m_per_deg_lon = 111412.84 * math.cos(math.radians(center_lat))
    east = (lon - center_lon) * m_per_deg_lon
    north = (lat - center_lat) * m_per_deg_lat
    if not size_m:
        return None
    return (0.5 + east / size_m, 0.5 + north / size_m)


def package_root_from(dsf_path):
    """The scenery package root: the parent of 'Earth nav data'."""
    d = os.path.dirname(os.path.abspath(dsf_path))
    for _ in range(6):
        if os.path.basename(d).lower() == "earth nav data":
            return os.path.dirname(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return os.path.dirname(os.path.abspath(dsf_path))


class TerResolver:
    """Resolves DSF terrain-definition entries to .ter files and textures."""

    def __init__(self, dsf_path, xp_root=None, extra_roots=()):
        self.pkg_root = package_root_from(dsf_path)
        self.xp_root = xp_root or xplane_root_from(dsf_path)
        self.prefixes = library_prefixes(self.xp_root)
        self.extra_roots = [r for r in extra_roots if r]

    def candidate_paths(self, entry):
        """Ordered candidate locations for a TERT/DEFN entry."""
        e = _norm_seps(entry.strip())
        low = e.lower()
        out = []

        # lib/... reference: try every library that mentions this prefix
        for pre, roots in self.prefixes.items():
            if low.startswith(pre):
                out += [os.path.join(r, e[len(pre):]) for r in roots]
        if low.startswith("lib/"):
            out += [os.path.join(r, e[4:]) for r in self.extra_roots]

        # package-relative, then extra roots, then all library roots
        out.append(os.path.join(self.pkg_root, e))
        out += [os.path.join(r, e) for r in self.extra_roots]
        for roots in self.prefixes.values():
            out += [os.path.join(r, e) for r in roots]
        return out

    def resolve(self, entry):
        """Return (abs_path, kind) where kind is 'ter', 'image', 'builtin' or None."""
        e = _norm_seps(entry.strip())
        low = e.lower()
        ext = os.path.splitext(low)[1]
        if low in BUILTIN_TYPES:
            return None, "builtin"
        for cand in self.candidate_paths(e):
            if ext == ".ter":
                if os.path.isfile(cand):
                    return os.path.normpath(cand), "ter"
            else:
                for c in _candidate_names(cand):
                    if os.path.isfile(c):
                        return os.path.normpath(c), "image"
                if os.path.isfile(cand):
                    return os.path.normpath(cand), "image"
        return None, None

    def load(self, entry):
        """Resolve and parse in one step. Returns (TerFile|None, kind, texture)."""
        path, kind = self.resolve(entry)
        if kind == "ter":
            return parse_ter(path), kind, None
        if kind == "image":
            return None, kind, path
        return None, kind, None