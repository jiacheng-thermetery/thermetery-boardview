"""TVW connectivity graph — board-level trace topology.

Builds a connected-component graph from the geometric primitives the
binary scanner cracked out of Gigabyte Teboview .tvw files. With this
graph a tool can ask "starting at U_PCH pin AC3, what other pads are
reachable through traces and vias?" and get a real answer — useful for
broken-trace detection or any net-walking workflow.

Inputs the module relies on:
  * `tvw_seg_27_unified_v3` — the 5-pass binary scanner from Phase 1
    (polyline blocks, tagged polylines, pad runs, segments, polyline
    chains). We don't redo any of that work; we just feed the records
    out of those scanners into a typed in-memory graph.
  * `tvw_parser._find_net_table` / `_build_net_index` — used in
    READ-ONLY fashion to decode the net-name table so net_id → net_name
    lookup works.

What we BUILD:
  1. Typed records:  Pad, Segment, Polyline (each with layer + net_id).
     One important coordinate fix-up: the on-disk byte order of segment
     and polyline ints is `Y, X` (NOT `X, Y` as Phase 1's docstring
     suggested). We swap on read so all records share the same (x, y)
     coordinate space as the pad records. Verified by exact-match test:
     ~50 % of GND segment endpoints land at distance 0 from a GND pad
     when the swap is applied.
  2. A spatial-hash endpoint dedup so segment endpoints, polyline
     endpoints and pad centres that fall within `endpoint_tol` (default
     50 file-units, ~0.016 mm) get fused into a single graph node.
  3. Union-Find (path-compression + union-by-rank) over those nodes,
     using each segment / polyline / via as an edge.
  4. Cross-layer bridging via vias. A via shows up as a pad whose
     (x, y) appears on BOTH layers (within `via_tol`, default 25 units).
     We match-join those pads so the TOP component fuses with the BOTTOM
     component of the same net.
  5. Same-net pad cluster fusion. The TVW format records multiple pad
     entries for one physical pin (cup outlines, multi-row connector
     pads). Same-net pads within `same_net_pad_tol` (~5 mm) are unioned.
  6. Same-net trace-to-pad fusion. Trace endpoints often land at the
     edge of a pad outline rather than the pad's logical centre — this
     fuses pad nodes with same-net trace endpoints within
     `pad_to_trace_tol` (~0.5 mm).
  7. Net propagation: untagged geometry (net_id=0, ~20-30 % of X570)
     inherits a net_id from any tagged endpoint in the same component.
     A density-based detector decides whether net_id=0 means "untagged"
     (Z490, B550) or is a real net id like GND (X570). Conflicts (>1
     distinct net_id in one component) are logged and resolved by
     majority vote.

Public API (see TraceGraph at bottom):
    TraceGraph.from_file(path)
    graph.net_at(x, y, layer, tol)
    graph.net_name(net_id)
    graph.geometry_on_net(net_id)
    graph.connected_pads(start_pad_id)
    graph.stats()

The module is intentionally plain: dataclasses + module-level helpers,
no fancy graph library, no abstract base classes, no networkx. Spatial
queries use a uniform grid keyed on integer cells of side `endpoint_tol`.
That keeps endpoint dedup linear in the number of endpoints (~80 K per
board) which is fine for our scale.
"""
from __future__ import annotations

import os
import pickle
import struct
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

# READ-ONLY imports of the Phase 1 / production code. We never mutate
# anything here; we only call into them.
from tvw_seg_27_unified_v3 import (
    find_polyline_blocks,
    find_tagged_polylines_in_gap,
    find_pad_runs_in_gap,
    find_segments_in_gap,
    find_polyline_chains_in_gap,
    merge_intervals,
    find_gaps,
)
from tvw_parser import _find_net_table, _build_net_index


# --------------------------------------------------------------------------
# Region maps. Phase 1 documented these but didn't expose them as data;
# the four anchor offsets per board are reverse-engineered + verified.
# --------------------------------------------------------------------------

# Each tuple: (label, file_path, top_start, top_end, bot_start, bot_end).
# top_*  -> Custom_35 / Custom_21 / Custom_26  (TOP trace data)
# bot_*  -> the 2nd Custom_17 occurrence (BOTTOM trace data)
KNOWN_BOARDS: List[Tuple[str, str, int, int, int, int]] = [
    ("Z490", "C:/Claude Code/Z490 VISION G r1.0.tvw",
     8_528, 4_761_170, 4_761_926, 6_625_913),
    ("X570", "C:/Claude Code/Gigabyte_X570_GAMING_X_REV1.01.tvw",
     4_754, 1_838_204, 1_839_968, 3_236_212),
    ("B550", "C:/Claude Code/B550_AORUS_PRO_AC_REV1.0.tvw",
     6_474, 3_978_556, 3_980_816, 5_493_511),
]


def _board_regions_for(path: str) -> Tuple[Tuple[int, int], Tuple[int, int]]:
    """Return ((top_start, top_end), (bot_start, bot_end)) for `path`.
    Falls back to None if path doesn't match a known board."""
    p_norm = path.replace("\\", "/")
    for _label, kp, ts, te, bs, be in KNOWN_BOARDS:
        if kp.replace("\\", "/").lower().endswith(
                p_norm.lower().split("/")[-1]):
            return ((ts, te), (bs, be))
    raise ValueError(
        f"Unknown board file: {path}. Add an entry to KNOWN_BOARDS in "
        f"tvw_topology.py with the TOP and BOTTOM Custom_NN region offsets "
        f"(use tvw_customs.py to find them).")


# --------------------------------------------------------------------------
# Typed records. Plain dataclasses, no __slots__ (we have ~70 K records
# total per board, so attribute lookup speed dominates over memory).
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Pad:
    """A pad record. layer is "TOP" or "BOTTOM"; net_id 0 means unassigned
    (rare for pads — almost all pads come in tagged with a net_id from the
    file). pad_id is a stable index assigned at extraction time so the
    public API can refer to specific pads."""
    pad_id: int
    x: int
    y: int
    net_id: int
    layer: str
    pad_type: int
    stride: int  # 38 or 54


@dataclass(slots=True)
class Segment:
    """A 24-byte trace segment. K is the per-segment width / layer-index
    attribute (Phase 1 confirmed it's small, 0..50, NOT a net id). For
    untagged segments net_id starts at 0 and may be filled in by
    `_propagate_nets`."""
    seg_id: int
    x1: int
    y1: int
    x2: int
    y2: int
    net_id: int
    layer: str
    width: int  # the K field — likely physical width / layer-bit


@dataclass(slots=True)
class Polyline:
    """A multi-vertex polyline. vertices is a list of (x, y) tuples;
    len(vertices) >= 2. Edges are between consecutive vertices."""
    poly_id: int
    vertices: List[Tuple[int, int]]
    net_id: int
    layer: str


