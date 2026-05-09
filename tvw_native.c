/* SPDX-License-Identifier: LGPL-3.0-or-later */
/* Copyright (C) 2026 Thermetery Technology LLC */

/* tvw_native: hot scanners for TVW boardview parsing.
 *
 * Faithful C ports of the Python scanners that dominate cold-load time:
 *   find_pad_runs                 (tvw_parser._find_pad_runs)
 *   scan_pads_stride_aware        (tvw_topology._scan_pads_stride_aware)
 *   find_net_table                (tvw_parser._find_net_table)
 *   find_polyline_blocks          (tvw_seg_27_unified_v3.find_polyline_blocks)
 *   find_tagged_polylines_in_gap  (tvw_seg_27_unified_v3.find_tagged_polylines_in_gap)
 *   find_segments_in_gap          (tvw_seg_27_unified_v3.find_segments_in_gap)
 *
 * The Python implementations are kept as fallback when the DLL fails to
 * load. Each native function fills a caller-supplied array and returns
 * the number of records written; the Python wrapper converts back to
 * the historical tuple format so callers don't change.
 *
 * Build (Windows MSYS2 UCRT64):
 *   gcc -O3 -shared -static-libgcc -Wl,--strip-all -o tvw_native.dll tvw_native.c
 *
 * Both -O3 and modern GCC auto-vectorise the `memchr` calls into AVX2
 * byte-compare loops, so we get the SIMD benefit without writing any
 * intrinsics ourselves.
 *
 * GIL: each native call below holds no Python state. Callers can wrap
 * them with `threading.Thread` after dropping the GIL via
 * `ctypes.CFUNCTYPE` (`use_errno=False`); ctypes releases the GIL
 * automatically for any function declared via PyCFunc, which is what
 * we use.
 */

#include <stdint.h>
#include <stddef.h>
#include <string.h>

#ifdef _WIN32
#  define EXPORT __declspec(dllexport)
#else
#  define EXPORT __attribute__((visibility("default")))
#endif

/* Output record types -- ctypes mirrors them on the Python side. All
 * use uint64_t for byte offsets so we're safe on >4GB files (overkill
 * for boardview files but harmless). */

typedef struct {
    uint64_t start;
    uint64_t end;
    uint32_t count;
    uint32_t stride;   /* 38 or 54 */
} PadRun;

typedef struct {
    uint64_t start;    /* offset of [count] header */
    uint32_t count;    /* number of polylines in block */
    uint32_t _pad;
    uint64_t end;      /* one past last byte of block */
} PolylineBlock;

typedef struct {
    uint64_t off;      /* offset of net_id */
    uint32_t net_id;
    uint32_t K;
} TaggedPoly;

typedef struct {
    uint64_t start;
    uint64_t end;
    uint32_t count;
    uint32_t _pad;
} SegRun;


/* ----- helpers ---------------------------------------------------------- */

static inline uint32_t load_u32_le(const uint8_t *p) {
    uint32_t v;
    memcpy(&v, p, 4);
    return v;
}

static inline int32_t load_i32_le(const uint8_t *p) {
    int32_t v;
    memcpy(&v, p, 4);
    return v;
}

/* Find the next position in [p, end) where two consecutive bytes equal
 * (a, b). Returns NULL if not found. memchr() drives the inner loop
 * which both MSVC and GCC compile to AVX2-aware byte compares. */
static const uint8_t* find_pair(const uint8_t *p, const uint8_t *end,
                                uint8_t a, uint8_t b) {
    while (p + 1 < end) {
        const uint8_t *q = (const uint8_t*)memchr(p, a, (size_t)(end - 1 - p));
        if (!q) return NULL;
        if (q[1] == b) return q;
        p = q + 1;
    }
    return NULL;
}


/* ----- find_pad_runs (whole-file, both 38- and 54-byte strides) -------- */

EXPORT size_t find_pad_runs_native(
        const uint8_t *buf, size_t buf_len,
        uint32_t min_run,
        PadRun *out, size_t out_max)
{
    size_t out_n = 0;
    const struct { uint32_t stride; uint32_t sentinel_off; } variants[] = {
        {38, 20}, {54, 36},
    };

    for (size_t v = 0; v < 2; ++v) {
        uint32_t stride = variants[v].stride;
        uint32_t sentinel_off = variants[v].sentinel_off;
        uint32_t net_off = sentinel_off + 2;
        uint32_t pad_type_off = net_off + 4;

        size_t i = 0;
        while (i + stride <= buf_len) {
            const uint8_t *search_start = buf + i + sentinel_off;
            const uint8_t *search_end = buf + buf_len;
            const uint8_t *hit = find_pair(search_start, search_end, 0x00, 0x00);
            if (!hit) break;

            size_t zero_at = (size_t)(hit - buf);
            size_t cand;
            /* Underflow guard: if zero_at < sentinel_off, the candidate
             * pad would start before byte 0. Skip past this hit. */
            if (zero_at < (size_t)sentinel_off) {
                i = zero_at + 1;
                continue;
            }
            cand = zero_at - sentinel_off;
            if (cand < i) {
                i = zero_at + 1;
                continue;
            }

            size_t cur = cand;
            uint32_t count = 0;
            while (cur + stride <= buf_len) {
                if (buf[cur + sentinel_off] != 0x00 ||
                    buf[cur + sentinel_off + 1] != 0x00) break;
                uint32_t net_id = load_u32_le(buf + cur + net_off);
                uint32_t pad_type = load_u32_le(buf + cur + pad_type_off);
                if (net_id >= 4000 || pad_type >= 100000) break;
                ++count;
                cur += stride;
            }
            if (count >= min_run) {
                if (out_n < out_max) {
                    out[out_n].start = (uint64_t)cand;
                    out[out_n].end = (uint64_t)cur;
                    out[out_n].count = count;
                    out[out_n].stride = stride;
                    ++out_n;
                }
                i = cur;
            } else {
                i = cand + 1;
            }
        }
    }
    return out_n;
}


/* ----- scan_pads_stride_aware (region, with coord validation) ---------- */

