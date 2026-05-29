"""Render-tier verification + frame-time benchmark.

Loads each of Z490, B550, X570 in turn through walker.make_board_canvas,
selects a known net (VCORE on Z490; the highest-pad-count net on the
others — generally still VCORE / GND / a major rail), then times one
redraw at zoom 1.0, 2.96 and 8.0 with traces enabled.

Output:
    Z490: tier=gl, zoom 2.96=4ms, zoom 8.0=3ms, zoom 1.0=2ms

If GL fails to initialise on this box, the script downgrades the test
to a cpu-tier smoke test instead of failing.

Run from the project directory:  `python walker_render_test.py`
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path
from typing import List, Optional, Tuple

import tkinter as tk

import walker
from boardview import parse as parse_board


BOARDS: List[Tuple[str, str, Optional[str]]] = [
    # (label, tvw path, default net to highlight or None for auto-pick)
    ("Z490", r"C:\Claude Code\Z490 VISION G r1.0.tvw",                "VCORE"),
    ("B550", r"C:\Claude Code\B550_AORUS_PRO_AC_REV1.0.tvw",          None),
    ("X570", r"C:\Claude Code\Gigabyte_X570_GAMING_X_REV1.01.tvw",    None),
]

# Frame zooms to time. The middle one (~2.96) is the "typical
# user-zoomed" workload — that's the perf gate.
ZOOMS = [2.96, 8.0, 1.0]
N_TIMING_FRAMES = 5  # run multiple frames per zoom + take the median
W, H = 1920, 1080


def _pick_net(board, fallback: Optional[str]) -> Optional[str]:
    """Return a net name we can highlight. Prefer the caller-specified
    fallback if it actually exists in this board's net table; else
    walk the topology and pick a net with substantial geometry that
    isn't obviously power/ground (we want to see traces, not planes)."""
    if not getattr(board, "topology_available", False):
        return None
    if fallback:
        try:
            nid = board.topology.net_id_by_name(fallback)
            if nid is not None:
                return fallback
        except Exception:
            pass
    # Fallback: scan net_id frequency among segments, take a non-power
    # net with at least 50 segments.
    try:
        topo = board.topology
        from collections import Counter
        c: Counter[int] = Counter()
        for seg in topo.segments:
            if seg.net_id != 0:
                c[seg.net_id] += 1
        for nid, _count in c.most_common(20):
            name = topo.net_name(nid)
            if not name:
                continue
            uname = name.upper()
            if any(p in uname for p in ("GND", "VSS", "GROUND")):
                continue
            return name
    except Exception:
        traceback.print_exc()
    return None


def _force_redraw(canvas) -> float:
    """Trigger one immediate redraw and return wall-clock ms."""
    t0 = time.perf_counter()
    if canvas.render_tier == "gl":
        # GL path: cancel any deferred after_idle and call _display
        # directly so the timing is for ONE frame end-to-end.
        canvas._redraw_scheduled = False
        canvas._do_redraw()
    else:
        canvas._redraw()
        # CPU path also schedules tk.PhotoImage etc. via tk's image
        # loader — let the event queue settle before we stop the clock.
        canvas.update_idletasks()
    return (time.perf_counter() - t0) * 1000


def _time_zooms(canvas, zooms: List[float]) -> List[float]:
    """Time one redraw at each zoom. Median of N_TIMING_FRAMES."""
    medians: List[float] = []
    for z in zooms:
        canvas.zoom = z
        canvas.pan_x = 0.0
        canvas.pan_y = 0.0
        # Throw away one warm-up frame (first frame at a new zoom
        # tends to be cache-cold).
        _force_redraw(canvas)
        samples = sorted(_force_redraw(canvas) for _ in range(N_TIMING_FRAMES))
        median = samples[len(samples) // 2]
        medians.append(median)
    return medians


def run_one(label: str, tvw_path: str, default_net: Optional[str]) -> None:
    p = Path(tvw_path)
    if not p.exists():
        print(f"{label}: SKIP — file not found ({tvw_path})")
        return
    print(f"{label}: loading {p.name} ...", flush=True)
    t0 = time.perf_counter()
    try:
        board = parse_board(p)
    except Exception as e:
        print(f"{label}: load FAILED — {e}")
        return
    load_ms = (time.perf_counter() - t0) * 1000
    n_comp = len(board.components)
    has_topo = getattr(board, "topology_available", False)
    print(f"{label}: loaded in {load_ms:.0f} ms ({n_comp} components, "
          f"topology_available={has_topo})", flush=True)

    root = tk.Tk()
    root.geometry(f"{W}x{H}+0+0")
    root.title(f"render-test {label}")

    canvas = walker.make_board_canvas(root, board)
    canvas.pack(fill="both", expand=True)
    root.update_idletasks()
    root.update()

    # Force a redraw cycle so widget realises and (if GL) initgl runs.
    if hasattr(canvas, "_schedule_redraw"):
        canvas._schedule_redraw()
    else:
        canvas._redraw()
    root.update_idletasks()
    root.update()

    tier = canvas.render_tier
    net_name = _pick_net(board, default_net)
    print(f"{label}: tier={tier}, net={net_name}", flush=True)

    if has_topo and net_name:
        # Force topology build now (lazy, 3-6 s) before we time anything.
        t0 = time.perf_counter()
        _ = board.topology
        topo_ms = (time.perf_counter() - t0) * 1000
        print(f"{label}: topology built in {topo_ms:.0f} ms", flush=True)

        canvas.set_selected_net(net_name)
        canvas.toggle_traces()
        # toggle_traces also triggers a redraw — let it complete.
        root.update_idletasks()
        root.update()

    medians = _time_zooms(canvas, ZOOMS)
    parts = [f"zoom {z}={ms:.0f}ms" for z, ms in zip(ZOOMS, medians)]
    print(f"{label}: tier={tier}, " + ", ".join(parts), flush=True)

    root.destroy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gl-probe-only", action="store_true",
                    help="Just probe the GL stack and exit")
    args = ap.parse_args()

    if args.gl_probe_only:
        ok = walker._probe_gl_canvas(verbose=True)
        print(f"GL probe: {'OK' if ok else 'FAIL'}")
        sys.exit(0 if ok else 1)

    print(f"_GL_AVAILABLE = {walker._GL_AVAILABLE}")
    print(f"GL probe: {walker._gl_probe_cached()}")
    print()

    for label, path, default_net in BOARDS:
        try:
            run_one(label, path, default_net)
        except Exception:
            traceback.print_exc()
        print()


if __name__ == "__main__":
    main()
