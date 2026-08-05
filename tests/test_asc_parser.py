import tempfile
import unittest
from pathlib import Path

from src.parsers.asc_parser import parse
from src.parsers import boardview

_BANNER = (
    " TEST-BOARD R1        eM-Test Expert (R)     licence #0 TEST, \n\n"
)

_PARTS = _BANNER + """
 Parts List              0/3 Selected Parts             15-Jun-2006 9:52
                                                        INCH units

Part             X         Y     Rot  Grid  T/B  'Device', 'Outline'

U1             1.000     1.000    0.0  A1   (T)  'BGA_4P|TESTCHIP', 'BGA_4P'
C1             2.000     0.500  180.0  B1   (T)  '0.1UF/25V|MLCC', 'C0603'
R1             0.400     1.500   90.0  A2   (B)  '10KOHM|5%', 'R0603'
"""

_PINS = _BANNER + """
 Part Pins List          0/3 Selected Parts             15-Jun-2006 9:52
                                                        INCH units

Part        T/B
Pin   Name      X         Y     Layer  Net               Nail(s)

Part U1     (T)

   1    A1    0.950     1.050     1    VCC               1
   2    A2    1.050     1.050     1    GND
   3    B1    0.950     0.950     1    DATA0             2
   4    B2    1.050     0.950     1    NC__100

Part C1     (T)

   1    1     2.000     0.525     1    VCC
   2    2     2.000     0.475     1    GND

Part R1     (B)

   1    1     0.400     1.525     2    DATA0
   2    2     0.400     1.475     2    (NC)
"""

_NAILS = _BANNER + """
 Test Fixture Nails    3/3 Selected Drills           15-Jun-2006 9:52
                       3 Nails, 3 Nets               INCH units

Nail         X         Y   Type Grid T/B  Net   Net Name   Virtual Pin/Via

$1         0.100     0.100   1  A1   (B)  #1    VCC              V VIA .
$2         0.200     0.200   2  A1   (B)  #2    DATA0            V PIN U1.3
$3         0.300     0.300   1  A1   (B)  #3    GND              V VIA .
"""

_FORMAT = _BANNER + """
 Board Outline Contour             INCH units               15-Jun-2006 9:52

 User Datum  X  0.000,  Y  0.000,  Rotation   0.0

      X           Y         Radius

    0.000       0.000       0.000
    3.000       0.000       0.000
    3.000       2.000       0.000
    0.000       2.000       0.000
    0.000       0.000       0.000
"""


def _write_set(directory: Path) -> None:
    (directory / "parts.asc").write_text(_PARTS, encoding="utf-8")
    (directory / "pins.asc").write_text(_PINS, encoding="utf-8")
    (directory / "nails.asc").write_text(_NAILS, encoding="utf-8")
    (directory / "format.asc").write_text(_FORMAT, encoding="utf-8")


class AscParserTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        _write_set(self.dir)

    def tearDown(self):
        self._tmp.cleanup()

    def test_parses_from_directory_or_any_member(self):
        for target in (self.dir, self.dir / "pins.asc", self.dir / "nets.asc"):
            if target.name == "nets.asc":
                continue  # not written; directory itself must still work
            model = parse(target)
            self.assertIn("U1", model.components)

    def test_components_sides_and_device(self):
        model = parse(self.dir)
        # 3 parts + 2 via-nails ($1, $3; $2 targets a PIN and is skipped)
        self.assertEqual(len(model.components), 5)
        self.assertEqual(model.components["U1"].layer, "TOP")
        self.assertEqual(model.components["R1"].layer, "BOTTOM")
        self.assertEqual(model.components["C1"].device, "0.1UF/25V|MLCC")
        self.assertIn("$1", model.components)
        self.assertIn("$3", model.components)
        self.assertNotIn("$2", model.components)

    def test_units_scaled_to_mils(self):
        model = parse(self.dir)
        u1 = model.components["U1"]
        self.assertAlmostEqual(u1.x, 1000.0)
        self.assertAlmostEqual(u1.y, 1000.0)

    def test_pin_offsets_relative_to_centroid(self):
        model = parse(self.dir)
        shape = model.shapes[model.components["U1"].shape]
        self.assertEqual(len(shape.pins), 4)
        offsets = {name: (x, y) for name, x, y in shape.pins}
        self.assertAlmostEqual(offsets["A1"][0], -50.0)
        self.assertAlmostEqual(offsets["A1"][1], 50.0)

    def test_signals_exclude_no_connects(self):
        model = parse(self.dir)
        self.assertNotIn("NC__100", model.signals)
        self.assertNotIn("(NC)", model.signals)
        # VCC: U1.A1, C1.1, plus via-nail $1
        self.assertEqual(
            sorted(model.signals["VCC"]),
            [("$1", "1"), ("C1", "1"), ("U1", "A1")],
        )
        self.assertEqual(
            sorted(model.signals["DATA0"]),
            [("R1", "1"), ("U1", "B1")],
        )

    def test_outline_segments_closed_rectangle(self):
        model = parse(self.dir)
        segs = model.outline_segments
        self.assertEqual(len(segs), 4)
        self.assertEqual(segs[0], ((0.0, 0.0), (3000.0, 0.0)))

    def test_dispatcher_routes_asc_and_directories(self):
        model = boardview.parse(self.dir / "parts.asc")
        self.assertIn("U1", model.components)
        model = boardview.parse(self.dir)
        self.assertIn("U1", model.components)

    def test_missing_required_members_raises(self):
        with tempfile.TemporaryDirectory() as other:
            lone = Path(other) / "parts.asc"
            lone.write_text(_PARTS, encoding="utf-8")
            with self.assertRaises(ValueError):
                parse(lone)


