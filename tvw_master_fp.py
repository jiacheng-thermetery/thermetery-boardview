# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""TVW master footprint table — pin-position decoder and transformer.

A TVW file's tail section contains a "master footprint" table that defines
each footprint type's pin geometry once, in a footprint-local coordinate
system. The per-chip-instance pre-32 metadata (chip XY + rotation) tells
us how to place each instance in world coords.

Master footprint body layout (immediately after the Pascal name)
================================================================
A master fp's body is a stream of `\\xff\\xff\\xff\\xff`-prefixed records.
Each record:

    [ff ff ff ff]            4-byte separator
    [<flag> 00 00 00]        1-byte flag, 3 zero bytes (flag is small,
                             typically 4, 7, 8, 10, 11, 12, ..., 90)
    record body              variable

Two record bodies appear (distinguished by stride from one separator
to the next):

  - **24-byte stride: line segment**
        i32 X1, i32 Y1   start point
        i32 X2, i32 Y2   end point
    Used for body outline polygons and per-pad outline polygons.

  - **19-byte stride: pin point** (4 + 4 + 4 + 4 + 3 = 19 bytes)
        i32 X, i32 Y     pin position in footprint-local coords
        00 00 00         3 trailing zeros

The flag byte is NOT a record-type discriminator. Empirically, both
flag=10 and flag=11 records appear as pin-points (e.g. IC128TQFP has 64
flag=10 pins on the horizontal edges and 64 flag=11 pins on the vertical
edges, total 128). For BGAs with multiple ball-shape variants, flags
12..18 also appear as pin-points (BGA874-PCH has 9 distinct flag values,
all valid pin positions). The flag appears to be an index into the
pad-shape table at the beginning of the body.

Stride is recovered from the file: at each separator we test which of
+19 or +24 contains the next `ffffffff`. If +19 has it AND the bytes at
[+16..+18] are all zero, we read a 19-byte point record; otherwise a
24-byte segment record.

Coordinate transform — the cracked formula
==========================================
For every chip instance with `(chip_x, chip_y, rotation)` from the
pre-32 metadata, master-footprint-local pin position `(lx, ly)` maps to
world position `(wx, wy)` as:

    wx = chip_x + ly * cos(-rot) - lx * sin(-rot)
    wy = chip_y + ly * sin(-rot) + lx * cos(-rot)

