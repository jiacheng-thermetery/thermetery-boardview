# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""
Unified boardview loader. Picks the right parser by file extension (with
content sniffing as a fallback) and returns a common BoardModel.

Supported today:
  .cad                 — GENCAD 1.4 (Mentor / Teradyne)            full
  .brd .brd2 .bv       — OpenBoardView ASCII (BRD2 modern, legacy) full
  .tvw                 — Teboview                                  full
                         (components, positions, per-chip pins, and
                         pin↔net mapping via the 38-byte pad records
                         buried in Custom_35/Custom_17 trace blocks)
  .fz                  — ASRock / ASUS Allegro Extracta            partial
                         (zlib-only for ASRock; RC6+zlib for ASUS,
                         needs an FZKey at private/fz_key.txt). No
                         trace routing data in the file format.
  .pcb                 — XZZPCB V1.0 (MSI / Chinese repair shops)  partial
                         (binary; needs an XZZ DES key at
                         private/XZZ_Key.txt or env XZZPCB_KEY.
                         Without a key the outline + test pads +
                         net list still parse.)

Importers should pull `BoardModel`, `Component`, `Shape` from here so we
have one consistent surface.
"""

from pathlib import Path
from typing import Union

from gencad_parser import BoardModel, Component, Shape
from gencad_parser import parse as _parse_gencad
from brd_parser import parse as _parse_brd
from tvw_parser import parse as _parse_tvw
from fz_parser import parse as _parse_fz, FZKeyError
from xzzpcb_parser import parse as _parse_xzzpcb
from xzzpcb_parser import verify_format as _verify_xzzpcb

PathLike = Union[str, Path]


GENCAD_EXTS = {".cad"}
BRD_EXTS = {".brd", ".brd2", ".bv"}
TVW_EXTS = {".tvw"}
FZ_EXTS = {".fz"}
XZZPCB_EXTS = {".pcb"}
ALL_EXTS = GENCAD_EXTS | BRD_EXTS | TVW_EXTS | FZ_EXTS | XZZPCB_EXTS


def parse(path: PathLike, key=None) -> BoardModel:
    """Parse a boardview file into a BoardModel.

    `key`, if given, is a manually-supplied decryption key forwarded to the
    encrypted-format parsers when the private/ key file is missing: the FZ
    (ASUS) parser wants 44 hex words, the XZZPCB parser wants 16 hex digits.
    Both accept it as a string. Ignored for unencrypted formats."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in GENCAD_EXTS:
        return _parse_gencad(p)
    if ext in BRD_EXTS:
        return _parse_brd(p)
    if ext in TVW_EXTS:
        return _parse_tvw(p)
    if ext in FZ_EXTS:
        return _parse_fz(p, key=key)
    if ext in XZZPCB_EXTS:
        return _parse_xzzpcb(p, key=key)
    return _sniff_and_parse(p, key=key)


def is_stub_format(path: PathLike) -> bool:
    """True if loading this file goes through a stub (i.e. returns an
    empty model).

    Currently no supported format is a stub — TVW used to be (we couldn't
    decode pin↔net) but the 38-byte pad-record format is now decoded."""
    return False


def _sniff_and_parse(path: Path, key=None) -> BoardModel:
    """Look at the first few KB to decide. Useful when the extension is
    unfamiliar but the contents are recognisable."""
    # Binary formats first — XZZPCB has a stable magic at offset 0
    # (sometimes XOR-obfuscated, handled by verify_format).
    try:
        head_bytes = path.read_bytes()[:0x40]
    except OSError:
        head_bytes = b""
    if head_bytes and _verify_xzzpcb(head_bytes):
        return _parse_xzzpcb(path, key=key)
    head = path.read_text(encoding="utf-8", errors="replace")[:8000]
    if "$COMPONENTS" in head and "$SIGNALS" in head:
        return _parse_gencad(path)
    if "BRDOUT:" in head or ("var_data:" in head and "Format:" in head):
        return _parse_brd(path)
    raise ValueError(
        f"{path.name}: unrecognised boardview format. Supported extensions: "
        + ", ".join(sorted(ALL_EXTS))
    )


__all__ = ["BoardModel", "Component", "Shape", "parse", "is_stub_format", "FZKeyError"]
