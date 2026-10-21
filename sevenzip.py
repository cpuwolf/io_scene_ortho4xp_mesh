"""Minimal 7z reader -- pure Python, no subprocess, no third-party module.

X-Plane ships its Global Scenery `.dsf` files as bare 7z archives (the file *is* a
7z container holding one `.dsf`), so reading them inside an add-on used to mean
shelling out to `bsdtar` on macOS.  This module does the decode with the standard
library only: the 7z container is parsed here, and the actual decompression is
`lzma`'s raw mode, which supports exactly the filter chain 7z is built on
(LZMA / LZMA2, optionally preceded by the x86 BCJ or delta filter).

Scope, deliberately narrow:
  * single-file archives (the DSF case), one folder / one pack stream;
  * coders: Copy, LZMA, LZMA2, x86 BCJ, delta (the last three via `lzma`);
  * anything else raises `SevenZipError` instead of guessing.

The uncompressed payload is CRC-checked when the archive carries a CRC.
"""

import lzma
import struct
import zlib

MAGIC = b"7z\xbc\xaf\x27\x1c"

# 7z property ids
K_END = 0x00
K_HEADER = 0x01
K_ARCHIVE_INFO = 0x02
K_MAIN_STREAMS = 0x04
K_FILES_INFO = 0x05
K_PACK_INFO = 0x06
K_UNPACK_INFO = 0x07
K_SUBSTREAMS_INFO = 0x08
K_SIZE = 0x09
K_CRC = 0x0A
K_FOLDER = 0x0B
K_CODERS_UNPACK_SIZE = 0x0C
K_NUM_UNPACK_STREAM = 0x0D
K_EMPTY_STREAM = 0x0E
K_EMPTY_FILE = 0x0F
K_ANTI = 0x10
K_NAME = 0x11
K_DUMMY = 0x19
K_ENCODED_HEADER = 0x17

# coder ids
METHOD_COPY = b"\x00"
METHOD_LZMA = b"\x03\x01\x01"
METHOD_LZMA2 = b"\x21"
METHOD_BCJ_X86 = b"\x03\x03\x01\x03"
METHOD_DELTA = b"\x03"


class SevenZipError(Exception):
    pass


def _num(buf, pos):
    """7z variable-length integer."""
    first = buf[pos]
    mask = 0x80
    value = 0
    for i in range(8):
        if not first & mask:
            value |= (first & (mask - 1)) << (8 * i)
            return value, pos + 1 + i       # the masked byte plus the i raw ones
        value |= buf[pos + 1 + i] << (8 * i)
        mask >>= 1
    return value, pos + 9