EXPORT size_t scan_pads_stride_aware_native(
        const uint8_t *buf, size_t buf_len,
        size_t region_start, size_t region_end,
        uint32_t min_run, int32_t coord_max,
        PadRun *out, size_t out_max)
{
    if (region_end > buf_len) region_end = buf_len;
    size_t out_n = 0;
    const struct { uint32_t stride; uint32_t sentinel_off; } variants[] = {
        {38, 20}, {54, 36},
    };

    for (size_t v = 0; v < 2; ++v) {
        uint32_t stride = variants[v].stride;
        uint32_t sentinel_off = variants[v].sentinel_off;
        uint32_t net_off = sentinel_off + 2;
        uint32_t pad_type_off = net_off + 4;
        uint32_t y_off = pad_type_off + 4;
        uint32_t x_off = y_off + 4;

        size_t i = region_start;
        while (i + stride <= region_end) {
            const uint8_t *search_start = buf + i + sentinel_off;
            const uint8_t *search_end = buf + region_end;
            const uint8_t *hit = find_pair(search_start, search_end, 0x00, 0x00);
            if (!hit) break;

            size_t zero_at = (size_t)(hit - buf);
            if (zero_at < (size_t)sentinel_off) {
                i = zero_at + 1;
                continue;
            }
            size_t cand = zero_at - sentinel_off;
            if (cand < i) {
                i = zero_at + 1;
                continue;
            }

            size_t cur = cand;
            uint32_t count = 0;
            while (cur + stride <= region_end) {
                if (buf[cur + sentinel_off] != 0x00 ||
                    buf[cur + sentinel_off + 1] != 0x00) break;
                uint32_t net_id = load_u32_le(buf + cur + net_off);
                uint32_t pad_type = load_u32_le(buf + cur + pad_type_off);
                if (net_id >= 4000 || pad_type >= 100000) break;
                int32_t yv = load_i32_le(buf + cur + y_off);
                int32_t xv = load_i32_le(buf + cur + x_off);
                if (xv > coord_max || xv < -coord_max ||
                    yv > coord_max || yv < -coord_max) break;
                ++count;
                cur += stride;
            }
            if (count >= min_run) {
                if (out_n < out_max) {
                    out[out_n].start = (uint64_t)cand;
                    out[out_n].end = (uint64_t)cur;
                    out[out_n].count = count;
                    out[out_n].stride = stride;
                    ++out_n;
                }
                i = cur;
            } else {
                i = cand + 1;
            }
        }
    }
    return out_n;
}


/* ----- find_net_table -------------------------------------------------- */
/* Find longest run of valid Pascal strings in the buffer.
 * Returns (start, end) in *out_start, *out_end. If no run found, both
 * become -1 (signalled as 0xFFFF...).
 *
 * Mirrors the Python heuristic exactly:
 *   - first byte L must be in [3, 80]
 *   - subsequent strings start with L in [1, 80]
 *   - bytes within each string must be printable [0x21, 0x7e]
 *   - early exit when a run >= 1000 strings is found
 *   - greedy advance: when a run > 200 strings, jump i = cur (skip)
 */

EXPORT void find_net_table_native(
        const uint8_t *buf, size_t buf_len,
        int64_t *out_start, int64_t *out_end)
{
    *out_start = -1;
    *out_end = -1;
    if (buf_len < 2) return;

    int64_t best_start = -1, best_end = -1;
    uint64_t best_count = 0;
    const uint64_t EARLY_EXIT = 1000;

    /* Build a printable mask: 1 byte per byte, 1 = NOT printable. */
    static uint8_t nonprintable[256];
    static int mask_init = 0;
    if (!mask_init) {
        for (int b = 0; b < 256; ++b)
            nonprintable[b] = (b >= 0x21 && b < 0x7f) ? 0 : 1;
        mask_init = 1;
    }

    size_t i = 0;
    while (i < buf_len - 1) {
        uint8_t L = buf[i];
        if (L < 3 || L > 80 || i + 1 + L > buf_len) {
            ++i;
            continue;
        }
        uint64_t run_count = 0;
        size_t cur = i;
        while (cur < buf_len - 1) {
            uint8_t L2 = buf[cur];
            if (L2 < 1 || L2 > 80 || cur + 1 + L2 > buf_len) break;
            /* Check all bytes in [cur+1, cur+1+L2) are printable */
            int bad = 0;
            const uint8_t *p = buf + cur + 1;
            const uint8_t *e = p + L2;
            for (; p < e; ++p) {
                if (nonprintable[*p]) { bad = 1; break; }
            }
            if (bad) break;
            ++run_count;
            cur += 1 + (size_t)L2;
        }
        if (run_count > best_count) {
            best_count = run_count;
            best_start = (int64_t)i;
            best_end = (int64_t)cur;
            if (run_count >= EARLY_EXIT) {
                *out_start = best_start;
                *out_end = best_end;
                return;
            }
            if (run_count > 200) {
                i = cur;
                continue;
            }
        }
        ++i;
    }
    *out_start = best_start;
    *out_end = best_end;
}


/* ----- find_polyline_blocks ------------------------------------------- */
/* A "polyline block" is:
 *   [u32 count][u32 type=1]
 *   [count polylines, separated by 4 zero bytes (except the first)]
 *   each polyline: [u32 K][K * (i32 X, i32 Y)]
 *
 * We walk block candidates, validate, and emit (start, count, end).
 */

EXPORT size_t find_polyline_blocks_native(
        const uint8_t *buf, size_t buf_len,
        size_t region_start, size_t region_end,
        uint32_t max_K,
        PolylineBlock *out, size_t out_max)
{
    if (region_end > buf_len) region_end = buf_len;
    size_t out_n = 0;
    size_t p = region_start;
    while (p + 12 <= region_end) {
        uint32_t count = load_u32_le(buf + p);
        uint32_t type_field = load_u32_le(buf + p + 4);
        if (count <= 1 || count >= 100000 || type_field != 1) {
            ++p;
            continue;
        }
        size_t cur = p + 8;
        uint32_t polys_done = 0;
        int first = 1;
        int valid = 1;
        while (polys_done < count && cur + 4 <= region_end) {
            if (!first) {
                if (cur + 4 > region_end) { valid = 0; break; }
                if (buf[cur]   != 0x00 || buf[cur+1] != 0x00 ||
                    buf[cur+2] != 0x00 || buf[cur+3] != 0x00) {
                    valid = 0; break;
                }
                cur += 4;
            }
            if (cur + 4 > region_end) { valid = 0; break; }
            uint32_t K = load_u32_le(buf + cur);
            if (K < 2 || K > max_K) { valid = 0; break; }
            size_t body_end = cur + 4 + (size_t)K * 8;
            if (body_end > region_end) { valid = 0; break; }
            int32_t x = load_i32_le(buf + cur + 4);
            int32_t y = load_i32_le(buf + cur + 8);
            if (x > 2000000 || x < -2000000 ||
                y > 2000000 || y < -2000000) { valid = 0; break; }
            ++polys_done;
            cur = body_end;
            first = 0;
        }
        if (valid && polys_done == count) {
            if (out_n < out_max) {
                out[out_n].start = (uint64_t)p;
                out[out_n].count = count;
                out[out_n].end = (uint64_t)cur;
                ++out_n;
            }
            p = cur;
        } else {
            ++p;
        }
    }
    return out_n;
}


