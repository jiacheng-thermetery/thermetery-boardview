# SPDX-License-Identifier: MIT
#
# Python port of OpenBoardView's XZZPCBFile.cpp/.h (parser) and of
# dhuertas/DES (decryption). Both upstreams are MIT — kept under MIT
# here for upstream consistency. Full permission notices and per-author
# attribution: LICENSES/OpenBoardView-MIT.txt and LICENSES/dhuertas-
# DES-MIT.txt. See also THIRD_PARTY_NOTICES.md.
#
#   Copyright (c) 2016 Chloridite and OpenBoardView contributors
#   Copyright (c) 2020 Dani Huertas
#   Copyright (C) 2026 Thermetery Technology LLC (Python port + the
#                     layer-split, keyfile-resolver, and BoardModel-
#                     mapping additions on top of the port).

"""
Parse XZZPCB V1.0 boardview files (Chinese phone/board-repair format,
"XZZ .pcb") into a BoardModel compatible with the rest of the toolchain.

Format reference: this module is a Python port of OpenBoardView's
XZZPCBFile.cpp / .h. The DES decryption used to unwrap part/pin records
is a port of dhuertas/DES (also reproduced inside OpenBoardView under
src/openboardview/Crypto/des.c). Both upstreams are MIT-licensed; their
verbatim license texts are reproduced under LICENSES/.

  Upstream parser : https://github.com/OpenBoardView/OpenBoardView
                    Copyright (c) 2016 Chloridite and OpenBoardView contributors
                    Format reversal credit: @huertas (DES), @inflex,
                    @MuertoGB, @slimeinacloak, @piernov, Thomas Lamy
                    See LICENSES/OpenBoardView-MIT.txt

  Upstream DES    : https://github.com/dhuertas/DES
                    Copyright (c) 2020 Dani Huertas
                    See LICENSES/dhuertas-DES-MIT.txt

----- File structure -----

    Header @ 0x00
        "XZZPCB V1.0\0" magic (or XOR-obfuscated, see below)
        ...
        u32 @ 0x20  : main_data_offset (relative)
        u32 @ 0x28  : net_data_offset  (relative)

    main_data_start = 0x20 + main_data_offset
        u32 main_data_blocks_size; then a stream of typed records:
            u8 type, u32 size, <size> bytes payload
        Types:
            0x01 ARC        — only layer 28 (board edges) kept
            0x02 VIA        — skipped
            0x05 LINE SEG   — only layer 28 (board edges) kept
            0x06 TEXT       — skipped
            0x07 PART       — DES-encrypted; contains nested sub-blocks
                              (parts + pins). Requires a valid 64-bit
                              key. Without one, type-7 records are
                              ignored and parse() returns a partial
                              BoardModel.
            0x09 TEST PAD   — standalone test pads / drill holes

    net_data_start = 0x20 + net_data_offset
        u32 net_block_size; then entries of:
            u32 entry_size, u32 net_index, <entry_size-8> bytes ASCII

    Optional XOR obfuscation: if the byte at 0x10 is non-zero, the buffer
    from offset 0 up to (but not including) the literal sentinel
    "v6v6555v6v6" is XOR'd with that byte.

----- Coordinate scaling -----

    Internal coords are 1e-7 inch (so 100,000 raw units == 1 mil). All
    geometry returned by this parser is in MILS — divided by
    `XZZ_GLOBAL_SCALE = 10000`. This matches the existing BRD parser
    convention.

----- Key handling -----

    `parse(path, key=...)` accepts the key as a 64-bit unsigned int. If
    omitted, the parser checks (in order):
        1. environment variable XZZPCB_KEY (hex string)
        2. private/XZZ_Key.txt next to the parser (or lowercase
           private/xzz_key.txt — same convention as private/fz_key.txt)
        3. legacy config file ~/.boardviewer/xzz_key (hex string)
    The key file is forgiving: takes the first 16 hex digits found,
    `#`-prefixed comments are stripped.

    A key is "valid" iff it passes the parity check from XZZPCBFile.cpp:
    bytes 0..6 must have even popcount, byte 7 must have odd popcount.

    If no valid key is found, the parser still returns a BoardModel with
    the outline, test pads, and net table populated; encrypted part/pin
    records are skipped and a warning is written to model.warnings.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from gencad_parser import BoardModel, Component, Shape


XZZ_GLOBAL_SCALE = 10000  # raw -> mils
XZZPCB_MAGIC = b"XZZPCB"
V6_SENTINEL = b"v6v6555v6v6"


# ============================================================================
# DES — port of https://github.com/dhuertas/DES (MIT, see LICENSES/)
# ============================================================================

# Initial permutation
_IP = (
    58, 50, 42, 34, 26, 18, 10,  2,
    60, 52, 44, 36, 28, 20, 12,  4,
    62, 54, 46, 38, 30, 22, 14,  6,
    64, 56, 48, 40, 32, 24, 16,  8,
    57, 49, 41, 33, 25, 17,  9,  1,
    59, 51, 43, 35, 27, 19, 11,  3,
    61, 53, 45, 37, 29, 21, 13,  5,
    63, 55, 47, 39, 31, 23, 15,  7,
)

# Inverse initial permutation
_PI = (
    40,  8, 48, 16, 56, 24, 64, 32,
    39,  7, 47, 15, 55, 23, 63, 31,
    38,  6, 46, 14, 54, 22, 62, 30,
    37,  5, 45, 13, 53, 21, 61, 29,
    36,  4, 44, 12, 52, 20, 60, 28,
    35,  3, 43, 11, 51, 19, 59, 27,
    34,  2, 42, 10, 50, 18, 58, 26,
    33,  1, 41,  9, 49, 17, 57, 25,
)

# Expansion (32 -> 48)
_E = (
    32,  1,  2,  3,  4,  5,
     4,  5,  6,  7,  8,  9,
     8,  9, 10, 11, 12, 13,
    12, 13, 14, 15, 16, 17,
    16, 17, 18, 19, 20, 21,
    20, 21, 22, 23, 24, 25,
    24, 25, 26, 27, 28, 29,
    28, 29, 30, 31, 32,  1,
)

# Post-S-Box permutation
_P = (
    16,  7, 20, 21, 29, 12, 28, 17,
     1, 15, 23, 26,  5, 18, 31, 10,
     2,  8, 24, 14, 32, 27,  3,  9,
    19, 13, 30,  6, 22, 11,  4, 25,
)

# 8 S-boxes, each 4x16
_S = (
    (
        14,  4, 13,  1,  2, 15, 11,  8,  3, 10,  6, 12,  5,  9,  0,  7,
         0, 15,  7,  4, 14,  2, 13,  1, 10,  6, 12, 11,  9,  5,  3,  8,
         4,  1, 14,  8, 13,  6,  2, 11, 15, 12,  9,  7,  3, 10,  5,  0,
        15, 12,  8,  2,  4,  9,  1,  7,  5, 11,  3, 14, 10,  0,  6, 13,
    ), (
        15,  1,  8, 14,  6, 11,  3,  4,  9,  7,  2, 13, 12,  0,  5, 10,
         3, 13,  4,  7, 15,  2,  8, 14, 12,  0,  1, 10,  6,  9, 11,  5,
         0, 14,  7, 11, 10,  4, 13,  1,  5,  8, 12,  6,  9,  3,  2, 15,
        13,  8, 10,  1,  3, 15,  4,  2, 11,  6,  7, 12,  0,  5, 14,  9,
    ), (
        10,  0,  9, 14,  6,  3, 15,  5,  1, 13, 12,  7, 11,  4,  2,  8,
        13,  7,  0,  9,  3,  4,  6, 10,  2,  8,  5, 14, 12, 11, 15,  1,
        13,  6,  4,  9,  8, 15,  3,  0, 11,  1,  2, 12,  5, 10, 14,  7,
         1, 10, 13,  0,  6,  9,  8,  7,  4, 15, 14,  3, 11,  5,  2, 12,
    ), (
         7, 13, 14,  3,  0,  6,  9, 10,  1,  2,  8,  5, 11, 12,  4, 15,
        13,  8, 11,  5,  6, 15,  0,  3,  4,  7,  2, 12,  1, 10, 14,  9,
        10,  6,  9,  0, 12, 11,  7, 13, 15,  1,  3, 14,  5,  2,  8,  4,
         3, 15,  0,  6, 10,  1, 13,  8,  9,  4,  5, 11, 12,  7,  2, 14,
    ), (
         2, 12,  4,  1,  7, 10, 11,  6,  8,  5,  3, 15, 13,  0, 14,  9,
        14, 11,  2, 12,  4,  7, 13,  1,  5,  0, 15, 10,  3,  9,  8,  6,
         4,  2,  1, 11, 10, 13,  7,  8, 15,  9, 12,  5,  6,  3,  0, 14,
        11,  8, 12,  7,  1, 14,  2, 13,  6, 15,  0,  9, 10,  4,  5,  3,
    ), (
        12,  1, 10, 15,  9,  2,  6,  8,  0, 13,  3,  4, 14,  7,  5, 11,
        10, 15,  4,  2,  7, 12,  9,  5,  6,  1, 13, 14,  0, 11,  3,  8,
         9, 14, 15,  5,  2,  8, 12,  3,  7,  0,  4, 10,  1, 13, 11,  6,
         4,  3,  2, 12,  9,  5, 15, 10, 11, 14,  1,  7,  6,  0,  8, 13,
    ), (
         4, 11,  2, 14, 15,  0,  8, 13,  3, 12,  9,  7,  5, 10,  6,  1,
        13,  0, 11,  7,  4,  9,  1, 10, 14,  3,  5, 12,  2, 15,  8,  6,
         1,  4, 11, 13, 12,  3,  7, 14, 10, 15,  6,  8,  0,  5,  9,  2,
         6, 11, 13,  8,  1,  4, 10,  7,  9,  5,  0, 15, 14,  2,  3, 12,
    ), (
        13,  2,  8,  4,  6, 15, 11,  1, 10,  9,  3, 14,  5,  0, 12,  7,
         1, 15, 13,  8, 10,  3,  7,  4, 12,  5,  6, 11,  0, 14,  9,  2,
         7, 11,  4,  1,  9, 12, 14,  2,  0,  6, 10, 13, 15,  3,  5,  8,
         2,  1, 14,  7,  4, 10,  8, 13, 15, 12,  9,  0,  3,  5,  6, 11,
    ),
)

# Permuted Choice 1 (key 64 -> 56)
_PC1 = (
    57, 49, 41, 33, 25, 17,  9,
     1, 58, 50, 42, 34, 26, 18,
    10,  2, 59, 51, 43, 35, 27,
    19, 11,  3, 60, 52, 44, 36,
    63, 55, 47, 39, 31, 23, 15,
     7, 62, 54, 46, 38, 30, 22,
    14,  6, 61, 53, 45, 37, 29,
    21, 13,  5, 28, 20, 12,  4,
)

# Permuted Choice 2 (key 56 -> 48)
_PC2 = (
    14, 17, 11, 24,  1,  5,
     3, 28, 15,  6, 21, 10,
    23, 19, 12,  4, 26,  8,
    16,  7, 27, 20, 13,  2,
    41, 52, 31, 37, 47, 55,
    30, 40, 51, 45, 33, 48,
    44, 49, 39, 56, 34, 53,
    46, 42, 50, 36, 29, 32,
)

# Per-round shift counts
_ITER_SHIFT = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)


def _permute(value: int, table: Tuple[int, ...], in_bits: int) -> int:
    """Apply a 1-indexed permutation table to `value` (in_bits wide).
    Bit numbering matches FIPS 46-3: bit 1 is the MSB of the input."""
    out = 0
    for pos in table:
        out = (out << 1) | ((value >> (in_bits - pos)) & 1)
    return out


def des(input_block: int, key: int, mode: str) -> int:
    """Single-block DES. `input_block` and `key` are 64-bit unsigned ints.
    `mode` is 'e' for encrypt or 'd' for decrypt. Returns the 64-bit
    block. Faithful port of dhuertas/DES.

    Note: the standard DES key uses 56 effective bits — the LSB of each
    of the 8 key bytes is a parity bit and is discarded by PC1. The XZZ
    format uses the full 64-bit value as a key handle, validated by an
    8-byte parity check (see `check_key`)."""

    init_perm = _permute(input_block, _IP, 64)
    L = (init_perm >> 32) & 0xFFFFFFFF
    R = init_perm & 0xFFFFFFFF

    pc1 = _permute(key, _PC1, 64)
    C = (pc1 >> 28) & 0x0FFFFFFF
    D = pc1 & 0x0FFFFFFF

    sub_keys = []
    for shift in _ITER_SHIFT:
        C = ((C << shift) | (C >> (28 - shift))) & 0x0FFFFFFF
        D = ((D << shift) | (D >> (28 - shift))) & 0x0FFFFFFF
        cd = (C << 28) | D
        sub_keys.append(_permute(cd, _PC2, 56))

    for i in range(16):
        s_input = _permute(R, _E, 32)
        s_input ^= sub_keys[15 - i] if mode == "d" else sub_keys[i]

        s_output = 0
        for j in range(8):
            chunk = (s_input >> (42 - 6 * j)) & 0x3F
            row = ((chunk >> 4) & 0x2) | (chunk & 0x1)
            col = (chunk >> 1) & 0xF
            s_output = (s_output << 4) | (_S[j][16 * row + col] & 0xF)

        f_res = _permute(s_output, _P, 32)
        L, R = R, L ^ f_res

    pre_output = (R << 32) | L
    return _permute(pre_output, _PI, 64)


def _des_decrypt_buf(buf: bytes, key: int) -> bytes:
    """Decrypt `buf` (multiple of 8 bytes) using DES in ECB mode with
    big-endian block conversion. Mirrors XZZPCBFile::des_decrypt.

    If the optional `xzz_native` C extension is available (~100x faster
    than this pure-Python loop), uses it transparently. We deliberately
    do NOT cache decrypted results to disk — the plaintext is the
    proprietary file's content, and a cache file leaks that content."""
    try:
        from xzz_native import decrypt as _native_decrypt
        native = _native_decrypt(buf, key)
        if native is not None:
            return native
    except ImportError:
        pass
    out = bytearray(len(buf))
    for i in range(0, len(buf) - (len(buf) % 8), 8):
        block_in = int.from_bytes(buf[i:i + 8], "big")
        block_out = des(block_in, key, "d")
        out[i:i + 8] = block_out.to_bytes(8, "big")
    # Trailing bytes that don't fill a block are left as zero, matching
    # the C++ reference (which only writes within the loop's inpos +i
    # bound — anything past is the buffer's default-initialized zero).
    return bytes(out)


