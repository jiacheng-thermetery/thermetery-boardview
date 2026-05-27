# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""
Compal/Lenovo TVW variant decoder.

The Compal variant (used by Compal-manufactured laptops, notably the
Lenovo Thinkpad T-series via NM-B501 and related boards) deviates from
the Gigabyte TVW convention in several structural places:

  * Chip enumeration uses a multi-source union: Region 3 (primary,
    chip records anchored by `00 00 00 00 + Pascal-refdes`) plus the
    cap-section (the historical `0x01 + Pascal-dev + Pascal-fp` chips
    that tvw_parser._find_chip_headers already finds) plus Region 1
    (a supervised layer-flag table identified by a `0xbb800 / 0x12c00`
    constants signature).
  * Layer pads are 19-byte stride records for ALL 10 copper layers,
    not Gigabyte's 38-byte (and 54-byte through-hole) format. A single
    38-byte stride region exists but only carries GND stitching vias.
  * Master footprint pool starts ~234 KB earlier in the file than the
    Gigabyte convention would predict — around 0xbd0000 in T480.
  * Chip layer is not encoded as a single byte inside the chip R3
    record. It comes from a 3-source chain (R1 supervised → cap-section
    trailer byte +20 → master record `_B` suffix).
  * The canonical pin-position transform uses the chip's bbox-anchor
    (f2, f3) at after-Pascal +16..+23 — NOT the chip's world position
    at +0..+7 — as the origin for adding master-local pin offsets.

See `TVW_FORMAT.html` (in this repo) sections 5-13 for the full format
spec with byte layouts and ground-truth anchors.

This module is the *minimum-viable* Compal decoder: it produces chips
with correct positions, rotations, layers, and shape geometry. Pin-net
mapping (model.signals) is intentionally NOT populated here — that
requires matching predicted pin world coords against the 19-byte layer
pad records, which is its own substantial code path and lands in a
follow-up commit. The warnings list surfaces this fact to the viewer
so users know net browsing won't return results yet.

Verified against Lenovo Thinkpad T480 NM-B501R10 (MD5
6983e8afd4af43829ec210c1eef0136f).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import math
import re

from gencad_parser import BoardModel, Component, Shape


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

# 8-byte signature of Region 1 layer-flag records: the chip's
# `0xbb800 / 0x12c00` constants packed at after-Pascal +12..+19. Used
# both for variant detection (in tvw_parser._detect_variant) and for
# scanning the supervised layer-flag table.
_R1_SIGNATURE = b"\xb8\x0b\x00\x00\x2c\x01\x00\x00"

# 9-byte signature marking the start of a master footprint record:
# 8 zero bytes followed by 0x01, then a Pascal-prefixed footprint name.
_MASTER_SIGNATURE = b"\x00\x00\x00\x00\x00\x00\x00\x00\x01"

# Where to start scanning for the master footprint pool. This is stable
# across the Compal/Lenovo files we've verified (T480). If future Compal
# sub-variants shift this we'll need to autodetect by walking backwards
# from end-of-file looking for the first master signature.
_MASTER_POOL_START_HINT = 0xbd0000

# Region 3 chip-record search range. R3 records cluster in this offset
# range on T480; the lower bound skips early aux tables and the upper
# bound stops before the net-name table region. Sized generously since
# the scan is cheap (byte-level skip-ahead).
_R3_SEARCH_START = 0xa00000
_R3_SEARCH_END = 0xc00000

# Sentinel byte found at chip after-Pascal +44 in ALL valid Compal R3
# records. The bytes immediately following vary by chip type (ICs use
# `01 01 2a 00 00` then a Pascal footprint name; passives pack their
# value as a small Pascal string first), so we use the single byte at
# +44 as a cheap pre-filter and rely on the fp_id → master lookup to
# reject false positives.
_CHIP_PRELUDE_SENTINEL = 0x01

# Test-point fp_ids — these chip records have no electrical body, just
# outline placeholders for TB_TP1..TB_TP6. Filtering them keeps the
# component count meaningful.
_TEST_POINT_FP_IDS = frozenset({235, 236, 237, 238, 239, 240})

# Refdes patterns that are pin sub-records nested inside larger chips,
# NOT standalone components. The `H`, `FL`, `FLJ` prefixes are pin-name
# conventions used inside connectors and ICs on T480. Note: many `P*`
# compound prefixes (PQ, PD, PL, PU, PR, …) are NOT pin sub-records on
# T480 — they're real chip refdes (MOSFETs, diodes, etc.). We rely on
# the structural pre-filter (byte +44 sentinel + valid fp_id) plus the
# `_KNOWN_PIN_PATTERNS` regex below to keep the chip set clean.
_KNOWN_PIN_PATTERNS = re.compile(r"^(H[0-9]+|FL[0-9]+|FLJ[0-9]+)$")


