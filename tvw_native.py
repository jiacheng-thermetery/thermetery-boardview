# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""ctypes glue for tvw_native.dll — the hot scanners ported to C.

The DLL implements the 5 scanners that dominate cold-load time:
  - find_pad_runs              (whole-file, both 38- and 54-byte strides)
  - scan_pads_stride_aware     (region-bounded, with coord validation)
  - find_net_table             (longest run of Pascal strings)
  - find_polyline_blocks       (count/type/poly framed blocks)
  - find_tagged_polylines      (net_id/K/verts/term tagged polylines)
  - find_segments_in_gap       (24-byte trace segments)

Each native function returns the count of records written to a
caller-supplied output array; we wrap them so callers see the same
list-of-tuples shape they did before.

If the DLL is missing or fails to load, every accessor returns None and
callers fall through to the Python implementation transparently.
"""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path
from typing import List, Optional, Tuple


# --------------------------------------------------------------------------
# Output record types — must mirror the C side byte-for-byte.
# --------------------------------------------------------------------------

class _PadRun(ctypes.Structure):
    _fields_ = [
        ("start", ctypes.c_uint64),
        ("end", ctypes.c_uint64),
        ("count", ctypes.c_uint32),
        ("stride", ctypes.c_uint32),
    ]


class _PolylineBlock(ctypes.Structure):
    _fields_ = [
        ("start", ctypes.c_uint64),
        ("count", ctypes.c_uint32),
        ("_pad", ctypes.c_uint32),
        ("end", ctypes.c_uint64),
    ]


class _TaggedPoly(ctypes.Structure):
    _fields_ = [
        ("off", ctypes.c_uint64),
        ("net_id", ctypes.c_uint32),
        ("K", ctypes.c_uint32),
    ]


class _SegRun(ctypes.Structure):
    _fields_ = [
        ("start", ctypes.c_uint64),
        ("end", ctypes.c_uint64),
        ("count", ctypes.c_uint32),
        ("_pad", ctypes.c_uint32),
    ]


class _PolyChain(ctypes.Structure):
    _fields_ = [
        ("start", ctypes.c_uint64),
        ("end", ctypes.c_uint64),
        ("count", ctypes.c_uint32),
        ("_pad", ctypes.c_uint32),
    ]


# --------------------------------------------------------------------------
# DLL loader
# --------------------------------------------------------------------------

_LIB = None


def _load() -> Optional[ctypes.CDLL]:
    global _LIB
    if _LIB is not None:
        return _LIB if _LIB is not False else None
    here = Path(__file__).resolve().parent
    if sys.platform.startswith("win"):
        names = ["tvw_native.dll"]
    elif sys.platform == "darwin":
        names = ["tvw_native.dylib", "libtvw_native.dylib"]
    else:
        names = ["tvw_native.so", "libtvw_native.so"]
    for n in names:
        p = here / n
        if not p.exists():
            continue
        try:
            lib = ctypes.CDLL(str(p))
        except OSError:
            continue

        # Bind signatures.
        lib.find_pad_runs_native.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.POINTER(_PadRun), ctypes.c_size_t,
        ]
        lib.find_pad_runs_native.restype = ctypes.c_size_t

        lib.scan_pads_stride_aware_native.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_size_t, ctypes.c_size_t,
            ctypes.c_uint32, ctypes.c_int32,
            ctypes.POINTER(_PadRun), ctypes.c_size_t,
        ]
        lib.scan_pads_stride_aware_native.restype = ctypes.c_size_t

        lib.find_net_table_native.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_int64), ctypes.POINTER(ctypes.c_int64),
        ]
        lib.find_net_table_native.restype = None

        lib.find_polyline_blocks_native.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_size_t, ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.POINTER(_PolylineBlock), ctypes.c_size_t,
        ]
        lib.find_polyline_blocks_native.restype = ctypes.c_size_t

        lib.find_tagged_polylines_in_gap_native.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_size_t, ctypes.c_size_t,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(_TaggedPoly), ctypes.c_size_t,
        ]
        lib.find_tagged_polylines_in_gap_native.restype = ctypes.c_size_t

        lib.find_segments_in_gap_native.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_size_t, ctypes.c_size_t,
            ctypes.c_uint32, ctypes.c_int,
            ctypes.POINTER(_SegRun), ctypes.c_size_t,
        ]
        lib.find_segments_in_gap_native.restype = ctypes.c_size_t

        lib.find_polyline_chains_in_gap_native.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_size_t, ctypes.c_size_t,
            ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(_PolyChain), ctypes.c_size_t,
        ]
        lib.find_polyline_chains_in_gap_native.restype = ctypes.c_size_t

        _LIB = lib
        return lib
    _LIB = False
    return None


def available() -> bool:
    return _load() is not None


# --------------------------------------------------------------------------
# Public Python-friendly wrappers. Each returns the same list-of-tuples
# shape as the legacy Python scanner, OR None if the DLL is unavailable
# (caller should fall back to Python).
# --------------------------------------------------------------------------

# Output buffer sizes — chosen well above the maximum we observe on real
# motherboards (the largest TVW we have produces ~150 pad runs across
# both layers, ~2 K polyline blocks, ~10 K tagged polylines, ~200 seg
# runs). If we ever hit the ceiling we'd silently truncate — bumping
# these doesn't hurt much (a few KB stack-side arrays).
_MAX_PAD_RUNS = 4096
_MAX_BLOCKS = 32768
_MAX_TAGGED = 65536
_MAX_SEGS = 4096


def find_pad_runs(buf: bytes, min_run: int = 50
                  ) -> Optional[List[Tuple[int, int, int]]]:
    """Whole-file pad scan. Returns list of (start, end, stride)."""
    lib = _load()
    if lib is None:
        return None
    out = (_PadRun * _MAX_PAD_RUNS)()
    n = lib.find_pad_runs_native(buf, len(buf), min_run, out, _MAX_PAD_RUNS)
    return [(int(out[i].start), int(out[i].end), int(out[i].stride))
            for i in range(n)]


def scan_pads_stride_aware(buf: bytes, region_start: int, region_end: int,
                           min_run: int = 3, coord_max: int = 2_000_000
                           ) -> Optional[List[Tuple[int, int, int, int]]]:
    """Region pad scan with coord validation. Returns
    list of (start, end, count, stride)."""
    lib = _load()
    if lib is None:
        return None
    out = (_PadRun * _MAX_PAD_RUNS)()
    n = lib.scan_pads_stride_aware_native(
        buf, len(buf), region_start, region_end,
        min_run, coord_max, out, _MAX_PAD_RUNS,
    )
    return [(int(out[i].start), int(out[i].end),
             int(out[i].count), int(out[i].stride))
            for i in range(n)]


def find_net_table(buf: bytes) -> Optional[Tuple[int, int]]:
    """Returns (start, end). Both -1 if no run found.
    None if the native lib isn't available."""
    lib = _load()
    if lib is None:
        return None
    s = ctypes.c_int64(-1)
    e = ctypes.c_int64(-1)
    lib.find_net_table_native(buf, len(buf), ctypes.byref(s), ctypes.byref(e))
    return (int(s.value), int(e.value))


