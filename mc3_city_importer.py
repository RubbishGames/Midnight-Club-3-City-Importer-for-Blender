# SPDX-License-Identifier: GPL-3.0-or-later
#
# Midnight Club 3 (PS2) city importer for Blender.
#
# The city/level reading code is a port of tools/mc3_view.py (parse_ps2_city),
# tools/mc3_vifpos.py and tools/mc3_cityobj.py from znxee/mc3-znx-tools
# (GPL-3.0), and the ASSETS.DAT ("Dave") reader follows the format notes of the
# MC3 ASSETS.DAT Explorer. This add-on is therefore GPL-3.0 as well.
#
# No game data is included. You supply your own ASSETS.DAT or an extracted
# resources/city/<city>.pck from your own copy of the game.

bl_info = {
    "name": "Midnight Club 3 City Importer",
    "author": "port of znxee/mc3-znx-tools readers",
    "version": (1, 3, 0),
    "blender": (2, 93, 0),
    "location": "3D Viewport > Sidebar (N) > MC3, and File > Import",
    "description": "Import Midnight Club 3: DUB Edition Remix (PS2) city maps "
                   "straight from the game ISO, ASSETS.DAT or an extracted city .pck",
    "category": "Import-Export",
}

import array
import math
import re
import os
import struct
import sys
import zlib

try:
    import bpy
    from mathutils import Matrix
except ImportError:          # lets the parsers be tested outside Blender
    bpy = None


# =============================================================================
# ASSETS.DAT ("Dave" container)
# =============================================================================

_DAVE_CHARS = "\u0000 #$()-./?0123456789_abcdefghijklmnopqrstuvwxyz~\u007f"


def _decode_name(nb, pos, prev, six):
    if not six:
        e = pos
        while e < len(nb) and nb[e] != 0:
            e += 1
        return bytes(nb[pos:e]).decode("latin1")
    c = pos
    codes = []

    def refill():
        nonlocal c
        if c + 3 > len(nb):
            raise ValueError("name overrun")
        v = nb[c] | (nb[c + 1] << 8) | (nb[c + 2] << 16)
        c += 3
        for k in range(4):
            codes.append((v >> (6 * k)) & 63)

    refill()
    pre = ""
    if codes[0] >= 0x38:
        keep = (codes[1] - 0x20) * 8 + codes[0] - 0x38
        pre = prev[:keep]
        del codes[:2]
    s = []
    for _ in range(600):
        if not codes:
            refill()
        k = codes.pop(0)
        if not k:
            break
        s.append(_DAVE_CHARS[k] if k < len(_DAVE_CHARS) else "?")
    return pre + "".join(s)


def dave_entries(f):
    """Entries of a DAVE/Dave archive: [{i, name, fo, full, st}]."""
    f.seek(0, 2)
    size = f.tell()
    f.seek(0)
    h = f.read(16)
    if len(h) < 16 or h[:4] not in (b"DAVE", b"Dave"):
        raise ValueError("Not a DAVE/Dave archive (header magic is %r)" % h[:4])
    six = h[:4] == b"Dave"
    n, tsz, nsz = struct.unpack_from("<3I", h, 4)
    if not n or n * 16 > tsz or 0x800 + tsz > size:
        raise ValueError("Dave header fields look wrong (count %d, table %d)" % (n, tsz))
    f.seek(0x800)
    toc = f.read(n * 16)
    f.seek(0x800 + tsz)
    nb = f.read(max(nsz, 1) + 16) + b"\0\0\0\0"
    out, prev = [], ""
    for i in range(n):
        no, fo, full, st = struct.unpack_from("<4I", toc, i * 16)
        try:
            name = _decode_name(nb, no, prev, six)
        except Exception:
            name = "?bad_name_%d" % i
        prev = name
        out.append({"i": i, "name": name.replace("\\", "/"),
                    "fo": fo, "full": full, "st": st})
    return out


def dave_read(f, en):
    f.seek(en["fo"])
    raw = f.read(en["st"])
    if en["full"] != en["st"]:
        return zlib.decompress(raw, -15)
    return raw


class _SubFile(object):
    """Read-only window [base, base+size) of an open file (a file in an ISO)."""

    def __init__(self, f, base, size):
        self._f, self._base, self._size, self._pos = f, base, size, 0

    def seek(self, off, whence=0):
        if whence == 1:
            off += self._pos
        elif whence == 2:
            off += self._size
        self._pos = max(0, min(off, self._size))
        return self._pos

    def tell(self):
        return self._pos

    def read(self, n=-1):
        if n is None or n < 0 or self._pos + n > self._size:
            n = self._size - self._pos
        self._f.seek(self._base + self._pos)
        b = self._f.read(n)
        self._pos += len(b)
        return b

    def close(self):
        pass


