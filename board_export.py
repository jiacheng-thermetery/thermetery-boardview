# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""BoardModel -> JSON exporter for the Android port (contract v1).

Module-level functions called from Kotlin via Chaquopy (see
docs/android_contract.md SS1-2):

    open_board(path, key=None) -> str   # board JSON (no topology build)
    load_traces() -> str                # segments/vias JSON (builds topology)
    ping() -> str                       # native-kernel availability report

Every function returns a compact JSON *string* and never raises across
the bridge — all errors come back as the failure shape
``{"ok": false, "error": ..., "reason": ..., "format": ...}``.

IMPORTANT: this module must never import ``viewer`` (it pulls tkinter,
which does not exist on Android). Only parser modules are imported.

Coordinate / transform provenance — replicated from viewer.py:

* Pin world transform (absolute pin coords): board_canvas.py
  (``_find_pin_at``, the hit-testing path; identical math in the
  pin-draw and measurement paths)::

      theta = math.radians(comp.rotation)
      ct, st = math.cos(theta), math.sin(theta)
      wx = comp.x + dx * ct - dy * st
      wy = comp.y + dx * st + dy * ct

  Note there is deliberately NO per-component mirror for BOTTOM-layer
  components: in viewer.py the BOTTOM-view mirror is a *view* transform
  applied to the whole world at projection time
  (board_canvas.py _apply_view_transform: ``if (self._view_layer == "BOTTOM") ^ self._mirror_x``),
  never baked into world coordinates. World coords here match what the
  desktop hit-testing sees.

* Component outline polygon: board_canvas.py
  (``_component_polygon_world``) — shape.bbox() padded by 5 units per
  side, 4 corners rotated by the same rotation matrix; None when the
  shape is missing/degenerate (extent < 0.5 in both axes).

* Segment layer encoding: board_canvas.py (``_segments_arrays``) —
  ``topo._seg_arrays["layer"]`` is a uint8 index into
  ``topo._layer_names`` (out-of-range bytes fall back to TOP); when a
  graph has no layer table the historical 2-layer encoding applies
  (byte 0 = TOP, anything else = BOTTOM).

* Key-prompt detection: viewer.py
  (``_open_board_path`` / ``_load_with_key_prompt``) — FZKeyError from
  the ASUS .fz path carries ``.reason`` ("missing"/"invalid"); XZZ .pcb
  parses without raising but sets ``model.key_required`` when no valid
  key was in play (a supplied key that fails the parity check also
  lands here -> "invalid").

* units_per_mm heuristic: src/units.py (``units_per_mm_for_span``,
  shared with the desktop canvases) — component-extent span > 50,000
  file units => centi-mil (3937.0 u/mm), else mil (39.37 u/mm); null
  when there are no components to measure.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.parsers.boardview import BoardModel, FZKeyError, parse as parse_board
from src.units import units_per_mm_for_span

_COMPACT = (",", ":")

_EXT_FORMAT = {
    ".cad": "gencad",
    ".brd": "brd",
    ".brd2": "brd",
    ".bv": "brd",
    ".tvw": "tvw",
    ".fz": "fz",
    ".pcb": "xzzpcb",
    ".asc": "asc",
}

# ---------------------------------------------------------------------------
# Module-global current-board state (the Kotlin shell holds one board at a
# time; load_traces() operates on whatever open_board() last loaded).
# ---------------------------------------------------------------------------

_STATE: Dict[str, Any] = {
    "model": None,       # BoardModel
    "path": None,        # Path
    "format": "?",       # meta.format string
    "nets": None,        # List[str] — index order shipped in open_board
    "net_index": None,   # Dict[str, int] — name -> index into nets
}


def _fail(error: str, reason: str, fmt: str) -> str:
    return json.dumps(
        {"ok": False, "error": error, "reason": str(reason), "format": fmt},
        separators=_COMPACT,
    )


def _num(v) -> float:
    """Coerce a (possibly numpy) scalar to a plain Python float."""
    item = getattr(v, "item", None)
    if item is not None:
        v = item()
    return float(v)


def _layer_index(layer: str) -> int:
    """Component layer -> index. TOP=0 / BOTTOM=1 always (contract SS1).
    Component.layer is constrained to TOP/BOTTOM by the data model (see
    parsers; see gencad_parser.Component); anything unexpected falls back to TOP."""
    return 1 if str(layer).upper() == "BOTTOM" else 0


