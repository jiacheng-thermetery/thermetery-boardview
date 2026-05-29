"""Verification script for tvw_master_fp.

For each board, parse master footprints, then for every chip whose
footprint is in the master_fps dict, compute pin world positions and
match them against the file's actual pad records. Reports per-board
match rate at multiple tolerance levels and exits non-zero if the
overall match rate is below 90 %.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, "C:/Claude Code")

from tvw_parser import (  # type: ignore
    _decode_position,
    _extract_pads,
    _find_chip_headers,
    _find_pad_runs,
)
from tvw_master_fp import (
    parse_master_footprints,
    pins_world_positions,
)


# Tolerance buckets (in file units; ~1 unit ≈ 0.000325 mm ≈ 0.325 µm,
# so 50 ≈ 16 µm; 200 ≈ 65 µm; 1000 ≈ 325 µm).
TOL_LEVELS = (50, 200, 1000)
GRID_UNITS = 5000  # spatial-grid cell size; ~1.6 mm


def _build_pad_grid(
    pad_xy: List[Tuple[int, int]],
) -> Dict[Tuple[int, int], List[Tuple[int, int]]]:
    grid: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
    for x, y in pad_xy:
        key = (x // GRID_UNITS, y // GRID_UNITS)
        grid.setdefault(key, []).append((x, y))
    return grid


def _nearest_pad(
    grid: Dict[Tuple[int, int], List[Tuple[int, int]]],
    wx: float, wy: float,
    radius_cells: int = 5,
) -> Tuple[Optional[Tuple[int, int]], float]:
    cgx = int(wx // GRID_UNITS)
    cgy = int(wy // GRID_UNITS)
    best = float("inf")
    bp: Optional[Tuple[int, int]] = None
    for gy in range(cgy - radius_cells, cgy + radius_cells + 1):
        for gx in range(cgx - radius_cells, cgx + radius_cells + 1):
            for px, py in grid.get((gx, gy), ()):
                d = (wx - px) * (wx - px) + (wy - py) * (wy - py)
                if d < best:
                    best = d
                    bp = (px, py)
    return bp, math.sqrt(best) if bp is not None else float("inf")


def verify_board(path: Path) -> Dict[str, object]:
    """Run verification on a single TVW file. Returns a result dict."""
    buf = path.read_bytes()

    # Parse master footprints
    master_fps = parse_master_footprints(buf)

    # Extract all pads (38- and 54-byte format runs)
    pad_runs = _find_pad_runs(buf)
    pads = _extract_pads(buf, pad_runs)
    pad_xy = sorted(set((p["x"], p["y"]) for p in pads))
    grid = _build_pad_grid(pad_xy)

    # Find every chip header
    chips = _find_chip_headers(buf)

    # Stats accumulators
    per_fp: Dict[str, Dict[str, int]] = {}
    overall = {f"@{t}": 0 for t in TOL_LEVELS}
    overall["pins"] = 0
    overall["chips"] = 0

    detail_rows: List[Dict[str, object]] = []
    for chip in chips:
        fp = chip["footprint"]
        if fp not in master_fps:
            continue
        if not master_fps[fp]:
            continue
        cx, cy, rot = _decode_position(buf, chip["off"])
        world_pins = pins_world_positions(fp, (cx, cy), rot, master_fps)
        n_pins = len(world_pins)

        bucket_counts = {t: 0 for t in TOL_LEVELS}
        max_d = 0.0
        for _, wx, wy in world_pins:
            _, d = _nearest_pad(grid, wx, wy)
            if d > max_d:
                max_d = d
            for t in TOL_LEVELS:
                if d < t:
                    bucket_counts[t] += 1

        # Update stats
        overall["chips"] += 1
        overall["pins"] += n_pins
        for t in TOL_LEVELS:
            overall[f"@{t}"] += bucket_counts[t]

        fp_entry = per_fp.setdefault(fp, {"chips": 0, "pins": 0,
                                           **{f"@{t}": 0 for t in TOL_LEVELS}})
        fp_entry["chips"] += 1
        fp_entry["pins"] += n_pins
        for t in TOL_LEVELS:
            fp_entry[f"@{t}"] += bucket_counts[t]

        detail_rows.append({
            "footprint": fp,
            "dev_name": chip["dev_name"],
            "rot": rot,
            "pins": n_pins,
            **{f"@{t}": bucket_counts[t] for t in TOL_LEVELS},
            "max_d": int(max_d) if max_d != float("inf") else -1,
        })

    return {
        "path": path,
        "master_fps_total": len(master_fps),
        "master_fps_with_pins": sum(1 for v in master_fps.values() if v),
        "chips_total": len(chips),
        "overall": overall,
        "per_fp": per_fp,
        "details": detail_rows,
    }


def main() -> None:
    boards = [
        Path("C:/Claude Code/Z490 VISION G r1.0.tvw"),
        Path("C:/Claude Code/Gigabyte_X570_GAMING_X_REV1.01.tvw"),
        Path("C:/Claude Code/B550_AORUS_PRO_AC_REV1.0.tvw"),
    ]

    grand_pins = 0
    grand_at = {t: 0 for t in TOL_LEVELS}

    for path in boards:
        if not path.exists():
            print(f"Missing: {path}")
            continue
        print(f"\n{'='*70}")
        print(f"Board: {path.name}")
        print(f"{'='*70}")
        res = verify_board(path)
        print(f"  Master footprints: {res['master_fps_total']} total, "
              f"{res['master_fps_with_pins']} with pin vertices")
        print(f"  Chips with master fp: {res['overall']['chips']}")
        print(f"  Total pins evaluated: {res['overall']['pins']}")
        n = max(res['overall']['pins'], 1)
        print(f"  Match rates:")
        for t in TOL_LEVELS:
            cnt = res['overall'][f"@{t}"]
            print(f"    @{t:>4} units: {cnt:>5}/{n:<5}  "
                  f"({100*cnt/n:5.1f}%)")
            grand_at[t] += cnt
        grand_pins += res['overall']['pins']

        # Per-footprint breakdown
        print(f"\n  Per-footprint (only fps with chip instances):")
        per_fp = res['per_fp']
        rows = sorted(per_fp.items(), key=lambda kv: -kv[1]['pins'])
        print(f"    {'footprint':<32} {'inst':>4} {'pins':>5} "
              f"{'@50':>5} {'@200':>5} {'@1000':>6}")
        for name, st in rows:
            n_pins = st['pins']
            print(f"    {name:<32} {st['chips']:>4} {n_pins:>5} "
                  f"{st['@50']:>5} {st['@200']:>5} {st['@1000']:>6}  "
                  f"({100*st['@50']/max(n_pins,1):5.1f}% @ 50)")

        # Imperfect chips
        imperfect = [d for d in res['details'] if d['@50'] < d['pins']]
        if imperfect:
            print(f"\n  Chips with imperfect match (sorted by missed pins):")
            imperfect.sort(key=lambda d: -(d['pins'] - d['@50']))
            for d in imperfect[:20]:
                print(f"    {d['footprint']:<28} rot={d['rot']:>3}  "
                      f"{d['dev_name'][:20]:<20}  "
                      f"@50={d['@50']:>3}/{d['pins']:<3}  max_d={d['max_d']:,}")

    # Grand totals
    print(f"\n{'='*70}")
    print(f"GRAND TOTALS across all boards: {grand_pins} pins evaluated")
    n = max(grand_pins, 1)
    for t in TOL_LEVELS:
        print(f"  @{t:>4}: {grand_at[t]:>5}/{n:<5}  ({100*grand_at[t]/n:5.1f}%)")

    # Exit code: success if @50 >= 90% of grand total
    pct = 100 * grand_at[50] / n
    if pct < 90.0:
        print(f"\nFAIL: @50 match rate {pct:.1f}% below 90% threshold")
        sys.exit(1)
    print(f"\nPASS: @50 match rate {pct:.1f}% (target: 90%)")


if __name__ == "__main__":
    main()
