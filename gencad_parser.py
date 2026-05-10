# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""
Parse a GENCAD 1.4 boardview file into a BoardModel for diagnostic use.

Captures:
  $COMPONENTS  -> component instances (refdes, position, layer, rotation, shape)
  $SIGNALS     -> netlist (net name -> [(refdes, pin), ...])
  $SHAPES      -> footprint definitions (pin offsets relative to component
                  origin) — used to render components at their actual size.
  $TRACKS      -> track-id → physical width table (referenced from $ROUTES)
  $ROUTES      -> per-net trace polylines (LINE primitives, layer-tagged) —
                  used to build a TraceGraph for the trace renderer.

Skips $HEADER, $BOARD, $PADS, $PADSTACKS, $DEVICES, $LAYERS, $MECH,
$TESTPINS, $POWERPINS — not needed for the viewer.

In addition to the GENCAD parser proper, this module hosts the common
`BoardModel` dataclass shared by every parser (BRD, GENCAD, TVW).
BoardModel optionally carries a *trace topology* — a `TraceGraph` from
`tvw_topology` — produced lazily on first access via the
`_topology_loader` callable. GENCAD files with a populated $ROUTES
section get a loader; files without (or BRD/empty TVW) leave it None
and `topology_available` reads False, so GUI code can short-circuit
cleanly. Topology build is 3-6 s per board, so loading must stay lazy.
"""

from collections import namedtuple
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# Lightweight pad view used by `find_broken_nets` when iterating the
# numpy-array path. Tuple construction is ~3× faster than the slotted
# `Pad` dataclass — we only need a handful of attributes here, never
# all of Pad's fields, and these instances never leave this function.
_PadView = namedtuple("_PadView", "pad_id x y net_id layer")


# Re-export topology record types so callers don't need a second import.
#
# Direct import would create a cycle (tvw_topology → tvw_parser →
# gencad_parser → tvw_topology), so we defer the actual import via
# `__getattr__`: `from gencad_parser import Pad, Segment, Polyline`
# triggers a one-shot import of `tvw_topology` and resolves to its
# real classes. This costs no time when nobody touches those names.
def __getattr__(name: str):  # PEP 562 module-level __getattr__
    if name in ("TraceGraph", "Pad", "Segment", "Polyline"):
        from tvw_topology import (  # local import — breaks the cycle
            TraceGraph, Pad, Segment, Polyline,
        )
        # Cache on the module so subsequent lookups skip the import.
        import sys
        mod = sys.modules[__name__]
        for n, v in (("TraceGraph", TraceGraph), ("Pad", Pad),
                     ("Segment", Segment), ("Polyline", Polyline)):
            setattr(mod, n, v)
        return locals()[name]
    raise AttributeError(f"module 'gencad_parser' has no attribute {name!r}")


import re as _re

# Power / ground / reference nets that legitimately span the board via
# inner copper planes the TVW format doesn't expose. Reporting these as
# "broken" is a guaranteed false positive — the topology graph never
# saw the plane geometry that ties them together.
#
# Built on top of `tvw_parser._POWER_NET_RE` but more permissive — that
# parser-side regex requires a `\b` word boundary after the prefix,
# which excludes real power rails like `VCC3`, `3VDUAL_PCH`,
# `VDDIO_MEM`, `PM_1VSOC`. Those rails are unmistakably power on the
# observed boards (Z490 / X570 / B550). When in doubt we lean
# permissive: a missed signal-net break is recoverable by looking at
# the schematic, but a flood of false positives drowns the user.
_BROKEN_NET_POWER_RE = _re.compile(
    r"^("
    # Pure GND / VSS family
    r"A?D?GND|VSS|"
    # Generic VCC/VDD/VPP/VBAT/VTT/VREF — match prefix even when it
    # runs straight into a digit (VCC3, VDDQ_2H, VTT_DDR ...).
    # `[_A-Z]?` lets us catch leading-underscore variants (`_VDD18S5`)
    # which appear on some boards.
    r"A?_?VCC[A-Z0-9_]*|A?_?VDD[A-Z0-9_]*|VPP[A-Z0-9_]*|"
    r"VBAT[A-Z0-9_]*|VTT[A-Z0-9_]*|VREF[A-Z0-9_]*|VCORE[A-Z0-9_]*|"
    # Bare V12, V5, V_1V2, V_3V3 — short voltage tags.
    r"V_?\d+(V\d+)?[A-Z0-9_]*|"
    # Numbered voltage prefixes: 3VDUAL_PCH, 5VDUAL_USB, 12VIN, etc.
    r"\+?\d+\.?\d*V[A-Z0-9_]*|"
    # PM_xxx rails on AMD AGESA boards (PM_1VSOC, PM_CLDO12, PM_1V05).
    r"PM_\d+V[A-Z0-9_]*|"
    # Catch-all for x_DUAL / x_AUX / x_MAIN power-rail tags (e.g.
    # 3VDUAL_LAN1 already matched above; this picks up VIN_xxx).
    r"VIN[A-Z0-9_]*"
    r")$",
    _re.I,
)


@dataclass
class BrokenNet:
    """A net that should be one connected component but is split across
    multiple components in the trace graph. Reported by
    `BoardModel.find_broken_nets`.

    Fields:
        net_name                : the net's human-readable name.
        n_pads                  : total pad count on this net (after
                                  multi-net-component filtering).
        n_components            : number of distinct components the
                                  remaining pads fall into. >= 2.
        biggest_component_size  : pad count in the largest component
                                  (the "trunk").
        pads_in_each_component  : pads grouped by component, ordered
                                  largest-component-first. Each entry
                                  is a list of `Pad` records.
    """
    net_name: str
    n_pads: int
    n_components: int
    biggest_component_size: int
    pads_in_each_component: List[List[Any]]


@dataclass
class Component:
    refdes: str
    x: float = 0.0
    y: float = 0.0
    layer: str = "TOP"
    rotation: float = 0.0
    shape: str = ""
    device: str = ""


@dataclass
class Shape:
    """Footprint geometry — a list of pin offsets relative to component
    origin. Used to derive the bounding box / outline of an instance.

    `bbox_override` lets callers (e.g. the BRD2 parser) supply an explicit
    rectangle when the file already provides a part outline; otherwise
    bbox() falls back to the convex extent of pin offsets."""
    name: str
    pins: List[Tuple[str, float, float]] = field(default_factory=list)
    bbox_override: Tuple[float, float, float, float] | None = None

    def bbox(self) -> Tuple[float, float, float, float]:
        if self.bbox_override is not None:
            return self.bbox_override
        if not self.pins:
            return (-1.0, -1.0, 1.0, 1.0)
        xs = [p[1] for p in self.pins]
        ys = [p[2] for p in self.pins]
        return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class BoardModel:
    components: Dict[str, Component] = field(default_factory=dict)
    signals: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    shapes: Dict[str, Shape] = field(default_factory=dict)

    # ---- trace topology (lazy, set by parsers that can supply it) -------
    # `_topology_loader` is a zero-arg callable that builds and returns a
    # `TraceGraph`. Parsers that decode geometry attach one in `parse()`;
    # parsers that can't (BRD, GENCAD) leave it None. The actual graph is
    # cached in `_topology` on first access. Excluded from repr/compare so
    # BoardModel diffs and prints stay readable. See module docstring.
    _topology_loader: Optional[Callable[[], Any]] = field(
        default=None, repr=False, compare=False)
    _topology: Optional[Any] = field(
        default=None, repr=False, compare=False)

    def nets_for_component(self, refdes: str) -> List[Tuple[str, str]]:
        """Return [(net, pin), ...] for the given refdes."""
        return [
            (net, pin)
            for net, nodes in self.signals.items()
            for r, pin in nodes
            if r == refdes
        ]

    def find_signal(self, name: str, fuzzy: bool = True) -> str | None:
        """Return the canonical net name matching `name`. With fuzzy=True,
        try common variations: #-suffix toggle, B-suffix for active-low,
        underscore/hyphen swap, case variants, and a final case-insensitive
        scan over all nets."""
        if not name:
            return None
        if name in self.signals:
            return name
        if not fuzzy:
            return None

        base = name.strip()
        no_hash = base.rstrip("#").rstrip("B")
        variants = {
            base,
            base + "#",
            base.rstrip("#"),
            no_hash,
            no_hash + "#",
            no_hash + "B",
            no_hash + "_N",
            base.replace("_", "-"),
            base.replace("-", "_"),
            base.upper(),
            base.lower(),
        }
        for v in variants:
            if v in self.signals:
                return v

        upper = base.upper()
        for k in self.signals:
            if k.upper() == upper:
                return k
        return None

    # ------------------------------------------------------------------
    # Trace topology accessors. These short-circuit gracefully when no
    # loader was attached (BRD/GENCAD models), so callers can use the
    # same API regardless of which parser produced the model.
    # ------------------------------------------------------------------

    @property
    def topology_available(self) -> bool:
        """True if `topology` can produce *something* — either a real
        routed-trace graph (parser attached a loader) or a synthetic
        ratsnest built from `self.signals`. False only when the model
        has neither a loader nor any pin-net mapping to ratsnest from.

        Cheap: only checks loader presence and signal-dict emptiness;
        doesn't trigger a build."""
        if self._topology_loader is not None:
            return True
        return bool(self.signals)

    @property
    def topology(self):
        """Return the cached topology graph for this board, building it
        on first access. Subsequent accesses return the cached instance.

        Three paths:
          * Real loader attached (TVW today)        -> call the loader.
          * No loader, signals present (CAD/BRD/    -> build a synthetic
            FZ/XZZPCB)                                ratsnest via
                                                      `ratsnest.build_synthetic_topology`.
          * Neither loader nor signals               -> RuntimeError.
            (e.g. an empty/corrupt parse).

        Synthetic builds run 30-80 ms on a typical motherboard; real
        TVW builds run 3-6 s. Either way the result is cached for the
        life of the BoardModel — call `topology_available` first if you
        want to avoid forcing the build.
        """
        if self._topology is not None:
            return self._topology
        if self._topology_loader is not None:
            self._topology = self._topology_loader()
            return self._topology
        if self.signals:
            from ratsnest import build_synthetic_topology
            self._topology = build_synthetic_topology(self)
            return self._topology
        raise RuntimeError(
            "BoardModel has no trace topology loader and no pin-net "
            "mapping (`signals`) to synthesize a ratsnest from. Check "
            "`topology_available` before calling `.topology`."
        )

    # ------------------------------------------------------------------
    # Diagnostic helpers built on top of the topology. All of them
    # short-circuit to a no-op return when no topology is available, so
    # caller code (e.g. the GUI) can call them blind.
    # ------------------------------------------------------------------

    def find_broken_nets(
        self,
        *,
        min_pads: int = 2,
        ignore_power: bool = True,
    ) -> List[BrokenNet]:
        """Return nets that should be one connected component but appear
        split into multiple components in the trace topology — strong
        signal of a broken trace, lifted pad, or cracked via.

        Parameters:
            min_pads     : skip nets with fewer than this many pads
                           (a 1-pad net can't be "broken").
            ignore_power : skip GND / VCC / numeric-volt nets. The TVW
                           format doesn't decode the inner copper layer
                           where power planes live, so power nets show
                           up as fragmented in the topology graph
                           regardless of board health — would always
                           false-positive. Phase 2 noted this; matched
                           with `tvw_parser._POWER_NET_RE`.

        Returns: list of `BrokenNet`, descending by pad count. Empty
        list when no topology is available (e.g. on a BRD-loaded model).

        Note: when a single component on the graph contains pads from
        more than one net (a "conflict" — Phase 2 reported 30-70 of
        these per board, mostly tolerance-driven false unions), the
        pads in that component are excluded from this net's analysis
        rather than counted. Reporting a conflict-merged component as
        a broken-net break would be misleading.
        """
        if not self.topology_available:
            return []
        graph = self.topology

        # Power-rail filter. We use the local `_BROKEN_NET_POWER_RE`
        # (more permissive than the parser-side `_POWER_NET_RE`, which
        # has a word-boundary anchor that misses real rails like VCC3).
        # We OR the parser regex in too so any name the parser flags
        # as power is also skipped here.
        try:
            from tvw_parser import _POWER_NET_RE
        except Exception:
            _POWER_NET_RE = _re.compile(r"^$")  # never matches

        # Single pass over pads: group pads by their effective net (using
        # the propagated node net when the raw pad.net_id is 0), record
        # each pad's UF root, and tally distinct nets per root so we can
        # spot conflict (multi-net) components in O(P) rather than
        # repeating the work per net.
        #
        # We iterate the canonical numpy arrays when available (TVW
        # boards built via the array-direct path), avoiding the cost of
        # materialising 100K Pad dataclass instances. Falls back to the
        # legacy `graph.pads` list for any source that didn't populate
        # `_pad_arrays` (cache-loaded graphs, GENCAD topology builders).
        pads_by_net: Dict[int, List] = {}
        pad_root: Dict[int, int] = {}  # pad_id -> UF root
        root_nets: Dict[int, set] = {}
        pad_arrays = getattr(graph, "_pad_arrays", None)
        if pad_arrays:
            x_arr = pad_arrays["x"].tolist()
            y_arr = pad_arrays["y"].tolist()
            net_arr = pad_arrays["net_id"].tolist()
            pid_arr = pad_arrays["pad_id"].tolist()
            layer_arr = pad_arrays["layer"].tolist()
            # N-layer aware: the topology graph carries its own layer-name
            # table. For 2-layer mobos this is ("TOP", "BOTTOM"); for GPU
            # PCBs it's ("TOP", "BOTTOM", "INNER_1", ...). Fall back to a
            # 2-entry tuple if the graph predates the field (e.g. a v8
            # cache that survived a version skew).
            layer_str = getattr(graph, "_layer_names", None) \
                or ("TOP", "BOTTOM")
            pad_node_get = graph._pad_node.get
            uf_find = graph._uf.find
            node_nets = graph._node_net
            n = len(x_arr)
            for i in range(n):
                pad_id = pid_arr[i]
                node = pad_node_get(pad_id, -1)
                if node < 0:
                    continue
                root = uf_find(node)
                pad_net = net_arr[i]
                node_net = node_nets[node]
                net_for_pad = pad_net if pad_net else node_net
                if not net_for_pad:
                    continue
                # Store a tiny tuple instead of a dataclass — the
                # caller (this function) only needs (pad_id, x, y,
                # layer, net_id) to populate the BrokenNet.
                pad_entry = _PadView(pad_id, x_arr[i], y_arr[i],
                                      pad_net, layer_str[layer_arr[i]])
                pads_by_net.setdefault(net_for_pad, []).append(pad_entry)
                pad_root[pad_id] = root
                root_nets.setdefault(root, set()).add(net_for_pad)
        else:
            for pad in graph.pads:
                node = graph._pad_node.get(pad.pad_id, -1)
                if node < 0:
                    continue
                root = graph._uf.find(node)
                node_net = graph._node_net[node]
                net_for_pad = pad.net_id if pad.net_id else node_net
                if not net_for_pad:
                    continue
                pads_by_net.setdefault(net_for_pad, []).append(pad)
                pad_root[pad.pad_id] = root
                root_nets.setdefault(root, set()).add(net_for_pad)

        out: List[BrokenNet] = []
        # Iterate over self.signals — that's the canonical net list with
        # human-readable names, and matches what the GUI knows about.
        # Some signals from the parser may not have any pads in the
        # topology (e.g. all-pin-1 placeholders); skip those silently.
        for net_name in self.signals:
            if ignore_power and (
                    _BROKEN_NET_POWER_RE.match(net_name)
                    or _POWER_NET_RE.match(net_name)):
                continue
            net_id = graph.net_id_by_name(net_name)
            if net_id is None:
                continue
            pads = pads_by_net.get(net_id)
            if not pads or len(pads) < min_pads:
                continue

            # Group pads by UF root, dropping any pad that lives in a
            # multi-net (conflict) component. See note above.
            comp_pads: Dict[int, List] = {}
            for pad in pads:
                root = pad_root.get(pad.pad_id, -1)
                if root < 0:
                    continue
                if len(root_nets.get(root, ())) > 1:
                    continue  # conflict component; skip
                comp_pads.setdefault(root, []).append(pad)

            if len(comp_pads) <= 1:
                continue  # one component (or zero after filtering) → not broken

            groups = sorted(comp_pads.values(), key=len, reverse=True)
            n_pads_kept = sum(len(g) for g in groups)
            if n_pads_kept < min_pads:
                continue
            out.append(BrokenNet(
                net_name=net_name,
                n_pads=n_pads_kept,
                n_components=len(groups),
                biggest_component_size=len(groups[0]),
                pads_in_each_component=groups,
            ))

        # Biggest first — easiest to investigate.
        out.sort(key=lambda b: (-b.n_pads, -b.n_components))
        return out

    def trace_geometry_for_net(
        self, net_name: str,
    ) -> Tuple[List[Any], List[Any]]:
        """Return (segments, polylines) on the named net for rendering.
        Returns ([], []) if topology isn't available or the net is
        unknown to the topology layer."""
        if not self.topology_available:
            return ([], [])
        graph = self.topology
        net_id = graph.net_id_by_name(net_name)
        if net_id is None:
            return ([], [])
        return graph.geometry_on_net(net_id)

    def net_at_point(
        self,
        x: float,
        y: float,
        layer: str = "TOP",
        tol: float = 100,
    ) -> Optional[str]:
        """Resolve a (layer, x, y) point to the net name at that point.
        Returns None when no net is within `tol` of the point or no
        topology is available. The reverse of "show me where this net
        runs" — used for click-to-identify-net GUI queries.

        Coordinates and tolerance are in the same TVW file units used
        by `Component.x` / `pad.x` etc."""
        if not self.topology_available:
            return None
        graph = self.topology
        net_id = graph.net_at(int(x), int(y), layer=layer, tol=int(tol))
        if not net_id:
            # net_id 0 may legitimately mean "untagged" on Z490/B550 or
            # a real net (GND on X570). graph.net_at returns 0 either
            # way; we treat it as "no answer". A canvas-click hitting
            # a real GND on X570 is rare enough that we live with this.
            return None
        return graph.net_name(net_id)