def _ext_format(path: Path) -> str:
    """Format tag from a path. Directory-shaped boards (eM-Test .asc
    sets, opened via the Android folder picker) have no suffix."""
    if path.is_dir():
        return "asc"
    return _EXT_FORMAT.get(path.suffix.lower(), "?")


def _detect_format(path: Path, model: Optional[BoardModel]) -> str:
    fmt = _ext_format(path)
    if fmt == "tvw" and model is not None:
        # The Compal/Lenovo decoder (tvw_compal.py:1049) names every
        # shape "_compal_<master>_<refdes>"; the Gigabyte decoder uses
        # "_tvw_..." (tvw_parser.py:813). Cheaper than re-reading the
        # file for tvw_parser._detect_variant().
        for name in model.shapes:
            if name.startswith("_compal_"):
                return "tvw-compal"
            break  # all shapes share one prefix family; first is enough
    return fmt


def _component_outline(
    comp,
    shape,
    *,
    local_bbox: Optional[Tuple[float, float, float, float]] = None,
    transform: Optional[Tuple[float, float, float, float]] = None,
) -> Optional[List[List[float]]]:
    """Absolute outline polygon — replicates board_canvas.py's
    _component_polygon_world: shape bbox + 5-unit pad, 4 corners
    rotated about the component origin. None => renderer uses bbox."""
    if shape is None or not shape.pins:
        return None
    x0, y0, x1, y1 = local_bbox if local_bbox is not None else shape.bbox()
    if (x1 - x0) < 0.5 and (y1 - y0) < 0.5:
        return None
    pad = 5
    x0 -= pad
    y0 -= pad
    x1 += pad
    y1 += pad
    corners = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    if transform is None:
        theta = math.radians(_num(comp.rotation))
        ct, st = math.cos(theta), math.sin(theta)
        cx, cy = _num(comp.x), _num(comp.y)
    else:
        cx, cy, ct, st = transform
    return [[cx + rx * ct - ry * st, cy + rx * st + ry * ct]
            for rx, ry in corners]


# ---------------------------------------------------------------------------
# open_board
# ---------------------------------------------------------------------------

def open_board(path: str, key: Optional[str] = None) -> str:
    """Parse a boardview file and return the board JSON (contract SS1).

    Does NOT build the trace topology — first paint stays fast; call
    load_traces() afterwards for segments/vias."""
    try:
        return _open_board(path, key)
    except Exception as exc:  # never raise across the bridge
        fmt = "?"
        try:
            fmt = _ext_format(Path(path))
        except Exception:
            pass
        return _fail("parse_error", f"{type(exc).__name__}: {exc}", fmt)


