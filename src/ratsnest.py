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

_PinTransform = Tuple[float, float, Optional[float], Optional[float]]


def _component_pin_transform(component) -> _PinTransform:
    """Return the invariant transform used for every pin on ``component``.

    ``cos``/``sin`` are ``None`` for an unrotated component so the hot path
    retains the original direct-add arithmetic exactly.  This matters both
    for speed and for preserving the old floating-point results bit-for-bit.
    """
    rot = component.rotation or 0.0
    if rot == 0.0:
        return (component.x, component.y, None, None)
    rot_rad = math.radians(rot)
    return (
        component.x,
        component.y,
        math.cos(rot_rad),
        math.sin(rot_rad),
    )


def _apply_pin_transform(
    transform: _PinTransform, dx: float, dy: float,
) -> Tuple[float, float]:
    """Apply a cached component transform to one footprint-local pin."""
    cx, cy, cos_r, sin_r = transform
    if cos_r is None or sin_r is None:
        return (cx + dx, cy + dy)
    wx = cx + cos_r * dx - sin_r * dy
    wy = cy + sin_r * dx + cos_r * dy
    return (wx, wy)


def _shape_pin_index(shape) -> Dict[str, Tuple[float, float]]:
    """Build ``pin name -> local (x, y)`` while keeping the first duplicate.

    The legacy linear lookup used ``next(...)``, so duplicate pin names
    resolved to their first occurrence.  An explicit membership check (rather
    than a dict comprehension) preserves that behaviour and insertion order.
    """
    index: Dict[str, Tuple[float, float]] = {}
    for name, dx, dy in shape.pins:
        if name not in index:
            index[name] = (dx, dy)
    return index


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
    return _apply_pin_transform(_component_pin_transform(component), dx, dy)


# --------------------------------------------------------------------------
# Kruskal MST
# --------------------------------------------------------------------------

# Below this pin count the pure-Python all-pairs build wins: numpy's
# fixed per-call overhead (array alloc, triu_indices, argsort setup) costs
# more than the handful of tuples a tiny net produces. The crossover is
# empirically ~64 pins; the giant power/ground nets that dominate cold
# load (GND can be 3000+ pins) are far above it and are exactly where
# the O(n²) Python loop + 6 M-tuple sort hurts. Keeping the small-net
# path on pure Python also keeps those builds allocation-free.
_NUMPY_MST_THRESHOLD = 64

# Candidate-pool sizing for the numpy MST (see `_mst_edges_numpy`).
# Kruskal stops as soon as the tree is spanned, so we sort only a pool of
# the cheapest edges rather than all m = n·(n-1)/2 of them. The initial
# pool is max(_MST_POOL_MIN, m // _MST_POOL_DIVISOR); on the power/ground
# nets we have, the longest MST edge ranks well under 1/8 of all edges, so
# a divisor of 8 spans the tree in a single argpartition+sort. The minimum
# guards small/medium nets where m // 8 would be a too-tight pool.
_MST_POOL_DIVISOR = 8
_MST_POOL_MIN = 4096


def _mst_edges_python(
    points: List[Tuple[float, float, str]], n: int
) -> List[Tuple[int, int]]:
    """Reference Kruskal MST — the original pure-Python implementation.

    Builds all-pairs squared distances, sorts `(dist², i, j)` ascending,
    then runs union-find. This is the fallback when numpy is unavailable
    and the exact behaviour the numpy path reproduces bit-for-bit.
    """
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


