# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""
Parse the ASCII boardview formats maintained and documented by
OpenBoardView — BRD2 (modern) and the legacy Allegro-style BRD —
into a BoardModel compatible with gencad_parser. Independent Python
implementation; format details cross-referenced against BRDFile.cpp
and BRD2File.cpp at github.com/OpenBoardView/OpenBoardView (see
THIRD_PARTY_NOTICES.md). No code ported.

BRD2 sections (most common today):
    BRDOUT: <num_format> <max.x> <max.y>
    NETS:   <num_nets>          → <id> <name>
    PARTS:  <num_parts>         → <name> <p1.x> <p1.y> <p2.x> <p2.y>
                                  <pin_anchor> <side>
    PINS:   <num_pins>          → <x> <y> <netid> <side>
    NAILS:  <num_nails>         → <probe> <x> <y> <netid> <is_top>

Legacy BRD sections:
    str_length: ...
    var_data:   <num_format> <num_parts> <num_pins> <num_nails>
    Format:     <num_format lines of "x y">
    Parts:      <num_parts lines of "name type_layer end_of_pins">
    Pins:       <num_pins lines of "x y probe part_id net_name">
    Nails:      <num_nails lines of "probe x y side net_name">

The PARTS `pin_anchor` field has TWO conventions in the wild:

  * **end_of_pins** (OpenBoardView reference exporter): exclusive upper
    bound — part i owns `pins[prev_eop : pin_anchor]`. The last part's
    `pin_anchor` equals the total pin count.

  * **start_of_pins** (Apple-style dumps, e.g. iMac A1311 820-2492-A):
    inclusive lower bound — part i owns `pins[pin_anchor :
    next_part.pin_anchor]`. The first part's `pin_anchor` is 0; the
    last part's is `n_pins - last_part_pin_count`.

We detect the convention per-file via `_detect_pin_anchor_convention`
(structural test on the first/last `pin_anchor` value, with a hit-rate
fallback for ambiguous files). Misclassifying causes pins to render
spread out across the board with components showing no pin dots.

