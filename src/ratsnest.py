# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""Synthetic ratsnest topology for boardviews without real trace data.

When a boardview file carries only pin-net mapping but no actual routed-
trace geometry (GENCAD .cad, OpenBoardView .brd / .brd2 / .bv, ASUS .fz,
XZZPCB .pcb), `build_synthetic_topology(model)` returns a TraceGraph-
shaped object whose segments are an MST through each net's pin world
positions. Plugged into the viewer's existing trace-rendering pipeline,
this gives the user a "ratsnest" view — straight lines that illustrate
which pads share a net, drawn with the same layer-palette colors as
real traces, with cross-layer edges dashed.

This is illustrative connectivity, NOT actual routing. The viewer
appends "(ratsnest)" to the layer label in the status bar and Layer
dropdown so the synthetic origin is never mistaken for the routed
geometry of a TVW file.

Algorithm: Kruskal MST over Euclidean distances between pin world XY.
For typical net sizes (<50 pins) the O(n²) all-pairs distance build is
faster than computing a Delaunay triangulation first. Total cost on a
mainstream motherboard (~3000 nets, ~5 pins/net average) is ~30-80 ms;
amortised over a single lazy build at first T-press.

Edge classification:
    both endpoints TOP    -> solid, layer="TOP"
    both endpoints BOTTOM -> solid, layer="BOTTOM"
    cross-layer           -> dashed, emitted on BOTH layers so the user
                             sees the cross-layer hint regardless of which
                             side they're viewing.

Output is shaped to mimic `tvw_topology.TraceGraph`:
    .segments       - list[SyntheticSegment]  (Segment-shape dataclass +
                                                a `dashed` flag)
    .polylines      - []
    .pads           - []
    .net_names      - list[str], indexed by net_id (index 0 reserved
                                                     for "")
    ._seg_arrays    - dict of numpy arrays matching TVW's storage shape
                      with an added 'dashed' uint8 column, so the GL fast
                      path in viewer.py:`_segments_arrays` can read us
                      directly without dataclass materialisation.
    ._layer_names   - ["TOP", "BOTTOM"] (synthetic topology never has
                                          inner-layer geometry of its
                                          own; cross-layer edges are
                                          dashed, not separate copper).
    is_synthetic    - True (renderer key for the dashed paint and the
                            ratsnest status indicator).
    net_id_by_name(name) -> Optional[int]
    geometry_on_net(net_id) -> tuple[list[seg], list[poly]]

Anything else the renderer reads from a TVW TraceGraph (find_broken_nets,
net_at_point, propagation_changes, etc.) is intentionally absent — those
features depend on real routed geometry and are meaningless for an MST
visualisation. Callers should branch on `is_synthetic` if they need
trace-physics behaviour.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    import numpy as _np
    _HAVE_NUMPY = True
except ImportError:
    _np = None  # type: ignore
    _HAVE_NUMPY = False


# --------------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------------

@dataclass(slots=True)
class SyntheticSegment:
    """One MST edge. Shape matches `tvw_topology.Segment` so existing
    render code can iterate this transparently. The extra `dashed` flag
    is what the renderer keys off of for the cross-layer dash style."""
    seg_id: int
    x1: int
    y1: int
    x2: int
    y2: int
    net_id: int
    layer: str
    width: int = 0
    dashed: bool = False


# --------------------------------------------------------------------------
# Pin world-coord resolution
# --------------------------------------------------------------------------

def _pin_world_xy(component, shape, pin_name: str) -> Optional[Tuple[float, float]]:
    """Resolve `(refdes, pin_name)` to its world (x, y) by applying the
    component's rotation around its origin. Returns None if the pin name
    is not in the shape's pin list (rare — happens on partially-decoded
    XZZPCB pads where the parser couldn't recover the per-pin offset).
    """
    pin = next((p for p in shape.pins if p[0] == pin_name), None)
    if pin is None:
        return None
    _, dx, dy = pin
    rot = component.rotation or 0.0
    if rot == 0.0:
        return (component.x + dx, component.y + dy)
    rot_rad = math.radians(rot)
    cos_r = math.cos(rot_rad)
    sin_r = math.sin(rot_rad)
    wx = component.x + cos_r * dx - sin_r * dy
    wy = component.y + sin_r * dx + cos_r * dy
    return (wx, wy)