def parse(path: Path) -> BoardModel:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    model = BoardModel()
    section: str | None = None
    current_component: Component | None = None
    current_signal: str | None = None
    current_shape: Shape | None = None

    # $TRACKS and $ROUTES storage. We accumulate raw line tuples here and
    # only construct the TraceGraph if and when the GUI actually accesses
    # `model.topology` — see `_make_topology_loader` below.
    tracks_widths: Dict[int, float] = {}
    # routes: list of (net_name, track_id, layer, [(x1,y1,x2,y2), ...])
    routes: List[Tuple[str, int, str, List[Tuple[float, float, float, float]]]] = []
    current_route_net: Optional[str] = None
    current_route_track: int = 0
    current_route_layer: str = "TOP"
    current_route_lines: List[Tuple[float, float, float, float]] = []

    def _flush_route_segment():
        # Emit the currently accumulating (net, track, layer, lines) as a
        # route segment if it has any lines.
        nonlocal current_route_lines
        if current_route_net and current_route_lines:
            routes.append((
                current_route_net, current_route_track,
                current_route_layer, current_route_lines,
            ))
        current_route_lines = []

    def _flush_component():
        nonlocal current_component
        if current_component is not None and current_component.refdes:
            model.components[current_component.refdes] = current_component
        current_component = None

    def _flush_shape():
        nonlocal current_shape
        if current_shape is not None and current_shape.name:
            model.shapes[current_shape.name] = current_shape
        current_shape = None

    for raw in lines:
        line = raw.rstrip()
        if not line:
            continue

        # Section transitions
        if line.startswith("$END"):
            _flush_component()
            _flush_shape()
            _flush_route_segment()
            current_signal = None
            current_route_net = None
            section = None
            continue
        if line.startswith("$"):
            _flush_component()
            _flush_shape()
            _flush_route_segment()
            current_signal = None
            current_route_net = None
            section = line[1:].strip()
            continue

        if section == "COMPONENTS":
            tokens = line.split()
            if not tokens:
                continue
            kw = tokens[0]
            if kw == "COMPONENT":
                _flush_component()
                refdes = tokens[1] if len(tokens) > 1 else ""
                current_component = Component(refdes=refdes)
            elif current_component is None:
                continue
            elif kw == "PLACE" and len(tokens) >= 3:
                try:
                    current_component.x = float(tokens[1])
                    current_component.y = float(tokens[2])
                except ValueError:
                    pass
            elif kw == "LAYER" and len(tokens) >= 2:
                current_component.layer = tokens[1]
            elif kw == "ROTATION" and len(tokens) >= 2:
                try:
                    current_component.rotation = float(tokens[1])
                except ValueError:
                    pass
            elif kw == "SHAPE" and len(tokens) >= 2:
                current_component.shape = tokens[1]
            elif kw == "DEVICE" and len(tokens) >= 2:
                current_component.device = tokens[1]

        elif section == "SIGNALS":
            tokens = line.split()
            if not tokens:
                continue
            kw = tokens[0]
            if kw == "SIGNAL":
                # Net name is everything after "SIGNAL"; nets can contain
                # punctuation (#, /, etc.) but rarely whitespace.
                current_signal = line[len("SIGNAL"):].strip()
                model.signals.setdefault(current_signal, [])
            elif kw == "NODE" and current_signal is not None and len(tokens) >= 3:
                refdes = tokens[1]
                pin = tokens[2]
                model.signals[current_signal].append((refdes, pin))

        elif section == "SHAPES":
            tokens = line.split()
            if not tokens:
                continue
            kw = tokens[0]
            if kw == "SHAPE":
                _flush_shape()
                name = line[len("SHAPE"):].strip()
                current_shape = Shape(name=name)
            elif kw == "PIN" and current_shape is not None and len(tokens) >= 5:
                # Format: PIN <name> <pad_type> <x> <y> [layer] [rotation] [...]
                try:
                    pin_name = tokens[1]
                    x = float(tokens[3])
                    y = float(tokens[4])
                    current_shape.pins.append((pin_name, x, y))
                except (ValueError, IndexError):
                    pass

        elif section == "TRACKS":
            # Format: TRACK <id> <width-in-user-units>
            tokens = line.split()
            if len(tokens) >= 3 and tokens[0] == "TRACK":
                try:
                    tracks_widths[int(tokens[1])] = float(tokens[2])
                except (ValueError, IndexError):
                    pass

        elif section == "ROUTES":
            # Format:
            #   ROUTE <net_name>
            #   [VIA <padstack> <x> <y> ALL <num> <id>]*
            #   [TRACK <track_id>
            #    [LAYER <name>
            #     [LINE <x1> <y1> <x2> <y2>]*
            #    ]*
            #   ]*
            tokens = line.split()
            if not tokens:
                continue
            kw = tokens[0]
            if kw == "ROUTE":
                # Net name is everything after the keyword (may contain
                # punctuation but typically no whitespace).
                _flush_route_segment()
                current_route_net = line[len("ROUTE"):].strip()
                current_route_track = 0
                current_route_layer = "TOP"
            elif kw == "TRACK" and len(tokens) >= 2:
                _flush_route_segment()
                try:
                    current_route_track = int(tokens[1])
                except ValueError:
                    pass
            elif kw == "LAYER" and len(tokens) >= 2:
                _flush_route_segment()
                current_route_layer = tokens[1]
            elif kw == "LINE" and len(tokens) >= 5 and current_route_net:
                try:
                    x1 = float(tokens[1])
                    y1 = float(tokens[2])
                    x2 = float(tokens[3])
                    y2 = float(tokens[4])
                    current_route_lines.append((x1, y1, x2, y2))
                except (ValueError, IndexError):
                    pass
            # VIA / ARC / other directives are ignored for now — we only
            # need straight LINE primitives to render trace overlays.

    _flush_component()
    _flush_shape()
    _flush_route_segment()

    # Normalise "flattened" encoding before anything downstream sees the
    # model. Some GENCAD exports (notably ASUS X870E-class) bake the
    # placement into the shape definition: every COMPONENT has PLACE 0 0,
    # and the matching SHAPE's pin coordinates are absolute board coords.
    # Unflattened, all components stack at the origin and nothing renders.
    # See `_normalize_flattened_components` for the detection rule.
    _normalize_flattened_components(model)

    # Lazily attach a topology loader if there's any route data. The
    # heavy work (TraceGraph build) happens on first `model.topology`
    # access; the parser stays fast.
    if routes:
        model._topology_loader = _make_gencad_topology_loader(
            model, tracks_widths, routes,
        )
    return model


