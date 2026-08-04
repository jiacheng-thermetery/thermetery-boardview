# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""
Parse the five-file ASCII boardview set exported by "eM-Test Expert"
ICT software (Teradyne/GenRad lineage, seen on mid-2000s ASUS boards,
e.g. the N4L-VM DH exported 2006). A board is a *directory* — usually
named after the vendor part number, e.g. ``60-MJB000-C11`` — holding:

    format.asc   board outline contour: one ``X Y Radius`` row per vertex
    parts.asc    component list: ``refdes x y rot grid (T|B) 'device', 'outline'``
    pins.asc     per-part pin list: ``pin name x y layer net nail(s)``
    nails.asc    ICT fixture nails: ``$id x y type grid (T|B) #net name target``
    nets.asc     net -> part.pin adjacency (same data as pins.asc, inverted;
                 not read — pins.asc is authoritative and carries geometry)

Open any one of the five files (or the directory itself); the parser
locates the siblings by filename in the same directory. Only parts.asc
and pins.asc are required; format.asc adds the board outline and
nails.asc adds ICT test nails.

Conventions handled:

* **Units** — headers declare ``INCH units`` or ``MM units``; both are
  normalised to mils so the viewer's span heuristic (< 50,000 file
  units => mils) infers units_per_mm correctly.
* **Sides** — ``(T)``/``(B)`` markers on parts and nails map to
  TOP/BOTTOM. Pin coordinates are absolute and share one coordinate
  system for both sides (no mirroring).
* **Rotation** — parts.asc carries a rotation column, but pins.asc
  coordinates are already absolute, so rotation is baked into the pin
  offsets and ``Component.rotation`` stays 0 (same approach as the BRD
  and XZZPCB parsers).
* **No-connects** — nets named ``(NC)``, ``(R)`` or ``NC__<n>``
  (per-pin synthetic no-connect nets) are excluded from ``signals``.