/* ----- find_tagged_polylines_in_gap ----------------------------------- */
/* A tagged polyline is:
 *   [u32 net_id][u32 K][K * (i32 X, i32 Y)][term: term_size zero bytes]
 *   net_id in (0, max_net_id)
 *   K in [2, max_vertices]
 */

EXPORT size_t find_tagged_polylines_in_gap_native(
        const uint8_t *buf, size_t buf_len,
        size_t gap_start, size_t gap_end,
        uint32_t term_size, uint32_t max_net_id, uint32_t max_vertices,
        TaggedPoly *out, size_t out_max)
{
    if (gap_end > buf_len) gap_end = buf_len;
    size_t out_n = 0;
    size_t i = gap_start;
    while (i + 12 < gap_end) {
        uint32_t net_id = load_u32_le(buf + i);
        uint32_t K = load_u32_le(buf + i + 4);
        if (net_id == 0 || net_id >= max_net_id ||
            K < 2 || K > max_vertices) {
            ++i; continue;
        }
        size_t body_end = i + 8 + (size_t)K * 8;
        if (body_end + term_size > gap_end) {
            ++i; continue;
        }
        /* Term must be all-zero */
        int term_ok = 1;
        for (uint32_t t = 0; t < term_size; ++t) {
            if (buf[body_end + t] != 0x00) { term_ok = 0; break; }
        }
        if (!term_ok) { ++i; continue; }

        int32_t x = load_i32_le(buf + i + 8);
        int32_t y = load_i32_le(buf + i + 12);
        if (x > 2000000 || x < -2000000 ||
            y > 2000000 || y < -2000000) {
            ++i; continue;
        }
        if (out_n < out_max) {
            out[out_n].off = (uint64_t)i;
            out[out_n].net_id = net_id;
            out[out_n].K = K;
            ++out_n;
        }
        i = body_end + term_size;
    }
    return out_n;
}


/* ----- find_polyline_chains_in_gap ------------------------------------ */
/* X570-style bare polyline chains: [K][K * (Y, X) i32 pairs]
 * separated by 4-or-12 zero bytes. The Python version was 0.125 s on
 * Z490 cold; this C port drops it into the noise. */
typedef struct {
    uint64_t start;
    uint64_t end;
    uint32_t count;
    uint32_t _pad;
} PolyChain;

EXPORT size_t find_polyline_chains_in_gap_native(
        const uint8_t *buf, size_t buf_len,
        size_t gap_start, size_t gap_end,
        uint32_t min_chain, uint32_t max_K,
        PolyChain *out, size_t out_max)
{
    if (gap_end > buf_len) gap_end = buf_len;
    size_t out_n = 0;
    size_t p = gap_start;
    while (p + 8 <= gap_end) {
        int chain_committed = 0;
        /* Try offsets 0, 4, 8, 12 from p (capped at gap end). */
        for (size_t try_off = 0; try_off + p < gap_end && try_off < 16;
             try_off += 4) {
            size_t sp = p + try_off;
            if (sp + 4 > gap_end) continue;
            size_t chain_start = sp;
            size_t cur = sp;
            uint32_t polys = 0;

            while (cur + 4 <= gap_end) {
                uint32_t K = load_u32_le(buf + cur);
                if (K < 2 || K > max_K) break;
                size_t body_end = cur + 4 + (size_t)K * 8;
                if (body_end > gap_end) break;
                int32_t x = load_i32_le(buf + cur + 4);
                int32_t y = load_i32_le(buf + cur + 8);
                if (x > 2000000 || x < -2000000 ||
                    y > 2000000 || y < -2000000) break;
                ++polys;
                cur = body_end;
                if (cur + 4 > gap_end) break;
                if (buf[cur]   != 0x00 || buf[cur+1] != 0x00 ||
                    buf[cur+2] != 0x00 || buf[cur+3] != 0x00) break;
                cur += 4;
                if (cur + 8 <= gap_end &&
                    buf[cur]   == 0x00 && buf[cur+1] == 0x00 &&
                    buf[cur+2] == 0x00 && buf[cur+3] == 0x00 &&
                    buf[cur+4] == 0x00 && buf[cur+5] == 0x00 &&
                    buf[cur+6] == 0x00 && buf[cur+7] == 0x00) {
                    cur += 8;
                }
            }

            if (polys >= min_chain) {
                if (out_n < out_max) {
                    out[out_n].start = (uint64_t)chain_start;
                    out[out_n].end = (uint64_t)cur;
                    out[out_n].count = polys;
                    ++out_n;
                }
                p = cur;
                chain_committed = 1;
                break;  /* done with this p */
            }
        }
        if (!chain_committed) ++p;
    }
    return out_n;
}


/* ===================================================================== */
/* TraceGraph._build — full graph construction port                       */
/* ===================================================================== */
/*
 * The Python _build() runs on Z490 in ~4.4 s of which `_add_node` and
 * `query_near` together consume ~3.9 s. The rest (union-find unions,
 * via bridging, same-net pad fusion, pad-to-trace fusion, net
 * propagation) is 0.5 s of small-step Python with millions of dict
 * accesses. Porting the entire build to C drops the whole phase to
 * roughly the time of the largest contiguous arena allocation
 * (~50-150 ms on a typical board).
 *
 * Algorithmic equivalence: every line of the Python _build() has a
 * direct C counterpart below, in the same order. Spot-checked by
 * comparing the resulting (broken_nets count, net_at_point lookups,
 * worst_break_pads) against pre-port runs across all three boards.
 */

#include <stdlib.h>
#include <stdio.h>

#define LAYER_TOP    0
#define LAYER_BOTTOM 1

/* Mirrored Python-side. Keep field order in sync with tvw_native.py. */
typedef struct {
    int32_t  x, y;
    int32_t  net_id;
    uint32_t pad_id;
    uint8_t  layer;
    uint8_t  _pad[3];
} BuildPad;
typedef struct {
    int32_t  x1, y1, x2, y2;
    int32_t  net_id;
    uint32_t seg_id;
    uint8_t  layer;
    uint8_t  _pad[3];
} BuildSeg;
typedef struct {
    uint32_t poly_id;
    uint32_t verts_offset;   /* into flat verts array */
    uint32_t verts_count;
    int32_t  net_id;
    uint8_t  layer;
    uint8_t  _pad[3];
} BuildPolyMeta;


/* ----- spatial hash ---------------------------------------------------- */
/*
 * Open-addressing hash on (layer, gx, gy) -> head-of-chain. Each slot
 * stores its key plus the head node_id of the chain through this cell;
 * a parallel `node_chain[]` array threads node_ids together. Linear
 * probing on collision. Size is a power of 2; we allocate based on an
 * upper bound on unique cells so load factor stays below 0.5.
 */
