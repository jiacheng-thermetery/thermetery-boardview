"""Verification driver for tvw_topology.TraceGraph.

Runs all three reference boards, reports stats, picks well-known nets
and checks they end up in one big component (split components = either
a tolerance bug or a real broken trace). Also picks a small distinctive
net to confirm endpoints connect.

Run:
    python tvw_topo_test.py [Z490|X570|B550|all]
"""
from __future__ import annotations

import sys
import time
from collections import Counter, defaultdict
from typing import Dict, List, Tuple

from tvw_topology import TraceGraph, KNOWN_BOARDS


# Universal nets we expect to find on every board. Order matters: GND
# always comes first (it's the giant ground plane), then 12V rails,
# then VCORE for Intel platforms / variants for AMD. The exact net name
# differs by board (Intel vs AMD vs Gigabyte naming) so we list common
# aliases — the test reports the first match.
UNIVERSAL_NETS = [
    "GND",
    "+12V", "MB_VIN", "VIN",
    "VCORE",
    "VCC3", "VCC3_CPU", "VCC", "+3V",
    "AGND",
]


def _component_sizes_for_net(graph: TraceGraph, net_id: int) -> List[int]:
    """Return component sizes (sorted desc) of all components touching
    `net_id`. One big component = healthy; many small = bug or break."""
    comp_size: Dict[int, int] = defaultdict(int)
    for nid in range(len(graph._node_xy)):
        if graph._node_net[nid] == net_id:
            root = graph._uf.find(nid)
            comp_size[root] += 1
    return sorted(comp_size.values(), reverse=True)


def _print_net_health(graph: TraceGraph, net_name: str) -> None:
    """Print a single net's health line. Key metrics:
       - components_with_pads: how many distinct components hold AT
         LEAST ONE pad of this net. Ideal=1; >1 is fragmentation.
       - pads_in_biggest: how many pads are in the largest component.
       Healthy = 1 component, or biggest holds >=80 % of pads.
    """
    nid = graph.net_id_by_name(net_name)
    if nid is None:
        print(f"      {net_name:<20s}  NOT IN NET TABLE")
        return
    pads = graph.pads_on_net(nid)
    if not pads:
        print(f"      {net_name:<20s}  net id {nid:>4d}  NO PADS")
        return
    # Count pads per component.
    from collections import Counter
    comp_pad: Counter = Counter()
    for p in pads:
        node = graph._pad_node.get(p.pad_id, -1)
        if node < 0:
            continue
        comp_pad[graph._uf.find(node)] += 1
    sizes = sorted(comp_pad.values(), reverse=True)
    if not sizes:
        print(f"      {net_name:<20s}  net id {nid:>4d}  pads but no nodes")
        return
    n_comp = len(sizes)
    biggest = sizes[0]
    biggest_pct = 100.0 * biggest / len(pads)
    health = "OK" if (n_comp == 1 or biggest_pct >= 80) else "DEGRADED"
    extra = ""
    if n_comp > 1:
        extra = f"  top: {sizes[:5]}"
    print(f"      {net_name:<20s}  id={nid:>4d}  pads={len(pads):>5d}  "
          f"comps={n_comp:>4d}  biggest={biggest:>5d}pads "
          f"({biggest_pct:5.1f}%)  [{health}]{extra}")