def find_polyline_blocks(buf: bytes, region_start: int, region_end: int,
                         max_K: int = 100000
                         ) -> Optional[List[Tuple[int, int, int]]]:
    """Returns list of (start, count, end) — same shape as the
    Python implementation."""
    lib = _load()
    if lib is None:
        return None
    out = (_PolylineBlock * _MAX_BLOCKS)()
    n = lib.find_polyline_blocks_native(
        buf, len(buf), region_start, region_end, max_K, out, _MAX_BLOCKS,
    )
    return [(int(out[i].start), int(out[i].count), int(out[i].end))
            for i in range(n)]


def find_tagged_polylines_in_gap(
        buf: bytes, gap_start: int, gap_end: int,
        term_size: int = 4, max_net_id: int = 4000,
        max_vertices: int = 100000,
        ) -> Optional[List[Tuple[int, int, int]]]:
    """Returns list of (offset, net_id, K)."""
    lib = _load()
    if lib is None:
        return None
    out = (_TaggedPoly * _MAX_TAGGED)()
    n = lib.find_tagged_polylines_in_gap_native(
        buf, len(buf), gap_start, gap_end,
        term_size, max_net_id, max_vertices,
        out, _MAX_TAGGED,
    )
    return [(int(out[i].off), int(out[i].net_id), int(out[i].K))
            for i in range(n)]