* **Nails** — nails whose target is a VIA become standalone single-pin
  components (refdes = the nail's ``$<n>`` id), so ICT probe points are
  searchable and net-highlightable. Nails targeting a component PIN are
  skipped: that pad is already in the model via pins.asc.
* **Outline arcs** — format.asc has a Radius column; every sample seen
  so far uses 0.000. Non-zero radii are rendered as straight chords
  (a warning is attached to the model).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .gencad_parser import BoardModel, Component, Shape

# Case-insensitive sibling filenames that make up one board.
_MEMBERS = ("format", "parts", "pins", "nails", "nets")

_NO_CONNECT = {"", "(NC)", "(R)", "NC", "UNCONNECTED"}
_NC_PREFIX = "NC__"

_MILS_PER_INCH = 1000.0
_MILS_PER_MM = 1000.0 / 25.4

# parts.asc data row:
#   IC1   8.584   5.059  180.0  G5  (T)  '0.047UF/25V|MLCC/+/-20%', 'C0603'
_PART_RE = re.compile(
    r"^\s*(\S+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+\S+\s+"
    r"\(([TB])\)\s+'([^']*)'\s*,\s*'([^']*)'"
)

# pins.asc part header:  Part IC1    (T)
_PIN_PART_RE = re.compile(r"^\s*Part\s+(\S+)\s+\(([TB])\)")

# pins.asc data row:  1  A1  8.584  5.084  1  P66DETECT  1139
#   pin index, pin name, x, y, layer, [net], [nail ids...]
_PIN_RE = re.compile(
    r"^\s*\d+\s+(\S+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+\d+\s*(\S*)"
)

# nails.asc data row:
#   $18  0.282  0.967  1  A1  (B)  #60  APORT_B_R  V PIN AC21.1
_NAIL_RE = re.compile(
    r"^\s*(\$\d+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+\d+\s+\S+\s+\(([TB])\)\s+"
    r"#\d+\s+(\S+)\s+(.*)$"
)

# format.asc data row:  0.021   0.080   0.000   (X Y Radius)
_OUTLINE_RE = re.compile(
    r"^\s*(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s*$"
)


def parse(path: Path) -> BoardModel:
    """Parse an eM-Test Expert .asc set into a BoardModel.

    `path` may be the board directory or any of the member .asc files
    inside it. Raises ValueError when the directory doesn't contain a
    recognisable set (at minimum parts.asc + pins.asc).
    """
    members = _locate_members(Path(path))
    if "parts" not in members or "pins" not in members:
        raise ValueError(
            f"{Path(path).name}: not an eM-Test Expert boardview set "
            "(need parts.asc + pins.asc in the same directory)"
        )

    warnings: List[str] = []
    scale = _detect_scale(members, warnings)

    parts = _parse_parts(_read(members["parts"]), scale)
    pins_by_part = _parse_pins(_read(members["pins"]), scale)

    model = BoardModel()
    model.warnings = warnings                       # type: ignore[attr-defined]

    for refdes, (px, py, layer, device) in parts.items():
        my_pins = pins_by_part.get(refdes, [])
        if my_pins:
            xs = [p[1] for p in my_pins]
            ys = [p[2] for p in my_pins]
            # Centroid of pin extents — same trick the BRD/XZZ parsers use.
            cx = (min(xs) + max(xs)) / 2.0
            cy = (min(ys) + max(ys)) / 2.0
        else:
            cx, cy = px, py

        shape = Shape(name=f"_asc_{refdes}")
        for pin_name, x, y, net in my_pins:
            shape.pins.append((pin_name, x - cx, y - cy))
            if _is_connected(net):
                model.signals.setdefault(net, []).append((refdes, pin_name))
        model.shapes[shape.name] = shape
        model.components[refdes] = Component(
            refdes=refdes, x=cx, y=cy, layer=layer,
            rotation=0.0, shape=shape.name, device=device,
        )

    # Pins for parts missing from parts.asc (defensive: keep the net data).
    for refdes in pins_by_part.keys() - parts.keys():
        warnings.append(f"pins.asc part {refdes!r} missing from parts.asc")

    if "nails" in members:
        _add_via_nails(model, _read(members["nails"]), scale)

    if "format" in members:
        segs, has_arcs = _parse_outline(_read(members["format"]), scale)
        model.outline_segments = segs               # type: ignore[attr-defined]
        if has_arcs:
            warnings.append(
                "format.asc contains arc radii; rendered as straight chords")

    return model


# --------------------------------------------------------------------------
# File location & units
# --------------------------------------------------------------------------

def _locate_members(path: Path) -> Dict[str, Path]:
    """Map member name -> path for the .asc set containing `path`."""
    directory = path if path.is_dir() else path.parent
    members: Dict[str, Path] = {}
    try:
        entries = list(directory.iterdir())
    except OSError:
        return members
    for entry in entries:
        if entry.suffix.lower() != ".asc":
            continue
        stem = entry.stem.lower()
        for member in _MEMBERS:
            # Exact name ("parts.asc") or suffixed ("<board>_parts.asc").
            if stem == member or stem.endswith("_" + member):
                members.setdefault(member, entry)
    return members


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _detect_scale(members: Dict[str, Path], warnings: List[str]) -> float:
    """File units -> mils multiplier, from the 'INCH units'/'MM units'
    header every member file carries."""
    for name in ("parts", "pins", "format"):
        p = members.get(name)
        if p is None:
            continue
        head = _read(p)[:2000].upper()
        if "INCH UNITS" in head:
            return _MILS_PER_INCH
        if "MM UNITS" in head:
            return _MILS_PER_MM
    warnings.append("no 'INCH units'/'MM units' header found; assuming inches")
    return _MILS_PER_INCH


# --------------------------------------------------------------------------
# Member-file parsers
# --------------------------------------------------------------------------

def _parse_parts(text: str, scale: float) -> Dict[str, Tuple[float, float, str, str]]:
    """refdes -> (x, y, layer, device)."""
    parts: Dict[str, Tuple[float, float, str, str]] = {}
    for line in text.splitlines():
        m = _PART_RE.match(line)
        if not m:
            continue
        refdes, xs, ys, _rot, side, device, _footprint = m.groups()
        parts[refdes] = (
            float(xs) * scale,
            float(ys) * scale,
            "TOP" if side == "T" else "BOTTOM",
            device,
        )
    return parts


def _parse_pins(text: str, scale: float) -> Dict[str, List[Tuple[str, float, float, str]]]:
    """refdes -> [(pin_name, x, y, net), ...] in file order."""
    pins: Dict[str, List[Tuple[str, float, float, str]]] = {}
    current: Optional[List[Tuple[str, float, float, str]]] = None
    for line in text.splitlines():
        pm = _PIN_PART_RE.match(line)
        if pm:
            current = pins.setdefault(pm.group(1), [])
            continue
        if current is None:
            continue
        m = _PIN_RE.match(line)
        if not m:
            continue
        name, xs, ys, net = m.groups()
        current.append((name, float(xs) * scale, float(ys) * scale, net))
    return pins


def _is_connected(net: str) -> bool:
    return net not in _NO_CONNECT and not net.startswith(_NC_PREFIX)


def _add_via_nails(model: BoardModel, text: str, scale: float) -> None:
    """ICT nails that land on a VIA become standalone single-pin
    components (refdes = '$<n>'), so probe points are searchable. Nails
    on component PINs are already in the model via pins.asc."""
    for line in text.splitlines():
        m = _NAIL_RE.match(line)
        if not m:
            continue
        nail, xs, ys, side, net, target = m.groups()
        if "VIA" not in target.upper():
            continue
        shape = Shape(name=f"_asc_nail_{nail}")
        shape.pins.append(("1", 0.0, 0.0))
        model.shapes[shape.name] = shape
        model.components[nail] = Component(
            refdes=nail,
            x=float(xs) * scale, y=float(ys) * scale,
            layer="TOP" if side == "T" else "BOTTOM",
            rotation=0.0, shape=shape.name, device="ICT nail (via)",
        )
        if _is_connected(net):
            model.signals.setdefault(net, []).append((nail, "1"))


def _parse_outline(text: str, scale: float
                   ) -> Tuple[List[Tuple[Tuple[float, float], Tuple[float, float]]], bool]:
    """Contour rows -> line segments. Blank lines split contours (cutouts).
    Returns (segments, saw_nonzero_radius)."""
    segments: List[Tuple[Tuple[float, float], Tuple[float, float]]] = []
    prev: Optional[Tuple[float, float]] = None
    has_arcs = False
    for line in text.splitlines():
        if not line.strip():
            prev = None
            continue
        m = _OUTLINE_RE.match(line)
        if not m:
            prev = None
            continue
        x, y, radius = (float(g) for g in m.groups())
        if radius:
            has_arcs = True
        pt = (x * scale, y * scale)
        if prev is not None and pt != prev:
            segments.append((prev, pt))
        prev = pt
    return segments, has_arcs


# --------------------------------------------------------------------------
# CLI smoke test
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("Usage: python asc_parser.py <board-dir | any-member.asc>")
    model = parse(Path(sys.argv[1]))
    n_top = sum(1 for c in model.components.values() if c.layer == "TOP")
    n_bot = len(model.components) - n_top
    n_nails = sum(1 for r in model.components if r.startswith("$"))
    n_nodes = sum(len(v) for v in model.signals.values())
    print(f"Components: {len(model.components)} ({n_top} TOP, {n_bot} BOTTOM, "
          f"{n_nails} nails)")
    print(f"Signals:    {len(model.signals)}   Nodes: {n_nodes}")
    print(f"Outline:    {len(getattr(model, 'outline_segments', []))} segments")
    for w in getattr(model, "warnings", []):
        print(f"warning: {w}")
