# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""Coverage for src/parsers/boardview.py's FORMATS table: extension
dispatch, directory routing, content sniffing, and the unknown-format
error. Mocks the module-level _parse_* callables rather than touching
real parsers (established pattern in tests/test_parser_io.py) — FORMATS
binds them as late-binding lambdas exactly so tests can do this."""

import unittest
from pathlib import Path
from unittest import mock

from src.parsers import boardview
from src.parsers.gencad_parser import BoardModel


class _PrefixStream:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, size=-1):
        return self.data if size < 0 else self.data[:size]


class _FakePath:
    """Minimal Path stand-in: extension via .suffix, sniffing via .open("rb")."""
    name = "board.unknown"

    def __init__(self, data=b"", suffix="", is_dir=False):
        self._suffix = suffix
        self._is_dir = is_dir
        self.stream = _PrefixStream(data)

    @property
    def suffix(self):
        return self._suffix

    def is_dir(self):
        return self._is_dir

    def open(self, mode):
        assert mode == "rb"
        return self.stream


class FormatTableTests(unittest.TestCase):
    def test_every_ext_in_all_exts_maps_to_a_spec(self):
        for ext in boardview.ALL_EXTS:
            spec = boardview._PARSER_BY_EXT[ext]
            self.assertIn(ext, spec.exts)
            self.assertIsInstance(spec, boardview.FormatSpec)

    def test_all_exts_matches_documented_extensions(self):
        # docs/android_contract.md + module docstring enumerate these;
        # a drifted set here means one of them went stale.
        self.assertEqual(
            boardview.ALL_EXTS,
            frozenset({".cad", ".brd", ".brd2", ".bv", ".tvw", ".fz",
                       ".pcb", ".asc"}),
        )

    def test_parse_dispatches_directories_to_asc_parser(self):
        # parse() does `Path(path)` up front, so use a real Path and mock
        # only is_dir() rather than a fully synthetic PathLike (which
        # pathlib.Path's constructor rejects on 3.12+).
        path = Path("some_asc_dir")
        expected = BoardModel()
        with (
            mock.patch.object(Path, "is_dir", return_value=True),
            mock.patch.object(boardview, "_parse_asc", return_value=expected) as parse,
        ):
            actual = boardview.parse(path)
        self.assertIs(actual, expected)
        parse.assert_called_once_with(path)

    def test_parse_dispatches_known_extension_without_sniffing(self):
        path = Path("fixture.cad")
        expected = BoardModel()
        with (
            mock.patch.object(boardview, "_parse_gencad", return_value=expected) as parse,
            mock.patch.object(boardview, "_sniff_and_parse",
                              side_effect=AssertionError("must not sniff")),
        ):
            actual = boardview.parse(path)
        self.assertIs(actual, expected)
        parse.assert_called_once_with(path)

    def test_parse_forwards_key_only_to_key_accepting_formats(self):
        path = Path("fixture.fz")
        expected = BoardModel()
        with mock.patch.object(boardview, "_parse_fz", return_value=expected) as parse:
            actual = boardview.parse(path, key="deadbeef")
        self.assertIs(actual, expected)
        parse.assert_called_once_with(path, key="deadbeef")

    def test_parse_does_not_forward_key_to_non_key_format(self):
        path = Path("fixture.cad")
        expected = BoardModel()
        with mock.patch.object(boardview, "_parse_gencad", return_value=expected) as parse:
            actual = boardview.parse(path, key="ignored")
        self.assertIs(actual, expected)
        parse.assert_called_once_with(path)

    # ---- sniffing -----------------------------------------------------

    def test_sniff_picks_gencad_from_component_signal_markers(self):
        data = b"$COMPONENTS\n$SIGNALS\n" + b"x" * 20_000
        path = _FakePath(data, suffix="")
        expected = BoardModel()
        with (
            mock.patch.object(boardview, "_verify_xzzpcb", return_value=False),
            mock.patch.object(boardview, "_parse_gencad", return_value=expected) as parse,
        ):
            actual = boardview._sniff_and_parse(path)
        self.assertIs(actual, expected)
        parse.assert_called_once_with(path)

    def test_sniff_picks_brd_from_brdout_marker(self):
        data = b"BRDOUT: some header\n" + b"x" * 8000
        path = _FakePath(data, suffix="")
        expected = BoardModel()
        with (
            mock.patch.object(boardview, "_verify_xzzpcb", return_value=False),
            mock.patch.object(boardview, "_parse_brd", return_value=expected) as parse,
        ):
            actual = boardview._sniff_and_parse(path)
        self.assertIs(actual, expected)
        parse.assert_called_once_with(path)

    def test_sniff_picks_brd_from_var_data_and_format_markers(self):
        data = b"var_data: 1\nFormat: 2\n" + b"x" * 8000
        path = _FakePath(data, suffix="")
        expected = BoardModel()
        with (
            mock.patch.object(boardview, "_verify_xzzpcb", return_value=False),
            mock.patch.object(boardview, "_parse_brd", return_value=expected) as parse,
        ):
            actual = boardview._sniff_and_parse(path)
        self.assertIs(actual, expected)
        parse.assert_called_once_with(path)

    def test_sniff_picks_xzzpcb_from_binary_magic_before_ascii_checks(self):
        # xzzpcb sniffing runs first in FORMATS specifically so an
        # obfuscated binary never falls through to a text heuristic.
        data = b"\x00" * 0x40 + b"$COMPONENTS\n$SIGNALS\n" + b"x" * 8000
        path = _FakePath(data, suffix="")
        expected = BoardModel()
        with (
            mock.patch.object(boardview, "_verify_xzzpcb", return_value=True) as verify,
            mock.patch.object(boardview, "_parse_xzzpcb", return_value=expected) as parse,
            mock.patch.object(boardview, "_parse_gencad",
                              side_effect=AssertionError("must not reach gencad")),
        ):
            actual = boardview._sniff_and_parse(path)
        self.assertIs(actual, expected)
        verify.assert_called_once_with(data[:0x40])
        parse.assert_called_once_with(path, key=None)

    def test_sniff_forwards_key_to_xzzpcb_dispatch(self):
        data = b"\x00" * 0x40 + b"x" * 8000
        path = _FakePath(data, suffix="")
        expected = BoardModel()
        with (
            mock.patch.object(boardview, "_verify_xzzpcb", return_value=True),
            mock.patch.object(boardview, "_parse_xzzpcb", return_value=expected) as parse,
        ):
            actual = boardview._sniff_and_parse(path, key="cafe")
        self.assertIs(actual, expected)
        parse.assert_called_once_with(path, key="cafe")

    def test_sniff_unrecognised_content_raises_value_error_listing_extensions(self):
        data = b"nothing recognisable here" + b"y" * 8000
        path = Path("mystery.unknown")
        with mock.patch.object(Path, "open", return_value=_PrefixStream(data)):
            with mock.patch.object(boardview, "_verify_xzzpcb", return_value=False):
                with self.assertRaises(ValueError) as ctx:
                    boardview._sniff_and_parse(path)
        message = str(ctx.exception)
        self.assertIn("unrecognised boardview format", message)
        for ext in boardview.ALL_EXTS:
            self.assertIn(ext, message)


if __name__ == "__main__":
    unittest.main()
