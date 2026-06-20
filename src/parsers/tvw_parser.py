# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC
#
# TVW format research starting point: https://github.com/inflex/teboviewformat
# (MIT, Copyright (c) 2021 Paul Daniels). The pioneering reverse engineering
# done there is the basis from which this independent decode was developed —
# the implementation below does not include or derive from teboviewformat's
# source, but credit is due. See THIRD_PARTY_NOTICES.md for the full courtesy
# reproduction of the upstream MIT notice.

"""
Best-effort parser for Teboview / TVW boardview files (Gigabyte/Lenovo etc).

What we extract (reverse-engineered from cross-board comparison of Z490,
X570, B550, and BoardViewer.exe ground-truth on individual chips):

  - File-wide chip-header records: each chip is preceded by a 0x01 marker,
    a Pascal device-name string, a 2-byte zero gap, and a Pascal footprint
    name. The 32 bytes before the marker hold (x_ref, y_ref, x_place, rot,
    type_idx, instance, 0, 0) as i32 LE — `x_place` is the chip body's
    centre coordinate.
  - Real silkscreen refdes (PCH, M2A_SB, U32, ...) — found as a Pascal
    string ~32-58 bytes before the chip-header marker. ~99% coverage on
    Z490, ~95% on X570/B550. Falls back to auto-generated refdes from
    footprint family if not found.
  - Per-chip pin lists (BGA-style names like "A4" or numeric names "1"..N)
    in the L+17 record format. Pin meta = (0, counter, 0, pin_no) as 4
    i32 LE; counter increments by 8 per record.
  - Per-chip layer/side flag from trailer byte 9 after the footprint
    name (0x02 = TOP, anything else = BOTTOM).
  - Pin grid synthesis: BGA names parsed into (column, row) for proper
    grid layout; numeric names distributed around the perimeter.
  - Footprint sizing calibrated to file units (1 unit ≈ 0.000325 mm,
    derived from board span ≈ ATX 305mm vs 938k chip-position units).
  - Net name table (3015 packed Pascal strings on Z490) at file offset
    ~+5987078 — kept as a list for reference.

Pin → net mapping (formerly the missing piece, now solved):

  - The mapping is encoded as fixed-size 38-byte pad records embedded
    inside the Custom_35 (TOP) and Custom_17 (BOTTOM) trace-data blocks,
    not in any obvious table. Each record carries the chip-relative pad
    XY plus the net id at +22 (key into the net-name table). A 54-byte
    variant appears with 16 extra bytes inserted before the coordinates.
    See the scanner/decoder a few hundred lines below this docstring,
    and TVW_FORMAT.md for the full layout. Verified on Z490 / B550 /
    X570 / GV-N780OC.

Format anchors (verified across 3 boards):

  * Header watermark "O95w-28ps49m 02v9o." identical across all files —
    a fixed format signature.
  * Largest pin run = socket pin count: LGA1200 → 1200 pins, AM4 → 1337
    records (1331 pins + 6 housing markers).
  * Total chip-header count matches expected component count on all
    three Gigabyte boards: Z490 = 2790, X570 = 2124, B550 = 2510.

References:
  - github.com/inflex/teboviewformat (incomplete public RE)
"""

from __future__ import annotations

import math
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .gencad_parser import BoardModel, Component, Shape
from .tvw_master_fp import parse_master_footprints, pins_world_positions

# --------------------------------------------------------------------------
# Optional C-DLL fast path for the two hottest cold-load scanners:
#   _find_chip_headers       (~28% of cold load — whole-file Pascal scan)
#   _parse_pin_records sweep  (~47% of cold load — per-chip probe loop)
#
# The pure-Python implementations below remain the authoritative
# reference and the default whenever the DLL is missing, fails to load,
# or raises. The native path is preferred only when:
#   * tvw_native imports and reports available(), AND
#   * the TVW_DISABLE_NATIVE_PINREC env var is NOT set (the benchmark and
#     correctness harness use it to force the pure-Python fallback).
# Each native call is additionally wrapped in try/except so any runtime
# error transparently falls back to Python for that call.
#
# The native functions were verified BIT-IDENTICAL to the pure-Python
# originals (full BoardModel hash match on Gigabyte X570) before this
# wiring was committed — see _verify_pinrec.py.
try:  # pragma: no cover - import guard
    from . import tvw_native as _tvw_native
except Exception:  # noqa: BLE001 - any import failure disables the fast path
    _tvw_native = None


def _native_pinrec_enabled() -> bool:
    """True when the C fast path for chip-headers / pin-record sweep
    should be used. Pure-Python otherwise."""
    if _tvw_native is None:
        return False
    if os.environ.get("TVW_DISABLE_NATIVE_PINREC"):
        return False
    try:
        return bool(_tvw_native.available())
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# Footprint → refdes family mapping
# --------------------------------------------------------------------------

def footprint_to_family(fp: str) -> str:
    """Map a TVW footprint name to a refdes prefix."""
    f = fp.upper()
    # Resistors first (must come before Q since R0... etc.)
    if f.startswith(("R0", "R8P", "R-")):
        return "R"
    if f.startswith(("C0", "C1", "EC", "TANT")):
        return "C"
    if f.startswith(("Q_", "TDSON", "SOT")):
        return "Q"
    if f.startswith(("DIODE", "LED")) or "DIODE" in f:
        return "D"
    if f.startswith(("CHOKE", "FERRI", "INDUCT", "EMI", "BEAD", "L0")):
        return "L"
    if f.startswith(("FUSE", "PTC")):
        return "F"
    if f.startswith(("OSC", "XTAL", "CRYSTAL", "Y_")):
        return "Y"
    if f.startswith(("BAT", "CR2032", "BAT-")):
        return "BAT"
    if f.startswith(("HOLE", "MH-", "MHOLE")):
        return "MH"
    if f.startswith(("TESTPT", "TP-", "TESTPOINT")):
        return "TP"
    if f.startswith(("PCIESLOT", "USB", "M2_", "WIFI", "PIN1X", "TPM",
                     "BH", "PH", "RJ45", "AUDIO_J", "DCJ", "SOCKET",
                     "F_PANEL", "ATX_", "JACK")):
        return "J"
    if f.startswith(("IC", "BGA", "LGA", "FH", "FCBGA", "QFN", "QFP",
                     "SOIC", "TQFP", "DFN", "MSOP", "SSOP", "PDIP",
                     "SOT223", "TSSOP")):
        return "U"
    return "X"  # unknown family


# --------------------------------------------------------------------------
# Header / chip-header parsing
# --------------------------------------------------------------------------

def _is_pascal(buf: bytes, off: int, min_len: int = 3,
               max_len: int = 40) -> Optional[str]:
    if off + 1 >= len(buf):
        return None
    L = buf[off]
    if not (min_len <= L <= max_len) or off + 1 + L > len(buf):
        return None
    s = buf[off+1:off+1+L]
    if not all(0x20 <= b < 0x7f for b in s):
        return None
    return s.decode('latin-1')


