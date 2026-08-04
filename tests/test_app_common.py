# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""Coverage for src/app_common.py's ConfigStore (JSON round-trip via an
injected path_provider) and pin_sort_key (natural pin ordering). No tk
widgets are instantiated — importing the module is fine (it only uses
tkinter for a message box invoked lazily inside a function), but this
test never calls into tk itself so it stays headless-CI safe."""

import tempfile
import unittest
from pathlib import Path

from src.app_common import ConfigStore, pin_sort_key


class ConfigStoreTests(unittest.TestCase):
    def _store_for(self, tmp_path: Path) -> ConfigStore:
        return ConfigStore(lambda: tmp_path / "config.json")

    def test_load_missing_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._store_for(Path(d))
            self.assertEqual(store.load(), {})

    def test_save_then_load_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            store = self._store_for(Path(d))
            config = {"window": {"w": 800, "h": 600}, "recent": ["a.cad", "b.brd"]}
            store.save(config)
            self.assertEqual(store.load(), config)

    def test_save_creates_missing_parent_directories(self):
        with tempfile.TemporaryDirectory() as d:
            nested = Path(d) / "nested" / "dirs"
            store = ConfigStore(lambda: nested / "config.json")
            store.save({"a": 1})
            self.assertTrue((nested / "config.json").exists())
            self.assertEqual(store.load(), {"a": 1})

    def test_load_corrupt_json_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text("{not valid json", encoding="utf-8")
            store = ConfigStore(lambda: path)
            self.assertEqual(store.load(), {})

    def test_save_failure_is_swallowed(self):
        # path_provider pointing at a directory (not a file) makes
        # write_text() raise IsADirectoryError/OSError — save() must not
        # propagate it (config is a convenience, never worth crashing over).
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStore(lambda: Path(d))  # a directory, not a file
            store.save({"a": 1})  # must not raise

    def test_load_failure_on_permission_style_oserror_returns_empty_dict(self):
        # Simulate an OSError other than FileNotFoundError from the
        # path_provider callable itself (e.g. resolving a bad env path).
        def _boom():
            raise OSError("simulated I/O failure")
        store = ConfigStore(_boom)
        self.assertEqual(store.load(), {})


class PinSortKeyTests(unittest.TestCase):
    def test_numeric_pins_sort_first_and_numerically(self):
        pins = [("10", "N10"), ("2", "N2"), ("1", "N1")]
        ordered = sorted(pins, key=pin_sort_key)
        self.assertEqual([p[0] for p in ordered], ["1", "2", "10"])

    def test_bga_style_pins_group_by_alpha_then_numeric(self):
        pins = [("B1", "x"), ("A2", "x"), ("A1", "x"), ("A10", "x")]
        ordered = sorted(pins, key=pin_sort_key)
        self.assertEqual([p[0] for p in ordered], ["A1", "A2", "A10", "B1"])

    def test_numeric_pins_sort_before_bga_style_pins(self):
        pins = [("A1", "x"), ("3", "x")]
        ordered = sorted(pins, key=pin_sort_key)
        self.assertEqual([p[0] for p in ordered], ["3", "A1"])

    def test_non_matching_pins_fall_back_to_lexicographic(self):
        pins = [("PAD_C", "x"), ("PAD_A", "x"), ("PAD_B", "x")]
        ordered = sorted(pins, key=pin_sort_key)
        self.assertEqual([p[0] for p in ordered], ["PAD_A", "PAD_B", "PAD_C"])

    def test_fallback_group_sorts_after_numeric_and_bga_groups(self):
        pins = [("MOUNT1", "x"), ("A1", "x"), ("5", "x")]
        ordered = sorted(pins, key=pin_sort_key)
        self.assertEqual([p[0] for p in ordered], ["5", "A1", "MOUNT1"])

    def test_bga_suffix_letter_participates_in_ordering(self):
        # Same alpha+digit group, differing trailing suffix letter.
        pins = [("A1B", "x"), ("A1A", "x"), ("A1", "x")]
        ordered = sorted(pins, key=pin_sort_key)
        self.assertEqual([p[0] for p in ordered], ["A1", "A1A", "A1B"])

    def test_lowercase_bga_pin_matches_via_uppercasing(self):
        self.assertEqual(pin_sort_key(("a1", "x")), pin_sort_key(("A1", "x")))


if __name__ == "__main__":
    unittest.main()