# --------------------------------------------------------------------------
# Data classes (parser-internal, not exported)
# --------------------------------------------------------------------------

@dataclass(slots=True)
class _Master:
    """One footprint master record. Built by `_scan_master_pool`."""
    idx: int            # master_idx (pool index, 0-based)
    name: str           # Pascal footprint name (may end in "_B")
    record_off: int     # file offset of the 8-zero signature byte
    pin_locals: List[Tuple[int, int]]  # per-pin (A, B) in Section C order


@dataclass(slots=True)
class _Chip:
    """One R3 chip record. Built by `_enumerate_r3_chips`."""
    refdes: str
    record_off: int     # offset of Pascal-length byte
    Y: int              # world Y (chip silkscreen anchor)
    X: int              # world X
    f2: int             # bbox-anchor Y (canonical pin transform origin)
    f3: int             # bbox-anchor X
    rot: int            # rotation (0 / 90 / 180 / 270)
    fp_id: int          # footprint ID, maps to master at fp_id - 1


# --------------------------------------------------------------------------
# Master pool scanner
# --------------------------------------------------------------------------

def _scan_master_pool(data: bytes) -> Dict[int, _Master]:
    """Walk the master footprint pool starting near
    `_MASTER_POOL_START_HINT`. Returns ``{master_idx -> _Master}``.

    Each master record begins with the 9-byte signature
    ``00 00 00 00 00 00 00 00 01`` followed by a Pascal-prefixed
    footprint name (length 4..80, ASCII). The data after the name
    breaks into sub-sections — outline polyline (24-byte stride),
    pad-shape table, per-pin local coords (19-byte stride), and a
    bounding trailer — but for this minimum-viable parser we only
    extract the per-pin (A, B) coords from the 19-byte stride run.

    Two-pass implementation: first pass collects all valid master
    offsets+names; second pass extracts pin coords for each, using the
    NEXT master's offset as the body-end bound. The 9-byte signature
    byte sequence appears many times INSIDE master data (any run of 8
    zero bytes followed by 0x01 will match), so single-pass scanning
    with `data.find()` for the next signature picks up false positives
    and cuts master bodies far too short.
    """
    # Pass 1: collect (offset, name_len, name) for every record whose
    # 9-byte signature is followed by a plausible Pascal-prefixed
    # ASCII footprint name. This still admits some false positives
    # (e.g. a master's data containing the 9-byte pattern then a
    # coincidentally-ASCII run), but those are rare and rejected by
    # the implicit constraint that fp_id -> master_idx must yield a
    # sane footprint name for real chips.
    raw: List[Tuple[int, int, str]] = []
    i = _MASTER_POOL_START_HINT
    while i < len(data) - 12:
        if data[i:i+9] == _MASTER_SIGNATURE:
            L = data[i+9]
            if 4 <= L <= 80:
                name_bytes = data[i+10:i+10+L]
                if all(0x20 <= b < 0x7f for b in name_bytes):
                    name = name_bytes.decode("ascii", errors="replace")
                    raw.append((i, L, name))
                    i = i + 10 + L
                    continue
        i += 1
    # Pass 2: extract pin coords with proper body bounds.
    masters: List[_Master] = []
    for k, (off, L, name) in enumerate(raw):
        body_start = off + 10 + L
        body_end = raw[k+1][0] if k + 1 < len(raw) else len(data)
        pin_locals = _extract_master_pins(data, body_start, body_end)
        masters.append(_Master(
            idx=k, name=name, record_off=off, pin_locals=pin_locals,
        ))
    return {m.idx: m for m in masters}