def _find_chip_headers(buf: bytes) -> List[Dict]:
    """Find every chip-header location: 0x01 + Pascal + 0..4 zero pad + Pascal.

    Returns dicts with off, dev_name, footprint, after_off (byte after
    second Pascal string).

    The dev_name string can be quite long for connectors (up to ~80 chars,
    e.g. "PCI-E/16X-164P/BK/LONG DOUBLE/HK*2/SHELL/GEN4.0" is 47 chars).
    Allow up to 80 chars for s1; the footprint s2 is shorter and stricter.
    """
    out = []
    n = len(buf)
    for i in range(0, n - 50):
        if buf[i] != 0x01:
            continue
        # Allow long dev_name strings — connector names can be 40+ chars
        s1 = _is_pascal(buf, i + 1, min_len=3, max_len=80)
        if not s1:
            continue
        end_s1 = i + 1 + 1 + len(s1)
        # Skip 0..4 zero pad bytes between strings
        gap = end_s1
        while gap < end_s1 + 4 and gap < n and buf[gap] == 0:
            gap += 1
        s2 = _is_pascal(buf, gap)
        if not s2:
            continue
        # Filter footprint to plausible characters
        if not all(c.isalnum() or c in '/_-+.' for c in s2):
            continue
        out.append({
            'off': i,
            'dev_name': s1,
            'footprint': s2,
            'after_off': gap + 1 + len(s2),
        })
    return out


def _decode_refdes(buf: bytes, marker_off: int) -> Optional[str]:
    """Look ~30..60 bytes before the chip-header 0x01 marker for a
    Pascal-prefixed refdes string ("PCH", "M_BIOS", "U32", etc.).

    Empirically the refdes lives 32-58 bytes before the marker, in the
    region between any number-format pin records and the 32-byte chip
    pre-header. Length is typically 1-8 chars, starts with a letter,
    contains only alphanumeric / `_` / `-`.
    """
    for off in range(max(0, marker_off - 60), marker_off - 30):
        L = buf[off]
        if not (1 <= L <= 12):
            continue
        s = buf[off+1:off+1+L]
        if not all(0x20 <= b < 0x7f for b in s):
            continue
        try:
            sd = s.decode('latin-1')
        except UnicodeDecodeError:
            continue
        # Refdes must start with a letter, be alphanumeric+underscore+hyphen,
        # and not be a pure number (those are pin-number records).
        if not sd or not sd[0].isalpha():
            continue
        if not all(c.isalnum() or c in '_-' for c in sd):
            continue
        if sd.isdigit():
            continue
        return sd
    return None


def _decode_layer(buf: bytes, after_off: int) -> str:
    """Read the per-chip layer/side flag from the trailer.

    Empirically (cross-validated on Z490, X570, B550): trailer byte 9
    has bimodal distribution with `0x02` always denoting TOP and any
    non-2 value (`0x05` on X570, `0x07` on Z490/B550) denoting BOTTOM.
    Big chips like LGA1200 / AM4 / chipset BGAs are consistently TOP;
    R/C/Q footprints have a mix, matching real-board reality where
    most passives sit on TOP with a few hundred on BOTTOM.
    """
    if after_off + 10 > len(buf):
        return "TOP"
    flag = buf[after_off + 9]
    return "TOP" if flag == 0x02 else "BOTTOM"