def _mini_set(prefix: str, refdes: str) -> dict:
    parts = _BANNER + f"""
 Parts List              0/1 Selected Parts             15-Jun-2006 9:52
                                                        INCH units

Part             X         Y     Rot  Grid  T/B  'Device', 'Outline'

{refdes}           1.000     1.000    0.0  A1   (T)  'DEV', 'OUT'
"""
    pins = _BANNER + f"""
 Part Pins List          0/1 Selected Parts             15-Jun-2006 9:52
                                                        INCH units

Part {refdes}     (T)

   1    1     1.000     1.050     1    NET_{refdes}
"""
    name = (prefix + "_") if prefix else ""
    return {f"{name}parts.asc": parts, f"{name}pins.asc": pins}


class AscMultiBoardDirectoryTests(unittest.TestCase):
    """Regression tests: a directory holding several boards' flattened
    exports must never load or splice the wrong board's files."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        for prefix, refdes in (("boarda", "AAAA"), ("boardb", "BBBB")):
            for fname, text in _mini_set(prefix, refdes).items():
                (self.dir / fname).write_text(text, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_opened_file_selects_its_own_board(self):
        model = parse(self.dir / "boardb_pins.asc")
        self.assertEqual(list(model.components), ["BBBB"])
        model = parse(self.dir / "boarda_parts.asc")
        self.assertEqual(list(model.components), ["AAAA"])

    def test_no_splicing_across_prefix_groups(self):
        # boarda has parts+pins; an unrelated zboard contributes only
        # nails — it must NOT be grafted onto boarda's model.
        (self.dir / "zboard_nails.asc").write_text(_NAILS, encoding="utf-8")
        model = parse(self.dir / "boarda_parts.asc")
        self.assertEqual(list(model.components), ["AAAA"])

    def test_directory_open_prefers_bare_names(self):
        _write_set(self.dir)  # adds the canonical bare parts/pins/... set
        model = parse(self.dir)
        self.assertIn("U1", model.components)
        self.assertNotIn("AAAA", model.components)


class AscEdgeCaseTests(unittest.TestCase):
    def test_nail_on_pin_of_via_named_component_is_skipped(self):
        nails = _BANNER + """
 Test Fixture Nails    2/2 Selected Drills           15-Jun-2006 9:52
                       2 Nails, 2 Nets               INCH units

Nail         X         Y   Type Grid T/B  Net   Net Name   Virtual Pin/Via

$1         0.100     0.100   1  A1   (B)  #1    REALVIA          V VIA .
$2         0.200     0.200   1  A1   (B)  #2    PINNET           V PIN VIA1.3
"""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for fname, text in _mini_set("", "VIA1").items():
                (d / fname).write_text(text, encoding="utf-8")
            (d / "nails.asc").write_text(nails, encoding="utf-8")
            model = parse(d)
            # $1 targets a via -> synthetic component; $2 targets pin
            # VIA1.3 -> must be skipped despite "VIA" in the refdes.
            self.assertIn("$1", model.components)
            self.assertNotIn("$2", model.components)

    def test_mm_units_with_padded_header(self):
        files = _mini_set("", "U9")
        files["parts.asc"] = files["parts.asc"].replace(
            "INCH units", "MM   UNITS")
        files["pins.asc"] = files["pins.asc"].replace(
            "INCH units", "MM   UNITS")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for fname, text in files.items():
                (d / fname).write_text(text, encoding="utf-8")
            model = parse(d)
            u9 = model.components["U9"]
            # 1.0 "mm" -> 39.37 mils, not 1000 (inch scaling)
            self.assertAlmostEqual(u9.x, 39.37, places=2)

    def test_net_literally_named_nc_is_kept(self):
        files = _mini_set("", "K1")
        files["pins.asc"] = files["pins.asc"].replace("NET_K1", "NC")
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for fname, text in files.items():
                (d / fname).write_text(text, encoding="utf-8")
            model = parse(d)
            self.assertIn("NC", model.signals)


if __name__ == "__main__":
    unittest.main()
