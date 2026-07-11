import math
import unittest
from unittest import mock

from src import ratsnest
from src.parsers.gencad_parser import BoardModel, Component, Shape


def _segment_tuple(segment):
    return (
        segment.seg_id,
        segment.x1,
        segment.y1,
        segment.x2,
        segment.y2,
        segment.net_id,
        segment.layer,
        segment.width,
        segment.dashed,
    )


class _CountingPins(list):
    def __init__(self, values):
        super().__init__(values)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


class RatsnestPinIndexTests(unittest.TestCase):
    def test_output_order_and_duplicate_first_match_are_preserved(self):
        shape = Shape(
            "S",
            pins=[
                ("DUP", 1.0, 2.0),
                ("DUP", 100.0, 200.0),
                ("P2", -3.0, 4.0),
            ],
        )
        model = BoardModel(
            components={
                "U1": Component("U1", 10.0, 20.0, "TOP", 0.0, "S"),
                "U2": Component("U2", 100.0, 200.0, "BOTTOM", 90.0, "S"),
                "U3": Component("U3", -50.0, -60.0, "TOP", 180.0, "S"),
            },
            signals={
                "CROSS": [("U1", "DUP"), ("U2", "DUP")],
                "SAME": [("U1", "P2"), ("U3", "P2")],
                "MISSING": [("U1", "missing"), ("U2", "DUP")],
                "ONE": [("U1", "DUP")],
            },
            shapes={"S": shape},
        )

        # The legacy resolver used next(...), so this explicitly guards the
        # first duplicate's coordinates before comparing the full graph.
        self.assertEqual(
            ratsnest._pin_world_xy(model.components["U1"], shape, "DUP"),
            (11.0, 22.0),
        )

        graph = ratsnest.build_synthetic_topology(model)

        self.assertEqual(graph.net_names, ["", "CROSS", "SAME"])
        self.assertEqual(
            [_segment_tuple(segment) for segment in graph.segments],
            [
                (0, 11, 22, 98, 201, 1, "TOP", 0, True),
                (1, 11, 22, 98, 201, 1, "BOTTOM", 0, True),
                (2, 7, 24, -47, -64, 2, "TOP", 0, False),
            ],
        )

    def test_shape_index_and_component_transforms_are_reused(self):
        pins = _CountingPins([
            ("1", 1.0, 0.0),
            ("2", 2.0, 0.0),
            ("3", 3.0, 0.0),
        ])
        shape = Shape("S", pins=pins)
        model = BoardModel(
            components={
                "U1": Component("U1", 0.0, 0.0, "TOP", 45.0, "S"),
                "U2": Component("U2", 100.0, 0.0, "TOP", 135.0, "S"),
            },
            signals={
                "N1": [("U1", "1"), ("U2", "1")],
                "N2": [("U1", "2"), ("U2", "2")],
                "N3": [("U1", "3"), ("U2", "3")],
            },
            shapes={"S": shape},
        )

        real_cos = math.cos
        real_sin = math.sin
        with (
            mock.patch.object(ratsnest.math, "cos", wraps=real_cos) as cos,
            mock.patch.object(ratsnest.math, "sin", wraps=real_sin) as sin,
        ):
            graph = ratsnest.build_synthetic_topology(model)

        self.assertEqual(len(graph.segments), 3)
        self.assertEqual(pins.iterations, 1)
        self.assertEqual(cos.call_count, 2)
        self.assertEqual(sin.call_count, 2)


if __name__ == "__main__":
    unittest.main()