def find_segments_in_gap(buf: bytes, gap_start: int, gap_end: int,
                         min_run: int = 10, allow_zero_net: bool = True,
                         ) -> Optional[List[Tuple[int, int, int]]]:
    """Returns list of (run_start, run_end, count)."""
    lib = _load()
    if lib is None:
        return None
    out = (_SegRun * _MAX_SEGS)()
    n = lib.find_segments_in_gap_native(
        buf, len(buf), gap_start, gap_end,
        min_run, 1 if allow_zero_net else 0,
        out, _MAX_SEGS,
    )
    return [(int(out[i].start), int(out[i].end), int(out[i].count))
            for i in range(n)]


_MAX_CHAINS = 4096


def find_polyline_chains_in_gap(buf: bytes, gap_start: int, gap_end: int,
                                min_chain: int = 3, max_K: int = 100000,
                                ) -> Optional[List[Tuple[int, int, int]]]:
    """Returns list of (chain_start, chain_end, count)."""
    lib = _load()
    if lib is None:
        return None
    out = (_PolyChain * _MAX_CHAINS)()
    n = lib.find_polyline_chains_in_gap_native(
        buf, len(buf), gap_start, gap_end,
        min_chain, max_K, out, _MAX_CHAINS,
    )
    return [(int(out[i].start), int(out[i].end), int(out[i].count))
            for i in range(n)]


# --------------------------------------------------------------------------
# Topology build (C port of TraceGraph._build).
# --------------------------------------------------------------------------
#
# Inputs are laid out as numpy structured arrays whose dtypes mirror the
# C structs (`BuildPad`, `BuildSeg`, `BuildPolyMeta`). Outputs are
# numpy int32 / uint8 arrays preallocated by the caller; the C function
# fills them and reports back the actual node count.

import numpy as np

# These dtypes MUST match the C structs in tvw_native.c byte-for-byte.
# Field types and order are critical; padding is explicit.
_BUILD_PAD_DTYPE = np.dtype({
    "names":   ["x", "y", "net_id", "pad_id", "layer", "_pad"],
    "formats": ["<i4", "<i4", "<i4", "<u4", "u1", "(3,)u1"],
    "offsets": [0, 4, 8, 12, 16, 17],
    "itemsize": 20,
})

_BUILD_SEG_DTYPE = np.dtype({
    "names":   ["x1", "y1", "x2", "y2", "net_id", "seg_id",
                "layer", "_pad"],
    "formats": ["<i4", "<i4", "<i4", "<i4", "<i4", "<u4",
                "u1", "(3,)u1"],
    "offsets": [0, 4, 8, 12, 16, 20, 24, 25],
    "itemsize": 28,
})

_BUILD_POLY_DTYPE = np.dtype({
    "names":   ["poly_id", "verts_offset", "verts_count", "net_id",
                "layer", "_pad"],
    "formats": ["<u4", "<u4", "<u4", "<i4", "u1", "(3,)u1"],
    "offsets": [0, 4, 8, 12, 16, 17],
    "itemsize": 20,
})


def _bind_build_topology(lib: ctypes.CDLL) -> None:
    """Bind `build_topology_native` signature on the loaded DLL."""
    # 13 input pointers/scalars, all pre-typed to avoid implicit conversion bugs.
    lib.build_topology_native.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32,             # pads, n_pads
        ctypes.c_void_p, ctypes.c_uint32,             # segs, n_segs
        ctypes.c_void_p, ctypes.c_uint32,             # polys, n_polys
        ctypes.c_void_p,                              # poly_verts (flat int32)
        ctypes.c_int32, ctypes.c_int32,               # endpoint_tol, via_tol
        ctypes.c_int32, ctypes.c_int32,               # snp_tol, ptt_tol
        ctypes.c_int32,                               # zero_is_real_net
        ctypes.c_void_p,                              # BuildOut*
    ]
    lib.build_topology_native.restype = ctypes.c_int32


