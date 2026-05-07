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

Importers should pull `BoardModel`, `Component`, `Shape` from here so we
have one consistent surface.
"""

from pathlib import Path
from typing import Union

from gencad_parser import BoardModel, Component, Shape
from gencad_parser import parse as _parse_gencad
from brd_parser import parse as _parse_brd
from tvw_parser import parse as _parse_tvw

PathLike = Union[str, Path]


GENCAD_EXTS = {".cad"}
BRD_EXTS = {".brd", ".brd2", ".bv"}
TVW_EXTS = {".tvw"}
ALL_EXTS = GENCAD_EXTS | BRD_EXTS | TVW_EXTS


def parse(path: PathLike) -> BoardModel:
    p = Path(path)
    ext = p.suffix.lower()
    if ext in GENCAD_EXTS:
        return _parse_gencad(p)
    if ext in BRD_EXTS:
        return _parse_brd(p)
    if ext in TVW_EXTS:
        return _parse_tvw(p)
    return _sniff_and_parse(p)


def is_stub_format(path: PathLike) -> bool:
    """True if loading this file goes through a stub (i.e. returns an
    empty model).

    Currently no supported format is a stub — TVW used to be (we couldn't
    decode pin↔net) but the 38-byte pad-record format is now decoded."""
    return False


def _sniff_and_parse(path: Path) -> BoardModel:
    """Look at the first few KB to decide. Useful when the extension is
    unfamiliar but the contents are recognisable."""
    head = path.read_text(encoding="utf-8", errors="replace")[:8000]
    if "$COMPONENTS" in head and "$SIGNALS" in head:
        return _parse_gencad(path)
    if "BRDOUT:" in head or ("var_data:" in head and "Format:" in head):
        return _parse_brd(path)
    raise ValueError(
        f"{path.name}: unrecognised boardview format. Supported extensions: "
        + ", ".join(sorted(ALL_EXTS))
    )


__all__ = ["BoardModel", "Component", "Shape", "parse", "is_stub_format"]
