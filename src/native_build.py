# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""Compile the native parser accelerators on demand, with or without meson.

The three libraries under ``src/parsers/native`` are what keep cold loads
tolerable: without them a ``.pcb`` costs an extra 30-60 s and every ``.tvw``
an extra 1-2 s. They used to be the user's problem -- the app printed a
``meson compile -C build`` hint and carried on slowly -- which is a poor
trade when the sources are three self-contained C files that need nothing
but libc and about 800 ms of a compiler's time.

Nothing here needs meson. Each library is one translation unit with no
project headers and no third-party dependencies, so a single compiler
invocation per file reproduces exactly what meson would emit. meson remains
the canonical build for releases; this is the fallback that makes a plain
checkout fast on any machine that has a C compiler at all.

Design constraints worth knowing before changing anything:

* **This module must stay off the Android and frozen paths.** It is imported
  by ``app_common.check_native_dlls`` and nothing else. The ctypes wrappers
  (``tvw_native.py``, ``xzz_native.py``, ``fz_parser.py``) are staged into
  the Chaquopy APK and are called directly by the packaging self-test, so
  build logic must never live in them.
* **The build has to finish before anything probes for a library.** All
  three wrappers latch their first load result -- a miss is remembered for
  the life of the process -- and Windows keys ``LoadLibrary`` by base name,
  so a library that lands after the first probe is invisible until restart.
  That rules out doing this on a background thread.
* **Never raise.** ``check_native_dlls`` is the first statement of both
  entry points and is documented as a warning, not an error. Every failure
  here becomes a field on :class:`BuildReport`.