class _BuildOut(ctypes.Structure):
    _fields_ = [
        ("node_x",            ctypes.c_void_p),
        ("node_y",            ctypes.c_void_p),
        ("node_layer",        ctypes.c_void_p),
        ("node_net",          ctypes.c_void_p),
        ("uf_parent",         ctypes.c_void_p),
        ("uf_rank",           ctypes.c_void_p),
        ("uf_size",           ctypes.c_void_p),
        ("pad_node",          ctypes.c_void_p),
        ("seg_node_a",        ctypes.c_void_p),
        ("seg_node_b",        ctypes.c_void_p),
        ("poly_nodes_data",   ctypes.c_void_p),
        ("poly_nodes_off",    ctypes.c_void_p),
        ("seg_net_out",       ctypes.c_void_p),
        ("poly_net_out",      ctypes.c_void_p),
        ("node_count",                ctypes.c_uint32),
        ("via_count",                 ctypes.c_uint32),
        ("snp_count",                 ctypes.c_uint32),
        ("ptt_count",                 ctypes.c_uint32),
        ("propagation_conflicts",     ctypes.c_uint32),
        ("propagation_changes",       ctypes.c_uint32),
    ]


# Bind on first DLL load. We need to extend the existing _load() to
# include build_topology_native. Patch via a wrapper that handles both
# the original scanners and the build entry point.

_original_load = _load


def _load_with_build() -> Optional[ctypes.CDLL]:
    lib = _original_load()
    if lib is None:
        return None
    if not hasattr(lib, "_build_topology_bound"):
        try:
            _bind_build_topology(lib)
            setattr(lib, "_build_topology_bound", True)
        except (AttributeError, OSError):
            return None
    return lib


_load = _load_with_build  # type: ignore[assignment]


def build_topology_arrays(
    pad_arrays: Optional[dict],
    seg_arrays: Optional[dict],
    poly_records: list,
    *,
    endpoint_tol: int,
    via_tol: int,
    same_net_pad_tol: int,
    pad_to_trace_tol: int,
    zero_is_real_net: bool,
):
    """Direct-from-arrays variant of `build_topology`.

    Skips the per-record Python iteration that the list-of-tuples form
    requires. Inputs:
      pad_arrays  : dict with int32 arrays {x, y, net_id, pad_id, layer}
                     (`layer` is uint8: 0=TOP, 1=BOTTOM)
      seg_arrays  : dict with {x1, y1, x2, y2, net_id, seg_id, layer}
      poly_records: list of (xs_arr, ys_arr, net_id, poly_id, layer_byte)

    Each `pad_id` / `seg_id` / `poly_id` value indexes the per-record
    output map; the C side stores results by ARRAY POSITION (the i-th
    input pad gets pad_node[i]). The Python side carries the id mapping
    so callers can resolve back to original ids.
    """
    lib = _load()
    if lib is None:
        return None

    n_pads = int(pad_arrays["x"].shape[0]) if pad_arrays else 0
    n_segs = int(seg_arrays["x1"].shape[0]) if seg_arrays else 0
    n_polys = len(poly_records)

    # Pack inputs straight into structured arrays via column assignment.
    # Each numpy field assignment is a single C-level memcpy when source
    # and dest dtypes match — far faster than per-element __setitem__.
    pad_arr = np.empty(max(n_pads, 1), dtype=_BUILD_PAD_DTYPE)
    if n_pads:
        pad_arr["x"][:n_pads]      = pad_arrays["x"]
        pad_arr["y"][:n_pads]      = pad_arrays["y"]
        pad_arr["net_id"][:n_pads] = pad_arrays["net_id"]
        pad_arr["pad_id"][:n_pads] = pad_arrays["pad_id"]
        pad_arr["layer"][:n_pads]  = pad_arrays["layer"]

    seg_arr = np.empty(max(n_segs, 1), dtype=_BUILD_SEG_DTYPE)
    if n_segs:
        seg_arr["x1"][:n_segs]     = seg_arrays["x1"]
        seg_arr["y1"][:n_segs]     = seg_arrays["y1"]
        seg_arr["x2"][:n_segs]     = seg_arrays["x2"]
        seg_arr["y2"][:n_segs]     = seg_arrays["y2"]
        seg_arr["net_id"][:n_segs] = seg_arrays["net_id"]
        seg_arr["seg_id"][:n_segs] = seg_arrays["seg_id"]
        seg_arr["layer"][:n_segs]  = seg_arrays["layer"]

    total_verts = sum(int(rec[0].shape[0]) for rec in poly_records)
    poly_arr = np.empty(max(n_polys, 1), dtype=_BUILD_POLY_DTYPE)
    poly_verts = np.empty(max(total_verts, 1) * 2, dtype=np.int32)
    if n_polys:
        offsets = np.empty(n_polys, dtype=np.uint32)
        counts = np.empty(n_polys, dtype=np.uint32)
        running = 0
        for i, (xs_arr, _ys_arr, _nid, _pid, _lb) in enumerate(poly_records):
            offsets[i] = running
            counts[i] = int(xs_arr.shape[0])
            running += int(xs_arr.shape[0])
        poly_arr["poly_id"][:n_polys]      = [r[3] for r in poly_records]
        poly_arr["verts_offset"][:n_polys] = offsets
        poly_arr["verts_count"][:n_polys]  = counts
        poly_arr["net_id"][:n_polys]       = [r[2] for r in poly_records]
        poly_arr["layer"][:n_polys]        = [r[4] for r in poly_records]
        # Pack vertices: for each poly, write x,y interleaved.
        write = 0
        for xs_arr, ys_arr, _, _, _ in poly_records:
            k = int(xs_arr.shape[0])
            poly_verts[2 * write    : 2 * (write + k) : 2] = xs_arr
            poly_verts[2 * write + 1: 2 * (write + k) : 2] = ys_arr
            write += k

    return _call_build_topology(
        pad_arr, n_pads, seg_arr, n_segs,
        poly_arr, n_polys, poly_verts, total_verts,
        endpoint_tol, via_tol, same_net_pad_tol, pad_to_trace_tol,
        zero_is_real_net,
    )