Equivalent forms (whichever's clearest):

  - "swap axes, then rotate by -rot CCW":
        (lx, ly) -> (ly, lx) -> rotate_ccw(-rot) -> + chip_xy

  - "explicit per-rotation table" (cardinal rotations, which is all the
    file uses) — the file's rotation field acts as a 4-state enum:
        rot=0   -> ( ly,  lx)         (swap XY)
        rot=90  -> ( lx, -ly)         (mirror Y)
        rot=180 -> (-ly, -lx)         (swap XY, negate both)
        rot=270 -> (-lx,  ly)         (mirror X)
    These are NOT the four standard CCW rotations of the (lx, ly) basis.
    Two of them are pure reflections (rot=90 and rot=270). The file uses
    a left-handed convention (or local axes are transposed relative to
    world) — that's the missing piece.

Verification (Z490 / B550 / X570; see tvw_mfp_verify.py)
========================================================
- Z490: 92.8 % @ 50 file units (92.6 % using only flag in {10,11}; some
  fps with multiple pad-shape flags need {10,11,12,...} for full coverage)
- BGA874-PCH (Z490): 874/874 pins @ 50 units across 9 different pad-shape
  flags
- DDR4 (×4 instances on Z490, ×4 on B550): 295/295 pins each, 100 % match
- IC128TQFP, all PCIE slots, all M.2 slots, all DDR4 slots: 100 % match

Public API
==========
- `parse_master_footprints(buf)` -> dict[name, list[(pin_idx, lx, ly)]]
- `pin_world_position(name, chip_xy, rot, pin_idx, master_fps)` -> (wx, wy)
- `pins_world_positions(name, chip_xy, rot, master_fps)`
       -> list[(pin_idx, wx, wy)]
- `parse_master_footprint_outlines(buf)`
       -> dict[name, list[(x1, y1, x2, y2)]]
- `apply_master_fp_transform(lx, ly, chip_xy, rot)` -> (wx, wy)
"""

from __future__ import annotations

import math
import struct
from typing import Dict, List, Optional, Tuple


# Signature that appears immediately after every master footprint name's
# pad-shape descriptor block.
_MFP_SIG = b'\x01\x03\x00\x00\x00\x02\x00\x00\x00\x01'

# Record separator + flag-zeros pattern. We don't fix the flag byte
# because pin records use any of {10, 11, 12, ..., 18, ...}.
_REC_SEP = b'\xff\xff\xff\xff'


def _is_pascal_footprint_name(buf: bytes, off: int,
                              min_len: int = 5, max_len: int = 50) -> Optional[str]:
    if off < 0 or off + 1 >= len(buf):
        return None
    L = buf[off]
    if not (min_len <= L <= max_len) or off + 1 + L > len(buf):
        return None
    s = buf[off+1:off+1+L]
    try:
        if not all(0x20 <= b < 0x7f for b in s):
            return None
        sd = s.decode('latin-1')
    except UnicodeDecodeError:
        return None
    if not sd[0].isalpha():
        return None
    if not all(c.isalnum() or c in '/_-+.' for c in sd):
        return None
    return sd


def _find_master_fp_offsets(buf: bytes,
                            scan_start: Optional[int] = None) -> List[Tuple[int, str]]:
    """Find every master footprint by scanning for `_MFP_SIG`.

    Master fps live in the last ~500 KB of the file in all observed
    boards. Searching the whole file would also pick up chip-record
    separators that share the same signature.

    Returns list of (name_offset, name) in order of appearance.
    """
    n = len(buf)
    if scan_start is None:
        scan_start = max(0, n - 500_000)

    out: List[Tuple[int, str]] = []
    i = scan_start
    while i < n:
        idx = buf.find(_MFP_SIG, i)
        if idx < 0:
            break
        # Scan back 7..60 bytes for a Pascal name preceding this sig.
        for back in range(7, 60):
            name_off = idx - back
            if name_off < 0:
                break
            name = _is_pascal_footprint_name(buf, name_off)
            if name is not None:
                out.append((name_off, name))
                break
        i = idx + len(_MFP_SIG)
    return out


def _walk_body_records(
    buf: bytes, body_start: int, body_end: int,
) -> Tuple[List[Tuple[int, int, int, int, int]], List[Tuple[int, int, int]]]:
    """Walk the entire body of a master footprint, classifying every
    `ffffffff`-prefixed record as either a 24-byte segment (line) or a
    19-byte point.

    Returns `(segments, points)` where:
      - segments: list of (flag, x1, y1, x2, y2)
      - points:   list of (flag, x, y)

    Stride detection: at each separator, we test if the bytes at +19
    are another separator. If so AND the bytes at [+16..+18] are all
    zero (the trailing-zero signature of a point record), we read a
    19-byte point. Otherwise we test +24 for a separator and read a
    24-byte segment. If neither successor is a separator (last record
    in the body), we fall back to whichever stride leaves the body in
    a sane state.
    """
    segments: List[Tuple[int, int, int, int, int]] = []
    points: List[Tuple[int, int, int]] = []

    i = body_start
    while i + 8 <= body_end:
        if buf[i:i+4] != _REC_SEP:
            i += 1
            continue
        # Need [+5..+7] = 00 00 00 (flag is at +4)
        if buf[i+5:i+8] != b'\x00\x00\x00':
            # Not a record we recognize; skip past the separator.
            i += 4
            continue
        flag = buf[i+4]

        # Decide stride
        ok_19 = (i + 19 + 4 <= body_end and
                 buf[i+19:i+19+4] == _REC_SEP)
        ok_24 = (i + 24 + 4 <= body_end and
                 buf[i+24:i+24+4] == _REC_SEP)
        trailing_zero = (i + 19 <= body_end and
                         buf[i+16:i+19] == b'\x00\x00\x00')

        if ok_19 and trailing_zero:
            stride = 19
        elif ok_24:
            stride = 24
        elif ok_19:
            stride = 19
        else:
            # Last record in body. Decide by trailing-zero signature.
            stride = 19 if trailing_zero else 24

        if stride == 19:
            if i + 19 > body_end:
                break
            x, y = struct.unpack_from('<2i', buf, i + 8)
            points.append((flag, x, y))
        else:  # 24
            if i + 24 > body_end:
                break
            x1, y1, x2, y2 = struct.unpack_from('<4i', buf, i + 8)
            segments.append((flag, x1, y1, x2, y2))
        i += stride

    return segments, points


def parse_master_footprints(
    buf: bytes,
) -> Dict[str, List[Tuple[int, int, int]]]:
    """Parse every master footprint in `buf`.

    Returns a dict mapping footprint name -> ordered list of
    `(pin_index, lx, ly)` tuples (`pin_index` starts at 0).

    Notes:
      - A pin is any 19-byte point record in the body, regardless of
        flag value. For TQFP/QFN/SOIC, flag=10 and flag=11 typically
        cover different sides of the chip; for BGAs with multiple ball
        shapes, flags 10..20 are all valid pin records.
      - Some master fps include "outline" point vertices that aren't
        physically pins (e.g. one duplicate at a body corner). These
        appear as a tiny number of extra points (≤2 typically). They
        match nearby pads coincidentally or not at all and don't hurt
        verification numbers materially.
    """
    out: Dict[str, List[Tuple[int, int, int]]] = {}
    fps = sorted(_find_master_fp_offsets(buf))
    n = len(buf)

    for k, (name_off, name) in enumerate(fps):
        body_start = name_off + 1 + len(name)
        body_end = fps[k + 1][0] if k + 1 < len(fps) else n

        _, points = _walk_body_records(buf, body_start, body_end)
        if not points:
            continue

        if name in out:
            # Duplicate name (rare). Keep the first occurrence.
            continue
        out[name] = [(i, x, y) for i, (_flag, x, y) in enumerate(points)]
    return out


def parse_master_footprint_outlines(
    buf: bytes,
) -> Dict[str, List[Tuple[int, int, int, int]]]:
    """Same as `parse_master_footprints`, but returns the outline line
    segments (each as `(x1, y1, x2, y2)`) instead of pin vertices.
    """
    out: Dict[str, List[Tuple[int, int, int, int]]] = {}
    fps = sorted(_find_master_fp_offsets(buf))
    n = len(buf)
    for k, (name_off, name) in enumerate(fps):
        body_start = name_off + 1 + len(name)
        body_end = fps[k + 1][0] if k + 1 < len(fps) else n
        segments, _ = _walk_body_records(buf, body_start, body_end)
        if not segments:
            continue
        if name in out:
            continue
        out[name] = [(x1, y1, x2, y2) for (_flag, x1, y1, x2, y2) in segments]
    return out


def parse_master_footprints_full(
    buf: bytes,
) -> Dict[str, Tuple[List[Tuple[int, int, int, int, int]],
                     List[Tuple[int, int, int]]]]:
    """Full parse — returns dict[name, (segments, points)] where
    segments retain their flag and points retain their flag too.
    Useful for callers that want to distinguish per-pad-shape flags.
    """
    out = {}
    fps = sorted(_find_master_fp_offsets(buf))
    n = len(buf)
    for k, (name_off, name) in enumerate(fps):
        body_start = name_off + 1 + len(name)
        body_end = fps[k + 1][0] if k + 1 < len(fps) else n
        segments, points = _walk_body_records(buf, body_start, body_end)
        if name in out:
            continue
        out[name] = (segments, points)
    return out


def apply_master_fp_transform(
    lx: float, ly: float,
    chip_xy: Tuple[float, float],
    rotation: float,
) -> Tuple[float, float]:
    """Apply the cracked master-fp-local-to-world transform.

    Formula:
        wx = chip_x + ly * cos(-rot) - lx * sin(-rot)
        wy = chip_y + ly * sin(-rot) + lx * cos(-rot)

    Equivalent: swap (lx, ly) to (ly, lx), then rotate by `-rotation`
    degrees (CCW), then translate by chip_xy.
    """
    cx, cy = chip_xy
    sx, sy = ly, lx
    theta = math.radians(-rotation)
    ct, st = math.cos(theta), math.sin(theta)
    return (cx + sx * ct - sy * st,
            cy + sx * st + sy * ct)


def pin_world_position(
    footprint_name: str,
    chip_xy: Tuple[float, float],
    rotation: float,
    pin_index: int,
    master_fps: Dict[str, List[Tuple[int, int, int]]],
) -> Tuple[float, float]:
    """Compute the world position of pin `pin_index` of an instance.

    Raises:
        KeyError: footprint not in master_fps
        IndexError: pin_index out of range
    """
    pins = master_fps[footprint_name]
    if not (0 <= pin_index < len(pins)):
        raise IndexError(
            f"pin_index {pin_index} out of range for {footprint_name} "
            f"(has {len(pins)} pins)"
        )
    _, lx, ly = pins[pin_index]
    return apply_master_fp_transform(lx, ly, chip_xy, rotation)


def pins_world_positions(
    footprint_name: str,
    chip_xy: Tuple[float, float],
    rotation: float,
    master_fps: Dict[str, List[Tuple[int, int, int]]],
) -> List[Tuple[int, float, float]]:
    """Return all pins of an instance as `(pin_index, wx, wy)` tuples."""
    pins = master_fps.get(footprint_name)
    if pins is None:
        return []
    cx, cy = chip_xy
    theta = math.radians(-rotation)
    ct, st = math.cos(theta), math.sin(theta)
    out: List[Tuple[int, float, float]] = []
    for idx, lx, ly in pins:
        sx, sy = ly, lx
        out.append((idx, cx + sx * ct - sy * st, cy + sx * st + sy * ct))
    return out
