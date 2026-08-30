import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import runtime_paths
from src.parsers import fz_parser, xzzpcb_parser


class RuntimePathTests(unittest.TestCase):
    def test_source_paths_remain_compatible(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            runtime_paths.sys, "frozen", False, create=True
        ):
            self.assertIsNone(runtime_paths.managed_data_dir())
            self.assertEqual(runtime_paths.private_dir(), Path("private"))

    def test_native_lib_names_per_platform(self):
        # iOS reports sys.platform == "ios" (PEP 730) and uses Mach-O
        # dylibs like macOS; everything else non-Windows stays ".so".
        cases = {
            "win32": ["tvw_native.dll", "libtvw_native.dll"],
            "darwin": ["tvw_native.dylib", "libtvw_native.dylib"],
            "ios": ["tvw_native.dylib", "libtvw_native.dylib"],
            "linux": ["tvw_native.so", "libtvw_native.so"],
        }
        for platform, expected in cases.items():
            with mock.patch.object(runtime_paths.sys, "platform", platform):
                self.assertEqual(
                    runtime_paths.native_lib_names("tvw_native"), expected,
                    f"sys.platform={platform}",
                )

    def test_native_lib_candidates_ios_framework_shape(self):
        # On iOS the BOARDVIEW_NATIVE_DIR override is searched in App Store
        # framework shape first (<dir>/<base>.framework/<base>), then the
        # plain dylib names.
        with mock.patch.object(runtime_paths.sys, "platform", "ios"), \
             mock.patch.dict(os.environ, {runtime_paths.NATIVE_DIR_ENV: "/fw"}):
            candidates = runtime_paths.native_lib_candidates(
                Path("/nonexistent"), "tvw_native")
        self.assertEqual(
            candidates[0], str(Path("/fw/tvw_native.framework/tvw_native")))
        self.assertEqual(candidates[1], str(Path("/fw/tvw_native.dylib")))

    def test_explicit_data_root_has_precedence(self):
        # Platform-neutral absolute path: "C:/..." is not absolute on
        # POSIX (managed_data_dir would CWD-resolve it), which made this
        # test Windows-only until CI started running on Linux.
        root = Path(tempfile.gettempdir()).resolve() / "managed data" / "\u6d4b\u8bd5"
        with mock.patch.dict(
            os.environ, {runtime_paths.DATA_DIR_ENV: str(root)}, clear=True
        ):
            self.assertEqual(runtime_paths.managed_data_dir(), root)
            self.assertEqual(runtime_paths.config_path(), root / "config.json")
            self.assertEqual(
                runtime_paths.key_path("xzz"), root / "private" / "XZZ_Key.txt"
            )

    def test_frozen_portable_uses_executable_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe_dir = Path(tmp) / "space \u6d4b\u8bd5"
            exe_dir.mkdir()
            (exe_dir / runtime_paths.PORTABLE_MARKER).touch()
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(runtime_paths.sys, "frozen", True, create=True),
                mock.patch.object(
                    runtime_paths.sys,
                    "executable",
                    str(exe_dir / "ThermeteryBoardviewer.exe"),
                ),
            ):
                self.assertTrue(runtime_paths.portable_mode())
                self.assertEqual(
                    runtime_paths.managed_data_dir(), exe_dir.resolve() / "data"
                )

    def test_frozen_installed_uses_local_app_data(self):
        root = Path("C:/Users/test/AppData/Local")
        with (
            mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root)}, clear=True),
            mock.patch.object(runtime_paths.sys, "frozen", True, create=True),
            mock.patch.object(
                runtime_paths.sys,
                "executable",
                "C:/Program Files/ThermeteryBoardviewer.exe",
            ),
        ):
            self.assertFalse(runtime_paths.portable_mode())
            self.assertEqual(
                runtime_paths.managed_data_dir(), root / runtime_paths.APP_DATA_DIRNAME
            )

    def test_parser_key_readers_share_managed_root(self):
        valid_fz = [1 if expected == 0 else 0 for expected in fz_parser._RC6_PARITY]
        key_text = "\n".join(f"{word:08x}" for word in valid_fz)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            private = root / "private"
            private.mkdir()
            (private / "fz_key.txt").write_text(key_text, encoding="utf-8")
            (private / "XZZ_Key.txt").write_text(
                "0123456789abcdef", encoding="utf-8"
            )
            with mock.patch.dict(
                os.environ, {runtime_paths.DATA_DIR_ENV: str(root)}, clear=True
            ):
                self.assertEqual(fz_parser._load_fz_key(), valid_fz)
                self.assertEqual(
                    xzzpcb_parser._resolve_key(None), 0x0123456789ABCDEF
                )

    def test_managed_mode_ignores_cwd_key_decoys(self):
        valid_fz = [1 if expected == 0 else 0 for expected in fz_parser._RC6_PARITY]
        key_text = "\n".join(f"{word:08x}" for word in valid_fz)
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            decoy = root / "private"
            managed = root / "managed"
            decoy.mkdir()
            managed.mkdir()
            (decoy / "fz_key.txt").write_text(key_text, encoding="utf-8")
            (decoy / "XZZ_Key.txt").write_text(
                "0123456789abcdef", encoding="utf-8"
            )
            try:
                os.chdir(root)
                with mock.patch.dict(
                    os.environ,
                    {runtime_paths.DATA_DIR_ENV: str(managed)},
                    clear=True,
                ):
                    self.assertIsNone(fz_parser._load_fz_key())
                    self.assertIsNone(xzzpcb_parser._resolve_key(None))
            finally:
                os.chdir(old_cwd)

    def test_viewer_saved_keys_are_immediately_parser_visible(self):
        from src import viewer

        valid_fz = [1 if expected == 0 else 0 for expected in fz_parser._RC6_PARITY]
        fz_text = "\n".join(f"{word:08x}" for word in valid_fz)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {runtime_paths.DATA_DIR_ENV: tmp}, clear=True
        ), mock.patch.object(viewer.messagebox, "askyesno", return_value=True), mock.patch.object(
            viewer.messagebox, "showinfo"
        ):
            owner = object()
            viewer.ViewerApp._maybe_save_key(owner, "fz", fz_text)
            viewer.ViewerApp._maybe_save_key(owner, "xzz", "0123456789abcdef")
            self.assertEqual(fz_parser._resolve_fz_key(), valid_fz)
            self.assertEqual(
                xzzpcb_parser._resolve_key(None), 0x0123456789ABCDEF
            )

    def test_viewer_config_uses_managed_root(self):
        # Import lazily: renderer imports are intentionally outside the
        # lightweight path tests above.
        from src import viewer

        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {runtime_paths.DATA_DIR_ENV: tmp}, clear=True
        ):
            viewer._save_config({"last_dir": "example"})
            path = Path(tmp) / "config.json"
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["last_dir"],
                "example",
            )
            self.assertEqual(viewer._load_config()["last_dir"], "example")


if __name__ == "__main__":
    unittest.main()