def _call_build_topology(pad_arr, n_pads, seg_arr, n_segs,
                          poly_arr, n_polys, poly_verts, total_verts,
                          endpoint_tol, via_tol, same_net_pad_tol,
                          pad_to_trace_tol, zero_is_real_net):
    """Common tail of build_topology: invoke the C function and adapt
    its outputs into a dict the Python caller can consume."""
    lib = _load()
    if lib is None:
        return None

    node_cap = max(n_pads + 2 * n_segs + total_verts, 1)
    node_x = np.zeros(node_cap, dtype=np.int32)
    node_y = np.zeros(node_cap, dtype=np.int32)
    node_layer = np.zeros(node_cap, dtype=np.uint8)
    node_net = np.zeros(node_cap, dtype=np.int32)
    uf_parent = np.zeros(node_cap, dtype=np.int32)
    uf_rank = np.zeros(node_cap, dtype=np.int32)
    uf_size = np.zeros(node_cap, dtype=np.int32)
    pad_node = np.zeros(max(n_pads, 1), dtype=np.int32)
    seg_node_a = np.zeros(max(n_segs, 1), dtype=np.int32)
    seg_node_b = np.zeros(max(n_segs, 1), dtype=np.int32)
    poly_nodes_data = np.zeros(max(total_verts, 1), dtype=np.int32)
    poly_nodes_off = np.zeros(max(n_polys + 1, 1), dtype=np.uint32)
    seg_net_out = np.zeros(max(n_segs, 1), dtype=np.int32)
    poly_net_out = np.zeros(max(n_polys, 1), dtype=np.int32)

    out = _BuildOut()
    out.node_x = node_x.ctypes.data
    out.node_y = node_y.ctypes.data
    out.node_layer = node_layer.ctypes.data
    out.node_net = node_net.ctypes.data
    out.uf_parent = uf_parent.ctypes.data
    out.uf_rank = uf_rank.ctypes.data
    out.uf_size = uf_size.ctypes.data
    out.pad_node = pad_node.ctypes.data
    out.seg_node_a = seg_node_a.ctypes.data
    out.seg_node_b = seg_node_b.ctypes.data
    out.poly_nodes_data = poly_nodes_data.ctypes.data
    out.poly_nodes_off = poly_nodes_off.ctypes.data
    out.seg_net_out = seg_net_out.ctypes.data
    out.poly_net_out = poly_net_out.ctypes.data

    rv = lib.build_topology_native(
        pad_arr.ctypes.data, n_pads,
        seg_arr.ctypes.data, n_segs,
        poly_arr.ctypes.data, n_polys,
        poly_verts.ctypes.data,
        endpoint_tol, via_tol,
        same_net_pad_tol, pad_to_trace_tol,
        1 if zero_is_real_net else 0,
        ctypes.byref(out),
    )
    if rv != 0:
        return None

    nc = int(out.node_count)
    return {
        "node_x":       node_x[:nc],
        "node_y":       node_y[:nc],
        "node_layer":   node_layer[:nc],
        "node_net":     node_net[:nc],
        "uf_parent":    uf_parent[:nc],
        "uf_rank":      uf_rank[:nc],
        "uf_size":      uf_size[:nc],
        "pad_node":     pad_node[:n_pads],
        "seg_node_a":   seg_node_a[:n_segs],
        "seg_node_b":   seg_node_b[:n_segs],
        "poly_nodes_data": poly_nodes_data[:total_verts],
        "poly_nodes_off":  poly_nodes_off[:n_polys + 1],
        "seg_net":      seg_net_out[:n_segs],
        "poly_net":     poly_net_out[:n_polys],
        "node_count":            nc,
        "via_count":             int(out.via_count),
        "snp_count":             int(out.snp_count),
        "ptt_count":             int(out.ptt_count),
        "propagation_conflicts": int(out.propagation_conflicts),
        "propagation_changes":   int(out.propagation_changes),
    }