def _extract_master_pins(data: bytes, body_start: int, body_end: int
                         ) -> List[Tuple[int, int]]:
    """Pull Section C 19-byte per-pin records and decode each as an
    (A, B) int32 pair in master-local coordinates.

    Sections inside a master record are delimited by `ff ff ff ff`
    sentinels. Section A (outline polyline) uses 24-byte stride;
    Section C (per-pin pad coords) uses 19-byte stride. We only collect
    consecutive sentinels exactly 19 bytes apart — this skips Section A
    cleanly, skips the variable-length Section B (pad-shape table)
    between A and C, and stops at the Section D (bounding trailer)
    boundary where the stride changes again.
    """
    sentinels: List[int] = []
    pos = body_start
    while pos < body_end:
        idx = data.find(b"\xff\xff\xff\xff", pos, body_end)
        if idx < 0:
            break
        sentinels.append(idx)
        pos = idx + 4
    if not sentinels:
        return []
    pins: List[Tuple[int, int]] = []
    for i in range(len(sentinels) - 1):
        s_off = sentinels[i]
        nxt = sentinels[i + 1]
        if nxt - s_off != 19:
            continue
        # 19-byte record layout (from sentinel start):
        #   +0..+3   sentinel `ff ff ff ff`
        #   +4..+7   uint32 pad_shape_enum (indexes Section B)
        #   +8..+11  int32 A (local X)
        #   +12..+15 int32 B (local Y)
        #   +16..+18 3 zero padding bytes
        A = int.from_bytes(data[s_off+8:s_off+12], "little", signed=True)
        B = int.from_bytes(data[s_off+12:s_off+16], "little", signed=True)
        pins.append((A, B))
    return pins


# --------------------------------------------------------------------------
# Region 1 layer-flag scanner
# --------------------------------------------------------------------------

def _scan_r1_layers(data: bytes) -> Dict[str, int]:
    """Scan for Region 1 layer-flag records. Returns
    ``{refdes -> layer_byte}`` where layer_byte is the low byte of the
    int32 at after-Pascal +24..+27 (0 = TOP, 1 = BOTTOM).

    Region 1 records: any Pascal-prefixed refdes where the 8-byte
    signature ``b8 0b 00 00 2c 01 00 00`` (the chip's `0xbb800 /
    0x12c00` constants) appears at after-Pascal +12..+19. Verified on
    296 unique chip refdes in T480; never disagrees with itself across
    chips that appear in multiple regions.

    The upper 3 bytes of the layer-flag int32 occasionally carry
    non-zero metadata, so we mask to the low byte. (Across T480 the
    high bytes are 0 in 99 % of records; the rare exceptions are
    structurally consistent with low-byte-only layer encoding.)
    """
    r1: Dict[str, int] = {}
    i = 0
    while i < len(data) - 50:
        L = data[i]
        if 2 <= L <= 16:
            s = data[i+1:i+1+L]
            if all(0x20 <= b < 0x7f for b in s):
                try:
                    text = s.decode("ascii")
                except UnicodeDecodeError:
                    i += 1
                    continue
                if (text[0].isalpha()
                    and all(c.isalnum() or c in "_-" for c in text)
                    and data[i+1+L+12:i+1+L+20] == _R1_SIGNATURE
                    and not _KNOWN_PIN_PATTERNS.fullmatch(text)):
                    if text not in r1:
                        v = int.from_bytes(
                            data[i+1+L+24:i+1+L+28], "little")
                        r1[text] = v & 0xFF
                    i += 1 + L
                    continue
        i += 1
    return r1


# --------------------------------------------------------------------------
# Cap-section layer overlay
# --------------------------------------------------------------------------

def _scan_cap_section_layers(data: bytes) -> Dict[str, int]:
    """For each cap-section chip record (the historical `0x01 +
    Pascal-dev + Pascal-fp` markers found by tvw_parser._find_chip_headers),
    return ``{refdes -> layer_byte}`` from the cap-section trailer at
    `after_off + 20`.

    On Compal/Lenovo files: 0x02 -> TOP, 0x0b -> BOTTOM. Verified
    across 20 chips that overlap between cap-section and R1 — all 20
    agree on layer.
    """
    # Re-use the existing Gigabyte chip-header finder; the marker
    # pattern is the same on both variants. The trailer LAYOUT differs
    # (Gigabyte: layer byte at +9; Compal: layer byte at +20 past a
    # leading 11-char "SE...T" / "SGA...T" / "SH...T" part code), and
    # we use the Compal offset here.
    from tvw_parser import _find_chip_headers, _decode_refdes
    chips = _find_chip_headers(data)
    out: Dict[str, int] = {}
    for c in chips:
        rd = _decode_refdes(data, c["off"])
        if rd and rd not in out:
            after = c["after_off"]
            if after + 21 <= len(data):
                out[rd] = data[after + 20]
    return out


# --------------------------------------------------------------------------
# Region 3 chip enumerator
# --------------------------------------------------------------------------

