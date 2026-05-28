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
    Y: int              # world Y — BL corner of chip world bbox
    X: int              # world X — BL corner of chip world bbox
    f0: int             # world Y — TR corner of chip world bbox (= Y + dy)
    f1: int             # world X — TR corner of chip world bbox (= X + dx)
    f2: int             # bbox-anchor Y (canonical pin transform origin)
    f3: int             # bbox-anchor X
    rot: int            # rotation (0 / 90 / 180 / 270)
    fp_id: int          # footprint ID, maps to master at fp_id - 1
    # uint32 at after-Pascal +32..+35. Encodes a chip-type enum used
    # by the file format to discriminate pin-layout conventions. Known
    # values on T480: 0=IC/BGA, 1=LED/Diode, 2=MOSFET, 3=Resistor,
    # 5=Capacitor, 9=Test jack, 13=Fuse, 14=Inductor, 15=Crystal,
    # 16=Switch, 17=Connector, 18=Test point, 29=Fiducial. Currently
    # only class==17 needs a non-canonical pin transform (the others
    # all resolve correctly with the canonical rule).
    chip_class: int
    # Packed Pascal-string slots starting at after-Pascal +45 (after the
    # 0x01 sentinel at +44). 100 % of T480 chips carry all 5; the agent
    # report calls these "device value / tolerance / tolerance-dup /
    # footprint name / part code". Slot 0 == "*" for ICs (placeholder),
    # actual value (e.g. "10K", "0.01") for passives.
    device_value: str   # slot 0 — passive value or "*" for ICs
    tolerance: str      # slot 1 — tolerance %
    part_code: str      # slot 4 — supplier / part code, e.g. "SD000012RYT"
    # Per-pin records walked from the chip's inline pin list. Each tuple
    # is `(master_pin_idx, display_pin_number, pascal_pin_name)`.
    # `master_pin_idx` = (ptr - first_ptr) // 8 — selects the pin's
    # local (A, B) coord in the master record's Section C.
    # `display_pin_number` = uint32 at pin-record +8..+11 — the integer
    # BoardViewer shows in its Pin column (e.g. 148).
    # `pascal_pin_name` = the Pascal-prefixed name (e.g. "A148") —
    # kept for completeness but `display_pin_number` is what we use.
    pins: List[Tuple[int, int, str]] = field(default_factory=list)


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
    # Find runs of consecutive 19-spaced sentinels — each run is Section
    # C of one master. A K-pin master has K sentinels at offsets 0, 19,
    # 38, ..., 19*(K-1) from section start. The earlier pair-up loop
    # `for i in range(len(sentinels)-1)` emitted only K-1 records per
    # run (fence-post: K sentinels = K-1 pairs); we now emit ALL
    # sentinels in any run of length >= 2. Verified to give 2702/2754
    # exact match against chip-side pin counts on T480 (remaining 52
    # are test-point masters whose Section C is genuinely empty by
    # construction).
    i = 0
    while i < len(sentinels):
        run_start = i
        while i + 1 < len(sentinels) and sentinels[i + 1] - sentinels[i] == 19:
            i += 1
        if i - run_start + 1 >= 2:
            for j in range(run_start, i + 1):
                s_off = sentinels[j]
                # 19-byte record layout (from sentinel start):
                #   +0..+3   sentinel `ff ff ff ff`
                #   +4..+7   uint32 pad_shape_enum (indexes Section B)
                #   +8..+11  int32 A (local X)
                #   +12..+15 int32 B (local Y)
                #   +16..+18 3 zero padding bytes
                A = int.from_bytes(data[s_off+8:s_off+12], "little", signed=True)
                B = int.from_bytes(data[s_off+12:s_off+16], "little", signed=True)
                pins.append((A, B))
        i += 1
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
        f0 = int.from_bytes(data[p+8:p+12], "little", signed=True)
        f1 = int.from_bytes(data[p+12:p+16], "little", signed=True)
        f2 = int.from_bytes(data[p+16:p+20], "little", signed=True)
        f3 = int.from_bytes(data[p+20:p+24], "little", signed=True)
        rot = int.from_bytes(data[p+24:p+28], "little", signed=True)
        fp_id = int.from_bytes(data[p+28:p+32], "little", signed=True)
        # uint32 chip_class at +32..+35. The 8 bytes at +36..+43 are
        # still zero in 100% of T480 records — only +32..+35 carries
        # the new field. See _Chip docstring for the enum values.
        chip_class = int.from_bytes(data[p+32:p+36], "little")
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
        # Walk the 5 packed Pascal strings starting at after-Pascal +45.
        # Each string: 1 length byte + L content bytes. Slots:
        #   0 = device value     (passive value, or "*" for ICs)
        #   1 = tolerance %
        #   2 = tolerance %      (duplicate of slot 1 in 100 % of cases)
        #   3 = footprint name   (= master[fp_id-1].name; verified)
        #   4 = part / supplier code
        # If any string fails to parse cleanly (bad length / non-ASCII)
        # we bail out — the record is malformed or our offsets are off.
        slots: List[str] = []
        sp = p + 45
        slots_ok = True
        for _ in range(5):
            if sp >= len(data):
                slots_ok = False
                break
            sL = data[sp]
            if sL > 100 or sp + 1 + sL > len(data):
                slots_ok = False
                break
            sb = data[sp+1:sp+1+sL]
            if not all(0x20 <= b < 0x7f for b in sb):
                slots_ok = False
                break
            slots.append(sb.decode("ascii"))
            sp += 1 + sL
        if not slots_ok:
            i += 1
            continue
        device_value, tolerance, _tol_dup, _fp_name_dup, part_code = slots
        # After slot 4: 4 zero bytes, then uint32 pin_count, uint32
        # mystery flag, 4 more bytes, then the pin-record run.
        if sp + 16 > len(data):
            i += 1
            continue
        pin_count = int.from_bytes(data[sp+4:sp+8], "little")
        pin_list_pos = sp + 16
        # Walk pin records (variable stride 17 + L per pin).
        pin_records: List[Tuple[int, int, str]] = []
        pos = pin_list_pos
        first_ptr: Optional[int] = None
        for _ in range(min(pin_count, 5000)):  # safety cap
            if pos + 18 > len(data):
                break
            ptr = int.from_bytes(data[pos:pos+4], "little")
            pin_display = int.from_bytes(data[pos+8:pos+12], "little")
            L_name = data[pos+12]
            if L_name < 1 or L_name > 12:
                break
            name_bytes = data[pos+13:pos+13+L_name]
            if not all(0x20 <= b < 0x7f for b in name_bytes):
                break
            name = name_bytes.decode("ascii")
            if first_ptr is None:
                first_ptr = ptr
                master_pin_idx = 0
            else:
                master_pin_idx = (ptr - first_ptr) // 8
            pin_records.append((master_pin_idx, pin_display, name))
            pos += 17 + L_name
        if refdes not in chips:
            chips[refdes] = _Chip(
                refdes=refdes, record_off=i + 4,
                Y=Y, X=X, f0=f0, f1=f1, f2=f2, f3=f3,
                rot=rot, fp_id=fp_id, chip_class=chip_class,
                device_value=device_value, tolerance=tolerance,
                part_code=part_code, pins=pin_records,
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
    residual = 0 against actual pad world coords for non-connector
    chips. Class-17 (connector) chips require the additional negation
    step below, verified on JKBL1 (38/38 pins), JTAG2 (8/8 pins),
    plus pin-1 spot-checks on JLAN1, JDOCK1, JHDMI1, JUSBC1 and J41.
    """
    rot = chip.rot
    if   rot ==   0: dy, dx = -A, -B
    elif rot ==  90: dy, dx = -B,  A
    elif rot == 180: dy, dx =  A,  B
    elif rot == 270: dy, dx =  B, -A
    else:            dy, dx =  A,  B  # unusual rotation, accept verbatim
    if chip.chip_class == 17:
        # Connector pin frame matches canonical only for the two
        # "natural" cases: rot ∈ {0, 180} on BOTTOM. In all other
        # placements (TOP-side, or rotated 90°/270° on BOTTOM) the
        # frame is inverted and we negate (dy, dx) BEFORE the BOTTOM
        # dx-flip. Empirically verified:
        #   rot=0   BOTTOM   no-negate  (JTAG2, JSIM1, JWLAN1, JFAN1)
        #   rot=180 BOTTOM   no-negate  (JLCD1, JPWR1, JDDR1, JDDR2)
        #   rot=0   TOP      negate     (JTP1)
        #   rot=180 TOP      negate     (JKBL1, J41)
        #   rot=90  BOTTOM   negate     (JLAN1, JSD1, JUSB1, JHDMI1)
        #   rot=270 BOTTOM   negate     (JDOCK1, JUSBC1, JHP1, JSPK1)
        if not (rot in (0, 180) and layer == "BOTTOM"):
            dy, dx = -dy, -dx
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
# Net-name table
# --------------------------------------------------------------------------

def _scan_net_names(data: bytes) -> List[str]:
    """Locate and decode the net-name table.

    The table has a 2-uint32 header where the count is repeated twice,
    followed by `count` Pascal-prefixed net names. On T480 it lives at
    `0xb384fe` (count = 2774), but to be sub-variant-tolerant we scan
    for the duplicated-count + valid-first-Pascal signature.

    Returns a list where index `net_id` -> net name. **Net IDs are
    0-based**: ``nets[0]`` is the first Pascal-prefixed name in the
    table (verified against ground-truth from BoardViewer.exe — GND
    at index 45, USBCOMP at 1446, PECI at 1330, etc.).
    """
    # Scan a likely region. The table sits roughly between the chip
    # tables and the master pool on Compal/Lenovo files.
    for i in range(0xa00000, min(0xc40000, len(data) - 16), 1):
        c1 = int.from_bytes(data[i:i+4], "little")
        if not (100 <= c1 <= 20000):
            continue
        c2 = int.from_bytes(data[i+4:i+8], "little")
        if c1 != c2:
            continue
        # Verify the first Pascal name parses as ASCII text.
        L = data[i+8]
        if not (1 <= L <= 80):
            continue
        first = data[i+9:i+9+L]
        if not all(0x20 <= b < 0x7f for b in first):
            continue
        # Try to walk all `count` Pascal strings; if any fails the
        # candidate is not the real net table. The file uses 0-based
        # net_ids (verified) — no leading placeholder entry.
        nets: List[str] = []
        pos = i + 8
        ok = True
        for _ in range(c1):
            if pos >= len(data):
                ok = False
                break
            sL = data[pos]
            if sL > 200 or pos + 1 + sL > len(data):
                ok = False
                break
            sb = data[pos+1:pos+1+sL]
            if not all(0x20 <= b < 0x7f for b in sb):
                ok = False
                break
            nets.append(sb.decode("ascii"))
            pos += 1 + sL
        if ok and len(nets) == c1:
            return nets
    return []


# --------------------------------------------------------------------------
# Layer pad records — spatial index for pin-net matching
# --------------------------------------------------------------------------

def _build_pad_index(data: bytes) -> Dict[Tuple[int, int], int]:
    """Scan the entire file for 19-byte pad records and return
    ``{(world_Y, world_X) -> net_id}``.

    Layer pad record (per agent-confirmed Compal layout):
        +0..+1   2 zero bytes  (record sentinel)
        +2       1 byte         pad-shape enum (0=plain SMD, 1=variant,
                                2=through-hole/via, 5=BGA annular ring,
                                8=press-fit / mounting tab)
        +3..+6   uint32 net_id  (< 4000)
        +7..+10  uint32 pad_type (>= 1, < 100000)
        +11..+14 int32  Y world
        +15..+18 int32  X world

    Earlier drafts required 3 zero bytes at +0..+2 (treating the
    pad-shape byte as part of the sentinel). That hid 11,175 unique
    pad coords — primarily connector through-holes, BGA-style pads
    with annular rings, and press-fit mounting tabs — concentrated
    in the 0x008d8000..0x00914012 region (the gap between GND1 end
    and BOTTOM trace start). Without those, all class-17 connectors
    (JKBL1, JTAG2, JDOCK1, ...) returned empty pin → net mappings.

    Pads appear in every layer the net touches (1..10 per pin), all
    sharing the same `(Y, X)`. We dedup by coord, keeping the first
    seen net_id. The byte-aligned advance (`i += 1` after a match)
    is required because the dense gap-region records don't sit on a
    stride-19 grid relative to file position 0 — stride-19 advance
    happened to work historically only because of zero-padded slack
    between the early layer regions.
    """
    index: Dict[Tuple[int, int], int] = {}
    i = 0
    n = len(data) - 19
    while i < n:
        # Cheapest pre-check: 2 zero bytes at +0..+1. ~half the file
        # bytes are non-zero so this skips most positions quickly.
        if data[i] != 0 or data[i+1] != 0:
            i += 1
            continue
        net_id = int.from_bytes(data[i+3:i+7], "little")
        if net_id >= 4000:
            i += 1
            continue
        pad_type = int.from_bytes(data[i+7:i+11], "little")
        # pad_type==0 is the most common false-positive at this stage
        # (zero-padded alignment slack between regions). Real pad_types
        # start at 1 and stay below ~280 in the per-layer aperture
        # table; the 100k cap admits all-the-way-to-master-pad-shape
        # references (which can go higher) while rejecting random ints.
        if pad_type == 0 or pad_type >= 100000:
            i += 1
            continue
        Y = int.from_bytes(data[i+11:i+15], "little", signed=True)
        X = int.from_bytes(data[i+15:i+19], "little", signed=True)
        if abs(Y) > 2_000_000 or abs(X) > 2_000_000:
            i += 1
            continue
        key = (Y, X)
        if key not in index:
            index[key] = net_id
        # Byte-aligned advance — see docstring.
        i += 1
    return index


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

def parse(path: Path) -> BoardModel:
    """Parse a Compal/Lenovo TVW file into a BoardModel.

    Produces chips with correct positions, rotations, layers,
    per-pin shape geometry from master footprints, real pin names from
    the chip's inline pin records, and **pin-net mapping via spatial
    matching against the 19-byte layer pad records**.

    Pin-net coverage: each chip pin's world coord is computed via the
    canonical transform; we look it up in a deduped pad index built
    from all 19-byte layer-pad records in the file. Exact-coord
    matching works because chip coords are integer file units and
    rotation angles are multiples of 90°, so the transform output is
    deterministic with zero residual.

    Pins whose predicted world coord doesn't match any pad are treated
    as no-connect (NC) and don't appear in `model.signals`.
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
    nets = _scan_net_names(data)
    pad_index = _build_pad_index(data)

    n_no_master_pins = 0
    n_pin_net_hits = 0
    n_pin_net_misses = 0
    for refdes, chip in r3_chips.items():
        master = masters[chip.fp_id - 1]
        layer = _determine_layer(refdes, master.name, r1, cap_layers)
        shape_name = f"_compal_{master.name}_{refdes}"
        shape = Shape(name=shape_name)

        # Build a lookup of master_pin_idx -> (display_pin_number, name)
        # so we can emit real BGA names like "A148" instead of sequential
        # integers. Master pins not present in the chip's inline list
        # (rare — usually means a chip-NC ball) fall back to a
        # sequential name based on master order.
        by_master_idx: Dict[int, Tuple[int, str]] = {
            m_idx: (pin_disp, pin_name)
            for (m_idx, pin_disp, pin_name) in chip.pins
        }

        if master.pin_locals:
            for pin_idx, (A, B) in enumerate(master.pin_locals):
                world_Y, world_X = _pin_local_to_world(chip, A, B, layer)
                dx, dy = _world_to_chip_local(chip, world_Y, world_X)
                pin_disp, _pin_name = by_master_idx.get(
                    pin_idx, (pin_idx + 1, str(pin_idx + 1)))
                # Use the integer display number as the pin name —
                # matches what BoardViewer.exe shows in its Pin column.
                pin_name_out = str(pin_disp)
                shape.pins.append((pin_name_out, dx, dy))
                # Look up this pin's net via exact-coord match against
                # the pad index.
                net_id = pad_index.get((world_Y, world_X))
                if net_id is not None and net_id < len(nets):
                    net_name = nets[net_id]
                    if net_name:
                        model.signals.setdefault(net_name, []).append(
                            (refdes, pin_name_out))
                        n_pin_net_hits += 1
                    else:
                        n_pin_net_misses += 1
                else:
                    n_pin_net_misses += 1
            xs = [p[1] for p in shape.pins]
            ys = [p[2] for p in shape.pins]
            shape.bbox_override = (min(xs), min(ys), max(xs), max(ys))
        else:
            n_no_master_pins += 1
            shape.bbox_override = (-1000.0, -1000.0, 1000.0, 1000.0)

        model.shapes[shape_name] = shape
        # Component.device: prefer the device value from the R3 packed
        # Pascal strings (e.g. "10K", "0.1U", "1U") for passives. ICs
        # have device_value == "*" — use the master footprint name
        # instead so the user sees something meaningful in the UI.
        if chip.device_value and chip.device_value != "*":
            device_label = chip.device_value
        else:
            device_label = master.name
        comp = Component(
            refdes=refdes, x=float(chip.X), y=float(chip.Y),
            layer=layer, rotation=float(chip.rot),
            shape=shape_name,
            device=device_label,
        )
        model.components[refdes] = comp

    warns: List[str] = []
    if n_no_master_pins:
        warns.append(
            f"{n_no_master_pins} chips reference master footprints "
            f"with no pin-coordinate records (rendered as placeholder "
            f"bboxes).")
    if not nets:
        warns.append(
            "net-name table not found — net browsing will show no "
            "net names (only IDs).")
    if warns:
        model.warnings = warns
    return model


__all__ = ["parse"]