def iso_find(f, want="assets.dat"):
    """Locate a file by name in an ISO 9660 image (2048-byte sectors).

    Returns (offset, size). Walks every directory, so ASSETS.DAT is found in
    the root or in a subfolder. Raises ValueError with what it did find."""
    f.seek(16 * 2048)
    pvd = f.read(2048)
    if pvd[1:6] != b"CD001":
        raise ValueError("Not a 2048-byte-sector ISO 9660 image "
                         "(no CD001 marker at sector 16).")
    root_lba, root_size = struct.unpack_from("<I", pvd, 156 + 2)[0], \
        struct.unpack_from("<I", pvd, 156 + 10)[0]
    seen, todo, names = set(), [(root_lba, root_size)], []
    while todo:
        lba, size = todo.pop()
        if lba in seen or size <= 0 or size > 64 * 1024 * 1024:
            continue
        seen.add(lba)
        f.seek(lba * 2048)
        buf = f.read(size)
        pos = 0
        while pos < len(buf):
            ln = buf[pos]
            if ln == 0:                              # padding to next sector
                pos = (pos // 2048 + 1) * 2048
                continue
            ext = struct.unpack_from("<I", buf, pos + 2)[0]
            sz = struct.unpack_from("<I", buf, pos + 10)[0]
            flags = buf[pos + 25]
            nl = buf[pos + 32]
            nm = buf[pos + 33:pos + 33 + nl]
            pos += ln
            if nm in (b"\x00", b"\x01"):
                continue
            name = nm.decode("latin1").split(";")[0]
            if flags & 2:
                todo.append((ext, sz))
            else:
                names.append(name)
                if name.lower() == want:
                    return ext * 2048, sz
    raise ValueError("%s not found in the ISO (saw %d files: %s)"
                     % (want.upper(), len(names), ", ".join(sorted(names)[:12])))


def open_assets(path):
    """Open an ASSETS.DAT given either the .DAT itself or a game ISO."""
    low = path.lower()
    f = open(path, "rb")
    try:
        if low.endswith((".iso", ".bin", ".img", ".cue")) or f.read(4) not in (b"DAVE", b"Dave"):
            if low.endswith(".cue"):
                raise ValueError("Pick the .iso/.bin itself, not the .cue sheet.")
            off, size = iso_find(f)
            return _SubFile(f, off, size), f
        f.seek(0)
        return f, f
    except Exception:
        f.close()
        raise


def list_city_entries(path):
    """City geometry entries (resources/city/*.pck) of an ASSETS.DAT / ISO."""
    af, raw = open_assets(path)
    try:
        ents = dave_entries(af)
    finally:
        raw.close()
    city = [e for e in ents if re.search(r"(^|/)city/[^/]+\.pck$", e["name"], re.I)]
    if not city:   # unknown layout: offer every .pck
        city = [e for e in ents if e["name"].lower().endswith(".pck")]
    return sorted(city, key=lambda e: e["name"].lower())


# =============================================================================
# VIF position decoder (port of mc3_vifpos.py)
# =============================================================================

def _s8(v):
    return v - 0x100 if v & 0x80 else v


def _s32(v):
    return v - 0x100000000 if v & 0x80000000 else v


def _unpack_size(cmd, num, mask=0, cl=1, wl=1):
    """Bytes of source data an UNPACK consumes (padded to 4).

    Handles STCYCL fill writes (wl > cl: only the first cl rows of each cycle
    read data) and masked unpacks (only fields whose mask bits are 00 read
    data, so an all-ROW mask like 0x55555555 reads nothing at all)."""
    n = num or 256
    cols = ((cmd >> 2) & 3) + 1
    vl = cmd & 3
    bits = (32, 16, 8, 5)[vl] if vl != 3 else 5
    if vl == 3:                         # V4-5
        return (n * 2 + 3) & ~3
    if wl > cl:
        data_rows = [i for i in range(n) if (i % wl) < cl]
    else:
        data_rows = list(range(n))
    if cmd & 0x10:
        total = 0
        for i in data_rows:
            j = min(i % wl, 3)
            fields = [(mask >> (8 * j + 2 * k)) & 3 for k in range(4)]
            total += sum(1 for k in range(cols) if fields[k] == 0)
    else:
        total = len(data_rows) * cols
    return ((total * bits + 7) // 8 + 3) & ~3


def _commands(data):
    off = 0
    mode = 0
    cl = wl = 1
    mask = 0
    row = (0, 0, 0, 0)
    while off < len(data):
        if off + 4 > len(data):
            raise ValueError("partial VIF command at %#x" % off)
        cmd_off = off
        word = struct.unpack_from("<I", data, off)[0]
        off += 4
        cmd = (word >> 24) & 0x7F
        num = (word >> 16) & 0xFF
        imm = word & 0xFFFF
        size = 0
        before = row
        if cmd in (0, 2, 3, 4, 6, 7, 0x10, 0x11, 0x13, 0x14, 0x15, 0x17):
            pass
        elif cmd == 1:
            cl = (imm & 0xFF) or 256
            wl = ((imm >> 8) & 0xFF) or 256
        elif cmd == 5:
            mode = imm & 3
        elif cmd == 0x20:
            size = 4
            if off + 4 <= len(data):
                mask = struct.unpack_from("<I", data, off)[0]
        elif cmd in (0x30, 0x31):
            size = 16
        elif cmd == 0x4A:
            size = (num or 256) * 8
        elif cmd in (0x50, 0x51):
            size = (imm or 65536) * 16
        elif 0x60 <= cmd <= 0x7F:
            size = _unpack_size(cmd, num, mask, cl, wl)
        else:
            raise ValueError("unknown VIF command %#x at %#x" % (cmd, cmd_off))
        if off + size > len(data):
            raise ValueError("VIF payload beyond packet at %#x" % cmd_off)
        yield {"off": cmd_off, "payload": off, "cmd": cmd, "num": num,
               "imm": imm, "size": size, "mode": mode, "row": before,
               "cl": cl, "wl": wl, "fill": wl > cl}
        if cmd == 0x30:
            row = tuple(_s32(struct.unpack_from("<I", data, off + 4 * k)[0])
                        for k in range(4))
        off += size


def _pos_header(c, data):
    if not (c["cmd"] == 0x60 and (c["num"] or 256) == 2 and (c["imm"] & 0x4000)):
        return None
    scale = struct.unpack_from("<f", data, c["payload"])[0]
    count = struct.unpack_from("<I", data, c["payload"] + 4)[0]
    if not math.isfinite(scale) or not (0.0 < scale <= 1.0):
        return None
    if not (0 < count <= 255):
        return None
    return c["imm"] & 0x3FF, scale, count


def _is_position(c, base, count):
    if not (0x60 <= c["cmd"] <= 0x7F):
        return False
    base_cmd = c["cmd"] & ~0x10
    if ((base_cmd >> 2) & 3) + 1 != 3:
        return False
    start = (base + 0x62) & 0x3FF
    addr = c["imm"] & 0x3FF
    n = c["num"] or 256
    return start <= addr < start + count and addr + n <= start + count


def decode_batches(data, with_uv=False):
    """One list of dequantised XYZ tuples per short-packet chain.

    With `with_uv` it returns (positions, uvs): `uvs` has one list of
    (u, v, adc) per chain, or None where the chain has no V2-16 stream at
    ITOP+50 (u, v are 12-bit fixed point, so they can exceed 1 for tiled
    textures; `adc` is the strip-restart flag stored in bit 0 of u).
    """
    batches = []
    uvbatches = []
    uv_cur = None
    current = []
    base = scale = count = None
    rows = 0
    row = [0, 0, 0, 0]

    def close():
        if count is None:
            return
        if rows != count:
            raise ValueError("position count %d != header %d" % (rows, count))
        batches.append(list(current))
        uvbatches.append(uv_cur if uv_cur is not None and len(uv_cur) == count else None)

    for c in _commands(data):
        h = _pos_header(c, data)
        if h is not None:
            close()
            base, scale, count = h
            current = []
            uv_cur = None
            rows = 0
            continue
        if (with_uv and base is not None and c["cmd"] == 0x65 and not c["fill"]
                and (c["num"] or 256) == count
                and (c["imm"] & 0x3FF) == ((base + 50) & 0x3FF)):
            p = c["payload"]
            # Bit 0 of the 16-bit u is NOT texture data: it is the ADC flag
            # (corr 1.00 with the normal-stream ADC on 28k vertices). A set
            # flag means "do not draw the triangle that ends at this vertex",
            # which is how quads and separate pieces share one strip.
            uv_cur = []
            for r in range(count):
                u16, v16 = struct.unpack_from("<2H", data, p + 4 * r)
                uv_cur.append(((u16 & 0xFFFE) / 4096.0, v16 / 4096.0, u16 & 1))
            continue
        if c["cmd"] == 0x30:
            row = [_s32(struct.unpack_from("<I", data, c["payload"] + 4 * k)[0])
                   for k in range(4)]
            continue
        if base is None or not _is_position(c, base, count):
            continue
        if c["cmd"] & 0x10:
            raise ValueError("masked position UNPACK at %#x" % c["off"])
        if c["fill"]:
            raise ValueError("STCYCL fill on a position UNPACK at %#x" % c["off"])
        cmd = c["cmd"]
        n = c["num"] or 256
        p = c["payload"]
        vals = []
        if cmd == 0x68:
            vals = [tuple(_s32(struct.unpack_from("<I", data, p + 12 * r + 4 * k)[0])
                          for k in range(3)) for r in range(n)]
        elif cmd == 0x69:
            vals = [struct.unpack_from("<3h", data, p + 6 * r) for r in range(n)]
        elif cmd == 0x6A:
            for r in range(n):
                raw = tuple(_s8(data[p + 3 * r + k]) for k in range(3))
                if c["mode"] == 2:
                    prev = row if r == 0 else vals[-1]
                    vals.append(tuple(prev[k] + raw[k] for k in range(3)))
                elif c["mode"] == 0:
                    vals.append(raw)
                else:
                    raise ValueError("unsupported STMOD %d at %#x" % (c["mode"], c["off"]))
        else:
            raise ValueError("unsupported position UNPACK %#x" % cmd)
        current.extend(tuple(v * scale for v in xyz) for xyz in vals)
        rows += len(vals)
        if vals and c["mode"] == 2:
            row[:3] = vals[-1]
    close()
    if not batches:
        raise ValueError("short packet header not found")
    return (batches, uvbatches) if with_uv else batches


# =============================================================================
# VIF walk, only used to find the ADC (strip-restart) stream (mc3_view.py)
# =============================================================================

_VIF_UNPACK = {0x60: ("S", 32), 0x61: ("S", 16), 0x62: ("S", 8),
               0x64: ("V2", 32), 0x65: ("V2", 16), 0x66: ("V2", 8),
               0x68: ("V3", 32), 0x69: ("V3", 16), 0x6A: ("V3", 8),
               0x6C: ("V4", 32), 0x6D: ("V4", 16), 0x6E: ("V4", 8),
               0x6F: ("V4", 5)}
_VIF_COMPS = {"S": 1, "V2": 2, "V3": 3, "V4": 4}


def _vif_data_size(cmd, num, mask, cl):
    kind, bits = _VIF_UNPACK[cmd & 0x6F]
    n = num if num else 256
    comps = _VIF_COMPS[kind]
    if not (cmd & 0x10):
        total = n * comps
    else:
        rows = cl if cl else 1
        per = []
        for c in range(min(rows, 4)):
            fields = [(mask >> (8 * c + 2 * k)) & 3 for k in range(4)]
            per.append(sum(1 for k in range(comps) if fields[k] == 0))
        total = sum(per[k % len(per)] for k in range(n))
    return ((total * bits + 7) // 8 + 3) // 4 * 4


def _vif_walk(d, base, size):
    """[[(rel_addr, cmd, data_offset, count)]], one list per MSCAL."""
    end = base + size
    q = base + 0x0C
    mask, cl, itop = 0, 1, None
    out, pend = [], []
    while q < end - 3:
        cmd, num = d[q + 3], d[q + 2]
        imm = d[q] | (d[q + 1] << 8)
        if cmd == 0x20:
            mask = struct.unpack_from("<I", d, q + 4)[0]
            q += 8
            continue
        if cmd in (0x30, 0x31):
            q += 20
            continue
        if cmd == 0x01:
            cl = imm & 0xFF
            q += 4
            continue
        if cmd == 0x04:
            itop = imm
            q += 4
            continue
        if cmd in (0x14, 0x15, 0x17):
            if pend:
                out.append([(a - itop if itop is not None else a, c, o, n)
                            for a, c, o, n in pend])
            pend = []
            q += 4
            continue
        if (cmd & 0x60) == 0x60 and (cmd & 0x6F) in _VIF_UNPACK:
            n = num if num else 256
            if q + 4 + _vif_data_size(cmd, num, mask, cl) > end:
                return []
            pend.append((imm & 0x3FFF, cmd, q + 4, n))
            q += 4 + _vif_data_size(cmd, num, mask, cl)
            continue
        q += 4
    if pend:
        out.append([(a - itop if itop is not None else a, c, o, n)
                    for a, c, o, n in pend])
    return out


# =============================================================================
# City container (port of mc3_view.parse_ps2_city)
# =============================================================================

_IDENT = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, 0.0))


def _u32_array(d):
    n = (len(d) - 0x80) // 4
    a = array.array("I")
    a.frombytes(d[0x80:0x80 + n * 4])
    if sys.byteorder == "big":
        a.byteswap()
    return a


_WATER_RE = re.compile(r"(river|moat|water|canal|harbou?r|bay|pond|lake)", re.I)


def read_palette(d):
    """The city's 256-entry vertex colour palette (MapRoot+0x1290, the source
    copy; the live copy at +0x290 is the same in the file). Entries are RGBA
    floats, rgb 0..1."""
    try:
        n = 1024
        if 0x80 + 0x1290 + 4 * n > len(d):
            return []
        v = struct.unpack_from("<%df" % n, d, 0x80 + 0x1290)
        return [tuple(v[4 * i:4 * i + 3]) for i in range(256)]
    except struct.error:
        return []


def read_vertex_index(d, N, BASE, mo, ngrp, blk_at, nverts):
    """Per-vertex palette index of a mesh, or None.

    mesh+0x14 -> slot[k] -> per-group {counts, vectors} -> per-block vector of
    one byte per vertex. Slot 0 is used. A block whose counts disagree with
    its BlockEntry (or that did not decode) leaves 255 (= untinted)."""
    def U(o):
        return struct.unpack_from("<I", d, o)[0] if 0 <= o <= N - 4 else 0

    def ok(v):
        return bool(v) and 0x80 <= v - BASE <= N - 4

    sc, pb = struct.unpack_from("<H", d, mo + 10)[0], U(mo + 0x14)
    if sc == 0 or not ok(pb):
        return None
    slot = U(pb - BASE + 4 * 0)
    if not ok(slot):
        return None
    out = [255] * nverts
    got = False
    for gi in range(ngrp):
        cva, vva = U(slot - BASE + 8 * gi), U(slot - BASE + 8 * gi + 4)
        if not ok(cva) or not ok(vva):
            continue
        nb = U(cva - BASE)
        if nb > 4096:
            continue
        for j in range(nb):
            at = blk_at.get((gi, j))
            cnt = U(cva - BASE + 4 + 4 * j)
            vp = U(vva - BASE + 4 * j)
            if at is None or at[1] != cnt or not ok(vp) or vp - BASE + cnt > N:
                continue
            out[at[0]:at[0] + cnt] = d[vp - BASE:vp - BASE + cnt]
            got = True
    return out if got else None


def read_thumbnails(d, BASE):
    """The city's rmcTextureProxyPS2 (MapRoot+0x228): one 16x16 4-bit thumbnail
    (128 bytes) and one 16-entry RGBA palette (64 bytes) per texture.

    Returns a list of 1024-byte RGBA images, alpha 0..255. Rows are kept in
    STORED order: row 0 is the first row in the file, which is where the
    GS texture coordinate v = 0 points. Blender image rows are also indexed
    from v = 0, so the UVs can be used unchanged.
    """
    N = len(d)
    try:
        pv = struct.unpack_from("<I", d, 0x80 + 0x228)[0]
        if not pv:
            return []
        po = pv - BASE
        n = struct.unpack_from("<H", d, po)[0]
        th = struct.unpack_from("<I", d, po + 4)[0] - BASE
        pal = struct.unpack_from("<I", d, po + 8)[0] - BASE
        if not (0 < n <= 4096) or th < 0 or pal < 0 or th + 128 * n > N or pal + 64 * n > N:
            return []
    except struct.error:
        return []
    out = []
    for i in range(n):
        raw = d[th + 128 * i:th + 128 * i + 128]
        p = d[pal + 64 * i:pal + 64 * i + 64]
        rows = []
        for y in range(16):
            row = bytearray()
            for x in range(16):
                k = y * 16 + x
                idx = (raw[k >> 1] >> 4) if (k & 1) else (raw[k >> 1] & 15)
                r, g, b, a = p[idx * 4:idx * 4 + 4]
                row += bytes((r, g, b, min(255, a * 2)))
            rows.append(bytes(row))
        out.append(b"".join(rows))
    return out


# --------------------------------------------------------------------------
# Full-resolution textures (TEXTURE.DAT / STREAMS.DAT / BANKS.DAT / .ppf)
# --------------------------------------------------------------------------
# A texture pack holds 8bpp swizzled PSMT8 images (256 colour CLUT-swizzled
# palette, PS2 alpha 0..128). Each one starts at an anchor
#   CDCDCDCD 0E00FFFF 00000000 CDCDCDCD
# with the pixels 0x80 bytes after the anchor start and the size word
# (size + 0x100) at anchor-4. No names are stored, so the city's 16x16
# thumbnails are matched to the full images by their down-sampled look.
# Any file can be scanned: a DAVE/Dave archive is searched entry by entry,
# anything else is searched as one blob, so it does not matter which of the
# game's .DAT files the textures turn out to be in.
_TEX_ANCHOR = b"\xcd\xcd\xcd\xcd\x0e\x00\xff\xff\x00\x00\x00\x00\xcd\xcd\xcd\xcd"
_TEX_DIMS = {0x10500: (256, 256), 0x4500: (128, 128), 0x2500: (128, 64),
             0x1500: (64, 64), 0x900: (32, 32), 0x600: (16, 16)}
_ISO_PACKS = ("texture.dat", "streams.dat", "banks.dat", "assets.dat")
_swz_cache = {}


def _swizzle_map(w, h):
    import numpy as np
    k = (w, h)
    if k not in _swz_cache:
        y, x = np.mgrid[0:h, 0:w].astype(np.int64)
        by = (y & ~15) * w
        sw = (((y + 2) >> 2) & 1) * 4
        cy = ((((y & ~3) >> 1) + (y & 1)) & 7) * w * 2
        bb = (y >> 1) & 1
        _swz_cache[k] = (by + (x & ~15) * 2 + cy + ((x + sw) & 7) * 4 + bb
                         + ((x >> 2) & 2)).ravel()
    return _swz_cache[k]


def _unswizzle_palette(raw):
    import numpy as np
    src = np.frombuffer(raw, dtype=np.uint8).reshape(256, 4)
    order = [pt * 32 + b * 8 + s * 16 + c for pt in range(8) for b in range(2)
             for s in range(2) for c in range(8)]
    return src[np.array(order)]


def scan_texture_pack(d):
    """[(pixel_offset, w, h)] for every 8bpp texture found in `d`."""
    out, i, n = [], 0x70, len(d)
    while True:
        i = d.find(_TEX_ANCHOR, i)
        if i < 0:
            break
        px, fo = i + 0x80, i - 4
        i += 4
        if fo < 0 or fo + 4 > n:
            continue
        v = struct.unpack_from("<I", d, fo)[0]
        if v == 0x160:
            continue                                    # 8x8 4bpp placeholder
        dim = _TEX_DIMS.get(v)
        if not dim:
            a = v - 0x100 - 1024
            cand = []
            if a > 0:
                w = 16
                while w <= 1024:
                    if a % w == 0:
                        h = a // w
                        if 16 <= h <= 1024 and h & (h - 1) == 0:
                            cand.append((w, h))
                    w *= 2
            cand.sort(key=lambda t: (abs(t[0] - t[1]), -t[0]))
            dim = cand[0] if cand else None
        if dim and px + dim[0] * dim[1] + 1024 <= n:
            out.append((px, dim[0], dim[1]))
    return out


def decode_texture(d, px, w, h):
    """RGBA uint8 array (h, w, 4) in file row order; alpha scaled to 0..255."""
    import numpy as np
    buf = np.frombuffer(d, dtype=np.uint8)
    idx = buf[px + _swizzle_map(w, h)]
    pal = _unswizzle_palette(bytes(buf[px + w * h:px + w * h + 1024])).astype(np.uint16)
    pal[:, 3] = np.minimum(255, pal[:, 3] * 2)
    return pal[idx].reshape(h, w, 4).astype(np.uint8)


def _signature(rgba):
    """16x16 alpha-weighted RGBA signature (float, 0..1) of an (h, w, 4) image."""
    import numpy as np
    h, w = rgba.shape[:2]
    f = rgba.astype(np.float32) / 255.0
    f[..., :3] *= f[..., 3:4]
    return f.reshape(16, h // 16, 16, w // 16, 4).mean(axis=(1, 3))


def _blobs_of(src, prefer=""):
    """Texture-bearing byte blobs of an open file-like (archive or raw)."""
    src.seek(0)
    head = src.read(4)
    src.seek(0)
    if head in (b"Hash", b"hsaH"):
        # Angel Studios "Hash" archive (STREAMS.DAT / BANKS.DAT): sound data
        # keyed by name hash. No textures in there, and they can be huge.
        return []
    if head in (b"DAVE", b"Dave"):
        ents = dave_entries(src)
        pick = [e for e in ents if e["name"].lower().endswith(".ppf")
                and e["full"] > 0x1000]          # tiny ones are stubs
        if prefer and pick:
            # TEXTURE.DAT has one .ppf per city and weather: use this city's
            # own pack (else the same city's other weather), not all of them
            pref = prefer.lower()
            stem = lambda e: e["name"].lower().rsplit("/", 1)[-1].rsplit(".", 1)[0]
            tier = [e for e in pick if stem(e) == pref]
            if not tier:
                tier = [e for e in pick if stem(e).split("_")[0] == pref.split("_")[0]]
            if tier:
                # dawn/dusk packs only hold what differs from the night one
                # (~370 of ~3600 textures): add the city's biggest pack too
                city = pref.split("_")[0]
                base = max((e for e in pick if stem(e).split("_")[0] == city),
                           key=lambda e: e["full"])
                if all(base["i"] != e["i"] for e in tier) and \
                        base["full"] > 2 * max(e["full"] for e in tier):
                    tier = tier + [base]
            pick = tier or pick
        if not pick:                       # unknown names: look in everything
            pick = [e for e in ents if 0x1000 < e["full"] <= 96 << 20 and not
                    e["name"].lower().endswith((".pck", ".mp3", ".wav", ".mesh"))]
        return [dave_read(src, e) for e in pick]
    return [src.read()]


def read_texture_packs(path, prefer=""):
    """Byte blobs that may hold textures. `path` can be TEXTURE.DAT,
    STREAMS.DAT, BANKS.DAT, ASSETS.DAT, a .ppf, or a game ISO (every one of
    those files inside it is searched)."""
    f = open(path, "rb")
    try:
        head = f.read(4)
        f.seek(0)
        is_iso = path.lower().endswith((".iso", ".bin", ".img"))
        if head in (b"Hash", b"hsaH"):
            raise ValueError("That is a Hash (audio) archive such as STREAMS.DAT or "
                             "BANKS.DAT; it holds sound, not textures.")
        if not is_iso and head not in (b"DAVE", b"Dave", b"pf05"):
            f.seek(0)
            is_iso = _TEX_ANCHOR not in f.read(0x4000)
            f.seek(0)
            if is_iso:
                f.seek(16 * 2048 + 1)
                is_iso = f.read(5) == b"CD001"
                f.seek(0)
        if not is_iso:
            blobs = _blobs_of(f, prefer)
            if not blobs:
                raise ValueError("No textures found in %s." % os.path.basename(path))
            return blobs
        blobs = []
        for nm in _ISO_PACKS:
            try:
                off, size = iso_find(f, nm)
            except ValueError:
                continue
            try:
                blobs.extend(_blobs_of(_SubFile(f, off, size), prefer))
            except ValueError:
                continue
        if not blobs:
            raise ValueError("No texture data found in %s inside the ISO."
                             % ", ".join(_ISO_PACKS))
        return blobs
    finally:
        f.close()


def match_full_textures(thumbs, blobs, max_rmse=0.12, info=None):
    """{thumbnail id (1-based): (w, h, RGBA bytes in stored row order)}.

    Every texture in `blobs` is reduced to 16x16 and compared with each
    thumbnail (upright and flipped). A thumbnail takes a texture only when it
    is close enough AND that thumbnail is the texture's own best match, so
    flat or dark thumbnails do not grab unrelated pictures."""
    import numpy as np
    if not thumbs:
        return {}
    T = np.stack([_signature(np.frombuffer(bytes(raw), dtype=np.uint8).reshape(16, 16, 4))
                  for raw in thumbs]).reshape(len(thumbs), -1)
    refs, errs, flips = [], [], []
    for bi, d in enumerate(blobs):
        for (px, w, h) in scan_texture_pack(d):
            try:
                sig = _signature(decode_texture(d, px, w, h))
            except Exception:
                continue
            e0 = np.sqrt(((T - sig.reshape(-1)) ** 2).mean(axis=1))
            e1 = np.sqrt(((T - sig[::-1].reshape(-1)) ** 2).mean(axis=1))
            errs.append(np.minimum(e0, e1))
            flips.append(e1 < e0)
            refs.append((bi, px, w, h))
    out, best_e = {}, {}
    if info is not None:
        info["scanned"] = len(refs)
    if not refs:
        return out
    E, F = np.stack(errs), np.stack(flips)          # (textures, thumbs)
    for f in range(len(refs)):
        m = E[f].min()
        if m > max_rmse:
            continue
        for t in map(int, np.nonzero(E[f] <= m + 1e-6)[0]):   # best thumbnail(s)
            if t + 1 in best_e and best_e[t + 1] <= E[f, t]:
                continue
            best_e[t + 1] = E[f, t]
            bi, px, w, h = refs[f]
            img = decode_texture(blobs[bi], px, w, h)
            if F[f, t]:
                img = img[::-1]
            out[t + 1] = (w, h, np.ascontiguousarray(img).tobytes())
    # Second pass: a thumbnail that lost out to a better-matching neighbour
    # (several thumbnails can stand for variants of one picture) still takes
    # its closest texture when that is a really close match. Flat thumbnails
    # are skipped: they look like everything.
    for t in range(E.shape[1]):
        if t + 1 in out:
            continue
        f = int(E[:, t].argmin())
        if E[f, t] > min(max_rmse * 0.5, 0.06):
            continue
        a4 = np.frombuffer(bytes(thumbs[t]), dtype=np.uint8).reshape(-1, 4)
        if len({tuple(x) for x in a4[:, :3]}) < 3:
            continue
        bi, px, w, h = refs[f]
        img = decode_texture(blobs[bi], px, w, h)
        if F[f, t]:
            img = img[::-1]
        out[t + 1] = (w, h, np.ascontiguousarray(img).tobytes())
    if info is not None:
        # how close the thumbnails that found nothing came: tells whether the
        # tolerance is too tight or their texture is simply not in this pack
        left = [float(E[:, t].min()) for t in range(E.shape[1]) if t + 1 not in out]
        bins = [(0.06, 0.08), (0.08, 0.12), (0.12, 0.2), (0.2, 9.0)]
        info["unmatched_bins"] = {"%.2f-%.2f" % b: sum(b[0] < v <= b[1] for v in left)
                                  for b in bins}
        flat = 0
        for t in range(len(thumbs)):
            if t + 1 not in out:
                a = np.frombuffer(bytes(thumbs[t]), dtype=np.uint8).reshape(-1, 4)
                flat += int(len({tuple(x) for x in a[:, :3]}) <= 2)
        info["unmatched_flat"] = flat
    return out


def parse_city(d, include_lods=False):
    """Parse a city .pck.

    Returns (meshes, stats). Each mesh is a dict with model-space `verts`,
    `tris`, the owner `name`, `material` index and `instances`, a list of
    placements ((r0, r1, r2, t): world = x*r0 + y*r1 + z*r2 + t). A mesh the
    object graph does not reach has an empty list and `inherited`/`unplaced`
    set (see below).
    """
    d = bytes(d)
    N = len(d)
    if N < 0x100:
        raise ValueError("File too small to be a city .pck")
    va, kind, version, _size = struct.unpack_from("<4I", d, 0)
    if kind not in (67, 4) or version != 1:
        raise ValueError("Not a city container (type %d version %d). "
                         "Pick a resources/city/<city>.pck." % (kind, version))
    BASE = va - 0x80

    def F(v):
        return v - BASE

    def U(o):
        return struct.unpack_from("<I", d, o)[0] if 0 <= o <= N - 4 else 0

    def U16(o):
        return struct.unpack_from("<H", d, o)[0] if 0 <= o <= N - 2 else 0

    def ok(v):
        return bool(v) and 0x80 <= v - BASE <= N - 4

    arr = _u32_array(d)

    # The mesh tag is a build-time vtable token that differs per city, so take
    # the value that most often sits in front of a usable group table.
    votes = {}
    for i, v in enumerate(arr):
        if not (0x600000 <= v < 0x7C0000):
            continue
        o = 0x80 + 4 * i
        ng, gt = U16(o + 8), U(o + 0x10)
        if not (1 <= ng <= 64) or not ok(gt) or F(gt) + 8 > N:
            continue
        lp, cnt, _ = struct.unpack_from("<IHH", d, F(gt))
        if ok(lp) and 1 <= cnt <= 4096:
            votes[v] = votes.get(v, 0) + 1
    if not votes:
        # same keys as a normal result, so callers never hit a KeyError
        return [], {"hoods": 0, "meshes": 0, "orphans": 0, "mesh_tag": 0,
                    "thumbs": read_thumbnails(d, BASE), "palette": [], "matdefs": [], "textured_tris": 0,
                    "tris": 0}
    MESH_TAG = max(votes, key=votes.get)

    # mesh virtual address -> owner name and every placement of it.
    # MapRoot -> hoods -> the two component arrays -> ComponentData (+0x0C)
    # -> LOD slots (+0x3C).
    info = {}
    R = 0x80
    nh, hp = U(R + 0x14), U(R + 0x18)
    if ok(hp):
        hb = F(hp)
        for h in range(nh if nh < 64 else 0):
            e = hb + 20 * h
            for cnt, arr_va, stride, placed in ((U(e + 0x04), U(e + 0x08), 28, False),
                                                 (U(e + 0x0C), U(e + 0x10), 96, True)):
                if not ok(arr_va) or cnt > 20000:
                    continue
                ab = F(arr_va)
                for k in range(cnt):
                    comp = ab + stride * k
                    dp = U(comp + 0x0C)
                    if not ok(dp):
                        continue
                    cd = F(dp)
                    if cd + 0xB0 > N:
                        continue
                    sgrp = d[comp + 0x0A]       # shader (pass) group of the component
                    nm = bytes(d[cd + 0x70:cd + 0xB0]).split(b"\x00")[0]
                    nm = nm.decode("latin1") if nm and all(32 <= c < 127 for c in nm) else ""
                    mat = _IDENT
                    if placed:
                        try:
                            r = [struct.unpack_from("<3f", d, comp + 0x20 + 12 * j)
                                 for j in range(4)]
                            if all(-1e5 < c < 1e5 for c in r[3]):
                                mat = tuple(tuple(x) for x in r)
                        except Exception:
                            pass
                    else:
                        try:
                            c = struct.unpack_from("<3f", d, cd + 0x2C)
                            if all(-1e5 < x < 1e5 for x in c):
                                mat = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0),
                                       (0.0, 0.0, 1.0), tuple(c))
                        except Exception:
                            pass
                    # The ten slots are detail levels, and a component often
                    # fills only ONE of them, not necessarily slot 0 (in Tokyo
                    # ~1500 placed components use only slot 1 or slot 4). So
                    # the lowest filled slot is the component's visible mesh
                    # and any further filled slot is a coarser LOD.
                    seen = set()
                    prim = None
                    for bslot in range(10):
                        mp = U(cd + 0x3C + 4 * bslot)
                        if not ok(mp) or mp in seen:
                            continue
                        seen.add(mp)
                        if prim is None:
                            prim = bslot
                        ent = info.get(mp)
                        if ent is None:
                            ent = info[mp] = {"name": nm, "mats": [],
                                              "lodmats": [], "lodname": "", "sg": sgrp}
                        if bslot == prim:
                            if not ent["mats"]:
                                ent["name"] = nm
                                ent["sg"] = sgrp
                            ent["mats"].append(mat)
                        else:
                            ent["lodmats"].append(mat)
                            if not ent["lodname"]:
                                ent["lodname"] = "%s#%d" % (nm, bslot)

    # ---- textures -------------------------------------------------------
    # material index -> shader (MapRoot+0x0C: one array per pass group, the
    # component picks its group with the byte at +0x0A) -> texture parameter ->
    # TEX1 proxy id (11 bits, 1-based) -> the 16x16 thumbnail kept in the pck.
    sgroups = []
    ngrp_s, gva = U(R + 0x08), U(R + 0x0C)
    if 0 < ngrp_s <= 64 and ok(gva):
        for g in range(ngrp_s):
            o = F(gva) + 16 * g
            if o + 16 > N:
                break
            sgroups.append((U(o + 4), U16(o + 8)))
    thumbs = read_thumbnails(d, BASE)
    tex_cache = {}
    shader_info = {}      # (group, material) -> (shader index, draw bucket, params)
    shader_params = {}    # (group, material) -> (shader index, n, texture ids, floats)
    matdefs, matdef_ids, matdef_cache = [], {}, {}

    def resolve_tex(sg, mat):
        key = (sg, mat)
        if key in tex_cache:
            return tex_cache[key]
        tid = 0
        if sg < len(sgroups):
            arr_va, sc = sgroups[sg]
            if ok(arr_va) and mat < sc and F(arr_va) + 4 * mat + 4 <= N:
                sp = U(F(arr_va) + 4 * mat)
                if ok(sp) and F(sp) + 0x20 <= N:
                    o = F(sp)
                    flags = U(o + 4)
                    n = (flags >> 22) & 7
                    mask = (flags >> 25) & 0x7F
                    shader_info[key] = ((flags >> 15) & 0x7F, (flags >> 7) & 0x1F, n)
                    if n == 0:          # slot 0 exists even with a zero count
                        mask = 1
                    tids, floats = [], []
                    for i in range(max(n, 1)):
                        if not (mask >> i) & 1:
                            if n and F(o) + 8 + 4 * i + 4 <= N:
                                floats.append(struct.unpack_from("<f", d, o + 8 + 4 * i)[0])
                            continue
                        t_id = 0
                        pp = U(o + 8 + 4 * i)
                        t = F(pp) if ok(pp) else -1
                        if 0 <= t and t + 0x90 <= N:
                            kind = d[t + 4]
                            if kind == 2:               # reference: follow it
                                w = U(t + 0x0C)
                                t = F(w) if ok(w) and F(w) + 0x90 <= N else -1
                                kind = d[t + 4] if t >= 0 else -1
                            if kind == 0 and t >= 0:
                                t1 = struct.unpack_from("<Q", d, t + 0x88)[0]
                                t_id = (t1 >> 21) & 0x7FF
                        tids.append(t_id if t_id <= len(thumbs) else 0)
                    shader_params[key] = ((flags >> 15) & 0x7F, n, tids, floats)
                    tid = next((x for x in tids if x), 0)
        if tid > len(thumbs):
            tid = 0
        tex_cache[key] = tid
        return tid

    def resolve_mat(sg, mat):
        """Material definition id (1-based) for a (pass group, material), or 0.

        The definition keeps what the shader uses beyond its first texture:
        the window shader (6 params) has a window mask in texture 0, a lit
        interior in texture 1 and an RGBA light colour; the tinted shaders
        (5 params) have one texture and an RGBA multiplier."""
        key = (sg, mat)
        if key in matdef_cache:
            return matdef_cache[key]
        tid = resolve_tex(sg, mat)
        out = 0
        if tid:
            idx, n, tids, floats = shader_params.get(key, (0, 0, [], []))
            kind, tid1, rgba = "plain", 0, None
            if n == 6 and len(tids) >= 2 and tids[1] and len(floats) >= 4:
                kind, tid1, rgba = "window", tids[1], tuple(floats[:4])
            elif n == 5 and len(floats) >= 4:
                kind, rgba = "tint", tuple(floats[:4])
            elif n == 4 and len(tids) >= 2 and not tids[1]:
                kind = "road"
            md = (kind, tid, tid1, rgba)
            if md not in matdef_ids:
                matdefs.append(md)
                matdef_ids[md] = len(matdefs)
            out = matdef_ids[md]
        matdef_cache[key] = out
        return out

    meshes, orphans = [], []
    tags = [0x80 + 4 * i for i, v in enumerate(arr) if v == MESH_TAG]
    for mo in tags:
        ngrp = U16(mo + 8)
        gt, mt = U(mo + 0x10), U(mo + 0x0C)
        if not (1 <= ngrp <= 64) or not ok(gt):
            continue
        verts, tris, tri_mat, tri_tex, tri_shader, uvs = [], [], [], [], [], []
        blk_at = {}
        ent0 = info.get(mo + BASE)
        sg0 = ent0["sg"] if ent0 else -1
        # material table: one u16 per group, so the groups of one mesh can
        # use different materials
        gmat = [U16(F(mt) + 2 * i) if ok(mt) else 0 for i in range(ngrp)]
        material = gmat[0] if gmat else 0
        for gi in range(ngrp):
            if F(gt) + 8 * gi + 8 > N:
                break
            lp, cnt, _ = struct.unpack_from("<IHH", d, F(gt) + 8 * gi)
            if not ok(lp) or cnt > 4096:
                break
            for bi in range(cnt):
                if F(lp) + 8 * bi + 8 > N:
                    break
                bp, qw, nv = struct.unpack_from("<IHH", d, F(lp) + 8 * bi)
                if not ok(bp) or not (3 <= nv <= 256):
                    continue
                b = F(bp)
                try:
                    pos_batches, uv_batches = decode_batches(
                        bytes(d[b:b + qw * 16]), True)
                except ValueError:
                    continue
                if sum(len(x) for x in pos_batches) != nv:
                    continue
                blk_at[(gi, bi)] = (len(verts), nv)
                walked = _vif_walk(d, b, qw * 16)
                adcs = []
                for batch in walked:
                    adc = None
                    for rel, cmd, off, n in batch:
                        if rel == 2 and (cmd & 0x6F) in (0x6A, 0x6E):
                            adc = (off, 3 if (cmd & 0x6F) == 0x6A else 4)
                            break
                    adcs.append(adc)
                # every batch is its own strip: joining them would bridge
                # parts of the mesh that never touch
                for li, positions in enumerate(pos_batches):
                    vn = len(positions)
                    adc = adcs[li] if li < len(adcs) else None
                    base = len(verts)
                    verts.extend(positions)
                    uvb = uv_batches[li] if li < len(uv_batches) else None
                    uvs.extend([(a, b) for a, b, _f in uvb] if uvb else [(0.0, 0.0)] * vn)
                    skip = [False] * vn
                    if uvb:
                        skip = [bool(f) for _a, _b, f in uvb]
                    elif adc and adc[0] + adc[1] * vn <= N:
                        no, st = adc
                        skip = [(d[no + st * i] & 1) == 1 for i in range(vn)]
                    for i in range(2, vn):
                        if skip[i]:
                            continue
                        a, bb, c = (i - 2, i - 1, i) if i % 2 == 0 else (i - 1, i - 2, i)
                        A, B, C = verts[base + a], verts[base + bb], verts[base + c]
                        if A == B or B == C or A == C:
                            continue
                        tris.append((base + a, base + bb, base + c))
                        tri_mat.append(gmat[gi])
                        tx = resolve_mat(sg0, gmat[gi]) if sg0 >= 0 else 0
                        if tx == 0 and ent0 and _WATER_RE.search(ent0["name"]):
                            tx = -1         # water surface with no usable texture
                        tri_tex.append(tx)
                        tri_shader.append(shader_info.get((sg0, gmat[gi]), (-1, 0, 0)))
        if not tris:
            continue
        vidx = read_vertex_index(d, N, BASE, mo, ngrp, blk_at, len(verts))
        ent = info.get(mo + BASE)
        prim_mats = list(ent["mats"]) if ent else []
        lod_mats = list(ent["lodmats"]) if ent else []
        m = {"voff": mo + BASE, "material": material, "verts": verts, "tris": tris,
             "tri_mat": tri_mat, "tri_tex": tri_tex, "tri_shader": tri_shader,
             "uvs": uvs, "vidx": vidx,
             "name": ent["name"] if ent else "mesh@%06X" % mo,
             # a mesh that is nobody's primary slot is pure LOD
             "instances": prim_mats if prim_mats else lod_mats,
             "lod_instances": lod_mats if prim_mats else [],
             "lod": bool(ent and not prim_mats),
             "inherited": False, "unplaced": ent is None}
        if ent is None:
            orphans.append(len(meshes))
        meshes.append(m)

    # Only part of the meshes are reachable through the graph. An orphan
    # inherits the translation of the nearest placed mesh by file offset: a
    # display HEURISTIC, flagged `inherited` and named with a trailing " ~".
    placed = []
    for m in meshes:
        if m["instances"]:
            t = m["instances"][0][3]
            if t[0] or t[1] or t[2]:
                placed.append((m["voff"], t))
    if placed and orphans:
        import bisect
        placed.sort(key=lambda x: x[0])
        keys = [k for k, _t in placed]
        for gi in orphans:
            m = meshes[gi]
            j = bisect.bisect_left(keys, m["voff"])
            best = None
            for cand in (j - 1, j):
                if 0 <= cand < len(placed):
                    if best is None or abs(placed[cand][0] - m["voff"]) < abs(best[0] - m["voff"]):
                        best = placed[cand]
            if best is None:
                continue
            m["instances"] = [((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0),
                               tuple(best[1]))]
            m["inherited"] = True
            m["unplaced"] = False
            m["name"] += " ~"

    if include_lods:
        for m in meshes:
            m["instances"] = m["instances"] + [x for x in m["lod_instances"]
                                               if x not in m["instances"]]
    else:
        meshes = [m for m in meshes if not m["lod"]]
    stats = {"hoods": nh if nh < 64 else 0, "meshes": len(meshes),
             "orphans": len(orphans), "mesh_tag": MESH_TAG, "thumbs": thumbs, "palette": read_palette(d), "matdefs": matdefs,
             "textured_tris": sum(1 for m in meshes for t in m["tri_tex"] if t > 0),
             "tris": sum(len(m["tris"]) for m in meshes)}
    return meshes, stats


