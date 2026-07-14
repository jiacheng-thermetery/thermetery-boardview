# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""Offline decryption-key validation and local storage.

Single source of truth for key handling shared by the desktop key manager
(``src.viewer``) and the Android bridge (``board_export.validate_key``). The
parity / structural checks themselves live in the format parsers; this module
maps their results to a ``(status, message)`` pair and manages the on-disk key
files under :func:`runtime_paths.key_path`.

Status vocabulary (kept stable — the Android bridge serializes these verbatim):
``valid`` | ``invalid`` | ``unverified`` | ``malformed`` | ``unknown_format``
| ``error``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from .runtime_paths import key_path


def validate_key_text(fmt: str, text: str) -> Tuple[str, str]:
    """Validate a pasted/loaded key WITHOUT a board. Returns ``(status, message)``.

    * ASUS ``fz`` (RC6) is fully verifiable offline: the text must parse to 44
      hex words and pass OpenBoardView's parity check.
    * XZZ ``xzzpcb`` (DES) is only structurally checkable offline — a DES key
      has no self-validating parity — so a well-formed key is ``unverified``.

    Never raises: any parser failure is reported as ``("error", message)`` so
    both callers stay total.
    """
    f = (fmt or "").strip().lower()
    if f in ("pcb", "xzz", "xzzpcb"):
        f = "xzzpcb"
    try:
        if f == "fz":
            from .parsers import fz_parser
            words = fz_parser._parse_fz_key_text(text or "")
            if words is None:
                return "malformed", "Need exactly 44 hex words (32-bit each)."
            if fz_parser._validate_fz_key(words):
                return "valid", "Valid ASUS key — parity check passed."
            return ("invalid",
                    "44 words parsed but the parity check failed "
                    "— this is not a correct ASUS key.")
        if f == "xzzpcb":
            from .parsers import xzzpcb_parser
            if xzzpcb_parser._parse_key_text(text or "") is None:
                return "malformed", "Need a hex key value (e.g. 16 hex digits)."
            return ("unverified",
                    "Key is well-formed. XZZ keys can only be fully "
                    "verified by opening an encrypted board.")
        return "unknown_format", "Unknown key format: " + f
    except Exception as exc:  # never raise across the bridge / into the UI
        return "error", str(exc)


def is_savable(status: str) -> bool:
    """Whether a validation status is safe to persist (well-formed key)."""
    return status in ("valid", "unverified")


def read_key(fmt: str) -> Optional[str]:
    """Return the saved key text for ``fmt``, or ``None`` if there is none."""
    try:
        text = key_path(fmt).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def write_key(fmt: str, text: str) -> Path:
    """Persist ``text`` as the key for ``fmt``; returns the file path."""
    dest = key_path(fmt)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text.strip() + "\n", encoding="utf-8")
    return dest


def clear_key(fmt: str) -> bool:
    """Delete the saved key for ``fmt``; returns True iff a file was removed."""
    try:
        key_path(fmt).unlink()
        return True
    except OSError:
        return False


__all__ = [
    "validate_key_text",
    "is_savable",
    "read_key",
    "write_key",
    "clear_key",
]
