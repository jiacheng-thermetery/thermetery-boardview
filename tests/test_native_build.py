# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""Coverage for src/native_build.py.

Every test points source_dir/output_dir at a TemporaryDirectory, so the
checkout is never mutated and no test can leave a stray library behind.

The tests that need a real compiler skip themselves when none is found, so
the file is safe on any CI image; the ubuntu-latest runner has gcc and does
exercise the full round trip."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import native_build
from src.native_build import (AUTOBUILD_ENV, CC_ENV, LIBRARY_NAMES,
                              BuildReport, autobuild_enabled, detect_compiler,
                              ensure_native_libraries, gnu_flags, msvc_flags)

REAL_SOURCES = Path(__file__).resolve().parent.parent / "src" / "parsers" / "native"
HAVE_COMPILER = detect_compiler() is not None


def _staged(tmp: str) -> Path:
    """Copy the three real .c files into a scratch directory."""
    source_dir = Path(tmp) / "native"
    source_dir.mkdir(parents=True, exist_ok=True)
    for name in LIBRARY_NAMES:
        (source_dir / f"{name}.c").write_bytes((REAL_SOURCES / f"{name}.c").read_bytes())
    return source_dir


class GateTests(unittest.TestCase):
    """Nothing may run a compiler unless it is supposed to."""

    def _no_subprocess(self):
        return mock.patch.object(
            native_build, "_run",
            side_effect=AssertionError("a compiler was invoked"))

    def test_disabled_by_environment(self):
        with tempfile.TemporaryDirectory() as tmp, self._no_subprocess():
            report = ensure_native_libraries(
                source_dir=_staged(tmp), output_dir=Path(tmp) / "out",
                environ={AUTOBUILD_ENV: "0"})
        self.assertIn(AUTOBUILD_ENV, report.skipped or "")
        self.assertEqual(report.built, ())

    def test_autobuild_enabled_accepts_the_documented_spellings(self):
        for value in ("0", "off", "false", "no", "OFF"):
            self.assertFalse(autobuild_enabled({AUTOBUILD_ENV: value}), value)
        for value in ("", "1", "yes", "anything"):
            self.assertTrue(autobuild_enabled({AUTOBUILD_ENV: value}), value)

    def test_frozen_build_never_compiles(self):
        with tempfile.TemporaryDirectory() as tmp, self._no_subprocess():
            with mock.patch.object(sys, "frozen", True, create=True):
                report = ensure_native_libraries(
                    source_dir=_staged(tmp), output_dir=Path(tmp) / "out",
                    environ={})
        self.assertIn("frozen", report.skipped or "")
        self.assertEqual(report.built, ())

    def test_missing_sources_are_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp, self._no_subprocess():
            report = ensure_native_libraries(
                source_dir=Path(tmp) / "absent", output_dir=Path(tmp) / "out",
                environ={})
        self.assertIn("no native sources", report.skipped or "")

    def test_no_compiler_is_advice_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(native_build, "detect_compiler",
                                   return_value=None):
                report = ensure_native_libraries(
                    source_dir=_staged(tmp), output_dir=Path(tmp) / "out",
                    environ={})
        self.assertIn("no C compiler", report.skipped or "")
        advice = " ".join(report.advice())
        self.assertIn(CC_ENV, advice)
        self.assertIn("meson", advice, "the canonical build stays on offer")


class StampTests(unittest.TestCase):
    def test_a_library_we_did_not_build_is_never_overwritten(self):
        """No stamp means meson, a release bundle, or a packager put it
        there. Clobbering it would silently downgrade a release."""
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = _staged(tmp)
            out = Path(tmp) / "out"
            out.mkdir()
            planted = out / native_build._output_name("rc6_native")
            planted.write_bytes(b"not really a library")
            with mock.patch.object(
                    native_build, "_run",
                    side_effect=AssertionError("a compiler was invoked")):
                report = ensure_native_libraries(
                    source_dir=source_dir, output_dir=out,
                    names=("rc6_native",), environ={})
            self.assertEqual(planted.read_bytes(), b"not really a library")
        self.assertIn("rc6_native", report.up_to_date)
        self.assertEqual(report.failed, {})

    def test_a_recorded_failure_is_not_retried_every_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = _staged(tmp)
            out = Path(tmp) / "out"
            fake = native_build.Compiler(path="cc", kind="gnu", ident="stub 1.0")
            failing = mock.Mock(return_value=mock.Mock(
                returncode=1, stdout="", stderr="stub: cannot compile"))
            with mock.patch.object(native_build, "detect_compiler", return_value=fake), \
                    mock.patch.object(native_build, "_run", failing):
                first = ensure_native_libraries(
                    source_dir=source_dir, output_dir=out,
                    names=("rc6_native",), environ={})
            self.assertIn("rc6_native", first.failed)
            calls_after_first = failing.call_count
            self.assertGreater(calls_after_first, 0)

            # Second launch: same source, same compiler -> no new attempt.
            with mock.patch.object(native_build, "detect_compiler", return_value=fake), \
                    mock.patch.object(native_build, "_run", failing):
                second = ensure_native_libraries(
                    source_dir=source_dir, output_dir=out,
                    names=("rc6_native",), environ={})
            self.assertEqual(failing.call_count, calls_after_first,
                             "a recorded failure must not be retried")
            self.assertIn("rc6_native", second.failed)


