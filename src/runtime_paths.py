# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""Writable path policy shared by source and frozen builds.

Source checkouts retain the historical paths so existing developer setups keep
working. A frozen Windows build writes beside the executable when the portable
marker is present, or below LocalAppData when installed.

Also the single source of truth for the two read-only discovery orders that
every parser needs: where a bundled native library may live
(:func:`native_lib_candidates`) and where a decryption key file may live
(:func:`private_dir_candidates`). Keeping them here stops the per-format
copies from drifting apart — a divergence would silently change which DLL or
which key a given deployment picks up.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List, Mapping, Optional


APP_DATA_DIRNAME = "Thermetery Boardviewer"
DATA_DIR_ENV = "BOARDVIEWER_DATA_DIR"
PORTABLE_MARKER = "portable.flag"
NATIVE_DIR_ENV = "BOARDVIEW_NATIVE_DIR"
NATIVE_SUBDIR = "native"

# Canonical on-disk key filename per format. The Android bridge and the
# desktop key manager both surface these paths verbatim, so the spellings
# (including the mixed case of the XZZ file) are part of the public contract.
KEY_FILENAMES = {
    "fz": "fz_key.txt",
    "xzz": "XZZ_Key.txt",
}


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _executable_dir() -> Path:
    return Path(sys.executable).resolve().parent


def portable_mode() -> bool:
    """Return whether this frozen executable has portable mode enabled."""
    return _is_frozen() and (_executable_dir() / PORTABLE_MARKER).is_file()


def managed_data_dir(environ: Optional[Mapping[str, str]] = None) -> Optional[Path]:
    """Return the managed writable root, or ``None`` for source execution.

    ``BOARDVIEWER_DATA_DIR`` is useful for managed deployments and tests. It
    takes precedence even outside a frozen build.
    """
    env = os.environ if environ is None else environ
    override = env.get(DATA_DIR_ENV, "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
            # Normalize the process override on first use so a later chdir()
            # cannot split config, logs, and keys across different folders.
            if environ is None:
                os.environ[DATA_DIR_ENV] = str(path)
        return path

    if not _is_frozen():
        return None
    if portable_mode():
        return _executable_dir() / "data"

    local_app_data = env.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / APP_DATA_DIRNAME
    try:
        return Path.home() / "AppData" / "Local" / APP_DATA_DIRNAME
    except RuntimeError:
        # Restricted embedded environments may have no resolvable home.
        return _executable_dir() / "data"


def config_path() -> Path:
    root = managed_data_dir()
    return root / "config.json" if root is not None else Path.home() / ".boardviewer.json"


def private_dir() -> Path:
    root = managed_data_dir()
    return root / "private" if root is not None else Path("private")


def key_filename(fmt: str) -> str:
    """Return the canonical key filename for ``fmt``."""
    normalized = fmt.strip().lower()
    if normalized == "fz":
        return KEY_FILENAMES["fz"]
    if normalized in {"xzz", "xzzpcb", "pcb"}:
        return KEY_FILENAMES["xzz"]
    raise ValueError(f"unknown key format: {fmt!r}")


def key_path(fmt: str) -> Path:
    return private_dir() / key_filename(fmt)


def private_dir_candidates(anchor_dir: Optional[Path] = None) -> List[Path]:
    """Ordered ``private/`` directories that may hold a key file.

    A managed root (frozen build, or an explicit ``BOARDVIEWER_DATA_DIR``)
    yields ONLY that location: the CWD, the user's home, and a bundler's
    extraction tree are all outside the app's control, so a stray key file
    dropped there must never shadow the managed one.

    Source execution keeps the historical developer layout — the CWD's
    ``private/`` first (works when launched from the project root), then a
    ``private/`` beside the calling module, then one in its parent directory
    (covers a package nested below the project root that owns the key).
    """
    root = private_dir()
    if managed_data_dir() is not None:
        return [root]
    bases = [root]
    if anchor_dir is not None:
        anchor = Path(anchor_dir).resolve()
        bases += [anchor / "private", anchor.parent / "private"]
    return bases


def native_lib_names(base_name: str) -> List[str]:
    """Platform filenames for native library ``base_name``, preferred first.

    MinGW commonly emits a Unix-style ``lib`` prefix even for Windows DLLs, so
    both the prefixed and the prefix-free release spelling are accepted on
    every platform.
    """
    if sys.platform.startswith("win"):
        suffix = ".dll"
    elif sys.platform in ("darwin", "ios"):
        # CPython on iOS reports sys.platform == "ios" (PEP 730); Mach-O
        # dylibs are the shared-library format there just like on macOS.
        suffix = ".dylib"
    else:
        suffix = ".so"
    return [base_name + suffix, "lib" + base_name + suffix]


def native_lib_candidates(anchor_dir: Path, base_name: str) -> List[str]:
    """Ordered ``ctypes.CDLL`` arguments to try when loading ``base_name``.

    Order is load-bearing: ``BOARDVIEW_NATIVE_DIR`` overrides everything (and
    is tried even when the file is not present, so an explicit override still
    surfaces the OS loader's own error path), then the src-layout ``native/``
    subdirectory below ``anchor_dir``, then the legacy flat layout with the
    library sitting next to the module.

    On non-Windows the bare sonames are appended last: inside an APK, a system
    install, or a zipped package the library is not a plain file next to this
    module (it lives in the app's ``nativeLibraryDir``), and a bare soname
    lets ``dlopen`` search the dynamic linker's own paths. Those entries are
    linker lookups rather than filesystem locations, which is why this returns
    strings rather than ``Path`` objects.
    """
    here = Path(anchor_dir)
    names = native_lib_names(base_name)
    candidates: List[str] = []
    env_dir = os.environ.get(NATIVE_DIR_ENV)
    if env_dir:
        if sys.platform == "ios":
            # App Store packaging requires every dynamic library to live in
            # its own .framework bundle, so on iOS the override directory is
            # searched in that shape first (<dir>/<base>.framework/<base>).
            candidates.append(str(Path(env_dir) / f"{base_name}.framework" / base_name))
        candidates += [str(Path(env_dir) / n) for n in names]
    candidates += [str(here / NATIVE_SUBDIR / n) for n in names
                   if (here / NATIVE_SUBDIR / n).exists()]
    candidates += [str(here / n) for n in names if (here / n).exists()]
    if not sys.platform.startswith("win"):
        candidates += [n for n in names if n.startswith("lib")]
    return candidates


__all__ = [
    "APP_DATA_DIRNAME",
    "DATA_DIR_ENV",
    "KEY_FILENAMES",
    "NATIVE_DIR_ENV",
    "NATIVE_SUBDIR",
    "PORTABLE_MARKER",
    "config_path",
    "key_filename",
    "key_path",
    "managed_data_dir",
    "native_lib_candidates",
    "native_lib_names",
    "portable_mode",
    "private_dir",
    "private_dir_candidates",
]