def _normalize_flattened_components(model: "BoardModel") -> int:
    """Detect and rewrite components encoded with "flattened" placement.

    Some GENCAD 1.4 exporters bake each component's world placement into
    its shape definition rather than emitting it via `PLACE`. The marker:
    every COMPONENT has `PLACE 0 0`, every component has its own unique
    SHAPE, and the SHAPE's pins carry absolute board coordinates. Loaded
    naively, the result is 6,000+ components stacked at the origin —
    nothing renders.

    Per-component detection. We rewrite a (component, shape) pair when
    ALL of these hold:

      (a) component.x and component.y are both zero (within 1e-6),
      (b) the shape is referenced by exactly one component, AND
      (c) the shape's pin centroid is far from origin (>= 100 file units,
          which is 2.54 mm in mils or 0.025 mm in centi-mils — well
          beyond any plausible relative-coords footprint).

    The rewrite moves the centroid into `component.x` / `component.y`
    and subtracts the centroid from each pin so the shape ends up in
    the standard component-relative form. After this pass, downstream
    code (renderer / topology / measurement tool) sees a model that
    looks identical to a normally-encoded GENCAD file.

    Per-component instead of file-wide so mixed encodings (some flat,
    some normal) parse correctly. Returns the count of rewritten pairs.
    """
    from collections import Counter
    if not model.components or not model.shapes:
        return 0
    shape_refcount: Counter = Counter(
        c.shape for c in model.components.values() if c.shape
    )
    NEAR_ZERO = 1e-6
    FAR_FROM_ORIGIN_SQ = 100.0 * 100.0
    n_normalized = 0
    rewritten_shapes: set = set()
    for comp in model.components.values():
        if abs(comp.x) > NEAR_ZERO or abs(comp.y) > NEAR_ZERO:
            continue
        if not comp.shape or shape_refcount.get(comp.shape, 0) != 1:
            continue
        if comp.shape in rewritten_shapes:
            # Defence in depth: a duplicate refcount==1 hit shouldn't
            # happen, but if it did we'd double-shift the pins.
            continue
        shape = model.shapes.get(comp.shape)
        if shape is None or not shape.pins:
            continue
        xs = [p[1] for p in shape.pins]
        ys = [p[2] for p in shape.pins]
        # Bounding-box centre — robust to lopsided pin counts (a 100-pin
        # connector with most pins on one side would skew an arithmetic
        # mean toward the dense side, but the bbox centre stays put).
        cx = (min(xs) + max(xs)) / 2.0
        cy = (min(ys) + max(ys)) / 2.0
        if (cx * cx + cy * cy) < FAR_FROM_ORIGIN_SQ:
            # Pin centroid sits near origin. Looks like a normal
            # relative-coords footprint that just happens to be referenced
            # once. Leave it alone.
            continue
        comp.x = cx
        comp.y = cy
        shape.pins = [
            (name, x - cx, y - cy) for name, x, y in shape.pins
        ]
        rewritten_shapes.add(comp.shape)
        n_normalized += 1
    return n_normalized


