# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""TVW connectivity graph (Phase 2 — board-level trace topology).

Builds a connected-component graph from the geometric primitives that
the parser already cracked out of Gigabyte Teboview .tvw files. With
this graph a caller can ask "starting at U_PCH pin AC3, what other pads
are reachable through traces and vias?" and get a real answer — used
by the viewer's net-highlight overlay and by `find_broken_nets`.

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
# Region detection.
#
# A "trace-data region" is one copper layer's worth of pad / segment /
# polyline records. Two-layer mobos have two regions (TOP, BOTTOM);
# 8-12 layer GPU PCBs have one per layer. Each region begins at a
# Custom_NN Pascal-prefixed header and runs until the next Custom_NN
# header — header-to-header gaps for genuine layer regions are several
# hundred KB to several MB, while the file's outer Custom_NN frame
# (component / footprint / metadata clusters before and after the
# layers) has gaps of only a few hundred bytes. That gap separation
# (three orders of magnitude) is what auto-detection keys on.
#
# Until 2026-05, we shipped a hand-curated KNOWN_BOARDS list keyed by
# filename to hard-coded (top_start, top_end, bot_start, bot_end)
# tuples for the three reference mobos. The list was a stopgap from
# before auto-detect existed: cross-checked end-to-end on Z490 / X570
# / B550, the auto-detected regions are bit-identical to those tuples
# after the standard `nt_start` trim — so the hand-curated entries
# were vestigial. Dropped the list entirely; `_board_regions_for`
# always auto-detects.
# --------------------------------------------------------------------------

# Auto-detect threshold. A trace-data region is a Custom_NN header followed
# by a gap >= this many bytes before the next Custom_NN. Header-only blocks
# have gaps in the 100-1000 B range; real layer regions on the smallest
# board we tested (X570) are ~1.4 MB. 50 KB sits ~30x above the largest
# header gap we've ever seen and ~30x below the smallest layer region —
# safe with three orders of magnitude of slack.
_LAYER_REGION_MIN_GAP = 50_000


def _scan_custom_headers(buf: bytes) -> List[Tuple[int, str]]:
    """Find every "Custom_NN" Pascal-prefixed string in `buf`.

    A Pascal-prefixed string is one byte of length L followed by L bytes
    of ASCII payload. We accept payloads 8..14 chars long that start
    with "Custom_" — that's the full Custom_NN header set Gigabyte ever
    emits. Returns [(offset_of_length_byte, payload_string), ...].

    Inlined here (rather than importing from a `tvw_explore` diagnostic
    module) so this file stays self-contained for the boardviewer
    distribution.
    """
    out: List[Tuple[int, str]] = []
    n = len(buf)
    i = 0
    MIN_L, MAX_L = 8, 14  # "Custom_NN" payloads are 9-14 chars long
    while i < n - 1:
        L = buf[i]
        if MIN_L <= L <= MAX_L and i + 1 + L <= n:
            s = buf[i + 1:i + 1 + L]
            # Cheap printable-ASCII gate before the substring check
            if all(0x20 <= b < 0x7F for b in s) and s.startswith(b"Custom_"):
                out.append((i, s.decode('latin-1')))
                i += 1 + L
                continue
        i += 1
    return out


def _autodetect_layer_regions(buf: bytes) -> List[Tuple[int, int]]:
    """Find each per-copper-layer trace region in a Gigabyte .tvw file.

    Algorithm: scan all Custom_NN Pascal-prefixed headers, pick those
    followed by a gap >= `_LAYER_REGION_MIN_GAP`. Each chosen header
    yields (start, end) where `start` is the header's offset and `end`
    is the next header's offset (or EOF for the final one).

    Returns regions in file order, which means region[0] is the
    OUTERMOST top copper and region[-1] is the OUTERMOST bottom copper.
    Anything in between is an inner copper layer (signal or plane).
    Caller is expected to apply the net-table upper-bound trim
    afterwards (regions starting at or past `nt_start` are metadata
    clusters that should be dropped; regions straddling it should
    be capped).
    """
    customs = _scan_custom_headers(buf)
    customs.sort(key=lambda x: x[0])
    n = len(buf)
    regions: List[Tuple[int, int]] = []
    for i, (off, _name) in enumerate(customs):
        next_off = customs[i + 1][0] if i + 1 < len(customs) else n
        if (next_off - off) >= _LAYER_REGION_MIN_GAP:
            regions.append((off, next_off))
    return regions


def _board_regions_for(
    path: str,
    buf: Optional[bytes] = None,
) -> List[Tuple[int, int]]:
    """Return the per-layer trace regions for `path`, in file order.

    Length 2 for two-layer mobos (TOP, BOTTOM); length N for N-layer
    boards (e.g. ~10 for GPU PCBs). Region detection is purely
    structural — it walks Custom_NN headers and picks ones followed by
    a >= `_LAYER_REGION_MIN_GAP` payload gap. No filename heuristics,
    no per-board overrides.
    """
    if buf is None:
        buf = Path(path).read_bytes()
    regions = _autodetect_layer_regions(buf)
    if not regions:
        raise ValueError(
            f"No trace-data regions detected in {path}. The file may not be "
            f"a Gigabyte Teboview .tvw, or its Custom_NN structure is "
            f"unfamiliar (no header is followed by a >= "
            f"{_LAYER_REGION_MIN_GAP:,}-byte gap). Run tvw_customs.py on "
            f"the file to inspect its macro structure.")
    return regions