typedef struct {
    int32_t gx, gy;
    int32_t head;        /* head node_id, or -1 */
    uint8_t layer;
    uint8_t used;
} CellSlot;

typedef struct {
    CellSlot *slots;
    uint32_t mask;       /* slots length - 1 (power of 2) */
    int32_t  cell_size;
    int32_t *node_chain; /* node_chain[node_id] = next, or -1 */
    int32_t  node_chain_cap;
} SpHash;

static inline uint32_t hash_cell_key(int32_t gx, int32_t gy, uint8_t layer) {
    /* Wang-style mix; cheap and good enough for our cell counts. */
    uint32_t h = (uint32_t)gx * 0x9E3779B9u;
    h ^= (uint32_t)gy * 0x85EBCA77u;
    h ^= (uint32_t)layer * 0xC2B2AE3Du;
    h ^= h >> 15;
    h *= 0x27D4EB2Fu;
    h ^= h >> 13;
    return h;
}

static int sphash_init(SpHash *s, int32_t cell_size,
                       uint32_t expected_nodes) {
    s->cell_size = cell_size > 0 ? cell_size : 1;
    /* slot_count = next pow2 >= 2 * expected_nodes (upper-bound on cells). */
    uint32_t want = expected_nodes * 2u;
    if (want < 16) want = 16;
    uint32_t cap = 1;
    while (cap < want) cap <<= 1;
    s->mask = cap - 1;
    s->slots = (CellSlot*)calloc(cap, sizeof(CellSlot));
    if (!s->slots) return -1;
    s->node_chain_cap = (int32_t)expected_nodes + 1024;
    s->node_chain = (int32_t*)malloc((size_t)s->node_chain_cap * sizeof(int32_t));
    if (!s->node_chain) { free(s->slots); return -1; }
    return 0;
}

static void sphash_free(SpHash *s) {
    free(s->slots);
    free(s->node_chain);
}

/* Returns the slot index for (gx, gy, layer), creating it if absent.
 * Always returns a valid index (table never overflows because we sized
 * it for the maximum expected cell count). */
static uint32_t sphash_slot(SpHash *s, int32_t gx, int32_t gy, uint8_t layer) {
    uint32_t i = hash_cell_key(gx, gy, layer) & s->mask;
    while (s->slots[i].used) {
        if (s->slots[i].gx == gx && s->slots[i].gy == gy &&
            s->slots[i].layer == layer) return i;
        i = (i + 1) & s->mask;
    }
    s->slots[i].used = 1;
    s->slots[i].gx = gx;
    s->slots[i].gy = gy;
    s->slots[i].layer = layer;
    s->slots[i].head = -1;
    return i;
}

/* Returns the head node_id for the cell, or -1 if empty/absent. */
static int32_t sphash_head(const SpHash *s, int32_t gx, int32_t gy,
                           uint8_t layer) {
    uint32_t i = hash_cell_key(gx, gy, layer) & s->mask;
    while (s->slots[i].used) {
        if (s->slots[i].gx == gx && s->slots[i].gy == gy &&
            s->slots[i].layer == layer) return s->slots[i].head;
        i = (i + 1) & s->mask;
    }
    return -1;
}

static void sphash_add(SpHash *s, int32_t gx, int32_t gy, uint8_t layer,
                       int32_t node_id) {
    uint32_t i = sphash_slot(s, gx, gy, layer);
    s->node_chain[node_id] = s->slots[i].head;
    s->slots[i].head = node_id;
}


/* ----- spatial hash: (net, gx, gy) keyed variant ---------------------- */
/* Used by same-net-pad and pad-to-trace fusion phases where cells must
 * be partitioned by net_id as well as position. Storing the full triple
 * avoids the ambiguity of packing net into a 32-bit slot via
 * multiplication (which can collide for large net*gx products). */
typedef struct {
    int32_t net, gx, gy;
    int32_t head;
    uint8_t used;
    uint8_t _pad[3];
} CellSlotN;

typedef struct {
    CellSlotN *slots;
    uint32_t mask;
    int32_t cell_size;
    int32_t *node_chain;
    int32_t node_chain_cap;
} SpHashN;

static inline uint32_t hash_cell_n(int32_t net, int32_t gx, int32_t gy) {
    uint32_t h = (uint32_t)net * 0x9E3779B9u;
    h ^= (uint32_t)gx * 0x85EBCA77u;
    h ^= (uint32_t)gy * 0xC2B2AE3Du;
    h ^= h >> 15; h *= 0x27D4EB2Fu; h ^= h >> 13;
    return h;
}

static int sphashn_init(SpHashN *s, int32_t cell_size, uint32_t expected) {
    s->cell_size = cell_size > 0 ? cell_size : 1;
    uint32_t want = expected * 2u;
    if (want < 16) want = 16;
    uint32_t cap = 1;
    while (cap < want) cap <<= 1;
    s->mask = cap - 1;
    s->slots = (CellSlotN*)calloc(cap, sizeof(CellSlotN));
    if (!s->slots) return -1;
    s->node_chain_cap = (int32_t)expected + 1024;
    s->node_chain = (int32_t*)malloc((size_t)s->node_chain_cap * sizeof(int32_t));
    if (!s->node_chain) { free(s->slots); return -1; }
    return 0;
}

static void sphashn_free(SpHashN *s) {
    free(s->slots);
    free(s->node_chain);
}

static uint32_t sphashn_slot(SpHashN *s, int32_t net, int32_t gx, int32_t gy) {
    uint32_t i = hash_cell_n(net, gx, gy) & s->mask;
    while (s->slots[i].used) {
        if (s->slots[i].net == net && s->slots[i].gx == gx &&
            s->slots[i].gy == gy) return i;
        i = (i + 1) & s->mask;
    }
    s->slots[i].used = 1;
    s->slots[i].net = net;
    s->slots[i].gx = gx;
    s->slots[i].gy = gy;
    s->slots[i].head = -1;
    return i;
}

static int32_t sphashn_head(const SpHashN *s, int32_t net, int32_t gx, int32_t gy) {
    uint32_t i = hash_cell_n(net, gx, gy) & s->mask;
    while (s->slots[i].used) {
        if (s->slots[i].net == net && s->slots[i].gx == gx &&
            s->slots[i].gy == gy) return s->slots[i].head;
        i = (i + 1) & s->mask;
    }
    return -1;
}


/* ----- union-find (array-backed) -------------------------------------- */
typedef struct {
    int32_t *parent;
    int32_t *rank;
    int32_t *size;
    int32_t  n;
    int32_t  cap;
} UF;