def _enumerate_r3_chips(
    data: bytes,
    masters: Dict[int, _Master],
) -> Dict[str, _Chip]:
    """Find all Region 3 chip records. Returns ``{refdes -> _Chip}``.

    Filter chain (cheapest checks first to keep the scan fast):
      * anchor must be `00 00 00 00` followed by Pascal-prefixed refdes
        with length 2..16, ASCII-only, alpha leading, alphanumeric +
        `_-` contents.
      * not a known pin sub-record pattern (`H<n>`, `FL<n>`, `FLJ<n>`).
      * byte at chip after-Pascal +44 must equal `_CHIP_PRELUDE_SENTINEL`
        (0x01) — this is true for ALL valid Compal R3 chip records
        regardless of chip type.
      * Y, X coords (after-Pascal +0..+7) must be reasonable (|val| <
        2_000_000 file units; the actual board span is ±1,000,000).
      * fp_id (after-Pascal +28..+31) must resolve to a master record
        (master_idx = fp_id - 1 in range).
      * fp_id must not be in `_TEST_POINT_FP_IDS`.
    """
    chips: Dict[str, _Chip] = {}
    i = _R3_SEARCH_START
    end = min(_R3_SEARCH_END, len(data) - 64)
    while i < end:
        # Cheapest pre-check first: byte at i must be 0.
        if data[i] != 0:
            i += 1
            continue
        if data[i:i+4] != b"\x00\x00\x00\x00":
            i += 1
            continue
        L = data[i+4]
        if L < 2 or L > 16:
            i += 1
            continue
        s = data[i+5:i+5+L]
        if not all(0x20 <= b < 0x7f for b in s):
            i += 1
            continue
        try:
            refdes = s.decode("ascii")
        except UnicodeDecodeError:
            i += 1
            continue
        if not refdes[0].isalpha():
            i += 1
            continue
        if not all(c.isalnum() or c in "_-" for c in refdes):
            i += 1
            continue
        if _KNOWN_PIN_PATTERNS.fullmatch(refdes):
            i += 1
            continue
        # Parse preamble.
        p = i + 5 + L
        if p + 48 > len(data):
            i += 1
            continue
        if data[p+44] != _CHIP_PRELUDE_SENTINEL:
            i += 1
            continue
        Y = int.from_bytes(data[p:p+4], "little", signed=True)
        X = int.from_bytes(data[p+4:p+8], "little", signed=True)
        f2 = int.from_bytes(data[p+16:p+20], "little", signed=True)
        f3 = int.from_bytes(data[p+20:p+24], "little", signed=True)
        rot = int.from_bytes(data[p+24:p+28], "little", signed=True)
        fp_id = int.from_bytes(data[p+28:p+32], "little", signed=True)
        if abs(Y) > 2_000_000 or abs(X) > 2_000_000:
            i += 1
            continue
        master_idx = fp_id - 1
        if master_idx not in masters:
            i += 1
            continue
        if fp_id in _TEST_POINT_FP_IDS:
            i += 1
            continue
        if refdes not in chips:
            chips[refdes] = _Chip(
                refdes=refdes, record_off=i + 4,
                Y=Y, X=X, f2=f2, f3=f3, rot=rot, fp_id=fp_id,
            )
        # Skip past the Pascal so we don't re-match its bytes when
        # walking forward. The chip's full data block extends further
        # (footprint name, part code, pin records) but the next anchor
        # `00 00 00 00 + Pascal-refdes` won't false-positive inside
        # that data: the embedded Pascal strings start at non-zero
        # offsets and pin records start with a uint32 ptr, not zero.
        i = i + 5 + L
    return chips


# --------------------------------------------------------------------------
# Layer determination — 3-source chain
# --------------------------------------------------------------------------

def _determine_layer(
    refdes: str,
    master_name: str,
    r1: Dict[str, int],
    cap_layers: Dict[str, int],
) -> str:
    """Return 'TOP' or 'BOTTOM' using the 3-source priority chain:

      1. Region 1 supervised flag (most reliable, 296 chips on T480).
      2. Cap-section trailer byte (covers 884 chips, mostly passives).
      3. Master `_B` suffix (covers chips with paired footprint variants).

    Chips covered by none of the three default to TOP. On T480 this
    default fires for ~10-20 truly orphan chips, all without
    electrical significance.
    """
    if refdes in r1:
        return "BOTTOM" if r1[refdes] == 1 else "TOP"
    if refdes in cap_layers:
        return "TOP" if cap_layers[refdes] == 0x02 else "BOTTOM"
    return "BOTTOM" if master_name.endswith("_B") else "TOP"


# --------------------------------------------------------------------------
# Canonical pin-position transform
# --------------------------------------------------------------------------

