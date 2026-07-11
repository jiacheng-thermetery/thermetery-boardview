import unittest
from pathlib import Path
from unittest import mock

from src.parsers import boardview, tvw_parser
from src.parsers.gencad_parser import BoardModel


class _PrefixStream:
    def __init__(self, data):
        self.data = data
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.data if size < 0 else self.data[:size]


class _FakePath:
    name = "board.unknown"

    def __init__(self, data):
        self.stream = _PrefixStream(data)
        self.open_modes = []

    def open(self, mode):
        self.open_modes.append(mode)
        return self.stream


class ParserIoTests(unittest.TestCase):
    def test_unknown_extension_reads_one_bounded_prefix(self):
        path = _FakePath(b"$COMPONENTS\n$SIGNALS\n" + b"x" * 20_000)
        expected = BoardModel()
        with (
            mock.patch.object(boardview, "_verify_xzzpcb", return_value=False),
            mock.patch.object(boardview, "_parse_gencad", return_value=expected) as parse,
        ):
            actual = boardview._sniff_and_parse(path)

        self.assertIs(actual, expected)
        self.assertEqual(path.open_modes, ["rb"])
        self.assertEqual(path.stream.read_sizes, [8000])
        parse.assert_called_once_with(path)

    def test_tvw_dispatch_reuses_variant_detection_buffer(self):
        path = Path("fixture.tvw")
        payload = b"tvw payload"
        expected = BoardModel()
        with (
            mock.patch.object(Path, "read_bytes", return_value=payload) as read,
            mock.patch.object(tvw_parser, "_detect_variant", return_value="gigabyte"),
            mock.patch.object(
                tvw_parser, "_parse_gigabyte", return_value=expected,
            ) as parse,
        ):
            actual = tvw_parser.parse(path)

        self.assertIs(actual, expected)
        read.assert_called_once_with()
        parse.assert_called_once_with(path, data=payload)

    def test_tvw_supplied_buffer_performs_no_file_read(self):
        path = Path("fixture.tvw")
        payload = b"already loaded"
        expected = BoardModel()
        with (
            mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read")),
            mock.patch.object(tvw_parser, "_detect_variant", return_value="compal_lenovo"),
            mock.patch.object(tvw_parser, "_parse_compal", return_value=expected) as parse,
        ):
            actual = tvw_parser.parse(path, data=payload)

        self.assertIs(actual, expected)
        parse.assert_called_once_with(path, data=payload)


if __name__ == "__main__":
    unittest.main()
