import json
import unittest
from pathlib import Path
from unittest import mock

import board_export
from src.parsers.gencad_parser import BoardModel, Component


class _CountingShape:
    def __init__(self):
        self.pins = [("1", 0.0, 0.0), ("2", 10.0, 0.0)]
        self.bbox_calls = 0

    def bbox(self):
        self.bbox_calls += 1
        return 0.0, 0.0, 10.0, 0.0


class BoardExportTests(unittest.TestCase):
    def test_units_per_mm_streams_component_bounds(self):
        self.assertIsNone(board_export._units_per_mm(BoardModel()))

        small = BoardModel(components={
            "A": Component("A", 10.0, 20.0),
            "B": Component("B", 20.0, 40.0),
        })
        self.assertEqual(board_export._units_per_mm(small), 39.37)

        large = BoardModel(components={
            "A": Component("A", -30_000.0, 0.0),
            "B": Component("B", 30_001.0, 0.0),
        })
        self.assertEqual(board_export._units_per_mm(large), 3937.0)

    def test_open_board_reuses_shape_bounds_and_preserves_geometry(self):
        shape = _CountingShape()
        model = BoardModel(
            components={
                "U1": Component("U1", 0.0, 0.0, "TOP", 0.0, "S"),
                "U2": Component("U2", 100.0, 100.0, "BOTTOM", 90.0, "S"),
            },
            signals={
                "GND": [("U1", "1"), ("U2", "2")],
                "VCC": [("U1", "2")],
            },
            shapes={"S": shape},
        )
        model.outline_segments = [[(-20.0, -30.0), (130.0, 140.0)]]

        with mock.patch.object(board_export, "parse_board", return_value=model):
            exported = json.loads(board_export._open_board("fixture.cad", None))

        self.assertEqual(shape.bbox_calls, 1)
        self.assertEqual(exported["meta"]["bbox"], [-20.0, -30.0, 130.0, 140.0])
        self.assertEqual(exported["meta"]["units_per_mm"], 39.37)
        self.assertEqual(exported["components"][0]["bbox"], [-5.0, -5.0, 15.0, 5.0])
        self.assertEqual(exported["components"][1]["bbox"], [95.0, 95.0, 105.0, 115.0])
        self.assertEqual(
            [pin["net"] for pin in exported["components"][0]["pins"]],
            [0, 1],
        )
        self.assertEqual(
            [pin["net"] for pin in exported["components"][1]["pins"]],
            [-1, 0],
        )

    def test_serialization_failure_does_not_publish_partial_state(self):
        saved_state = board_export._STATE.copy()
        previous_model = BoardModel()
        board_export._STATE.update({
            "model": previous_model,
            "path": Path("previous.cad"),
            "format": "gencad",
            "nets": ["OLD"],
            "net_index": {"OLD": 0},
        })
        try:
            with (
                mock.patch.object(board_export, "parse_board", return_value=BoardModel()),
                mock.patch.object(board_export.json, "dumps", side_effect=MemoryError),
            ):
                with self.assertRaises(MemoryError):
                    board_export._open_board("next.cad", None)

            self.assertIs(board_export._STATE["model"], previous_model)
            self.assertEqual(board_export._STATE["path"], Path("previous.cad"))
            self.assertEqual(board_export._STATE["nets"], ["OLD"])
        finally:
            board_export._STATE.clear()
            board_export._STATE.update(saved_state)


if __name__ == "__main__":
    unittest.main()