def _make_gencad_topology_loader(
    model: "BoardModel",
    tracks_widths: Dict[int, float],
    routes: List[Tuple[str, int, str,
                       List[Tuple[float, float, float, float]]]],
) -> Callable[[], Any]:
    """Return a zero-arg callable that builds and returns a TraceGraph
    from the parsed GENCAD route data. Captures `model`, `tracks_widths`,
    and `routes` by closure; called on demand from `BoardModel.topology`.
    """
    def _build() -> Any:
        # Local import to avoid the module-load cycle (see __getattr__).
        from tvw_topology import TraceGraph, Pad, Segment

        # GENCAD coordinates are float user-units (typically mils, given
        # `UNITS USER 1000` = 1000 user-units per inch). The canvas
        # renders components at their original float-mil coords, so
        # topology coords MUST share that space for traces to land on
        # the right components. We round to int mils — sub-mil precision
        # doesn't matter for the trace overlay (typical trace pitch is
        # 5 mil+) and ints play nicely with SpatialHash / UnionFind
        # which assume integer cell keys.

        # Net-name → numeric net_id. Keep the same ordering as
        # `model.signals` so net_ids are stable across reruns.
        net_names = list(model.signals.keys())
        net_id_by_name = {n: i for i, n in enumerate(net_names)}

        # Build segments from $ROUTES.
        segments: List[Any] = []
        seg_id = 0
        for net_name, track_id, layer, route_lines in routes:
            net_id = net_id_by_name.get(net_name, 0)
            width_user = tracks_widths.get(track_id, 0.0)
            width = int(round(width_user))
            for x1, y1, x2, y2 in route_lines:
                segments.append(Segment(
                    seg_id=seg_id,
                    x1=int(round(x1)),
                    y1=int(round(y1)),
                    x2=int(round(x2)),
                    y2=int(round(y2)),
                    net_id=net_id,
                    layer=layer,
                    width=width,
                ))
                seg_id += 1

        # Build pads from each component's pin world positions, looking
        # up the net via $SIGNALS. Same units as the segments so endpoint
        # dedup can fuse them.
        import math as _math
        pin_to_net: Dict[Tuple[str, str], str] = {}
        for n_name, nodes in model.signals.items():
            for refdes, pin in nodes:
                pin_to_net[(refdes, pin)] = n_name

        pads: List[Any] = []
        pad_id = 0
        for refdes, comp in model.components.items():
            shape = model.shapes.get(comp.shape) if comp.shape else None
            if not shape or not shape.pins:
                continue
            theta = _math.radians(comp.rotation)
            ct, st = _math.cos(theta), _math.sin(theta)
            comp_layer = comp.layer or "TOP"
            for pin_name, dx, dy in shape.pins:
                wx = comp.x + dx * ct - dy * st
                wy = comp.y + dx * st + dy * ct
                net_name = pin_to_net.get((refdes, pin_name), "")
                net_id = net_id_by_name.get(net_name, 0)
                pads.append(Pad(
                    pad_id=pad_id,
                    x=int(round(wx)),
                    y=int(round(wy)),
                    net_id=net_id,
                    layer=comp_layer,
                    pad_type=0,
                    stride=38,
                ))
                pad_id += 1

        # Construct the TraceGraph. Tolerances are in mils: 1 mil is
        # roughly the smallest meaningful endpoint spacing on a PCB,
        # and a typical trace pitch is 5+ mils.
        #
        # `pads` / `segments` / `polylines` are no longer dataclass
        # kwargs — they're @property accessors backed by lazy caches.
        # We assign through the setters so the lists are stored in
        # `_pads_cache` / `_segs_cache` / `_polys_cache`. `_build_native`
        # sees `_pad_arrays is None` and falls back to the legacy
        # list-of-tuples path, iterating these caches via `self.pads`.
        tg = TraceGraph(
            net_names=net_names,
            endpoint_tol=1,           # 1 mil
            via_tol=1,                # 1 mil — pads/vias on same node
            same_net_pad_tol=10,      # 10 mil — bridge multi-pad pins
            pad_to_trace_tol=2,       # 2 mil — pad↔trace fusion
        )
        tg.pads = pads
        tg.segments = segments
        tg.polylines = []
        tg._build()
        return tg

    return _build