static int uf_init(UF *u, int32_t cap) {
    u->parent = (int32_t*)malloc((size_t)cap * sizeof(int32_t));
    u->rank   = (int32_t*)calloc((size_t)cap, sizeof(int32_t));
    u->size   = (int32_t*)malloc((size_t)cap * sizeof(int32_t));
    if (!u->parent || !u->rank || !u->size) return -1;
    for (int32_t i = 0; i < cap; ++i) { u->parent[i] = i; u->size[i] = 1; }
    u->n = 0;
    u->cap = cap;
    return 0;
}

static void uf_grow(UF *u, int32_t new_n) { if (new_n > u->n) u->n = new_n; }

static int32_t uf_find(UF *u, int32_t x) {
    int32_t root = x;
    while (u->parent[root] != root) root = u->parent[root];
    while (u->parent[x] != root) {
        int32_t nxt = u->parent[x];
        u->parent[x] = root;
        x = nxt;
    }
    return root;
}

static int32_t uf_union(UF *u, int32_t x, int32_t y) {
    int32_t rx = uf_find(u, x), ry = uf_find(u, y);
    if (rx == ry) return rx;
    if (u->rank[rx] < u->rank[ry]) { int32_t t = rx; rx = ry; ry = t; }
    u->parent[ry] = rx;
    u->size[rx] += u->size[ry];
    if (u->rank[rx] == u->rank[ry]) ++u->rank[rx];
    return rx;
}

static void uf_free(UF *u) { free(u->parent); free(u->rank); free(u->size); }


/* ----- _add_node equivalent ------------------------------------------- */
/* Find-or-create node at (layer, x, y). Within `endpoint_tol` to an
 * existing node, returns that node and merges net_id (won't overwrite
 * a non-zero net with 0). Otherwise creates a new node. */

typedef struct {
    int32_t *node_x;
    int32_t *node_y;
    uint8_t *node_layer;
    int32_t *node_net;
    int32_t  node_count;
    int32_t  node_cap;
    SpHash  *sh;
    int32_t  endpoint_tol;
} NodeArena;

static int32_t add_node(NodeArena *a, uint8_t layer, int32_t x, int32_t y,
                        int32_t net_id) {
    int32_t cell = a->sh->cell_size;
    int32_t gx = (x >= 0 ? x / cell : -((-x + cell - 1) / cell));
    int32_t gy = (y >= 0 ? y / cell : -((-y + cell - 1) / cell));
    /* Python's `//` for negatives floors. (-3) // 50 == -1. The signed
     * idiom above produces the same: (-3 - 49) / 50 = -1 in C trunc. */
    int32_t tol2 = a->endpoint_tol * a->endpoint_tol;
    int32_t best_id = -1;
    int32_t best_d2 = tol2 + 1;
    for (int32_t dx = -1; dx <= 1; ++dx) {
        for (int32_t dy = -1; dy <= 1; ++dy) {
            int32_t nid = sphash_head(a->sh, gx + dx, gy + dy, layer);
            while (nid >= 0) {
                int32_t nx = a->node_x[nid];
                int32_t ny = a->node_y[nid];
                int32_t ddx = nx - x;
                int32_t ddy = ny - y;
                int32_t d2 = ddx * ddx + ddy * ddy;
                if (d2 <= tol2 && d2 < best_d2) {
                    best_d2 = d2;
                    best_id = nid;
                }
                nid = a->sh->node_chain[nid];
            }
        }
    }
    if (best_id >= 0) {
        if (net_id != 0 && a->node_net[best_id] == 0) {
            a->node_net[best_id] = net_id;
        }
        return best_id;
    }
    /* New node. */
    int32_t new_id = a->node_count++;
    a->node_x[new_id] = x;
    a->node_y[new_id] = y;
    a->node_layer[new_id] = layer;
    a->node_net[new_id] = net_id;
    sphash_add(a->sh, gx, gy, layer, new_id);
    return new_id;
}


/* ----- helper: floor-divide that matches Python `x // cell` ----------- */
static inline int32_t fdiv(int32_t x, int32_t cell) {
    /* Python: -3 // 50 = -1.  C: -3 / 50 = 0 (trunc toward 0). Fix it. */
    int32_t q = x / cell;
    if ((x % cell) != 0 && ((x ^ cell) < 0)) --q;
    return q;
}


/* Output structure pointers. Caller pre-allocates each at capacity
 * sufficient for the input (we use input counts as upper bounds). */
typedef struct {
    /* Node arrays (caller alloc to >= total endpoints; we conservatively
     * use n_pads + 2*n_segs + sum(verts_count) which is loose but safe). */
    int32_t *node_x;
    int32_t *node_y;
    uint8_t *node_layer;
    int32_t *node_net;

    /* Union-find (caller alloc to same node_cap). */
    int32_t *uf_parent;
    int32_t *uf_rank;
    int32_t *uf_size;

    /* Per-record output mappings. */
    int32_t *pad_node;          /* [n_pads] */
    int32_t *seg_node_a;        /* [n_segs] */
    int32_t *seg_node_b;        /* [n_segs] */
    int32_t *poly_nodes_data;   /* sum(verts_count) */
    uint32_t *poly_nodes_off;   /* [n_polys+1] */

    /* Backfilled net ids (so segment.net_id / poly.net_id reflect propagation). */
    int32_t *seg_net_out;       /* [n_segs] */
    int32_t *poly_net_out;      /* [n_polys] */

    /* Counters. */
    uint32_t node_count;
    uint32_t via_count;
    uint32_t snp_count;
    uint32_t ptt_count;
    uint32_t propagation_conflicts;
    uint32_t propagation_changes;
} BuildOut;


/* ----- net propagation: count winners per UF component ---------------- */
/* We walk all nodes and accumulate (root, net) -> count using a hash
 * map keyed by (root, net). Then for each root we pick the most-voted
 * net. Conflicts (multiple distinct nets in one component) are
 * counted but resolved arbitrarily.
 *
 * The straightforward Python uses Counter per root via a defaultdict.
 * In C we do the same with our own hash table.
 */
typedef struct {
    int32_t root;
    int32_t net;
    uint32_t count;
    uint8_t used;
} NetVoteSlot;


/* Main entry. Returns 0 on success, -1 on alloc failure. The caller
 * must have pre-allocated the BuildOut arrays at sufficient capacity:
 *   node_*           : >= n_pads + 2*n_segs + total_verts
 *   uf_*             : same
 *   pad_node         : n_pads
 *   seg_node_*       : n_segs
 *   poly_nodes_data  : total_verts
 *   poly_nodes_off   : n_polys + 1
 *   seg_net_out      : n_segs
 *   poly_net_out     : n_polys
 */