# ----- Key parity check (mirrors XZZPCBFile::checkKey) ----------------------

_REQUIRED_KEY_PARITY = (1, 1, 1, 1, 1, 1, 1, 0)


def check_key(key: int) -> bool:
    """Apply the XZZ key-parity check from XZZPCBFile.cpp. The parity
    of byte i of the 64-bit key (LSB-first byte ordering, matching the C
    `(key >> (i*8)) & 0xff`) must equal `1 - popcount_lsb`, with the
    expected pattern {1,1,1,1,1,1,1,0} — i.e. bytes 0..6 have even
    popcount, byte 7 has odd popcount."""
    for i in range(8):
        b = (key >> (i * 8)) & 0xFF
        # Fold all 8 bits into bit 0
        b ^= b >> 4
        b ^= b >> 2
        b ^= b >> 1
        bit = (~b) & 1
        if bit != _REQUIRED_KEY_PARITY[i]:
            return False
    return True


# ============================================================================
# Header / record-stream helpers
# ============================================================================

def _u32(buf: bytes, pos: int) -> int:
    return struct.unpack_from("<I", buf, pos)[0]


def verify_format(buf: bytes) -> bool:
    """Return True iff this is an XZZPCB file (raw or XOR-obfuscated)."""
    if len(buf) < 6:
        return False
    if buf[:6] == XZZPCB_MAGIC:
        return True
    # XOR-obfuscated case: byte 0x10 holds the XOR key
    if len(buf) > 0x10 and buf[0x10] != 0:
        xor = buf[0x10]
        return bytes(b ^ xor for b in buf[:6]) == XZZPCB_MAGIC
    return False