def _pin_local_to_world(
    chip: _Chip, A: int, B: int, layer: str,
) -> Tuple[int, int]:
    """Convert master-local (A, B) to world (Y, X) for the given chip's
    rotation and layer. Returns ``(world_Y, world_X)``.

    See TVW_FORMAT.html section 11 for the derivation. Verified on
    216 supervised chips across all 4 rotations × both layers with
    residual = 0 against actual pad world coords.
    """
    rot = chip.rot
    if   rot ==   0: dy, dx = -A, -B
    elif rot ==  90: dy, dx = -B,  A
    elif rot == 180: dy, dx =  A,  B
    elif rot == 270: dy, dx =  B, -A
    else:            dy, dx =  A,  B  # unusual rotation, accept verbatim
    if layer == "BOTTOM":
        dx = -dx
    return (chip.f2 + dy, chip.f3 + dx)


def _world_to_chip_local(
    chip: _Chip, world_Y: int, world_X: int,
) -> Tuple[float, float]:
    """Convert world (Y, X) back to the chip-local-as-renderer-expects
    coords stored in `Shape.pins`.

    The renderer applies `chip.X + rot(pin.dx, pin.dy)` to get pin
    world coords. We invert that here so the renderer's standard
    transform reproduces the world position we computed above.
    """
    rx = world_X - chip.X
    ry = world_Y - chip.Y
    theta_inv = math.radians(-chip.rot)
    cti, sti = math.cos(theta_inv), math.sin(theta_inv)
    dx = rx * cti - ry * sti
    dy = rx * sti + ry * cti
    return dx, dy


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def parse(path: Path) -> BoardModel:
    """Parse a Compal/Lenovo TVW file into a BoardModel.

    Minimum-viable implementation: produces chips with correct
    positions, rotations, layers, and per-pin shape geometry from the
    master footprint pool. Pin names are sequential 1..N (the file's
    inline pin records with real BGA names like "A3" are NOT yet
    parsed). Pin-to-net mapping is also NOT populated — model.signals
    stays empty until a follow-up commit adds matching against the
    19-byte layer pad records.

    The viewer's warnings list is populated so users see the current
    coverage limitation explicitly.
    """
    data = Path(path).read_bytes()
    model = BoardModel()

    masters = _scan_master_pool(data)
    if not masters:
        model.warnings = [
            f"{Path(path).name}: master footprint pool not found at "
            f"the expected offset. File may use a different Compal "
            f"sub-variant; please report on GitHub issue #1."
        ]
        return model

    r1 = _scan_r1_layers(data)
    cap_layers = _scan_cap_section_layers(data)
    r3_chips = _enumerate_r3_chips(data, masters)

    n_no_master_pins = 0
    for refdes, chip in r3_chips.items():
        master = masters[chip.fp_id - 1]
        layer = _determine_layer(refdes, master.name, r1, cap_layers)
        shape_name = f"_compal_{master.name}_{refdes}"
        shape = Shape(name=shape_name)
        if master.pin_locals:
            for pin_idx, (A, B) in enumerate(master.pin_locals):
                world_Y, world_X = _pin_local_to_world(chip, A, B, layer)
                dx, dy = _world_to_chip_local(chip, world_Y, world_X)
                # Sequential pin name. Real BGA names ("A3", "K1",
                # etc.) come from the chip's inline pin records, which
                # the MVP parser doesn't read yet.
                shape.pins.append((str(pin_idx + 1), dx, dy))
            xs = [p[1] for p in shape.pins]
            ys = [p[2] for p in shape.pins]
            shape.bbox_override = (min(xs), min(ys), max(xs), max(ys))
        else:
            # Master had no Section C pin records. Use a small default
            # bbox so the chip still renders as a placeholder.
            n_no_master_pins += 1
            shape.bbox_override = (-1000.0, -1000.0, 1000.0, 1000.0)
        model.shapes[shape_name] = shape
        comp = Component(
            refdes=refdes, x=float(chip.X), y=float(chip.Y),
            layer=layer, rotation=float(chip.rot),
            shape=shape_name,
            device=master.name,
        )
        model.components[refdes] = comp

    warns = [
        f"{Path(path).name}: Compal/Lenovo TVW variant. "
        f"{len(model.components)} chips loaded; pin-net mapping is not "
        f"populated in this build, so net browsing will show no nets. "
        f"See TVW_FORMAT.html and GitHub issue #1."
    ]
    if n_no_master_pins:
        warns.append(
            f"{n_no_master_pins} chips reference master footprints "
            f"with no pin-coordinate records (rendered as placeholder "
            f"bboxes)."
        )
    model.warnings = warns
    return model


__all__ = ["parse"]