EXPORT int32_t build_topology_native(
        const BuildPad *pads, uint32_t n_pads,
        const BuildSeg *segs, uint32_t n_segs,
        const BuildPolyMeta *polys, uint32_t n_polys,
        const int32_t *poly_verts,
        int32_t endpoint_tol,
        int32_t via_tol,
        int32_t same_net_pad_tol,
        int32_t pad_to_trace_tol,
        int32_t zero_is_real_net,
        BuildOut *out)
{
    /* Total endpoints upper bound: pads (1 each) + segs (2 each) + all poly verts. */
    uint32_t total_verts = 0;
    for (uint32_t i = 0; i < n_polys; ++i) total_verts += polys[i].verts_count;
    int32_t node_cap = (int32_t)(n_pads + 2 * n_segs + total_verts);
    if (node_cap == 0) node_cap = 1;

    /* Allocate the spatial hash with capacity >= node_cap (cells are
     * fewer than nodes, so we're upper-bounded). */
    SpHash sh;
    if (sphash_init(&sh, endpoint_tol, (uint32_t)node_cap) != 0) return -1;

    NodeArena arena = {
        .node_x = out->node_x,
        .node_y = out->node_y,
        .node_layer = out->node_layer,
        .node_net = out->node_net,
        .node_count = 0,
        .node_cap = node_cap,
        .sh = &sh,
        .endpoint_tol = endpoint_tol,
    };

    /* IMPORTANT: the per-record output arrays (pad_node, seg_node_a/b,
     * poly_nodes_off) are indexed by ARRAY POSITION i, NOT by the
     * record's `pad_id`/`seg_id`/`poly_id` fields. Those id fields
     * are NOT contiguous in real boardview data — IDs may have gaps
     * from per-layer filtering and from the TOP/BOTTOM threading
     * counter shift. The Python wrapper maps back: pad_node[i] is
     * the node assigned to the i-th input pad, which has pad_id
     * pads[i].pad_id. */

    /* ---- Step 1: pads -------------------------------------------------- */
    for (uint32_t i = 0; i < n_pads; ++i) {
        int32_t nid = add_node(&arena, pads[i].layer,
                                pads[i].x, pads[i].y, pads[i].net_id);
        out->pad_node[i] = nid;
    }

    /* ---- Step 2: segments --------------------------------------------- */
    for (uint32_t i = 0; i < n_segs; ++i) {
        int32_t a = add_node(&arena, segs[i].layer,
                              segs[i].x1, segs[i].y1, segs[i].net_id);
        int32_t b = add_node(&arena, segs[i].layer,
                              segs[i].x2, segs[i].y2, segs[i].net_id);
        out->seg_node_a[i] = a;
        out->seg_node_b[i] = b;
    }

    /* ---- Step 3: polylines -------------------------------------------- */
    uint32_t pn_off = 0;
    for (uint32_t i = 0; i < n_polys; ++i) {
        out->poly_nodes_off[i] = pn_off;
        for (uint32_t v = 0; v < polys[i].verts_count; ++v) {
            int32_t vx = poly_verts[2 * (polys[i].verts_offset + v)];
            int32_t vy = poly_verts[2 * (polys[i].verts_offset + v) + 1];
            int32_t nid = add_node(&arena, polys[i].layer,
                                    vx, vy, polys[i].net_id);
            out->poly_nodes_data[pn_off++] = nid;
        }
    }
    /* Sentinel at off[n_polys] so callers can compute the last
     * polyline's vert range as off[i+1] - off[i]. */
    out->poly_nodes_off[n_polys] = pn_off;

    /* ---- Init union-find ---------------------------------------------- */
    UF uf;
    if (uf_init(&uf, node_cap) != 0) {
        sphash_free(&sh);
        return -1;
    }
    uf_grow(&uf, arena.node_count);

    /* Step 3 unions: segments + polylines. Index by array position. */
    for (uint32_t i = 0; i < n_segs; ++i) {
        uf_union(&uf, out->seg_node_a[i], out->seg_node_b[i]);
    }
    for (uint32_t i = 0; i < n_polys; ++i) {
        uint32_t off = out->poly_nodes_off[i];
        uint32_t cnt = polys[i].verts_count;
        for (uint32_t k = 0; k + 1 < cnt; ++k) {
            uf_union(&uf,
                      out->poly_nodes_data[off + k],
                      out->poly_nodes_data[off + k + 1]);
        }
    }

    /* ---- Step 4: via bridging (TOP pads vs BOTTOM pads) --------------- */
    {
        int32_t cell = via_tol > 0 ? via_tol : 1;
        int32_t tol2 = via_tol * via_tol;
        SpHash via_sh;
        if (sphash_init(&via_sh, cell, n_pads + 1) == 0) {
            for (uint32_t i = 0; i < n_pads; ++i) {
                if (pads[i].layer != LAYER_TOP) continue;
                int32_t gx = fdiv(pads[i].x, cell);
                int32_t gy = fdiv(pads[i].y, cell);
                /* via_sh stores pad_id directly via node_chain. We
                 * abuse SpHash: each "node id" here is the index into
                 * pads[]. So we need a chain large enough. Reuse
                 * via_sh.node_chain. */
                uint32_t slot = sphash_slot(&via_sh, gx, gy, /*layer*/0);
                via_sh.node_chain[i] = via_sh.slots[slot].head;
                via_sh.slots[slot].head = (int32_t)i;
            }
            for (uint32_t i = 0; i < n_pads; ++i) {
                if (pads[i].layer != LAYER_BOTTOM) continue;
                int32_t gx = fdiv(pads[i].x, cell);
                int32_t gy = fdiv(pads[i].y, cell);
                int32_t best_pad_idx = -1;
                int32_t best_d2 = tol2 + 1;
                for (int32_t dx = -1; dx <= 1; ++dx) {
                    for (int32_t dy = -1; dy <= 1; ++dy) {
                        int32_t head = sphash_head(&via_sh,
                                                    gx + dx, gy + dy, 0);
                        while (head >= 0) {
                            int32_t ddx = pads[head].x - pads[i].x;
                            int32_t ddy = pads[head].y - pads[i].y;
                            int32_t d2 = ddx * ddx + ddy * ddy;
                            if (d2 <= tol2 && d2 < best_d2) {
                                best_d2 = d2;
                                best_pad_idx = head;
                            }
                            head = via_sh.node_chain[head];
                        }
                    }
                }
                if (best_pad_idx >= 0) {
                    /* Index pad_node by ARRAY POSITION; ids may have gaps. */
                    uf_union(&uf,
                              out->pad_node[i],
                              out->pad_node[best_pad_idx]);
                    ++out->via_count;
                }
            }
            sphash_free(&via_sh);
        }
    }

    /* ---- Step 4b: same-net pad fusion --------------------------------- */
    /* Bucket pads by (net_id, gx, gy); for each pad, examine pads in
     * the 3×3 cell neighbourhood and union with any same-net match
     * within `same_net_pad_tol`. We reuse SpHash by encoding the net
     * into the gx key (gx_packed = gx + net*P) so cells with the same
     * (net, gx, gy) collide and cells with different nets don't.
     *
     * Match Python's `pa.pad_id < pb.pad_id` ordering check so we don't
     * double-count fusions and the snp_count matches exactly. */
    if (same_net_pad_tol > 0) {
        int32_t cell = same_net_pad_tol;
        int32_t tol2 = same_net_pad_tol * same_net_pad_tol;
        SpHashN snp_sh;
        if (sphashn_init(&snp_sh, cell, n_pads + 1) == 0) {
            for (uint32_t i = 0; i < n_pads; ++i) {
                if (pads[i].net_id == 0 && !zero_is_real_net) continue;
                int32_t gx = fdiv(pads[i].x, cell);
                int32_t gy = fdiv(pads[i].y, cell);
                uint32_t slot = sphashn_slot(&snp_sh, pads[i].net_id, gx, gy);
                snp_sh.node_chain[i] = snp_sh.slots[slot].head;
                snp_sh.slots[slot].head = (int32_t)i;
            }
            for (uint32_t i = 0; i < n_pads; ++i) {
                if (pads[i].net_id == 0 && !zero_is_real_net) continue;
                int32_t pa_net = pads[i].net_id;
                uint32_t pa_pid = pads[i].pad_id;
                int32_t gx = fdiv(pads[i].x, cell);
                int32_t gy = fdiv(pads[i].y, cell);
                for (int32_t dx = -1; dx <= 1; ++dx) {
                    for (int32_t dy = -1; dy <= 1; ++dy) {
                        int32_t head = sphashn_head(&snp_sh, pa_net,
                                                     gx + dx, gy + dy);
                        while (head >= 0) {
                            uint32_t pb_pid = pads[head].pad_id;
                            int32_t hnext = snp_sh.node_chain[head];
                            if (pa_pid < pb_pid) {
                                int32_t ddx = pads[i].x - pads[head].x;
                                int32_t ddy = pads[i].y - pads[head].y;
                                int32_t d2 = ddx * ddx + ddy * ddy;
                                if (d2 <= tol2) {
                                    /* pad_node indexed by array index, not id. */
                                    int32_t ra = uf_find(&uf,
                                        out->pad_node[i]);
                                    int32_t rb = uf_find(&uf,
                                        out->pad_node[head]);
                                    if (ra != rb) {
                                        uf_union(&uf, ra, rb);
                                        ++out->snp_count;
                                    }
                                }
                            }
                            head = hnext;
                        }
                    }
                }
            }
            sphashn_free(&snp_sh);
        }
    }

    /* ---- Step 4c: pad-to-trace fusion --------------------------------- */
    if (pad_to_trace_tol > 0) {
        int32_t cell = pad_to_trace_tol;
        int32_t tol2 = pad_to_trace_tol * pad_to_trace_tol;
        SpHashN ep_sh;
        uint32_t total_nodes = (uint32_t)arena.node_count;
        if (sphashn_init(&ep_sh, cell, total_nodes + 1) == 0) {
            for (int32_t nid = 0; nid < arena.node_count; ++nid) {
                int32_t net = arena.node_net[nid];
                if (net == 0 && !zero_is_real_net) continue;
                int32_t gx = fdiv(arena.node_x[nid], cell);
                int32_t gy = fdiv(arena.node_y[nid], cell);
                uint32_t slot = sphashn_slot(&ep_sh, net, gx, gy);
                ep_sh.node_chain[nid] = ep_sh.slots[slot].head;
                ep_sh.slots[slot].head = nid;
            }
            for (uint32_t i = 0; i < n_pads; ++i) {
                int32_t pa_net = pads[i].net_id;
                if (pa_net == 0 && !zero_is_real_net) continue;
                int32_t gx = fdiv(pads[i].x, cell);
                int32_t gy = fdiv(pads[i].y, cell);
                /* pad_node indexed by array index. */
                int32_t pad_node_id = out->pad_node[i];
                int32_t pad_root = uf_find(&uf, pad_node_id);
                for (int32_t dx = -1; dx <= 1; ++dx) {
                    for (int32_t dy = -1; dy <= 1; ++dy) {
                        int32_t head = sphashn_head(&ep_sh, pa_net,
                                                     gx + dx, gy + dy);
                        while (head >= 0) {
                            int32_t cx = arena.node_x[head];
                            int32_t cy = arena.node_y[head];
                            int32_t ddx = cx - pads[i].x;
                            int32_t ddy = cy - pads[i].y;
                            int32_t d2 = ddx * ddx + ddy * ddy;
                            if (d2 <= tol2) {
                                int32_t cr = uf_find(&uf, head);
                                if (cr != pad_root) {
                                    uf_union(&uf, pad_root, cr);
                                    pad_root = uf_find(&uf, pad_root);
                                    ++out->ptt_count;
                                }
                            }
                            head = ep_sh.node_chain[head];
                        }
                    }
                }
            }
            sphashn_free(&ep_sh);
        }
    }

    /* ---- Step 5: net propagation -------------------------------------- */
    int32_t untagged = zero_is_real_net ? -1 : 0;

    /* Build (root, net) -> count via a hash table. Cap at 4*n_nodes
     * (loose; any real graph has way fewer (root,net) pairs). */
    uint32_t vote_cap = 1;
    while (vote_cap < (uint32_t)arena.node_count * 2 + 16) vote_cap <<= 1;
    NetVoteSlot *votes = (NetVoteSlot*)calloc(vote_cap, sizeof(NetVoteSlot));
    if (!votes) { uf_free(&uf); sphash_free(&sh); return -1; }
    uint32_t vote_mask = vote_cap - 1;

    for (int32_t nid = 0; nid < arena.node_count; ++nid) {
        int32_t net = arena.node_net[nid];
        if (net == untagged) continue;
        int32_t root = uf_find(&uf, nid);
        uint32_t h = ((uint32_t)root * 0x9E3779B9u) ^ ((uint32_t)net * 0x85EBCA77u);
        h ^= h >> 16; h *= 0x27D4EB2Fu; h ^= h >> 13;
        h &= vote_mask;
        while (votes[h].used) {
            if (votes[h].root == root && votes[h].net == net) {
                ++votes[h].count;
                goto vote_done;
            }
            h = (h + 1) & vote_mask;
        }
        votes[h].used = 1;
        votes[h].root = root;
        votes[h].net = net;
        votes[h].count = 1;
        vote_done:;
    }

    /* Per-root: track the winning net (highest count) and detect
     * conflicts (>1 distinct net per root). We need a small
     * root->{winner, votes, distinct_nets} map. Cap at vote_cap. */
    typedef struct { int32_t root; int32_t winner; uint32_t votes; uint32_t distinct; uint8_t used; } WinSlot;
    WinSlot *wins = (WinSlot*)calloc(vote_cap, sizeof(WinSlot));
    if (!wins) { free(votes); uf_free(&uf); sphash_free(&sh); return -1; }
    for (uint32_t i = 0; i < vote_cap; ++i) {
        if (!votes[i].used) continue;
        uint32_t h = (uint32_t)votes[i].root * 0x9E3779B9u;
        h ^= h >> 16; h *= 0x27D4EB2Fu; h ^= h >> 13;
        h &= vote_mask;
        while (wins[h].used && wins[h].root != votes[i].root) {
            h = (h + 1) & vote_mask;
        }
        if (!wins[h].used) {
            wins[h].used = 1;
            wins[h].root = votes[i].root;
            wins[h].winner = votes[i].net;
            wins[h].votes = votes[i].count;
            wins[h].distinct = 1;
        } else {
            ++wins[h].distinct;
            if (votes[i].count > wins[h].votes) {
                wins[h].votes = votes[i].count;
                wins[h].winner = votes[i].net;
            }
        }
    }

    uint32_t conflicts = 0;
    for (uint32_t i = 0; i < vote_cap; ++i) {
        if (wins[i].used && wins[i].distinct > 1) ++conflicts;
    }
    out->propagation_conflicts = conflicts;

    /* Apply: for each node, look up its root's winner; assign and
     * count changes (where node was untagged). */
    uint32_t changes = 0;
    for (int32_t nid = 0; nid < arena.node_count; ++nid) {
        int32_t root = uf_find(&uf, nid);
        uint32_t h = (uint32_t)root * 0x9E3779B9u;
        h ^= h >> 16; h *= 0x27D4EB2Fu; h ^= h >> 13;
        h &= vote_mask;
        int32_t winner = -1;
        while (wins[h].used) {
            if (wins[h].root == root) { winner = wins[h].winner; break; }
            h = (h + 1) & vote_mask;
        }
        if (winner < 0) continue;
        int32_t cur = arena.node_net[nid];
        if (cur == untagged) ++changes;
        arena.node_net[nid] = winner;
    }
    out->propagation_changes = changes;
    free(votes);
    free(wins);

    /* Backfill seg/poly net_ids: Python rule is "if record.net_id ==
     * untagged, set it from the first endpoint's net". Output arrays
     * indexed by array position. */
    for (uint32_t i = 0; i < n_segs; ++i) {
        out->seg_net_out[i] = segs[i].net_id;
        if (segs[i].net_id == untagged) {
            int32_t a = out->seg_node_a[i];
            out->seg_net_out[i] = arena.node_net[a];
        }
    }
    for (uint32_t i = 0; i < n_polys; ++i) {
        out->poly_net_out[i] = polys[i].net_id;
        if (polys[i].net_id == untagged) {
            uint32_t off = out->poly_nodes_off[i];
            int32_t first = out->poly_nodes_data[off];
            out->poly_net_out[i] = arena.node_net[first];
        }
    }

    /* Copy union-find state out for later find_broken_nets use. */
    for (int32_t i = 0; i < arena.node_count; ++i) {
        out->uf_parent[i] = uf.parent[i];
        out->uf_rank[i] = uf.rank[i];
        out->uf_size[i] = uf.size[i];
    }
    out->node_count = (uint32_t)arena.node_count;

    uf_free(&uf);
    sphash_free(&sh);
    return 0;
}


