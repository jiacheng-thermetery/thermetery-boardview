# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""Writable path policy shared by source and frozen builds.

Source checkouts retain the historical paths so existing developer setups keep
working. A frozen Windows build writes beside the executable when the portable
marker is present, or below LocalAppData when installed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, Optional


APP_DATA_DIRNAME = "Thermetery Boardviewer"
DATA_DIR_ENV = "BOARDVIEWER_DATA_DIR"
PORTABLE_MARKER = "portable.flag"


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


def key_path(fmt: str) -> Path:
    normalized = fmt.strip().lower()
    if normalized == "fz":
        name = "fz_key.txt"
    elif normalized in {"xzz", "xzzpcb", "pcb"}:
        name = "XZZ_Key.txt"
    else:
        raise ValueError(f"unknown key format: {fmt!r}")
    return private_dir() / name


__all__ = [
    "APP_DATA_DIRNAME",
    "DATA_DIR_ENV",
    "PORTABLE_MARKER",
    "config_path",
    "key_path",
    "managed_data_dir",
    "portable_mode",
    "private_dir",
]