def _find_small_distinctive_net(graph: TraceGraph) -> str | None:
    """Pick a small named net with a distinctive name (not GND/VCC/...)
    so we can sanity-check connectivity end-to-end on something simple.

    Heuristic: prefer nets with names containing hyphens or
    underscores between letter-segments (like "PCH_PWR_OK", "PEGX_RX0_P")
    that have between 4 and 30 pads."""
    candidates: List[Tuple[int, int, str]] = []  # (n_pads, net_id, name)
    pad_count: Dict[int, int] = defaultdict(int)
    for pad in graph.pads:
        pad_count[pad.net_id] += 1
    for nid, name in enumerate(graph.net_names):
        c = pad_count.get(nid, 0)
        if not (4 <= c <= 30):
            continue
        # Distinctive: contains _ between letter parts, or contains digits
        if "_" not in name:
            continue
        # Avoid power names
        upper = name.upper()
        if any(p in upper for p in
               ("GND", "VCC", "VSS", "+12", "+5", "+3", "VDD", "VDDQ",
                "VBAT", "VIN", "VRM", "FAN")):
            continue
        candidates.append((c, nid, name))
    if not candidates:
        return None
    # Take a mid-sized one.
    candidates.sort()
    pick = candidates[len(candidates) // 2]
    return pick[2]


def _check_distinctive_net(graph: TraceGraph, name: str) -> None:
    """Verify endpoints on this net actually connect (single component)."""
    nid = graph.net_id_by_name(name)
    if nid is None:
        print(f"      ({name}) NOT FOUND")
        return
    sizes = _component_sizes_for_net(graph, nid)
    pads = graph.pads_on_net(nid)
    n_comp = len(sizes)
    print(f"      Distinctive: {name!r}  net id {nid:>4d}  pads={len(pads)}  "
          f"components={n_comp}")
    # If one component, prove pads are interconnected: pick a pad ON
    # THIS NET (the first one in the list) and check connected_pads
    # contains every other pad.
    if pads:
        first_pad = pads[0]
        reach = set(graph.connected_pads(first_pad.pad_id))
        same_net_reach = sum(1 for p in pads if p.pad_id in reach)
        print(f"        from pad {first_pad.pad_id} "
              f"(at {first_pad.x},{first_pad.y},{first_pad.layer}): "
              f"reaches {same_net_reach}/{len(pads)} pads on this net")


def _check_via_layer_bridging(graph: TraceGraph) -> None:
    """Confirm we have at least some components that span both layers.
    A board with vias should have many cross-layer components — if all
    components are single-layer, vias never fired."""
    comp_layers: Dict[int, set] = defaultdict(set)
    for nid in range(len(graph._node_xy)):
        root = graph._uf.find(nid)
        comp_layers[root].add(graph._node_layer[nid])
    cross = sum(1 for layers in comp_layers.values() if len(layers) > 1)
    print(f"    components spanning both layers: {cross} "
          f"(of {len(comp_layers)})")


def run_one(label: str, path: str, endpoint_tol: int = 50) -> None:
    print(f"\n=== {label}  ({path}) ===")
    t0 = time.time()
    graph = TraceGraph.from_file(path, endpoint_tol=endpoint_tol)
    elapsed = time.time() - t0
    print(f"  build time: {elapsed:.1f} s")

    s = graph.stats()
    print(f"  pads={s['pads']:,}  segments={s['segments']:,}  "
          f"polylines={s['polylines']:,}")
    print(f"  nodes={s['nodes']:,}  components={s['components']:,}  "
          f"biggest={s['biggest_component']:,}")
    print(f"  segments_with_net={s['segments_with_net_pct']:.2f}%   "
          f"polylines_with_net={s['polylines_with_net_pct']:.2f}%")
    print(f"  propagation: changed {s['propagation_changes']:,} nodes, "
          f"{s['propagation_conflicts']} conflicts")
    print(f"  vias bridged: {s['vias_bridged']:,}")
    print(f"  top-10 component sizes: {s['top10_component_sizes']}")

    _check_via_layer_bridging(graph)

    print(f"  Universal-net health:")
    for n in UNIVERSAL_NETS:
        _print_net_health(graph, n)

    print(f"  Distinctive small net check:")
    pick = _find_small_distinctive_net(graph)
    if pick:
        _check_distinctive_net(graph, pick)
    else:
        print(f"      (no candidate net found)")

    # Spatial query smoke test: pick a pad and net_at it.
    if graph.pads:
        sample = graph.pads[len(graph.pads) // 2]
        nid = graph.net_at(sample.x, sample.y, sample.layer, tol=50)
        print(f"  net_at smoke: pad {sample.pad_id} at "
              f"({sample.x},{sample.y},{sample.layer}) -> "
              f"net_id={nid} ({graph.net_name(nid)!r}); "
              f"pad's record net_id={sample.net_id} "
              f"({graph.net_name(sample.net_id)!r})")


def main():
    args = sys.argv[1:]
    if not args or args[0] == "all":
        boards = KNOWN_BOARDS
    else:
        wanted = {a.upper() for a in args}
        boards = [b for b in KNOWN_BOARDS if b[0] in wanted]
    for label, path, *_ in boards:
        run_one(label, path)


if __name__ == "__main__":
    main()