"""

from __future__ import annotations

import ctypes
import glob
import hashlib
import json
import os
import platform
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple

from .runtime_paths import native_lib_names

LIBRARY_NAMES: Tuple[str, ...] = ("tvw_native", "xzz_native", "rc6_native")

AUTOBUILD_ENV = "BOARDVIEW_NATIVE_AUTOBUILD"
CC_ENV = "BOARDVIEW_CC"

MESON_HINT = "meson setup build && meson compile -C build"

# Entry points each library must actually export before we accept it. An
# auto-build makes "compiled on a machine no CI ever saw" the normal case,
# so a fresh library is not trusted until it loads and resolves the symbols
# its ctypes wrapper binds.
_REQUIRED_SYMBOLS: Dict[str, Tuple[str, ...]] = {
    "tvw_native": ("find_pad_runs_native", "find_net_table_native",
                   "build_topology_native"),
    "xzz_native": ("xzz_des_decrypt_buffer", "xzz_des_selftest"),
    "rc6_native": ("rc6_decode",),
}

# Keeps a windowed build from flashing a console per compile.
_NO_WINDOW = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
              if sys.platform.startswith("win") else 0)

_IS_WINDOWS = sys.platform.startswith("win")
_IS_DARWIN = sys.platform == "darwin"


# ----- compiler discovery -------------------------------------------------

@dataclass(frozen=True)
class Compiler:
    path: str
    kind: str                      # "gnu" (gcc/clang) or "msvc"
    ident: str                     # version/target string, part of the stamp
    env: Optional[Dict[str, str]] = field(default=None, repr=False)


def _run(argv, *, env=None, cwd=None, timeout=300):
    return subprocess.run(
        argv, capture_output=True, text=True, errors="replace",
        env=env, cwd=str(cwd) if cwd else None, timeout=timeout,
        creationflags=_NO_WINDOW,
    )


def _which(name: str, environ: Mapping[str, str]) -> Optional[str]:
    import shutil

    return shutil.which(name, path=environ.get("PATH"))


def _arch_matches(triple: str) -> bool:
    """Reject a compiler that targets a different architecture than us.

    A 32-bit gcc under a 64-bit interpreter produces a library that loads
    nowhere useful, and the ctypes error it eventually raises is opaque.
    """
    triple = triple.strip().lower()
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        return triple.startswith(("x86_64", "amd64"))
    if machine in ("arm64", "aarch64"):
        return triple.startswith(("aarch64", "arm64"))
    return True  # unknown host: trust the toolchain rather than refuse


def _probe_gnu(path: str, environ: Mapping[str, str]) -> Optional[Compiler]:
    """Validate a gcc/clang candidate and capture its identity.

    Two rejections matter here. A cygwin or msys target links the produced
    DLL against ``cygwin1.dll`` / ``msys-2.0.dll``, which native ``python``
    cannot load -- it builds cleanly and then fails at import on the very
    machine that built it. And a mismatched architecture is worse, because
    the failure surfaces much later.

    MSYS2's gcc additionally cannot run at all unless its own ``bin`` is on
    PATH: it exits 1 with *no* diagnostic whatsoever. Callers get that PATH
    treatment from :func:`_compiler_env`.
    """
    try:
        proc = _run([path, "-dumpmachine"], env=_compiler_env(path, environ),
                    timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    triple = (proc.stdout or "").strip().splitlines()
    if not triple:
        return None
    target = triple[0].strip()
    if _IS_WINDOWS and ("-cygwin" in target or "-msys" in target):
        return None
    if not _arch_matches(target):
        return None
    version = ""
    try:
        ver = _run([path, "--version"], env=_compiler_env(path, environ),
                   timeout=60)
        if ver.returncode == 0 and ver.stdout:
            version = ver.stdout.splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError):
        # Cosmetic only: the version string just makes the stamp key and the
        # log line more legible. -dumpmachine already proved the compiler
        # runs, so failing to get a banner is no reason to reject it.
        pass
    return Compiler(path=path, kind="gnu",
                    ident=f"{version} ({target})" if version else target)


def _compiler_env(path: str, environ: Mapping[str, str]) -> Dict[str, str]:
    """Environment for invoking `path`, with its own directory on PATH.

    Load-bearing for MSYS2: its gcc.exe links against libraries that sit
    beside it, so invoking it by absolute path with an unrelated PATH makes
    it exit 1 silently. Prepending its directory costs nothing elsewhere.
    """
    env = dict(environ)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        env["PATH"] = directory + os.pathsep + env.get("PATH", "")
    return env


def _windows_gnu_candidates() -> Sequence[str]:
    found = [
        r"C:\msys64\ucrt64\bin\gcc.exe",
        r"C:\msys64\mingw64\bin\gcc.exe",
        r"C:\mingw64\bin\gcc.exe",
        r"C:\tools\mingw64\bin\gcc.exe",
    ]
    # Same shape as build_windows.ps1's Add-DiscoveredCompilerPath.
    found.extend(sorted(glob.glob(r"C:\Program Files\gcc-*mingw*\bin\gcc.exe"),
                        reverse=True))
    return found


def _detect_msvc(environ: Mapping[str, str]) -> Optional[Compiler]:
    """Find a usable cl.exe.

    cl.exe cannot be used by putting its directory on PATH: with no INCLUDE
    it dies at <stdint.h> with "fatal error C1034: no include path set",
    which reads like a broken source tree. So either we are already inside a
    developer shell, or we capture the environment vcvarsall.bat exports.
    """
    direct = _which("cl.exe", environ)
    if direct and environ.get("INCLUDE"):
        return Compiler(path=direct, kind="msvc", ident="cl.exe (developer shell)",
                        env=dict(environ))

    vswhere = Path(environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) \
        / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        return None
    try:
        proc = _run([str(vswhere), "-latest", "-products", "*", "-requires",
                     "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                     "-property", "installationPath"], env=dict(environ))
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    root = Path(proc.stdout.strip().splitlines()[0].strip())
    vcvarsall = root / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
    if not vcvarsall.is_file():
        return None
    env = _vcvars_env(vcvarsall, "x64", environ)
    if env is None:
        return None
    cl = _which("cl.exe", env)
    if not cl:
        return None
    return Compiler(path=cl, kind="msvc", ident=f"cl.exe via {root.name}", env=env)


def _vcvars_env(vcvarsall: Path, arch: str,
                environ: Mapping[str, str]) -> Optional[Dict[str, str]]:
    """Capture what vcvarsall.bat exports, once, for reuse by every compile.

    Note vcvarsall can print noise to stderr ("'vswhere.exe' is not
    recognized") while still succeeding, so the return code and the marker
    are the only trustworthy signals.
    """
    marker = "__BOARDVIEW_VCVARS__"
    line = f'call "{vcvarsall}" {arch} >NUL && echo {marker} && set'
    try:
        proc = _run(["cmd", "/d", "/c", line], env=dict(environ), timeout=180)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or marker not in (proc.stdout or ""):
        return None
    env = dict(environ)
    for entry in proc.stdout.split(marker, 1)[1].splitlines():
        key, sep, value = entry.partition("=")
        if sep and key.strip():
            env[key] = value
    return env if env.get("INCLUDE") else None


def detect_compiler(environ: Optional[Mapping[str, str]] = None) -> Optional[Compiler]:
    """Resolve one compiler, once, to an absolute path.

    Resolved once rather than per file because a machine with two toolchains
    on PATH will otherwise hand different invocations different compilers,
    and the stamp would record whichever answered last.

    BOARDVIEW_CC comes first deliberately: a hardcoded candidate list can
    only ever guess at C:\\-rooted installs, and a toolchain living anywhere
    else is unreachable without it.
    """
    env = os.environ if environ is None else environ
    for key in (CC_ENV, "CC"):
        raw = (env.get(key) or "").strip()
        if raw:
            path = raw if os.path.isabs(raw) else (_which(raw, env) or raw)
            found = _probe_gnu(path, env)
            if found is not None:
                return found

    for name in ("cc", "gcc", "clang"):
        path = _which(name, env)
        if path:
            found = _probe_gnu(path, env)
            if found is not None:
                return found

    if _IS_WINDOWS:
        for candidate in _windows_gnu_candidates():
            if os.path.isfile(candidate):
                found = _probe_gnu(candidate, env)
                if found is not None:
                    return found
        return _detect_msvc(env)
    return None


# ----- flags --------------------------------------------------------------

# -O3 rather than -O2: it is what meson's buildtype=release selects for
# gcc/clang, and what the hand-build lines documented in the sources
# themselves already use.
_GNU_CORE = ("-O3", "-std=c11", "-DNDEBUG")


def gnu_flags(optional: bool = True) -> Tuple[str, ...]:
    if _IS_DARWIN:
        # ld64 wants -dynamiclib, and has no --strip-all.
        return _GNU_CORE + ("-dynamiclib", "-fPIC")
    if _IS_WINDOWS:
        extra = ("-static-libgcc", "-Wl,--strip-all") if optional else ()
        return _GNU_CORE + ("-shared",) + extra
    extra = ("-Wl,--strip-all",) if optional else ()
    return _GNU_CORE + ("-shared", "-fPIC") + extra


def msvc_flags() -> Tuple[str, ...]:
    # /MT, not /MD: a /MD build imports VCRUNTIME140.dll, which a plain
    # source checkout has no reason to have. /utf-8 is mandatory rather
    # than cosmetic -- all three sources contain UTF-8 in comments, and on
    # a CJK-codepage machine cl treats that as warning C4819.
    return ("/nologo", "/LD", "/MT", "/O2", "/Gw", "/std:c11", "/utf-8",
            "/DNDEBUG")


def _flags_for(cc: Compiler, optional: bool = True) -> Tuple[str, ...]:
    return msvc_flags() if cc.kind == "msvc" else gnu_flags(optional)


def _output_name(base: str) -> str:
    """`tvw_native.dll` on Windows, `libtvw_native.so` elsewhere.

    Same rule as meson.build. On POSIX the `lib` prefix is load-bearing:
    native_lib_candidates' last-resort bare-soname dlopen entries only
    include names that start with it.
    """
    names = native_lib_names(base)
    return names[0] if _IS_WINDOWS else names[1]


# ----- build --------------------------------------------------------------

@dataclass
class BuildReport:
    built: Tuple[str, ...] = ()
    up_to_date: Tuple[str, ...] = ()
    failed: Dict[str, str] = field(default_factory=dict)
    skipped: Optional[str] = None
    compiler: Optional[str] = None

    def advice(self) -> Tuple[str, ...]:
        """ASCII-only stderr lines explaining what to do next."""
        lines = []
        if self.skipped:
            lines.append(f"native auto-build skipped: {self.skipped}")
        for name, error in self.failed.items():
            lines.append(f"native auto-build failed for {name}: {error}")
        if self.skipped or self.failed:
            if self.compiler is None:
                lines.append(
                    f"install gcc or clang, or point {CC_ENV} at a compiler, "
                    "then relaunch")
            lines.append(f"manual build: {MESON_HINT}")
        return tuple(lines)


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        # Best effort cleanup of a temp or rejected artifact. It is already
        # gone, or something else holds it; either way the caller's decision
        # does not change and a failure here must not mask the real result.
        pass


def _stamp_path(library: Path) -> Path:
    return library.with_name(library.name + ".build-stamp")


def _stamp_key(source: Path, cc: Compiler, flags: Sequence[str]) -> str:
    """Identity of a build: the source, the compiler, and the flags.

    A content hash rather than an mtime comparison, because mtimes are not
    a trustworthy input here: git stamps every file with the checkout time
    and archive extraction rounds to two seconds, which produces both
    spurious rebuilds and missed ones.
    """
    digest = hashlib.sha256()
    digest.update(source.read_bytes())
    digest.update(b"\0" + cc.ident.encode("utf-8", "replace"))
    digest.update(b"\0" + "\0".join(flags).encode("utf-8", "replace"))
    return digest.hexdigest()


def _read_stamp(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_stamp(path: Path, key: str, ok: bool, error: str = "") -> None:
    try:
        path.write_text(json.dumps({"key": key, "ok": ok, "error": error}),
                        encoding="utf-8")
    except OSError:
        # The stamp is an optimisation, not state we depend on. Losing it
        # costs a rebuild next launch (or a retry of a failure), which is
        # strictly better than failing a build that already succeeded.
        pass


def _unload(lib) -> None:
    """Release a handle taken purely to validate a library.

    Verification must not leave the file mapped: on Windows a mapped DLL
    cannot be replaced or deleted, so holding it would stop this process
    from ever rebuilding it and would pin the file for anything else. The
    wrapper re-loads by name a moment later and gets its own handle.
    """
    handle = getattr(lib, "_handle", None)
    if not handle:
        return
    try:
        if _IS_WINDOWS:
            ctypes.windll.kernel32.FreeLibrary(ctypes.c_void_p(handle))
        else:
            ctypes.CDLL(None).dlclose(ctypes.c_void_p(handle))
    except Exception:
        # Deliberately broad: this is a courtesy unload on a library that
        # has already been verified, and the platform surface here (windll,
        # dlclose via the process handle) varies enough that an unexpected
        # failure must not turn a good build into a failed one. Worst case
        # the library stays mapped, exactly as it would have before.
        pass


def _verify(path: Path, name: str) -> Optional[str]:
    """Load the new library exactly as the wrappers will, or reject it."""
    lib = None
    try:
        lib = ctypes.CDLL(str(path))
        for symbol in _REQUIRED_SYMBOLS[name]:
            getattr(lib, symbol)
        if name == "xzz_native":
            lib.xzz_des_selftest.argtypes = []
            lib.xzz_des_selftest.restype = ctypes.c_int32
            if lib.xzz_des_selftest() != 0:
                return "DES self-test vector mismatch"
    except (OSError, AttributeError) as exc:
        return f"{type(exc).__name__}: {exc}"
    finally:
        if lib is not None:
            _unload(lib)
    return None


def _compile(cc: Compiler, flags: Sequence[str], source: Path, out: Path,
             obj_dir: Path):
    if cc.kind == "msvc":
        # cwd=obj_dir rather than /Fo:<dir>\ -- a trailing backslash escapes
        # the quote that list2cmdline adds around any path with a space.
        argv = [cc.path, *flags, f"/Fe:{out}", str(source),
                "/link", f"/IMPLIB:{obj_dir / (out.stem + '.lib')}"]
        return _run(argv, env=cc.env, cwd=obj_dir)
    argv = [cc.path, *flags, "-o", str(out), str(source)]
    return _run(argv, env=cc.env or _compiler_env(cc.path, os.environ))


def _install(temp: Path, final: Path) -> None:
    """Move a freshly built library into place.

    os.replace over a DLL any process has mapped fails on Windows with
    WinError 5, including when the mapping belongs to us. That failure is
    benign -- it means a usable library already sits at exactly this path,
    which is the goal -- so the temp copy is simply dropped.
    """
    try:
        os.replace(temp, final)
    except OSError:
        _unlink_quiet(temp)


def _build_one(cc: Compiler, name: str, source: Path, out_dir: Path,
               obj_dir: Path) -> Optional[str]:
    """Build one library. Returns None on success, else an error string."""
    final = out_dir / _output_name(name)
    # Unique per attempt: the rename is atomic, but two instances starting
    # together must not have their compilers writing one shared temp file.
    temp = out_dir / f"{name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp{final.suffix}"
    try:
        proc = _compile(cc, _flags_for(cc), source, temp, obj_dir)
        if proc.returncode != 0 and cc.kind == "gnu":
            # -static-libgcc / --strip-all are insurance, not requirements;
            # an unfamiliar toolchain rejecting one should not cost the
            # whole feature.
            proc = _compile(cc, _flags_for(cc, optional=False), source, temp,
                            obj_dir)
        if proc.returncode != 0 or not temp.is_file():
            _unlink_quiet(temp)
            return _first_error(proc)
        _install(temp, final)
    except (OSError, subprocess.SubprocessError) as exc:
        _unlink_quiet(temp)
        return f"{type(exc).__name__}: {exc}"

    error = _verify(final, name)
    if error is not None:
        return error
    return None


def _first_error(proc) -> str:
    text = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
    if not text:
        return f"compiler exited {proc.returncode} without output"
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:300]
    return f"compiler exited {proc.returncode}"


def autobuild_enabled(environ: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(AUTOBUILD_ENV, "") or "").strip().lower() not in (
        "0", "off", "false", "no")


def ensure_native_libraries(
    *,
    log: Callable[[str], None] = lambda _message: None,
    source_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    names: Sequence[str] = LIBRARY_NAMES,
    environ: Optional[Mapping[str, str]] = None,
) -> BuildReport:
    """Compile any missing native library. Never raises.

    Returns a :class:`BuildReport` describing what happened; the caller
    turns that into the stderr advice it already prints for missing
    libraries.
    """
    env = os.environ if environ is None else environ
    report = BuildReport()

    if not autobuild_enabled(env):
        report.skipped = f"disabled by {AUTOBUILD_ENV}"
        return report
    if getattr(sys, "frozen", False):
        # A frozen tree ships its libraries and contains no .c files.
        report.skipped = "frozen build ships prebuilt libraries"
        return report

    sources = Path(source_dir) if source_dir else Path(__file__).parent / "parsers" / "native"
    out_dir = Path(output_dir) if output_dir else sources
    try:
        wanted = [n for n in names if (sources / f"{n}.c").is_file()]
    except OSError:
        wanted = []
    if not wanted:
        report.skipped = f"no native sources under {sources}"
        return report

    # Only consider libraries that are actually absent. A library with no
    # stamp beside it was put there by meson, a release bundle or a
    # packager -- never overwrite something this module did not build.
    todo = []
    for name in wanted:
        library = out_dir / _output_name(name)
        if library.is_file():
            report.up_to_date += (name,)
            continue
        todo.append(name)
    if not todo:
        return report

    cc = detect_compiler(env)
    if cc is None:
        report.skipped = (
            f"no C compiler found (tried {CC_ENV}/CC, then cc/gcc/clang on "
            "PATH" + (", then the known MinGW and Visual Studio locations"
                      if _IS_WINDOWS else "") + ")")
        return report
    report.compiler = cc.ident

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        obj_dir = out_dir / ".build-tmp"
        obj_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report.skipped = f"cannot write to {out_dir}: {exc}"
        return report

    pending = []
    for name in todo:
        source = sources / f"{name}.c"
        library = out_dir / _output_name(name)
        stamp_file = _stamp_path(library)
        try:
            key = _stamp_key(source, cc, _flags_for(cc))
        except OSError as exc:
            report.failed[name] = f"cannot read {source.name}: {exc}"
            continue
        stamp = _read_stamp(stamp_file)
        if stamp and stamp.get("key") == key and not stamp.get("ok", False):
            # This exact source+compiler already failed. Retrying every
            # launch is how a retry storm starts.
            report.failed[name] = stamp.get("error") or "previous build failed"
            continue
        pending.append((name, source, stamp_file, key))

    if not pending:
        _prune(obj_dir)
        return report

    log(f"building native accelerators with {cc.ident} ...")
    for name, source, stamp_file, key in pending:
        error = _build_one(cc, name, source, out_dir, obj_dir)
        _write_stamp(stamp_file, key, error is None, error or "")
        if error is None:
            report.built += (name,)
        else:
            report.failed[name] = error
    if report.built:
        log("built " + ", ".join(report.built))
    _prune(obj_dir)
    return report


def _prune(obj_dir: Path) -> None:
    """Drop the compiler's intermediate litter (.obj/.lib/.exp from MSVC)."""
    try:
        for entry in obj_dir.iterdir():
            if entry.is_file():
                _unlink_quiet(entry)
        obj_dir.rmdir()
    except OSError:
        pass
