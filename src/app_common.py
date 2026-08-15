# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""App-level plumbing shared by the viewer and the walker: native-DLL
preflight, parser-warning surfacing, pin natural-sort, and the JSON
config store. UI-light on purpose (tkinter only for the warning popup);
anything render-related lives in board_canvas.py instead."""

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

import tkinter as tk
from tkinter import messagebox

from .parsers.boardview import BoardModel


def check_native_dlls(app: str = "viewer") -> None:
    """Probe the three native shared libraries that accelerate boardview parsing
    and warn the user (to stderr) if any are missing.

    Without these the cold-load times balloon dramatically:
      * tvw_native — TVW pad/poly/net scanners (+1-2 s on each .tvw)
      * xzz_native — XZZPCB DES decryption  (+30-60 s on each .pcb)
      * rc6_native — ASUS .fz RC6 decryption (+6 s on each ASUS .fz;
                         ASRock .fz is unaffected — it only uses zlib)

    The apps still run without them — this is a perf warning, not an
    error. Useful when shipping to a colleague and forgetting to
    bundle the compiled libraries alongside the .py files. `app` is
    the stderr log prefix ('viewer' / 'walker').

    ASCII-only messages so they render on cp1252 / cp437 consoles."""
    import sys

    # Build platform-appropriate library name hints.
    if sys.platform.startswith("win"):
        ext = ".dll"
        lib_prefix = ""
    elif sys.platform == "darwin":
        ext = ".dylib"
        lib_prefix = "lib"
    else:
        ext = ".so"
        lib_prefix = "lib"

    # Compile whatever is missing BEFORE probing for it -- never after, and
    # never on a worker thread. Three separate facts each rule that out:
    #   * the ctypes wrappers latch their first load result (tvw_native._LIB
    #     becomes False, xzz_native._LOAD_ATTEMPTED True, fz_parser._NATIVE_RC6
    #     False), so a library that appears after the first probe stays
    #     invisible for the rest of the process;
    #   * Windows keys LoadLibrary by base name, so reloading a replaced
    #     tvw_native.dll hands back the module already mapped;
    #   * Windows refuses to replace a mapped DLL at all, so a build must
    #     never race a load.
    # native_build returns a report rather than raising: an absent, wrong or
    # broken compiler simply leaves `missing` populated below.
    try:
        from .native_build import ensure_native_libraries
        report = ensure_native_libraries(
            log=lambda message: print(f"[{app}] {message}", file=sys.stderr))
    except Exception as exc:  # pragma: no cover - defence in depth
        report = None
        print(f"[{app}] native auto-build unavailable: {exc}", file=sys.stderr)

    missing: List[Tuple[str, str]] = []  # (library filename, slowdown)

    # tvw_native -- exposes _load() returning the lib (or None on miss).
    try:
        from .parsers.tvw_native import _load as _load_tvw
        if _load_tvw() is None:
            missing.append((
                f"{lib_prefix}tvw_native{ext}",
                "+1-2 s per .tvw cold load (slower pad/net/poly scans)",
            ))
    except Exception:
        missing.append((
            f"{lib_prefix}tvw_native{ext}",
            "+1-2 s per .tvw cold load",
        ))

    # xzz_native -- has a clean public available() helper.
    try:
        from .parsers import xzz_native
        if not xzz_native.available():
            missing.append((
                f"{lib_prefix}xzz_native{ext}",
                "+30-60 s per .pcb (XZZPCB) cold load: DES in pure Python",
            ))
    except Exception:
        missing.append((
            f"{lib_prefix}xzz_native{ext}",
            "+30-60 s per .pcb (XZZPCB) cold load",
        ))

    # rc6_native -- private helper inside fz_parser.py. Only matters for
    # ASUS .fz; ASRock .fz files don't need RC6 at all.
    try:
        from .parsers.fz_parser import _load_native_rc6
        if _load_native_rc6() is None:
            missing.append((
                f"{lib_prefix}rc6_native{ext}",
                "+6 s per ASUS .fz cold load (ASRock .fz unaffected)",
            ))
    except Exception:
        missing.append((
            f"{lib_prefix}rc6_native{ext}",
            "+6 s per ASUS .fz cold load",
        ))

    if not missing:
        return
    print(
        f"[{app}] WARNING: one or more native shared libraries are missing -- cold "
        "loads will be much slower:",
        file=sys.stderr,
    )
    for name, slowdown in missing:
        print(f"  - {name}: {slowdown}", file=sys.stderr)
    # Why the automatic build did not supply them, and what to do about it.
    # The meson command is still the canonical build, so it stays as the
    # last line of advice -- it is just no longer the only thing we tell a
    # user who already has a perfectly good compiler installed.
    for line in (report.advice() if report is not None else ()):
        print(f"[{app}] {line}", file=sys.stderr)
    print(
        f"[{app}] These libraries live in src/parsers/native/ next to the "
        "matching .py wrappers. The app will still run — this is a perf "
        "warning, not an error.",
        file=sys.stderr,
    )


def surface_model_warnings(model: BoardModel, parent=None, *,
                           app: str = "viewer") -> None:
    """If the parser flagged anything on `model.warnings`, show it to
    the user as a single modal popup. Silent for parsers that don't
    set the attribute, or for clean parses (empty list).

    Used to surface partial-parse situations the loader can't or won't
    raise for — e.g. XZZPCB without a configured key, where the model
    still loads but is missing every encrypted part/pin record."""
    warnings = getattr(model, "warnings", None)
    if not warnings:
        return
    title = "Boardview parsed with warnings"
    body = (
        "The boardview loaded, but the parser flagged the following — "
        "parts of the board may be missing from the model:\n\n"
        + "\n".join(f"  • {w}" for w in warnings)
    )
    try:
        messagebox.showwarning(title, body, parent=parent)
    except tk.TclError:
        import sys
        print(f"[{app}] {title}", file=sys.stderr)
        for w in warnings:
            print(f"  - {w}", file=sys.stderr)


def pin_sort_key(pn: Tuple[str, str]) -> Tuple[int, str, int, str]:
    """Natural-sort pins. Numeric pins first, then BGA-style (alpha+digits)
    grouped by alpha, then anything else lexicographically."""
    p = pn[0]
    try:
        return (0, "", int(p), "")
    except ValueError:
        pass
    m = re.match(r"^([A-Z]+)(\d+)([A-Z]*)$", p.upper())
    if m:
        alpha, num, suffix = m.groups()
        return (1, alpha, int(num), suffix)
    return (2, p, 0, "")


class ConfigStore:
    """Tiny JSON config store shared by the viewer and the walker.

    `path_provider` is a zero-arg callable returning the config file
    location; it is re-resolved on every access so the frozen/portable
    path decision in runtime_paths stays lazy. Load failures return {}
    and save failures are swallowed — config is a convenience, never
    worth crashing the app over."""

    def __init__(self, path_provider: Callable[[], Path]):
        self._path_provider = path_provider

    def load(self) -> Dict[str, Any]:
        try:
            return json.loads(
                self._path_provider().read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def save(self, config: Dict[str, Any]) -> None:
        try:
            path = self._path_provider()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(config, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass


__all__ = [
    "ConfigStore",
    "check_native_dlls",
    "pin_sort_key",
    "surface_model_warnings",
]