# --------------------------------------------------------------------------
# Extraction. Re-uses Phase 1 scanners; we walk the run-bytes ourselves
# to materialise concrete records (the scanners only return offset/count).
# --------------------------------------------------------------------------

def _scan_pads_stride_aware(
    buf: bytes, region_start: int, region_end: int,
) -> List[Tuple[int, int, int, int]]:
    """Scan [region_start, region_end) for pad runs using BOTH 38-byte
    and 54-byte strides. Returns list of (run_start, run_end, count,
    stride) tuples. Pads are highly distinctive (00 00 sentinel + small
    net/type ints), so we run this BEFORE the polyline/segment scanners
    — the prior approach of scanning pads in leftover gaps misses ~50 %
    of pads because polyline scanners falsely claim their bytes first.
    """
    n = region_end
    runs: List[Tuple[int, int, int, int]] = []
    # Coord bound — any real trace coord on a motherboard is well under 2M
    # file units (typical ATX board span ~1M). With threshold lowered to 3,
    # we MUST validate coords too — without it, byte regions matching just
    # the sentinel/net_id/pad_type pattern by chance get claimed and emit
    # pads with garbage X/Y.
    COORD_MAX = 2_000_000
    for stride, sentinel_off in [(38, 20), (54, 36)]:
        net_off = sentinel_off + 2
        pad_type_off = net_off + 4
        y_off = pad_type_off + 4
        x_off = y_off + 4
        i = region_start
        while i + stride <= n:
            zero_at = buf.find(b'\x00\x00', i + sentinel_off, n)
            if zero_at < 0:
                break
            cand = zero_at - sentinel_off
            if cand < i:
                i = zero_at + 1
                continue
            cur = cand
            count = 0
            while cur + stride <= n:
                if buf[cur+sentinel_off:cur+sentinel_off+2] != b'\x00\x00':
                    break
                nid = struct.unpack_from('<I', buf, cur + net_off)[0]
                pt = struct.unpack_from('<I', buf, cur + pad_type_off)[0]
                if nid >= 4000 or pt >= 100_000:
                    break
                yv = struct.unpack_from('<i', buf, cur + y_off)[0]
                xv = struct.unpack_from('<i', buf, cur + x_off)[0]
                if abs(xv) > COORD_MAX or abs(yv) > COORD_MAX:
                    break
                count += 1
                cur += stride
            # Threshold lowered from 50 to 3 (2026-05-07 polyline crack).
            # Reason: ~85% of "garbage polylines" the polyline scanner emits
            # are actually short pad-record runs (3-30 records) that this
            # scanner missed at the old threshold. The pad signature is
            # very distinctive: 00 00 sentinel + net_id<4000 + pad_type<100k
            # + valid coords. Three consecutive validating records is a
            # strong enough signal — random byte regions don't satisfy this
            # at any meaningful rate. Catching them here prevents the
            # downstream polyline scanner from misidentifying them.
            if count >= 3:
                runs.append((cand, cur, count, stride))
                i = cur
            else:
                i = cand + 1
    return runs