# --------------------------------------------------------------------------
# Kruskal MST
# --------------------------------------------------------------------------

def _mst_edges(points: List[Tuple[float, float, str]]) -> List[Tuple[int, int]]:
    """Compute MST edge indices over `points = [(x, y, layer), ...]`
    using Kruskal over squared Euclidean distances. Layer is passed
    through but not used as a metric — cross-layer edges are emitted,
    just classified later by the caller.

    Returns list of `(idx_a, idx_b)` indices into `points`. The MST has
    exactly `len(points) - 1` edges (or 0 if len(points) < 2).
    """
    n = len(points)
    if n < 2:
        return []

    # All-pairs squared Euclidean distances. For n < ~150 this beats
    # the per-edge sort cost of a triangulation.
    edges: List[Tuple[float, int, int]] = []
    for i in range(n):
        xi, yi, _ = points[i]
        for j in range(i + 1, n):
            xj, yj, _ = points[j]
            dx = xi - xj
            dy = yi - yj
            edges.append((dx * dx + dy * dy, i, j))
    edges.sort()

    # Union-Find with path compression.
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    out: List[Tuple[int, int]] = []
    target = n - 1
    for _d, i, j in edges:
        ri = find(i)
        rj = find(j)
        if ri != rj:
            parent[ri] = rj
            out.append((i, j))
            if len(out) == target:
                break
    return out


# --------------------------------------------------------------------------
# SyntheticTraceGraph
# --------------------------------------------------------------------------

class SyntheticTraceGraph:
    """TraceGraph-shaped object exposing the subset of attributes the
    viewer's trace-rendering code reads.

    Public attributes (read by viewer):
        is_synthetic  : bool, always True
        segments      : list[SyntheticSegment]
        polylines     : []
        pads          : []
        net_names     : list[str], indexed by net_id (index 0 = "")
        _layer_names  : ["TOP", "BOTTOM"]
        _zero_is_real_net : False (matches TVW's untagged-zero invariant)

    Methods (read by viewer):
        net_id_by_name(name) -> Optional[int]
        geometry_on_net(net_id) -> (list[seg], list[poly])

    Numpy-fast-path attribute (read by GL renderer's _segments_arrays):
        _seg_arrays : dict with keys
                        x1, y1, x2, y2 (int32)
                        net_id        (int32)
                        seg_id        (int32)
                        layer         (uint8 — index into _layer_names)
                        width         (int32)
                        dashed        (uint8 — 0=solid, 1=dashed)
    """

    is_synthetic: bool = True

    def __init__(
        self,
        segments: List[SyntheticSegment],
        net_names: List[str],
    ) -> None:
        self.segments = segments
        self.polylines: List = []
        self.pads: List = []
        self.net_names = net_names
        self._layer_names = ["TOP", "BOTTOM"]
        self._zero_is_real_net = False
        self.endpoint_tol = 0
        self.via_tol = 0
        self.same_net_pad_tol = 0
        self.pad_to_trace_tol = 0
        self.propagation_changes = 0
        self.propagation_conflicts = 0

        # Reverse lookup for net_id_by_name.
        self._net_id_by_name: Dict[str, int] = {
            n: i for i, n in enumerate(net_names) if n
        }

        # Per-net segment index for geometry_on_net (used in the
        # selected-net highlight phase). Build once at construction
        # time so highlight rendering stays O(net-size), not O(total).
        self._segs_by_net: Dict[int, List[SyntheticSegment]] = {}
        for s in segments:
            self._segs_by_net.setdefault(s.net_id, []).append(s)

        # Numpy fast path for the GL renderer. Only built when numpy
        # is importable; falls back to dataclass iteration otherwise.
        if _HAVE_NUMPY and segments:
            n = len(segments)
            x1 = _np.empty(n, dtype=_np.int32)
            y1 = _np.empty(n, dtype=_np.int32)
            x2 = _np.empty(n, dtype=_np.int32)
            y2 = _np.empty(n, dtype=_np.int32)
            net_id = _np.empty(n, dtype=_np.int32)
            seg_id = _np.empty(n, dtype=_np.int32)
            layer = _np.empty(n, dtype=_np.uint8)
            width = _np.zeros(n, dtype=_np.int32)
            dashed = _np.empty(n, dtype=_np.uint8)
            for i, s in enumerate(segments):
                x1[i] = s.x1
                y1[i] = s.y1
                x2[i] = s.x2
                y2[i] = s.y2
                net_id[i] = s.net_id
                seg_id[i] = s.seg_id
                # 0=TOP, 1=BOTTOM matches the TVW layer-byte convention.
                layer[i] = 0 if s.layer == "TOP" else 1
                dashed[i] = 1 if s.dashed else 0
            self._seg_arrays = {
                "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                "net_id": net_id, "seg_id": seg_id,
                "layer": layer, "width": width,
                "dashed": dashed,
            }
        else:
            self._seg_arrays = None

    # ---- TraceGraph-compatible API -----------------------------------

    def net_id_by_name(self, name: str) -> Optional[int]:
        return self._net_id_by_name.get(name)

    def geometry_on_net(
        self, net_id: int
    ) -> Tuple[List[SyntheticSegment], List]:
        return (self._segs_by_net.get(net_id, []), [])


