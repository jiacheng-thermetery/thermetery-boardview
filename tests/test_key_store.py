import json
import os
import tempfile
import unittest
from pathlib import Path

import board_export
from src import key_store
from src.parsers import fz_parser


def _valid_fz_key_words():
    # Synthesize a parity-valid FZ key straight from the parser's parity
    # table (no real secret): _validate_fz_key wants (~parity(word)) & 1 to
    # equal _RC6_PARITY[i], so word 0 (even popcount) satisfies expected 1
    # and word 1 (odd popcount) satisfies expected 0.
    return [0 if expected == 1 else 1 for expected in fz_parser._RC6_PARITY]


def _fz_text(words):
    return " ".join(f"{w:08x}" for w in words)


class ValidateKeyTextTests(unittest.TestCase):
    def test_fz_valid(self):
        status, _ = key_store.validate_key_text("fz", _fz_text(_valid_fz_key_words()))
        self.assertEqual(status, "valid")

    def test_fz_invalid_parity(self):
        words = _valid_fz_key_words()
        words[0] ^= 1  # flip one bit -> parses as 44 words but fails parity
        status, _ = key_store.validate_key_text("fz", _fz_text(words))
        self.assertEqual(status, "invalid")

    def test_fz_malformed(self):
        status, _ = key_store.validate_key_text("fz", "not a key")
        self.assertEqual(status, "malformed")

    def test_xzz_unverified(self):
        status, _ = key_store.validate_key_text("xzz", "0123456789abcdef")
        self.assertEqual(status, "unverified")

    def test_xzz_malformed(self):
        self.assertEqual(key_store.validate_key_text("xzz", "")[0], "malformed")

    def test_format_aliases(self):
        for alias in ("xzz", "pcb", "xzzpcb", "PCB"):
            self.assertEqual(
                key_store.validate_key_text(alias, "0123456789abcdef")[0],
                "unverified",
            )

    def test_unknown_format(self):
        self.assertEqual(key_store.validate_key_text("brd", "x")[0], "unknown_format")

    def test_is_savable(self):
        self.assertTrue(key_store.is_savable("valid"))
        self.assertTrue(key_store.is_savable("unverified"))
        for bad in ("invalid", "malformed", "unknown_format", "error"):
            self.assertFalse(key_store.is_savable(bad))


class KeyStorageTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("BOARDVIEWER_DATA_DIR")
        self._tmp = tempfile.mkdtemp(prefix="bv_keystore_test-")
        os.environ["BOARDVIEWER_DATA_DIR"] = self._tmp

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("BOARDVIEWER_DATA_DIR", None)
        else:
            os.environ["BOARDVIEWER_DATA_DIR"] = self._prev
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_round_trip(self):
        self.assertIsNone(key_store.read_key("xzz"))
        dest = key_store.write_key("xzz", "  0123456789abcdef\n")
        self.assertTrue(Path(dest).is_file())
        self.assertEqual(key_store.read_key("xzz"), "0123456789abcdef")

    def test_clear(self):
        key_store.write_key("fz", "deadbeef")
        self.assertTrue(key_store.clear_key("fz"))
        self.assertIsNone(key_store.read_key("fz"))
        self.assertFalse(key_store.clear_key("fz"))  # already gone


class BoardExportWrapperTests(unittest.TestCase):
    def test_validate_key_json_shape_matches_status(self):
        cases = {
            ("fz", "junk"): ("malformed", False),
            ("xzz", ""): ("malformed", False),
            ("xzz", "0123456789abcdef"): ("unverified", True),
            ("nope", "x"): ("unknown_format", False),
        }
        for (fmt, text), (status, ok) in cases.items():
            payload = json.loads(board_export.validate_key(fmt, text))
            self.assertEqual(payload["status"], status)
            self.assertEqual(payload["ok"], ok)
            self.assertIn("message", payload)

    def test_validate_key_valid_fz(self):
        payload = json.loads(
            board_export.validate_key("fz", _fz_text(_valid_fz_key_words()))
        )
        self.assertEqual(payload["status"], "valid")
        self.assertTrue(payload["ok"])


if __name__ == "__main__":
    unittest.main()