def _open_board(path: str, key: Optional[str]) -> str:
    p = Path(path)
    fmt = _ext_format(p)

    # ---- parse, mirroring viewer.py's _open_board_path ---------
    try:
        model = parse_board(p, key=key)
    except FZKeyError as exc:
        # ASUS (RC6) .fz with a missing or bad key (see viewer._open_board_path).
        return _fail("key_required", getattr(exc, "reason", "missing"), "fz")
    except Exception as exc:
        return _fail("parse_error", f"{type(exc).__name__}: {exc}", fmt)

    # XZZPCB parses its cleartext sections even without a key but flags
    # model.key_required (viewer._open_board_path offers the prompt; the
    # Android shell owns the retry loop, so we just report). A key that
    # was supplied but failed the parity check is "invalid"
    # (viewer._load_with_key_prompt), no key at all is "missing".
    if getattr(model, "key_required", False):
        return _fail("key_required",
                     "invalid" if key else "missing",
                     "xzzpcb")

    fmt = _detect_format(p, model)

    # ---- nets (index order is the wire-format contract) -------------------
    nets: List[str] = list(model.signals.keys())
    net_index: Dict[str, int] = {n: i for i, n in enumerate(nets)}

    # pin -> net index, replicating viewer._build_pin_to_net (
    # last assignment wins on duplicate (refdes, pin) keys).
    pin_net: Dict[Tuple[str, str], int] = {}
    for net_name, nodes in model.signals.items():
        ni = net_index[net_name]
        for refdes, pin in nodes:
            pin_net[(refdes, pin)] = ni

    # ---- components with absolute pin coords ------------------------------
    components: List[Dict[str, Any]] = []
    bb_minx = bb_miny = math.inf
    bb_maxx = bb_maxy = -math.inf
    comp_minx = comp_miny = math.inf
    comp_maxx = comp_maxy = -math.inf
    # A board commonly instantiates the same footprint hundreds of times.
    # Shape geometry is immutable after parsing, so cache its local bounds.
    shape_bbox_cache: Dict[str, Tuple[float, float, float, float]] = {}

    for refdes, comp in model.components.items():
        shape = model.shapes.get(comp.shape) if comp.shape else None
        cx, cy = _num(comp.x), _num(comp.y)
        if cx < comp_minx:
            comp_minx = cx
        if cx > comp_maxx:
            comp_maxx = cx
        if cy < comp_miny:
            comp_miny = cy
        if cy > comp_maxy:
            comp_maxy = cy
        # Pin world transform — board_canvas._find_pin_at (see module docstring).
        rotation = _num(comp.rotation)
        theta = math.radians(rotation)
        ct, st = math.cos(theta), math.sin(theta)

        pins: List[Dict[str, Any]] = []
        pin_minx = pin_miny = math.inf
        pin_maxx = pin_maxy = -math.inf
        if shape is not None:
            for pin_name, dx, dy in shape.pins:
                dx = _num(dx)
                dy = _num(dy)
                wx = cx + dx * ct - dy * st
                wy = cy + dx * st + dy * ct
                name = str(pin_name)
                pins.append({
                    "name": name,
                    "x": wx,
                    "y": wy,
                    "net": pin_net.get((refdes, name), -1),
                })
                if wx < pin_minx:
                    pin_minx = wx
                if wx > pin_maxx:
                    pin_maxx = wx
                if wy < pin_miny:
                    pin_miny = wy
                if wy > pin_maxy:
                    pin_maxy = wy

        local_bbox = None
        if shape is not None and shape.pins:
            local_bbox = shape_bbox_cache.get(comp.shape)
            if local_bbox is None:
                local_bbox = shape.bbox()
                shape_bbox_cache[comp.shape] = local_bbox
        outline = _component_outline(
            comp,
            shape,
            local_bbox=local_bbox,
            transform=(cx, cy, ct, st),
        )

        # Component bbox: prefer the outline polygon (what the desktop
        # hit-tests against, board_canvas._bbox_of_points over the
        # polygon); fall back to the absolute pin extent, then to the
        # origin point for shapeless components.
        if outline is not None:
            outline_minx = min(pt[0] for pt in outline)
            outline_miny = min(pt[1] for pt in outline)
            outline_maxx = max(pt[0] for pt in outline)
            outline_maxy = max(pt[1] for pt in outline)
            cbb = [outline_minx, outline_miny, outline_maxx, outline_maxy]
        elif pins:
            cbb = [pin_minx, pin_miny, pin_maxx, pin_maxy]
        else:
            cbb = [cx, cy, cx, cy]

        components.append({
            "ref": str(refdes),
            "x": cx,
            "y": cy,
            "layer": _layer_index(comp.layer),
            "rotation": rotation,
            "bbox": cbb,
            "outline": outline,
            "pins": pins,
        })

        # meta.bbox accumulates pins + outlines (contract SS1).
        if pins:
            if pin_minx < bb_minx: bb_minx = pin_minx
            if pin_maxx > bb_maxx: bb_maxx = pin_maxx
            if pin_miny < bb_miny: bb_miny = pin_miny
            if pin_maxy > bb_maxy: bb_maxy = pin_maxy
        if outline is not None:
            if outline_minx < bb_minx: bb_minx = outline_minx
            if outline_maxx > bb_maxx: bb_maxx = outline_maxx
            if outline_miny < bb_miny: bb_miny = outline_miny
            if outline_maxy > bb_maxy: bb_maxy = outline_maxy

    # Board outline segments (XZZ stashes them on the model,
    # xzzpcb_parser.py:846) also count toward the overall bounds.
    for seg in getattr(model, "outline_segments", None) or []:
        for px, py in seg:
            px, py = _num(px), _num(py)
            if px < bb_minx: bb_minx = px
            if px > bb_maxx: bb_maxx = px
            if py < bb_miny: bb_miny = py
            if py > bb_maxy: bb_maxy = py

    if bb_minx is math.inf:
        bbox = [0.0, 0.0, 0.0, 0.0]
    else:
        bbox = [bb_minx, bb_miny, bb_maxx, bb_maxy]

    if components:
        component_span = max(comp_maxx - comp_minx, comp_maxy - comp_miny)
        units_per_mm = units_per_mm_for_span(component_span)
    else:
        units_per_mm = None

    out = {
        "ok": True,
        "version": 1,
        "meta": {
            "title": p.stem,
            "format": fmt,
            "warnings": [str(w) for w in (getattr(model, "warnings", None) or [])],
            "units_per_mm": units_per_mm,
            "bbox": bbox,
            "traces_available": bool(model.topology_available),
        },
        "layers": ["TOP", "BOTTOM"],
        "nets": nets,
        "components": components,
    }

    # Serialize before publishing the model.  If a very large payload cannot
    # be encoded, the UI still shows the previous board and load_traces()
    # must therefore keep referring to that same previous board.
    # allow_nan=False: a non-finite coordinate must fail loudly here (the
    # open_board wrapper turns it into the ok:false shape) instead of
    # emitting bare NaN, which only today's lenient consumers tolerate.
    payload = json.dumps(out, separators=_COMPACT, allow_nan=False)
    _STATE.update({
        "model": model,
        "path": p,
        "format": fmt,
        "nets": nets,
        "net_index": net_index,
    })
    return payload