# =============================================================================
# Blender side
# =============================================================================

def _hsv_rgb(h, s, v):
    import colorsys
    return colorsys.hsv_to_rgb(h, s, v)


def _conv(z_up, scale):
    s = float(scale)
    if z_up:       # game is Y-up, Blender Z-up: (x, y, z) -> (x, -z, y)
        return Matrix(((s, 0, 0, 0), (0, 0, -s, 0), (0, s, 0, 0), (0, 0, 0, 1)))
    return Matrix.Scale(s, 4)


def _mat4(m):
    r0, r1, r2, t = m
    return Matrix(((r0[0], r1[0], r2[0], t[0]),
                   (r0[1], r1[1], r2[1], t[1]),
                   (r0[2], r1[2], r2[2], t[2]),
                   (0.0, 0.0, 0.0, 1.0)))


def _fallback_material(cache):
    if "fallback" in cache:
        return cache["fallback"]
    mat = bpy.data.materials.new("MC3_untextured")
    mat.diffuse_color = (0.6, 0.6, 0.6, 1.0)
    mat.use_backface_culling = False
    cache["fallback"] = mat
    return mat


def _water_material(cache):
    if "water" in cache:
        return cache["water"]
    mat = bpy.data.materials.new("MC3_water")
    mat.diffuse_color = (0.06, 0.16, 0.26, 1.0)
    mat.use_backface_culling = False
    try:
        mat.use_nodes = True
        b = mat.node_tree.nodes.get("Principled BSDF")
        if b:
            b.inputs["Base Color"].default_value = (0.06, 0.16, 0.26, 1.0)
            b.inputs["Roughness"].default_value = 0.25
            for k in ("Specular IOR Level", "Specular"):
                if k in b.inputs:
                    b.inputs[k].default_value = 0.8
                    break
    except Exception:
        pass
    cache["water"] = mat
    return mat


