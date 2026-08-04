# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""Coverage for board_export.load_traces()/_load_traces() — the Android
``load_traces`` wire format (docs/android_contract.md SS load_traces
result). These fakes mirror only the TraceGraph surface that
_load_traces actually reads (`net_names`, `_seg_arrays`/`segments`,
`_layer_names`, `vias`, `is_synthetic`) — see src/tvw_topology.py for
the real shape."""

import json
import unittest
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# numpy is an optional dependency of the codebase; the numpy-fast-path
# assertions below need it, but its absence must skip this module rather
# than break collection of the whole suite.
try:
    import numpy as np
except ImportError:  # pragma: no cover
    raise unittest.SkipTest("numpy not installed")

import board_export
from src.parsers.gencad_parser import BoardModel


@dataclass
class _FakeSegment:
    x1: float
    y1: float
    x2: float
    y2: float
    layer: str
    net_id: int
    width: float = 0.0


@dataclass
class _FakeVia:
    x: float
    y: float
    net_id: int


@dataclass
class _FakeTopology:
    """Stand-in for tvw_topology.TraceGraph exposing only what
    board_export._load_traces reads."""
    net_names: List[str] = field(default_factory=list)
    _seg_arrays: Optional[Dict[str, Any]] = None
    _layer_names: List[str] = field(default_factory=list)
    segments: List[_FakeSegment] = field(default_factory=list)
    vias: List[_FakeVia] = field(default_factory=list)
    is_synthetic: bool = False


def _install_topology(model: BoardModel, topo: _FakeTopology) -> None:
    # BoardModel.topology_available is True whenever a loader is attached
    # (gencad_parser.py: `_topology_loader is not None`); .topology calls
    # it once and caches the result.
    model._topology_loader = lambda: topo


class LoadTracesTests(unittest.TestCase):
    def setUp(self):
        self._saved_state = board_export._STATE.copy()

    def tearDown(self):
        board_export._STATE.clear()
        board_export._STATE.update(self._saved_state)

    def _set_state(self, model: BoardModel, fmt: str = "gencad",
                    nets: Optional[List[str]] = None) -> None:
        nets = nets if nets is not None else []
        board_export._STATE.update({
            "model": model,
            "path": None,
            "format": fmt,
            "nets": nets,
            "net_index": {n: i for i, n in enumerate(nets)},
        })

    # ---- no board / no topology fallbacks --------------------------------

    def test_no_board_loaded_returns_documented_failure_shape(self):
        board_export._STATE.clear()
        board_export._STATE.update({
            "model": None, "path": None, "format": "?",
            "nets": None, "net_index": None,
        })
        result = json.loads(board_export.load_traces())
        self.assertEqual(result, {
            "ok": False,
            "error": "parse_error",
            "reason": "no board loaded (call open_board first)",
            "format": "?",
        })

    def test_no_topology_available_returns_documented_failure_shape(self):
        # An empty BoardModel has no loader and no signals -> topology_available
        # is False (gencad_parser.BoardModel.topology_available).
        model = BoardModel()
        self.assertFalse(model.topology_available)
        self._set_state(model, fmt="brd")
        result = json.loads(board_export.load_traces())
        self.assertEqual(result, {
            "ok": False,
            "error": "parse_error",
            "reason": "no trace topology available for this board",
            "format": "brd",
        })

    def test_load_traces_wraps_unexpected_exceptions_in_failure_shape(self):
        model = BoardModel()
        _install_topology(model, _FakeTopology())

        def _boom():
            raise RuntimeError("kaboom")
        model._topology_loader = _boom
        self._set_state(model, fmt="tvw")

        result = json.loads(board_export.load_traces())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "parse_error")
        self.assertIn("kaboom", result["reason"])
        self.assertEqual(result["format"], "tvw")

    # ---- numpy fast path (_seg_arrays) -------------------------------------

    def test_numpy_fast_path_matches_contract_shape_and_net_filtering(self):
        model = BoardModel()
        topo = _FakeTopology(
            net_names=["GND", "STRAY"],  # STRAY is unknown to open_board's nets
            _seg_arrays={
                "x1": np.array([0.0, 10.0]),
                "y1": np.array([0.0, 10.0]),
                "x2": np.array([5.0, 20.0]),
                "y2": np.array([5.0, 20.0]),
                "layer": np.array([0, 1], dtype=np.uint8),
                "net_id": np.array([0, 1], dtype=np.int32),
                "width": np.array([1.5, 0.0]),
            },
            _layer_names=[],  # no layer table -> historical 2-layer encoding
            is_synthetic=False,
        )
        _install_topology(model, topo)
        # open_board's own net table only knows GND; STRAY must map to -1
        # (contract: "net" is -1 unknown).
        self._set_state(model, fmt="tvw", nets=["GND"])

        result = json.loads(board_export.load_traces())

        self.assertEqual(result["ok"], True)
        self.assertEqual(result["synthetic"], False)
        self.assertEqual(result["layers"], ["TOP", "BOTTOM"])
        self.assertEqual(
            set(result["segments"].keys()),
            {"x1", "y1", "x2", "y2", "layer", "net", "width"},
        )
        self.assertEqual(result["segments"]["x1"], [0.0, 10.0])
        self.assertEqual(result["segments"]["y1"], [0.0, 10.0])
        self.assertEqual(result["segments"]["x2"], [5.0, 20.0])
        self.assertEqual(result["segments"]["y2"], [5.0, 20.0])
        # byte 0 -> TOP(0), anything else -> BOTTOM(1) (historical encoding).
        self.assertEqual(result["segments"]["layer"], [0, 1])
        self.assertEqual(result["segments"]["net"], [0, -1])
        self.assertEqual(result["segments"]["width"], [1.5, 0.0])
        self.assertEqual(result["vias"], {"x": [], "y": [], "net": []})
        # Compact separators: no space after ',' or ':' anywhere in the
        # payload (contract requires json.dumps(..., separators=(",", ":"))).
        raw = board_export.load_traces()
        self.assertNotIn(", ", raw)
        self.assertNotIn(": ", raw)

    def test_numpy_fast_path_layer_table_appends_inner_layers(self):
        model = BoardModel()
        topo = _FakeTopology(
            net_names=["GND"],
            _seg_arrays={
                "x1": np.array([0.0]),
                "y1": np.array([0.0]),
                "x2": np.array([1.0]),
                "y2": np.array([1.0]),
                "layer": np.array([2], dtype=np.uint8),
                "net_id": np.array([0], dtype=np.int32),
            },
            _layer_names=["TOP", "BOTTOM", "In1"],
            is_synthetic=False,
        )
        _install_topology(model, topo)
        self._set_state(model, fmt="tvw", nets=["GND"])

        result = json.loads(board_export.load_traces())
        # layers list REPLACES the open_board one and is a superset
        # (contract: ["TOP", "BOTTOM", "In1", ...]).
        self.assertEqual(result["layers"], ["TOP", "BOTTOM", "In1"])
        self.assertEqual(result["segments"]["layer"], [2])
        # No explicit "width" array on the fake -> zero-filled, contract
        # default ("width": 0 = hairline).
        self.assertEqual(result["segments"]["width"], [0])

    def test_numpy_fast_path_out_of_range_layer_byte_falls_back_to_top(self):
        model = BoardModel()
        topo = _FakeTopology(
            net_names=["GND"],
            _seg_arrays={
                "x1": np.array([0.0]),
                "y1": np.array([0.0]),
                "x2": np.array([1.0]),
                "y2": np.array([1.0]),
                "layer": np.array([200], dtype=np.uint8),  # out of range
                "net_id": np.array([0], dtype=np.int32),
            },
            _layer_names=["TOP", "BOTTOM"],
        )
        _install_topology(model, topo)
        self._set_state(model, fmt="tvw", nets=["GND"])

        result = json.loads(board_export.load_traces())
        self.assertEqual(result["segments"]["layer"], [0])

    def test_numpy_fast_path_out_of_range_net_id_maps_to_minus_one(self):
        model = BoardModel()
        topo = _FakeTopology(
            net_names=["GND"],
            _seg_arrays={
                "x1": np.array([0.0]),
                "y1": np.array([0.0]),
                "x2": np.array([1.0]),
                "y2": np.array([1.0]),
                "layer": np.array([0], dtype=np.uint8),
                "net_id": np.array([99], dtype=np.int32),  # out of net_map range
            },
        )
        _install_topology(model, topo)
        self._set_state(model, fmt="tvw", nets=["GND"])

        result = json.loads(board_export.load_traces())
        self.assertEqual(result["segments"]["net"], [-1])

    # ---- legacy dataclass path (topo.segments) -----------------------------

    def test_legacy_dataclass_path_matches_contract_shape(self):
        model = BoardModel()
        topo = _FakeTopology(
            net_names=["GND", "VCC"],
            _seg_arrays=None,  # forces the legacy per-segment loop
            segments=[
                _FakeSegment(0.0, 0.0, 1.0, 1.0, "TOP", 0, width=2.0),
                _FakeSegment(2.0, 2.0, 3.0, 3.0, "In2", 1),
            ],
            vias=[_FakeVia(5.0, 5.0, 0)],
            is_synthetic=True,
        )
        _install_topology(model, topo)
        self._set_state(model, fmt="tvw-compal", nets=["GND", "VCC"])

        result = json.loads(board_export.load_traces())

        self.assertTrue(result["synthetic"])
        self.assertEqual(result["layers"], ["TOP", "BOTTOM", "In2"])
        self.assertEqual(result["segments"]["layer"], [0, 2])
        self.assertEqual(result["segments"]["net"], [0, 1])
        self.assertEqual(result["segments"]["width"], [2.0, 0.0])
        self.assertEqual(result["vias"], {"x": [5.0], "y": [5.0], "net": [0]})

    def test_legacy_dataclass_path_unknown_net_id_maps_to_minus_one(self):
        model = BoardModel()
        topo = _FakeTopology(
            net_names=["GND"],
            segments=[_FakeSegment(0.0, 0.0, 1.0, 1.0, "BOTTOM", 7)],
        )
        _install_topology(model, topo)
        self._set_state(model, fmt="tvw", nets=["GND"])

        result = json.loads(board_export.load_traces())
        self.assertEqual(result["segments"]["net"], [-1])

    def test_synthetic_ratsnest_via_signals_only_model(self):
        # GENCAD/BRD/FZ/XZZ models have no _topology_loader; a non-empty
        # `signals` dict is enough for topology_available to report True
        # (BoardModel.topology builds a synthetic ratsnest on access).
        from src.parsers.gencad_parser import Component
        model = BoardModel(
            components={"U1": Component("U1", 0.0, 0.0)},
            signals={"GND": [("U1", "1")]},
        )
        self.assertTrue(model.topology_available)
        # Replace the synthetic-build path with our own fake so the test
        # doesn't depend on ratsnest's MST internals.
        model._topology = _FakeTopology(
            net_names=["GND"],
            segments=[_FakeSegment(0.0, 0.0, 1.0, 1.0, "TOP", 0)],
            is_synthetic=True,
        )
        self._set_state(model, fmt="gencad", nets=["GND"])

        result = json.loads(board_export.load_traces())
        self.assertTrue(result["ok"])
        self.assertTrue(result["synthetic"])
        self.assertEqual(result["segments"]["net"], [0])


if __name__ == "__main__":
    unittest.main()