def _extract_layer_records(
    buf: bytes,
    region_start: int,
    region_end: int,
    layer: str,
    next_pad_id: int,
    next_seg_id: int,
    next_poly_id: int,
) -> Tuple[List[Pad], List[Segment], List[Polyline], int, int, int]:
    """Run a 5-pass scan over [region_start, region_end), then decode
    each found block/run into Pad/Segment/Polyline records.

    Pass order matters. Phase 1's reference scanner runs polyline blocks
    first, but that loses many pads to false-positive polyline claims.
    Here we scan PADS FIRST (they're the most distinctive structurally:
    fixed 38- or 54-byte stride + zero sentinel + small ints), exclude
    those bytes, then run polyline blocks, tagged polylines, segments,
    and finally polyline chains.

    Returns (pads, segments, polylines, next_pad_id, next_seg_id,
    next_poly_id) so the caller can keep ID counters monotonic across
    layers.
    """
    pads: List[Pad] = []
    segments: List[Segment] = []
    polylines: List[Polyline] = []

    # Pass 1: pad runs (38- or 54-byte) FIRST. See note above on order.
    # NOTE on coordinate normalisation: TVW pads, segments AND polylines
    # all use the same on-disk byte order, but Phase 1 verified pads
    # store (Y, X) at the "X, Y" looking offsets — so we read them
    # swapped here. Independently we found segments and polylines also
    # use that same (Y, X) layout. We normalise EVERYTHING to (x, y) at
    # extraction time so all downstream code can compare coords without
    # caring which structure they came from.
    pad_runs = _scan_pads_stride_aware(buf, region_start, region_end)
    pad_intervals: List[Tuple[int, int]] = []
    for run_s, run_e, count, stride in pad_runs:
        if stride == 38:
            net_off, pad_type_off, y_off, x_off = 22, 26, 30, 34
        else:  # 54
            net_off, pad_type_off, y_off, x_off = 38, 42, 46, 50
        # Vectorised decode: read the run as a (count, stride) byte array,
        # slice each i32 field, view as int32 and tolist() for cheap
        # native-int extraction. Fields aren't 4-byte aligned within
        # stride=38 records so a direct .view() would fail; we copy each
        # 4-byte column before viewing. Total alloc per field is ~count*4
        # bytes (~180 KB for 45 k pads × 4 fields) — well below the
        # ~50 ms saved per layer.
        run_bytes = np.frombuffer(buf, dtype=np.uint8,
                                  count=count * stride, offset=run_s)
        rec = run_bytes.reshape(count, stride)
        net_ids = (rec[:, net_off:net_off+4]
                   .copy().view(np.uint32).reshape(-1).tolist())
        pad_types = (rec[:, pad_type_off:pad_type_off+4]
                     .copy().view(np.uint32).reshape(-1).tolist())
        ys = (rec[:, y_off:y_off+4]
              .copy().view(np.int32).reshape(-1).tolist())
        xs = (rec[:, x_off:x_off+4]
              .copy().view(np.int32).reshape(-1).tolist())
        for k in range(count):
            pads.append(Pad(
                pad_id=next_pad_id, x=xs[k], y=ys[k], net_id=net_ids[k],
                layer=layer, pad_type=pad_types[k], stride=stride,
            ))
            next_pad_id += 1
        pad_intervals.append((run_s, run_e))

    # Pass 2: polyline blocks ([count][type=1] framed) in the gaps left
    # after pad scanning. Note on coords: polyline vertex pairs at on-disk
    # offsets (+0, +4) are stored (Y, X) — same convention as pads. We
    # read them swapped so vertices come out as (x, y) in pad-space.
    blocks_intervals: List[Tuple[int, int]] = []
    gaps = find_gaps(region_start, region_end, merge_intervals(pad_intervals))
    for gs, ge in gaps:
        blocks = find_polyline_blocks(buf, gs, ge)
        for start_off, count, end_off in blocks:
            # Walk the polylines inside this block (separated by 4 zero bytes).
            cur = start_off + 8
            first = True
            for _ in range(count):
                if not first:
                    cur += 4
                K = struct.unpack_from('<I', buf, cur)[0]
                # Vectorised vertex decode: K pairs of (Y, X) i32 starting
                # at cur+4. Read once as int32, reshape to (K, 2), swap
                # columns to canonical (x, y), tolist for cheap tuple
                # building. Saves 2K unpack_from calls per polyline; with
                # ~6 k polys averaging 50 verts that's ~600 K calls → ~80 ms.
                verts_arr = np.frombuffer(
                    buf, dtype=np.int32,
                    count=K * 2, offset=cur + 4).reshape(K, 2)
                ys_l = verts_arr[:, 0].tolist()
                xs_l = verts_arr[:, 1].tolist()
                verts = list(zip(xs_l, ys_l))
                # Polyline blocks don't carry a per-polyline net_id —
                # propagation resolves via shared endpoints.
                polylines.append(Polyline(
                    poly_id=next_poly_id, vertices=verts,
                    net_id=0, layer=layer,
                ))
                next_poly_id += 1
                cur += 4 + K * 8
                first = False
            blocks_intervals.append((start_off, end_off))

    # Pass 3: tagged polylines in the gaps between pads + blocks.
    current = merge_intervals(pad_intervals + blocks_intervals)
    gaps = find_gaps(region_start, region_end, current)
    tagged_intervals: List[Tuple[int, int]] = []
    for gs, ge in gaps:
        for off, net_id, K in find_tagged_polylines_in_gap(buf, gs, ge):
            # Same Y,X swap as the block path; vertices start at off+8.
            verts_arr = np.frombuffer(
                buf, dtype=np.int32,
                count=K * 2, offset=off + 8).reshape(K, 2)
            ys_l = verts_arr[:, 0].tolist()
            xs_l = verts_arr[:, 1].tolist()
            polylines.append(Polyline(
                poly_id=next_poly_id, vertices=list(zip(xs_l, ys_l)),
                net_id=net_id, layer=layer,
            ))
            next_poly_id += 1
            tagged_intervals.append((off, off + 8 + K * 8 + 4))

    # Pass 4: trace segments (24-byte). The on-disk layout is documented
    # by Phase 1 as `i32 X1, Y1, X2, Y2`, but empirically the four ints
    # are stored as (Y1, X1, Y2, X2) — verified by exact-match test
    # against pad coords (491/1000 GND segs found a GND pad at distance
    # 0 with this swap). We swap on read so segments enter pad-space.
    current = merge_intervals(
        pad_intervals + blocks_intervals + tagged_intervals)
    gaps = find_gaps(region_start, region_end, current)
    seg_intervals: List[Tuple[int, int]] = []
    for gs, ge in gaps:
        for run_s, run_e, _cnt in find_segments_in_gap(
                buf, gs, ge, allow_zero_net=True):
            # Vectorised: 24-byte stride is 4-byte-aligned, so we can
            # frombuffer-as-int32 directly and reshape to (n, 6). Columns
            # are net_id (u32 reinterpreted as i32 — fine for 0..3999),
            # K, then (Y1, X1, Y2, X2) which we swap into (x1, y1, x2, y2)
            # at construction.
            n_segs = (run_e - run_s) // 24
            arr = np.frombuffer(buf, dtype=np.int32,
                                count=n_segs * 6, offset=run_s).reshape(n_segs, 6)
            net_ids = arr[:, 0].tolist()
            widths = arr[:, 1].tolist()
            y1s = arr[:, 2].tolist()
            x1s = arr[:, 3].tolist()
            y2s = arr[:, 4].tolist()
            x2s = arr[:, 5].tolist()
            for k in range(n_segs):
                segments.append(Segment(
                    seg_id=next_seg_id,
                    x1=x1s[k], y1=y1s[k], x2=x2s[k], y2=y2s[k],
                    net_id=net_ids[k], layer=layer, width=widths[k],
                ))
                next_seg_id += 1
            seg_intervals.append((run_s, run_e))

    # Pass 5: polyline chains (X570-style bare chains). Same Y,X swap.
    current = merge_intervals(
        pad_intervals + blocks_intervals
        + tagged_intervals + seg_intervals)
    gaps = find_gaps(region_start, region_end, current)
    for gs, ge in gaps:
        for chain_s, chain_e, _polys in find_polyline_chains_in_gap(buf, gs, ge):
            cur = chain_s
            while cur + 4 <= chain_e:
                K = struct.unpack_from('<I', buf, cur)[0]
                if K < 2 or K > 100_000 or cur + 4 + K * 8 > chain_e:
                    break
                verts_arr = np.frombuffer(
                    buf, dtype=np.int32,
                    count=K * 2, offset=cur + 4).reshape(K, 2)
                ys_l = verts_arr[:, 0].tolist()
                xs_l = verts_arr[:, 1].tolist()
                polylines.append(Polyline(
                    poly_id=next_poly_id, vertices=list(zip(xs_l, ys_l)),
                    net_id=0, layer=layer,
                ))
                next_poly_id += 1
                cur += 4 + K * 8
                if cur + 4 <= chain_e and buf[cur:cur+4] == b'\x00\x00\x00\x00':
                    cur += 4
                    if cur + 8 <= chain_e and buf[cur:cur+8] == b'\x00' * 8:
                        cur += 8
                else:
                    break

    # Defensive vertex filter (2026-05-07 polyline crack residue): a few
    # record families are misidentified by the polyline/segment/pad
    # scanners. Three cleanup passes:
    #
    # (a) ABSURD COORDS — vertices outside +/- 2,000,000 file units.
    #     Polyline scanner emits these from the Family-B format (per-chip
    #     footprint pin annotations with float rotation; bytes
    #     `00 00 87 43` = 270.0 read as i32 = 1.13e9). Documented in
    #     TVW_FORMAT.md §10.
    #
    # (b) NEAR-ORIGIN ENDPOINTS — coords within NEAR_ORIGIN of (0, 0).
    #     Three sub-families produce these:
    #       * Family A round apertures (shape_type=0, reserved=0) → (0, 0)
    #       * Family A oval/special (shape_type=1 or 3) → (0, 1) / (0, 3)
    #       * Family C dimension records (16-byte constant prefix
    #         `01 00 00 00 00 00 00 00 00 00 00 00 01 00 00 00` then
    #         (Y, X)) → endpoint (1, 0)
    #     All converge near origin via the spatial-hash dedup (50 unit
    #     endpoint_tol) onto whatever chip sits at world (0, 0) — a
    #     mounting hole on every Gigabyte board (MH1 on X570/B550, MH1#2
    #     on Z490). Real CAD never anchors trace endpoints near origin —
    #     it's just the coord-system reference.
    #
    # (c) NEAR-ORIGIN PADS — dropped likewise. Pads at (0, 0) with
    #     placeholder nets (`N48617361`, `NC_xxxx`, `PA_EXP_SW_*`) are
    #     scanner artifacts, not real geometry. A real screw-hole pad is
    #     covered by the master-fp pin transform; we don't need scanner
    #     hits at origin to represent it.
    COORD_MAX = 2_000_000
    AXIS_EPSILON = 10  # file units; covers exact 0/1 axis-aligned artifacts
    # Real PCB single-segment traces top out at ~200,000 file units
    # (~64 mm), measured across all 3 boards (Z490/X570/B550). We cap at
    # 500,000 — 2.5× the observed max, well above any legitimate trace,
    # but well below the obvious fakes (750,000+ from Family B regions
    # whose int fields happen to satisfy segment validation, producing
    # 240 mm "traces" that cross the whole board). The Phase-1 scanner
    # default of 1,000,000 (~320 mm) is too lenient.
    SEG_LEN_MAX_SQ = 500_000 * 500_000
    def _on_axis(x: int, y: int) -> bool:
        # Point is suspicious when EITHER coord is within AXIS_EPSILON of 0.
        # This catches:
        #   * (0, 0) — Family A round apertures, real screw-hole pads
        #   * (0, 1) / (1, 0) / (0, 3) — Family A oval/special apertures
        #   * (1, V) — Family C dimension records (constant prefix produces
        #             X1=1 in misaligned reads; V is an aperture dim like
        #             5900, 11800)
        #   * (V, 1) — symmetric Family D variant
        # Real PCB trace endpoints are never within ~3 µm of either axis.
        # The X axis at Y=0 and the Y axis at X=0 pass through the MH1
        # mounting hole, not through any signal traces.
        return abs(x) <= AXIS_EPSILON or abs(y) <= AXIS_EPSILON
    filtered_polylines = [
        p for p in polylines
        if all(abs(vx) <= COORD_MAX and abs(vy) <= COORD_MAX
               for vx, vy in p.vertices)
        and not any(_on_axis(vx, vy) for vx, vy in p.vertices)
    ]
    filtered_segments = [
        s for s in segments
        if not _on_axis(s.x1, s.y1) and not _on_axis(s.x2, s.y2)
        and (s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2 <= SEG_LEN_MAX_SQ
    ]
    filtered_pads = [p for p in pads if not _on_axis(p.x, p.y)]
    return (filtered_pads, filtered_segments, filtered_polylines,
            next_pad_id, next_seg_id, next_poly_id)


# --------------------------------------------------------------------------
# Spatial hash for endpoint dedup. A grid of integer cells, side =
# endpoint_tol. Each cell holds a list of node ids whose coord falls in
# the cell. Lookup is O(9) cells per query (3×3 neighbourhood) which is
# enough since a tol-radius ball is contained in at most 4 cells.
# --------------------------------------------------------------------------

class SpatialHash:
    """Per-layer 2D spatial hash keyed on integer cell coords."""

    def __init__(self, cell_size: int):
        self.cell = max(1, cell_size)
        # (layer, gx, gy) -> list[node_id]
        self.buckets: Dict[Tuple[str, int, int], List[int]] = defaultdict(list)

    def _key(self, layer: str, x: int, y: int) -> Tuple[str, int, int]:
        return (layer, x // self.cell, y // self.cell)

    def add(self, layer: str, x: int, y: int, node_id: int) -> None:
        self.buckets[self._key(layer, x, y)].append(node_id)

    def query_near(self, layer: str, x: int, y: int) -> Iterable[int]:
        gx = x // self.cell
        gy = y // self.cell
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                k = (layer, gx + dx, gy + dy)
                bucket = self.buckets.get(k)
                if bucket:
                    yield from bucket


# --------------------------------------------------------------------------
# Union-Find with path compression + union-by-rank. Plain arrays.
# --------------------------------------------------------------------------

class UnionFind:
    """Standard DSU. Indexed 0..n-1; unioning two roots merges their
    components. find() compresses paths."""

    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n
        self.size = [1] * n

    def grow(self, new_n: int) -> None:
        cur = len(self.parent)
        for i in range(cur, new_n):
            self.parent.append(i)
            self.rank.append(0)
            self.size.append(1)

    def find(self, x: int) -> int:
        # Iterative path compression.
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            nxt = self.parent[x]
            self.parent[x] = root
            x = nxt
        return root

    def union(self, x: int, y: int) -> int:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return rx
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        self.size[rx] += self.size[ry]
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1
        return rx


# --------------------------------------------------------------------------
# The graph. Construction does the heavy lifting; the public methods are
# thin lookups on top of it.
# --------------------------------------------------------------------------

@dataclass
class TraceGraph:
    """Connectivity graph for one TVW board file.

    Public attributes:
        pads, segments, polylines : the typed records (lists).
        net_names                  : index → name (e.g. net_names[42] = 'GND').
        node_count                 : number of unique endpoints in the graph.
        endpoint_tol, via_tol      : the tolerances used (preserved for
                                      diagnostics / re-runs).

    Internal:
        _uf            : UnionFind over the endpoint-fused nodes.
        _node_at       : (layer, x_q, y_q) → node_id where (x_q, y_q) is
                          the canonical (representative) coord of the
                          fused endpoint cluster.
        _node_layer    : node_id → layer.
        _node_xy       : node_id → (x, y) representative coord.
        _node_net      : node_id → propagated net_id (0 if still unknown).
        _seg_node      : seg_id → (node_a, node_b).
        _poly_nodes    : poly_id → list[node_id] for each vertex.
        _pad_node      : pad_id → node_id (the fused endpoint at the pad).
        _spatial       : SpatialHash for net_at queries.
    """
    pads: List[Pad] = field(default_factory=list)
    segments: List[Segment] = field(default_factory=list)
    polylines: List[Polyline] = field(default_factory=list)
    net_names: List[str] = field(default_factory=list)

    endpoint_tol: int = 50
    via_tol: int = 25
    # Pads on the same net within this distance get fused into one cluster.
    # The TVW format records multiple pad entries for one physical pin
    # (e.g. through-hole "cup" outlines, or multi-row connector pads),
    # often offset by ~1-3mm (~3000-9000 file units). This tolerance
    # bridges them. Set to 0 to disable.
    same_net_pad_tol: int = 15000
    # Same-net trace endpoint <-> pad fusion. A trace going to a pad
    # often ends a short distance INSIDE the pad outline rather than at
    # the pad's logical centre — this tol bridges that gap. Cross-layer
    # is enabled (a TOP-recorded pad fuses with a BOTTOM-routed trace
    # endpoint, which represents a layered drop-down through the pad
    # via). Set to 0 to disable.
    pad_to_trace_tol: int = 1500

    # Filled in by _build (kept private; expose via methods).
    _uf: Optional[UnionFind] = None
    _node_xy: List[Tuple[int, int]] = field(default_factory=list)
    _node_layer: List[str] = field(default_factory=list)
    _node_net: List[int] = field(default_factory=list)
    _seg_nodes: Dict[int, Tuple[int, int]] = field(default_factory=dict)
    _poly_nodes: Dict[int, List[int]] = field(default_factory=dict)
    _pad_node: Dict[int, int] = field(default_factory=dict)
    _spatial: Optional[SpatialHash] = None

    # Whether net_id=0 is a real net on this board. Some files (e.g.
    # X570) place "GND" at net_names[0]; others (Z490, B550) use index 0
    # for a synthesized N-prefixed placeholder, so 0 effectively means
    # "untagged". Auto-detected from net_names[0].
    _zero_is_real_net: bool = False

    # Diagnostics filled in during build.
    propagation_changes: int = 0
    propagation_conflicts: int = 0

    # ---- public construction ---------------------------------------------

    # Cache version: bump if the on-disk pickle layout changes (e.g. new
    # fields on Pad/Segment/Polyline or a different graph build algorithm).
    # Mismatched versions trigger a rebuild rather than risk a wrong graph.
    # v2: Pad/Segment/Polyline gained slots=True (different pickle layout).
    # v3: pad-scanner threshold lowered 50→3 + region cap at net-table start.
    #     Different pad/polyline counts; cached graphs are invalid.
    # v4: defensive segment filter — drop records with an endpoint at exactly
    #     (0, 0). False segment-records radiate from MH1 (mounting hole at
    #     world origin) when its pad is selected.
    # v5: widen "exactly origin" filter to "within NEAR_ORIGIN=100 file units
    #     of origin" + extend to pads. Catches Family-A oval apertures
    #     (endpoints (0,1)/(0,3)) and Family-C dimension records (endpoints
    #     (1,0)) which dedup'd to mounting-hole pin nodes via the 50-unit
    #     spatial-hash tolerance.
    # v6: widen to "axis epsilon" — drop records whose endpoint has EITHER
    #     coord within 10 file units of zero. Catches Family-D records
    #     (24-byte aperture variant with constant `01 00 00 00` byte at the
    #     X1 position) which produce endpoints like (1, 5900), (1, 7400),
    #     etc. — on the Y axis but not near origin, so the v5 near-origin
    #     filter missed them.
    # v7: segment length cap tightened from 1,000,000 (~320 mm — Phase 1
    #     scanner default) to 200,000 (~64 mm). Real PCB segments are at
    #     most ~50 mm; longer "segments" are Family B int fields satisfying
    #     segment validation by chance, producing 240 mm fake traces.
    _CACHE_VERSION = 7

    @classmethod
    def from_file(
        cls,
        path: str,
        endpoint_tol: int = 50,
        via_tol: int = 25,
        same_net_pad_tol: int = 15000,
        pad_to_trace_tol: int = 1500,
        use_cache: bool = True,
    ) -> "TraceGraph":
        """Parse a TVW file and return a fully built TraceGraph.

        endpoint_tol: max distance (in TVW file units) between two
        endpoints for them to fuse into one graph node. 50 ≈ 0.016 mm.

        via_tol: max distance for a TOP pad and a BOTTOM pad to be
        considered the same via. 25 ≈ 0.008 mm.

        same_net_pad_tol: pad-to-pad fusion distance for pads sharing a
        non-zero net_id. Bridges through-hole "cup" outlines and multi-
        row connector pads that share one electrical net but are
        recorded as separate pad entries. ~3 mm in file units.

        pad_to_trace_tol: same-net cross-layer fusion of a trace endpoint
        to a pad centre. Lets a BOTTOM-layer trace going to a TOP-layer
        pad still join the same component.

        use_cache: if True (default), look for `<path>.topocache.pkl`
        next to the source file. Cache key = source file size + mtime
        + the four tolerance parameters + cache version. Stale caches
        are ignored and a fresh build is written.
        """
        if use_cache:
            cached = cls._try_load_cache(path, endpoint_tol, via_tol,
                                         same_net_pad_tol, pad_to_trace_tol)
            if cached is not None:
                return cached

        buf = Path(path).read_bytes()
        (top_s, top_e), (bot_s, bot_e) = _board_regions_for(path)

        # Decode the net name table once; needed by net_name() and useful
        # for diagnostics on the way out.
        nt_start, nt_end = _find_net_table(buf)
        net_names = _build_net_index(buf, nt_start, nt_end) if nt_start >= 0 else []

        # Region cap (2026-05-07 polyline crack): the BOTTOM region in
        # KNOWN_BOARDS extends past the net-table into footprint/chip data.
        # Polyline scanners false-match on those bytes (especially huge K
        # values >10k). The actual trace polyline data ends BEFORE the
        # net-table start, so cap any region that runs into it.
        if nt_start > 0:
            if top_s < nt_start < top_e:
                top_e = nt_start
            if bot_s < nt_start < bot_e:
                bot_e = nt_start

        # Pull all geometry. Layer comes from which region we scanned.
        pads, segs, polys = [], [], []
        next_pad_id = next_seg_id = next_poly_id = 0
        for layer, rs, re_ in [("TOP", top_s, top_e), ("BOTTOM", bot_s, bot_e)]:
            (lp, ls, lpoly,
             next_pad_id, next_seg_id, next_poly_id) = _extract_layer_records(
                buf, rs, re_, layer, next_pad_id, next_seg_id, next_poly_id)
            pads.extend(lp)
            segs.extend(ls)
            polys.extend(lpoly)

        # Decide whether net_id=0 is a real net or an "untagged" sentinel.
        # Use record density: if >2 % of pads have net_id=0, it's a real
        # net (typically GND on X570). Otherwise treat 0 as untagged.
        # The name field is unreliable — Z490 has "N48617361" (clearly
        # synthesised), B550 has "VNB_FB+" (real-looking but unused),
        # X570 has "GND" (real and used). Density tells us the truth.
        n0_pads = sum(1 for p in pads if p.net_id == 0)
        zero_real = bool(pads) and (n0_pads / len(pads)) > 0.02

        graph = cls(
            pads=pads, segments=segs, polylines=polys, net_names=net_names,
            endpoint_tol=endpoint_tol, via_tol=via_tol,
            same_net_pad_tol=same_net_pad_tol,
            pad_to_trace_tol=pad_to_trace_tol,
            _zero_is_real_net=zero_real,
        )
        graph._build()
        if use_cache:
            cls._try_save_cache(graph, path, endpoint_tol, via_tol,
                                same_net_pad_tol, pad_to_trace_tol)
        return graph

    @classmethod
    def _cache_path_for(cls, source_path: str) -> Path:
        return Path(str(source_path) + ".topocache.pkl")

    @classmethod
    def _cache_key(cls, source_path: str,
                   endpoint_tol: int, via_tol: int,
                   same_net_pad_tol: int, pad_to_trace_tol: int) -> Dict:
        """Identity tuple for cache validation. Source size + mtime detect
        file changes; tolerance params detect parameter changes; version
        detects code/format changes."""
        st = os.stat(source_path)
        return {
            "version": cls._CACHE_VERSION,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "endpoint_tol": endpoint_tol,
            "via_tol": via_tol,
            "same_net_pad_tol": same_net_pad_tol,
            "pad_to_trace_tol": pad_to_trace_tol,
        }

    @classmethod
    def _try_load_cache(cls, path: str, endpoint_tol: int, via_tol: int,
                        same_net_pad_tol: int,
                        pad_to_trace_tol: int) -> Optional["TraceGraph"]:
        """Best-effort cache load. Any failure (missing, version skew,
        unpickle error, key mismatch) returns None — caller falls back
        to a fresh build."""
        cache_p = cls._cache_path_for(path)
        if not cache_p.exists():
            return None
        try:
            wanted = cls._cache_key(path, endpoint_tol, via_tol,
                                    same_net_pad_tol, pad_to_trace_tol)
            with open(cache_p, "rb") as f:
                blob = pickle.load(f)
            if not isinstance(blob, dict) or blob.get("key") != wanted:
                return None
            graph = blob.get("graph")
            if not isinstance(graph, cls):
                return None
            return graph
        except Exception:
            return None

    @classmethod
    def _try_save_cache(cls, graph: "TraceGraph", path: str,
                        endpoint_tol: int, via_tol: int,
                        same_net_pad_tol: int,
                        pad_to_trace_tol: int) -> None:
        """Best-effort cache save. Any failure (read-only dir, disk full)
        is silently ignored — the cache is an optimisation."""
        cache_p = cls._cache_path_for(path)
        try:
            key = cls._cache_key(path, endpoint_tol, via_tol,
                                 same_net_pad_tol, pad_to_trace_tol)
            tmp = cache_p.with_suffix(cache_p.suffix + ".tmp")
            with open(tmp, "wb") as f:
                pickle.dump({"key": key, "graph": graph}, f,
                            protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp, cache_p)
        except Exception:
            pass

    # ---- internal: graph construction ------------------------------------

    def _add_node(self, layer: str, x: int, y: int, net_id: int = 0) -> int:
        """Find-or-create a graph node for an endpoint at (layer, x, y).

        Looks in the spatial hash for an existing node within
        `endpoint_tol`. If found, returns its id (and merges the new net
        info if any). Otherwise creates a fresh node.
        """
        sh = self._spatial
        # Squared tolerance — comparing in squared form avoids sqrt.
        tol2 = self.endpoint_tol * self.endpoint_tol
        best_id = -1
        best_d2 = tol2 + 1
        for nid in sh.query_near(layer, x, y):
            nx, ny = self._node_xy[nid]
            dx = nx - x
            dy = ny - y
            d2 = dx * dx + dy * dy
            if d2 <= tol2 and d2 < best_d2:
                best_d2 = d2
                best_id = nid
        if best_id >= 0:
            # Merge net info. Don't overwrite an existing net with 0.
            if net_id and not self._node_net[best_id]:
                self._node_net[best_id] = net_id
            return best_id
        # New node.
        new_id = len(self._node_xy)
        self._node_xy.append((x, y))
        self._node_layer.append(layer)
        self._node_net.append(net_id)
        sh.add(layer, x, y, new_id)
        return new_id

    def _build(self) -> None:
        """Wire endpoints into nodes, segments/polylines into edges,
        propagate nets, and bridge layers via vias."""
        self._spatial = SpatialHash(self.endpoint_tol)
        self._uf = UnionFind(0)

        # ---- Step 1: register pad centres as nodes ----------------------
        # Pads are how we hook into the graph from the chip side. Every
        # pad becomes a node; same-layer pads with overlapping XY get
        # fused naturally by _add_node.
        for pad in self.pads:
            nid = self._add_node(pad.layer, pad.x, pad.y, pad.net_id)
            self._pad_node[pad.pad_id] = nid

        # ---- Step 2: register segment endpoints, record seg→(a,b) ------
        for seg in self.segments:
            a = self._add_node(seg.layer, seg.x1, seg.y1, seg.net_id)
            b = self._add_node(seg.layer, seg.x2, seg.y2, seg.net_id)
            self._seg_nodes[seg.seg_id] = (a, b)

        # ---- Step 3: polyline vertices -> nodes; consecutive = edges ---
        for poly in self.polylines:
            nodes = [
                self._add_node(poly.layer, vx, vy, poly.net_id)
                for vx, vy in poly.vertices
            ]
            self._poly_nodes[poly.poly_id] = nodes

        # Grow Union-Find to current node count, then union edges.
        self._uf.grow(len(self._node_xy))

        for seg_id, (a, b) in self._seg_nodes.items():
            self._uf.union(a, b)
        for poly_id, nodes in self._poly_nodes.items():
            for i in range(len(nodes) - 1):
                self._uf.union(nodes[i], nodes[i + 1])

        # ---- Step 4: cross-layer bridging via vias ---------------------
        # A via is a pad whose XY is matched by a pad on the OTHER layer
        # within via_tol. Bridge those nodes' components.
        # We bucket TOP pads by a (via_tol)-grid then probe BOTTOM pads
        # against it. O(N + M) instead of O(N*M).
        via_cell = max(1, self.via_tol)
        top_pads_by_cell: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        for pad in self.pads:
            if pad.layer == "TOP":
                gx, gy = pad.x // via_cell, pad.y // via_cell
                top_pads_by_cell[(gx, gy)].append(pad.pad_id)

        via_count = 0
        tol2 = self.via_tol * self.via_tol
        # Map pad_id -> Pad for quick lookup.
        pads_by_id = {p.pad_id: p for p in self.pads}
        for pad in self.pads:
            if pad.layer != "BOTTOM":
                continue
            gx, gy = pad.x // via_cell, pad.y // via_cell
            best_id = -1
            best_d2 = tol2 + 1
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    cell = top_pads_by_cell.get((gx + dx, gy + dy))
                    if not cell:
                        continue
                    for tp_id in cell:
                        tp = pads_by_id[tp_id]
                        ddx = tp.x - pad.x
                        ddy = tp.y - pad.y
                        d2 = ddx * ddx + ddy * ddy
                        if d2 <= tol2 and d2 < best_d2:
                            best_d2 = d2
                            best_id = tp_id
            if best_id >= 0:
                # Bridge this BOTTOM pad's node with the TOP pad's node.
                self._uf.union(
                    self._pad_node[pad.pad_id],
                    self._pad_node[best_id],
                )
                via_count += 1
        self._via_count = via_count

        # ---- Step 4b: same-net pad cluster fusion ------------------
        # The TVW format records multiple distinct pad entries for one
        # physical pin / cluster: through-hole "cup" outlines, multi-
        # row connector contacts, BGA cells with separate top-mask and
        # solderable-pad records, etc. They share an exact net_id and
        # sit within a couple of mm of each other. Fuse pad pairs with
        # matching net_id and proximity <= same_net_pad_tol.
        #
        # Net_id 0 is excluded UNLESS the board uses 0 as a real net id
        # (X570 puts GND at index 0). Without that check, on Z490/B550
        # we would fuse all "untagged" sentinel-zero pads into one
        # giant blob.
        snp_count = 0
        if self.same_net_pad_tol > 0:
            snp_cell = max(1, self.same_net_pad_tol)
            tol2 = self.same_net_pad_tol * self.same_net_pad_tol
            # Bucket pads by (net_id, gx, gy). Same-net only.
            buckets: Dict[Tuple[int, int, int], List[Pad]] = defaultdict(list)
            for pad in self.pads:
                if not pad.net_id and not self._zero_is_real_net:
                    continue
                gx, gy = pad.x // snp_cell, pad.y // snp_cell
                buckets[(pad.net_id, gx, gy)].append(pad)
            # For each bucket, union all pads in 3x3 neighbourhood within tol.
            for (net_id, gx, gy), pads_in in buckets.items():
                # Collect candidates from neighbour cells (same net).
                neighbour_pads: List[Pad] = []
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        nb = buckets.get((net_id, gx + dx, gy + dy))
                        if nb:
                            neighbour_pads.extend(nb)
                # Anchor on each pad in this bucket; union with any close
                # neighbour. Cheap O(B*N) but B,N small per cell.
                for pa in pads_in:
                    for pb in neighbour_pads:
                        if pa.pad_id >= pb.pad_id:
                            continue
                        d2 = (pa.x - pb.x) ** 2 + (pa.y - pb.y) ** 2
                        if d2 <= tol2:
                            ra = self._uf.find(self._pad_node[pa.pad_id])
                            rb = self._uf.find(self._pad_node[pb.pad_id])
                            if ra != rb:
                                self._uf.union(ra, rb)
                                snp_count += 1
        self._same_net_pad_fusions = snp_count

        # ---- Step 4c: same-net trace-to-pad fusion (any layer) --------
        # A trace endpoint often sits at the EDGE of a pad's outline,
        # not at the pad's logical centre. Distance up to the pad
        # radius (~300-1500 units, ~0.1-0.5 mm). endpoint_tol=50 is too
        # tight to catch that. So: for each pad with a non-zero net_id,
        # find the closest endpoint sharing the same net_id within
        # pad_to_trace_tol and union them. Cross-layer too — many pads
        # are recorded on one layer but routed in/out on the other.
        ptt_count = 0
        if self.pad_to_trace_tol > 0:
            tol2 = self.pad_to_trace_tol * self.pad_to_trace_tol
            # Bucket endpoints by (net_id, gx, gy). Layer is mixed in
            # the bucket — that's intentional; layer mismatches still
            # union (the pad sits between the two physical layers).
            ptt_cell = max(1, self.pad_to_trace_tol)
            ep_buckets: Dict[Tuple[int, int, int], List[int]] = defaultdict(list)
            for nid in range(len(self._node_xy)):
                net = self._node_net[nid]
                if not net and not self._zero_is_real_net:
                    continue
                nx, ny = self._node_xy[nid]
                ep_buckets[(net, nx // ptt_cell, ny // ptt_cell)].append(nid)
            # For each pad, look in same-net 3x3 cells; union with all
            # endpoints within tol. (Union them all, not just the best,
            # so a pad standing on top of multiple short trace stubs
            # joins all of them.)
            for pad in self.pads:
                if not pad.net_id and not self._zero_is_real_net:
                    continue
                gx, gy = pad.x // ptt_cell, pad.y // ptt_cell
                pad_node = self._pad_node[pad.pad_id]
                pad_root = self._uf.find(pad_node)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        bucket = ep_buckets.get(
                            (pad.net_id, gx + dx, gy + dy))
                        if not bucket:
                            continue
                        for cand in bucket:
                            cx, cy = self._node_xy[cand]
                            d2 = (cx - pad.x) ** 2 + (cy - pad.y) ** 2
                            if d2 <= tol2:
                                cr = self._uf.find(cand)
                                if cr != pad_root:
                                    self._uf.union(pad_root, cr)
                                    pad_root = self._uf.find(pad_root)
                                    ptt_count += 1
        self._pad_to_trace_fusions = ptt_count

        # ---- Step 5: net propagation ------------------------------------
        # For each connected component, take the majority net_id among
        # its member nodes (excluding the "untagged" sentinel) and
        # stamp every node with that. Conflicts (>1 distinct net_id in
        # same component) are logged.
        #
        # `untagged_value` is the magic id meaning "no net info". On
        # boards where 0 is a real net (X570 GND), nothing is treated
        # as untagged — every nonzero AND zero net_id is "data".
        untagged_value = -1 if self._zero_is_real_net else 0
        comp_to_nets: Dict[int, Counter] = defaultdict(Counter)
        for nid in range(len(self._node_xy)):
            net = self._node_net[nid]
            if net != untagged_value:
                root = self._uf.find(nid)
                comp_to_nets[root][net] += 1

        comp_winning_net: Dict[int, int] = {}
        conflicts = 0
        for root, ctr in comp_to_nets.items():
            if len(ctr) > 1:
                conflicts += 1
            (winner, _votes) = ctr.most_common(1)[0]
            comp_winning_net[root] = winner
        self.propagation_conflicts = conflicts

        # Apply: assign each node its component's winning net (if any).
        # Track how many previously-untagged nodes got a net.
        changes = 0
        for nid in range(len(self._node_xy)):
            root = self._uf.find(nid)
            win = comp_winning_net.get(root)
            if win is None:
                continue
            current = self._node_net[nid]
            if current == untagged_value:
                self._node_net[nid] = win
                changes += 1
            else:
                # Already had a net — make sure it agrees; if not we
                # already counted the conflict above.
                self._node_net[nid] = win
        self.propagation_changes = changes

        # Backfill the records' net_id from their nodes — useful so
        # geometry_on_net() can index by record.net_id directly.
        for seg in self.segments:
            if seg.net_id == untagged_value:
                a, _b = self._seg_nodes[seg.seg_id]
                seg.net_id = self._node_net[a]
        for poly in self.polylines:
            if poly.net_id == untagged_value:
                first = self._poly_nodes[poly.poly_id][0]
                poly.net_id = self._node_net[first]

    # ---- public queries --------------------------------------------------

    def net_name(self, net_id: int) -> str:
        """Resolve a net_id to its human-readable name. Returns
        f'<id={n}>' for ids outside the table (rare; usually data error)."""
        if 0 <= net_id < len(self.net_names):
            return self.net_names[net_id]
        return f"<id={net_id}>"

    def net_id_by_name(self, name: str) -> Optional[int]:
        """Reverse lookup. None if not found."""
        for i, n in enumerate(self.net_names):
            if n == name:
                return i
        return None

    def net_at(
        self, x: int, y: int, layer: str = "TOP", tol: int = 100,
    ) -> int:
        """Return the net_id at the given physical point on `layer`,
        or 0 if no node within `tol` of (x, y).

        Uses the spatial hash directly so this is O(cells_in_tol) ~ O(9)
        for tol <= endpoint_tol; falls back to a slightly wider search
        when tol is bigger.
        """
        # If tol > endpoint_tol the 3x3 neighbourhood may miss matches;
        # widen the search radius in cells.
        if tol <= self.endpoint_tol:
            best_id = -1
            best_d2 = tol * tol + 1
            for nid in self._spatial.query_near(layer, x, y):
                nx, ny = self._node_xy[nid]
                d2 = (nx - x) ** 2 + (ny - y) ** 2
                if d2 <= tol * tol and d2 < best_d2:
                    best_d2 = d2
                    best_id = nid
            return self._node_net[best_id] if best_id >= 0 else 0
        # Wider scan: iterate manually over more cells.
        cell = self._spatial.cell
        radius_cells = (tol // cell) + 1
        gx = x // cell
        gy = y // cell
        best_id = -1
        best_d2 = tol * tol + 1
        for dx in range(-radius_cells, radius_cells + 1):
            for dy in range(-radius_cells, radius_cells + 1):
                bucket = self._spatial.buckets.get((layer, gx + dx, gy + dy))
                if not bucket:
                    continue
                for nid in bucket:
                    nx, ny = self._node_xy[nid]
                    d2 = (nx - x) ** 2 + (ny - y) ** 2
                    if d2 <= tol * tol and d2 < best_d2:
                        best_d2 = d2
                        best_id = nid
        return self._node_net[best_id] if best_id >= 0 else 0

    def geometry_on_net(
        self, net_id: int,
    ) -> Tuple[List[Segment], List[Polyline]]:
        """All segments and polylines on the given net. For renderers."""
        s = [seg for seg in self.segments if seg.net_id == net_id]
        p = [poly for poly in self.polylines if poly.net_id == net_id]
        return s, p

    def pads_on_net(self, net_id: int) -> List[Pad]:
        """All pads tagged with this net (plus pads whose node was
        propagated to this net)."""
        out: List[Pad] = []
        for pad in self.pads:
            if pad.net_id == net_id:
                out.append(pad)
                continue
            # Propagation may have given the pad's node a net even if
            # the original record's net_id was 0.
            node = self._pad_node.get(pad.pad_id, -1)
            if node >= 0 and self._node_net[node] == net_id:
                out.append(pad)
        return out

    def connected_pads(self, start_pad_id: int) -> List[int]:
        """All pads in the same connected component as start_pad_id.
        Useful for "starting at this BGA pin, what else does this trace
        reach?" queries."""
        node = self._pad_node.get(start_pad_id, -1)
        if node < 0:
            return []
        root = self._uf.find(node)
        out: List[int] = []
        for pad in self.pads:
            pn = self._pad_node.get(pad.pad_id, -1)
            if pn >= 0 and self._uf.find(pn) == root:
                out.append(pad.pad_id)
        return out

    def component_of(self, node_id: int) -> int:
        """Return the union-find root for the given node id."""
        return self._uf.find(node_id)

    def stats(self) -> Dict[str, int | float]:
        """Diagnostics over the whole graph."""
        # Component sizes.
        comp_size: Dict[int, int] = defaultdict(int)
        for nid in range(len(self._node_xy)):
            comp_size[self._uf.find(nid)] += 1
        sizes = sorted(comp_size.values(), reverse=True)
        # Segments with a known net.
        tagged_segs = sum(1 for s in self.segments if s.net_id)
        tagged_polys = sum(1 for p in self.polylines if p.net_id)
        return {
            "pads": len(self.pads),
            "segments": len(self.segments),
            "polylines": len(self.polylines),
            "nodes": len(self._node_xy),
            "components": len(comp_size),
            "biggest_component": sizes[0] if sizes else 0,
            "top10_component_sizes": sizes[:10],
            "segments_with_net_pct": (
                100.0 * tagged_segs / len(self.segments) if self.segments else 0.0),
            "polylines_with_net_pct": (
                100.0 * tagged_polys / len(self.polylines) if self.polylines else 0.0),
            "propagation_changes": self.propagation_changes,
            "propagation_conflicts": self.propagation_conflicts,
            "vias_bridged": getattr(self, "_via_count", 0),
            "same_net_pad_fusions":
                getattr(self, "_same_net_pad_fusions", 0),
            "pad_to_trace_fusions":
                getattr(self, "_pad_to_trace_fusions", 0),
            "net_names_loaded": len(self.net_names),
        }

    def components_for_net(self, net_id: int) -> List[int]:
        """Return distinct UF roots that contain at least one node of
        this net. Ideal-world this is len 1 per net (one big component);
        if it's much larger, we have either a tolerance issue or a
        legitimately broken trace."""
        roots: set = set()
        for nid in range(len(self._node_xy)):
            if self._node_net[nid] == net_id:
                roots.add(self._uf.find(nid))
        return sorted(roots)


# --------------------------------------------------------------------------
# Standalone smoke test for the module. Heavy lifting is in tvw_topo_test.
# --------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else KNOWN_BOARDS[0][1]
    print(f"Building TraceGraph for {target} ...")
    g = TraceGraph.from_file(target)
    s = g.stats()
    for k, v in s.items():
        print(f"  {k:30s} {v}")
