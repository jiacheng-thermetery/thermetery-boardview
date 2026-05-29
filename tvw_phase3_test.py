"""Phase 3 part 1 verification — exercise the lazy topology hook on
BoardModel for the three reference TVW boards (Z490, X570, B550).

Goals (each printed as PASS/FAIL):

  1. boardview.parse() returns a BoardModel.
  2. `model.topology_available` is True for `.tvw`, with no build cost
     paid yet (the loader is just a callable thunk).
  3. The first access of `model.topology` builds the graph and the
     second access returns the cached instance with negligible cost.
  4. `find_broken_nets()` returns a list and reports counts.
  5. `trace_geometry_for_net()` returns non-empty (segments + polylines)
     for at least one signal net per board.
  6. `net_at_point()` resolves a pad's centre back to the same net name
     as the pad's source net for at least one pad per board.

Total runtime budget: 25 s. Each topology build is 3-6 s × 3 boards =
~15 s; the rest is metadata work.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable, List, Tuple

import boardview
from gencad_parser import BoardModel, BrokenNet


BOARDS: List[str] = [
    "C:/Claude Code/Z490 VISION G r1.0.tvw",
    "C:/Claude Code/Gigabyte_X570_GAMING_X_REV1.01.tvw",
    "C:/Claude Code/B550_AORUS_PRO_AC_REV1.0.tvw",
]


def _hr(label: str) -> None:
    print()
    print("=" * 70)
    print(label)
    print("=" * 70)


def _ok(label: str, ok: bool, detail: str = "") -> None:
    badge = "PASS" if ok else "FAIL"
    line = f"  [{badge}] {label}"
    if detail:
        line = f"{line}  --  {detail}"
    print(line)


def _pick_signal_nets_with_geometry(model: BoardModel,
                                    n: int = 2) -> List[str]:
    """Return up to `n` signal nets that (a) are not power nets and
    (b) have non-empty topology geometry. The first match for each is
    used as a smoke test for `trace_geometry_for_net`.
    """
    from tvw_parser import _POWER_NET_RE
    graph = model.topology
    # One-pass collection of nets that actually carry geometry. Calling
    # `geometry_on_net` per candidate is O(segs+polys) each — too slow
    # on big boards. Instead, scan segments + polylines once and tag
    # each net_id seen.
    nets_with_geom: set = {s.net_id for s in graph.segments if s.net_id}
    nets_with_geom.update(p.net_id for p in graph.polylines if p.net_id)
    out: List[str] = []
    for nid in sorted(nets_with_geom):
        if nid >= len(graph.net_names):
            continue
        nm = graph.net_names[nid]
        if not nm or _POWER_NET_RE.match(nm):
            continue
        out.append(nm)
        if len(out) >= n:
            break
    return out


def _pick_pad_centres(model: BoardModel,
                      n: int = 2) -> List[Tuple[float, float, str, str]]:
    """Pick up to n pads on signal (non-power) nets, return as
    (x, y, layer, expected_net_name)."""
    from tvw_parser import _POWER_NET_RE
    graph = model.topology
    seen_nets: set = set()
    out: List[Tuple[float, float, str, str]] = []
    for pad in graph.pads:
        if not pad.net_id:
            continue
        if pad.net_id >= len(graph.net_names):
            continue
        name = graph.net_names[pad.net_id]
        if not name or _POWER_NET_RE.match(name):
            continue
        if name in seen_nets:
            continue
        seen_nets.add(name)
        out.append((float(pad.x), float(pad.y), pad.layer, name))
        if len(out) >= n:
            break
    return out


def _summarise_breaks(breaks: List[BrokenNet], top: int = 5) -> str:
    if not breaks:
        return "    (none)"
    lines = []
    for b in breaks[:top]:
        lines.append(
            f"    {b.net_name!r:34s} pads={b.n_pads:3d}  "
            f"comps={b.n_components:2d}  biggest={b.biggest_component_size}"
        )
    if len(breaks) > top:
        lines.append(f"    ... ({len(breaks) - top} more)")
    return "\n".join(lines)


def _exercise_board(path: str) -> Tuple[bool, dict]:
    """Run the full Phase 3 part 1 check on one TVW board.

    Returns (overall_pass, info) where info has timing and
    broken-net counts useful for the cross-board summary.
    """
    overall = True
    info: dict = {"board": Path(path).name}
    _hr(f"Board: {Path(path).name}")
    t0 = time.perf_counter()
    model = boardview.parse(path)
    t1 = time.perf_counter()
    info["parse_s"] = t1 - t0
    _ok("parsed via boardview.parse",
        isinstance(model, BoardModel),
        f"{t1 - t0:.2f} s, {len(model.components)} components, "
        f"{len(model.signals)} nets")
    overall &= isinstance(model, BoardModel)

    # 2. topology_available without paying for a build.
    t0 = time.perf_counter()
    available = model.topology_available
    t1 = time.perf_counter()
    avail_cost = t1 - t0
    info["available_check_s"] = avail_cost
    _ok("topology_available is True",
        available is True,
        f"check took {avail_cost*1000:.3f} ms")
    overall &= (available is True)
    # The loader is a thunk; not yet called. Should be sub-millisecond.
    overall_cheap = avail_cost < 0.05
    _ok("topology_available is cheap (<50 ms)",
        overall_cheap,
        "no build triggered")
    overall &= overall_cheap

    # 3. First access builds; second access cached.
    t0 = time.perf_counter()
    g1 = model.topology
    t1 = time.perf_counter()
    cold = t1 - t0
    info["cold_topology_s"] = cold
    _ok("first .topology access builds the graph",
        cold > 0.05,
        f"{cold:.2f} s, "
        f"{len(g1.pads)} pads, {len(g1.segments)} segs, "
        f"{len(g1.polylines)} polys")
    overall &= cold > 0.05

    t0 = time.perf_counter()
    g2 = model.topology
    t1 = time.perf_counter()
    cached = t1 - t0
    info["cached_topology_s"] = cached
    cached_ok = (g2 is g1) and cached < 0.001
    _ok("second .topology access returns cached instance",
        cached_ok,
        f"{cached*1e6:.1f} us")
    overall &= cached_ok

    # 4. find_broken_nets
    t0 = time.perf_counter()
    breaks = model.find_broken_nets()
    t1 = time.perf_counter()
    info["broken_nets_s"] = t1 - t0
    info["n_broken"] = len(breaks)
    info["biggest_break"] = (
        max(b.n_components for b in breaks) if breaks else 0)
    info["worst_pads"] = (
        max(b.n_pads for b in breaks) if breaks else 0)
    _ok("find_broken_nets returns a list", isinstance(breaks, list),
        f"{len(breaks)} broken signal nets, in {t1 - t0:.2f} s")
    overall &= isinstance(breaks, list)
    print(_summarise_breaks(breaks))

    # 5. trace_geometry_for_net for 1-2 sample nets
    sample_nets = _pick_signal_nets_with_geometry(model, n=2)
    geom_ok = bool(sample_nets)
    for nn in sample_nets:
        segs, polys = model.trace_geometry_for_net(nn)
        non_empty = bool(segs) or bool(polys)
        _ok(f"trace_geometry_for_net({nn!r}) non-empty",
            non_empty,
            f"{len(segs)} segs, {len(polys)} polys")
        geom_ok &= non_empty
    if not sample_nets:
        _ok("trace_geometry_for_net sampled OK", False,
            "no non-power signal net with geometry — unexpected")
    overall &= geom_ok

    # An unknown net should return ([],[])
    segs, polys = model.trace_geometry_for_net("DEFINITELY_NOT_A_NET_xx")
    _ok("trace_geometry_for_net(unknown) is ([], [])",
        segs == [] and polys == [],
        "")
    overall &= (segs == [] and polys == [])

    # 6. net_at_point for 1-2 pads (round-trip the pad's net through
    #    a coord lookup and confirm name matches).
    samples = _pick_pad_centres(model, n=2)
    for x, y, layer, expected in samples:
        got = model.net_at_point(x, y, layer=layer, tol=200)
        ok = (got == expected)
        _ok(f"net_at_point({x:.0f}, {y:.0f}, {layer}) -> {expected!r}",
            ok,
            f"got {got!r}")
        overall &= ok

    # net_at_point with no pad anywhere nearby (way off canvas).
    none_got = model.net_at_point(-10_000_000, -10_000_000, "TOP", tol=10)
    _ok("net_at_point(off-canvas) -> None",
        none_got is None,
        f"got {none_got!r}")
    overall &= (none_got is None)

    info["overall"] = overall
    return overall, info


def _exercise_non_tvw_smoke() -> bool:
    """GENCAD models now carry topology too (built lazily from $ROUTES).
    Verify the loader fires correctly and the same helpers return data."""
    _hr("GENCAD topology smoke test")
    cad = "C:/Claude Code/MSI MS-7680 Rev 5.1 BoardView.cad"
    if not Path(cad).exists():
        _ok("GENCAD board file present", False,
            f"missing {cad!r}; skipping")
        return True

    model = boardview.parse(cad)
    overall = True
    overall &= bool(model.topology_available)
    _ok("topology_available True on .cad model",
        bool(model.topology_available))

    g = model.topology
    overall &= (len(g.segments) > 0)
    _ok(f"GENCAD topology has segments", len(g.segments) > 0,
        f"{len(g.segments):,} segments, {len(g.pads):,} pads, "
        f"{len(g.net_names):,} nets")

    breaks = model.find_broken_nets()
    overall &= isinstance(breaks, list)
    _ok("find_broken_nets returns a list", isinstance(breaks, list),
        f"{len(breaks)} broken nets")

    # Find a real net with traces and verify the helpers return data.
    sample = next((n for n in ("+12V", "GND", "VCC3", "VCC")
                   if n in model.signals), None)
    if sample:
        segs, polys = model.trace_geometry_for_net(sample)
        ok = len(segs) > 0
        overall &= ok
        _ok(f"trace_geometry_for_net({sample!r}) non-empty", ok,
            f"{len(segs)} segs, {len(polys)} polys")

    return overall


def main() -> int:
    t0 = time.perf_counter()
    all_ok = True
    summary: List[dict] = []
    for path in BOARDS:
        if not Path(path).exists():
            print(f"  [SKIP] {path} — file missing")
            continue
        ok, info = _exercise_board(path)
        all_ok &= ok
        summary.append(info)

    all_ok &= _exercise_non_tvw_smoke()

    elapsed = time.perf_counter() - t0
    _hr("SUMMARY")
    print(f"  total runtime: {elapsed:.2f} s")
    for info in summary:
        print(
            f"  {info['board']:50s}  "
            f"cold={info.get('cold_topology_s', 0):.2f}s  "
            f"cached={info.get('cached_topology_s', 0)*1e6:.1f}us  "
            f"broken={info.get('n_broken', 0)}  "
            f"worst_break_pads={info.get('worst_pads', 0)}"
        )
    print()
    print("  RESULT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