/* ----- find_segments_in_gap ------------------------------------------- */

EXPORT size_t find_segments_in_gap_native(
        const uint8_t *buf, size_t buf_len,
        size_t gap_start, size_t gap_end,
        uint32_t min_run, int allow_zero_net,
        SegRun *out, size_t out_max)
{
    if (gap_end > buf_len) gap_end = buf_len;
    size_t out_n = 0;
    size_t p = gap_start;
    while (p + 24 <= gap_end) {
        /* is_segment(p)? */
        uint32_t nid = load_u32_le(buf + p);
        uint32_t K = load_u32_le(buf + p + 4);
        int ok = 1;
        if (!allow_zero_net && nid == 0) ok = 0;
        if (nid >= 4000) ok = 0;
        if (K > 50) ok = 0;
        int32_t X1 = 0, Y1 = 0, X2 = 0, Y2 = 0;
        if (ok) {
            X1 = load_i32_le(buf + p + 8);
            Y1 = load_i32_le(buf + p + 12);
            X2 = load_i32_le(buf + p + 16);
            Y2 = load_i32_le(buf + p + 20);
            if (X1 > 2000000 || X1 < -2000000) ok = 0;
            if (Y1 > 2000000 || Y1 < -2000000) ok = 0;
            if (X2 > 2000000 || X2 < -2000000) ok = 0;
            if (Y2 > 2000000 || Y2 < -2000000) ok = 0;
        }
        if (ok) {
            int64_t dx = (int64_t)X2 - X1;
            int64_t dy = (int64_t)Y2 - Y1;
            int64_t d2 = dx * dx + dy * dy;
            if (d2 > (int64_t)1000000000000LL) ok = 0;
        }

        if (ok) {
            size_t run_start = p;
            uint32_t cnt = 0;
            do {
                ++cnt;
                p += 24;
                if (p + 24 > gap_end) break;
                /* is_segment(p) inline */
                nid = load_u32_le(buf + p);
                K = load_u32_le(buf + p + 4);
                if (!allow_zero_net && nid == 0) break;
                if (nid >= 4000) break;
                if (K > 50) break;
                X1 = load_i32_le(buf + p + 8);
                Y1 = load_i32_le(buf + p + 12);
                X2 = load_i32_le(buf + p + 16);
                Y2 = load_i32_le(buf + p + 20);
                if (X1 > 2000000 || X1 < -2000000) break;
                if (Y1 > 2000000 || Y1 < -2000000) break;
                if (X2 > 2000000 || X2 < -2000000) break;
                if (Y2 > 2000000 || Y2 < -2000000) break;
                int64_t dx2 = (int64_t)X2 - X1;
                int64_t dy2 = (int64_t)Y2 - Y1;
                int64_t d2b = dx2 * dx2 + dy2 * dy2;
                if (d2b > (int64_t)1000000000000LL) break;
            } while (1);
            if (cnt >= min_run) {
                if (out_n < out_max) {
                    out[out_n].start = (uint64_t)run_start;
                    out[out_n].end = (uint64_t)p;
                    out[out_n].count = cnt;
                    ++out_n;
                }
            } else {
                /* Reset: walk one byte forward from where we started. */
                p = run_start + 1;
            }
        } else {
            ++p;
        }
    }
    return out_n;
}
