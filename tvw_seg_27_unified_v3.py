# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""Phase 27: Unified scanner v3 with polyline chains support.

A polyline chain is a sequence of polylines [u32 K][K * (X, Y)] separated by
either 4 zero bytes OR 12 zero bytes (4 + extra 8 padding).
Found primarily in X570.

Probably what's happening: X570 stores polylines NOT in [count][type=1] blocks,
but as bare chains. Z490 and B550 mostly use blocks, so chains add little there.
"""
from __future__ import annotations
import struct
from pathlib import Path
from collections import Counter


def find_polyline_blocks(buf, region_start, region_end, max_K=100000):
    # Native fast path — see tvw_native.c. ~600× speedup on cold loads.
    try:
        from tvw_native import find_polyline_blocks as _nat_find_blocks
        result = _nat_find_blocks(buf, region_start, region_end, max_K=max_K)
        if result is not None:
            return result
    except Exception:
        pass
    blocks = []
    n = min(region_end, len(buf))

    def walk_block(start):
        if start + 8 > n: return None
        count = struct.unpack_from('<I', buf, start)[0]
        type_field = struct.unpack_from('<I', buf, start + 4)[0]
        if not (1 < count < 100000): return None
        if type_field != 1: return None

        polys = []
        cur = start + 8
        first = True
        while len(polys) < count and cur + 4 <= n:
            if not first:
                if buf[cur:cur+4] != b'\x00\x00\x00\x00': return None
                cur += 4
            if cur + 4 > n: return None
            K = struct.unpack_from('<I', buf, cur)[0]
            if K < 2 or K > max_K: return None
            body_end = cur + 4 + K * 8
            if body_end > n: return None
            x = struct.unpack_from('<i', buf, cur + 4)[0]
            y = struct.unpack_from('<i', buf, cur + 8)[0]
            if abs(x) > 2_000_000 or abs(y) > 2_000_000: return None
            polys.append((cur, K))
            cur = body_end
            first = False

        if len(polys) == count:
            return (start, count, cur)
        return None

    p = region_start
    while p + 12 <= n:
        result = walk_block(p)
        if result:
            blocks.append(result)
            p = result[2]
        else:
            p += 1

    return blocks


def find_tagged_polylines_in_gap(buf, gap_start, gap_end, term_size=4,
                                  max_net_id=4000, max_vertices=100000):
    # Native fast path — see tvw_native.c.
    try:
        from tvw_native import find_tagged_polylines_in_gap as _nat_find_tagged
        result = _nat_find_tagged(buf, gap_start, gap_end,
                                   term_size=term_size,
                                   max_net_id=max_net_id,
                                   max_vertices=max_vertices)
        if result is not None:
            return result
    except Exception:
        pass
    out = []
    i = gap_start
    n = gap_end
    while i + 12 < n:
        net_id = struct.unpack_from('<I', buf, i)[0]
        K = struct.unpack_from('<I', buf, i + 4)[0]
        if net_id == 0 or net_id >= max_net_id or K < 2 or K > max_vertices:
            i += 1; continue
        body_end = i + 8 + K * 8
        if body_end + term_size > n:
            i += 1; continue
        if buf[body_end:body_end+term_size] != b'\x00' * term_size:
            i += 1; continue
        x = struct.unpack_from('<i', buf, i + 8)[0]
        y = struct.unpack_from('<i', buf, i + 12)[0]
        if abs(x) > 2_000_000 or abs(y) > 2_000_000:
            i += 1; continue
        out.append((i, net_id, K))
        i = body_end + term_size
    return out


def find_pad_runs_in_gap(buf, gap_start, gap_end, min_run=50):
    n = gap_end
    runs = []

    def is_pad(off):
        if off + 38 > n: return False
        if buf[off+20:off+22] != b'\x00\x00': return False
        nid = struct.unpack_from('<I', buf, off+22)[0]
        return 0 < nid < 4000

    p = gap_start
    while p + 38 <= n:
        if is_pad(p):
            run_start = p; cnt = 0
            while p + 38 <= n and is_pad(p):
                p += 38; cnt += 1
            if cnt >= min_run:
                runs.append((run_start, p))
        else:
            p += 1
    return runs


def find_segments_in_gap(buf, gap_start, gap_end, min_run=10, allow_zero_net=True):
    # Native fast path — see tvw_native.c.
    try:
        from tvw_native import find_segments_in_gap as _nat_find_segs
        result = _nat_find_segs(buf, gap_start, gap_end,
                                 min_run=min_run,
                                 allow_zero_net=allow_zero_net)
        if result is not None:
            return result
    except Exception:
        pass
    n = gap_end

    def is_segment(off):
        if off + 24 > n: return False
        try:
            nid, K, X1, Y1, X2, Y2 = struct.unpack_from('<2I4i', buf, off)
        except: return False
        if not (allow_zero_net or nid > 0):
            return False
        if nid >= 4000: return False
        if K > 50: return False
        if abs(X1) > 2_000_000 or abs(Y1) > 2_000_000: return False
        if abs(X2) > 2_000_000 or abs(Y2) > 2_000_000: return False
        dx = X2 - X1; dy = Y2 - Y1
        if dx*dx + dy*dy > 1_000_000_000_000: return False
        return True

    runs = []
    p = gap_start
    while p + 24 <= n:
        if is_segment(p):
            run_start = p; cnt = 0
            while p + 24 <= n and is_segment(p):
                p += 24; cnt += 1
            if cnt >= min_run:
                runs.append((run_start, p, cnt))
        else:
            p += 1
    return runs


def find_polyline_chains_in_gap(buf, gap_start, gap_end, min_chain=3, max_K=100000):
    """Find chains of untagged polylines [K][verts] separated by 4 or 12 zeros."""
    # Native fast path — see tvw_native.c.
    try:
        from tvw_native import find_polyline_chains_in_gap as _nat_chains
        result = _nat_chains(buf, gap_start, gap_end,
                             min_chain=min_chain, max_K=max_K)
        if result is not None:
            return result
    except Exception:
        pass
    chains = []
    n = gap_end

    p = gap_start
    while p + 8 <= n:
        # Try multiple offsets for chain start
        for try_offset in range(0, min(16, n - p), 4):
            sp = p + try_offset
            if sp + 4 > n: continue

            chain_start = sp
            cur = sp
            polys = []

            while cur + 4 <= n:
                K = struct.unpack_from('<I', buf, cur)[0]
                if K < 2 or K > max_K: break
                body_end = cur + 4 + K * 8
                if body_end > n: break
                x = struct.unpack_from('<i', buf, cur + 4)[0]
                y = struct.unpack_from('<i', buf, cur + 8)[0]
                if abs(x) > 2_000_000 or abs(y) > 2_000_000: break
                polys.append((cur, K))
                cur = body_end
                if cur + 4 > n:
                    break
                # Check 4-zero separator first
                if buf[cur:cur+4] != b'\x00' * 4:
                    break
                cur += 4
                # Check if there's an additional 8-zero padding
                if cur + 8 <= n and buf[cur:cur+8] == b'\x00' * 8:
                    # Skip 8 more zeros
                    cur += 8

            if len(polys) >= min_chain:
                chain_end = cur
                # Roll back the trailing separator to record actual chain bytes
                # Actually keep the trailing 4 zeros as part of chain to match offsets
                chains.append((chain_start, chain_end, len(polys)))
                p = chain_end
                break  # done with this position
        else:
            p += 1
            continue
        # If we found a chain, p is updated, continue outer loop

    return chains


def merge_intervals(intervals):
    if not intervals: return []
    intervals = sorted(intervals)
    merged = [intervals[0]]
    for s, e in intervals[1:]:
        ls, le = merged[-1]
        if s <= le:
            merged[-1] = (ls, max(le, e))
        else:
            merged.append((s, e))
    return merged


def find_gaps(region_start, region_end, covered):
    covered = sorted(covered)
    gaps = []
    cur = region_start
    for s, e in covered:
        if s > cur:
            gaps.append((cur, s))
        cur = max(cur, e)
    if cur < region_end:
        gaps.append((cur, region_end))
    return gaps


def analyze(file_path, region_start, region_end, label):
    buf = Path(file_path).read_bytes()
    region_size = region_end - region_start
    print(f"\n=== {label} ({file_path}) ===")
    print(f"Region: [+{region_start:,}, +{region_end:,}] size={region_size:,}\n")

    # Step 1: blocks
    blocks = find_polyline_blocks(buf, region_start, region_end)
    block_intervals = [(s, e) for s, c, e in blocks]
    block_bytes = sum(e - s for s, e in block_intervals)
    block_polys = sum(c for _, c, _ in blocks)
    print(f"1. Polyline blocks:    {len(blocks):>5}, {block_polys:>5} polys, {block_bytes:>10,} bytes ({100*block_bytes/region_size:.2f}%)")

    # Step 2: tagged
    current = merge_intervals(block_intervals)
    gaps = find_gaps(region_start, region_end, current)
    tagged = []
    for gs, ge in gaps:
        ps = find_tagged_polylines_in_gap(buf, gs, ge)
        tagged.extend(ps)
    tag_intervals = [(off, off + 8 + K*8 + 4) for off, _, K in tagged]
    tag_bytes = sum(e - s for s, e in tag_intervals)
    print(f"2. Tagged polylines:   {len(tagged):>5}, {tag_bytes:>10,} bytes ({100*tag_bytes/region_size:.2f}%)")

    # Step 3: pad runs
    current = merge_intervals(block_intervals + tag_intervals)
    gaps = find_gaps(region_start, region_end, current)
    pad_runs = []
    for gs, ge in gaps:
        rs = find_pad_runs_in_gap(buf, gs, ge)
        pad_runs.extend(rs)
    pad_bytes = sum(e - s for s, e in pad_runs)
    print(f"3. Pad runs:           {len(pad_runs):>5}, {pad_bytes:>10,} bytes ({100*pad_bytes/region_size:.2f}%)")

    # Step 4: trace segments
    current = merge_intervals(block_intervals + tag_intervals + pad_runs)
    gaps = find_gaps(region_start, region_end, current)
    seg_runs = []
    for gs, ge in gaps:
        rs = find_segments_in_gap(buf, gs, ge, allow_zero_net=True)
        seg_runs.extend(rs)
    seg_intervals = [(s, e) for s, e, c in seg_runs]
    seg_bytes = sum(e - s for s, e in seg_intervals)
    seg_count = sum(c for _, _, c in seg_runs)
    print(f"4. Trace segments:     {len(seg_runs):>5}, {seg_count:>5} segs, {seg_bytes:>10,} bytes ({100*seg_bytes/region_size:.2f}%)")

    # Step 5: polyline chains (NEW)
    current = merge_intervals(block_intervals + tag_intervals + pad_runs + seg_intervals)
    gaps = find_gaps(region_start, region_end, current)
    chains = []
    for gs, ge in gaps:
        cs = find_polyline_chains_in_gap(buf, gs, ge)
        chains.extend(cs)
    chain_intervals = [(s, e) for s, e, c in chains]
    chain_bytes = sum(e - s for s, e in chain_intervals)
    chain_polys = sum(c for _, _, c in chains)
    print(f"5. Polyline chains:    {len(chains):>5}, {chain_polys:>5} polys, {chain_bytes:>10,} bytes ({100*chain_bytes/region_size:.2f}%)")

    # Total
    all_covered = merge_intervals(
        block_intervals + tag_intervals + pad_runs + seg_intervals + chain_intervals
    )
    covered_bytes = sum(e - s for s, e in all_covered)
    final_gaps = find_gaps(region_start, region_end, all_covered)
    gap_total = sum(e - s for s, e in final_gaps)
    print(f"\nTOTAL COVERED: {covered_bytes:,} ({100*covered_bytes/region_size:.2f}%)")
    print(f"REMAINING:     {gap_total:,} ({100*gap_total/region_size:.2f}%)")

    return final_gaps


if __name__ == "__main__":
    analyze("C:/Claude Code/Z490 VISION G r1.0.tvw", 8_528, 4_761_170, "Z490 Custom_35")
    analyze("C:/Claude Code/Gigabyte_X570_GAMING_X_REV1.01.tvw", 4_754, 1_838_204, "X570 Custom_21")
    analyze("C:/Claude Code/B550_AORUS_PRO_AC_REV1.0.tvw", 6_474, 3_978_556, "B550 Custom_26")