# ---------------------------------------------------------------------------
# load_traces
# ---------------------------------------------------------------------------

def load_traces() -> str:
    """Build (or fetch the cached) trace topology for the current board
    and return the traces JSON (contract SS1). TVW builds take seconds —
    the shell calls this off the UI thread."""
    try:
        return _load_traces()
    except Exception as exc:  # never raise across the bridge
        return _fail("parse_error", f"{type(exc).__name__}: {exc}",
                     _STATE.get("format", "?"))


def _load_traces() -> str:
    model: Optional[BoardModel] = _STATE.get("model")
    fmt = _STATE.get("format", "?")
    if model is None:
        return _fail("parse_error", "no board loaded (call open_board first)",
                     fmt)
    if not model.topology_available:
        return _fail("parse_error", "no trace topology available for this board",
                     fmt)

    topo = model.topology  # triggers the build / cache load
    net_index: Dict[str, int] = _STATE["net_index"] or {}

    # Map topology net_id -> open_board nets index, by NAME (the topology
    # keeps its own net table; open_board's `nets` order is the wire
    # contract). Unknown / unnamed nets -> -1.
    topo_net_names = list(getattr(topo, "net_names", []) or [])
    net_map: List[int] = [net_index.get(n, -1) for n in topo_net_names]
    n_net_map = len(net_map)

    # Output layer table: TOP=0 / BOTTOM=1 always; inner layers appended
    # from the topology's own table (contract SS1: layers list REPLACES
    # the open_board one and must be a superset).
    out_layers: List[str] = ["TOP", "BOTTOM"]

    seg_x1: List[float] = []
    seg_y1: List[float] = []
    seg_x2: List[float] = []
    seg_y2: List[float] = []
    seg_layer: List[int] = []
    seg_net: List[int] = []
    seg_width: List[float] = []

    seg_arr = getattr(topo, "_seg_arrays", None)
    layer_names = list(getattr(topo, "_layer_names", []) or [])

    if seg_arr is not None:
        # Numpy fast path — replicates board_canvas._segments_arrays
        # (_segments_arrays): `layer` is a uint8 index into
        # `topo._layer_names`; out-of-range bytes fall back to TOP; a
        # graph with no layer table uses the historical 2-layer
        # encoding (byte 0 = TOP, anything else = BOTTOM).
        seg_x1 = seg_arr["x1"].tolist()
        seg_y1 = seg_arr["y1"].tolist()
        seg_x2 = seg_arr["x2"].tolist()
        seg_y2 = seg_arr["y2"].tolist()
        layer_bytes = seg_arr["layer"].tolist()
        if layer_names:
            byte_to_out: List[int] = []
            for name in layer_names:
                if name == "TOP":
                    byte_to_out.append(0)
                elif name == "BOTTOM":
                    byte_to_out.append(1)
                else:
                    if name not in out_layers:
                        out_layers.append(name)
                    byte_to_out.append(out_layers.index(name))
            n_names = len(byte_to_out)
            seg_layer = [byte_to_out[b] if 0 <= b < n_names else 0
                         for b in layer_bytes]
        else:
            seg_layer = [0 if b == 0 else 1 for b in layer_bytes]
        seg_net = [net_map[t] if 0 <= t < n_net_map else -1
                   for t in seg_arr["net_id"].tolist()]
        width_arr = seg_arr.get("width")
        if width_arr is not None:
            seg_width = width_arr.tolist()
        else:
            seg_width = [0] * len(seg_x1)
    else:
        # Legacy dataclass path (board_canvas._segments_arrays fallback): seg.layer is the
        # layer NAME string here.
        name_to_out: Dict[str, int] = {"TOP": 0, "BOTTOM": 1}
        for seg in topo.segments:
            seg_x1.append(_num(seg.x1))
            seg_y1.append(_num(seg.y1))
            seg_x2.append(_num(seg.x2))
            seg_y2.append(_num(seg.y2))
            lname = str(seg.layer)
            li = name_to_out.get(lname)
            if li is None:
                out_layers.append(lname)
                li = len(out_layers) - 1
                name_to_out[lname] = li
            seg_layer.append(li)
            tid = int(seg.net_id)
            seg_net.append(net_map[tid] if 0 <= tid < n_net_map else -1)
            seg_width.append(_num(getattr(seg, "width", 0)))

    # Vias (tvw_topology.Via records; synthetic ratsnest has none).
    via_x: List[float] = []
    via_y: List[float] = []
    via_net: List[int] = []
    for v in getattr(topo, "vias", None) or []:
        via_x.append(_num(v.x))
        via_y.append(_num(v.y))
        tid = int(v.net_id)
        via_net.append(net_map[tid] if 0 <= tid < n_net_map else -1)

    out = {
        "ok": True,
        "synthetic": bool(getattr(topo, "is_synthetic", False)),
        "layers": out_layers,
        "segments": {
            "x1": seg_x1, "y1": seg_y1, "x2": seg_x2, "y2": seg_y2,
            "layer": seg_layer,
            "net": seg_net,
            "width": seg_width,
        },
        "vias": {"x": via_x, "y": via_y, "net": via_net},
    }
    return json.dumps(out, separators=_COMPACT, allow_nan=False)


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------