def _kruskal_select(
    ii_l: List[int], jj_l: List[int], n: int, target: int
) -> Tuple[List[Tuple[int, int]], bool]:
    """Run the reference union-find over pre-sorted edge index lists.

    `ii_l`/`jj_l` are the (i, j) endpoints in ascending `(dist², i, j)`
    order. Returns `(edges, complete)` where `complete` is True iff the
    MST was fully spanned (`target` edges found) before the pool ran out.
    This is the *identical* union-find used by `_mst_edges_python`, so as
    long as the pool covers the edges Kruskal would consume, the result
    is bit-for-bit the same.
    """
    parent = list(range(n))
    out: List[Tuple[int, int]] = []
    out_append = out.append
    found = 0

    # `find` is inlined below with the identical path-halving the
    # reference uses (`parent[a] = parent[parent[a]]`), and union is the
    # identical rank-less `parent[ri] = rj`. Inlining matters because this
    # loop scans hundreds of thousands of edges on big power nets and the
    # per-edge function-call overhead dominated otherwise. The mutations
    # are byte-for-byte the reference's, so the output is unchanged.
    for i, j in zip(ii_l, jj_l):
        # find(i) with path halving
        ri = i
        while parent[ri] != ri:
            parent[ri] = parent[parent[ri]]
            ri = parent[ri]
        # find(j) with path halving
        rj = j
        while parent[rj] != rj:
            parent[rj] = parent[parent[rj]]
            rj = parent[rj]
        if ri != rj:
            parent[ri] = rj
            out_append((i, j))
            found += 1
            if found == target:
                return out, True
    return out, found == target