def _decode_position(buf: bytes, marker_off: int) -> Tuple[int, int, int]:
    """Decode the 32 bytes before the chip-header 0x01 marker.

    Layout (eight i32 LE), cross-validated visually against BoardViewer.exe's
    rendering and against the pad-record coordinates (with pad-XY swap):
        [0] alternate chip Y (close to [2])
        [1] chip body Y (world coords)
        [2] chip body X (world coords)
        [3] rotation in degrees (0/90/180/270)
        [4] device-type / library index (small int, same across instances)
        [5] instance counter / variant number
        [6,7] zeros

    The pad records, despite their fields APPEARING as (X, Y) at offsets
    +30/+34, actually store (Y, X) — see _extract_pads. With that swap
    applied, pad and chip XYs share the same coord space.

    Returns (x, y, rotation).
    """
    if marker_off < 32:
        return (0, 0, 0)
    pre = buf[marker_off-32:marker_off]
    try:
        i32s = struct.unpack('<8i', pre)
    except struct.error:
        return (0, 0, 0)
    _y_alt, chip_y, chip_x, rot = i32s[0], i32s[1], i32s[2], i32s[3]
    # Snap rotation to nearest 90°
    if rot not in (0, 90, 180, 270):
        rot = (rot // 90 * 90) if 0 <= rot < 360 else 0
    return (chip_x, chip_y, rot)


def _parse_pin_records(buf: bytes, start: int, end_limit: int,
                       max_records: int = 4000) -> Tuple[List, int]:
    """Read L+17 pin records starting at `start`, bounded by `end_limit`
    (typically the next chip header offset). Stop when we hit an invalid
    record. Returns (records, end_off).

    Each pin record:
        1 byte length L
        L bytes pin name (e.g. "A4", "AC18", "1", "2")
        16 bytes meta (4× i32 LE):
            meta[0] = pin X (chip-relative)
            meta[1] = pin Y (chip-relative)
            meta[2] = layer flag (0 = top, 1 = bottom; small)
            meta[3] = sequential pin index (with gaps)

    Validation: pin coords must be < 1,000,000 (= 1m at 0.001mm units).
    A meta with garbage values terminates the run.
    """
    out = []
    i = start
    n = min(end_limit, len(buf))
    valid_chars = set(
        b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
        b'0123456789_#-+.'
    )
    COORD_MAX = 1_000_000  # ~1m at 0.001mm units
    while i < n - 17 and len(out) < max_records:
        L = buf[i]
        if L < 1 or L > 20 or i + 1 + L + 16 > n:
            break
        name_bytes = buf[i+1:i+1+L]
        if not all(b in valid_chars for b in name_bytes):
            break
        b0 = name_bytes[0]
        if not (0x30 <= b0 <= 0x39 or 0x41 <= b0 <= 0x5a or 0x61 <= b0 <= 0x7a):
            break
        meta = struct.unpack_from('<4i', buf, i+1+L)
        # Validate: pin coords reasonable, layer flag small, idx small
        if abs(meta[0]) > COORD_MAX or abs(meta[1]) > COORD_MAX:
            break
        if not (-2 <= meta[2] <= 32):  # layer/flag should be small
            break
        if not (0 <= meta[3] <= 200_000):  # pin index reasonable
            break
        out.append({
            'name': name_bytes.decode('latin-1'),
            'x': meta[0], 'y': meta[1],
            'flag': meta[2], 'idx': meta[3],
        })
        i += 1 + L + 16
    return out, i


# --------------------------------------------------------------------------
# Native-preferring wrappers (C fast path with pure-Python fallback)
# --------------------------------------------------------------------------
#
# These wrap the two pure-Python scanners above. When the C DLL is
# available and not disabled, they delegate to it; otherwise (or on any
# native error) they run the identical pure-Python logic. The pure-Python
# functions remain the authoritative reference and the default.

def _chip_headers(buf: bytes) -> List[Dict]:
    """Find chip headers, preferring the C fast path. Identical result to
    `_find_chip_headers`."""
    if _native_pinrec_enabled():
        try:
            res = _tvw_native.find_chip_headers(buf)
            if res is not None:
                return res
        except Exception:  # noqa: BLE001 - fall back to pure-Python
            pass
    return _find_chip_headers(buf)


def _best_pin_run(buf: bytes, pin_search_start: int, next_marker: int
                  ) -> List[Dict]:
    """Run the per-chip 0..64 probe-and-parse sweep and return the best
    (longest) pin-record run — identical to the legacy driver loop:

        best = []
        for probe in range(0, 64):
            tentative = pin_search_start + probe
            if tentative >= next_marker: break
            recs, _ = _parse_pin_records(buf, tentative, next_marker, 4000)
            if len(recs) > len(best): best = recs
            if len(recs) >= 50: break
        return best

    Prefers the C fast path (one native call folds the whole sweep);
    falls back to the pure-Python loop on absence or error.
    """
    if _native_pinrec_enabled():
        try:
            res = _tvw_native.parse_pin_records_sweep(
                buf, pin_search_start, next_marker,
                probe_max=64, early_exit=50, max_records=4000,
            )
            if res is not None:
                return res
        except Exception:  # noqa: BLE001 - fall back to pure-Python
            pass
    # Pure-Python fallback: the original driver loop, verbatim.
    best: List[Dict] = []
    for probe in range(0, 64):
        tentative = pin_search_start + probe
        if tentative >= next_marker:
            break
        recs, _end = _parse_pin_records(
            buf, tentative, end_limit=next_marker, max_records=4000,
        )
        if len(recs) > len(best):
            best = recs
        if len(recs) >= 50:  # plenty for any chip; stop early
            break
    return best


# --------------------------------------------------------------------------
# Pin → net mapping
# --------------------------------------------------------------------------
#
# The pin-net mapping lives inside the Custom_35 (TOP) and Custom_17 (BOTTOM)
# trace-data blocks as 38-byte fixed-size pad records. Each record:
#
#   [0..3]   uint32  pad shape signature (varies; e.g. 0x01010001)
#   [4..19]  i32[4]  pad geometry (bbox / dimensions)
#   [20..21] uint16  zeros — used to detect record-stride alignment
#   [22..25] uint32  net_id (key into net name table) ★ the prize
#   [26..29] uint32  pad_type / shape category enum (small int)
#   [30..33] int32   pad world X
#   [34..37] int32   pad world Y
#
# The net name table is a flat run of packed Pascal strings, locatable as
# the longest such run in the file. Indices into it match the net_id field.
#
# To populate (refdes, pin) → net for the BoardModel:
#   1. Find the net name table.
#   2. Scan whole file for runs of >=50 consecutive 38-byte records that
#      have 00 00 at +20 and small values at +22, +26.
#   3. For each chip, find pads inside its bbox, then assign each pad's
#      net to the closest synthesized pin position.

def _find_net_table(buf: bytes) -> Tuple[int, int]:
    """Find the longest run of consecutive packed Pascal strings in `buf`.
    Returns (start, end). The longest such run is the net name table.

    Strategy: scan candidate start offsets, walk forward counting valid
    Pascal strings, take the longest run. A pre-built printable mask
    skips the inner all-bytes-printable check via C-speed `bytes.find`.
    Early-exit once a run of >= 1000 strings is found — the net table
    on real boards has thousands of entries; nothing else comes close.
    """
    # Native fast path — `tvw_native.find_net_table` is a faithful port
    # that runs ~100× faster. Falls through to Python below if the DLL
    # isn't available.
    try:
        from .tvw_native import find_net_table as _nat_find_net_table
        result = _nat_find_net_table(buf)
        if result is not None:
            return result
    except Exception:
        pass
    n = len(buf)
    # Bitmap of bytes outside [0x21, 0x7e] — '\x00' = printable, '\xff' = not.
    # `mask.find(b'\xff', start, end)` then tells us whether any byte in a
    # slice is non-printable in C speed.
    table = bytes(0x00 if 0x21 <= b < 0x7f else 0xff for b in range(256))
    mask = buf.translate(table)

    best_start = best_end = -1
    best_count = 0
    EARLY_EXIT = 1000
    i = 0
    while i < n - 1:
        L = buf[i]
        # Quick filter: byte at i must be a plausible Pascal length
        if not (3 <= L <= 80) or i + 1 + L > n:
            i += 1
            continue
        # Walk forward counting valid Pascals
        run_count = 0
        cur = i
        while cur < n - 1:
            L2 = buf[cur]
            if not (1 <= L2 <= 80) or cur + 1 + L2 > n:
                break
            # All bytes in the candidate string must be printable.
            # mask is 0x00 for printable, 0xff otherwise — find returns
            # -1 if no \xff in the slice.
            if mask.find(b'\xff', cur + 1, cur + 1 + L2) >= 0:
                break
            run_count += 1
            cur += 1 + L2
        if run_count > best_count:
            best_count = run_count
            best_start = i
            best_end = cur
            if run_count >= EARLY_EXIT:
                return best_start, best_end
            if run_count > 200:
                i = cur
                continue
        i += 1
    return best_start, best_end


def _build_net_index(buf: bytes, start: int, end: int) -> List[str]:
    """Walk packed Pascal strings inside [start, end) and return list of
    decoded names. Index in the list = net_id."""
    out: List[str] = []
    i = start
    while i < end:
        L = buf[i]
        if L == 0:
            i += 1
            continue
        if not (1 <= L <= 80) or i + 1 + L > end:
            break
        s = buf[i+1:i+1+L]
        if not all(0x20 <= b < 0x7f for b in s):
            break
        out.append(s.decode('latin-1'))
        i += 1 + L
    return out


def _find_pad_runs(
    buf: bytes, min_run: int = 50,
) -> List[Tuple[int, int, int]]:
    """Scan the whole file for runs of consecutive pad records.

    Two formats coexist in TVW files:
      - 38-byte records (TOP-layer pads, mostly in Custom_35).
        Sentinel: `00 00` at +20. net_id at +22, pad_type at +26,
        Y at +30, X at +34.
      - 54-byte records (BOTTOM-layer pads, in Custom_17).
        Same as 38-byte but with 16 extra bytes inserted between the
        bbox and the per-pad fields. Sentinel: `00 00` at +36.
        net_id at +38, pad_type at +42, Y at +46, X at +50.

    Returns list of (start, end, stride) tuples for each run.

    Both formats use bytes.find on the sentinel `\\x00\\x00` to skip
    quickly to candidate positions in C speed.
    """
    # Native fast path — ~60× speedup. See tvw_native.c.
    try:
        from .tvw_native import find_pad_runs as _nat_find_pad_runs
        result = _nat_find_pad_runs(buf, min_run=min_run)
        if result is not None:
            return result
    except Exception:
        pass
    n = len(buf)
    runs: List[Tuple[int, int, int]] = []

    for stride, sentinel_off in [(38, 20), (54, 36)]:
        net_off = sentinel_off + 2     # net_id position
        pad_type_off = net_off + 4     # pad_type position
        i = 0
        while i + stride <= n:
            # Jump to next \x00\x00 candidate at the sentinel offset
            zero_at = buf.find(b'\x00\x00', i + sentinel_off)
            if zero_at < 0:
                break
            i = zero_at - sentinel_off
            if i < 0:
                i = zero_at + 1
                continue
            cur = i
            count = 0
            while cur + stride <= n:
                if buf[cur+sentinel_off:cur+sentinel_off+2] != b'\x00\x00':
                    break
                net_id = struct.unpack_from('<I', buf, cur + net_off)[0]
                pad_type = struct.unpack_from('<I', buf, cur + pad_type_off)[0]
                if net_id >= 4000 or pad_type >= 100_000:
                    break
                count += 1
                cur += stride
            if count >= min_run:
                runs.append((i, cur, stride))
                i = cur
            else:
                i += 1
    return runs


def _extract_pads(buf: bytes,
                   runs: List[Tuple[int, int, int]]) -> List[Dict]:
    """Pull every pad record from the discovered runs. Each pad is a
    dict(x, y, net_id, pad_type, stride).

    Two formats supported (see `_find_pad_runs`): 38-byte (TOP layer)
    and 54-byte (BOTTOM layer). Field offsets within each record:
      38-byte: net_id@+22, pad_type@+26, Y@+30, X@+34
      54-byte: net_id@+38, pad_type@+42, Y@+46, X@+50

    NOTE on field order: pad records store coordinates as (Y, X) at the
    given byte offsets — not the more obvious (X, Y) order. Verified by
    matching pad coordinate ranges to chip pre-32 i32[2]=X and i32[1]=Y
    ranges across all 3 sample boards (Z490, B550, X570).
    """
    out: List[Dict] = []
    for s, e, stride in runs:
        if stride == 38:
            net_off, pad_type_off, y_off, x_off = 22, 26, 30, 34
        elif stride == 54:
            net_off, pad_type_off, y_off, x_off = 38, 42, 46, 50
        else:
            continue
        for k in range((e - s) // stride):
            off = s + k * stride
            out.append({
                'y':        struct.unpack_from('<i', buf, off + y_off)[0],
                'x':        struct.unpack_from('<i', buf, off + x_off)[0],
                'net_id':   struct.unpack_from('<I', buf, off + net_off)[0],
                'pad_type': struct.unpack_from('<I', buf, off + pad_type_off)[0],
                'stride':   stride,
            })
    return out


def _build_signals(model: BoardModel, buf: bytes,
                   chips_with_pins: List[Dict]) -> int:
    """Populate model.signals with `net_name -> [(refdes, pin_name), ...]`.

    For each chip we read the master-footprint pin positions (`tvw_master_fp`)
    and find the nearest pad to each pin within a tight tolerance. The
    pin's name comes from the file's per-chip pin records (matched by
    index), with numeric fallback when the file gave fewer pin records
    than the master footprint has positions.

    No bbox-based contention, no Pass-N rebalances — every pin maps to
    its own physical pad directly. Pins without a pad within tolerance
    stay unassigned (truthful: we don't know the net).
    """
    nt_start, nt_end = _find_net_table(buf)
    if nt_start < 0:
        return 0
    net_names = _build_net_index(buf, nt_start, nt_end)
    if not net_names:
        return 0

    pads = _extract_pads(buf, _find_pad_runs(buf))
    if not pads:
        return 0

    # Spatial hash on world pad positions. Cell of 5000 file units (~1mm)
    # is comfortably wider than our matching tolerance, so the 3×3 cell
    # neighbourhood covers any candidate within tolerance.
    GRID = 5000
    pad_grid: Dict[Tuple[int, int], List[Dict]] = {}
    for p in pads:
        key = (p['x'] // GRID, p['y'] // GRID)
        pad_grid.setdefault(key, []).append(p)

    # Match tolerance — see tvw_master_fp.py verification (91.8 % at 50
    # file units across Z490 / B550 / X570). Misses are real-world
    # footprint variants (MOSFET land patterns, M.2 mounting holes); we
    # leave those unassigned rather than guess.
    TOL = 50
    TOL_SQ = TOL * TOL

    n_assignments = 0
    for entry in chips_with_pins:
        comp = entry['component']
        shape = entry['shape']
        if not shape.pins:
            continue
        # shape.pins has been populated from master fp coords already
        # (see parse() loop). Project each pin to world via the standard
        # rotation and find the nearest pad in the spatial hash.
        theta = math.radians(comp.rotation)
        ct, st = math.cos(theta), math.sin(theta)
        for pin_name, dx, dy in shape.pins:
            wx = comp.x + dx * ct - dy * st
            wy = comp.y + dx * st + dy * ct
            gx = int(wx) // GRID
            gy = int(wy) // GRID
            best_d2 = float('inf')
            best_pad = None
            for ddx in (-1, 0, 1):
                for ddy in (-1, 0, 1):
                    for q in pad_grid.get((gx + ddx, gy + ddy), ()):
                        d2 = (q['x'] - wx) ** 2 + (q['y'] - wy) ** 2
                        if d2 < best_d2:
                            best_d2 = d2
                            best_pad = q
            if best_pad is None or best_d2 > TOL_SQ:
                continue
            net_id = best_pad['net_id']
            if 0 <= net_id < len(net_names):
                model.signals.setdefault(net_names[net_id], []).append(
                    (comp.refdes, pin_name))
                n_assignments += 1

    return n_assignments


# --------------------------------------------------------------------------
# Public entry point — variant dispatch
# --------------------------------------------------------------------------

# Compal/Lenovo (e.g. Thinkpad NM-B501) TVW files carry a "Region 1"
# layer-flag table where each chip has the constants 0xbb800 / 0x12c00
# packed at after-Pascal +12..+19. The 8-byte signature `b8 0b 00 00 2c
# 01 00 00` is therefore a strong-and-cheap discriminator: Compal files
# contain hundreds of these (one per chip, mostly), Gigabyte files
# contain essentially zero (the bytes are a specific 8-byte constant
# extremely unlikely to occur by chance in trace data or other records).
# See TVW_FORMAT.html §14 (Variant detection) for the full discriminator
# survey and threshold rationale.
_COMPAL_R1_SIGNATURE = b"\xb8\x0b\x00\x00\x2c\x01\x00\x00"
_COMPAL_R1_THRESHOLD = 100


def _detect_variant(data: bytes) -> str:
    """Detect whether a TVW byte buffer is the Gigabyte variant or the
    Compal/Lenovo variant. Returns ``"compal_lenovo"`` or ``"gigabyte"``.

    Default is ``"gigabyte"`` — the historical decoder is calibrated for
    Gigabyte boards, and producing a (potentially wrong) parse via the
    Gigabyte path is preferable to silently returning an empty model if
    the discriminator misclassifies a Gigabyte file. Compal-variant
    detection is intentionally conservative (high threshold) to avoid
    false positives on Gigabyte's GPU files which can contain unusual
    byte patterns in their wider trace regions.
    """
    n = data.count(_COMPAL_R1_SIGNATURE)
    if n > _COMPAL_R1_THRESHOLD:
        return "compal_lenovo"
    return "gigabyte"


def _parse_compal(path: Path) -> BoardModel:
    """Dispatch to the Compal/Lenovo decoder in ``tvw_compal``.

    Local import keeps the Compal module out of the import path for
    Gigabyte-only callers, and means a broken tvw_compal.py can't crash
    the Gigabyte path during module load.
    """
    from ..tvw_compal import parse as parse_compal
    return parse_compal(path)


def parse(path: Path) -> BoardModel:
    """Public entry point. Detects the file's TVW variant and dispatches
    to the appropriate decoder.

    Gigabyte files (Z490, X570, B550, GPU boards) go through the
    historical decoder, unchanged. Compal/Lenovo files (Thinkpad
    NM-B501, etc.) currently return a stub with a warning until the
    Compal decoder lands.
    """
    data = Path(path).read_bytes()
    if _detect_variant(data) == "compal_lenovo":
        return _parse_compal(path)
    return _parse_gigabyte(path)


def _parse_gigabyte(path: Path) -> BoardModel:
    """Gigabyte-variant TVW decoder. Historical body of ``parse()`` —
    no behavioural changes vs the pre-dispatch implementation.
    """
    data = Path(path).read_bytes()
    model = BoardModel()

    chips = _chip_headers(data)
    family_count: Dict[str, int] = {}
    # Collected per-chip parse results, used by _build_signals to map pads
    # back to pins after all chips are placed.
    chips_with_pins: List[Dict] = []

    # Parse the master footprint table once — gives us each footprint
    # type's pin positions in footprint-local coords. Combined with each
    # chip instance's XY + rotation, this yields exact world pin
    # positions, which `_build_signals` then matches against pad records
    # to recover net IDs. Verified at 91.8 % match within 50 file units
    # across Z490 / B550 / X570 (see tvw_master_fp.py).
    master_fps = parse_master_footprints(data)

    # Compute next-chip-header bounds so the per-chip pin parser doesn't
    # bleed into a neighbouring chip's records.
    bounds = [c['off'] for c in chips] + [len(data)]

    for ci, chip in enumerate(chips):
        marker = chip['off']
        x, y, rot = _decode_position(data, marker)
        layer = _decode_layer(data, chip['after_off'])

        # Try the real silkscreen refdes (Pascal string before the chip
        # pre-header). If we can't find it, auto-generate from footprint
        # family + sequential number.
        real_refdes = _decode_refdes(data, marker)
        if real_refdes:
            refdes = real_refdes
            # Track family count anyway for fallbacks
            family = footprint_to_family(chip['footprint'])
            family_count[family] = family_count.get(family, 0) + 1
        else:
            family = footprint_to_family(chip['footprint'])
            family_count[family] = family_count.get(family, 0) + 1
            refdes = f"{family}{family_count[family]}"

        # Avoid duplicate refdeses (could happen if two chips share a
        # silkscreen label or the heuristic mis-fires). Append a suffix
        # if the name is taken.
        base = refdes
        suffix = 1
        while refdes in model.components:
            suffix += 1
            refdes = f"{base}#{suffix}"

        # Search for the pin record run between this chip's trailer and the
        # next chip header. Probe each starting offset; pick the longest
        # valid run.
        pin_search_start = chip['after_off']
        next_marker = bounds[ci + 1]
        # Fold the whole 0..64 probe-and-parse sweep into one call. With
        # the C DLL this is a single native call per chip; otherwise it
        # runs the identical pure-Python driver loop. Result is the
        # longest valid pin-record run found across the probe offsets.
        best_pins: List = _best_pin_run(data, pin_search_start, next_marker)

        shape_name = f"_tvw_{chip['footprint']}_{family_count[family]}"
        shape = Shape(name=shape_name)

        # Populate shape.pins from the master footprint table when this
        # footprint is in there (~99 % of chips). Each master-fp pin has
        # a known position in footprint-local coords; we transform to
        # world via `pins_world_positions`, then convert back to the
        # chip-local frame the renderer expects (so the renderer's
        # standard rotation reproduces the world position). Pin names
        # come from the file's per-chip pin records by index, with
        # numeric fallback when the file gave fewer records than the
        # master fp has positions. Footprints not in the master table
        # (rare — odd legacy variants) fall back to the perimeter / BGA
        # synthesizer just like before.
        master_pins = master_fps.get(chip['footprint'])
        if master_pins:
            theta_inv = math.radians(-rot)
            cti, sti = math.cos(theta_inv), math.sin(theta_inv)
            world_pins = pins_world_positions(
                chip['footprint'], (x, y), rot, master_fps,
            )
            existing_names = {p['name'] for p in best_pins}
            new_pin_list: List[Tuple[str, float, float]] = []
            next_num = 1
            for pin_idx, wx, wy in world_pins:
                # World → chip-local-as-renderer-expects.
                rx = wx - x
                ry = wy - y
                dx = rx * cti - ry * sti
                dy = rx * sti + ry * cti
                if pin_idx < len(best_pins):
                    pin_name = best_pins[pin_idx]['name']
                else:
                    while str(next_num) in existing_names:
                        next_num += 1
                    pin_name = str(next_num)
                    existing_names.add(pin_name)
                    next_num += 1
                new_pin_list.append((pin_name, dx, dy))
            shape.pins = new_pin_list
            if new_pin_list:
                xs = [p[1] for p in new_pin_list]
                ys = [p[2] for p in new_pin_list]
                shape.bbox_override = (
                    min(xs), min(ys), max(xs), max(ys),
                )
        else:
            # Footprint not in master table — fall back to the legacy
            # synthesizer so the chip still renders something. Pin↔net
            # matching for these chips will get whatever the synthesized
            # geometry happens to align with — same as before this fix.
            w, h = _footprint_size(chip['footprint'])
            if rot in (0, 180):
                if w > h:
                    w, h = h, w
            else:
                if h > w:
                    w, h = h, w
            shape.bbox_override = (-w / 2, -h / 2, w / 2, h / 2)
            expected = _expected_pin_count(chip['footprint'], chip['dev_name'])
            if expected and len(best_pins) < expected:
                existing_names = {p['name'] for p in best_pins}
                next_num = 1
                while len(best_pins) < expected:
                    while str(next_num) in existing_names:
                        next_num += 1
                    best_pins.append({
                        'name': str(next_num),
                        'x': 0, 'y': 0, 'flag': 0, 'idx': next_num,
                    })
                    existing_names.add(str(next_num))
                    next_num += 1
            if best_pins:
                _populate_synth_pin_positions(shape, best_pins, w, h)

        model.shapes[shape_name] = shape
        comp = Component(
            refdes=refdes, x=float(x), y=float(y),
            layer=layer,
            rotation=float(rot),
            shape=shape_name,
            device=chip['dev_name'],
        )
        model.components[refdes] = comp
        chips_with_pins.append({
            'component': comp,
            'shape':     shape,
            'footprint': chip['footprint'],
            'dev_name':  chip['dev_name'],
        })

    # Decode pin → net mapping from the pad records buried in the
    # Custom_35 / Custom_17 trace blocks. See `_build_signals` for the
    # format and matching strategy.
    _build_signals(model, data, chips_with_pins)

    # Attach a lazy trace-topology loader. The TraceGraph build is heavy
    # (3-6 s per board); many user sessions never need it (schematic-
    # only walks, BRD-style boards, etc.) so we register a thunk that
    # constructs it on first access. Imports happen inside the lambda
    # so a broken `tvw_topology` doesn't prevent the basic parse from
    # succeeding — callers just get `topology_available is False` until
    # the thunk is fixed.
    tvw_path = Path(path)

    def _build_topology():
        from ..tvw_topology import TraceGraph  # local import — see above
        return TraceGraph.from_file(str(tvw_path))

    model._topology_loader = _build_topology

    return model


def _expected_pin_count(fp: str, dev_name: str = "") -> int | None:
    """Return the expected pin count for a chip from footprint + dev_name,
    or None if undetermined.

    Used both to synthesise missing pins when the per-instance pin-record
    parser found fewer than the footprint dictates, AND in the Pass 2
    rebalance to detect chips that over-claimed pads from a containing
    connector."""
    # M.2 connector pin count lives in dev_name (e.g. 'M2/67/BK/...'),
    # not in the footprint (which encodes module length / key cut).
    if dev_name:
        m = re.search(r'^M2/(\d+)/', dev_name)
        if m:
            return int(m.group(1))
    f = fp.upper()
    # 2-pin passives
    if any(x in f for x in ("0201", "0402", "0603", "0805", "1206", "1210",
                             "EC", "TANT", "FUSE", "CHOKE", "FERRI", "BEAD",
                             "INDUCT", "DIODE_SOD")):
        return 2
    # SOT23 / 3-pin transistors
    if "SOT23" in f or "SOT-23" in f or "TO252" in f:
        return 3
    # SOT223 = 4-pin
    if "SOT223" in f or "SOT-223" in f:
        return 4
    # Crystal — typically 4 pin SMD
    if "OSC" in f or "XTAL" in f or "CRYSTAL" in f:
        return 4
    # Connector / slot footprints
    m = (re.search(r'PCIESLOT-(\d+)', f)
         or re.search(r'DDR\d*-(\d+)P', f)
         or re.search(r'^ATXPWR_(\d+)', f)
         or re.search(r'^ATX_(\d+)-', f))
    if m:
        return int(m.group(1))
    m = re.search(r'^ATXPW(\d+)X(\d+)', f)
    if m:
        return int(m.group(1)) * int(m.group(2))
    # IC packages with explicit pin count
    m = re.search(r'IC(\d+)', f) or re.search(r'(\d+)QFN', f) \
        or re.search(r'(\d+)QFP', f) or re.search(r'(\d+)TQFN', f) \
        or re.search(r'(\d+)TQFP', f) or re.search(r'(\d+)DFN', f) \
        or re.search(r'(\d+)SOIC', f) or re.search(r'SOIC(\d+)', f) \
        or re.search(r'MSOP(\d+)', f) or re.search(r'SSOP(\d+)', f) \
        or re.search(r'TSSOP(\d+)', f) or re.search(r'PIN1X(\d+)', f) \
        or re.search(r'WSON(\d+)', f) or re.search(r'TDSON(\d+)', f)
    if m:
        return int(m.group(1))
    return None


# BGA column-letter sequence — skips I, O, Q to avoid digit-confusion
# (Intel/JEDEC convention)
_BGA_COL_LETTERS = "ABCDEFGHJKLMNPRSTUVWY"  # 21 letters


def _bga_column_index(letter: str) -> int | None:
    """Convert a BGA column label like 'A', 'AC', 'BAA' to a 0-based index.

    Uses the JEDEC-friendly alphabet (no I/O/Q). Returns None if the
    label doesn't look like a BGA column letter sequence.
    """
    if not letter or not letter.isalpha() or not letter.isupper():
        return None
    idx = 0
    for ch in letter:
        ci = _BGA_COL_LETTERS.find(ch)
        if ci < 0:
            return None
        idx = idx * len(_BGA_COL_LETTERS) + (ci + 1)
    return idx - 1


_BGA_NAME_RE = re.compile(r'^([A-HJ-NPR-Y]+)(\d+)$')

# Power / ground nets that legitimately span many pins on a single chip
# (so per-net dedup must not treat them like signal nets).
_POWER_NET_RE = re.compile(
    r'^(GND|AGND|DGND|VSS|VCC|VDD|AVCC|AVDD|VCORE|VBAT|VREF|'
    r'\+?\d+V\d?|\+?\d+\.\d+V|3VDUAL|5VDUAL|VDDQ|VPP|VTT)\b',
    re.I,
)

def _populate_synth_pin_positions(shape, pin_records, w: float, h: float) -> None:
    """Add (name, x, y) entries to `shape.pins` with synthesised XY.

    Strategy:
      - Pin names that look like BGA grid labels (e.g. 'AC18') are parsed
        into (column-letter index, row number) and laid out on a uniform
        grid filling the bbox.
      - Other pin names (pure numerics, fiducials) are distributed along
        the perimeter starting at top-left, going counter-clockwise.

    A chip can mix both — a BGA with a few numeric fiducial markers is
    common — so we don't bail out of the grid path the moment we hit a
    non-BGA name. We classify each pin individually.
    """
    n = len(pin_records)
    if n == 0:
        return

    bga_parsed: List[Tuple[str, int, int]] = []
    other: List[str] = []

    for p in pin_records:
        m = _BGA_NAME_RE.match(p['name'])
        if m:
            col_idx = _bga_column_index(m.group(1))
            if col_idx is not None:
                bga_parsed.append((p['name'], col_idx, int(m.group(2))))
                continue
        other.append(p['name'])

    # If majority of pins are BGA, lay them on a grid; place stragglers
    # along the perimeter.
    if len(bga_parsed) >= max(4, n // 2):
        cols = [c for _, c, _ in bga_parsed]
        rows = [r for _, _, r in bga_parsed]
        c_min, c_max = min(cols), max(cols)
        r_min, r_max = min(rows), max(rows)
        c_span = max(c_max - c_min, 1)
        r_span = max(r_max - r_min, 1)
        inset = 0.07
        for name, col_idx, row_idx in bga_parsed:
            fx = (col_idx - c_min) / c_span  # 0..1 (A=left, max-col=right)
            fy = (row_idx - r_min) / r_span  # 0..1 (row1=top, max-row=bot)
            x = (-w / 2) + (inset + fx * (1 - 2 * inset)) * w
            y = (h / 2) - (inset + fy * (1 - 2 * inset)) * h
            shape.pins.append((name, x, y))
        # Stragglers (numeric fiducials etc.) — perimeter from top-left CCW.
        if other:
            _add_perimeter_pins(shape, other, w, h)
        return

    # All-numeric / non-BGA: perimeter layout for everything.
    _add_perimeter_pins(shape, [p['name'] for p in pin_records], w, h)


def _add_perimeter_pins(shape, names: List[str], w: float, h: float) -> None:
    """Distribute `names` evenly along the chip's perimeter, starting at
    top-left and going counter-clockwise."""
    n = len(names)
    if n == 0:
        return
    perim = 2 * (w + h)
    inset_x, inset_y = w * 0.07, h * 0.07
    for i, name in enumerate(names):
        t = (i / n) * perim
        if t < h:
            x = -w / 2 + inset_x
            y = h / 2 - t
        elif t < h + w:
            x = -w / 2 + (t - h)
            y = -h / 2 + inset_y
        elif t < 2 * h + w:
            x = w / 2 - inset_x
            y = -h / 2 + (t - h - w)
        else:
            x = w / 2 - (t - 2 * h - w)
            y = h / 2 - inset_y
        shape.pins.append((name, x, y))


# TVW coordinate-unit calibration. Empirically derived:
#   Z490 chip-position X span (1st-99th percentile, excluding mounting-hole
#   outliers) ≈ 887,050 units. The Z490 VISION G is an ATX board, ~305 mm
#   long → 1 unit ≈ 0.000325 mm. All sizes in `_footprint_size_raw` are
#   in mm × 1000; we scale by `_UNITS_PER_MM` to convert to file units.
_UNITS_PER_MM = 3.077  # 1 mm ≈ 3077 TVW units


def _footprint_size(fp: str) -> Tuple[float, float]:
    """Return (width, height) for a TVW footprint name in file units."""
    w_mm, h_mm = _footprint_size_mm(fp)
    return (w_mm * _UNITS_PER_MM, h_mm * _UNITS_PER_MM)


def _footprint_size_mm(fp: str) -> Tuple[float, float]:
    """Return (width, height) for a TVW footprint name in mm × 1000.

    Sizes correspond to actual physical chip body dimensions (in
    micrometres-equivalent / mm×1000 units).

    The TVW format stores pin records as flat counters, not 2D positions,
    so per-instance pad XY isn't directly available. This table maps
    each common footprint name to a realistic body size for rendering.
    """
    f = fp.upper()

    # Sockets
    if "LGA1200" in f or "1200/S/15" in f or "CPU-SK/1200" in f:
        return (37500, 37500)
    if "AM4" in f or "1331" in f:
        return (40000, 40000)
    if "LGA17" in f or "LGA20" in f or "LGA12" in f:
        return (45000, 37500)

    # CPU/PCH BGA chipsets — body roughly 22-24mm
    if "BGA874" in f or "FH82" in f:
        return (22500, 22500)
    if "218-0" in f or "218-1" in f:  # AMD chipsets like 218-0933067
        return (22000, 22000)
    if "FCBGA" in f:
        return (28000, 28000)

    # TQFP / QFP — leadframe packages, body + lead frame is bigger
    m = re.search(r'IC(\d+)TQFP', f) or re.search(r'(\d+)TQFP', f) \
        or re.search(r'IC(\d+)QFP', f) or re.search(r'QFP(\d+)', f) \
        or re.search(r'TQFP(\d+)', f)
    if m:
        n = int(m.group(1))
        if n <= 32:
            return (8000, 8000)
        if n <= 64:
            return (12000, 12000)
        if n <= 100:
            return (16000, 16000)
        if n <= 128:
            return (17000, 17000)   # LQFP-128 ~14×14mm body + 1.5mm leads
        if n <= 144:
            return (20000, 20000)
        if n <= 176:
            return (24000, 24000)
        if n <= 208:
            return (28000, 28000)
        return (32000, 32000)

    # QFN — leadless, smaller than QFP for same pin count
    m = re.search(r'IC(\d+)QFN', f) or re.search(r'IC(\d+)TQFN', f) \
        or re.search(r'QFN(\d+)', f) or re.search(r'TQFN(\d+)', f) \
        or re.search(r'IC(\d+)DFN', f)
    if m:
        n = int(m.group(1))
        if n <= 16:
            return (4000, 4000)
        if n <= 24:
            return (5000, 5000)
        if n <= 32:
            return (6000, 6000)
        if n <= 48:
            return (8000, 8000)
        if n <= 56:
            return (9000, 9000)
        if n <= 68:
            return (10000, 10000)
        if n <= 76:
            return (11000, 11000)
        if n <= 100:
            return (13000, 13000)
        return (16000, 16000)

    # Discrete IC packages
    if "SOT23" in f or "SOT-23" in f:
        return (2900, 1300)
    if "SOT223" in f or "SOT-223" in f:
        return (6500, 3500)
    if "DFN" in f or "WDFN" in f or "TDSON" in f:
        return (3000, 3000)
    if "MSOP8" in f or "MSOP" in f:
        return (3000, 3000)
    if "SOIC8" in f or "SOIC-8" in f or "SOIC" in f:
        return (5000, 4000)
    if "SOIC16" in f:
        return (10000, 4000)
    if "SSOP" in f:
        return (4500, 4000)
    if "TSSOP" in f:
        return (5000, 4500)
    if "WSON" in f:
        return (3000, 3000)

    # Resistors / capacitors / diodes by package code
    if "0201" in f:
        return (700, 350)
    if "0402" in f:
        return (1200, 700)
    if "0603" in f:
        return (1800, 1000)
    if "0805" in f:
        return (2400, 1400)
    if "1206" in f:
        return (3500, 1800)
    if "1210" in f:
        return (3500, 2700)

    # Inductors / chokes
    m = re.search(r'CHOKE(\d+)X(\d+)', f)
    if m:
        return (int(m.group(1)) * 1100, int(m.group(2)) * 1100)
    if "CHOKE" in f or "FERRI" in f or "INDUCT" in f:
        return (8000, 6000)

    # Electrolytic / tantalum caps with mm dimensions in name
    m = re.search(r'EC(\d+)D(\d+)MM', f) or re.search(r'EC(\d+)X(\d+)', f)
    if m:
        return (int(m.group(1)) * 1100, int(m.group(2)) * 1100)
    if "TANT" in f:
        return (4000, 3000)

    # Crystals/oscillators
    if "OSC" in f or "XTAL" in f or "CRYSTAL" in f:
        return (5000, 3500)

    # Battery
    if "BAT" in f or "CR2032" in f:
        return (16000, 16000)

    # Mounting holes
    if "HOLE" in f or "MH" in f.split("/")[0]:
        return (3500, 3500)

    # TestPoints / fiducials
    if "TESTPT" in f or "TESTPOINT" in f or "FIDUCIAL" in f:
        return (1500, 1500)

    # Connectors — sockets and slots are big
    if "DDR4" in f or "DDR5" in f or "DIMM" in f:
        return (140000, 6500)
    m_slot = re.search(r'PCIESLOT-?(\d+)', f)
    m_lane = re.search(r'PCI-E/(\d+)X', f)
    if m_slot:
        # PCIESLOT-NNN where NNN is pin count
        pins = int(m_slot.group(1))
        if pins >= 160:  # x16: 164 pins, ~89mm
            return (90000, 8500)
        if pins >= 95:   # x8: 98 pins, ~60mm
            return (60000, 8500)
        if pins >= 60:   # x4: 64-66 pins, ~39mm
            return (39000, 8500)
        if pins >= 30:   # x1: 36 pins, ~25mm
            return (25000, 8500)
        return (25000, 8500)
    if m_lane:
        # PCI-E/NX where N is the lane width
        lanes = int(m_lane.group(1))
        if lanes >= 16:
            return (90000, 8500)
        if lanes >= 8:
            return (60000, 8500)
        if lanes >= 4:
            return (39000, 8500)
        return (25000, 8500)
    if "PCIESLOT" in f:
        return (60000, 8500)
    if "M2_" in f or "/M2/" in f or "M.2" in f or "NGFF" in f:
        return (25000, 4500)
    if "USB30" in f or "USB3+" in f or "USB-A" in f:
        return (15000, 14000)
    if "USB" in f:
        return (12000, 13000)
    if "RJ45" in f or "LAN" in f:
        return (16000, 14000)
    if "AUDIO_J" in f or "JACK" in f or "DCJ" in f:
        return (10000, 8000)
    if "ATX_" in f or "ATX/" in f.replace("//", "/"):
        return (50000, 8000)
    # ATX power connector footprint. "ATXPWR_24-SOLID" = 24-pin (2×12 grid).
    # "ATXPWR_8" = CPU 8-pin (2×4). The number after underscore is pin count.
    if f.startswith("ATXPWR") or f.startswith("APW"):
        m = re.search(r'(\d+)', f)
        n = int(m.group(1)) if m else 24
        # Standard ATX-24 body ≈ 56×8mm; scale by half pin count
        return ((n // 2) * 4200 + 6000, 8500)
    if "F_PANEL" in f or "FRONT_PANEL" in f:
        return (12000, 6000)
    if "PIN1X" in f:  # 1×N header
        m = re.search(r'PIN1X(\d+)', f)
        n = int(m.group(1)) if m else 2
        return (n * 2540, 2540)
    if "PIN2X" in f or "/2X" in f or "/2*" in f:
        m = re.search(r'2[X*](\d+)', f)
        n = int(m.group(1)) if m else 2
        return (n * 2540, 5080)
    if "PH/" in f or "BH/" in f or "TPM" in f:
        return (10000, 5000)
    if "DP+HDMI" in f or "/DP/" in f or "HDMI" in f:
        return (28000, 14000)
    if "SHELL" in f or "AUDIO" in f or "REAR" in f:
        return (15000, 12000)

    # Fuse
    if "FUSE" in f:
        return (3500, 1800)

    # Default catch-all
    return (5000, 5000)


def _default_size_for_footprint(fp: str) -> float:
    """Backwards-compat: largest dimension for fallback bbox computation."""
    w, h = _footprint_size(fp)
    return float(max(w, h))


# --------------------------------------------------------------------------
# CLI smoke test
# --------------------------------------------------------------------------

def _summary(model: BoardModel) -> str:
    n_top = sum(1 for c in model.components.values() if c.layer == "TOP")
    n_bot = sum(1 for c in model.components.values() if c.layer == "BOTTOM")
    fams: Dict[str, int] = {}
    for c in model.components.values():
        prefix = re.match(r'^[A-Z]+', c.refdes).group()
        fams[prefix] = fams.get(prefix, 0) + 1
    fam_str = ", ".join(f"{k}:{v}" for k, v in
                        sorted(fams.items(), key=lambda x: -x[1])[:10])
    n_nodes = sum(len(v) for v in model.signals.values())
    return (
        f"Components: {len(model.components)} ({n_top} TOP, {n_bot} BOTTOM)\n"
        f"Shapes:     {len(model.shapes)}\n"
        f"Signals:    {len(model.signals)} nets, {n_nodes:,} (refdes,pin) "
        f"connections\n"
        f"Top families: {fam_str}"
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("Usage: python tvw_parser.py <file.tvw>")
    m = parse(Path(sys.argv[1]))
    print(_summary(m))
    print()
    # Sample positions
    print("Sample components (first 8):")
    for refdes in list(m.components.keys())[:8]:
        c = m.components[refdes]
        sh = m.shapes.get(c.shape)
        bb = sh.bbox() if sh else (0, 0, 0, 0)
        print(f"  {c.refdes:6s}  ({c.x:>10.0f}, {c.y:>10.0f})  rot={c.rotation:>5.0f}  "
              f"pins={len(sh.pins) if sh else 0}  fp={c.device[:30]!r}")
    print("\nSample big-footprint chips (likely BGAs):")
    for c in m.components.values():
        sh = m.shapes.get(c.shape)
        if sh and sh.bbox_override:
            x0, y0, x1, y1 = sh.bbox_override
            if max(x1 - x0, y1 - y0) > 20000:
                print(f"  {c.refdes:6s}  pos=({c.x:>10.0f}, {c.y:>10.0f})  "
                      f"size={x1-x0:>6.0f}x{y1-y0:>6.0f}  "
                      f"pins={len(sh.pins)}  fp={c.device!r}")