def _layer_names_for_regions(n_regions: int) -> List[str]:
    """Pick the `layer` string each region's records will carry.

    Convention: byte 0 = TOP (first region), byte 1 = BOTTOM (last
    region), bytes 2..N-1 = INNER_1..INNER_{N-2} (intermediate regions
    in file order). This keeps byte 0/1 stable so the C-side via stitch
    in tvw_native.dll (which hardcodes LAYER_TOP=0 / LAYER_BOTTOM=1)
    keeps doing the right thing for the outer copper without a recompile.
    Inner-layer pads still get unique bytes 2..N-1 for spatial-hash
    keying; cross-stitching inner-to-inner vias is a follow-up that
    needs a DLL change.
    """
    if n_regions <= 0:
        return []
    if n_regions == 1:
        return ["TOP"]
    names = ["TOP", "BOTTOM"]
    for i in range(1, n_regions - 1):
        names.append(f"INNER_{i}")
    return names


def _layer_byte_for_region(region_index: int, n_regions: int) -> int:
    """Map file-order region index -> layer_byte. See `_layer_names_for_regions`
    docstring for the convention. Returns 0 for region 0 (TOP), 1 for
    region n-1 (BOTTOM), and 2..n-1 for intermediate regions in file order."""
    if region_index == 0:
        return 0
    if region_index == n_regions - 1:
        return 1
    # Intermediate region i (1..n-2) -> byte i+1 (2..n-1)
    return region_index + 1


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