def _image(tid, thumbs, cache):
    key = ("img", tid)
    if key not in cache and tid in cache.get("full", {}):
        import numpy as np
        w, h, rgba = cache["full"][tid]
        img = bpy.data.images.new("MC3_tex_%03d" % tid, w, h, alpha=True)
        img.pixels.foreach_set(np.frombuffer(rgba, dtype=np.uint8)
                               .astype(np.float32) / 255.0)
        img.pack()
        al = rgba[3::4]
        cache[key] = (img, min(al) < 255, any(0 < a < 255 for a in set(al)))
    if key not in cache:
        raw = list(thumbs[tid - 1])
        if raw and max(raw[3::4]) == 0:
            # an all-zero alpha channel means the shader ignores alpha
            # (ground tiles etc.); keeping it would make the mesh invisible
            raw[3::4] = [255] * (len(raw) // 4)
            cache[('zeroalpha', tid)] = True
        img = bpy.data.images.new("MC3_tex_%03d" % tid, 16, 16, alpha=True)
        img.pixels.foreach_set([c / 255.0 for c in raw])
        img.pack()
        alphas = set(raw[i] for i in range(3, len(raw), 4))
        cache[key] = (img, any(a < 255 for a in alphas),
                      any(0 < a < 255 for a in alphas))
    return cache[key]


def _mix_multiply(nt, a_out, b_in_color):
    """a_out * b (RGB multiply) as a node, tolerant of the Blender version."""
    try:
        n = nt.nodes.new("ShaderNodeMix")
        n.data_type = "RGBA"
        n.blend_type = "MULTIPLY"
        n.inputs[0].default_value = 1.0
        ins = [i for i in n.inputs if i.type == "RGBA"]
        outs = [o for o in n.outputs if o.type == "RGBA"]
        nt.links.new(a_out, ins[0])
        if isinstance(b_in_color, tuple):
            ins[1].default_value = b_in_color
        else:
            nt.links.new(b_in_color, ins[1])
        return outs[0]
    except Exception:
        n = nt.nodes.new("ShaderNodeMixRGB")
        n.blend_type = "MULTIPLY"
        n.inputs[0].default_value = 1.0
        nt.links.new(a_out, n.inputs[1])
        if isinstance(b_in_color, tuple):
            n.inputs[2].default_value = b_in_color
        else:
            nt.links.new(b_in_color, n.inputs[2])
        return n.outputs[0]


def _set_input(bsdf, names, value):
    for k in names:
        if k in bsdf.inputs:
            bsdf.inputs[k].default_value = value
            return True
    return False


def _tex_material(md, thumbs, cache, glow=2.0):
    """Material for a definition (kind, tex0, tex1, rgba).

    plain  - the texture, alpha-clipped so fences, trusses and cut-outs keep
             their shape, multiplied by the baked vertex colour.
    tint   - the same, multiplied by the shader's RGBA colour parameter.
    window - texture 0 is the window mask (black glass, transparent around
             it); texture 1 is the lit interior seen through it. The RGBA is
             the light colour, so windows glow."""
    key = ("mat", md, round(glow, 2))
    if key in cache:
        return cache[key]
    kind, tid, tid1, rgba = md
    img, has_alpha, soft = _image(tid, thumbs, cache)
    mat = bpy.data.materials.new("MC3_%s_%03d" % (kind, tid))
    mat.diffuse_color = (0.7, 0.7, 0.7, 1.0)
    mat.use_backface_culling = False       # PS2 geometry is often single-sided
    try:
        mat.use_nodes = True
        nt = mat.node_tree
        bsdf = nt.nodes.get("Principled BSDF")
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = img
        tex.interpolation = "Closest"
        tex.extension = "REPEAT"
        tex.location = (-700, 200)
        vc = nt.nodes.new("ShaderNodeVertexColor")
        vc.layer_name = "MC3Col"
        vc.location = (-700, -100)
        color = _mix_multiply(nt, tex.outputs["Color"], vc.outputs["Color"])
        alpha = tex.outputs["Alpha"]
        if cache.get(("zeroalpha", tid)) and kind == "plain":
            # ground/asphalt tiles: in game these are dark asphalt, but the
            # embedded 16x16 thumbnail is a pale base layer. Darken it.
            color = _mix_multiply(nt, color, (0.30, 0.30, 0.32, 1.0))
        if kind == "road":
            # lane markings over an asphalt base (the base texture is not
            # resolvable, so use a flat dark asphalt colour)
            mx = nt.nodes.new("ShaderNodeMix")
            mx.data_type = "RGBA"
            mx.inputs[6].default_value = (0.12, 0.12, 0.13, 1.0)
            nt.links.new(alpha, mx.inputs[0])
            nt.links.new(tex.outputs["Color"], mx.inputs[7])
            color = _mix_multiply(nt, mx.outputs[2], vc.outputs["Color"])
            has_alpha = soft = False
        if rgba and kind == "tint":
            color = _mix_multiply(nt, color, (rgba[0] / 255.0, rgba[1] / 255.0,
                                              rgba[2] / 255.0, 1.0))
            if rgba[3] < 255:
                m = nt.nodes.new("ShaderNodeMath")
                m.operation = "MULTIPLY"
                m.inputs[1].default_value = rgba[3] / 255.0
                nt.links.new(alpha, m.inputs[0])
                alpha = m.outputs[0]
                soft = has_alpha = True
        if bsdf:
            nt.links.new(color, bsdf.inputs["Base Color"])
            if has_alpha:
                nt.links.new(alpha, bsdf.inputs["Alpha"])
            _set_input(bsdf, ("Roughness",), 1.0)
            _set_input(bsdf, ("Specular IOR Level", "Specular"), 0.0)   # matte, PS2 style
            if kind == "window" and tid1:
                img1, _a1, _s1 = _image(tid1, thumbs, cache)
                t1 = nt.nodes.new("ShaderNodeTexImage")
                t1.image = img1
                t1.interpolation = "Closest"
                t1.extension = "REPEAT"
                t1.location = (-700, -400)
                lit = _mix_multiply(nt, t1.outputs["Color"],
                                    (rgba[0] / 255.0, rgba[1] / 255.0, rgba[2] / 255.0, 1.0))
                if _set_input(bsdf, ("Emission Color", "Emission"), (0, 0, 0, 1)):
                    nt.links.new(lit, bsdf.inputs["Emission Color"
                                                  if "Emission Color" in bsdf.inputs else "Emission"])
                    _set_input(bsdf, ("Emission Strength",), glow)
        if has_alpha:
            for attr, val in (("blend_method", "HASHED" if soft else "CLIP"),
                              ("alpha_threshold", 0.5),
                              ("surface_render_method", "DITHERED"),
                              ("use_backface_culling", False)):
                try:
                    setattr(mat, attr, val)
                except Exception:
                    pass
    except Exception:
        pass
    cache[key] = mat
    return mat


def _set_vcols(me, cols):
    """cols: Nx4 float array, one per vertex, stored as the MC3Col attribute."""
    import numpy as np
    cols = np.asarray(cols, dtype=np.float32)
    try:
        at = me.color_attributes.new("MC3Col", "FLOAT_COLOR", "POINT")
        at.data.foreach_set("color", cols.ravel())
    except (AttributeError, RuntimeError, TypeError):
        vc = me.vertex_colors.new(name="MC3Col")
        loop_v = np.empty(len(me.loops), dtype=np.int32)
        me.loops.foreach_get("vertex_index", loop_v)
        vc.data.foreach_set("color", cols[loop_v].ravel())


def _fill_mesh(me, verts, tris, uvs, tri_tex, thumbs, cache, vcols=None,
               matdefs=(), glow=2.0):
    """verts: Nx3 array, tris: Mx3 int array, uvs: Nx2, tri_tex: M ints
    (1-based index into `matdefs`, -1 water, 0 none), vcols: Nx4 or None."""
    import numpy as np
    if len(verts) == 0 or len(tris) == 0:
        return                      # nothing to build (and no loops to fill)
    me.from_pydata(verts.tolist(), [], tris.tolist())
    used = sorted(set(tri_tex.tolist()))
    slot = {}
    for t in used:
        slot[t] = len(slot)
        if t == -1:
            me.materials.append(_water_material(cache))
        elif 0 < t <= len(matdefs):
            me.materials.append(_tex_material(matdefs[t - 1], thumbs, cache, glow))
        else:
            me.materials.append(_fallback_material(cache))
    me.polygons.foreach_set("material_index",
                            np.fromiter((slot[t] for t in tri_tex.tolist()),
                                        dtype=np.int32, count=len(tri_tex)))
    uvl = me.uv_layers.new(name="UVMap")
    loop_v = np.empty(len(me.loops), dtype=np.int32)
    me.loops.foreach_get("vertex_index", loop_v)
    uvl.data.foreach_set("uv", uvs[loop_v].astype(np.float32).ravel())
    if vcols is None:
        vcols = np.ones((len(verts), 4), dtype=np.float32)
    _set_vcols(me, vcols)
    me.update()


def _mesh_vcols(m, palette, gain):
    """Per-vertex RGBA from the mesh's palette indices (white where untinted)."""
    import numpy as np
    n = len(m["verts"])
    out = np.ones((n, 4), dtype=np.float32)
    vi = m.get("vidx")
    if vi and palette:
        P = np.asarray(palette, dtype=np.float32) * gain
        idx = np.asarray(vi, dtype=np.int32)
        have = idx < len(P)
        out[have, :3] = P[idx[have]]
    return out


def build_city(context, meshes, label, stats, z_up=True, scale=1.0, merge=False,
               include_orphans=False, use_vcol=True, vcol_gain=2.0, glow=2.0,
               fulltex=None):
    thumbs, matdefs = stats["thumbs"], stats["matdefs"]
    palette = stats.get("palette") if use_vcol else None
    import numpy as np
    if not include_orphans:
        meshes = [m for m in meshes if m["instances"] and not m["inherited"]]
    else:
        meshes = [m for m in meshes if m["instances"] or m["unplaced"]]
    if not meshes:
        raise ValueError("No placeable geometry found in this city.")
    coll = bpy.data.collections.new(label)
    context.scene.collection.children.link(coll)
    C = _conv(z_up, scale)
    cache = {"full": fulltex or {}}
    n_obj = n_tri = 0

    if merge:
        Cn = np.array([[C[i][j] for j in range(3)] for i in range(3)], dtype=np.float64)
        vs, fs, ts, us, cs = [], [], [], [], []
        off = 0
        for m in meshes:
            V = np.asarray(m["verts"], dtype=np.float64)
            T = np.asarray(m["tris"], dtype=np.int64)
            TT = np.asarray(m["tri_tex"], dtype=np.int64)
            UV = np.asarray(m["uvs"], dtype=np.float64)
            CV = _mesh_vcols(m, palette, vcol_gain)
            for pl in (m["instances"] or [_IDENT]):
                rows = np.array(pl[:3], dtype=np.float64)
                vs.append((V @ rows + np.array(pl[3], dtype=np.float64)) @ Cn.T)
                fs.append(T + off)
                ts.append(TT)
                us.append(UV)
                cs.append(CV)
                off += len(V)
                n_tri += len(T)
        me = bpy.data.meshes.new(label)
        _fill_mesh(me, np.concatenate(vs), np.concatenate(fs), np.concatenate(us),
                   np.concatenate(ts), thumbs, cache, np.concatenate(cs), matdefs, glow)
        ob = bpy.data.objects.new(label, me)
        coll.objects.link(ob)
        n_obj = 1
    else:
        for m in meshes:
            me = bpy.data.meshes.new(m["name"])
            _fill_mesh(me, np.asarray(m["verts"], dtype=np.float64),
                       np.asarray(m["tris"], dtype=np.int64),
                       np.asarray(m["uvs"], dtype=np.float64),
                       np.asarray(m["tri_tex"], dtype=np.int64), thumbs, cache,
                       _mesh_vcols(m, palette, vcol_gain), matdefs, glow)
            for pl in (m["instances"] or [_IDENT]):
                ob = bpy.data.objects.new(m["name"], me)   # linked duplicates share `me`
                ob.matrix_world = C @ _mat4(pl)
                ob["mc3_va"] = "0x%06X" % m["voff"]
                if m["inherited"]:
                    ob["mc3_position_inherited"] = True
                coll.objects.link(ob)
                n_obj += 1
                n_tri += len(m["tris"])

    # make the whole city visible: clip end tuned to its size
    try:
        for win in context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == "VIEW_3D":
                    for sp in area.spaces:
                        if sp.type == "VIEW_3D":
                            sp.clip_end = max(sp.clip_end, 100000.0)
    except Exception:
        pass
    return n_obj, n_tri


if bpy:
    from bpy.props import (BoolProperty, CollectionProperty, EnumProperty,
                           FloatProperty, IntProperty, StringProperty)
    from bpy.types import Operator, Panel, PropertyGroup, UIList
    from bpy_extras.io_utils import ImportHelper

    class MC3_CityEntry(PropertyGroup):
        name: StringProperty()
        index: IntProperty()
        size: IntProperty()

    class MC3_Settings(PropertyGroup):
        path: StringProperty(name="File", subtype="FILE_PATH",
                             description="Game ISO, ASSETS.DAT, or an extracted city .pck")
        texpath: StringProperty(name="Textures", subtype="FILE_PATH",
                                description="Optional: TEXTURE.DAT, STREAMS.DAT, BANKS.DAT, a .ppf or "
                                            "the ISO. Replaces the 16x16 thumbnails with full-size textures")
        tex_tol: FloatProperty(name="Texture match tolerance", default=0.12,
                               min=0.01, max=0.3,
                               description="How different a full-size texture may look from the "
                                           "city's thumbnail and still be used (higher matches "
                                           "more, but may pick wrong ones)")
        entries: CollectionProperty(type=MC3_CityEntry)
        active: IntProperty()
        z_up: BoolProperty(name="Convert Y-up to Z-up", default=True,
                           description="The game is Y-up; Blender is Z-up")
        scale: FloatProperty(name="Scale", default=1.0, min=1e-6, soft_max=10.0,
                             description="Multiply game units (the cull grid cell is 40 units)")
        mode: EnumProperty(name="Mode", items=(
            ("OBJECTS", "Separate objects", "One object per placed model; repeats share mesh data"),
            ("MERGED", "Single mesh", "Bake everything into one object (fast, heavy)")),
            default="OBJECTS")
        vcol: BoolProperty(name="Baked vertex colours", default=True,
                           description="Tint models with the city's per-vertex palette")
        vcol_gain: FloatProperty(name="Vertex colour gain", default=2.0, min=0.1, max=4.0,
                                 description="PS2 vertex colours are 'modulate x2': 0.5 is neutral")
        glow: FloatProperty(name="Window glow", default=2.0, min=0.0, max=20.0,
                            description="Emission strength of lit windows")
        lods: BoolProperty(name="Include LOD variants", default=False,
                           description="Models stored in LOD slots 1-9 that have no level-0 mesh")
        orphans: BoolProperty(name="Include unreachable meshes", default=False,
                              description="Meshes the object graph does not reach get the position "
                                          "of their nearest neighbour: a guess, not file data")

    def _load_bytes(s):
        path = bpy.path.abspath(s.path)
        if path.lower().endswith(".pck"):
            with open(path, "rb") as f:
                return f.read(), bpy.path.display_name_from_filepath(path)
        if not s.entries:
            raise ValueError("Scan the ISO / ASSETS.DAT first, then pick a city.")
        item = s.entries[s.active]
        af, raw = open_assets(path)          # .DAT directly, or inside an ISO
        try:
            ents = dave_entries(af)
            en = ents[item.index]
            return dave_read(af, en), item.name.split("/")[-1].rsplit(".", 1)[0]
        finally:
            raw.close()

    def _do_import(op, context):
        s = context.scene.mc3_city
        wm = context.window_manager
        wm.progress_begin(0, 100)
        try:
            data, label = _load_bytes(s)
            wm.progress_update(20)
            meshes, stats = parse_city(data, s.lods)
            if not meshes:
                raise ValueError("No city meshes found in %s. Is it a city .pck "
                                 "(resources/city/<city>.pck, header type 67)?" % label)
            wm.progress_update(50)
            fulltex = None
            tp = bpy.path.abspath(s.texpath) if s.texpath else ""
            if tp:
                blobs = read_texture_packs(tp, label)
                minfo = {}
                fulltex = match_full_textures(stats["thumbs"], blobs,
                                              s.tex_tol, minfo)
                op.report({"INFO"}, "Full-size textures: %d of %d matched"
                          % (len(fulltex), len(stats["thumbs"])))
                print("MC3 texture match: scanned %s, unmatched closeness %s, "
                      "flat/near-flat unmatched thumbnails %s"
                      % (minfo.get("scanned"), minfo.get("unmatched_bins"),
                         minfo.get("unmatched_flat")))
            wm.progress_update(70)
            n_obj, n_tri = build_city(context, meshes, "MC3_" + label, stats,
                                      s.z_up, s.scale, s.mode == "MERGED", s.orphans,
                                      s.vcol, s.vcol_gain, s.glow, fulltex)
            op.report({"INFO"}, "%s: %d objects, %d triangles, %d%% textured "
                                "(%d meshes parsed, %d not reachable through the graph)"
                      % (label, n_obj, n_tri,
                         100 * stats["textured_tris"] // max(stats["tris"], 1),
                         stats["meshes"], stats["orphans"]))
        except Exception as e:
            op.report({"ERROR"}, str(e))
            return {"CANCELLED"}
        finally:
            wm.progress_end()
        return {"FINISHED"}

    class MC3_OT_scan(Operator):
        bl_idname = "mc3.scan_archive"
        bl_label = "Scan ISO / ASSETS.DAT"
        bl_description = "List the city .pck files inside the ISO or archive"

        def execute(self, context):
            s = context.scene.mc3_city
            try:
                ents = list_city_entries(bpy.path.abspath(s.path))
            except Exception as e:
                self.report({"ERROR"}, str(e))
                return {"CANCELLED"}
            s.entries.clear()
            if not ents:
                self.report({"ERROR"}, "No city .pck files found in this archive. "
                                       "Is it the right ISO / ASSETS.DAT?")
                return {"CANCELLED"}
            for e in ents:
                it = s.entries.add()
                it.name, it.index, it.size = e["name"], e["i"], e["full"]
            s.active = 0
            self.report({"INFO"}, "%d city file(s) found" % len(ents))
            return {"FINISHED"}

    class MC3_OT_import(Operator):
        bl_idname = "mc3.import_city"
        bl_label = "Import city"
        bl_options = {"REGISTER", "UNDO"}

        def execute(self, context):
            return _do_import(self, context)

    class IMPORT_SCENE_OT_mc3_city(Operator, ImportHelper):
        bl_idname = "import_scene.mc3_city"
        bl_label = "Import MC3 City"
        bl_options = {"REGISTER", "UNDO"}
        filename_ext = ".pck"
        filter_glob: StringProperty(default="*.pck;*.iso;*.dat;*.bin", options={"HIDDEN"})

        def execute(self, context):
            s = context.scene.mc3_city
            s.path = self.filepath
            if not self.filepath.lower().endswith(".pck"):
                r = bpy.ops.mc3.scan_archive()
                if "FINISHED" not in r:
                    return {"CANCELLED"}
                self.report({"INFO"}, "Archive scanned: pick a city in the MC3 sidebar "
                                      "tab and press Import city.")
                return {"FINISHED"}
            return _do_import(self, context)

        def draw(self, context):
            pass

    class MC3_UL_cities(UIList):
        def draw_item(self, context, layout, data, item, icon, active_data,
                      active_propname, index):
            layout.label(text=item.name, icon="WORLD")
            layout.label(text="%.1f MB" % (item.size / 1048576.0))

    class MC3_PT_panel(Panel):
        bl_label = "Midnight Club 3 City"
        bl_idname = "MC3_PT_panel"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "MC3"

        def draw(self, context):
            s = context.scene.mc3_city
            lay = self.layout
            lay.prop(s, "path")
            lay.prop(s, "texpath")
            if s.texpath:
                lay.prop(s, "tex_tol")
            is_pck = s.path.lower().endswith(".pck")
            if not is_pck:
                lay.operator("mc3.scan_archive", icon="VIEWZOOM")
                lay.template_list("MC3_UL_cities", "", s, "entries", s, "active", rows=6)
            col = lay.column(align=True)
            col.prop(s, "z_up")
            col.prop(s, "scale")
            col.prop(s, "mode", text="")
            col.prop(s, "vcol")
            col.prop(s, "vcol_gain")
            col.prop(s, "glow")
            col.prop(s, "lods")
            col.prop(s, "orphans")
            row = lay.row()
            row.scale_y = 1.4
            row.operator("mc3.import_city", icon="IMPORT")

    def _menu(self, context):
        self.layout.operator(IMPORT_SCENE_OT_mc3_city.bl_idname,
                             text="Midnight Club 3 City (ISO / ASSETS.DAT / .pck)")

    _classes = (MC3_CityEntry, MC3_Settings, MC3_OT_scan, MC3_OT_import,
                IMPORT_SCENE_OT_mc3_city, MC3_UL_cities, MC3_PT_panel)

    def register():
        for c in _classes:
            bpy.utils.register_class(c)
        bpy.types.Scene.mc3_city = bpy.props.PointerProperty(type=MC3_Settings)
        bpy.types.TOPBAR_MT_file_import.append(_menu)

    def unregister():
        bpy.types.TOPBAR_MT_file_import.remove(_menu)
        del bpy.types.Scene.mc3_city
        for c in reversed(_classes):
            bpy.utils.unregister_class(c)

    if __name__ == "__main__":
        register()