def build_topology(
    pads: List[Tuple[int, int, int, int, str]],
    segs: List[Tuple[int, int, int, int, int, int, str]],
    polys: List[Tuple[int, List[Tuple[int, int]], int, str]],
    *,
    endpoint_tol: int,
    via_tol: int,
    same_net_pad_tol: int,
    pad_to_trace_tol: int,
    zero_is_real_net: bool,
):
    """Run the C topology builder and return its results.

    pads:  list of (x, y, net_id, pad_id, layer)
    segs:  list of (x1, y1, x2, y2, net_id, seg_id, layer)
    polys: list of (poly_id, [(vx, vy), ...], net_id, layer)

    All `pad_id` / `seg_id` / `poly_id` values must form contiguous
    ranges 0..N-1 (the C side uses them as direct array indices).
    `layer` is the string "TOP" or "BOTTOM".

    Returns a dict with numpy arrays:
      node_x, node_y, node_layer, node_net   (length = node_count)
      uf_parent, uf_rank, uf_size            (length = node_count)
      pad_node, seg_node_a, seg_node_b       (one per record)
      poly_nodes_data, poly_nodes_off        (flat + offsets)
      seg_net, poly_net                       (back-filled net ids)
      node_count, via_count, snp_count, ptt_count,
      propagation_conflicts, propagation_changes (scalars)

    Returns None if the DLL is unavailable.
    """
    lib = _load()
    if lib is None:
        return None

    n_pads = len(pads)
    n_segs = len(segs)
    n_polys = len(polys)

    # Pack inputs into numpy structured arrays. Earlier this loop did
    # per-element `pad_arr[i]['x'] = x` assignments (47 K × 5 numpy
    # __setitem__ calls), which dominated the build wrapper at ~0.36 s.
    # Switching to column-wise list assignment (numpy converts a Python
    # list to its own dtype in C) drops that to ~10 ms.

    # ---- pads (5 columns) ---------------------------------------------
    pad_arr = np.empty(max(n_pads, 1), dtype=_BUILD_PAD_DTYPE)
    if n_pads:
        pad_arr["x"][:n_pads]      = [p[0] for p in pads]
        pad_arr["y"][:n_pads]      = [p[1] for p in pads]
        pad_arr["net_id"][:n_pads] = [p[2] for p in pads]
        pad_arr["pad_id"][:n_pads] = [p[3] for p in pads]
        pad_arr["layer"][:n_pads]  = [0 if p[4] == "TOP" else 1 for p in pads]

    # ---- segments (7 columns) -----------------------------------------
    seg_arr = np.empty(max(n_segs, 1), dtype=_BUILD_SEG_DTYPE)
    if n_segs:
        seg_arr["x1"][:n_segs]     = [s[0] for s in segs]
        seg_arr["y1"][:n_segs]     = [s[1] for s in segs]
        seg_arr["x2"][:n_segs]     = [s[2] for s in segs]
        seg_arr["y2"][:n_segs]     = [s[3] for s in segs]
        seg_arr["net_id"][:n_segs] = [s[4] for s in segs]
        seg_arr["seg_id"][:n_segs] = [s[5] for s in segs]
        seg_arr["layer"][:n_segs]  = [0 if s[6] == "TOP" else 1 for s in segs]

    # ---- polylines: metadata + flat vertex array ----------------------
    total_verts = sum(len(p[1]) for p in polys)
    poly_arr = np.empty(max(n_polys, 1), dtype=_BUILD_POLY_DTYPE)
    poly_verts = np.empty(max(total_verts, 1) * 2, dtype=np.int32)
    if n_polys:
        # We need verts_offset cumulatively. Build counts list once,
        # cumsum gives the offsets.
        counts = np.fromiter((len(p[1]) for p in polys),
                              dtype=np.uint32, count=n_polys)
        offsets = np.empty(n_polys, dtype=np.uint32)
        offsets[0] = 0
        if n_polys > 1:
            np.cumsum(counts[:-1], out=offsets[1:])

        poly_arr["poly_id"][:n_polys]      = [p[0] for p in polys]
        poly_arr["verts_offset"][:n_polys] = offsets
        poly_arr["verts_count"][:n_polys]  = counts
        poly_arr["net_id"][:n_polys]       = [p[2] for p in polys]
        poly_arr["layer"][:n_polys]        = [0 if p[3] == "TOP" else 1
                                               for p in polys]

        # Vertex flattening. List comprehension over a flat output is
        # faster than nested per-vertex assignment because numpy's
        # __setitem__ from a Python list converts in one C call.
        flat = []
        for _, verts, _, _ in polys:
            for vx, vy in verts:
                flat.append(vx)
                flat.append(vy)
        poly_verts[:len(flat)] = flat

    # Output buffers. Conservative cap: every pad + 2*seg + every vert
    # could be a unique node. Real boards have lots of dedup so the
    # actual node count is far smaller — we trim afterwards.
    node_cap = max(n_pads + 2 * n_segs + total_verts, 1)
    node_x = np.zeros(node_cap, dtype=np.int32)
    node_y = np.zeros(node_cap, dtype=np.int32)
    node_layer = np.zeros(node_cap, dtype=np.uint8)
    node_net = np.zeros(node_cap, dtype=np.int32)
    uf_parent = np.zeros(node_cap, dtype=np.int32)
    uf_rank = np.zeros(node_cap, dtype=np.int32)
    uf_size = np.zeros(node_cap, dtype=np.int32)
    pad_node = np.zeros(max(n_pads, 1), dtype=np.int32)
    seg_node_a = np.zeros(max(n_segs, 1), dtype=np.int32)
    seg_node_b = np.zeros(max(n_segs, 1), dtype=np.int32)
    poly_nodes_data = np.zeros(max(total_verts, 1), dtype=np.int32)
    poly_nodes_off = np.zeros(max(n_polys + 1, 1), dtype=np.uint32)
    seg_net_out = np.zeros(max(n_segs, 1), dtype=np.int32)
    poly_net_out = np.zeros(max(n_polys, 1), dtype=np.int32)

    return _call_build_topology(
        pad_arr, n_pads, seg_arr, n_segs,
        poly_arr, n_polys, poly_verts, total_verts,
        endpoint_tol, via_tol, same_net_pad_tol, pad_to_trace_tol,
        zero_is_real_net,
    )


__all__ = [
    "available",
    "find_pad_runs",
    "scan_pads_stride_aware",
    "find_net_table",
    "find_polyline_blocks",
    "find_tagged_polylines_in_gap",
    "find_segments_in_gap",
    "find_polyline_chains_in_gap",
    "build_topology",
    "build_topology_arrays",
]