class FlagContractTests(unittest.TestCase):
    """These flags are not style choices; each one prevents a real failure."""

    def test_msvc_always_passes_utf8(self):
        # All three sources contain UTF-8 in comments; without /utf-8 cl
        # raises C4819 on a CJK-codepage machine.
        self.assertIn("/utf-8", msvc_flags())

    def test_msvc_links_the_static_runtime(self):
        # /MD would make the library import VCRUNTIME140.dll, which a plain
        # source checkout has no reason to have.
        self.assertIn("/MT", msvc_flags())
        self.assertNotIn("/MD", msvc_flags())

    def test_gnu_optional_flags_can_be_dropped(self):
        full, minimal = gnu_flags(True), gnu_flags(False)
        self.assertTrue(set(minimal).issubset(set(full)))
        for flag in ("-O3", "-std=c11", "-DNDEBUG"):
            self.assertIn(flag, minimal)

    def test_darwin_uses_dynamiclib_not_shared(self):
        with mock.patch.object(native_build, "_IS_DARWIN", True), \
                mock.patch.object(native_build, "_IS_WINDOWS", False):
            flags = gnu_flags()
        self.assertIn("-dynamiclib", flags)
        self.assertNotIn("-shared", flags)
        self.assertNotIn("-Wl,--strip-all", flags, "ld64 has no --strip-all")

    def test_posix_builds_position_independent(self):
        with mock.patch.object(native_build, "_IS_DARWIN", False), \
                mock.patch.object(native_build, "_IS_WINDOWS", False):
            self.assertIn("-fPIC", gnu_flags())

    def test_output_name_follows_the_meson_prefix_rule(self):
        name = native_build._output_name("tvw_native")
        if sys.platform.startswith("win"):
            self.assertEqual(name, "tvw_native.dll")
        else:
            # The lib prefix is load-bearing on POSIX: runtime_paths' bare
            # soname fallback only emits names that start with it.
            self.assertTrue(name.startswith("libtvw_native."), name)


class CompilerProbeTests(unittest.TestCase):
    def test_cygwin_and_msys_targets_are_rejected(self):
        """Those link against cygwin1.dll / msys-2.0.dll, which a native
        interpreter cannot load -- they build cleanly and then fail."""
        if not sys.platform.startswith("win"):
            self.skipTest("the rejection only applies on Windows")
        for triple in ("x86_64-pc-cygwin", "x86_64-pc-msys"):
            proc = mock.Mock(returncode=0, stdout=triple + "\n", stderr="")
            with mock.patch.object(native_build, "_run", return_value=proc):
                self.assertIsNone(native_build._probe_gnu("cc", {}), triple)

    def test_architecture_mismatch_is_rejected(self):
        proc = mock.Mock(returncode=0, stdout="i686-w64-mingw32\n", stderr="")
        with mock.patch.object(native_build, "_run", return_value=proc), \
                mock.patch.object(native_build.platform, "machine",
                                  return_value="AMD64"):
            self.assertIsNone(native_build._probe_gnu("cc", {}))

    def test_compiler_directory_is_prepended_to_path(self):
        """MSYS2's gcc exits 1 with no diagnostic at all unless its own bin
        directory is on PATH."""
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "bin" / "gcc"
            binary.parent.mkdir(parents=True)
            env = native_build._compiler_env(str(binary), {"PATH": "existing"})
        self.assertTrue(env["PATH"].startswith(str(binary.parent)), env["PATH"])
        self.assertIn("existing", env["PATH"])


@unittest.skipUnless(HAVE_COMPILER, "no C compiler available on this machine")
class RoundTripTests(unittest.TestCase):
    """The real thing: compile the actual sources and load the result."""

    def test_builds_loads_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = _staged(tmp)
            out = Path(tmp) / "out"
            report = ensure_native_libraries(source_dir=source_dir,
                                             output_dir=out, environ={})
            self.assertEqual(report.failed, {}, report.failed)
            self.assertEqual(sorted(report.built), sorted(LIBRARY_NAMES))

            # _verify already loaded each one and ran the xzz DES self-test;
            # if any had failed it would be in report.failed above.
            for name in LIBRARY_NAMES:
                self.assertTrue((out / native_build._output_name(name)).is_file())

            stamps = {name: (out / native_build._output_name(name)).stat().st_mtime_ns
                      for name in LIBRARY_NAMES}

            again = ensure_native_libraries(source_dir=source_dir,
                                            output_dir=out, environ={})
            self.assertEqual(again.built, ())
            self.assertEqual(sorted(again.up_to_date), sorted(LIBRARY_NAMES))
            for name in LIBRARY_NAMES:
                self.assertEqual(
                    (out / native_build._output_name(name)).stat().st_mtime_ns,
                    stamps[name], f"{name} was rebuilt unnecessarily")

    def test_no_intermediate_litter_is_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            ensure_native_libraries(source_dir=_staged(tmp), output_dir=out,
                                    names=("rc6_native",), environ={})
            leftovers = [p.name for p in out.iterdir()
                         if p.suffix.lower() in (".obj", ".lib", ".exp", ".tmp")]
            self.assertEqual(leftovers, [])
            self.assertFalse((out / ".build-tmp").exists())


class PlacementTests(unittest.TestCase):
    """native_build must stay off the Android and frozen code paths."""

    def test_not_staged_into_the_android_apk(self):
        gradle = (Path(__file__).resolve().parent.parent / "android" / "app"
                  / "build.gradle.kts")
        if not gradle.is_file():
            self.skipTest("android/ not present in this checkout")
        text = gradle.read_text(encoding="utf-8", errors="replace")
        self.assertNotIn("native_build", text)
        self.assertNotIn("app_common", text)

    def test_only_app_common_imports_it(self):
        """One importer keeps the compile path out of the ctypes wrappers,
        which run on-device and inside the packaging self-test."""
        src = Path(__file__).resolve().parent.parent / "src"
        importers = [p.name for p in src.rglob("*.py")
                     if p.name != "native_build.py"
                     and "native_build" in p.read_text(encoding="utf-8",
                                                       errors="replace")]
        self.assertEqual(importers, ["app_common.py"], importers)


if __name__ == "__main__":
    unittest.main()