def ping() -> str:
    """Report whether each native kernel loaded (contract SS2). The shell
    logs this at startup to prove the jniLibs / bare-soname loader path
    works on-device."""
    tvw = xzz = rc6 = False
    try:
        from src.parsers import tvw_native
        tvw = bool(tvw_native.available())
    except Exception:
        pass
    try:
        from src.parsers import xzz_native
        xzz = bool(xzz_native.available())
    except Exception:
        pass
    try:
        from src.parsers.fz_parser import _load_native_rc6
        rc6 = _load_native_rc6() is not None
    except Exception:
        pass
    return json.dumps(
        {"ok": True, "native": {"tvw": tvw, "xzz": xzz, "rc6": rc6}},
        separators=_COMPACT,
    )


def validate_key(fmt: str, key_text: str) -> str:
    """Validate a pasted/loaded decryption key WITHOUT a board, for the key
    manager screen. Returns JSON ``{"ok", "status", "message"}``.

    * ASUS ``fz`` (RC6): fully verifiable offline — parses to 44 hex words
      and runs OpenBoardView's parity check (fz_parser._validate_fz_key).
      status: ``valid`` | ``invalid`` (parity fail) | ``malformed``.
    * XZZ ``xzzpcb`` (DES): only structurally checkable offline (a DES key
      has no self-validating parity); status ``unverified`` when it parses,
      ``malformed`` otherwise — the shell tells the user it's confirmed only
      by opening a board.
    """
    # Validation itself is shared with the desktop key manager so the two
    # platforms can never drift; see src/key_store.py.
    from src.key_store import validate_key_text, is_savable
    status, message = validate_key_text(fmt, key_text)
    return json.dumps(
        {"ok": is_savable(status), "status": status, "message": message},
        separators=_COMPACT)