# --------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------

def build_synthetic_topology(model) -> SyntheticTraceGraph:
    """Build a ratsnest TraceGraph from a BoardModel that has pin-net
    mapping (`model.signals`) but no actual routed-trace geometry.

    For each net in `model.signals` with at least 2 resolvable pins,
    emit (n - 1) MST edges. Same-layer edges are solid; cross-layer
    edges are emitted as TWO dashed copies (one per layer) so the user
    sees the cross-layer hint regardless of current view layer.

    `model.components`, `model.shapes`, and `model.signals` must already
    be populated. Pins that fail to resolve (missing component, missing
    shape, or pin-name not in the shape's pin list) are skipped — the
    net's MST is still built over whichever pins did resolve, which is
    what the user wants for partially-decoded boards.

    Net IDs run 1..N; index 0 in `net_names` is the empty string. This
    matches the TVW convention where 0 means "untagged" so that the
    selected-net highlight code's `sel_net_id is not None` check works
    the same way.
    """
    net_names: List[str] = [""]
    segments: List[SyntheticSegment] = []
    next_seg_id = 0

    for net_name, nodes in model.signals.items():
        if not net_name or len(nodes) < 2:
            continue

        # Resolve pins to (x, y, layer).
        points: List[Tuple[float, float, str]] = []
        for refdes, pin_name in nodes:
            comp = model.components.get(refdes)
            if comp is None:
                continue
            shape = model.shapes.get(comp.shape)
            if shape is None:
                continue
            xy = _pin_world_xy(comp, shape, pin_name)
            if xy is None:
                continue
            points.append((xy[0], xy[1], comp.layer))

        if len(points) < 2:
            continue

        net_id = len(net_names)
        net_names.append(net_name)

        for i, j in _mst_edges(points):
            xi, yi, li = points[i]
            xj, yj, lj = points[j]
            ix1 = int(round(xi))
            iy1 = int(round(yi))
            ix2 = int(round(xj))
            iy2 = int(round(yj))
            if li == lj:
                segments.append(SyntheticSegment(
                    seg_id=next_seg_id, x1=ix1, y1=iy1, x2=ix2, y2=iy2,
                    net_id=net_id, layer=li, dashed=False,
                ))
                next_seg_id += 1
            else:
                # Cross-layer: emit TWO copies, one per layer, dashed.
                # The renderer filters by `seg.layer == view_layer` so
                # each copy is visible only on its own side; together
                # they ensure the user sees the cross-layer hint
                # regardless of which side they're viewing.
                for layer in (li, lj):
                    segments.append(SyntheticSegment(
                        seg_id=next_seg_id, x1=ix1, y1=iy1, x2=ix2, y2=iy2,
                        net_id=net_id, layer=layer, dashed=True,
                    ))
                    next_seg_id += 1

    return SyntheticTraceGraph(segments, net_names)


__all__ = [
    "SyntheticSegment",
    "SyntheticTraceGraph",
    "build_synthetic_topology",
]