def _deobfuscate(buf: bytearray) -> bytearray:
    """If buf[0x10] is non-zero, XOR everything from offset 0 up to (but
    not including) the v6v6555v6v6 sentinel with that byte. Mirrors the
    obfuscation strip in XZZPCBFile's constructor."""
    if len(buf) <= 0x10 or buf[0x10] == 0:
        return buf
    xor = buf[0x10]
    sentinel = buf.find(V6_SENTINEL)
    end = sentinel if sentinel >= 0 else len(buf)
    for i in range(end):
        buf[i] ^= xor
    return buf


# ============================================================================
# Parser
# ============================================================================

@dataclass
class _RawPin:
    """Pin record from a decrypted part block, before mapping to BoardModel."""
    name: str
    x: int  # mils
    y: int  # mils
    net_index: int


@dataclass
class _RawPart:
    """Part record from a decrypted part block."""
    name: str
    pins: List[_RawPin]
    layer: str = "TOP"  # set by _split_layers when the file holds two views


@dataclass
class _RawTestPad:
    name: str
    x: int  # mils
    y: int  # mils
    net_index: int
    layer: str = "TOP"  # set by _split_layers when the file holds two views


class XZZPCBParser:
    def __init__(self, buf: bytes, key: Optional[int]) -> None:
        self.warnings: List[str] = []
        self.outline: List[Tuple[Tuple[int, int], Tuple[int, int]]] = []
        self.parts: List[_RawPart] = []
        self.test_pads: List[_RawTestPad] = []
        self.nets: Dict[int, str] = {}

        if not verify_format(buf):
            raise ValueError("not an XZZPCB file (magic mismatch)")

        # Working copy — _deobfuscate mutates in place.
        work = bytearray(buf)
        _deobfuscate(work)

        # Header pointers are relative to 0x20.
        main_data_offset = _u32(work, 0x20)
        net_data_offset = _u32(work, 0x28)
        main_data_start = 0x20 + main_data_offset
        net_data_start = 0x20 + net_data_offset

        # ---- Net table (always cleartext) -----------------------------------
        net_block_size = _u32(work, net_data_start)
        net_buf = work[net_data_start + 4 : net_data_start + 4 + net_block_size]
        self._parse_net_block(net_buf)

        # ---- Main records ---------------------------------------------------
        main_blocks_size = _u32(work, main_data_start)
        self.key = key if (key is not None and check_key(key)) else None
        if key is not None and self.key is None:
            self.warnings.append(
                f"XZZPCB key 0x{key:016x} failed parity check; "
                "encrypted part/pin records will be skipped"
            )
        elif self.key is None:
            self.warnings.append(
                "XZZPCB key not provided; encrypted part/pin records "
                "will be skipped. Drop a 16-hex-digit key into "
                "private/XZZ_Key.txt, set XZZPCB_KEY in the environment, "
                "or pass key= to parse()."
            )

        self._process_blocks(work, main_data_start + 4, main_blocks_size)

        # ---- Split top + bottom views, if the outline has two rects --------
        # XZZ files draw the BOTTOM side as a second outline rectangle
        # adjacent to the TOP rectangle, with all bottom-side parts placed
        # there in the mirrored "as if you flipped the board" coordinate
        # system. Without this step, the viewer would render top and
        # bottom side-by-side as a single ~580mm-wide strip and leave
        # the TOP/BOTTOM toggle inert.
        self._split_layers()

        # ---- Translate to origin --------------------------------------------
        # Mirror find_xy_translation: subtract the lower-left of the
        # outline from every coord so the board sits at >= 0.
        if self.outline:
            min_x = min(min(s[0][0], s[1][0]) for s in self.outline)
            min_y = min(min(s[0][1], s[1][1]) for s in self.outline)
            self.outline = [
                ((x1 - min_x, y1 - min_y), (x2 - min_x, y2 - min_y))
                for (x1, y1), (x2, y2) in self.outline
            ]
            for tp in self.test_pads:
                tp.x -= min_x
                tp.y -= min_y
            for part in self.parts:
                for pin in part.pins:
                    pin.x -= min_x
                    pin.y -= min_y

    # ---- Top/bottom split ---------------------------------------------------

    def _split_layers(self) -> None:
        """Detect whether the outline encodes two adjacent rectangles
        (the XZZ convention for storing TOP and BOTTOM side views in
        one file) and, if so, classify every part / test pad / outline
        segment by which rectangle it lives in. BOTTOM-rect items have
        their x mirrored back into the TOP coord system so the rest
        of the viewer can use a single coordinate space and the
        existing TOP↔BOTTOM toggle handles the visual
        flip. Single-rectangle files (rare for this format but
        possible) leave the default layer="TOP" untouched."""
        if not self.outline:
            return
        rects = _split_outline_into_rects(self.outline)
        if len(rects) != 2:
            return  # single board, or something we don't recognise

        # Identify left vs right by min x of the bbox.
        bboxes = [_bbox_of_segments(r) for r in rects]
        left_idx = 0 if bboxes[0][0] < bboxes[1][0] else 1
        L_min, _, L_max, _ = bboxes[left_idx]
        R_min, _, R_max, _ = bboxes[1 - left_idx]
        if R_min <= L_max:
            return  # rectangles overlap — not a side-by-side layout

        midpoint = (L_max + R_min) / 2.0

        def mirror_x(x: float) -> float:
            # Map the BOTTOM-view x into the TOP coord system. The XZZ
            # right rectangle is the mirror of the left, so:
            #   right_edge_of_right (R_max) -> left_edge_of_left (L_min)
            #   left_edge_of_right (R_min)  -> right_edge_of_left (L_max)
            # Formula: mirrored = L_min + R_max - x
            return L_min + R_max - x

        # Reclassify outline: drop the right (BOTTOM-view) rectangle —
        # it's a duplicate of the left in mirrored coords. The viewer
        # will mirror the TOP outline horizontally when the user
        # toggles to BOTTOM, so we don't need to keep both.
        self.outline = list(rects[left_idx])

        # Reclassify parts. Use the centroid (already implicit in the
        # part's pins) — if any pin is on the right side, the whole
        # part is BOTTOM. Real parts don't span the gap.
        for part in self.parts:
            if not part.pins:
                continue
            pin_xs = [p.x for p in part.pins]
            if min(pin_xs) >= midpoint:
                part.layer = "BOTTOM"
                for p in part.pins:
                    p.x = mirror_x(p.x)
            elif max(pin_xs) <= midpoint:
                part.layer = "TOP"
            # else: straddles the midpoint — leave as TOP and warn
            # (we haven't seen this on real files but want a signal if
            # it ever happens).
            else:
                self.warnings.append(
                    f"part {part.name!r} straddles the TOP/BOTTOM "
                    f"divider at x={midpoint:.0f}; left as TOP"
                )

        # Same for test pads, by their own x.
        for tp in self.test_pads:
            if tp.x >= midpoint:
                tp.layer = "BOTTOM"
                tp.x = mirror_x(tp.x)
            else:
                tp.layer = "TOP"

    # ---- Net block ----------------------------------------------------------

    def _parse_net_block(self, buf: bytes) -> None:
        p = 0
        while p + 8 <= len(buf):
            entry_size = _u32(buf, p); p += 4
            net_index = _u32(buf, p); p += 4
            name_len = entry_size - 8
            if name_len < 0 or p + name_len > len(buf):
                self.warnings.append(
                    f"truncated net entry at offset {p}; stopping"
                )
                return
            name = buf[p : p + name_len].decode("ascii", errors="replace")
            self.nets[net_index] = name.rstrip("\x00")
            p += name_len

    # ---- Main record stream -------------------------------------------------

    def _process_blocks(self, buf: bytes, start: int, total: int) -> None:
        end = start + total
        p = start
        while p < end:
            if p + 5 > len(buf):
                self.warnings.append("truncated record header")
                return
            rtype = buf[p]; p += 1
            rsize = _u32(buf, p); p += 4
            if p + rsize > len(buf):
                self.warnings.append(f"record (type={rtype}) extends past EOF")
                return
            payload = bytes(buf[p : p + rsize])
            p += rsize

            if rtype == 0x01:
                self._parse_arc(payload)
            elif rtype == 0x05:
                self._parse_line_seg(payload)
            elif rtype == 0x07:
                if self.key is not None:
                    self._parse_part(payload)
            elif rtype == 0x09:
                self._parse_test_pad(payload)
            elif rtype in (0x02, 0x06):
                pass  # via / text — not currently rendered
            # else: silently skip unknown record types (matches OpenBoardView's
            # warning-but-continue behavior; we just lose the record)

    # ---- Outline (ARC + LINE SEG) ------------------------------------------

    def _parse_line_seg(self, buf: bytes) -> None:
        if len(buf) < 24:
            return
        layer, x1, y1, x2, y2, _scale = struct.unpack_from("<IIIIII", buf, 0)
        if layer != 28:  # board edges only
            return
        self.outline.append((
            (x1 // XZZ_GLOBAL_SCALE, y1 // XZZ_GLOBAL_SCALE),
            (x2 // XZZ_GLOBAL_SCALE, y2 // XZZ_GLOBAL_SCALE),
        ))

    def _parse_arc(self, buf: bytes) -> None:
        if len(buf) < 28:
            return
        layer, x, y, r, ang_s, ang_e, _scale = struct.unpack_from("<IIIIIII", buf, 0)
        if layer != 28:
            return
        cx = x // XZZ_GLOBAL_SCALE
        cy = y // XZZ_GLOBAL_SCALE
        rs = r // XZZ_GLOBAL_SCALE
        a0 = ang_s // XZZ_GLOBAL_SCALE
        a1 = ang_e // XZZ_GLOBAL_SCALE
        self.outline.extend(_arc_to_segments(a0, a1, rs, cx, cy))

    # ---- Test pad (type 9, cleartext) --------------------------------------

    def _parse_test_pad(self, buf: bytes) -> None:
        # Layout: pad_number (u32) | x (u32) | y (u32) |
        #         inner_diam+unknown (8 bytes) | name_len (u32) | name |
        #         padstack info (32 bytes, same as the pin block) |
        #         net_index (u32) | trailing zero padding.
        #
        # Note: OpenBoardView's reference reads net_index from the last
        # 4 bytes of the buffer, but on V389-style files (and any record
        # with trailing padding) that slot is zero. Following the pin
        # block convention — name_end + 32 — gives the actual net_index.
        if len(buf) < 24:
            return
        x = _u32(buf, 4)
        y = _u32(buf, 8)
        name_len = _u32(buf, 20)
        name_start = 24
        net_pos = name_start + name_len + 32
        if net_pos + 4 > len(buf):
            return
        name = buf[name_start : name_start + name_len].decode("ascii", errors="replace")
        net_index = _u32(buf, net_pos)
        self.test_pads.append(_RawTestPad(
            name=name,
            x=x // XZZ_GLOBAL_SCALE,
            y=y // XZZ_GLOBAL_SCALE,
            net_index=net_index,
        ))

    # ---- Part block (type 7, DES-encrypted) --------------------------------

    def _parse_part(self, encrypted: bytes) -> None:
        # Decrypt full block (multiple of 8). XZZ records aren't always
        # 16-aligned but they ARE 8-aligned (DES block size).
        decrypted = _des_decrypt_buf(encrypted, self.key)

        p = 0
        if p + 4 > len(decrypted):
            return
        part_size = _u32(decrypted, p); p += 4
        p += 18  # unknown
        if p + 4 > len(decrypted):
            return
        group_name_size = _u32(decrypted, p); p += 4
        p += group_name_size

        # Expect a 0x06 sub-block giving the part's display name.
        if p >= len(decrypted) or decrypted[p] != 0x06:
            return
        p += 31  # skip 1 (the 0x06) + 30 unknown? — match the C: "current_pointer += 31"
        # After the C++ "buf[current_pointer] == 0x06" check, current_pointer
        # is *not* advanced past the 0x06 — the +=31 happens next, so the
        # 0x06 byte itself is at offset (p_before_check). Replicating that:
        # in C the read happens with current_pointer pointing at 0x06,
        # then current_pointer += 31. Here we already did `p += 31` AFTER
        # incrementing past 0x06? No — we DIDN'T increment past it. The
        # `decrypted[p] != 0x06` check leaves p pointing at the 0x06; then
        # `p += 31` skips it plus 30 more bytes. That matches.
        if p + 4 > len(decrypted):
            return
        part_name_size = _u32(decrypted, p); p += 4
        if p + part_name_size > len(decrypted):
            return
        part_name = decrypted[p : p + part_name_size].decode("ascii", errors="replace")
        p += part_name_size

        pins: List[_RawPin] = []
        end = part_size + 4
        while p < end and p < len(decrypted):
            sub_type = decrypted[p]; p += 1
            if sub_type in (0x01, 0x05, 0x06):
                if p + 4 > len(decrypted):
                    return
                blk_size = _u32(decrypted, p)
                p += 4 + blk_size
            elif sub_type == 0x09:
                pin = self._parse_pin_block(decrypted, p)
                if pin is None:
                    return
                pin_obj, p = pin
                pins.append(pin_obj)
            elif sub_type == 0x00:
                pass  # padding
            else:
                # Unknown sub-block — the C++ logs a warning. We don't
                # know the size, so we can't safely skip; bail on this
                # part to avoid corrupting the stream.
                self.warnings.append(
                    f"part {part_name!r}: unknown sub-block 0x{sub_type:02x}"
                )
                return

        self.parts.append(_RawPart(name=part_name, pins=pins))

    def _parse_pin_block(self, buf: bytes, p: int) -> Optional[Tuple[_RawPin, int]]:
        if p + 4 > len(buf):
            return None
        pin_block_size = _u32(buf, p); p += 4
        pin_block_end = (p - 4) + pin_block_size + 4
        p += 4   # unknown
        if p + 8 > len(buf):
            return None
        x = _u32(buf, p); p += 4
        y = _u32(buf, p); p += 4
        p += 8   # unknown
        if p + 4 > len(buf):
            return None
        name_len = _u32(buf, p); p += 4
        if p + name_len > len(buf):
            return None
        name = buf[p : p + name_len].decode("ascii", errors="replace")
        p += name_len
        p += 32  # unknown
        if p + 4 > len(buf):
            return None
        net_index = _u32(buf, p)

        return (
            _RawPin(
                name=name,
                x=x // XZZ_GLOBAL_SCALE,
                y=y // XZZ_GLOBAL_SCALE,
                net_index=net_index,
            ),
            pin_block_end,
        )


def _bbox_of_segments(
    segs: List[Tuple[Tuple[int, int], Tuple[int, int]]],
) -> Tuple[int, int, int, int]:
    """Return (xmin, ymin, xmax, ymax) over all segment endpoints."""
    xs = [c for s in segs for c in (s[0][0], s[1][0])]
    ys = [c for s in segs for c in (s[0][1], s[1][1])]
    return (min(xs), min(ys), max(xs), max(ys))


def _split_outline_into_rects(
    outline: List[Tuple[Tuple[int, int], Tuple[int, int]]],
    eps_mil: int = 5,
) -> List[List[Tuple[Tuple[int, int], Tuple[int, int]]]]:
    """Group outline segments by connected component — two segments
    are connected if they share an endpoint within `eps_mil` mils. A
    closed rectangle = one component; XZZ files with TOP+BOTTOM views
    side-by-side give two disjoint rectangles → two components."""
    n = len(outline)
    if n == 0:
        return []
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    def near(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
        return abs(a[0] - b[0]) <= eps_mil and abs(a[1] - b[1]) <= eps_mil

    for i in range(n):
        for j in range(i + 1, n):
            if (near(outline[i][0], outline[j][0]) or
                near(outline[i][0], outline[j][1]) or
                near(outline[i][1], outline[j][0]) or
                near(outline[i][1], outline[j][1])):
                union(i, j)

    groups: Dict[int, List[Tuple[Tuple[int, int], Tuple[int, int]]]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(outline[i])
    return list(groups.values())


def _arc_to_segments(a0: int, a1: int, r: int, cx: int, cy: int,
                     n: int = 10) -> List[Tuple[Tuple[int, int], Tuple[int, int]]]:
    """Approximate an arc with line segments. Mirrors xzz_arc_to_segments."""
    import math
    if a0 > a1:
        a0, a1 = a1, a0
    if a1 - a0 > 180:
        a0 += 360
    rad = math.pi / 180.0
    s = a0 * rad
    e = a1 * rad
    step = (e - s) / (n - 1)
    out = []
    px = int(cx + r * math.cos(s))
    py = int(cy + r * math.sin(s))
    for i in range(1, n):
        a = s + i * step
        nx = int(cx + r * math.cos(a))
        ny = int(cy + r * math.sin(a))
        out.append(((px, py), (nx, ny)))
        px, py = nx, ny
    return out


# ============================================================================
# Public API — produce a BoardModel
# ============================================================================

def parse(path: Path, key=None) -> BoardModel:
    """Parse an XZZPCB file into a BoardModel.

    Args:
        path: path to a .pcb (XZZPCB V1.0) file.
        key:  64-bit XZZ key. If None, falls back to env XZZPCB_KEY (hex)
              and then ~/.boardviewer/xzz_key (hex). If still unavailable
              or invalid, returns a partial model with only the cleartext
              sections (outline, test pads, net names).

    The returned BoardModel may carry a `warnings` attribute (list of
    strings) describing any non-fatal issues encountered while parsing.
    """
    raw = Path(path).read_bytes()
    # A manually-supplied key may arrive as a hex string (from the UI / CLI
    # / boardview.parse); normalise it to the 64-bit int _resolve_key wants.
    if isinstance(key, str):
        key = _parse_key_text(key)
    resolved_key = _resolve_key(key)
    parser = XZZPCBParser(raw, resolved_key)

    model = BoardModel()
    # Stash warnings + outline on the model so callers (UI, viewer) can
    # surface them. BoardModel doesn't have these fields natively, but
    # Python lets us attach them — they're documented on the parse()
    # docstring above.
    model.warnings = list(parser.warnings)         # type: ignore[attr-defined]
    model.outline_segments = list(parser.outline)  # type: ignore[attr-defined]
    # Structured signal for the UI: True when no valid key was in play, so
    # the encrypted part/pin records were skipped and a key can be asked for.
    model.key_required = parser.key is None        # type: ignore[attr-defined]
    model.key_format = "xzz"                       # type: ignore[attr-defined]

    # ---- Components from parts (one Component + Shape per part) -----------
    for part in parser.parts:
        if not part.pins:
            # Part with no pins — emit an empty shape so refdes is still
            # selectable. Position at origin (we don't know better).
            shape = Shape(name=f"_xzz_{part.name}")
            model.shapes[shape.name] = shape
            model.components[part.name] = Component(
                refdes=part.name, x=0.0, y=0.0, layer=part.layer,
                rotation=0.0, shape=shape.name, device="",
            )
            continue
        xs = [pin.x for pin in part.pins]
        ys = [pin.y for pin in part.pins]
        # Centroid of pin extents — same trick the BRD parser uses.
        cx = (min(xs) + max(xs)) / 2.0
        cy = (min(ys) + max(ys)) / 2.0
        shape = Shape(name=f"_xzz_{part.name}")
        # Pin offsets relative to component origin.
        for idx, pin in enumerate(part.pins, start=1):
            pin_label = pin.name if pin.name else str(idx)
            shape.pins.append((pin_label, pin.x - cx, pin.y - cy))
            net = parser.nets.get(pin.net_index, "")
            if net and net not in ("UNCONNECTED", "NC", ""):
                model.signals.setdefault(net, []).append((part.name, pin_label))
        model.shapes[shape.name] = shape
        model.components[part.name] = Component(
            refdes=part.name, x=cx, y=cy, layer=part.layer,
            rotation=0.0, shape=shape.name, device="",
        )

    # ---- Test pads as standalone single-pin "components" ------------------
    # XZZ test pad records typically all share the literal pin name "1",
    # so we synthesise sequential refdeses (TP01, TP02, ...) to keep
    # them distinguishable in the BoardModel — which is keyed by refdes.
    for i, tp in enumerate(parser.test_pads, start=1):
        refdes = f"TP{i:02d}"
        pin_label = tp.name or "1"
        shape = Shape(name=f"_xzz_tp_{i:02d}")
        shape.pins.append((pin_label, 0.0, 0.0))
        model.shapes[shape.name] = shape
        model.components[refdes] = Component(
            refdes=refdes, x=float(tp.x), y=float(tp.y), layer=tp.layer,
            rotation=0.0, shape=shape.name, device="",
        )
        net = parser.nets.get(tp.net_index, "")
        if net and net not in ("UNCONNECTED", "NC", ""):
            model.signals.setdefault(net, []).append((refdes, pin_label))

    return model


def _resolve_key(explicit: Optional[int]) -> Optional[int]:
    """Resolve the XZZ key from (in order) explicit arg, env var,
    private/ key file, legacy ~/.boardviewer config file. Returns None
    if no source had a value."""
    if explicit is not None:
        return explicit
    env = os.environ.get("XZZPCB_KEY")
    if env:
        parsed = _parse_key_text(env)
        if parsed is not None:
            return parsed
    # Same convention as private/fz_key.txt — check the CWD's private/
    # first (works when launched from the project root), then the
    # parser's own directory (its own private/), then the parent of the
    # parser's directory (handles the case where xzzpcb_parser.py
    # lives inside boardviewer/ but the project root's private/ holds
    # the key).
    here = Path(__file__).resolve().parent
    bases = [Path("private"), here / "private", here.parent / "private"]
    seen: set = set()
    for base in bases:
        for name in ("XZZ_Key.txt", "xzz_key.txt", "XZZ_KEY.txt"):
            cand = base / name
            try:
                resolved = cand.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            if not cand.exists():
                continue
            try:
                parsed = _parse_key_text(cand.read_text(encoding="utf-8"))
            except OSError:
                continue
            if parsed is not None:
                return parsed
    legacy = Path.home() / ".boardviewer" / "xzz_key"
    if legacy.exists():
        try:
            parsed = _parse_key_text(legacy.read_text(encoding="utf-8"))
        except OSError:
            return None
        if parsed is not None:
            return parsed
    return None


def _parse_key_text(text: str) -> Optional[int]:
    """Forgiving extractor: strip `#`-prefixed comments and surrounding
    whitespace, accept a `0x` prefix, take the first 16 hex digits.
    Returns None if no parseable value remains."""
    # Strip line comments per fz_parser's convention.
    cleaned_lines = []
    for line in text.splitlines():
        idx = line.find("#")
        if idx >= 0:
            line = line[:idx]
        line = line.strip()
        if line:
            cleaned_lines.append(line)
    if not cleaned_lines:
        return None
    token = cleaned_lines[0].split()[0]
    if token.lower().startswith("0x"):
        token = token[2:]
    try:
        return int(token, 16)
    except ValueError:
        return None


# ============================================================================
# Self-test (Rivest's DES test vector)
# ============================================================================

def _selftest_des() -> None:
    """Verify DES against the Rivest test vector. X16 should equal
    0x1B1A2DDB4C642438 after 16 alternating encrypt/decrypt iterations
    starting from X0 = 0x9474B8E8C73BCA7D and using each step's output
    as both input and key for the next step."""
    x = 0x9474B8E8C73BCA7D
    for i in range(16):
        x = des(x, x, "e" if i % 2 == 0 else "d")
    assert x == 0x1B1A2DDB4C642438, f"DES self-test failed: got {x:016x}"


if __name__ == "__main__":
    import sys
    _selftest_des()
    print("DES self-test passed")

    if len(sys.argv) >= 2:
        path = Path(sys.argv[1])
        key = None
        if len(sys.argv) >= 3:
            key = int(sys.argv[2], 16)
        model = parse(path, key=key)
        warnings = getattr(model, "warnings", [])
        outline = getattr(model, "outline_segments", [])
        print(f"file:       {path}")
        print(f"outline:    {len(outline)} segments")
        print(f"components: {len(model.components)}")
        print(f"signals:    {len(model.signals)} nets with at least one pin")
        if warnings:
            print(f"warnings:   {len(warnings)}")
            for w in warnings:
                print(f"  - {w}")
