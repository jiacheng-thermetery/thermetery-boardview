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
  .asc                 — eM-Test Expert ICT set (mid-2000s ASUS)   full
                         (a directory of parts/pins/nails/nets/
                         format .asc files; open any one of them,
                         or the directory itself)

Every supported format is one `FormatSpec` row in `FORMATS` below —
extension dispatch and content sniffing both walk that table, so adding
a parser means adding exactly one entry (plus the UI file-dialog filter
lists in viewer.py/walker.py and, for Android, the manifest patterns).

Importers should pull `BoardModel`, `Component`, `Shape` from here so we
have one consistent surface.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple, Union

from .gencad_parser import BoardModel, Component, Shape
from .gencad_parser import parse as _parse_gencad
from .brd_parser import parse as _parse_brd
from .tvw_parser import parse as _parse_tvw
from .fz_parser import parse as _parse_fz, FZKeyError
from .xzzpcb_parser import parse as _parse_xzzpcb
from .xzzpcb_parser import verify_format as _verify_xzzpcb
from .asc_parser import parse as _parse_asc

PathLike = Union[str, Path]


@dataclass(frozen=True)
class FormatSpec:
    """One supported boardview format.

    `sniff(head_bytes, head_text)` inspects the first 8 KB of an
    unrecognised file (raw bytes and a utf-8-with-replacement decode of
    the same prefix) and returns True if this parser should take it.
    Binary formats should sniff on bytes, ASCII formats on text.
    `accepts_key` marks parsers whose `parse` takes a `key=` kwarg for
    encrypted content."""
    name: str
    exts: Tuple[str, ...]
    parse: Callable[..., BoardModel]
    accepts_key: bool = False
    sniff: Optional[Callable[[bytes, str], bool]] = None


# Sniff order matters: binary magic checks (XZZPCB) run before ASCII
# marker scans so obfuscated binaries never match a text heuristic.
# Parser callables are late-binding lambdas so tests (and debugging
# sessions) can monkeypatch the module-level _parse_* names and have the
# table pick the patch up.
FORMATS: Tuple[FormatSpec, ...] = (
    FormatSpec(
        "xzzpcb", (".pcb",), lambda p, **kw: _parse_xzzpcb(p, **kw),
        accepts_key=True,
        sniff=lambda head, _text: bool(head[:0x40]) and _verify_xzzpcb(head[:0x40]),
    ),
    FormatSpec(
        "gencad", (".cad",), lambda p: _parse_gencad(p),
        sniff=lambda _head, text: "$COMPONENTS" in text and "$SIGNALS" in text,
    ),
    FormatSpec(
        "brd", (".brd", ".brd2", ".bv"), lambda p: _parse_brd(p),
        sniff=lambda _head, text: ("BRDOUT:" in text
                                   or ("var_data:" in text and "Format:" in text)),
    ),
    FormatSpec("tvw", (".tvw",), lambda p: _parse_tvw(p)),
    FormatSpec("fz", (".fz",), lambda p, **kw: _parse_fz(p, **kw), accepts_key=True),
    FormatSpec("asc", (".asc",), lambda p: _parse_asc(p)),
)

_PARSER_BY_EXT = {ext: spec for spec in FORMATS for ext in spec.exts}

ALL_EXTS = frozenset(_PARSER_BY_EXT)


def parse(path: PathLike, key=None) -> BoardModel:
    """Parse a boardview file into a BoardModel.

    `key`, if given, is a manually-supplied decryption key forwarded to the
    encrypted-format parsers when the private/ key file is missing: the FZ
    (ASUS) parser wants 44 hex words, the XZZPCB parser wants 16 hex digits.
    Both accept it as a string. Ignored for unencrypted formats."""
    p = Path(path)
    if p.is_dir():
        # Only the eM-Test Expert .asc set is directory-shaped.
        return _parse_asc(p)
    spec = _PARSER_BY_EXT.get(p.suffix.lower())
    if spec is None:
        return _sniff_and_parse(p, key=key)
    return _dispatch(spec, p, key)


def is_stub_format(path: PathLike) -> bool:
    """True if loading this file goes through a stub (i.e. returns an
    empty model).

    Currently no supported format is a stub — TVW used to be (we couldn't
    decode pin↔net) but the 38-byte pad-record format is now decoded."""
    return False


def _dispatch(spec: FormatSpec, path: Path, key) -> BoardModel:
    if spec.accepts_key:
        return spec.parse(path, key=key)
    return spec.parse(path)


def _sniff_and_parse(path: Path, key=None) -> BoardModel:
    """Look at the first few KB to decide. Useful when the extension is
    unfamiliar but the contents are recognisable."""
    # Read one bounded binary prefix for both binary and text sniffing.
    # Let open/read errors propagate.
    with path.open("rb") as f:
        head = f.read(8000)
    text = head.decode("utf-8", errors="replace")

    for spec in FORMATS:
        if spec.sniff is not None and spec.sniff(head, text):
            return _dispatch(spec, path, key)
    raise ValueError(
        f"{path.name}: unrecognised boardview format. Supported extensions: "
        + ", ".join(sorted(ALL_EXTS))
    )


__all__ = ["BoardModel", "Component", "Shape", "FormatSpec", "FORMATS",
           "ALL_EXTS", "parse", "is_stub_format", "FZKeyError"]