Pin names are not stored in the file, so we use sequential "1", "2",
... per part.
"""

from pathlib import Path
from typing import Dict, List, Tuple

from gencad_parser import BoardModel, Component, Shape


def parse(path: Path) -> BoardModel:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    head = text[:8000]
    if "BRDOUT:" in head and ("NETS:" in head or "PARTS:" in head):
        return _parse_brd2(text)
    if "var_data:" in head and "Format:" in head:
        return _parse_brd_legacy(text)
    raise ValueError(
        f"{path.name}: not recognised as BRD2 or legacy BRD "
        "(missing 'BRDOUT:' / 'var_data:' markers)"
    )


# --------------------------------------------------------------------------
# BRD2
# --------------------------------------------------------------------------

_BRD2_HEADERS = {"BRDOUT", "NETS", "PARTS", "PINS", "NAILS"}


def _split_brd2_sections(text: str) -> Dict[str, List[str]]:
    """Slice the file by section header. Skips comments and blanks."""
    sections: Dict[str, List[str]] = {}
    current: List[str] | None = None
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        head = s.split(":", 1)[0]
        if head in _BRD2_HEADERS and ":" in s:
            current = []
            sections[head] = current
            continue
        if current is not None:
            current.append(s)
    return sections


def _parse_brd2(text: str) -> BoardModel:
    model = BoardModel()
    sections = _split_brd2_sections(text)

    # ---- NETS: id -> name ----
    nets_by_id: Dict[int, str] = {}
    for line in sections.get("NETS", []):
        toks = line.split(maxsplit=1)
        if len(toks) >= 2:
            try:
                nets_by_id[int(toks[0])] = toks[1].strip()
            except ValueError:
                pass

    # ---- PARTS: name p1.x p1.y p2.x p2.y pin_anchor side ----
    # pin_anchor is the file-format-dependent integer that links each part
    # to its run of pins; see module docstring for the two conventions.
    parts_info: List[
        Tuple[str, float, float, str, int, Tuple[float, float], Tuple[float, float]]
    ] = []
    for line in sections.get("PARTS", []):
        toks = line.split()
        if len(toks) < 7:
            continue
        try:
            refdes = toks[0]
            p1x, p1y = float(toks[1]), float(toks[2])
            p2x, p2y = float(toks[3]), float(toks[4])
            pin_anchor = int(toks[5])
            side = int(toks[6])
        except ValueError:
            continue
        cx = (p1x + p2x) / 2.0
        cy = (p1y + p2y) / 2.0
        layer = {1: "TOP", 2: "BOTTOM"}.get(side, "TOP")
        parts_info.append(
            (refdes, cx, cy, layer, pin_anchor, (p1x, p1y), (p2x, p2y))
        )

    # ---- PINS: x y netid [side] ----
    pins_all: List[Tuple[float, float, int]] = []
    for line in sections.get("PINS", []):
        toks = line.split()
        if len(toks) < 3:
            continue
        try:
            pins_all.append(
                (float(toks[0]), float(toks[1]), int(toks[2]))
            )
        except ValueError:
            pass

    # Decide whether `pin_anchor` is start_of_pins or end_of_pins for
    # this file. See module docstring for the two conventions.
    convention = _detect_pin_anchor_convention(parts_info, pins_all)

    # ---- Compose components & shapes ----
    n_pins = len(pins_all)
    prev_eop = 0
    for i, (refdes, cx, cy, layer, anchor, p1, p2) in enumerate(parts_info):
        if convention == "start":
            # Pin slice = [anchor_i, anchor_{i+1}); last part runs to EOF.
            s = min(anchor, n_pins)
            if i + 1 < len(parts_info):
                e = min(parts_info[i + 1][4], n_pins)
            else:
                e = n_pins
            my_pins = pins_all[s:e] if e >= s else []
        else:
            # Pin slice = [prev_eop, anchor); first part starts at 0.
            eop_clamped = min(anchor, n_pins)
            my_pins = pins_all[prev_eop:eop_clamped]
            prev_eop = eop_clamped

        shape_name = f"_brd_{refdes}"
        shape = Shape(name=shape_name)
        shape.bbox_override = (
            min(p1[0], p2[0]) - cx, min(p1[1], p2[1]) - cy,
            max(p1[0], p2[0]) - cx, max(p1[1], p2[1]) - cy,
        )
        for idx, (px, py, netid) in enumerate(my_pins, start=1):
            pin_name = str(idx)
            shape.pins.append((pin_name, px - cx, py - cy))
            net = nets_by_id.get(netid, "")
            if net and net != "UNCONNECTED":
                model.signals.setdefault(net, []).append((refdes, pin_name))
        model.shapes[shape_name] = shape

        comp = Component(
            refdes=refdes, x=cx, y=cy, layer=layer,
            rotation=0.0, shape=shape_name, device="",
        )
        model.components[refdes] = comp

    return model


def _detect_pin_anchor_convention(
    parts_info: List[Tuple], pins_all: List[Tuple[float, float, int]],
) -> str:
    """Decide whether PARTS field 6 means start_of_pins or end_of_pins.

    Detection strategy in order of preference:

      1. **Structural** — the cheapest, most authoritative signal:
         - Last part's `pin_anchor` == total pin count -> "end". The
           exclusive upper bound of the last part is exactly len(pins).
         - First part's `pin_anchor` == 0 AND last part's < n_pins ->
           "start". First part starts at index 0; last part starts
           somewhere before EOF.

      2. **Hit-rate fallback** — for ambiguous files (e.g. truncated or
         empty sections), score each interpretation by how many pins
         end up inside their assigned part's bounding box. The
         interpretation with materially more in-bbox hits wins. Files
         with a near-tie default to "end" (matches the OpenBoardView
         reference exporter).

    Returns "start" or "end".
    """
    if not parts_info:
        return "end"
    n_pins = len(pins_all)
    first_anchor = parts_info[0][4]
    last_anchor = parts_info[-1][4]
    # Structural rule: equality with n_pins is unambiguous.
    if last_anchor == n_pins:
        return "end"
    if first_anchor == 0 and 0 < last_anchor < n_pins:
        return "start"

    # Ambiguous — score both by how often the assigned pins land inside
    # the owning part's bbox (with a small margin for pins at the edge).
    def hit_count(convention: str) -> int:
        n_hit = 0
        prev = 0
        for i, (_refdes, cx, cy, _layer, anchor, p1, p2) in enumerate(parts_info):
            if convention == "start":
                s = min(anchor, n_pins)
                e = min(parts_info[i + 1][4], n_pins) if i + 1 < len(parts_info) else n_pins
                if e < s:
                    continue
                slc = pins_all[s:e]
            else:
                e = min(anchor, n_pins)
                slc = pins_all[prev:e]
                prev = e
            bx_min = min(p1[0], p2[0]) - 5.0
            bx_max = max(p1[0], p2[0]) + 5.0
            by_min = min(p1[1], p2[1]) - 5.0
            by_max = max(p1[1], p2[1]) + 5.0
            for px, py, _net in slc:
                if bx_min <= px <= bx_max and by_min <= py <= by_max:
                    n_hit += 1
        return n_hit

    hit_start = hit_count("start")
    hit_end = hit_count("end")
    return "start" if hit_start > hit_end else "end"


# --------------------------------------------------------------------------
# Legacy BRD (Allegro-style)
# --------------------------------------------------------------------------

_LEGACY_HEADERS = {"str_length", "var_data", "Format", "Parts", "Pins", "Nails"}


def _parse_brd_legacy(text: str) -> BoardModel:
    """
    Legacy BRD has no per-part bbox in the Parts block — we derive part
    position and bbox from the centroid/extent of its pins instead.
    """
    model = BoardModel()
    section: str | None = None
    parts_raw: List[Tuple[str, int, int]] = []  # name, type_layer, end_of_pins
    pins_raw: List[Tuple[float, float, int, int, str]] = []
    nails_raw: List[Tuple[int, str]] = []  # probe, net_name (for back-fill)

    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        head = s.split(":", 1)[0]
        if head in _LEGACY_HEADERS:
            section = head
            continue

        if section == "Parts":
            toks = s.split()
            if len(toks) >= 3:
                try:
                    parts_raw.append((toks[0], int(toks[1]), int(toks[2])))
                except ValueError:
                    pass
        elif section == "Pins":
            toks = s.split()
            if len(toks) >= 4:
                try:
                    x = float(toks[0]); y = float(toks[1])
                    probe = int(toks[2]); part_id = int(toks[3])
                    net = " ".join(toks[4:]) if len(toks) > 4 else ""
                    pins_raw.append((x, y, probe, part_id, net))
                except ValueError:
                    pass
        elif section == "Nails":
            toks = s.split()
            if len(toks) >= 5:
                try:
                    probe = int(toks[0])
                    net = " ".join(toks[4:])
                    nails_raw.append((probe, net))
                except ValueError:
                    pass

    # Back-fill missing pin nets from nails (BRDFile.cpp does this too)
    nail_net = {p: n for p, n in nails_raw if n}
    pins_raw = [
        (x, y, probe, pid, net or nail_net.get(probe, ""))
        for (x, y, probe, pid, net) in pins_raw
    ]

    prev_eop = 0
    for name, type_layer, eop in parts_raw:
        eop_clamped = min(eop, len(pins_raw))
        my_pins = pins_raw[prev_eop:eop_clamped]
        prev_eop = eop_clamped

        if my_pins:
            xs = [p[0] for p in my_pins]
            ys = [p[1] for p in my_pins]
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            xmin, xmax = min(xs), max(xs)
            ymin, ymax = min(ys), max(ys)
            # Pad bbox a touch so single-row parts aren't degenerate.
            pad = max((xmax - xmin), (ymax - ymin), 1.0) * 0.05
            xmin -= pad; xmax += pad; ymin -= pad; ymax += pad
        else:
            cx = cy = 0.0
            xmin = ymin = -1.0
            xmax = ymax = 1.0

        # type_layer bit 4 (0x10) = bottom side in OpenBoardView's BRDFile.cpp
        layer = "BOTTOM" if (type_layer & 0x10) else "TOP"

        shape_name = f"_brd_{name}"
        shape = Shape(name=shape_name)
        shape.bbox_override = (xmin - cx, ymin - cy, xmax - cx, ymax - cy)
        for k, (px, py, _probe, _pid, net) in enumerate(my_pins, start=1):
            pin_name = str(k)
            shape.pins.append((pin_name, px - cx, py - cy))
            if net and net != "UNCONNECTED":
                model.signals.setdefault(net, []).append((name, pin_name))
        model.shapes[shape_name] = shape

        comp = Component(
            refdes=name, x=cx, y=cy, layer=layer,
            rotation=0.0, shape=shape_name, device="",
        )
        model.components[name] = comp

    return model


# --------------------------------------------------------------------------
# CLI smoke test
# --------------------------------------------------------------------------

def _summary(model: BoardModel) -> str:
    n_top = sum(1 for c in model.components.values() if c.layer == "TOP")
    n_bot = sum(1 for c in model.components.values() if c.layer == "BOTTOM")
    n_nodes = sum(len(v) for v in model.signals.values())
    return (
        f"Components: {len(model.components)} ({n_top} TOP, {n_bot} BOTTOM)\n"
        f"Signals:    {len(model.signals)}\n"
        f"Nodes:      {n_nodes}\n"
        f"Shapes:     {len(model.shapes)}"
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("Usage: python brd_parser.py <file.brd>")
    model = parse(Path(sys.argv[1]))
    print(_summary(model))
    print()
    print("Sample components:")
    for refdes in list(model.components.keys())[:5]:
        c = model.components[refdes]
        sh = model.shapes.get(c.shape)
        bb = sh.bbox() if sh else (0, 0, 0, 0)
        print(f"  {c.refdes}: {c.layer} ({c.x:.1f}, {c.y:.1f}) "
              f"pins={len(sh.pins) if sh else 0} bbox={bb}")