@dataclass(slots=True)
class Via:
    """A through-board layer transition. Inferred from the existing
    via-bridging pass: a TOP pad whose XY matches a BOTTOM pad within
    `via_tol`. The Via record itself is not stored in the TVW file —
    we synthesize it from the pad geometry so the UI can render via
    markers and flip layers when the user clicks one.

    via_id is a stable index assigned during extraction (sequential).
    (x, y) is the midpoint of the two pads' centres so the marker sits
    exactly on the via, not biased to either pad. net_id is shared by
    construction; a via that bridged dissimilar nets would be a wiring
    error in the source file, not something we represent here."""
    via_id: int
    x: int
    y: int
    net_id: int
    top_pad_id: int
    bot_pad_id: int


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
    # Native fast path — ~60× speedup. See tvw_native.c.
    try:
        from tvw_native import scan_pads_stride_aware as _nat_scan_pads
        result = _nat_scan_pads(buf, region_start, region_end,
                                min_run=3, coord_max=2_000_000)
        if result is not None:
            return result
    except Exception:
        pass
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
    layer_byte: Optional[int] = None,
) -> Tuple[List[Pad], List[Segment], List[Polyline], int, int, int]:
    """Run a 5-pass scan over [region_start, region_end), then decode
    each found block/run into Pad/Segment/Polyline records.

    `layer_byte` is the small-integer index this region's records carry
    in the per-layer numpy arrays. If None we fall back to the legacy
    2-layer mapping (TOP=0, anything-else=1) so existing callers keep
    working.

    Pass order matters. Phase 1's reference scanner runs polyline blocks
    first, but that loses many pads to false-positive polyline claims.
    Here we scan PADS FIRST (they're the most distinctive structurally:
    fixed 38- or 54-byte stride + zero sentinel + small ints), exclude
    those bytes, then run polyline blocks, tagged polylines, segments,
    and finally polyline chains.

    Returns (pads, segments, polylines, next_pad_id, next_seg_id,
    next_poly_id) so the caller can keep ID counters monotonic across
    layers.

    Internally, scan output is held as numpy arrays per run / block /
    chain so that the post-pass filter can apply a single numpy mask
    against ~hundreds-of-K records rather than calling a Python helper
    per record. Dataclass instances (`Pad` / `Segment` / `Polyline`) are
    constructed AFTER filtering, with positional args (~2× faster than
    keyword args), via a single bulk list comprehension.
    """
    # Accumulate per-run numpy arrays; concat at the end. Each entry is
    # a (count,) int32 / int64 array slice extracted from the file.
    pad_xs_chunks: List[Any] = []
    pad_ys_chunks: List[Any] = []
    pad_nets_chunks: List[Any] = []
    pad_types_chunks: List[Any] = []
    pad_strides_chunks: List[Any] = []
    seg_x1_chunks: List[Any] = []
    seg_y1_chunks: List[Any] = []
    seg_x2_chunks: List[Any] = []
    seg_y2_chunks: List[Any] = []
    seg_nets_chunks: List[Any] = []
    seg_widths_chunks: List[Any] = []
    # Polylines remain a Python list of (verts, net_id) — they have
    # variable-length vertex arrays so they don't pack into a flat
    # structured array as cleanly. Filter and dataclass-build is done
    # with the same logic at the end, but per-poly.
    poly_records: List[Tuple[Any, Any, int]] = []  # (xs_arr, ys_arr, net_id)

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
        # slice each i32 field, view as int32. We KEEP the numpy arrays
        # (no .tolist()) so the downstream filter can mask them in one
        # numpy call instead of a per-record Python loop. Fields aren't
        # 4-byte aligned within stride=38 records so a direct .view()
        # would fail; we copy each 4-byte column before viewing.
        run_bytes = np.frombuffer(buf, dtype=np.uint8,
                                  count=count * stride, offset=run_s)
        rec = run_bytes.reshape(count, stride)
        pad_nets_chunks.append(
            rec[:, net_off:net_off+4].copy().view(np.uint32).reshape(-1))
        pad_types_chunks.append(
            rec[:, pad_type_off:pad_type_off+4].copy()
                .view(np.uint32).reshape(-1))
        pad_ys_chunks.append(
            rec[:, y_off:y_off+4].copy().view(np.int32).reshape(-1))
        pad_xs_chunks.append(
            rec[:, x_off:x_off+4].copy().view(np.int32).reshape(-1))
        pad_strides_chunks.append(np.full(count, stride, dtype=np.int32))
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
            cur = start_off + 8
            first = True
            for _ in range(count):
                if not first:
                    cur += 4
                K = struct.unpack_from('<I', buf, cur)[0]
                verts_arr = np.frombuffer(
                    buf, dtype=np.int32,
                    count=K * 2, offset=cur + 4).reshape(K, 2)
                # Keep raw int32 arrays for ys / xs; the dataclass
                # build at the end of the pass turns them into the
                # vertices list using `zip` which numpy handles cheaply.
                poly_records.append(
                    (verts_arr[:, 1].copy(),  # xs
                     verts_arr[:, 0].copy(),  # ys
                     0))
                cur += 4 + K * 8
                first = False
            blocks_intervals.append((start_off, end_off))

    # Pass 3: tagged polylines in the gaps between pads + blocks.
    current = merge_intervals(pad_intervals + blocks_intervals)
    gaps = find_gaps(region_start, region_end, current)
    tagged_intervals: List[Tuple[int, int]] = []
    for gs, ge in gaps:
        for off, net_id, K in find_tagged_polylines_in_gap(buf, gs, ge):
            verts_arr = np.frombuffer(
                buf, dtype=np.int32,
                count=K * 2, offset=off + 8).reshape(K, 2)
            poly_records.append(
                (verts_arr[:, 1].copy(),
                 verts_arr[:, 0].copy(),
                 net_id))
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
            # frombuffer-as-int32 directly and reshape to (n, 6). Keep
            # everything as numpy arrays — the post-pass filter masks
            # them all in one numpy call.
            n_segs = (run_e - run_s) // 24
            arr = np.frombuffer(buf, dtype=np.int32,
                                count=n_segs * 6, offset=run_s
                                ).reshape(n_segs, 6).copy()
            seg_nets_chunks.append(arr[:, 0].view(np.uint32))
            seg_widths_chunks.append(arr[:, 1])
            seg_y1_chunks.append(arr[:, 2])
            seg_x1_chunks.append(arr[:, 3])
            seg_y2_chunks.append(arr[:, 4])
            seg_x2_chunks.append(arr[:, 5])
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
                poly_records.append(
                    (verts_arr[:, 1].copy(),
                     verts_arr[:, 0].copy(),
                     0))
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

    # Resolve the layer byte once. Default keeps the legacy 2-layer
    # mapping (TOP=0, anything-else=1) for callers that haven't been
    # updated to pass it explicitly.
    if layer_byte is None:
        layer_byte = 0 if layer == "TOP" else 1

    # ---- Pad filter (numpy mask, no dataclass construction) -----------
    pad_arrays = None
    if pad_xs_chunks:
        all_x = np.concatenate(pad_xs_chunks).astype(np.int32, copy=False)
        all_y = np.concatenate(pad_ys_chunks).astype(np.int32, copy=False)
        all_n = np.concatenate(pad_nets_chunks).astype(np.int32, copy=False)
        all_t = np.concatenate(pad_types_chunks).astype(np.int32, copy=False)
        all_s = np.concatenate(pad_strides_chunks).astype(np.int32, copy=False)
        keep = (np.abs(all_x) > AXIS_EPSILON) & (np.abs(all_y) > AXIS_EPSILON)
        n_kept = int(np.count_nonzero(keep))
        # Per-pad ids — contiguous starting from next_pad_id. Advance
        # the caller's counter by the UNFILTERED count so cross-layer
        # ids stay collision-free even if filter drops different
        # numbers per layer (matches pre-refactor behaviour the Phase 3
        # spatial assertions depend on).
        pad_arrays = {
            "x":        all_x[keep],
            "y":        all_y[keep],
            "net_id":   all_n[keep],
            "pad_type": all_t[keep],
            "stride":   all_s[keep],
            "pad_id":   np.arange(next_pad_id, next_pad_id + n_kept,
                                   dtype=np.int32),
            "layer_byte": layer_byte,
        }
        next_pad_id += int(all_x.shape[0])

    # ---- Segment filter (numpy mask, no dataclass construction) ------
    seg_arrays = None
    if seg_x1_chunks:
        all_x1 = np.concatenate(seg_x1_chunks).astype(np.int32, copy=False)
        all_y1 = np.concatenate(seg_y1_chunks).astype(np.int32, copy=False)
        all_x2 = np.concatenate(seg_x2_chunks).astype(np.int32, copy=False)
        all_y2 = np.concatenate(seg_y2_chunks).astype(np.int32, copy=False)
        all_sn = np.concatenate(seg_nets_chunks).astype(np.int32, copy=False)
        all_sw = np.concatenate(seg_widths_chunks).astype(np.int32, copy=False)
        dx = all_x2.astype(np.int64) - all_x1.astype(np.int64)
        dy = all_y2.astype(np.int64) - all_y1.astype(np.int64)
        keep = (
            (np.abs(all_x1) > AXIS_EPSILON) & (np.abs(all_y1) > AXIS_EPSILON)
            & (np.abs(all_x2) > AXIS_EPSILON) & (np.abs(all_y2) > AXIS_EPSILON)
            & (dx * dx + dy * dy <= SEG_LEN_MAX_SQ)
        )
        n_kept = int(np.count_nonzero(keep))
        seg_arrays = {
            "x1":     all_x1[keep],
            "y1":     all_y1[keep],
            "x2":     all_x2[keep],
            "y2":     all_y2[keep],
            "net_id": all_sn[keep],
            "width":  all_sw[keep],
            "seg_id": np.arange(next_seg_id, next_seg_id + n_kept,
                                 dtype=np.int32),
            "layer_byte": layer_byte,
        }
        next_seg_id += int(all_x1.shape[0])

    # ---- Polyline filter (per-poly Python loop; varying vert counts) -
    # Output is a list of (xs_arr, ys_arr, net_id, poly_id, layer_byte)
    # tuples — variable-length verts make a single packed array
    # unwieldy.
    poly_arrays = None
    if poly_records:
        kept_polys: List[Tuple[Any, Any, int]] = []
        for xs_arr, ys_arr, net_id in poly_records:
            if (np.abs(xs_arr) > COORD_MAX).any() or \
                    (np.abs(ys_arr) > COORD_MAX).any():
                continue
            if (np.abs(xs_arr) <= AXIS_EPSILON).any() or \
                    (np.abs(ys_arr) <= AXIS_EPSILON).any():
                continue
            kept_polys.append((xs_arr, ys_arr, net_id))
        poly_arrays = {
            "kept": kept_polys,
            "poly_id_start": next_poly_id,
            "n_kept": len(kept_polys),
            "layer_byte": layer_byte,
        }
        next_poly_id += len(poly_records)

    # NOTE: we no longer build Pad / Segment / Polyline dataclass
    # instances here. The caller stashes the arrays on TraceGraph; the
    # dataclass lists are materialised lazily on first .pads/.segments/
    # .polylines access.
    return (next_pad_id, next_seg_id, next_poly_id,
            pad_arrays, seg_arrays, poly_arrays)


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

    Storage is **numpy-first**. The canonical record state lives in
    `_pad_arrays` / `_seg_arrays` / `_poly_records` (typed numpy arrays
    + Python list of variable-length polyline vertex pairs). The legacy
    `pads` / `segments` / `polylines` accessors are now lazy properties
    that materialise `Pad` / `Segment` / `Polyline` dataclass instances
    only when a consumer iterates them — typical hot paths
    (`find_broken_nets`, viewer trace rendering, `geometry_on_net`)
    have been converted to read directly from the arrays and never
    trigger materialisation.

    Public attributes:
        pads, segments, polylines : iterables of typed records,
                                     materialised lazily from arrays.
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
        _pad_arrays    : dict of int32/uint8 numpy arrays for pads
                          (x, y, net_id, pad_id, layer, pad_type, stride)
                          OR None on cache-loaded graphs that never had
                          arrays attached.
        _seg_arrays    : same shape for segments (x1, y1, x2, y2,
                          net_id, seg_id, layer, width).
        _poly_records  : list of (xs_arr, ys_arr, net_id, poly_id,
                          layer_byte). Variable-length verts make a
                          single packed array unwieldy for polylines.
    """
    # Numpy storage (canonical). Populated by _extract_layer_records via
    # the from_file flow, or rehydrated by cache load.
    _pad_arrays: Optional[Dict[str, Any]] = field(
        default=None, repr=False, compare=False)
    _seg_arrays: Optional[Dict[str, Any]] = field(
        default=None, repr=False, compare=False)
    _poly_records: List[Any] = field(
        default_factory=list, repr=False, compare=False)
    # Lazy dataclass-list caches. None until first .pads/.segments/.polylines
    # access materialises them.
    _pads_cache: Optional[List[Pad]] = field(
        default=None, repr=False, compare=False)
    _segs_cache: Optional[List[Segment]] = field(
        default=None, repr=False, compare=False)
    _polys_cache: Optional[List[Polyline]] = field(
        default=None, repr=False, compare=False)
    # Vias are synthesized after build (not from the TVW file directly) by
    # `_extract_vias`. Empty until then. Same shape as `pads`/`segments`:
    # public list of dataclass instances. We don't have a numpy-array
    # mirror because vias are sparse (typically <2 % of pad count) and
    # never iterated in tight per-segment loops.
    vias: List["Via"] = field(default_factory=list)
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

    # Layer-byte -> layer-name map. Default is the legacy 2-layer mapping
    # (matches what `_materialize_*` and `_build_native` used to hardcode).
    # `from_file` overwrites this with the actual layer count detected for
    # this board: 2 entries for mobos, ~10 for GPU PCBs. Index 0 is always
    # outermost-top, index 1 is always outermost-bottom; indices 2..N-1 are
    # inner layers in file order. The byte == 0 / byte == 1 invariant is
    # what tvw_native.dll's via stitcher relies on, so it must hold for any
    # board count (which is why bytes 2..N-1 carry inner layers, not
    # bytes 1..N-2).
    _layer_names: List[str] = field(
        default_factory=lambda: ["TOP", "BOTTOM"])

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
    # v8: TraceGraph storage refactor — `pads`/`segments`/`polylines`
    #     are now @property accessors backed by `_pad_arrays`/
    #     `_seg_arrays`/`_poly_records` numpy structures plus lazy
    #     `_pads_cache`/`_segs_cache`/`_polys_cache` materialisation.
    #     Old v7 pickles were dumped with the dataclass-field layout
    #     and will not unpickle into the new field set; force rebuild.
    # v9: N-layer support — TraceGraph now carries `_layer_names` and
    #     records' layer_byte indexes into it. For 2-layer mobos this is
    #     ["TOP", "BOTTOM"] (matches v8 behaviour). For boards with
    #     more layers (e.g. GPU PCBs) it's ["TOP", "BOTTOM", "INNER_1",
    #     "INNER_2", ...]. v8 caches lacked the field and would unpickle
    #     missing it; force rebuild so the field gets populated.
    # v10: Phase 2 — explicit Via records. TraceGraph gained a `vias`
    #     field (List[Via]) populated by `_extract_vias` after build.
    #     v9 caches lacked the field, so unpickling them succeeds (the
    #     dataclass tolerates missing list-fields by leaving the default
    #     factory value) but `vias` ends up empty even on boards that
    #     have many vias. Force rebuild so the via list gets populated.
    _CACHE_VERSION = 10

    # ---- lazy dataclass-list accessors ----------------------------------
    # These build Pad / Segment / Polyline instances on first access from
    # the canonical numpy storage. Hot consumers (find_broken_nets, viewer
    # trace render, geometry_on_net) read from `_pad_arrays`/`_seg_arrays`/
    # `_poly_records` directly and never trigger materialisation.

    @property
    def pads(self) -> List[Pad]:
        if self._pads_cache is None:
            self._pads_cache = self._materialize_pads()
        return self._pads_cache

    @pads.setter
    def pads(self, value: List[Pad]) -> None:
        # External callers (cache load, gencad_parser) set the list
        # directly; the next .pads access just returns it. We don't
        # backfill _pad_arrays from this — set _pad_arrays explicitly
        # if you want the fast path.
        self._pads_cache = list(value) if value is not None else []

    @property
    def segments(self) -> List[Segment]:
        if self._segs_cache is None:
            self._segs_cache = self._materialize_segments()
        return self._segs_cache

    @segments.setter
    def segments(self, value: List[Segment]) -> None:
        self._segs_cache = list(value) if value is not None else []

    @property
    def polylines(self) -> List[Polyline]:
        if self._polys_cache is None:
            self._polys_cache = self._materialize_polylines()
        return self._polys_cache

    @polylines.setter
    def polylines(self, value: List[Polyline]) -> None:
        self._polys_cache = list(value) if value is not None else []

    def _materialize_pads(self) -> List[Pad]:
        """Build Pad instances from `_pad_arrays`. ~25 ms / 50 K records.
        Returns [] if no arrays are present (e.g. legacy cache load
        already populated `_pads_cache` and we never get here)."""
        a = self._pad_arrays
        if not a:
            return []
        x = a["x"].tolist();      y = a["y"].tolist()
        net = a["net_id"].tolist(); pid = a["pad_id"].tolist()
        layer = a["layer"].tolist()
        ptype = a["pad_type"].tolist() if "pad_type" in a else [0] * len(x)
        stride = a["stride"].tolist() if "stride" in a else [38] * len(x)
        layer_str = self._layer_names
        return [
            Pad(pid[i], x[i], y[i], net[i], layer_str[layer[i]],
                ptype[i], stride[i])
            for i in range(len(x))
        ]

    def _materialize_segments(self) -> List[Segment]:
        a = self._seg_arrays
        if not a:
            return []
        x1 = a["x1"].tolist(); y1 = a["y1"].tolist()
        x2 = a["x2"].tolist(); y2 = a["y2"].tolist()
        net = a["net_id"].tolist(); sid = a["seg_id"].tolist()
        layer = a["layer"].tolist()
        width = a["width"].tolist() if "width" in a else [0] * len(x1)
        layer_str = self._layer_names
        return [
            Segment(sid[i], x1[i], y1[i], x2[i], y2[i],
                    net[i], layer_str[layer[i]], width[i])
            for i in range(len(x1))
        ]

    def _materialize_polylines(self) -> List[Polyline]:
        records = self._poly_records
        if not records:
            return []
        layer_str = self._layer_names
        return [
            Polyline(pid,
                     list(zip(xs.tolist(), ys.tolist())),
                     net_id, layer_str[layer_b])
            for xs, ys, net_id, pid, layer_b in records
        ]

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
        regions = _board_regions_for(path, buf)
        n_layers = len(regions)
        layer_names = _layer_names_for_regions(n_layers)

        # Decode the net name table once; needed by net_name() and useful
        # for diagnostics on the way out.
        nt_start, nt_end = _find_net_table(buf)
        net_names = _build_net_index(buf, nt_start, nt_end) if nt_start >= 0 else []

        # Net-table boundary handling. The net-table sits AFTER all
        # trace data on every TVW we've inspected, so it gives us a
        # hard upper bound on where genuine trace regions can live.
        # Two cases:
        #   (a) a region straddles nt_start  -> cap end at nt_start
        #       (2026-05-07 polyline-crack fix; the original 2-layer
        #       code only handled this case for TOP/BOTTOM)
        #   (b) a region starts AT or PAST nt_start -> drop entirely.
        #       These are metadata Custom_NN clusters (component data,
        #       footprint outlines, net-name table fragments) that the
        #       autodetect picked up because they have a >= 50KB gap
        #       between adjacent headers. They contain no trace data
        #       and would feed garbage into the topology if scanned.
        if nt_start > 0:
            new_regions: List[Tuple[int, int]] = []
            for rs, re_ in regions:
                if rs >= nt_start:
                    continue
                if re_ > nt_start:
                    re_ = nt_start
                new_regions.append((rs, re_))
            regions = new_regions
            n_layers = len(regions)
            layer_names = _layer_names_for_regions(n_layers)
        if n_layers == 0:
            raise ValueError(
                f"No trace-data regions remain in {path} after net-table "
                f"trim. The file structure may be unusual; run tvw_customs.py "
                f"to inspect.")

        # Pull all geometry. Each layer's records are tagged with its
        # layer-byte (see `_layer_byte_for_region`), so once merged the
        # arrays carry per-record layer info that survives downstream.
        # Run all N layers in parallel — the C scanners (tvw_native)
        # release the GIL during their scan loops, which dominate this
        # phase, so threads genuinely run on multiple cores. For mobos
        # that's 2 threads; for GPU PCBs ~10. The thread-pool overhead
        # is negligible vs the per-region scan time (hundreds of ms each).
        #
        # ID accounting: each thread starts at 0; per-layer ids are
        # shifted by the running unfiltered-count totals from preceding
        # layers (in file order) so cross-layer ids stay collision-free.
        # We use UNFILTERED counts returned by `_extract_layer_records`
        # (n_pad / n_seg / n_poly), not the filtered-list lengths, so
        # absolute id values match what the previous serial implementation
        # produced for the legacy 2-layer boards. That keeps the topology
        # graph bit-identical to pre-threading runs on Z490/X570/B550
        # (which the Phase 3 spatial assertions depend on).
        import threading
        per_layer_results: List[Tuple[Any, ...]] = [None] * n_layers

        def _do(idx: int, rs: int, re_: int) -> None:
            ln = layer_names[idx]
            lb = _layer_byte_for_region(idx, n_layers)
            (n_pad, n_seg, n_poly,
             pa, sa, qa) = _extract_layer_records(
                buf, rs, re_, ln, 0, 0, 0, layer_byte=lb,
            )
            per_layer_results[idx] = (n_pad, n_seg, n_poly, pa, sa, qa)

        threads = [
            threading.Thread(
                target=_do, args=(idx, rs, re_), daemon=True,
            )
            for idx, (rs, re_) in enumerate(regions)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Walk results in file order, accumulating id shifts. This keeps
        # final pad/seg/poly id values matching the legacy 2-layer code
        # (TOP first, BOTTOM second) bit-for-bit.
        cum_pad = cum_seg = cum_poly = 0
        pad_chunks: List[Tuple[Dict[str, Any], int]] = []
        seg_chunks: List[Tuple[Dict[str, Any], int]] = []
        poly_chunks: List[Tuple[Dict[str, Any], int, int]] = []
        for idx, (n_pad, n_seg, n_poly, pa, sa, qa) in enumerate(per_layer_results):
            if pa is not None:
                pad_chunks.append((pa, cum_pad))
            if sa is not None:
                seg_chunks.append((sa, cum_seg))
            if qa is not None:
                poly_chunks.append((qa, cum_poly, idx))
            cum_pad += n_pad
            cum_seg += n_seg
            cum_poly += n_poly

        # Merge per-layer pad arrays. Each pa already has its own
        # layer_byte; we just expand it into a per-record uint8 array.
        merged_pad_arrays: Optional[Dict[str, Any]] = None
        if pad_chunks:
            xs, ys, ns, ts, ss, ids, layers = [], [], [], [], [], [], []
            for pa, id_shift in pad_chunks:
                xs.append(pa["x"]); ys.append(pa["y"])
                ns.append(pa["net_id"])
                ts.append(pa["pad_type"])
                ss.append(pa["stride"])
                ids.append(pa["pad_id"] + id_shift)
                layers.append(np.full(pa["x"].shape[0],
                                      pa["layer_byte"], dtype=np.uint8))
            merged_pad_arrays = {
                "x":        np.concatenate(xs),
                "y":        np.concatenate(ys),
                "net_id":   np.concatenate(ns),
                "pad_type": np.concatenate(ts),
                "stride":   np.concatenate(ss),
                "pad_id":   np.concatenate(ids),
                "layer":    np.concatenate(layers),
            }

        merged_seg_arrays: Optional[Dict[str, Any]] = None
        if seg_chunks:
            x1s, y1s, x2s, y2s = [], [], [], []
            ns, ws, ids, layers = [], [], [], []
            for sa, id_shift in seg_chunks:
                x1s.append(sa["x1"]); y1s.append(sa["y1"])
                x2s.append(sa["x2"]); y2s.append(sa["y2"])
                ns.append(sa["net_id"])
                ws.append(sa["width"])
                ids.append(sa["seg_id"] + id_shift)
                layers.append(np.full(sa["x1"].shape[0],
                                      sa["layer_byte"], dtype=np.uint8))
            merged_seg_arrays = {
                "x1": np.concatenate(x1s), "y1": np.concatenate(y1s),
                "x2": np.concatenate(x2s), "y2": np.concatenate(y2s),
                "net_id": np.concatenate(ns),
                "width":  np.concatenate(ws),
                "seg_id": np.concatenate(ids),
                "layer":  np.concatenate(layers),
            }

        # Polylines stay as a list of (xs_arr, ys_arr, net_id, poly_id,
        # layer_byte) for the C build path.
        merged_poly_records: List[Tuple[Any, Any, int, int, int]] = []
        for qa, id_shift, _idx in poly_chunks:
            for i, (xs_arr, ys_arr, net_id) in enumerate(qa["kept"]):
                merged_poly_records.append(
                    (xs_arr, ys_arr, net_id,
                     qa["poly_id_start"] + id_shift + i,
                     qa["layer_byte"]))

        # Decide whether net_id=0 is a real net or an "untagged" sentinel.
        # Vectorised — counts directly off the merged numpy array; no
        # need to iterate dataclass instances.
        if merged_pad_arrays is not None:
            net_arr = merged_pad_arrays["net_id"]
            n0_pads = int(np.count_nonzero(net_arr == 0))
            n_pads = int(net_arr.shape[0])
            zero_real = n_pads > 0 and (n0_pads / n_pads) > 0.02
        else:
            zero_real = False

        # Build the graph WITHOUT pre-built dataclass lists — the
        # canonical state lives in the numpy arrays we just merged.
        # Lists materialise lazily on first .pads/.segments/.polylines
        # access via the @property accessors.
        graph = cls(
            net_names=net_names,
            endpoint_tol=endpoint_tol, via_tol=via_tol,
            same_net_pad_tol=same_net_pad_tol,
            pad_to_trace_tol=pad_to_trace_tol,
            _zero_is_real_net=zero_real,
            _layer_names=layer_names,
        )
        graph._pad_arrays = merged_pad_arrays
        graph._seg_arrays = merged_seg_arrays
        graph._poly_records = merged_poly_records
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

    def _build_native(self) -> bool:
        """Run the C `build_topology_native` and adapt its output into
        the same Python state shape `_build()` would have produced.
        Returns True if it succeeded, False if the DLL isn't available
        (caller falls through to pure Python).

        Hot path: when `from_file` stashed the post-filter numpy arrays
        on the graph (via `_build_pad_arrays` / `_build_seg_arrays` /
        `_build_poly_records`), we hand them directly to the array
        entry point — no Python iteration over the dataclass list.
        Cache-loaded graphs don't have those arrays; we fall back to
        the legacy list-of-tuples path which iterates self.pads etc.
        """
        try:
            from tvw_native import build_topology, build_topology_arrays
        except Exception:
            return False

        pad_arrays = self._pad_arrays
        seg_arrays = self._seg_arrays
        poly_records = self._poly_records
        if pad_arrays is not None or seg_arrays is not None \
                or poly_records:
            result = build_topology_arrays(
                pad_arrays, seg_arrays, poly_records or [],
                endpoint_tol=self.endpoint_tol,
                via_tol=self.via_tol,
                same_net_pad_tol=self.same_net_pad_tol,
                pad_to_trace_tol=self.pad_to_trace_tol,
                zero_is_real_net=self._zero_is_real_net,
            )
        else:
            # Cache-load path or external caller: fall back to lists.
            pads_in = [
                (p.x, p.y, p.net_id, p.pad_id, p.layer)
                for p in self.pads
            ]
            segs_in = [
                (s.x1, s.y1, s.x2, s.y2, s.net_id, s.seg_id, s.layer)
                for s in self.segments
            ]
            polys_in = [
                (p.poly_id, p.vertices, p.net_id, p.layer)
                for p in self.polylines
            ]
            result = build_topology(
                pads_in, segs_in, polys_in,
                endpoint_tol=self.endpoint_tol,
                via_tol=self.via_tol,
                same_net_pad_tol=self.same_net_pad_tol,
                pad_to_trace_tol=self.pad_to_trace_tol,
                zero_is_real_net=self._zero_is_real_net,
            )
        if result is None:
            return False

        # Materialise Python state from the numpy outputs. The shapes
        # match the legacy `_build` exactly so the public query API
        # (find_broken_nets / net_at / geometry_on_net) works unchanged.
        nx = result["node_x"].tolist()
        ny = result["node_y"].tolist()
        nl = result["node_layer"].tolist()
        self._node_xy = list(zip(nx, ny))
        # Map layer-byte back to the layer string. Indices outside the
        # board's `_layer_names` shouldn't happen (would mean a stray
        # uint8 value the C side never wrote) but guard anyway.
        layer_str = self._layer_names
        n_layers = len(layer_str)
        self._node_layer = [
            layer_str[v] if 0 <= v < n_layers
            else f"LAYER_{int(v)}"
            for v in nl
        ]
        self._node_net = result["node_net"].tolist()

        # Union-find: copy the C-side parent/rank/size arrays into a
        # Python UnionFind so the existing `find_broken_nets()` works.
        n_nodes = result["node_count"]
        self._uf = UnionFind(n_nodes)
        self._uf.parent = result["uf_parent"].tolist()
        self._uf.rank = result["uf_rank"].tolist()
        self._uf.size = result["uf_size"].tolist()

        # Per-record id->node maps. Resolve original ids from whatever
        # path we took: arrays carry pad_id/seg_id/poly_id directly;
        # the list path uses dataclass attributes.
        pn = result["pad_node"].tolist()
        if pad_arrays is not None:
            pid_list = pad_arrays["pad_id"].tolist()
            self._pad_node = {pid_list[i]: pn[i] for i in range(len(pn))}
        else:
            self._pad_node = {
                p.pad_id: pn[i] for i, p in enumerate(self.pads)
            }
        sa = result["seg_node_a"].tolist()
        sb = result["seg_node_b"].tolist()
        if seg_arrays is not None:
            sid_list = seg_arrays["seg_id"].tolist()
            self._seg_nodes = {
                sid_list[i]: (sa[i], sb[i]) for i in range(len(sa))
            }
        else:
            self._seg_nodes = {
                s.seg_id: (sa[i], sb[i]) for i, s in enumerate(self.segments)
            }
        pnd = result["poly_nodes_data"]
        pnoff = result["poly_nodes_off"]
        self._poly_nodes = {}
        if poly_records is not None:
            for i, rec in enumerate(poly_records):
                pid = rec[3]
                o0 = int(pnoff[i])
                o1 = int(pnoff[i + 1])
                self._poly_nodes[pid] = pnd[o0:o1].tolist()
        else:
            for i, p in enumerate(self.polylines):
                o0 = int(pnoff[i])
                o1 = int(pnoff[i + 1])
                self._poly_nodes[p.poly_id] = pnd[o0:o1].tolist()

        # Backfilled net ids — write into the canonical arrays. If the
        # dataclass list caches are already populated (rare; means
        # someone iterated .pads/.segments before build), invalidate
        # them so the next access re-materialises with the new nets.
        seg_net = result["seg_net"]
        if self._seg_arrays is not None and seg_net.shape[0]:
            self._seg_arrays["net_id"] = seg_net.astype(np.int32, copy=False)
        poly_net = result["poly_net"].tolist()
        if self._poly_records:
            self._poly_records = [
                (xs, ys, poly_net[i], pid, lb)
                for i, (xs, ys, _, pid, lb) in enumerate(self._poly_records)
            ]
        # Invalidate caches only when the canonical arrays were just
        # updated. On the legacy list-fallback path (e.g. GENCAD) the
        # caches ARE the canonical state — clearing them would force
        # `_materialize_*` to read empty arrays and return []. The
        # array path's caches (if any) need to drop because the new
        # net_ids in the arrays would otherwise diverge from the cache.
        if self._seg_arrays is not None:
            self._segs_cache = None
        if self._poly_records:
            self._polys_cache = None

        # Counters.
        self._via_count = result["via_count"]
        self._same_net_pad_fusions = result["snp_count"]
        self._pad_to_trace_fusions = result["ptt_count"]
        self.propagation_conflicts = result["propagation_conflicts"]
        self.propagation_changes = result["propagation_changes"]

        # Materialise Via records. The C side already did the UF unions
        # when it found via-bridges; `_extract_vias` re-runs the same
        # bucket scan in Python to build the public Via list (the C
        # struct doesn't return them). Re-unions are no-ops on already
        # merged nodes. C's reported via_count is preserved above; the
        # extracted Via list should match it 1:1 (same algorithm, same
        # tol) but we don't assert — safer to surface whatever Python
        # finds even if a future C-side change drifts.
        self._extract_vias()

        # Defer SpatialHash population. `net_at()` is the only consumer;
        # most builds never call it, so we build it lazily on first
        # access. Saves ~200 ms on the cold path. See `_ensure_spatial`.
        self._spatial = None

        return True

    def _extract_vias(self) -> int:
        """Synthesize Via records from same-XY pads on opposite layers
        and (defensively) ensure the corresponding Union-Find unions
        exist. Idempotent — calling twice rebuilds `self.vias` and
        re-runs unions; already-merged nodes are no-ops.

        Algorithm matches the original inline pass: bucket TOP pads by
        a (via_tol)-grid, probe each BOTTOM pad against the 3x3
        neighbourhood, take the closest match within tol². Each match
        becomes one Via record; via_id assigns sequentially in BOTTOM-
        iteration order (stable for a given pad order, not stable
        across rebuilds with different pad sets).

        Returns the number of vias found. Used by both `_build` (where
        unions are first established) and `_build_native` (where the C
        path already did the unions and this just materialises records)."""
        self.vias = []
        if self.via_tol <= 0 or not self.pads:
            return 0
        via_cell = max(1, self.via_tol)
        top_pads_by_cell: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        pads_by_id: Dict[int, Pad] = {p.pad_id: p for p in self.pads}
        for pad in self.pads:
            if pad.layer == "TOP":
                gx, gy = pad.x // via_cell, pad.y // via_cell
                top_pads_by_cell[(gx, gy)].append(pad.pad_id)

        tol2 = self.via_tol * self.via_tol
        next_via_id = 0
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
            if best_id < 0:
                continue
            tp = pads_by_id[best_id]
            # Midpoint of the two pad centres — a sub-via_tol shift but
            # makes the marker sit between the two pads instead of
            # biasing to either layer's recorded centre.
            cx = (tp.x + pad.x) // 2
            cy = (tp.y + pad.y) // 2
            # Net id: prefer non-zero (zero is "untagged" on most files
            # except X570). Both pads should agree by construction.
            nid = tp.net_id or pad.net_id
            self.vias.append(Via(
                via_id=next_via_id,
                x=cx, y=cy,
                net_id=nid,
                top_pad_id=best_id,
                bot_pad_id=pad.pad_id,
            ))
            next_via_id += 1
            # Defensive UF union — no-op when C path already merged
            # these nodes. _pad_node may not be populated on cache-
            # loaded graphs that skipped both build paths; gate on it.
            if self._uf is not None and self._pad_node:
                top_node = self._pad_node.get(best_id, -1)
                bot_node = self._pad_node.get(pad.pad_id, -1)
                if top_node >= 0 and bot_node >= 0:
                    self._uf.union(top_node, bot_node)
        return next_via_id

    def _ensure_spatial(self) -> "SpatialHash":
        """Lazy-build the SpatialHash from current node positions.
        Called on first `net_at` access after a native build."""
        if self._spatial is not None:
            return self._spatial
        sh = SpatialHash(self.endpoint_tol)
        for nid in range(len(self._node_xy)):
            x, y = self._node_xy[nid]
            sh.add(self._node_layer[nid], x, y, nid)
        self._spatial = sh
        return sh

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
        # Native fast path — runs the entire phase in C (~5-10× total
        # wall-time on Z490). Falls through to Python below if the DLL
        # isn't available.
        if self._build_native():
            return

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
        # within via_tol. `_extract_vias` materialises a Via record per
        # match (so the UI can render markers / handle clicks) AND
        # unions the corresponding pad nodes — that's why we call it
        # here instead of after the build is done.
        self._via_count = self._extract_vias()

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
        sh = self._ensure_spatial()
        if tol <= self.endpoint_tol:
            best_id = -1
            best_d2 = tol * tol + 1
            for nid in sh.query_near(layer, x, y):
                nx, ny = self._node_xy[nid]
                d2 = (nx - x) ** 2 + (ny - y) ** 2
                if d2 <= tol * tol and d2 < best_d2:
                    best_d2 = d2
                    best_id = nid
            return self._node_net[best_id] if best_id >= 0 else 0
        # Wider scan: iterate manually over more cells.
        cell = sh.cell
        radius_cells = (tol // cell) + 1
        gx = x // cell
        gy = y // cell
        best_id = -1
        best_d2 = tol * tol + 1
        for dx in range(-radius_cells, radius_cells + 1):
            for dy in range(-radius_cells, radius_cells + 1):
                bucket = sh.buckets.get((layer, gx + dx, gy + dy))
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
        """All segments and polylines on the given net. For renderers.

        Uses the numpy net_id array for O(N) mask + materialise only
        the matching rows, when arrays are available. Avoids paying
        the full `len(segments)` materialisation cost on every call.
        """
        # Segments: numpy mask first, then build only matching Segment
        # instances (typically 0 — 500 per net out of ~43 K total).
        s_arr = self._seg_arrays
        if s_arr is not None:
            net_arr = s_arr["net_id"]
            mask = (net_arr == net_id)
            if mask.any():
                idx = np.flatnonzero(mask).tolist()
                x1 = s_arr["x1"]; y1 = s_arr["y1"]
                x2 = s_arr["x2"]; y2 = s_arr["y2"]
                sid = s_arr["seg_id"]; width = s_arr["width"]
                layer = s_arr["layer"]
                layer_str = self._layer_names
                s = [
                    Segment(int(sid[i]), int(x1[i]), int(y1[i]),
                            int(x2[i]), int(y2[i]),
                            net_id, layer_str[layer[i]], int(width[i]))
                    for i in idx
                ]
            else:
                s = []
        else:
            s = [seg for seg in self.segments if seg.net_id == net_id]

        # Polylines: filter the records list (small count, varying len)
        if self._poly_records:
            layer_str = self._layer_names
            p = [
                Polyline(pid,
                         list(zip(xs.tolist(), ys.tolist())),
                         net_id, layer_str[layer_b])
                for xs, ys, n, pid, layer_b in self._poly_records
                if n == net_id
            ]
        else:
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