def _bits(buf, pos, count):
    """7z 'bit vector' with the all-ones shortcut, returned as a list of bools."""
    if count == 0:
        return [], pos
    b = buf[pos]
    if b == 0xFF:                                   # "all defined" marker
        head = buf[pos + 1: pos + 1 + (count + 7) // 8]
        out = [bool((head[i >> 3] >> (i & 7)) & 1) for i in range(count)]
        return out, pos + 1 + len(head)
    out = [bool((b >> i) & 1) for i in range(8)][:count]
    return out, pos + 1


def _lzma_dict_size_lzma2(prop):
    if prop > 40:
        raise SevenZipError("bad LZMA2 dictionary property %d" % prop)
    if prop == 40:
        return 0xFFFFFFFF
    return (2 | (prop & 1)) << (prop // 2 + 11)


class _Folder:
    __slots__ = ("coders", "unpack_sizes", "crc", "num_unpack_streams", "sub_sizes",
                 "sub_crcs")

    def __init__(self):
        self.coders = []            # [(method_id, props)]
        self.unpack_sizes = []      # per out-stream
        self.crc = None
        self.num_unpack_streams = 1
        self.sub_sizes = []
        self.sub_crcs = []


def _parse_pack_info(buf, pos, out):
    pos += 1                                        # kPackInfo
    pack_pos, pos = _num(buf, pos)
    num_pack, pos = _num(buf, pos)
    out["pack_pos"] = pack_pos
    out["pack_sizes"] = []
    while buf[pos] != K_END:
        pid = buf[pos]
        if pid == K_SIZE:
            pos += 1
            for _ in range(num_pack):
                size, pos = _num(buf, pos)
                out["pack_sizes"].append(size)
        elif pid == K_CRC:
            pos += 1
            defined, pos = _bits(buf, pos, num_pack)
            out["pack_crcs"] = []
            for i in range(num_pack):
                crc = None
                if defined[i]:
                    crc = struct.unpack_from("<I", buf, pos)[0]
                    pos += 4
                out["pack_crcs"].append(crc)
        else:
            raise SevenZipError("unexpected id 0x%02x in kPackInfo" % pid)
    return pos + 1


def _parse_folder(buf, pos):
    folder = _Folder()
    num_coders, pos = _num(buf, pos)
    out_streams = 0
    in_streams = 0
    for _ in range(num_coders):
        flags = buf[pos]
        pos += 1
        id_size = flags & 0x0F
        method = bytes(buf[pos:pos + id_size])
        pos += id_size
        if flags & 0x10:
            raise SevenZipError("multi-coder chains are not supported")
        props = b""
        if flags & 0x20:
            props_size, pos = _num(buf, pos)
            props = bytes(buf[pos:pos + props_size])
            pos += props_size
        out_streams += 1
        in_streams += 1
        folder.coders.append((method, props))
    if num_coders > 1:
        # bind pairs: decode order is already linear here, 7z lists them explicitly
        for _ in range(out_streams - 1):
            _, pos = _num(buf, pos)                  # in index
            _, pos = _num(buf, pos)                  # out index
    return folder, pos


def _parse_unpack_info(buf, pos, out):
    pos += 1                                        # kUnpackInfo
    if buf[pos] != K_FOLDER:
        raise SevenZipError("expected kFolder in kUnpackInfo")
    pos += 1
    num_folders, pos = _num(buf, pos)
    external = buf[pos]
    pos += 1
    if external:
        raise SevenZipError("external folder definitions are not supported")
    folders = []
    for _ in range(num_folders):
        folder, pos = _parse_folder(buf, pos)
        folders.append(folder)
    if buf[pos] != K_CODERS_UNPACK_SIZE:
        raise SevenZipError("expected kCodersUnpackSize in kUnpackInfo")
    pos += 1
    for folder in folders:
        for _ in range(len(folder.coders)):
            size, pos = _num(buf, pos)
            folder.unpack_sizes.append(size)
    while buf[pos] != K_END:
        pid = buf[pos]
        if pid == K_CRC:
            pos += 1
            defined, pos = _bits(buf, pos, num_folders)
            for i, folder in enumerate(folders):
                if defined[i]:
                    folder.crc = struct.unpack_from("<I", buf, pos)[0]
                    pos += 4
        else:
            raise SevenZipError("unexpected id 0x%02x in kUnpackInfo" % pid)
    out["folders"] = folders
    return pos + 1


def _parse_substreams_info(buf, pos, folders):
    pos += 1                                        # kSubStreamsInfo
    while buf[pos] != K_END:
        pid = buf[pos]
        if pid == K_NUM_UNPACK_STREAM:
            pos += 1
            for folder in folders:
                folder.num_unpack_streams, pos = _num(buf, pos)
        elif pid == K_SIZE:
            pos += 1
            for folder in folders:
                folder.sub_sizes = []
                for _ in range(folder.num_unpack_streams - 1):
                    size, pos = _num(buf, pos)
                    folder.sub_sizes.append(size)
                total = folder.unpack_sizes[-1]
                folder.sub_sizes.append(total - sum(folder.sub_sizes))
        elif pid == K_CRC:
            pos += 1
            defined, pos = _bits(buf, pos, sum(f.num_unpack_streams for f in folders))
            for folder in folders:
                folder.sub_crcs = []
                for _ in range(folder.num_unpack_streams):
                    folder.sub_crcs.append(
                        struct.unpack_from("<I", buf, pos)[0] if defined.pop(0) else None)
                    if folder.sub_crcs[-1] is not None:
                        pos += 4
        else:
            raise SevenZipError("unexpected id 0x%02x in kSubStreamsInfo" % pid)
    for folder in folders:
        if not folder.sub_sizes:
            folder.sub_sizes = [folder.unpack_sizes[-1]]
    return pos + 1


def _parse_files_info(buf, pos, out):
    pos += 1                                        # kFilesInfo
    num_files, pos = _num(buf, pos)
    names: list = [None] * num_files
    while buf[pos] != K_END:
        prop = buf[pos]
        pos += 1
        size, pos = _num(buf, pos)
        end = pos + size
        if prop == K_NAME:
            external = buf[pos]
            pos += 1
            if external:
                raise SevenZipError("external file names are not supported")
            for i in range(num_files):
                start = pos
                while buf[pos:pos + 2] != b"\x00\x00":
                    pos += 2
                name = bytes(buf[start:pos]).decode("utf-16-le")
                pos += 2
                names[i] = name
        # every other property is skipped by its own length: this reader needs
        # names and sizes only
        pos = end
    out["names"] = names
    return pos + 1


def _parse_header(buf, pos=0):
    out = {"pack_pos": 0, "pack_sizes": [], "folders": [], "names": []}
    if buf[pos] != K_HEADER:
        raise SevenZipError("not a plain 7z header (id 0x%02x)" % buf[pos])
    pos += 1
    while buf[pos] != K_END:
        pid = buf[pos]
        if pid == K_ARCHIVE_INFO:
            raise SevenZipError("archive properties are not supported")
        elif pid == K_MAIN_STREAMS:
            pos += 1
        elif pid == K_PACK_INFO:
            pos = _parse_pack_info(buf, pos, out)
        elif pid == K_UNPACK_INFO:
            pos = _parse_unpack_info(buf, pos, out)
        elif pid == K_SUBSTREAMS_INFO:
            pos = _parse_substreams_info(buf, pos, out["folders"])
        elif pid == K_FILES_INFO:
            pos = _parse_files_info(buf, pos, out)
        else:
            raise SevenZipError("unexpected id 0x%02x in kHeader" % pid)
    return out, pos + 1


def _coder_to_lzma_filter(method, props):
    if method == METHOD_LZMA:
        if len(props) != 5:
            raise SevenZipError("LZMA coder with %d property bytes" % len(props))
        d = props[0]
        lc = d % 9
        rem = d // 9
        lp = rem % 5
        pb = rem // 5
        dict_size = struct.unpack_from("<I", props, 1)[0]
        return {"id": lzma.FILTER_LZMA1, "dict_size": dict_size,
                "lc": lc, "lp": lp, "pb": pb}
    if method == METHOD_LZMA2:
        return {"id": lzma.FILTER_LZMA2,
                "dict_size": _lzma_dict_size_lzma2(props[0] if props else 0)}
    if method == METHOD_BCJ_X86:
        start = struct.unpack_from("<I", props, 0)[0] if len(props) == 4 else 0
        return {"id": lzma.FILTER_X86, "start_offset": start}
    if method == METHOD_DELTA:
        dist = (props[0] + 1) if props else 1
        return {"id": lzma.FILTER_DELTA, "dist": dist}
    raise SevenZipError("unsupported coder %s" % method.hex())


def _decode_folder(body, folder, pack_offset, pack_size):
    coders = folder.coders
    if coders and coders[0][0] == METHOD_COPY and len(coders) == 1:
        return bytes(body[pack_offset:pack_offset + pack_size])
    filters = [_coder_to_lzma_filter(m, p) for m, p in coders]
    if filters[-1]["id"] not in (lzma.FILTER_LZMA1, lzma.FILTER_LZMA2):
        raise SevenZipError("the last coder of the chain must be LZMA/LZMA2")
    dec = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
    try:
        data = dec.decompress(bytes(body[pack_offset:pack_offset + pack_size]),
                              max_length=folder.unpack_sizes[-1] or -1)
    except lzma.LZMAError as ex:
        raise SevenZipError("7z stream will not decode: %s" % ex) from ex
    want = folder.unpack_sizes[-1]
    if want and len(data) != want:
        raise SevenZipError("decoded %d bytes, header says %d" % (len(data), want))
    return data


def load(path):
    """Return ``(name, payload)`` for a single-file 7z archive."""
    with open(path, "rb") as fh:
        head = fh.read(32)
        if len(head) < 32 or head[:6] != MAGIC:
            raise SevenZipError("%s is not a 7z archive" % path)
        nh_offset, nh_size = struct.unpack_from("<QQ", head, 12)
        nh_crc = struct.unpack_from("<I", head, 28)[0]
        fh.seek(32 + nh_offset)
        next_header = fh.read(nh_size)
        fh.seek(0)
        body = fh.read(32 + nh_offset)
        if len(next_header) != nh_size:
            raise SevenZipError("%s is truncated" % path)
        if zlib.crc32(next_header) & 0xFFFFFFFF != nh_crc:
            raise SevenZipError("%s: next header CRC mismatch" % path)

        info = None
        if next_header[:1] == b"\x01":
            info, _ = _parse_header(next_header)
        elif next_header[:1] == b"\x17":
            # the header itself is a compressed stream: decode it first
            encoded, _ = _parse_header(b"\x01" + next_header[1:])
            folders = encoded["folders"]
            if len(folders) != 1:
                raise SevenZipError("encoded header with %d folders" % len(folders))
            offset = 32 + encoded["pack_pos"]
            raw = _decode_folder(body, folders[0], offset, encoded["pack_sizes"][0])
            info, _ = _parse_header(raw)
        else:
            raise SevenZipError("%s: unknown next-header id 0x%02x"
                                % (path, next_header[0]))

    folders = info["folders"]
    if len(folders) != 1:
        raise SevenZipError("%s: %d folders (a DSF holds one stream)"
                            % (path, len(folders)))
    folder = folders[0]
    if folder.num_unpack_streams != 1:
        raise SevenZipError("%s: %d substreams" % (path, folder.num_unpack_streams))
    offset = 32 + info["pack_pos"]
    data = _decode_folder(body, folder, offset, info["pack_sizes"][0])
    if folder.crc is not None and zlib.crc32(data) & 0xFFFFFFFF != folder.crc:
        raise SevenZipError("%s: payload CRC mismatch" % path)
    name = info["names"][0] if info["names"] else ""
    return name, data