def _mst_edges_numpy(
    points: List[Tuple[float, float, str]], n: int
) -> List[Tuple[int, int]]:
    """Vectorized all-pairs build + sort for the Kruskal MST.

    Bit-identical to `_mst_edges_python`: only the two O(n²) phases — the
    squared-distance build and the edge ordering — are moved into numpy.
    The union-find *selection* (`_kruskal_select`) is byte-for-byte the
    same as the reference, so tie-breaking, path-compression order, and
    the final edge list are all preserved exactly.

    Why this is identical, not merely "an MST":
      * Squared distances use float64 (== Python float), computed as
        (xi-xj)² + (yi-yj)² — the same IEEE-754 ops as the reference, so
        the dist² values match bit-for-bit.
      * `np.triu_indices(n, 1)` enumerates pairs in row-major order, i.e.
        already ascending in (i, then j). A *stable* argsort on dist²
        therefore breaks ties by (i, j) exactly as the reference's
        `list.sort()` on the `(dist², i, j)` tuple does — the resulting
        edge order is identical, which we assert in the test harness.
      * The same union-find then walks that identical order, so it adds
        exactly the same edges in the same sequence.

    Performance: Kruskal stops the instant the tree is spanned, so we
    avoid sorting all m = n·(n-1)/2 edges. We take a candidate POOL of the
    cheapest edges via `np.argpartition` (O(m), no sort), extend the cut
    to the whole tie group at its boundary (so no equal-dist² edge is
    split across the cut and the stable order within the pool matches the
    full-array order), stable-sort just that pool, and run union-find. If
    the pool is too small to span the tree we grow it; the final fallback
    sorts everything. Correctness never depends on the pool size — only
    speed does.
    """
    xs = _np.fromiter((p[0] for p in points), dtype=_np.float64, count=n)
    ys = _np.fromiter((p[1] for p in points), dtype=_np.float64, count=n)

    # Upper-triangle index pairs (i < j) in row-major order. We only build
    # the n*(n-1)/2 unique pairs, never the full n² matrix.
    ii, jj = _np.triu_indices(n, 1)

    dx = xs[ii] - xs[jj]
    dy = ys[ii] - ys[jj]
    dist2 = dx * dx + dy * dy  # float64, matches Python's dx*dx + dy*dy

    m = dist2.shape[0]
    target = n - 1

    def _select_from_pool(pool):
        """Stable-sort `pool` (edge indices, already ascending = row-major)
        by dist² and run the reference union-find over it."""
        # `pool` comes from flatnonzero / arange, so it is itself ascending
        # in edge index = ascending in (i, j). A stable sort by dist² thus
        # yields ascending (dist², i, j) — identical to the reference.
        order = _np.argsort(dist2[pool], kind="stable")
        sel = pool[order]
        return _kruskal_select(ii[sel].tolist(), jj[sel].tolist(), n, target)

    # Pool sizing. The longest MST edge tends to sit a few percent into the
    # full distance ranking on real power/ground nets, so an initial pool
    # of ~1/8 of the edges spans the tree in a single argpartition+sort for
    # the boards we have, while still being far cheaper than the full sort.
    # We grow geometrically and cap before the full-sort fallback so a
    # pathological net never loops more than a couple of times.
    K = max(_MST_POOL_MIN, m // _MST_POOL_DIVISOR)
    while K < m:
        # K cheapest edges by dist² (unordered). argpartition is O(m).
        part = _np.argpartition(dist2, K)[: K + 1]
        # Extend the cut to the COMPLETE tie group at the boundary: include
        # every edge with dist² <= the largest dist² in the partition. This
        # guarantees the pool is exactly {edges with dist² <= thresh}, so
        # its stable-by-dist² order matches the corresponding prefix of the
        # full-array stable order (no tie group straddles the cut).
        thresh = dist2[part].max()
        pool = _np.flatnonzero(dist2 <= thresh)
        out, complete = _select_from_pool(pool)
        if complete:
            return out
        K *= 4  # pool too small for this net — widen aggressively.

    # Fallback: sort the whole edge set. Still vectorized and identical
    # ordering; reached only for tiny m or a pathological non-spanning pool.
    out, _ = _select_from_pool(_np.arange(m))
    return out


def _mst_edges(points: List[Tuple[float, float, str]]) -> List[Tuple[int, int]]:
    """Compute MST edge indices over `points = [(x, y, layer), ...]`
    using Kruskal over squared Euclidean distances. Layer is passed
    through but not used as a metric — cross-layer edges are emitted,
    just classified later by the caller.

    Returns list of `(idx_a, idx_b)` indices into `points`. The MST has
    exactly `len(points) - 1` edges (or 0 if len(points) < 2).

    The all-pairs build is O(n²); on the power/ground nets (thousands of
    pins) that dominate GENCAD/CAD cold load this is the single biggest
    cost in the repo. When numpy is available and the net is large enough
    to amortise the per-call overhead, the O(n²) distance build and the
    edge sort run vectorized (`_mst_edges_numpy`); the union-find
    selection — and therefore the exact output — is unchanged. Tiny nets
    and the no-numpy case use the pure-Python reference
    (`_mst_edges_python`), which remains the default fallback.
    """
    n = len(points)
    if n < 2:
        return []

    if _HAVE_NUMPY and n >= _NUMPY_MST_THRESHOLD:
        try:
            return _mst_edges_numpy(points, n)
        except Exception:
            # Any numpy hiccup (unexpected dtype, allocation failure on a
            # pathologically huge net, etc.) falls back to the reference
            # so a build never fails just because the fast path tripped.
            return _mst_edges_python(points, n)

    return _mst_edges_python(points, n)


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

    # The same footprint is commonly shared by hundreds of passives, and a
    # large BGA can contribute hundreds of pins across many nets.  The former
    # `_pin_world_xy` call linearly scanned `shape.pins` for every signal node,
    # making a K-pin component O(K²).  Build each used shape's first-match pin
    # index once, and likewise compute each used component's rotation matrix
    # once.  Both caches are local to this immutable, one-shot topology build.
    pin_index_by_shape: Dict[str, Dict[str, Tuple[float, float]]] = {}
    transform_by_refdes: Dict[str, _PinTransform] = {}

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
            pin_index = pin_index_by_shape.get(comp.shape)
            if pin_index is None:
                pin_index = _shape_pin_index(shape)
                pin_index_by_shape[comp.shape] = pin_index
            pin_xy = pin_index.get(pin_name)
            if pin_xy is None:
                continue
            transform = transform_by_refdes.get(refdes)
            if transform is None:
                transform = _component_pin_transform(comp)
                transform_by_refdes[refdes] = transform
            xy = _apply_pin_transform(transform, pin_xy[0], pin_xy[1])
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