def _summary(model: BoardModel) -> str:
    n_top = sum(1 for c in model.components.values() if c.layer == "TOP")
    n_bot = sum(1 for c in model.components.values() if c.layer == "BOTTOM")
    n_total_nodes = sum(len(v) for v in model.signals.values())
    return (
        f"Components: {len(model.components)} ({n_top} TOP, {n_bot} BOTTOM)\n"
        f"Signals:    {len(model.signals)}\n"
        f"Nodes:      {n_total_nodes}"
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        sys.exit("Usage: python gencad_parser.py <file.cad>")

    model = parse(Path(sys.argv[1]))
    print(_summary(model))
    print()

    # Spot-check: a handful of common rails / reset signals on a typical
    # Sandy Bridge / H61 board. Just exercises `find_signal`'s fuzzy match.
    print("Net lookup:")
    for net in [
        "VCCRTC", "RSMRST#", "RSMRST", "PWRBTN#", "VCCDSW3_3",
        "CPU_VTT", "VCC_DDR", "VTT_DDR", "PLT_RST#", "PLTRST#", "PLTRST",
        "SLP_S3#", "SLP_S5#", "PWROK", "SYS_PWROK", "CPU_PWRGD",
    ]:
        canonical = model.find_signal(net)
        if canonical is None:
            print(f"  {net!r:20s} -> not found")
        else:
            nodes = model.signals[canonical]
            print(f"  {net!r:20s} -> {canonical!r}: {len(nodes)} nodes — {nodes[:3]}")

    print()
    print("Sample components:")
    for refdes in list(model.components.keys())[:5]:
        c = model.components[refdes]
        print(f"  {c.refdes}: {c.layer} ({c.x:.2f}, {c.y:.2f}) "
              f"rot={c.rotation} shape={c.shape} dev={c.device}")
