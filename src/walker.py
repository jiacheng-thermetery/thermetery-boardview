# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC
#
# Power-sequencing diagnostic walker. The UI, signal-resolution engine,
# and Claude integration are this project's. Diagnostic rules (the
# per-chipset YAML the walker consumes) are loaded at runtime from a
# user-supplied file and are not shipped with the repository.
#
# The BoardCanvasCPU and BoardCanvasGL classes below are kept in sync
# with their counterparts in the sister boardviewer project (same
# authors); cross-layer trace inspection and inner-layer ghosts are
# implemented identically in both.

"""
Interactive power-sequencing walker for board-level repair.

Three-pane main layout:
  - Left:   step list (click to jump; status column shows ✓/✗/⊘)
  - Middle: signal info + clickable probe candidate list
  - Right:  board canvas (top) + Component/Net tabs (bottom)

Plus:
  - Diagnosis helper below the main panes
  - Claude assistant chat panel at the bottom (Opus 4.7 via the anthropic SDK)
  - Buttons at the very bottom

Board canvas: drag to pan, mouse wheel to zoom, Home or "Reset view" to
fit-to-window. The "View: TOP/BOTTOM" toggle (or L key) flips the layer;
bottom is mirrored horizontally to match the physically flipped board.
Clicking, finding, or stepping to a probe on the other layer auto-flips.

Click any IC to select it. While an IC is selected, every pin from its
shape is rendered as a yellow dot; click a pin to focus on it. The
matching row highlights in the **Component** tab AND the **Net** tab
fills with every component on that pin's net. Clicking a row in either
tab takes you back to the relevant pin/component (the Net tab also
auto-flips the layer if needed).

Claude chat: collapsible bottom panel. Each user message bundles the
current step, selected component, and recent results as context, so you
can ask follow-up questions without re-explaining. Streamed responses,
prompt-cached system prompt, max effort, adaptive thinking with
summarized display.

Diagnosis helper shows section progress and (on FAIL) what to investigate
next. Progress is saved per-platform to private/walker_state_*.json.

Usage:
    python walker.py [<rules.yaml> <board.cad> <platform_prefix>]

Set ANTHROPIC_API_KEY in your environment to enable the Claude chat panel.
Without the key (or without `pip install anthropic`), the panel shows a
setup hint instead.
"""

import argparse
import json
import math
import os
import re
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import yaml

from .parsers.boardview import BoardModel, Component, parse as parse_board, is_stub_format
from .linker import link_platform


def _check_native_dlls() -> None:
    """Probe the three native shared libraries that accelerate boardview parsing
    and warn the user (to stderr) if any are missing.

    Without these the cold-load times balloon dramatically:
      * tvw_native — TVW pad/poly/net scanners (+1-2 s on each .tvw)
      * xzz_native — XZZPCB DES decryption  (+30-60 s on each .pcb)
      * rc6_native — ASUS .fz RC6 decryption (+6 s on each ASUS .fz;
                         ASRock .fz is unaffected — it only uses zlib)

    Walker still runs without them — this is a perf warning, not an
    error. Useful when shipping the walker to a colleague and forgetting
    to bundle the compiled libraries alongside the .py files."""
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

    def _hint(name: str) -> str:
        return (
            f"meson compile -C build && cp build/parsers/{lib_prefix}{name}{ext} src/parsers/native/"
        )

    missing: List[Tuple[str, str, str]] = []  # (name, slowdown, build hint)

    # tvw_native -- exposes _load() returning the lib (or None on miss).
    # ASCII-only messages so they render on cp1252 / cp437 consoles.
    try:
        from .parsers.tvw_native import _load as _load_tvw
        if _load_tvw() is None:
            missing.append((
                f"{lib_prefix}tvw_native{ext}",
                "+1-2 s per .tvw cold load (slower pad/net/poly scans)",
                _hint("tvw_native"),
            ))
    except Exception:
        missing.append((
            f"{lib_prefix}tvw_native{ext}",
            "+1-2 s per .tvw cold load",
            _hint("tvw_native"),
        ))

    # xzz_native -- has a clean public available() helper.
    try:
        from .parsers import xzz_native
        if not xzz_native.available():
            missing.append((
                f"{lib_prefix}xzz_native{ext}",
                "+30-60 s per .pcb (XZZPCB) cold load: DES in pure Python",
                _hint("xzz_native"),
            ))
    except Exception:
        missing.append((
            f"{lib_prefix}xzz_native{ext}",
            "+30-60 s per .pcb (XZZPCB) cold load",
            _hint("xzz_native"),
        ))

    # rc6_native -- private helper inside fz_parser.py. Only matters for
    # ASUS .fz; ASRock .fz files don't need RC6 at all.
    try:
        from .parsers.fz_parser import _load_native_rc6
        if _load_native_rc6() is None:
            missing.append((
                f"{lib_prefix}rc6_native{ext}",
                "+6 s per ASUS .fz cold load (ASRock .fz unaffected)",
                _hint("rc6_native"),
            ))
    except Exception:
        missing.append((
            f"{lib_prefix}rc6_native{ext}",
            "+6 s per ASUS .fz cold load",
            _hint("rc6_native"),
        ))

    if not missing:
        return
    print(
        "[walker] WARNING: one or more native shared libraries are missing -- cold "
        "loads will be much slower:",
        file=sys.stderr,
    )
    for name, slowdown, hint in missing:
        print(f"  - {name}: {slowdown}", file=sys.stderr)
        print(f"      build: {hint}", file=sys.stderr)
    print(
        "[walker] These libraries live in src/parsers/native/ next to the "
        "matching .py wrappers. Walker will still run -- this is a perf "
        "warning, not an error.",
        file=sys.stderr,
    )


def _surface_model_warnings(model: BoardModel, parent=None) -> None:
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
        # No Tk root yet (early startup before WalkerApp is built). Fall
        # back to stderr so the message still gets out.
        import sys
        print(f"[walker] {title}", file=sys.stderr)
        for w in warnings:
            print(f"  - {w}", file=sys.stderr)

# Optional Skia + numpy stack for fast trace rendering. The trace layer
# can have 40 k+ segments; tk.Canvas's per-line round-trip makes that
# unworkable. With Skia we render the whole frame to an off-screen surface
# (~30-50 ms), composite the premultiplied-alpha output onto the canvas
# background colour in numpy, build a binary PPM byte string, and hand it
# to tk.PhotoImage(data=ppm) — that path uses Tcl's C image loader (~10 ms
# for 1920×1080) and avoids the 2-second-per-frame ImageTk un-premul cost.
# Falls back to the tk-line path if any dep is missing.
try:
    import numpy as _np
    import skia as _skia
    _SKIA_AVAILABLE = True
except ImportError:
    _SKIA_AVAILABLE = False


# Optional GPU rendering stack: pyopengltk's OpenGLFrame + Skia's GL
# backend. When available the BoardCanvasGL class subclasses OpenGLFrame
# and drives a Skia GrDirectContext-backed surface for sub-10ms frames
# even at heavy zoom on 13k+ trace boards. Falls through to BoardCanvasCPU
# (the renamed CPU+PPM path) if either pyopengltk or PyOpenGL is missing,
# or if the GL probe at startup fails.
try:
    from pyopengltk import OpenGLFrame as _OpenGLFrame  # type: ignore
    from OpenGL import GL as _GL  # type: ignore
    _GL_AVAILABLE = _SKIA_AVAILABLE  # GL path needs skia too
except ImportError:
    _OpenGLFrame = None  # type: ignore
    _GL = None  # type: ignore
    _GL_AVAILABLE = False


try:
    import anthropic  # type: ignore
    _HAS_ANTHROPIC = True
except ImportError:
    anthropic = None  # type: ignore
    _HAS_ANTHROPIC = False

try:
    import openai  # type: ignore
    _HAS_OPENAI = True
except ImportError:
    openai = None  # type: ignore
    _HAS_OPENAI = False

try:
    import fitz  # PyMuPDF — schematic PDF rendering  # type: ignore
    _HAS_FITZ = True
except ImportError:
    fitz = None  # type: ignore
    _HAS_FITZ = False

try:
    import keyring  # type: ignore
    import keyring.errors  # noqa: F401
    _HAS_KEYRING = True
except ImportError:
    keyring = None  # type: ignore
    _HAS_KEYRING = False

_KEYRING_SERVICE = "walker_diagnostic"


# ----- Chat backends ------------------------------------------------------

BACKEND_ANTHROPIC = "anthropic"
BACKEND_OPENAI = "openai"
BACKEND_OLLAMA = "ollama"

# Order = dropdown order
BACKEND_ORDER: List[str] = [BACKEND_ANTHROPIC, BACKEND_OPENAI, BACKEND_OLLAMA]
BACKEND_LABELS: Dict[str, str] = {
    BACKEND_ANTHROPIC: "Anthropic",
    BACKEND_OPENAI:    "OpenAI",
    BACKEND_OLLAMA:    "Ollama (local)",
}
BACKEND_LABEL_TO_ID: Dict[str, str] = {v: k for k, v in BACKEND_LABELS.items()}

# Per-provider keyring usernames (so each provider has its own slot)
KEYRING_USERNAMES: Dict[str, str] = {
    BACKEND_ANTHROPIC: "anthropic_api_key",
    BACKEND_OPENAI:    "openai_api_key",
    BACKEND_OLLAMA:    "",  # no key needed for local
}

# Hardcoded model lists per remote provider. Ollama is fetched at runtime.
ANTHROPIC_MODELS: List[Tuple[str, str]] = [
    ("Opus 4.7",   "claude-opus-4-7"),
    ("Opus 4.6",   "claude-opus-4-6"),
    ("Sonnet 4.6", "claude-sonnet-4-6"),
    ("Haiku 4.5",  "claude-haiku-4-5"),
]
OPENAI_MODELS: List[Tuple[str, str]] = [
    ("GPT-4o",      "gpt-4o"),
    ("GPT-4o mini", "gpt-4o-mini"),
    ("o1",          "o1"),
    ("o1 mini",     "o1-mini"),
    ("o3 mini",     "o3-mini"),
]

# Effort options per model. Anthropic uses output_config.effort with its own
# scale; OpenAI o-series uses reasoning_effort with low/medium/high; non-
# reasoning models and Ollama have no effort knob.
EFFORT_BY_MODEL: Dict[str, List[str]] = {
    # Anthropic
    "claude-opus-4-7":   ["low", "medium", "high", "xhigh", "max"],
    "claude-opus-4-6":   ["low", "medium", "high", "max"],
    "claude-sonnet-4-6": ["low", "medium", "high"],
    "claude-haiku-4-5":  [],
    # OpenAI o-series
    "o1":      ["low", "medium", "high"],
    "o1-mini": ["low", "medium", "high"],
    "o3-mini": ["low", "medium", "high"],
    # OpenAI chat models — no reasoning_effort
    "gpt-4o":      [],
    "gpt-4o-mini": [],
}
DEFAULT_EFFORT: Dict[str, str] = {
    "claude-opus-4-7":   "max",
    "claude-opus-4-6":   "high",
    "claude-sonnet-4-6": "medium",
    "claude-haiku-4-5":  "",
    "o1":      "high",
    "o1-mini": "medium",
    "o3-mini": "medium",
    "gpt-4o":      "",
    "gpt-4o-mini": "",
}
DEFAULT_MODEL_PER_BACKEND: Dict[str, str] = {
    BACKEND_ANTHROPIC: "claude-opus-4-7",
    BACKEND_OPENAI:    "gpt-4o",
    BACKEND_OLLAMA:    "",  # filled in from /api/tags at runtime
}

NO_EFFORT_LABEL = "(n/a)"
OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1"


SEMANTIC_COLORS = {
    "critical_rail":      "#cc2a2a",
    "control_signal":     "#cc8800",
    "target_rail":        "#1a8a1a",
    "metadata_highlight": "#3060c0",
}

RESULT_COLORS = {
    "pass": "#1a8a1a",
    "fail": "#cc2a2a",
    "skip": "#888888",
}


CHAT_MAX_TOKENS = 8192

CHAT_SYSTEM_PROMPT = """\
You are an expert assistant for board-level motherboard repair, integrated \
into a power-sequencing diagnostic walker.

The walker links three artifacts:
  1. A GENCAD boardview file — the physical board (components, footprints,
     pins, nets, layer, X/Y coordinates).
  2. A rules YAML — per-chipset
     flow grouped into sections (no_trigger, post_trigger_fault,
     memory_not_detected, etc.), with stages (G3 / DDEP / S5 / triggers /
     SLP_S* / CLOCK / CPU core / RESET) and per-signal entries.
  3. A linker that cross-references signal names from the rules to nets in
     the boardview.

Each rule signal has:
  • a net name (e.g. VCCRTC, RSMRST#, PWROK, SLP_S3#)
  • an expected voltage (e.g. "2.8V-3.3V", "待机 0V/开机 3.3V" = "standby 0V / on 3.3V")
  • an expected resistance-to-ground in ohms (e.g. "400 左右" ≈ "~400Ω")
  • a semantic flag: critical_rail (red), control_signal (yellow), target_rail (green)
  • a section that names the failure mode if this signal is wrong
  • probe candidates: refdes/pin pairs on the matched boardview net

At the start of every user message you'll see a [Walker context] block
with: the current step, the component/pin currently selected on the
canvas, and pass/fail counts so far. Use it — don't ask the user to
re-state where they are.

The user is an experienced board-level repair tech. Treat them as a peer, not a beginner:
  • Be terse. Direct. No preamble.
  • Skip basics — don't explain what RSMRST# is in general; jump to what's
    likely wrong here.
  • When you suggest probes, name specific (refdes, pin) and what voltage
    or resistance to expect.
  • Distinguish what you know with confidence (Intel chipset architecture,
    standard PMIC/VRM behavior, typical failure modes for caps/MOSFETs/BGA)
    from what you're guessing.
  • If a measurement is borderline, say what 'borderline' means here in
    practical terms (e.g. "0.8V is too low for a 1.05V rail — check the
    enable signal on the controller and the inductor for shorts").
  • Resistance-to-ground readings are from the user's multimeter in
    diode/resistance mode (red probe on the net, black probe on ground).
    A reading much lower than expected suggests a short; much higher
    suggests an open or missing decoupling.

Knowledge you should bring:
  • Intel PCH power sequencing: VCCRTC → DSW rails → RSMRST# release →
    SLP_SUS# → SUSACK# → S5 rails → trigger → SLP_S4#/S3# → SLP_S3# release →
    DDR/CHIPSET rails → VR_EN → CPU core → PWROK chain → PLT_RST# release.
  • Common board-level shorthand: PCH = Cougar Point chip; SIO = Super I/O
    (often Fintek, ITE, Nuvoton); VRD12 = uPI Semi or similar PWM controller
    pattern; LGA1155 = Sandy/Ivy Bridge socket.
  • The MS-7680 board family in particular uses _CP and _SIO suffixes on
    sequencing signals (e.g. SLP_SUS#_CP vs SLP_SUS#_SIO) where a 0Ω
    resistor or buffer separates the PCH side from the SIO side.

When the linker reports "no boardview match" for a signal, the rule's net
name doesn't appear in this board's netlist. Either the board uses a
different name (alias gap), or the signal is internal to a chip (BGA pin
only). Suggest probing at the chipset BGA pin from the datasheet.

Output format: short paragraphs or tight bulleted lists. Code blocks only
when literally showing a command or table. No markdown headers.
"""


def _pin_sort_key(pn: Tuple[str, str]) -> Tuple[int, str, int, str]:
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


# ----- Layer palette ------------------------------------------------------
#
# Per-layer colors used by both the CPU and GL trace renderers. Inner layers
# come from a 6-entry palette cycled by the index in the layer name; the
# outer copper keeps the long-standing TOP=blue / BOTTOM=red identity.
# Each tuple is (bright, dim) — bright is for the highlighted-net overlay,
# dim is for the all-traces background.
_LAYER_OUTER = {
    "TOP":    ("#5b8fff", "#1c2c50"),
    "BOTTOM": ("#ff6b5b", "#3a1c14"),
}
_LAYER_INNER_PALETTE = [
    ("#5bff8f", "#1c5025"),  # green
    ("#bf5bff", "#350c4d"),  # purple
    ("#5bffe1", "#0c4d44"),  # cyan
    ("#ffaa5b", "#4d2d10"),  # orange
    ("#ff5bbf", "#4d0c35"),  # pink
    ("#bfff5b", "#3a4d0c"),  # lime
]


def _layer_color(layer: str, *, dim: bool = False) -> str:
    """Return the palette color for a layer name. `dim=False` gives the
    bright (highlight) tone, `dim=True` gives the muted background tone.
    Unknown layer names fall back to TOP."""
    if layer in _LAYER_OUTER:
        bright, dimmed = _LAYER_OUTER[layer]
        return dimmed if dim else bright
    if layer.startswith("INNER_"):
        try:
            idx = int(layer.split("_", 1)[1]) - 1
        except (ValueError, IndexError):
            idx = 0
        bright, dimmed = _LAYER_INNER_PALETTE[idx % len(_LAYER_INNER_PALETTE)]
        return dimmed if dim else bright
    bright, dimmed = _LAYER_OUTER["TOP"]
    return dimmed if dim else bright


def _available_layers_for(board: BoardModel) -> List[str]:
    """Layers the user can switch the viewport to. Always at least
    [TOP, BOTTOM] — the data model's `Component.layer` is constrained to
    those two regardless of how many copper layers a board has. Boards
    with a built trace topology contribute their `_layer_names` so inner
    copper (INNER_1..N on multi-layer GPU PCBs) shows up too. The
    topology is NOT built here; we only read `_layer_names` if it was
    already cached (i.e. the user has enabled the trace overlay at
    least once)."""
    base = ["TOP", "BOTTOM"]
    topo = getattr(board, "_topology", None)
    if topo is None:
        return base
    extra = list(getattr(topo, "_layer_names", []) or [])
    if not extra:
        return base
    seen = set(base)
    out = list(base)
    for name in extra:
        if name not in seen:
            out.append(name)
            seen.add(name)
    return out


# ----- Persisted config (last-used dirs + recent file list) ---------------

_CONFIG_PATH = Path("private") / "walker_config.json"
_RECENT_LIMIT = 10


def _load_config() -> Dict[str, Any]:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_config(config: Dict[str, Any]) -> None:
    try:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(
            json.dumps(config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _last_dir(kind: str) -> Optional[str]:
    return _load_config().get("last_dirs", {}).get(kind)


def _remember_dir(kind: str, path: Path) -> None:
    config = _load_config()
    config.setdefault("last_dirs", {})[kind] = str(path.parent)
    _save_config(config)


def _get_recent() -> List[Dict[str, str]]:
    return _load_config().get("recent", [])


def _add_recent(rules: Path, board: Path, platform: str) -> None:
    config = _load_config()
    entry = {"rules": str(rules), "board": str(board), "platform": platform}
    recent = [
        r for r in config.get("recent", [])
        if not (r.get("rules") == entry["rules"]
                and r.get("board") == entry["board"]
                and r.get("platform") == entry["platform"])
    ]
    recent.insert(0, entry)
    config["recent"] = recent[:_RECENT_LIMIT]
    _save_config(config)


def _clear_recent_persisted() -> None:
    config = _load_config()
    config["recent"] = []
    _save_config(config)


def _load_chat_settings() -> Dict[str, Any]:
    """Return the chat settings dict (provider, per-provider model/effort,
    ollama_base_url). Migrates pre-multi-provider configs."""
    chat = _load_config().get("chat", {}) or {}

    # Migration from the single-provider format (had top-level "model"/"effort")
    if "providers" not in chat:
        legacy_model = chat.get("model") or DEFAULT_MODEL_PER_BACKEND[BACKEND_ANTHROPIC]
        legacy_effort = chat.get("effort", "")
        chat = {
            "provider": BACKEND_ANTHROPIC,
            "providers": {
                BACKEND_ANTHROPIC: {"model": legacy_model, "effort": legacy_effort},
                BACKEND_OPENAI:    {"model": DEFAULT_MODEL_PER_BACKEND[BACKEND_OPENAI],
                                    "effort": DEFAULT_EFFORT.get(
                                        DEFAULT_MODEL_PER_BACKEND[BACKEND_OPENAI], "")},
                BACKEND_OLLAMA:    {"model": "", "effort": ""},
            },
            "ollama_base_url": OLLAMA_DEFAULT_BASE_URL,
        }

    chat.setdefault("provider", BACKEND_ANTHROPIC)
    if chat["provider"] not in BACKEND_ORDER:
        chat["provider"] = BACKEND_ANTHROPIC
    chat.setdefault("providers", {})
    for prov in BACKEND_ORDER:
        chat["providers"].setdefault(prov, {
            "model": DEFAULT_MODEL_PER_BACKEND.get(prov, ""),
            "effort": DEFAULT_EFFORT.get(
                DEFAULT_MODEL_PER_BACKEND.get(prov, ""), "") or "",
        })
    chat.setdefault("ollama_base_url", OLLAMA_DEFAULT_BASE_URL)
    return chat


def _save_chat_settings(chat: Dict[str, Any]) -> None:
    config = _load_config()
    config["chat"] = chat
    _save_config(config)


def _get_ollama_base_url() -> str:
    return _load_chat_settings().get("ollama_base_url") or OLLAMA_DEFAULT_BASE_URL


def _set_ollama_base_url(url: str) -> None:
    chat = _load_chat_settings()
    chat["ollama_base_url"] = url.strip() or OLLAMA_DEFAULT_BASE_URL
    _save_chat_settings(chat)


# ----- API key handling ---------------------------------------------------
#
# Storage backend: OS keyring via the `keyring` package.
#   • Windows: WinVaultKeyring (Credential Manager — DPAPI-encrypted, locked
#     to the current user account).
#   • macOS: KeychainKeyring.
#   • Linux: SecretService (GNOME Keyring / KWallet).
#
# Keys never touch walker_config.json. If a legacy plaintext key is found in
# the config (saved by an earlier version of the app), `_get_stored_api_key`
# migrates it to the keyring on first access and deletes the plaintext copy.


_PROVIDER_ENV_VARS: Dict[str, str] = {
    BACKEND_ANTHROPIC: "ANTHROPIC_API_KEY",
    BACKEND_OPENAI:    "OPENAI_API_KEY",
    BACKEND_OLLAMA:    "",  # local, no env var
}


def _get_stored_api_key(provider: str = BACKEND_ANTHROPIC) -> str:
    """Return the user's stored API key for `provider`, or "" if none.
    Migrates the (single-key) legacy plaintext format into the keyring."""
    username = KEYRING_USERNAMES.get(provider, "")
    if not username:
        return ""
    if _HAS_KEYRING:
        try:
            key = keyring.get_password(_KEYRING_SERVICE, username)
            if key:
                return key.strip()
        except Exception:
            pass
    # Legacy migration: pre-multi-provider builds stored a single
    # plaintext "api_key" in walker_config.json (Anthropic only).
    if provider == BACKEND_ANTHROPIC:
        legacy = (_load_config().get("api_key") or "").strip()
        if legacy and _HAS_KEYRING:
            try:
                keyring.set_password(_KEYRING_SERVICE, username, legacy)
                cfg = _load_config()
                cfg.pop("api_key", None)
                _save_config(cfg)
            except Exception:
                pass
        return legacy
    return ""


def _save_stored_api_key(key: str, provider: str = BACKEND_ANTHROPIC) -> None:
    """Save (or clear) the API key for `provider`. Raises RuntimeError if
    keyring isn't available so the caller can surface a clear instruction."""
    username = KEYRING_USERNAMES.get(provider, "")
    if not username:
        return  # no key needed for this provider (e.g. Ollama)
    key = (key or "").strip()
    # Strip any legacy plaintext on first save
    cfg = _load_config()
    if "api_key" in cfg:
        cfg.pop("api_key", None)
        _save_config(cfg)
    if not _HAS_KEYRING:
        if key:
            raise RuntimeError(
                "The `keyring` package is required to save API keys.\n"
                "Run:  pip install keyring\nthen restart the walker.")
        return
    try:
        if key:
            keyring.set_password(_KEYRING_SERVICE, username, key)
        else:
            try:
                keyring.delete_password(_KEYRING_SERVICE, username)
            except Exception:
                pass
    except Exception as exc:
        raise RuntimeError(f"Could not save key to system keyring:\n{exc}")


def _resolve_api_key(provider: str = BACKEND_ANTHROPIC) -> Tuple[Optional[str], str]:
    """Return (key, source) for `provider`. Source is 'keyring', 'env', 'missing'."""
    if not KEYRING_USERNAMES.get(provider, ""):
        return (None, "n/a")  # local, no key concept
    stored = _get_stored_api_key(provider)
    if stored:
        return (stored, "keyring")
    env_var = _PROVIDER_ENV_VARS.get(provider, "")
    if env_var:
        env_key = (os.environ.get(env_var) or "").strip()
        if env_key:
            return (env_key, "env")
    return (None, "missing")


def _keyring_backend_label() -> str:
    """Human-readable name of the active keyring backend, or "" if missing."""
    if not _HAS_KEYRING:
        return ""
    try:
        kr = keyring.get_keyring()
        return type(kr).__name__
    except Exception:
        return "unknown"


def _key_tail(key: str) -> str:
    """Render the last 4 chars of a key with ellipsis, for safe display."""
    if not key:
        return ""
    return f"…{key[-4:]}" if len(key) > 4 else "…"


# ----- Chat backend abstraction -------------------------------------------

class ChatBackend:
    """Common interface for streaming chat completions across providers.
    Subclass methods get a `cb` dict with keys:
      'on_thinking_start'(): assistant began a thinking block
      'on_thinking_chunk'(text)
      'on_text_start'(): assistant began emitting answer text
      'on_text_chunk'(text)
      'on_complete'(usage_dict): {input_tokens, output_tokens, ...,
                                  messages=[final assistant content]}
      'cancel': threading.Event — stream loops should poll this
    """
    name: str = ""
    label: str = ""

    def is_configured(self) -> Tuple[bool, str]:
        raise NotImplementedError

    def list_models(self, refresh: bool = False) -> List[Tuple[str, str]]:
        """Return [(label, model_id), ...]. May fetch from the provider."""
        raise NotImplementedError

    def supports_effort(self, model: str) -> List[str]:
        """Return effort options for `model`, or [] if none."""
        return EFFORT_BY_MODEL.get(model, [])

    def stream(
        self, system_prompt: str, messages: List[Dict[str, Any]],
        model: str, effort: str, cb: Dict[str, Any],
    ) -> None:
        raise NotImplementedError


class AnthropicChatBackend(ChatBackend):
    name = BACKEND_ANTHROPIC
    label = BACKEND_LABELS[BACKEND_ANTHROPIC]

    def is_configured(self) -> Tuple[bool, str]:
        if not _HAS_ANTHROPIC:
            return False, "anthropic SDK not installed (pip install anthropic)"
        key, _ = _resolve_api_key(BACKEND_ANTHROPIC)
        if not key:
            return False, "no Anthropic API key (Settings… or ANTHROPIC_API_KEY)"
        return True, ""

    def list_models(self, refresh: bool = False) -> List[Tuple[str, str]]:
        return list(ANTHROPIC_MODELS)

    def stream(self, system_prompt, messages, model, effort, cb):
        key, _ = _resolve_api_key(BACKEND_ANTHROPIC)
        client = anthropic.Anthropic(api_key=key)
        kwargs: Dict[str, Any] = {
            "model": model,
            "max_tokens": CHAT_MAX_TOKENS,
            "system": [{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            "messages": messages,
        }
        if model == "claude-opus-4-7":
            kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
        elif model in ("claude-opus-4-6", "claude-sonnet-4-6"):
            kwargs["thinking"] = {"type": "adaptive"}
        if effort and effort in self.supports_effort(model):
            kwargs["output_config"] = {"effort": effort}

        with client.messages.stream(**kwargs) as stream:
            cb["on_text_start"]()
            for event in stream:
                if cb["cancel"].is_set():
                    break
                if event.type == "content_block_start":
                    block = event.content_block
                    btype = getattr(block, "type", None)
                    if btype == "thinking":
                        cb["on_thinking_start"]()
                    elif btype == "text":
                        cb["on_text_start"]()
                elif event.type == "content_block_delta":
                    delta = event.delta
                    dtype = getattr(delta, "type", None)
                    if dtype == "thinking_delta":
                        cb["on_thinking_chunk"](getattr(delta, "thinking", ""))
                    elif dtype == "text_delta":
                        cb["on_text_chunk"](getattr(delta, "text", ""))
            final = stream.get_final_message()
            usage = getattr(final, "usage", None)
            cb["on_complete"]({
                "input_tokens":               getattr(usage, "input_tokens", 0) or 0,
                "output_tokens":              getattr(usage, "output_tokens", 0) or 0,
                "cache_read_input_tokens":    getattr(usage, "cache_read_input_tokens", 0) or 0,
                "cache_creation_input_tokens":getattr(usage, "cache_creation_input_tokens", 0) or 0,
                "messages":                   getattr(final, "content", None),
            })


class OpenAIChatBackend(ChatBackend):
    """OpenAI-flavored backend. Subclassed by Ollama for the local case."""
    name = BACKEND_OPENAI
    label = BACKEND_LABELS[BACKEND_OPENAI]

    def is_configured(self) -> Tuple[bool, str]:
        if not _HAS_OPENAI:
            return False, "openai SDK not installed (pip install openai)"
        key, _ = _resolve_api_key(BACKEND_OPENAI)
        if not key:
            return False, "no OpenAI API key (Settings… or OPENAI_API_KEY)"
        return True, ""

    def list_models(self, refresh: bool = False) -> List[Tuple[str, str]]:
        return list(OPENAI_MODELS)

    def _build_client(self):
        key, _ = _resolve_api_key(BACKEND_OPENAI)
        return openai.OpenAI(api_key=key)

    @staticmethod
    def _to_openai_messages(
        system_prompt: str, messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if system_prompt:
            out.append({"role": "system", "content": system_prompt})
        for m in messages:
            content = m.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if hasattr(block, "type") and getattr(block, "type", "") == "text":
                        parts.append(getattr(block, "text", ""))
                    elif isinstance(block, dict) and block.get("type") == "text":
                        parts.append(block.get("text", ""))
                text = "\n".join(p for p in parts if p)
            if text:
                out.append({"role": m["role"], "content": text})
        return out

    def stream(self, system_prompt, messages, model, effort, cb):
        client = self._build_client()
        oai_messages = self._to_openai_messages(system_prompt, messages)
        is_reasoning = model.startswith(("o1", "o3", "o4"))
        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # Reasoning models use max_completion_tokens, not max_tokens
        if is_reasoning:
            kwargs["max_completion_tokens"] = CHAT_MAX_TOKENS
            if effort and effort in ("low", "medium", "high"):
                kwargs["reasoning_effort"] = effort
        else:
            kwargs["max_tokens"] = CHAT_MAX_TOKENS

        cb["on_text_start"]()
        full_text = ""
        usage = None
        try:
            stream = client.chat.completions.create(**kwargs)
            for chunk in stream:
                if cb["cancel"].is_set():
                    try:
                        stream.close()
                    except Exception:
                        pass
                    break
                # Some chunks have only usage (final), no choices
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                for choice in (chunk.choices or []):
                    delta = getattr(choice, "delta", None)
                    if delta and getattr(delta, "content", None):
                        cb["on_text_chunk"](delta.content)
                        full_text += delta.content
        finally:
            cb["on_complete"]({
                "input_tokens":  getattr(usage, "prompt_tokens", 0) or 0,
                "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
                "cache_read_input_tokens":     0,
                "cache_creation_input_tokens": 0,
                # Re-emit full assistant turn as a single text block so
                # multi-turn history works with subsequent OpenAI calls and
                # also (degraded) with a later Anthropic call.
                "messages": [{"type": "text", "text": full_text}] if full_text else None,
            })


class OllamaChatBackend(OpenAIChatBackend):
    """Local Ollama via its OpenAI-compatible endpoint."""
    name = BACKEND_OLLAMA
    label = BACKEND_LABELS[BACKEND_OLLAMA]

    _cached_models: List[Tuple[str, str]] = []

    def is_configured(self) -> Tuple[bool, str]:
        if not _HAS_OPENAI:
            return False, "openai SDK not installed (pip install openai)"
        # No API key needed; assume reachable until proven otherwise
        return True, ""

    def supports_effort(self, model: str) -> List[str]:
        return []

    def _build_client(self):
        return openai.OpenAI(api_key="ollama", base_url=_get_ollama_base_url())

    def list_models(self, refresh: bool = False) -> List[Tuple[str, str]]:
        if refresh or not self._cached_models:
            try:
                tags = self._fetch_tags()
                self._cached_models = [(t, t) for t in tags]
            except Exception:
                self._cached_models = []
        return list(self._cached_models)

    @staticmethod
    def _fetch_tags() -> List[str]:
        """GET /api/tags — Ollama-native, returns installed models."""
        import urllib.request as _r
        import json as _json
        base = _get_ollama_base_url().rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        url = base + "/api/tags"
        with _r.urlopen(url, timeout=3) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


def _build_backends() -> Dict[str, ChatBackend]:
    return {
        BACKEND_ANTHROPIC: AnthropicChatBackend(),
        BACKEND_OPENAI:    OpenAIChatBackend(),
        BACKEND_OLLAMA:    OllamaChatBackend(),
    }


@dataclass
class Step:
    section_id: str
    section_diagnosis: str
    stage_label: str
    raw: Optional[str]
    net: Optional[str]
    expected_voltage: Optional[str]
    resistance_to_ground: Optional[str]
    semantic: Optional[str]
    note: Optional[str]
    step_text: Optional[str]
    boardview_net: Optional[str]
    probe_candidates: List[Dict[str, Any]]


def flatten_to_steps(linked: Dict[str, Any]) -> List[Step]:
    out: List[Step] = []
    # `.get` defaults — a "no-rules" launch passes an empty linked dict and
    # produces zero steps. The wizard UI methods then early-return on empty
    # `self.steps` so the walker stays usable as a board inspector.
    for section in linked.get("sections", []):
        for stage in section.get("stages", []):
            for sig in stage.get("signals", []):
                out.append(Step(
                    section_id=section.get("id") or "",
                    section_diagnosis=section.get("diagnosis_summary") or "",
                    stage_label=stage.get("label") or "",
                    raw=sig.get("raw"),
                    net=sig.get("net"),
                    expected_voltage=sig.get("expected_voltage"),
                    resistance_to_ground=sig.get("resistance_to_ground"),
                    semantic=sig.get("semantic"),
                    note=sig.get("note"),
                    step_text=sig.get("step"),
                    boardview_net=sig.get("boardview_net"),
                    probe_candidates=sig.get("probe_candidates") or [],
                ))
    return out


# ----- Board canvas -------------------------------------------------------
#
# BoardCanvasCPU and BoardCanvasGL below are kept in sync with the
# corresponding classes in boardviewer/viewer.py. Same authors, same
# project; the duplication exists because walker.py is the development /
# diagnostic tree and boardviewer/ is the carved-out viewer-only release.
# When something changes in one, mirror it to the other.

class BoardCanvasCPU(tk.Canvas):
    """Wireframe board renderer with TOP/BOTTOM layer toggle and pin-level
    selection while an IC is highlighted.

    This is the CPU fallback (Tier 2/3) path: trace overlay goes through
    `_draw_traces_skia` (Skia raster → PPM → tk.PhotoImage) when numpy +
    skia are available, else `_draw_traces_tk` (per-segment create_line).
    Components, pins, and labels are always plain tk.Canvas items.

    The GPU-accelerated counterpart is `BoardCanvasGL`. Both classes
    expose the same public API; `make_board_canvas()` picks the best
    backend at startup."""

    DOT_RADIUS = 1.4
    BG = "#0d1024"
    TOP_COLOR = "#5b8fff"
    BOTTOM_COLOR = "#ff6b5b"
    HIGHLIGHT = "#ffe45b"
    HIGHLIGHT_RING = "#ffffff"
    SELECTED_OUTLINE = "#22ddee"
    PIN_COLOR = "#ffff88"
    SELECTED_PIN_COLOR = "#ff3399"
    SELECTED_PIN_RING = "#ffffff"
    TRACE_DIMMED_TOP = "#1c2c50"
    TRACE_DIMMED_BOTTOM = "#3a1c14"
    TRACE_HIGHLIGHT = "#ffff66"
    TRACE_DIMMED_ZOOM_THRESHOLD = 2.0
    # Via markers: small open circles drawn on top of the trace layer.
    # Click → flip view layer (TOP↔BOTTOM). Cyan because the trace dim
    # palette is blue/red and yellow is reserved for net highlight, so
    # this stays unambiguously a via and not a trace or pad. Drawn only
    # at TRACE_DIMMED_ZOOM_THRESHOLD or higher — at low zoom the markers
    # would salt-and-pepper the board into noise.
    VIA_COLOR = "#00ccff"
    VIA_MARKER_R_PX = 3.5
    VIA_MARKER_THICKNESS_PX = 1.2
    # Click hit-test radius. A bit looser than the visual marker so the
    # user doesn't have to land exactly on the ring. Tighter than the
    # component-pick radius so vias only "win" the click race when
    # the cursor is genuinely on a marker.
    VIA_CLICK_RADIUS_PX = 8
    # Faint outline colour used when an inner copper layer is in view.
    # Components live on TOP/BOTTOM only, so on inner-layer view we
    # render every component as a ghost in this colour for orientation —
    # so the user can see "the trace I'm looking at runs under the CPU
    # socket" without losing the layer they care about.
    GHOST_OUTLINE = "#2a3052"
    MIN_ZOOM = 0.4
    MAX_ZOOM = 60.0
    WHEEL_FACTOR = 1.15
    DRAG_THRESHOLD_PX = 3
    CLICK_RADIUS_PX = 18
    PIN_CLICK_RADIUS_PX = 10

    # Reported by both canvas tiers so WalkerApp / status bar can show
    # which renderer is active without poking at private state.
    render_tier = "cpu"

    def __init__(self, parent: tk.Misc, board: BoardModel, **kw):
        super().__init__(parent, bg=self.BG, highlightthickness=0, **kw)
        self.board = board
        self._highlight: Set[str] = set()
        self._selected_refdes: Optional[str] = None
        self._selected_pin: Optional[str] = None
        self._on_select: Optional[Callable[[Optional[str]], None]] = None
        self._on_layer_change: Optional[Callable[[str], None]] = None
        self._on_pin_select: Optional[Callable[[Optional[str]], None]] = None
        self._view_layer: str = "TOP"
        self._mirror_x: bool = False
        self._rotation_quadrant: int = 0  # 0/1/2/3 = 0°/90°/180°/270° screen-CCW
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._drag_start: Optional[Tuple[int, int, float, float]] = None
        self._has_dragged = False
        self._show_traces: bool = False
        self._selected_net: Optional[str] = None
        self._on_traces_change: Optional[Callable[[bool], None]] = None
        # Skia-rasterised trace overlay state. Buffer & surface are sized
        # to the current canvas dimensions and recreated on resize. The
        # PhotoImage reference must be held on the instance — Tk drops
        # the rendered pixels the moment its only Python ref dies.
        self._skia_buf = None  # numpy.ndarray (H, W, 4) RGBA, lazy
        self._skia_surface = None
        self._skia_photo = None
        # Measurement-tool state. _measure_mode: when True, click captures
        # endpoints instead of selecting components. _measure_pts: world
        # (file-unit) coords, len 0/1/2. _measure_hover: live preview of
        # the second endpoint as the mouse moves with one point already
        # placed. _on_measure_change fires whenever the visible measurement
        # changes so WalkerApp can update the status bar.
        self._measure_mode: bool = False
        self._measure_pts: List[Tuple[float, float]] = []
        self._measure_hover: Optional[Tuple[float, float]] = None
        self._on_measure_change: Optional[Callable[[], None]] = None
        # Pending-redraw flag — coalesces bursty events (drag motion at
        # ~150 Hz, configure storms on resize) into a single actual paint
        # via after_idle. The GL canvas has the same machinery; the CPU
        # canvas was previously calling _redraw() synchronously on every
        # motion event, which cratered drag responsiveness on slow rigs.
        self._redraw_pending = False
        # Selected-net geometry cache. geometry_on_net does an O(N)
        # numpy mask over every trace segment to find the matching ones;
        # repeating that 60×/sec for the same net while the user is
        # panning/zooming is pure waste. Cache the (segs, polys) tuple
        # and recompute only when sel_net_id changes. Invalidated in
        # set_board (new topology) and on every successful net switch.
        self._geometry_net_cache: Tuple[
            Optional[int], Tuple[List[Any], List[Any]]
        ] = (None, ([], []))
        # Per-layer component count cache. Used in the status-bar text
        # ("N components on this layer") which the previous code
        # recomputed via sum(1 for ...) on every redraw — small but
        # meaningfully wasteful at drag-pan rates on big boards.
        self._comp_count_by_layer: Dict[str, int] = {}
        self._compute_bounds()
        self._area_cache: Dict[str, float] = {}
        self._sorted_components: List[Component] = []
        self._reorder_components()

        self.bind("<Configure>", lambda e: self._redraw())
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", self._on_wheel_x11)
        self.bind("<Button-5>", self._on_wheel_x11)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        # Bare cursor motion (no button held) — only consumed by the
        # measurement live-preview when one endpoint is already placed.
        # Cheap when measure mode is off (just a None check + return).
        self.bind("<Motion>", self._on_motion)

    # ---- Measurement tool (mirrors BoardCanvasGL below) -----------------

    @property
    def measure_mode(self) -> bool:
        return self._measure_mode

    def set_measure_mode(self, on: bool) -> None:
        """Enter or leave measurement mode. Leaving clears any in-progress
        measurement (one-point pending, two-point displayed)."""
        if self._measure_mode == on:
            return
        self._measure_mode = on
        self._measure_pts = []
        self._measure_hover = None
        self.config(cursor="crosshair" if on else "")
        self._redraw()
        if self._on_measure_change:
            self._on_measure_change()

    def clear_measurement(self) -> None:
        """Wipe the placed measurement points (e.g. Esc key). Mode stays on."""
        if not self._measure_pts and not self._measure_hover:
            return
        self._measure_pts = []
        self._measure_hover = None
        self._redraw()
        if self._on_measure_change:
            self._on_measure_change()

    def set_measure_change_callback(
        self, cb: Optional[Callable[[], None]],
    ) -> None:
        self._on_measure_change = cb

    def measurement_distance_units(self) -> Optional[float]:
        """Length of the current measurement in raw file units (None if
        fewer than two points are placed). Used by the status bar."""
        if len(self._measure_pts) < 2:
            return None
        (x1, y1), (x2, y2) = self._measure_pts[0], self._measure_pts[1]
        return ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5

    def measurement_distance_preview_units(self) -> Optional[float]:
        """Length from the placed first point to the live hover position
        (None if not in single-point-pending state)."""
        if len(self._measure_pts) != 1 or self._measure_hover is None:
            return None
        (x1, y1) = self._measure_pts[0]
        x2, y2 = self._measure_hover
        return ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5

    def units_per_mm(self) -> float:
        """Heuristic file-unit-to-mm scale.

        TVW stores coords in centi-mil (1/100,000 inch) -> 3937 u/mm.
        GENCAD and OpenBoardView ASCII store coords in mil (1/1,000
        inch) -> 39.37 u/mm. Detection: look at the component-bbox
        extent. For any real PCB the longest side is on the order of
        100-400 mm. If the in-file extent is > 50,000 units, we're in
        TVW's centi-mil coordinate system; otherwise we're in mils.
        Cached after first computation.
        """
        cached = getattr(self, "_units_per_mm_cache", None)
        if cached is not None:
            return cached
        xs = [c.x for c in self.board.components.values()]
        ys = [c.y for c in self.board.components.values()]
        if not xs:
            scale = 39.37
        else:
            span = max(max(xs) - min(xs), max(ys) - min(ys))
            scale = 3937.0 if span > 50_000 else 39.37
        self._units_per_mm_cache = scale
        return scale

    def _format_distance(self, d_units: float) -> str:
        """Pretty-print a distance in mm + mil. mm shown to 3 dp above 1 mm,
        as μm below that. Useful for both BGA pin pitches (~0.4 mm) and
        full board diagonals (~300 mm)."""
        upm = self.units_per_mm()
        mm = d_units / upm
        mil = mm * 39.3701
        if mm >= 1.0:
            return f"{mm:.3f} mm  ({mil:.1f} mil)"
        return f"{mm * 1000:.1f} um  ({mil:.2f} mil)"

    def _on_motion(self, event: tk.Event) -> None:
        if not self._measure_mode or len(self._measure_pts) != 1:
            return
        wx, wy = self._unproject(event.x, event.y)
        # Coalesce sub-pixel jitter — only redraw if hover moved more than
        # half a pixel in screen space (cheap visible-stability win).
        prev = self._measure_hover
        if prev is not None:
            w, h = self.winfo_width(), self.winfo_height()
            psx, psy = self._project(prev[0], prev[1], w, h)
            if abs(psx - event.x) < 0.5 and abs(psy - event.y) < 0.5:
                return
        self._measure_hover = (wx, wy)
        self._redraw()
        if self._on_measure_change:
            self._on_measure_change()

    @property
    def view_layer(self) -> str:
        return self._view_layer

    @property
    def selected_pin(self) -> Optional[str]:
        return self._selected_pin

    @property
    def selected_refdes(self) -> Optional[str]:
        return self._selected_refdes

    def _compute_bounds(self) -> None:
        xs = [c.x for c in self.board.components.values()]
        ys = [c.y for c in self.board.components.values()]
        if not xs or not ys:
            self.bounds = (0.0, 0.0, 1.0, 1.0)
            return
        self.bounds = (min(xs), min(ys), max(xs), max(ys))

    def _reorder_components(self) -> None:
        def area_of(c: Component) -> float:
            cached = self._area_cache.get(c.refdes)
            if cached is not None:
                return cached
            s = self.board.shapes.get(c.shape)
            if not s or not s.pins:
                a = 0.0
            else:
                x0, y0, x1, y1 = s.bbox()
                a = (x1 - x0) * (y1 - y0)
            self._area_cache[c.refdes] = a
            return a
        self._sorted_components = sorted(
            self.board.components.values(), key=lambda c: -area_of(c)
        )

    def set_select_callback(self, cb: Callable[[Optional[str]], None]) -> None:
        self._on_select = cb

    def set_layer_change_callback(self, cb: Callable[[str], None]) -> None:
        self._on_layer_change = cb

    def set_pin_select_callback(self, cb: Callable[[Optional[str]], None]) -> None:
        self._on_pin_select = cb

    def set_traces_change_callback(
        self, cb: Callable[[bool], None],
    ) -> None:
        self._on_traces_change = cb

    @property
    def show_traces(self) -> bool:
        return self._show_traces

    def set_selected_net(self, net_name: Optional[str]) -> None:
        if net_name == self._selected_net:
            return
        self._selected_net = net_name
        if self._show_traces:
            self._redraw()

    def toggle_traces(self) -> None:
        if not getattr(self.board, "topology_available", False):
            return
        self._show_traces = not self._show_traces
        # First-time activation: force the lazy topology build now so the
        # next redraw doesn't stall mid-paint. Tk has no progress dial here,
        # so swap the cursor to "wait" while we block.
        if self._show_traces:
            try:
                self.config(cursor="watch")
                self.update_idletasks()
                topo = self.board.topology
                # Eagerly build the SpatialHash — the native build path
                # defers it to "first net_at() call" to keep cold-load
                # snappy, but on the walker that first call lands inside
                # the user's first net click and stalls the click for
                # ~200 ms on a Z490. Pay the cost up front, while the
                # cursor is already on "watch", so the first click is
                # snappy.
                ensure_spatial = getattr(topo, "_ensure_spatial", None)
                if ensure_spatial is not None:
                    try:
                        ensure_spatial()
                    except Exception:
                        # Non-fatal — the lazy path will still work, we
                        # just lose the eager-init benefit.
                        pass
            finally:
                self.config(cursor="")
        self._redraw()
        if self._on_traces_change:
            self._on_traces_change(self._show_traces)

    def set_board(self, board: BoardModel) -> None:
        self.board = board
        self._highlight = set()
        self._selected_refdes = None
        self._selected_pin = None
        self._selected_net = None
        self._show_traces = False
        self._area_cache = {}
        self._sorted_components = []
        # New board → new topology object → drop the geometry-on-net
        # cache. Failing to do this would risk serving stale segments
        # if the new board reuses a net_id from the old.
        self._geometry_net_cache = (None, ([], []))
        # Per-layer count cache is also keyed off the old board's
        # components; clear it so the status bar reflects the new one.
        self._comp_count_by_layer = {}
        self._compute_bounds()
        self._reorder_components()
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._view_layer = "TOP"
        self._mirror_x = False
        self._rotation_quadrant = 0
        self._redraw()
        if self._on_layer_change:
            self._on_layer_change(self._view_layer)
        if self._on_traces_change:
            self._on_traces_change(self._show_traces)

    def set_view_layer(self, layer: str) -> None:
        if layer == self._view_layer:
            return
        if layer not in _available_layers_for(self.board):
            return
        self._reorient(lambda: setattr(self, "_view_layer", layer))
        if self._on_layer_change:
            self._on_layer_change(layer)

    def highlight(self, refdeses: List[str]) -> None:
        self._highlight = set(refdeses)
        if refdeses:
            first = self.board.components.get(refdeses[0])
            if first:
                if first.layer != self._view_layer:
                    self.set_view_layer(first.layer)
                if self.zoom > 1.5:
                    self._center_on(first.x, first.y)
        self._redraw()

    def select_refdes(self, refdes: Optional[str], center: bool = False) -> None:
        if refdes != self._selected_refdes:
            self._selected_pin = None
        if refdes:
            comp = self.board.components.get(refdes)
            if comp and comp.layer != self._view_layer:
                self.set_view_layer(comp.layer)
        self._selected_refdes = refdes
        if center and refdes:
            comp = self.board.components.get(refdes)
            if comp:
                self._center_on(comp.x, comp.y)
        self._redraw()

    def select_pin(self, pin_name: Optional[str], center: bool = False) -> None:
        if not self._selected_refdes:
            return
        self._selected_pin = pin_name
        if center and pin_name:
            comp = self.board.components.get(self._selected_refdes)
            if comp and comp.layer != self._view_layer:
                self.set_view_layer(comp.layer)
            shape = self.board.shapes.get(comp.shape) if comp else None
            if comp and shape:
                for name, dx, dy in shape.pins:
                    if name == pin_name:
                        theta = math.radians(comp.rotation)
                        ct, st = math.cos(theta), math.sin(theta)
                        wx = comp.x + dx * ct - dy * st
                        wy = comp.y + dx * st + dy * ct
                        if self.zoom < 8:
                            self.zoom = 12.0
                        self._center_on(wx, wy)
                        break
        self._redraw()
        if self._on_pin_select:
            self._on_pin_select(pin_name)

    def reset_view(self) -> None:
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._redraw()

    def _render_bounds(self) -> Tuple[float, float, float, float]:
        """World bbox after applying rotation. Mirror doesn't change bbox."""
        x0, y0, x1, y1 = self.bounds
        if self._rotation_quadrant % 2 == 0:
            return (x0, y0, x1, y1)
        cx_w = (x0 + x1) / 2
        cy_w = (y0 + y1) / 2
        bw = y1 - y0
        bh = x1 - x0
        return (cx_w - bw / 2, cy_w - bh / 2,
                cx_w + bw / 2, cy_w + bh / 2)

    def _apply_view_transform(self, x: float, y: float) -> Tuple[float, float]:
        """World → rotated/mirrored world coords. Layer flip and user mirror
        are XORed (a board flipped to BOTTOM and then user-mirrored is back to
        un-mirrored). Rotation is screen-CCW for positive quadrants."""
        x0, y0, x1, y1 = self.bounds
        cx_w = (x0 + x1) / 2
        cy_w = (y0 + y1) / 2
        if (self._view_layer == "BOTTOM") ^ self._mirror_x:
            x = x0 + x1 - x
        q = self._rotation_quadrant % 4
        if q == 0:
            return (x, y)
        if q == 1:  # 90° screen-CCW (= 90° world-CW because screen y is flipped)
            return (cx_w + (y - cy_w), cy_w - (x - cx_w))
        if q == 2:
            return (2 * cx_w - x, 2 * cy_w - y)
        return (cx_w - (y - cy_w), cy_w + (x - cx_w))  # 90° screen-CW

    def _invert_view_transform(self, rx: float, ry: float) -> Tuple[float, float]:
        x0, y0, x1, y1 = self.bounds
        cx_w = (x0 + x1) / 2
        cy_w = (y0 + y1) / 2
        q = self._rotation_quadrant % 4
        if q == 0:
            x, y = rx, ry
        elif q == 1:
            x = cx_w - (ry - cy_w)
            y = cy_w + (rx - cx_w)
        elif q == 2:
            x = 2 * cx_w - rx
            y = 2 * cy_w - ry
        else:
            x = cx_w + (ry - cy_w)
            y = cy_w - (rx - cx_w)
        if (self._view_layer == "BOTTOM") ^ self._mirror_x:
            x = x0 + x1 - x
        return (x, y)

    def _project(self, x: float, y: float, w: int, h: int) -> Tuple[float, float]:
        rx, ry = self._apply_view_transform(x, y)
        rx0, ry0, rx1, ry1 = self._render_bounds()
        bw = max(rx1 - rx0, 1.0)
        bh = max(ry1 - ry0, 1.0)
        pad = 12
        base_scale = min((w - 2 * pad) / bw, (h - 2 * pad) / bh)
        base_ox = pad + (w - 2 * pad - bw * base_scale) / 2
        base_oy = pad + (h - 2 * pad - bh * base_scale) / 2
        base_sx = base_ox + (rx - rx0) * base_scale
        base_sy = base_oy + (ry1 - ry) * base_scale
        cx, cy = w / 2, h / 2
        sx = cx + (base_sx - cx) * self.zoom + self.pan_x
        sy = cy + (base_sy - cy) * self.zoom + self.pan_y
        return sx, sy

    def _unproject(self, sx: float, sy: float) -> Tuple[float, float]:
        w, h = self.winfo_width(), self.winfo_height()
        cx, cy = w / 2, h / 2
        rx0, ry0, rx1, ry1 = self._render_bounds()
        bw = max(rx1 - rx0, 1.0)
        bh = max(ry1 - ry0, 1.0)
        pad = 12
        base_scale = min((w - 2 * pad) / bw, (h - 2 * pad) / bh)
        base_ox = pad + (w - 2 * pad - bw * base_scale) / 2
        base_oy = pad + (h - 2 * pad - bh * base_scale) / 2
        base_sx = (sx - cx - self.pan_x) / self.zoom + cx
        base_sy = (sy - cy - self.pan_y) / self.zoom + cy
        rx = (base_sx - base_ox) / base_scale + rx0
        ry = ry1 - (base_sy - base_oy) / base_scale
        return self._invert_view_transform(rx, ry)

    def _center_on(self, wx: float, wy: float) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        if w < 30 or h < 30:
            return
        rx, ry = self._apply_view_transform(wx, wy)
        rx0, ry0, rx1, ry1 = self._render_bounds()
        bw = max(rx1 - rx0, 1.0)
        bh = max(ry1 - ry0, 1.0)
        pad = 12
        base_scale = min((w - 2 * pad) / bw, (h - 2 * pad) / bh)
        base_ox = pad + (w - 2 * pad - bw * base_scale) / 2
        base_oy = pad + (h - 2 * pad - bh * base_scale) / 2
        base_sx = base_ox + (rx - rx0) * base_scale
        base_sy = base_oy + (ry1 - ry) * base_scale
        cx, cy = w / 2, h / 2
        self.pan_x = -(base_sx - cx) * self.zoom
        self.pan_y = -(base_sy - cy) * self.zoom

    def toggle_mirror_x(self) -> None:
        self._reorient(lambda: setattr(self, "_mirror_x", not self._mirror_x))

    def rotate(self, steps: int) -> None:
        """Rotate by `steps` × 90° screen-CCW (negative = CW)."""
        self._reorient(lambda: setattr(
            self, "_rotation_quadrant", (self._rotation_quadrant + steps) % 4
        ))

    def _reorient(self, mutate: Callable[[], None]) -> None:
        """Apply an orientation change while keeping the same world point at
        the canvas center. Common path for layer flip / mirror / rotate."""
        w, h = self.winfo_width(), self.winfo_height()
        wx_center = wy_center = None
        if w >= 30 and h >= 30:
            wx_center, wy_center = self._unproject(w / 2, h / 2)
        mutate()
        if wx_center is not None:
            self._center_on(wx_center, wy_center)
        self._redraw()

    def _component_polygon_world(self, c: Component) -> Optional[List[Tuple[float, float]]]:
        shape = self.board.shapes.get(c.shape)
        if not shape or not shape.pins:
            return None
        x0, y0, x1, y1 = shape.bbox()
        if (x1 - x0) < 0.5 and (y1 - y0) < 0.5:
            return None
        # The parser already added a 5% per-axis margin to bbox_override.
        # Adding another 10% here ON TOP (and using max(extent_x, extent_y)
        # for both axes) used to inflate the short axis of elongated chips
        # like DDR4 by ~5× — exactly the same bug class I just fixed in
        # the parser. Use a tiny floor padding (5 units) so a degenerate
        # rectangle still has area to draw.
        pad = 5
        x0 -= pad
        y0 -= pad
        x1 += pad
        y1 += pad
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        theta = math.radians(c.rotation)
        ct, st = math.cos(theta), math.sin(theta)
        return [(c.x + rx * ct - ry * st, c.y + rx * st + ry * ct) for rx, ry in corners]

    def _component_polygon_screen(
        self, c: Component, w: int, h: int
    ) -> Optional[List[Tuple[float, float]]]:
        world = self._component_polygon_world(c)
        if world is None:
            return None
        return [self._project(wx, wy, w, h) for wx, wy in world]

    @staticmethod
    def _bbox_of_points(points: List[Tuple[float, float]]) -> Tuple[float, float, float, float]:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return (min(xs), min(ys), max(xs), max(ys))

    def _redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 30 or h < 30:
            return
        dot_r = max(1.0, self.DOT_RADIUS * (self.zoom ** 0.4))

        # Traces render UNDER components so component outlines stay legible.
        if self._show_traces:
            self._draw_traces(w, h)

        is_inner = self._view_layer not in ("TOP", "BOTTOM")
        if is_inner:
            # Inner copper layer in view: components live on TOP/BOTTOM
            # only. Render every component as a faint outline ghost — no
            # fills, no labels, no pins, no highlight/selection.
            for c in self._sorted_components:
                self._draw_ghost(c, w, h)
        else:
            for c in self._sorted_components:
                if c.layer != self._view_layer:
                    continue
                if c.refdes in self._highlight or c.refdes == self._selected_refdes:
                    continue
                self._draw_one(c, w, h, dot_r, mode="normal")

            for refdes in self._highlight:
                if refdes == self._selected_refdes:
                    continue
                c = self.board.components.get(refdes)
                if c and c.layer == self._view_layer:
                    self._draw_one(c, w, h, dot_r, mode="highlight")

            if self._selected_refdes:
                c = self.board.components.get(self._selected_refdes)
                if c and c.layer == self._view_layer:
                    self._draw_one(c, w, h, dot_r, mode="selected")
                    self._draw_pins(c, w, h)

        zoom_pct = int(self.zoom * 100)
        if is_inner:
            n_layer = len(self.board.components)
            layer_indicator = (
                f"{self._view_layer} (inner copper, ghost components)"
            )
            comp_label = "ghost components"
        else:
            # Lazily fill the per-layer count cache. Cached forever
            # (within a single board) — components don't change layer
            # at runtime. Cleared in set_board.
            n_layer = self._comp_count_by_layer.get(self._view_layer)
            if n_layer is None:
                n_layer = sum(1 for c in self.board.components.values()
                              if c.layer == self._view_layer)
                self._comp_count_by_layer[self._view_layer] = n_layer
            layer_indicator = ("TOP (looking down)"
                               if self._view_layer == "TOP"
                               else "BOTTOM (mirrored, as if board flipped)")
            comp_label = "components on this layer"
        if not self._measure_mode:
            hint_extra = "  •  M=measure"
        else:
            d = self.measurement_distance_units()
            d_prev = self.measurement_distance_preview_units()
            if d is not None:
                readout = f"  •  measured: {self._format_distance(d)}"
            elif d_prev is not None:
                readout = (
                    f"  •  preview: {self._format_distance(d_prev)} "
                    "(click for 2nd pt)")
            else:
                readout = "  •  click first point"
            hint_extra = (
                "  •  measure mode" + readout
                + "  •  Esc to clear  •  M to exit"
            )
        self.create_text(
            8, 8,
            text=(f"{layer_indicator}  •  {n_layer} {comp_label}  •  "
                  f"zoom {zoom_pct}%  •  drag to pan, wheel to zoom, click an IC, "
                  "click a pin while selected, L=cycle layer, Home=reset"
                  + hint_extra),
            anchor="nw", fill="#aaaadd", font=("Segoe UI", 8),
        )

        # Measurement overlay sits on top of components and traces. Drawing
        # here (last in _redraw) keeps it on top after the canvas redraws.
        if self._measure_mode and (self._measure_pts or self._measure_hover):
            self._draw_measurement_overlay(w, h)

    def _draw_measurement_overlay(self, w: int, h: int) -> None:
        """Render the in-progress / completed measurement: endpoint dots,
        a connecting line, and a distance label at the line midpoint."""
        MEAS_COLOR = "#ffd24d"          # warm yellow, distinct from select cyan
        MEAS_OUTLINE = "#000000"
        DOT_R = 4

        def project(wxy: Tuple[float, float]) -> Tuple[float, float]:
            return self._project(wxy[0], wxy[1], w, h)

        # Compose the line from placed point(s) + hover preview.
        endpoints: List[Tuple[float, float]] = list(self._measure_pts)
        if len(endpoints) == 1 and self._measure_hover is not None:
            endpoints = endpoints + [self._measure_hover]

        # Draw endpoint dots first.
        for wxy in self._measure_pts:
            sx, sy = project(wxy)
            self.create_oval(sx - DOT_R, sy - DOT_R, sx + DOT_R, sy + DOT_R,
                             fill=MEAS_COLOR, outline=MEAS_OUTLINE, width=1)

        # Draw the connecting line + label only when we have two endpoints
        # (placed-placed, or placed-hover for the live preview).
        if len(endpoints) == 2:
            (x1, y1), (x2, y2) = endpoints
            sx1, sy1 = project((x1, y1))
            sx2, sy2 = project((x2, y2))
            # Black halo behind the colored line so it stays legible over
            # both light (component fills) and dark (board background) areas.
            self.create_line(sx1, sy1, sx2, sy2,
                             fill=MEAS_OUTLINE, width=4, capstyle="round")
            self.create_line(sx1, sy1, sx2, sy2,
                             fill=MEAS_COLOR, width=2, capstyle="round")
            # Hover-preview endpoint dot — drawn AFTER the line so it sits
            # on top, but smaller / hollow to differentiate from placed points.
            if len(self._measure_pts) == 1:
                self.create_oval(sx2 - DOT_R, sy2 - DOT_R,
                                 sx2 + DOT_R, sy2 + DOT_R,
                                 fill="", outline=MEAS_COLOR, width=2)
            # Distance label centred on the segment midpoint, offset slightly
            # perpendicular so it doesn't sit on top of the line.
            d_units = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            label = self._format_distance(d_units)
            mx, my = (sx1 + sx2) / 2, (sy1 + sy2) / 2
            # Perpendicular offset (14 px) — pick the side away from origin
            # so labels on different measurements don't collide.
            dx, dy = sx2 - sx1, sy2 - sy1
            seg_len = max((dx * dx + dy * dy) ** 0.5, 1.0)
            ox, oy = -dy / seg_len * 14, dx / seg_len * 14
            tx, ty = mx + ox, my + oy
            # Background pill for legibility.
            text_id = self.create_text(
                tx, ty, text=label, fill=MEAS_COLOR,
                font=("Segoe UI", 10, "bold"), anchor="center",
            )
            bbox = self.bbox(text_id)
            if bbox:
                bx0, by0, bx1, by1 = bbox
                pad = 3
                bg = self.create_rectangle(
                    bx0 - pad, by0 - pad, bx1 + pad, by1 + pad,
                    fill="#1a1a1a", outline=MEAS_COLOR, width=1,
                )
                # Restack so the text is on top of its background pill.
                self.tag_raise(text_id, bg)

    def _draw_ghost(self, c: Component, w: int, h: int) -> None:
        """Draw `c` as a faint outline only — used when an inner copper
        layer is in view. No fill, no label, no pin dots; just enough
        for orientation."""
        poly = self._component_polygon_screen(c, w, h)
        if poly is None:
            return
        x0p, y0p, x1p, y1p = self._bbox_of_points(poly)
        if x1p < -10 or x0p > w + 10 or y1p < -10 or y0p > h + 10:
            return
        if (x1p - x0p) < 2 and (y1p - y0p) < 2:
            return
        flat = [coord for pt in poly for coord in pt]
        self.create_polygon(
            *flat, fill="", outline=self.GHOST_OUTLINE, width=1.0,
        )

    def _draw_one(
        self, c: Component, w: int, h: int, dot_r: float, *, mode: str,
    ) -> None:
        layer_color = self.TOP_COLOR if c.layer == "TOP" else self.BOTTOM_COLOR
        if mode == "normal":
            fill, outline, outline_width = "", layer_color, 1.0
            label, label_color = False, ""
        elif mode == "highlight":
            fill, outline, outline_width = self.HIGHLIGHT, self.HIGHLIGHT_RING, 2.0
            label, label_color = True, "#ffffcc"
        elif mode == "selected":
            # No body fill — outline + label is the indicator. Lets the
            # trace overlay below stay visible through the chip body.
            # Step-highlighted chips keep their yellow fill.
            fill = self.HIGHLIGHT if c.refdes in self._highlight else ""
            outline, outline_width = self.SELECTED_OUTLINE, 3.0
            label, label_color = True, "#aaffff"
        else:
            return

        poly = self._component_polygon_screen(c, w, h)
        if poly:
            x0p, y0p, x1p, y1p = self._bbox_of_points(poly)
            if x1p < -10 or x0p > w + 10 or y1p < -10 or y0p > h + 10:
                return
            poly_w = x1p - x0p
            poly_h = y1p - y0p
            if poly_w >= 3 or poly_h >= 3:
                flat = [coord for pt in poly for coord in pt]
                # Auto-label big chips (>= 18 px on screen) even in normal
                # mode so sockets, BGAs, M.2/PCIe slots are findable at
                # any zoom.
                auto_label = (mode == "normal" and not label
                              and max(poly_w, poly_h) >= 18)
                if mode == "normal" and max(poly_w, poly_h) >= 18:
                    # Slightly thicker outline so big chips stand out from
                    # the dot soup.
                    outline_width = 2.0
                self.create_polygon(
                    *flat, fill=fill or "", outline=outline, width=outline_width,
                )
                if label or auto_label:
                    text_color = (label_color if label_color
                                  else "#9fb6ff" if c.layer == "TOP"
                                  else "#ffaa9f")
                    font_size = 9 if label else max(8, min(11,
                                                            int(min(poly_w, poly_h) / 12)))
                    self.create_text(
                        (x0p + x1p) / 2, (y0p + y1p) / 2,
                        text=c.refdes, anchor="center",
                        fill=text_color,
                        font=("Consolas", font_size, "bold"),
                    )
                return

        sx, sy = self._project(c.x, c.y, w, h)
        if sx < -10 or sx > w + 10 or sy < -10 or sy > h + 10:
            return
        dot_fill = fill if fill else outline
        self.create_oval(
            sx - dot_r, sy - dot_r, sx + dot_r, sy + dot_r,
            fill=dot_fill, outline="",
        )
        if label:
            self.create_text(
                sx + dot_r + 4, sy, text=c.refdes, anchor="w",
                fill=label_color, font=("Consolas", 9, "bold"),
            )

    def _draw_traces(self, w: int, h: int) -> None:
        """Dispatch to the Skia raster renderer when available, else the
        slower tk.create_line path. Both render the same picture: dimmed
        all-traces in viewport + bright highlight for the selected net.
        """
        topo = getattr(self.board, "topology", None)
        if topo is None:
            return
        if _SKIA_AVAILABLE:
            self._draw_traces_skia(topo, w, h)
        else:
            self._draw_traces_tk(topo, w, h)

    def _viewport_world(self, w: int, h: int) -> Tuple[float, float, float, float]:
        """Visible region in WORLD coords (after inverting all view xform).
        Used for AABB culling of segments/polylines."""
        u_tl = self._unproject(0, 0)
        u_tr = self._unproject(w, 0)
        u_bl = self._unproject(0, h)
        u_br = self._unproject(w, h)
        rx0 = min(u_tl[0], u_tr[0], u_bl[0], u_br[0])
        rx1 = max(u_tl[0], u_tr[0], u_bl[0], u_br[0])
        ry0 = min(u_tl[1], u_tr[1], u_bl[1], u_br[1])
        ry1 = max(u_tl[1], u_tr[1], u_bl[1], u_br[1])
        return rx0, ry0, rx1, ry1

    @staticmethod
    def _hex_to_rgba(hex_color: str, alpha: int = 255) -> Tuple[int, int, int, int]:
        c = hex_color.lstrip("#")
        return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha

    def _draw_traces_skia(self, topo, w: int, h: int) -> None:
        """Skia-raster path. Renders into an off-screen RGBA numpy buffer
        (zero-copy back to numpy because Skia is told the buffer's
        memory directly), wraps the buffer as a PIL Image, hands that to
        Tk via PhotoImage, and blits a single canvas item. Worst case
        ~50-80 ms for ~13 k visible segments, vs ~3-5 s for the tk path."""
        # Lazily create / resize the surface to match canvas size.
        if (self._skia_buf is None
                or self._skia_buf.shape[0] != h
                or self._skia_buf.shape[1] != w):
            self._skia_buf = _np.zeros((h, w, 4), dtype=_np.uint8)
            self._skia_surface = _skia.Surface(
                self._skia_buf, _skia.ColorType.kRGBA_8888_ColorType,
            )
        canvas = self._skia_surface.getCanvas()
        # Clear to OPAQUE canvas BG. Traces draw on top with full alpha,
        # so the result is a fully opaque image we can ship as P6 PPM
        # (RGB only — no alpha). Avoids a 70-ms numpy composite step.
        bg_r, bg_g, bg_b, _ = self._hex_to_rgba(self.BG)
        canvas.clear(_skia.Color(bg_r, bg_g, bg_b, 255))

        rx0, ry0, rx1, ry1 = self._viewport_world(w, h)
        layer = self._view_layer
        sel_net_id: Optional[int] = None
        if self._selected_net:
            try:
                sel_net_id = topo.net_id_by_name(self._selected_net)
            except Exception:
                sel_net_id = None

        # Synthetic ratsnest topologies (no real routed-trace data; e.g.
        # CAD/BRD/FZ/PCB) are styled at 70 % alpha to convey "illustrative,
        # not actual routing", and cross-layer MST edges (`seg.dashed`)
        # render with a dashed paint via Skia's PathEffect. Real TVW
        # topology has no `is_synthetic`/`dashed` so all that branches
        # off cleanly.
        is_synthetic = getattr(topo, "is_synthetic", False)
        synth_alpha_scale = 0.7 if is_synthetic else 1.0

        # Phase A — dimmed all-traces on the *current* layer.
        if self.zoom >= self.TRACE_DIMMED_ZOOM_THRESHOLD:
            r, g, b, a = self._hex_to_rgba(_layer_color(layer, dim=True))
            a = int(a * synth_alpha_scale)
            paint = _skia.Paint()
            paint.setColor(_skia.Color(r, g, b, a))
            paint.setStrokeWidth(1.0)
            paint.setAntiAlias(False)
            # Dashed paint, only built for synthetic topologies. Skia's
            # PathEffect requires drawPath (drawLine ignores the effect),
            # which costs one Path allocation per dashed segment — but
            # the dashed set is the cross-layer minority of a ratsnest
            # (~10 % of edges); the bulk solid path keeps drawLine speed.
            paint_dashed: Optional["_skia.Paint"] = None
            if is_synthetic:
                paint_dashed = _skia.Paint()
                paint_dashed.setColor(_skia.Color(r, g, b, a))
                paint_dashed.setStrokeWidth(1.0)
                paint_dashed.setAntiAlias(False)
                paint_dashed.setStyle(_skia.Paint.Style.kStroke_Style)
                paint_dashed.setPathEffect(
                    _skia.DashPathEffect.Make([4.0, 4.0], 0.0))
            for seg in topo.segments:
                if seg.layer != layer:
                    continue
                if sel_net_id is not None and seg.net_id == sel_net_id:
                    continue
                sx_min = seg.x1 if seg.x1 < seg.x2 else seg.x2
                sx_max = seg.x1 if seg.x1 > seg.x2 else seg.x2
                sy_min = seg.y1 if seg.y1 < seg.y2 else seg.y2
                sy_max = seg.y1 if seg.y1 > seg.y2 else seg.y2
                if sx_max < rx0 or sx_min > rx1: continue
                if sy_max < ry0 or sy_min > ry1: continue
                p0x, p0y = self._project(seg.x1, seg.y1, w, h)
                p1x, p1y = self._project(seg.x2, seg.y2, w, h)
                if paint_dashed is not None and getattr(seg, "dashed", False):
                    seg_path = _skia.Path()
                    seg_path.moveTo(p0x, p0y)
                    seg_path.lineTo(p1x, p1y)
                    canvas.drawPath(seg_path, paint_dashed)
                else:
                    canvas.drawLine(p0x, p0y, p1x, p1y, paint)

        # Phase B — selected-net highlight, spanning every layer the net
        # touches. Current-layer = bright TRACE_HIGHLIGHT (yellow);
        # off-current-layer segments = bright palette colour for their
        # layer. The graph already fuses cross-layer connectivity through
        # vias (UF unions in tvw_topology.py); we just stop filtering by
        # layer here. For synthetic ratsnest, dashed cross-layer edges
        # keep their dash style even when highlighted.
        if sel_net_id is not None:
            cached_id, cached_geom = self._geometry_net_cache
            if cached_id == sel_net_id:
                segs, polys = cached_geom
            else:
                try:
                    segs, polys = topo.geometry_on_net(sel_net_id)
                except Exception:
                    segs, polys = [], []
                self._geometry_net_cache = (sel_net_id, (segs, polys))

            paint_cache: Dict[Tuple[str, str, bool], "_skia.Paint"] = {}

            def _paint_for(seg_layer: str, role: str,
                           dashed: bool = False) -> "_skia.Paint":
                key = (seg_layer, role, dashed)
                p = paint_cache.get(key)
                if p is not None:
                    return p
                p = _skia.Paint()
                if seg_layer == layer:
                    color_hex = self.TRACE_HIGHLIGHT
                else:
                    color_hex = _layer_color(seg_layer, dim=False)
                rr, gg, bb, aa = self._hex_to_rgba(color_hex)
                p.setColor(_skia.Color(rr, gg, bb, aa))
                if role == "seg":
                    p.setStrokeWidth(2.0 if seg_layer == layer else 1.5)
                    p.setAntiAlias(True)
                    if dashed:
                        p.setStyle(_skia.Paint.Style.kStroke_Style)
                        p.setPathEffect(
                            _skia.DashPathEffect.Make([4.0, 4.0], 0.0))
                else:
                    p.setStrokeWidth(1.0)
                    p.setAntiAlias(True)
                    p.setStyle(_skia.Paint.Style.kStroke_Style)
                paint_cache[key] = p
                return p

            for seg in segs:
                p0x, p0y = self._project(seg.x1, seg.y1, w, h)
                p1x, p1y = self._project(seg.x2, seg.y2, w, h)
                if getattr(seg, "dashed", False):
                    seg_path = _skia.Path()
                    seg_path.moveTo(p0x, p0y)
                    seg_path.lineTo(p1x, p1y)
                    canvas.drawPath(
                        seg_path, _paint_for(seg.layer, "seg", dashed=True))
                else:
                    canvas.drawLine(
                        p0x, p0y, p1x, p1y, _paint_for(seg.layer, "seg"))
            for poly in polys:
                if len(poly.vertices) < 2:
                    continue
                path = _skia.Path()
                vx, vy = poly.vertices[0]
                px, py = self._project(vx, vy, w, h)
                path.moveTo(px, py)
                for vx, vy in poly.vertices[1:]:
                    px, py = self._project(vx, vy, w, h)
                    path.lineTo(px, py)
                canvas.drawPath(path, _paint_for(poly.layer, "poly"))

        # Phase C — via markers. Open cyan rings, viewport-culled,
        # zoom-gated. Vias bridge TOP↔BOTTOM by definition so we draw
        # them regardless of which layer is current — clicking one
        # flips the view to the other side. When the via belongs to
        # the selected net, fill with TRACE_HIGHLIGHT yellow so it
        # pops along the net trace. Synthetic ratsnest topologies
        # have no vias (empty list) so this loop is a no-op.
        if self.zoom >= self.TRACE_DIMMED_ZOOM_THRESHOLD:
            vias = getattr(topo, "vias", None) or []
            if vias:
                vr, vg, vb, _ = self._hex_to_rgba(self.VIA_COLOR)
                via_paint = _skia.Paint()
                via_paint.setColor(_skia.Color(vr, vg, vb, 255))
                via_paint.setStyle(_skia.Paint.Style.kStroke_Style)
                via_paint.setStrokeWidth(self.VIA_MARKER_THICKNESS_PX)
                via_paint.setAntiAlias(True)
                hr, hg, hb, _ = self._hex_to_rgba(self.TRACE_HIGHLIGHT)
                via_paint_hl: Optional["_skia.Paint"] = None
                if sel_net_id is not None:
                    via_paint_hl = _skia.Paint()
                    via_paint_hl.setColor(_skia.Color(hr, hg, hb, 255))
                    via_paint_hl.setStyle(_skia.Paint.Style.kFill_Style)
                    via_paint_hl.setAntiAlias(True)
                rpx = self.VIA_MARKER_R_PX
                for v in vias:
                    if v.x < rx0 or v.x > rx1: continue
                    if v.y < ry0 or v.y > ry1: continue
                    sx, sy = self._project(v.x, v.y, w, h)
                    if (via_paint_hl is not None
                            and v.net_id == sel_net_id):
                        canvas.drawCircle(sx, sy, rpx - 0.5, via_paint_hl)
                    canvas.drawCircle(sx, sy, rpx, via_paint)

        self._skia_surface.flushAndSubmit()
        # Buffer is fully opaque (we cleared with opaque BG and drew opaque
        # traces). Strip alpha, ship as P6 PPM. tk.PhotoImage(data=...)
        # uses Tcl's C-side image loader (~10 ms vs ~2 s via ImageTk).
        rgb = _np.ascontiguousarray(self._skia_buf[:, :, :3])
        ppm = self._ppm_header_for(w, h) + rgb.tobytes()
        self._skia_photo = tk.PhotoImage(data=ppm, format="PPM")
        self.create_image(0, 0, image=self._skia_photo, anchor="nw")

    @staticmethod
    def _ppm_header_for(w: int, h: int) -> bytes:
        return f"P6 {w} {h} 255 ".encode("ascii")

    def _draw_traces_tk(self, topo, w: int, h: int) -> None:
        """Fallback path used when Skia / numpy / Pillow are unavailable.
        Identical output to the Skia path but goes through tk.create_line
        per segment — slow at high zoom levels.

        Synthetic ratsnest: dashed cross-layer edges use tk's `dash=(4, 4)`
        kwarg (free — handled inside Tcl). The 70 % alpha cue from the
        Skia path can't translate directly (tk colors are RGB-only), so
        we leave the color as-is here; users on this fallback tier
        already accept reduced fidelity.
        """
        rx0, ry0, rx1, ry1 = self._viewport_world(w, h)
        layer = self._view_layer
        sel_net_id: Optional[int] = None
        if self._selected_net:
            try:
                sel_net_id = topo.net_id_by_name(self._selected_net)
            except Exception:
                sel_net_id = None
        if self.zoom >= self.TRACE_DIMMED_ZOOM_THRESHOLD:
            dimmed_color = _layer_color(layer, dim=True)
            for seg in topo.segments:
                if seg.layer != layer:
                    continue
                if sel_net_id is not None and seg.net_id == sel_net_id:
                    continue
                sx_min = seg.x1 if seg.x1 < seg.x2 else seg.x2
                sx_max = seg.x1 if seg.x1 > seg.x2 else seg.x2
                sy_min = seg.y1 if seg.y1 < seg.y2 else seg.y2
                sy_max = seg.y1 if seg.y1 > seg.y2 else seg.y2
                if sx_max < rx0 or sx_min > rx1: continue
                if sy_max < ry0 or sy_min > ry1: continue
                p0x, p0y = self._project(seg.x1, seg.y1, w, h)
                p1x, p1y = self._project(seg.x2, seg.y2, w, h)
                if getattr(seg, "dashed", False):
                    self.create_line(p0x, p0y, p1x, p1y,
                                     fill=dimmed_color, width=1,
                                     dash=(4, 4))
                else:
                    self.create_line(p0x, p0y, p1x, p1y,
                                     fill=dimmed_color, width=1)
        if sel_net_id is not None:
            cached_id, cached_geom = self._geometry_net_cache
            if cached_id == sel_net_id:
                segs, polys = cached_geom
            else:
                try:
                    segs, polys = topo.geometry_on_net(sel_net_id)
                except Exception:
                    segs, polys = [], []
                self._geometry_net_cache = (sel_net_id, (segs, polys))

            def _hl_for(seg_layer: str) -> str:
                return (self.TRACE_HIGHLIGHT if seg_layer == layer
                        else _layer_color(seg_layer, dim=False))

            for seg in segs:
                p0x, p0y = self._project(seg.x1, seg.y1, w, h)
                p1x, p1y = self._project(seg.x2, seg.y2, w, h)
                if getattr(seg, "dashed", False):
                    self.create_line(
                        p0x, p0y, p1x, p1y,
                        fill=_hl_for(seg.layer),
                        width=2 if seg.layer == layer else 1,
                        dash=(4, 4),
                    )
                else:
                    self.create_line(
                        p0x, p0y, p1x, p1y,
                        fill=_hl_for(seg.layer),
                        width=2 if seg.layer == layer else 1,
                    )
            for poly in polys:
                pts: List[float] = []
                for vx, vy in poly.vertices:
                    px, py = self._project(vx, vy, w, h)
                    pts.append(px); pts.append(py)
                if len(pts) >= 4:
                    self.create_line(
                        *pts, fill=_hl_for(poly.layer), width=1,
                    )

        # Via markers. See `_draw_traces_skia` Phase C for the design
        # rationale; this is the simpler tk fallback. Open cyan ring
        # via create_oval. When on selected net, also draw a smaller
        # filled yellow disc inside.
        if self.zoom >= self.TRACE_DIMMED_ZOOM_THRESHOLD:
            vias = getattr(topo, "vias", None) or []
            if vias:
                rpx = self.VIA_MARKER_R_PX
                inner_r = max(1.0, rpx - 1.5)
                for v in vias:
                    if v.x < rx0 or v.x > rx1: continue
                    if v.y < ry0 or v.y > ry1: continue
                    sx, sy = self._project(v.x, v.y, w, h)
                    if sel_net_id is not None and v.net_id == sel_net_id:
                        self.create_oval(
                            sx - inner_r, sy - inner_r,
                            sx + inner_r, sy + inner_r,
                            fill=self.TRACE_HIGHLIGHT, outline="",
                        )
                    self.create_oval(
                        sx - rpx, sy - rpx, sx + rpx, sy + rpx,
                        outline=self.VIA_COLOR, width=1,
                    )

    def _draw_pins(self, c: Component, w: int, h: int) -> None:
        shape = self.board.shapes.get(c.shape)
        if not shape:
            return
        theta = math.radians(c.rotation)
        ct, st = math.cos(theta), math.sin(theta)
        pin_r = max(0.8, 1.2 * (self.zoom ** 0.35))
        sel_pin_r = max(3.5, pin_r * 2.6)
        for pin_name, dx, dy in shape.pins:
            wx = c.x + dx * ct - dy * st
            wy = c.y + dx * st + dy * ct
            sx, sy = self._project(wx, wy, w, h)
            if sx < -2 or sx > w + 2 or sy < -2 or sy > h + 2:
                continue
            if pin_name == self._selected_pin:
                self.create_oval(
                    sx - sel_pin_r - 2, sy - sel_pin_r - 2,
                    sx + sel_pin_r + 2, sy + sel_pin_r + 2,
                    outline=self.SELECTED_PIN_RING, width=2,
                )
                self.create_oval(
                    sx - sel_pin_r, sy - sel_pin_r,
                    sx + sel_pin_r, sy + sel_pin_r,
                    fill=self.SELECTED_PIN_COLOR, outline="",
                )
                self.create_text(
                    sx + sel_pin_r + 4, sy, text=pin_name, anchor="w",
                    fill="#ffaadd", font=("Consolas", 10, "bold"),
                )
            else:
                self.create_oval(
                    sx - pin_r, sy - pin_r, sx + pin_r, sy + pin_r,
                    fill=self.PIN_COLOR, outline="",
                )

    def _on_wheel(self, event: tk.Event) -> None:
        f = self.WHEEL_FACTOR if event.delta > 0 else 1 / self.WHEEL_FACTOR
        self._apply_zoom(event.x, event.y, f)

    def _on_wheel_x11(self, event: tk.Event) -> None:
        f = self.WHEEL_FACTOR if event.num == 4 else 1 / self.WHEEL_FACTOR
        self._apply_zoom(event.x, event.y, f)

    def _apply_zoom(self, cx: int, cy: int, factor_in: float) -> None:
        new_zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, self.zoom * factor_in))
        factor = new_zoom / self.zoom
        if factor == 1.0:
            return
        canvas_cx = self.winfo_width() / 2
        canvas_cy = self.winfo_height() / 2
        self.pan_x = (cx - canvas_cx) * (1 - factor) + self.pan_x * factor
        self.pan_y = (cy - canvas_cy) * (1 - factor) + self.pan_y * factor
        self.zoom = new_zoom
        self._redraw()

    def _on_press(self, event: tk.Event) -> None:
        self._drag_start = (event.x, event.y, self.pan_x, self.pan_y)
        self._has_dragged = False
        self.config(cursor="fleur")

    def _on_drag(self, event: tk.Event) -> None:
        if not self._drag_start:
            return
        x0, y0, p0x, p0y = self._drag_start
        dx, dy = event.x - x0, event.y - y0
        if abs(dx) > self.DRAG_THRESHOLD_PX or abs(dy) > self.DRAG_THRESHOLD_PX:
            self._has_dragged = True
        self.pan_x = p0x + dx
        self.pan_y = p0y + dy
        # Coalesced redraw — bursts of motion events collapse into a
        # single repaint per Tk idle slice. Synchronous _redraw() here
        # was the biggest single source of pan lag on weaker rigs.
        self._schedule_redraw()

    def _schedule_redraw(self) -> None:
        """Coalesce multiple state-change calls in the same Tk event into
        a single repaint. Mirrors BoardCanvasGL._schedule_redraw."""
        if self._redraw_pending:
            return
        self._redraw_pending = True
        self.after_idle(self._do_coalesced_redraw)

    def _do_coalesced_redraw(self) -> None:
        self._redraw_pending = False
        self._redraw()

    def _on_release(self, event: tk.Event) -> None:
        was_drag = self._has_dragged
        self._drag_start = None
        self._has_dragged = False
        self.config(cursor="")
        if not was_drag:
            self._handle_click(event.x, event.y)

    def _handle_click(self, cx: int, cy: int) -> None:
        # Measurement mode short-circuits component / pin selection. Two
        # points get captured; a third click resets and starts a new pair
        # (so users can measure repeatedly without leaving and re-entering
        # the mode).
        if self._measure_mode:
            wx, wy = self._unproject(cx, cy)
            if len(self._measure_pts) >= 2:
                self._measure_pts = [(wx, wy)]
                self._measure_hover = None
            else:
                self._measure_pts.append((wx, wy))
                if len(self._measure_pts) == 2:
                    self._measure_hover = None
            self._redraw()
            if self._on_measure_change:
                self._on_measure_change()
            return

        # Via hit-test: only when traces are visible AND the click radius
        # finds a via. Wins over component selection because vias are
        # smaller targets than components and clicking near one is
        # almost always intentional (the user wants to "punch through"
        # to the other layer). Flip layer + bail.
        via = self._find_via_at(cx, cy)
        if via is not None:
            self._flip_layer_for_via(via)
            return

        if self._selected_refdes:
            comp = self.board.components.get(self._selected_refdes)
            if comp and comp.layer == self._view_layer:
                shape = self.board.shapes.get(comp.shape)
                if shape:
                    pin = self._find_pin_at(comp, shape, cx, cy)
                    if pin:
                        if pin != self._selected_pin:
                            self._selected_pin = pin
                            self._redraw()
                            if self._on_pin_select:
                                self._on_pin_select(pin)
                        return

        refdes = self._find_component_at(cx, cy)
        if refdes != self._selected_refdes:
            self._selected_refdes = refdes
            self._selected_pin = None
            self._redraw()
            if self._on_select:
                self._on_select(refdes)
        elif refdes is None and self._selected_pin:
            self._selected_pin = None
            self._redraw()
            if self._on_pin_select:
                self._on_pin_select(None)

    def _find_via_at(self, cx: int, cy: int) -> Optional[Any]:
        """Hit-test the click against rendered vias. Returns the closest
        Via within VIA_CLICK_RADIUS_PX, or None.

        Gated on `show_traces`: vias aren't drawn when traces are off,
        so they shouldn't be clickable either. Synthetic ratsnest
        topologies have no vias (empty list); the loop short-circuits.

        We don't viewport-cull here — the per-via screen-distance check
        is cheap enough (a few thousand subtractions and a single sqrt
        per visible via) that it stays well under 1 ms even on a
        Z490 with 9 k vias. Skipping cull keeps the code small."""
        if not self._show_traces:
            return None
        topo = getattr(self.board, "topology", None)
        if topo is None:
            return None
        vias = getattr(topo, "vias", None) or []
        if not vias:
            return None
        w, h = self.winfo_width(), self.winfo_height()
        r = self.VIA_CLICK_RADIUS_PX
        r2 = r * r
        best = None
        best_d2 = r2 + 1
        for v in vias:
            sx, sy = self._project(v.x, v.y, w, h)
            ddx = sx - cx
            ddy = sy - cy
            if abs(ddx) > r or abs(ddy) > r:
                continue
            d2 = ddx * ddx + ddy * ddy
            if d2 < best_d2:
                best_d2 = d2
                best = v
        return best

    def _flip_layer_for_via(self, via: Any) -> None:
        """Click on a via → flip the view to the OTHER side of this via.

        For a 2-layer board (the common case) this is just TOP↔BOTTOM.
        For multi-layer boards (GPU PCBs with INNER_n layers) the via
        is still strictly TOP↔BOTTOM — we don't model inner-layer
        microvias yet — so the flip rule is the same: if currently
        TOP, go to BOTTOM; otherwise go to TOP. An inner-layer view
        flips to TOP (so the user lands on a side the via actually
        traverses)."""
        cur = self._view_layer
        target = "BOTTOM" if cur == "TOP" else "TOP"
        if target != cur:
            self.set_view_layer(target)

    def _find_pin_at(
        self, comp: Component, shape: Any, cx: int, cy: int
    ) -> Optional[str]:
        w, h = self.winfo_width(), self.winfo_height()
        theta = math.radians(comp.rotation)
        ct, st = math.cos(theta), math.sin(theta)
        best_pin: Optional[str] = None
        best_dist = self.PIN_CLICK_RADIUS_PX
        for pin_name, dx, dy in shape.pins:
            wx = comp.x + dx * ct - dy * st
            wy = comp.y + dx * st + dy * ct
            sx, sy = self._project(wx, wy, w, h)
            if abs(sx - cx) > self.PIN_CLICK_RADIUS_PX or \
                    abs(sy - cy) > self.PIN_CLICK_RADIUS_PX:
                continue
            d = ((sx - cx) ** 2 + (sy - cy) ** 2) ** 0.5
            if d < best_dist:
                best_dist = d
                best_pin = pin_name
        return best_pin

    def _find_component_at(self, cx: int, cy: int) -> Optional[str]:
        # When several components contain the click, the original rule
        # "smallest screen area wins" works fine for normal nesting (a
        # chip drawn over a connector outline) but breaks when the .cad
        # file annotates a chip's keep-out zone with a duplicate
        # rectangle that has only a handful of mounting pins. On the
        # ROG Maximus Z690 .cad, `LGA_1200_HOLE` (4 corner pins, slightly
        # smaller bbox) was stealing every click from `LGA1700` (1708
        # real socket pins) — the user never saw the actual socket pins.
        #
        # Fix: weight the area by a pin-density factor. Sparsely-pinned
        # components get a penalty so they lose ties to densely-pinned
        # ones with similar bbox. Tuned so an 8+ pin component beats a
        # 4-pin HOLE annotation of similar size; nested chips inside
        # outlines still win because their area is much smaller.
        w, h = self.winfo_width(), self.winfo_height()
        candidates = [c for c in self.board.components.values()
                      if c.layer == self._view_layer]
        best_refdes = None
        best_score = float("inf")
        for c in candidates:
            poly = self._component_polygon_screen(c, w, h)
            if poly and self._point_in_poly(cx, cy, poly):
                area = self._poly_area(poly)
                shape = self.board.shapes.get(c.shape)
                n_pins = len(shape.pins) if shape else 0
                # Sparsity factor: 8x penalty for pin-less or 1-pin
                # components (board outlines, mechanical anchors), down
                # to 1x at 8+ pins. Real chips with ≥8 pins are
                # unaffected so dense-pin nesting still resolves to
                # the innermost chip.
                if n_pins >= 8:
                    factor = 1.0
                else:
                    factor = 8.0 / max(1, n_pins)
                score = area * factor
                if score < best_score:
                    best_score = score
                    best_refdes = c.refdes
        if best_refdes:
            return best_refdes
        best_dist = self.CLICK_RADIUS_PX
        for c in candidates:
            sx, sy = self._project(c.x, c.y, w, h)
            d = ((sx - cx) ** 2 + (sy - cy) ** 2) ** 0.5
            if d < best_dist:
                best_dist = d
                best_refdes = c.refdes
        return best_refdes

    @staticmethod
    def _point_in_poly(px: float, py: float, poly: List[Tuple[float, float]]) -> bool:
        n = len(poly)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = poly[i]
            xj, yj = poly[j]
            if ((yi > py) != (yj > py)) and \
                    (px < (xj - xi) * (py - yi) / (yj - yi + 1e-9) + xi):
                inside = not inside
            j = i
        return inside

    @staticmethod
    def _poly_area(poly: List[Tuple[float, float]]) -> float:
        n = len(poly)
        total = 0.0
        for i in range(n):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % n]
            total += x1 * y2 - x2 * y1
        return abs(total) / 2


# Module-level alias retained so external callers / pickled tools that
# imported `BoardCanvas` symbolically continue to resolve. The factory
# `make_board_canvas` is the recommended entry point.
BoardCanvas = BoardCanvasCPU


# ----- GPU-accelerated board canvas (Tier 1) ------------------------------
#
# BoardCanvasGL replaces tk.Canvas with a pyopengltk OpenGLFrame and
# routes every draw call through a Skia GrDirectContext-backed surface.
# Trace overlays at 1920×1080 zoom 2.96 on Z490 (~13 k visible segments)
# render in ~3-7 ms with this path versus ~200 ms for the CPU+PPM path
# and ~10 s for the per-line tk.Canvas path.
#
# The class shares its public API with BoardCanvasCPU verbatim — same
# methods, same callback signatures, same properties. WalkerApp only
# touches the abstract API, so swapping backends is invisible to it.
#
# Only available when pyopengltk + PyOpenGL + Skia all import. The
# factory probes at startup and falls back to BoardCanvasCPU on any
# failure.

if _GL_AVAILABLE:
    # See the cross-reference note above BoardCanvasCPU — this class is
    # also kept in sync with boardviewer/viewer.py's BoardCanvasGL.
    class BoardCanvasGL(_OpenGLFrame):  # type: ignore[misc]
        """GPU-backed board renderer. Public API matches BoardCanvasCPU.

        Rendering pipeline (per redraw):
          1. Skia GL surface bound to the GrDirectContext.
          2. Clear to BG colour (opaque — we don't need alpha blending).
          3. Trace layer (dimmed all-segs as a single drawPath, then
             highlighted net's geometry via drawLine + drawPath).
          4. Component polygons (drawn via drawPath per component).
          5. Refdes labels (Skia drawTextBlob).
          6. Pin dots + selected-pin ring (Skia drawCircle).
          7. surface.flushAndSubmit() — pushes GPU work out.
          8. tkSwapBuffers() (called automatically by OpenGLFrame).

        Hit-testing reuses the same projection math the CPU class uses
        (copied not inherited so we keep tight ownership of state on the
        OpenGLFrame instance)."""

        # Visual constants — kept identical to BoardCanvasCPU so the
        # user-visible picture is the same on both tiers.
        DOT_RADIUS = BoardCanvasCPU.DOT_RADIUS
        BG = BoardCanvasCPU.BG
        TOP_COLOR = BoardCanvasCPU.TOP_COLOR
        BOTTOM_COLOR = BoardCanvasCPU.BOTTOM_COLOR
        HIGHLIGHT = BoardCanvasCPU.HIGHLIGHT
        HIGHLIGHT_RING = BoardCanvasCPU.HIGHLIGHT_RING
        SELECTED_OUTLINE = BoardCanvasCPU.SELECTED_OUTLINE
        PIN_COLOR = BoardCanvasCPU.PIN_COLOR
        SELECTED_PIN_COLOR = BoardCanvasCPU.SELECTED_PIN_COLOR
        SELECTED_PIN_RING = BoardCanvasCPU.SELECTED_PIN_RING
        TRACE_DIMMED_TOP = BoardCanvasCPU.TRACE_DIMMED_TOP
        TRACE_DIMMED_BOTTOM = BoardCanvasCPU.TRACE_DIMMED_BOTTOM
        TRACE_HIGHLIGHT = BoardCanvasCPU.TRACE_HIGHLIGHT
        TRACE_DIMMED_ZOOM_THRESHOLD = BoardCanvasCPU.TRACE_DIMMED_ZOOM_THRESHOLD
        VIA_COLOR = BoardCanvasCPU.VIA_COLOR
        VIA_MARKER_R_PX = BoardCanvasCPU.VIA_MARKER_R_PX
        VIA_MARKER_THICKNESS_PX = BoardCanvasCPU.VIA_MARKER_THICKNESS_PX
        VIA_CLICK_RADIUS_PX = BoardCanvasCPU.VIA_CLICK_RADIUS_PX
        GHOST_OUTLINE = BoardCanvasCPU.GHOST_OUTLINE
        MIN_ZOOM = BoardCanvasCPU.MIN_ZOOM
        MAX_ZOOM = BoardCanvasCPU.MAX_ZOOM
        WHEEL_FACTOR = BoardCanvasCPU.WHEEL_FACTOR
        DRAG_THRESHOLD_PX = BoardCanvasCPU.DRAG_THRESHOLD_PX
        CLICK_RADIUS_PX = BoardCanvasCPU.CLICK_RADIUS_PX
        PIN_CLICK_RADIUS_PX = BoardCanvasCPU.PIN_CLICK_RADIUS_PX

        render_tier = "gl"

        def __init__(self, parent: tk.Misc, board: BoardModel, **kw):
            # OpenGLFrame doesn't accept bg/highlightthickness in the
            # same way; pass through the rest. Default to a sensible
            # initial size — the parent will resize it.
            kw.setdefault("width", 800)
            kw.setdefault("height", 600)
            super().__init__(parent, **kw)
            # Setting animate=0 means we don't run a redraw loop;
            # we redraw on demand from event bindings.
            self.animate = 0

            self.board = board
            self._highlight: Set[str] = set()
            self._selected_refdes: Optional[str] = None
            self._selected_pin: Optional[str] = None
            self._on_select: Optional[Callable[[Optional[str]], None]] = None
            self._on_layer_change: Optional[Callable[[str], None]] = None
            self._on_pin_select: Optional[Callable[[Optional[str]], None]] = None
            self._view_layer: str = "TOP"
            self._mirror_x: bool = False
            self._rotation_quadrant: int = 0
            self.zoom = 1.0
            self.pan_x = 0.0
            self.pan_y = 0.0
            self._drag_start: Optional[Tuple[int, int, float, float]] = None
            self._has_dragged = False
            self._show_traces: bool = False
            self._selected_net: Optional[str] = None
            self._on_traces_change: Optional[Callable[[bool], None]] = None
            # Measurement-tool state (mirrors BoardCanvasCPU). See that
            # class's __init__ for the field semantics — same shape, same
            # public API, just rendered via Skia instead of tk canvas items.
            self._measure_mode: bool = False
            self._measure_pts: List[Tuple[float, float]] = []
            self._measure_hover: Optional[Tuple[float, float]] = None
            self._on_measure_change: Optional[Callable[[], None]] = None

            # GL/Skia state — populated on first initgl() once the
            # OpenGL context is current.
            self._gl_ready = False
            self._grctx = None
            self._skia_surface = None
            self._skia_backend_target = None
            self._surface_w = 0
            self._surface_h = 0
            self._comp_arrays = None  # numpy cache built lazily
            self._typeface = None
            self._font_label = None
            self._font_pin = None
            self._font_status = None
            # Cached BG colour as a Skia Color so we don't recompute per
            # frame.
            self._bg_color = self._hex_to_skia(self.BG)
            # Pending redraw flag — coalesces multiple bursty events
            # (multiple <Configure> + <Expose> at startup) into a single
            # actual GL draw call via after_idle.
            self._redraw_scheduled = False
            # Selected-net geometry cache (mirrors BoardCanvasCPU). See
            # that class for cache invariants. Even on a GPU-backed
            # render, geometry_on_net itself runs on the CPU — caching
            # the (segs, polys) tuple skips a numpy mask + list-of-segs
            # rebuild every frame.
            self._geometry_net_cache: Tuple[
                Optional[int], Tuple[List[Any], List[Any]]
            ] = (None, ([], []))
            # Per-layer component count cache (mirrors BoardCanvasCPU).
            # The status bar reads this once per frame; previous code
            # ran a sum() over every component each redraw.
            self._comp_count_by_layer: Dict[str, int] = {}

            self._compute_bounds()
            self._area_cache: Dict[str, float] = {}
            self._sorted_components: List[Component] = []
            self._reorder_components()

            # Same bindings as the CPU path. <Configure> is already
            # bound by the OpenGLFrame base for tkResize, but Tk
            # delivers all bound handlers, so adding ours is fine.
            self.bind("<Configure>", lambda e: self._on_configure())
            self.bind("<MouseWheel>", self._on_wheel)
            self.bind("<Button-4>", self._on_wheel_x11)
            self.bind("<Button-5>", self._on_wheel_x11)
            self.bind("<ButtonPress-1>", self._on_press)
            self.bind("<B1-Motion>", self._on_drag)
            self.bind("<ButtonRelease-1>", self._on_release)
            self.bind("<Motion>", self._on_motion)

        # ---- public API ---------------------------------------------------

        @property
        def view_layer(self) -> str:
            return self._view_layer

        @property
        def selected_pin(self) -> Optional[str]:
            return self._selected_pin

        @property
        def selected_refdes(self) -> Optional[str]:
            return self._selected_refdes

        @property
        def show_traces(self) -> bool:
            return self._show_traces

        def set_select_callback(
            self, cb: Callable[[Optional[str]], None],
        ) -> None:
            self._on_select = cb

        def set_layer_change_callback(
            self, cb: Callable[[str], None],
        ) -> None:
            self._on_layer_change = cb

        def set_pin_select_callback(
            self, cb: Callable[[Optional[str]], None],
        ) -> None:
            self._on_pin_select = cb

        def set_traces_change_callback(
            self, cb: Callable[[bool], None],
        ) -> None:
            self._on_traces_change = cb

        def set_selected_net(self, net_name: Optional[str]) -> None:
            if net_name == self._selected_net:
                return
            self._selected_net = net_name
            if self._show_traces:
                self._schedule_redraw()

        def toggle_traces(self) -> None:
            if not getattr(self.board, "topology_available", False):
                return
            self._show_traces = not self._show_traces
            if self._show_traces:
                # Force-build the topology now (3-6s) before any redraw
                # tries to read it — same UX the CPU path provides.
                try:
                    self.config(cursor="watch")
                    self.update_idletasks()
                    topo = self.board.topology
                    # Eager SpatialHash build — see the matching block
                    # in BoardCanvasCPU.toggle_traces for the rationale.
                    ensure_spatial = getattr(topo, "_ensure_spatial", None)
                    if ensure_spatial is not None:
                        try:
                            ensure_spatial()
                        except Exception:
                            pass
                finally:
                    self.config(cursor="")
            self._schedule_redraw()
            if self._on_traces_change:
                self._on_traces_change(self._show_traces)

        def set_board(self, board: BoardModel) -> None:
            self.board = board
            self._highlight = set()
            self._selected_refdes = None
            self._selected_pin = None
            self._selected_net = None
            self._show_traces = False
            self._area_cache = {}
            self._sorted_components = []
            # New topology object → drop the geometry-on-net cache. See
            # the BoardCanvasCPU.set_board comment for the rationale.
            self._geometry_net_cache = (None, ([], []))
            # And the per-layer component count cache, same reason.
            self._comp_count_by_layer = {}
            self._compute_bounds()
            self._reorder_components()
            self.zoom = 1.0
            self.pan_x = 0.0
            self.pan_y = 0.0
            self._view_layer = "TOP"
            self._mirror_x = False
            self._rotation_quadrant = 0
            self._schedule_redraw()
            if self._on_layer_change:
                self._on_layer_change(self._view_layer)
            if self._on_traces_change:
                self._on_traces_change(self._show_traces)

        def set_view_layer(self, layer: str) -> None:
            if layer == self._view_layer:
                return
            if layer not in _available_layers_for(self.board):
                return
            self._reorient(lambda: setattr(self, "_view_layer", layer))
            if self._on_layer_change:
                self._on_layer_change(layer)

        def highlight(self, refdeses: List[str]) -> None:
            self._highlight = set(refdeses)
            if refdeses:
                first = self.board.components.get(refdeses[0])
                if first:
                    if first.layer != self._view_layer:
                        self.set_view_layer(first.layer)
                    if self.zoom > 1.5:
                        self._center_on(first.x, first.y)
            self._schedule_redraw()

        def select_refdes(
            self, refdes: Optional[str], center: bool = False,
        ) -> None:
            if refdes != self._selected_refdes:
                self._selected_pin = None
            if refdes:
                comp = self.board.components.get(refdes)
                if comp and comp.layer != self._view_layer:
                    self.set_view_layer(comp.layer)
            self._selected_refdes = refdes
            if center and refdes:
                comp = self.board.components.get(refdes)
                if comp:
                    self._center_on(comp.x, comp.y)
            self._schedule_redraw()

        def select_pin(
            self, pin_name: Optional[str], center: bool = False,
        ) -> None:
            if not self._selected_refdes:
                return
            self._selected_pin = pin_name
            if center and pin_name:
                comp = self.board.components.get(self._selected_refdes)
                if comp and comp.layer != self._view_layer:
                    self.set_view_layer(comp.layer)
                shape = self.board.shapes.get(comp.shape) if comp else None
                if comp and shape:
                    for name, dx, dy in shape.pins:
                        if name == pin_name:
                            theta = math.radians(comp.rotation)
                            ct, st = math.cos(theta), math.sin(theta)
                            wx = comp.x + dx * ct - dy * st
                            wy = comp.y + dx * st + dy * ct
                            if self.zoom < 8:
                                self.zoom = 12.0
                            self._center_on(wx, wy)
                            break
            self._schedule_redraw()
            if self._on_pin_select:
                self._on_pin_select(pin_name)

        def reset_view(self) -> None:
            self.zoom = 1.0
            self.pan_x = 0.0
            self.pan_y = 0.0
            self._schedule_redraw()

        def toggle_mirror_x(self) -> None:
            self._reorient(
                lambda: setattr(self, "_mirror_x", not self._mirror_x),
            )

        def rotate(self, steps: int) -> None:
            self._reorient(lambda: setattr(
                self, "_rotation_quadrant",
                (self._rotation_quadrant + steps) % 4,
            ))

        # ---- internal helpers — geometry / projection --------------------
        # Same math as BoardCanvasCPU. Kept self-contained on this class
        # so the CPU class can move/refactor without breaking us.

        def _compute_bounds(self) -> None:
            xs = [c.x for c in self.board.components.values()]
            ys = [c.y for c in self.board.components.values()]
            if not xs or not ys:
                self.bounds = (0.0, 0.0, 1.0, 1.0)
                return
            self.bounds = (min(xs), min(ys), max(xs), max(ys))
            # Invalidate the cached numpy component arrays — they'll
            # be rebuilt on the next frame.
            self._comp_arrays = None

        def _reorder_components(self) -> None:
            def area_of(c: Component) -> float:
                cached = self._area_cache.get(c.refdes)
                if cached is not None:
                    return cached
                s = self.board.shapes.get(c.shape)
                if not s or not s.pins:
                    a = 0.0
                else:
                    x0, y0, x1, y1 = s.bbox()
                    a = (x1 - x0) * (y1 - y0)
                self._area_cache[c.refdes] = a
                return a
            self._sorted_components = sorted(
                self.board.components.values(),
                key=lambda c: -area_of(c),
            )
            # Invalidate the per-component numpy cache — its row
            # order is keyed off _sorted_components.
            self._comp_arrays = None

        def _render_bounds(self) -> Tuple[float, float, float, float]:
            x0, y0, x1, y1 = self.bounds
            if self._rotation_quadrant % 2 == 0:
                return (x0, y0, x1, y1)
            cx_w = (x0 + x1) / 2
            cy_w = (y0 + y1) / 2
            bw = y1 - y0
            bh = x1 - x0
            return (cx_w - bw / 2, cy_w - bh / 2,
                    cx_w + bw / 2, cy_w + bh / 2)

        def _apply_view_transform(
            self, x: float, y: float,
        ) -> Tuple[float, float]:
            x0, y0, x1, y1 = self.bounds
            cx_w = (x0 + x1) / 2
            cy_w = (y0 + y1) / 2
            if (self._view_layer == "BOTTOM") ^ self._mirror_x:
                x = x0 + x1 - x
            q = self._rotation_quadrant % 4
            if q == 0:
                return (x, y)
            if q == 1:
                return (cx_w + (y - cy_w), cy_w - (x - cx_w))
            if q == 2:
                return (2 * cx_w - x, 2 * cy_w - y)
            return (cx_w - (y - cy_w), cy_w + (x - cx_w))

        def _invert_view_transform(
            self, rx: float, ry: float,
        ) -> Tuple[float, float]:
            x0, y0, x1, y1 = self.bounds
            cx_w = (x0 + x1) / 2
            cy_w = (y0 + y1) / 2
            q = self._rotation_quadrant % 4
            if q == 0:
                x, y = rx, ry
            elif q == 1:
                x = cx_w - (ry - cy_w)
                y = cy_w + (rx - cx_w)
            elif q == 2:
                x = 2 * cx_w - rx
                y = 2 * cy_w - ry
            else:
                x = cx_w + (ry - cy_w)
                y = cy_w - (rx - cx_w)
            if (self._view_layer == "BOTTOM") ^ self._mirror_x:
                x = x0 + x1 - x
            return (x, y)

        def _projection_params(
            self, w: int, h: int,
        ) -> Tuple[float, float, float, float, float, float]:
            """Returns (rx0, ry1, base_scale, base_ox, base_oy, cx)
            cached pieces of the projection so per-segment hot-loops
            don't re-derive them every call. Pure function of the view
            state; cheap; called once per redraw."""
            rx0, ry0, rx1, ry1 = self._render_bounds()
            bw = max(rx1 - rx0, 1.0)
            bh = max(ry1 - ry0, 1.0)
            pad = 12
            base_scale = min(
                (w - 2 * pad) / bw, (h - 2 * pad) / bh,
            )
            base_ox = pad + (w - 2 * pad - bw * base_scale) / 2
            base_oy = pad + (h - 2 * pad - bh * base_scale) / 2
            return rx0, ry1, base_scale, base_ox, base_oy, 0.0

        def _project(
            self, x: float, y: float, w: int, h: int,
        ) -> Tuple[float, float]:
            # Hot path during a redraw: use the cached snapshot built
            # in `_make_proj_state` so each call is just arithmetic.
            proj = getattr(self, "_frame_proj", None)
            if proj is not None:
                (x0, x1c, cx_w, cy_w, mirror, quad,
                 rx0_, ry1_, base_scale, base_ox, base_oy,
                 cx_s, cy_s, zoom, pan_x, pan_y) = proj
                if mirror:
                    x = (x0 + x1c) - x
                if quad == 0:
                    rx, ry = x, y
                elif quad == 1:
                    rx, ry = cx_w + (y - cy_w), cy_w - (x - cx_w)
                elif quad == 2:
                    rx, ry = (2 * cx_w) - x, (2 * cy_w) - y
                else:
                    rx, ry = cx_w - (y - cy_w), cy_w + (x - cx_w)
                base_sx = base_ox + (rx - rx0_) * base_scale
                base_sy = base_oy + (ry1_ - ry) * base_scale
                sx = cx_s + (base_sx - cx_s) * zoom + pan_x
                sy = cy_s + (base_sy - cy_s) * zoom + pan_y
                return sx, sy
            # Cold path (hit-testing / unproject scaffolding) — full
            # recompute. Only called outside the redraw window.
            rx, ry = self._apply_view_transform(x, y)
            rx0_, ry0_, rx1_, ry1_ = self._render_bounds()
            bw = max(rx1_ - rx0_, 1.0)
            bh = max(ry1_ - ry0_, 1.0)
            pad = 12
            base_scale = min((w - 2 * pad) / bw, (h - 2 * pad) / bh)
            base_ox = pad + (w - 2 * pad - bw * base_scale) / 2
            base_oy = pad + (h - 2 * pad - bh * base_scale) / 2
            base_sx = base_ox + (rx - rx0_) * base_scale
            base_sy = base_oy + (ry1_ - ry) * base_scale
            cx, cy = w / 2, h / 2
            sx = cx + (base_sx - cx) * self.zoom + self.pan_x
            sy = cy + (base_sy - cy) * self.zoom + self.pan_y
            return sx, sy

        def _unproject(
            self, sx: float, sy: float,
        ) -> Tuple[float, float]:
            w, h = self.winfo_width(), self.winfo_height()
            cx, cy = w / 2, h / 2
            rx0_, ry0_, rx1_, ry1_ = self._render_bounds()
            bw = max(rx1_ - rx0_, 1.0)
            bh = max(ry1_ - ry0_, 1.0)
            pad = 12
            base_scale = min((w - 2 * pad) / bw, (h - 2 * pad) / bh)
            base_ox = pad + (w - 2 * pad - bw * base_scale) / 2
            base_oy = pad + (h - 2 * pad - bh * base_scale) / 2
            base_sx = (sx - cx - self.pan_x) / self.zoom + cx
            base_sy = (sy - cy - self.pan_y) / self.zoom + cy
            rx = (base_sx - base_ox) / base_scale + rx0_
            ry = ry1_ - (base_sy - base_oy) / base_scale
            return self._invert_view_transform(rx, ry)

        def _center_on(self, wx: float, wy: float) -> None:
            w, h = self.winfo_width(), self.winfo_height()
            if w < 30 or h < 30:
                return
            rx, ry = self._apply_view_transform(wx, wy)
            rx0_, ry0_, rx1_, ry1_ = self._render_bounds()
            bw = max(rx1_ - rx0_, 1.0)
            bh = max(ry1_ - ry0_, 1.0)
            pad = 12
            base_scale = min((w - 2 * pad) / bw, (h - 2 * pad) / bh)
            base_ox = pad + (w - 2 * pad - bw * base_scale) / 2
            base_oy = pad + (h - 2 * pad - bh * base_scale) / 2
            base_sx = base_ox + (rx - rx0_) * base_scale
            base_sy = base_oy + (ry1_ - ry) * base_scale
            cx, cy = w / 2, h / 2
            self.pan_x = -(base_sx - cx) * self.zoom
            self.pan_y = -(base_sy - cy) * self.zoom

        def _reorient(self, mutate: Callable[[], None]) -> None:
            w, h = self.winfo_width(), self.winfo_height()
            wx_center = wy_center = None
            if w >= 30 and h >= 30:
                wx_center, wy_center = self._unproject(w / 2, h / 2)
            mutate()
            if wx_center is not None:
                self._center_on(wx_center, wy_center)
            self._schedule_redraw()

        def _viewport_world(
            self, w: int, h: int,
        ) -> Tuple[float, float, float, float]:
            u_tl = self._unproject(0, 0)
            u_tr = self._unproject(w, 0)
            u_bl = self._unproject(0, h)
            u_br = self._unproject(w, h)
            rx0 = min(u_tl[0], u_tr[0], u_bl[0], u_br[0])
            rx1 = max(u_tl[0], u_tr[0], u_bl[0], u_br[0])
            ry0 = min(u_tl[1], u_tr[1], u_bl[1], u_br[1])
            ry1 = max(u_tl[1], u_tr[1], u_bl[1], u_br[1])
            return rx0, ry0, rx1, ry1

        def _component_polygon_world(
            self, c: Component,
        ) -> Optional[List[Tuple[float, float]]]:
            shape = self.board.shapes.get(c.shape)
            if not shape or not shape.pins:
                return None
            x0, y0, x1, y1 = shape.bbox()
            if (x1 - x0) < 0.5 and (y1 - y0) < 0.5:
                return None
            # Same fix as the CPU class: parser already adds 5% per-axis
            # margin. The previous 10%-of-the-larger-axis padding here
            # blew up DDR4's short axis ~5× by adding the LONG-axis pad
            # to the SHORT axis. Use a tiny floor padding only.
            pad = 5
            x0 -= pad
            y0 -= pad
            x1 += pad
            y1 += pad
            corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            theta = math.radians(c.rotation)
            ct, st = math.cos(theta), math.sin(theta)
            return [
                (c.x + rx * ct - ry * st, c.y + rx * st + ry * ct)
                for rx, ry in corners
            ]

        def _component_polygon_screen(
            self, c: Component, w: int, h: int,
        ) -> Optional[List[Tuple[float, float]]]:
            world = self._component_polygon_world(c)
            if world is None:
                return None
            return [self._project(wx, wy, w, h) for wx, wy in world]

        @staticmethod
        def _bbox_of_points(
            points: List[Tuple[float, float]],
        ) -> Tuple[float, float, float, float]:
            xs = [p[0] for p in points]
            ys = [p[1] for p in points]
            return (min(xs), min(ys), max(xs), max(ys))

        @staticmethod
        def _hex_to_skia(hex_color: str, alpha: int = 255):
            c = hex_color.lstrip("#")
            r = int(c[0:2], 16)
            g = int(c[2:4], 16)
            b = int(c[4:6], 16)
            return _skia.Color(r, g, b, alpha)

        # ---- GL lifecycle -------------------------------------------------

        def initgl(self) -> None:
            """Called by OpenGLFrame on first map and on every resize.
            Idempotent: only builds the GrDirectContext once. Resize
            handling is in `_ensure_surface`."""
            w = max(self.winfo_width(), 1)
            h = max(self.winfo_height(), 1)
            _GL.glViewport(0, 0, w, h)
            r, g, b = (int(self.BG.lstrip("#")[i:i+2], 16) / 255.0
                       for i in (0, 2, 4))
            _GL.glClearColor(r, g, b, 1.0)
            if self._grctx is None:
                self._grctx = _skia.GrDirectContext.MakeGL()
                if self._grctx is None:
                    raise RuntimeError(
                        "Skia GrDirectContext.MakeGL returned None — "
                        "GL context not initialised correctly?"
                    )
                # Default Skia typeface — portable across platforms.
                # MakeFromName(None) gives whatever the OS provides.
                try:
                    self._typeface = _skia.Typeface.MakeFromName(
                        "", _skia.FontStyle.Bold(),
                    )
                except Exception:
                    self._typeface = _skia.Typeface()
                self._font_label = _skia.Font(self._typeface, 9.0)
                self._font_label.setEdging(_skia.Font.Edging.kAntiAlias)
                self._font_pin = _skia.Font(self._typeface, 10.0)
                self._font_pin.setEdging(_skia.Font.Edging.kAntiAlias)
                self._font_status = _skia.Font(self._typeface, 11.0)
                self._font_status.setEdging(_skia.Font.Edging.kAntiAlias)
                self._gl_ready = True

        def _ensure_surface(self, w: int, h: int) -> None:
            """Create / resize the Skia surface so it wraps GL framebuffer
            0 (the default backbuffer that tkSwapBuffers presents). Using
            an off-screen FBO via Surface.MakeRenderTarget would render
            into an invisible buffer — pixels would be drawn correctly
            but never reach the screen. We instead build a Skia surface
            backed by FBO 0 directly so flushAndSubmit + tkSwapBuffers
            display the result. Origin is bottom-left because GL's
            default framebuffer is y-flipped relative to Skia's
            top-left convention."""
            if (self._skia_surface is not None
                    and self._surface_w == w
                    and self._surface_h == h):
                return
            from OpenGL.GL import GL_RGBA8
            fb_info = _skia.GrGLFramebufferInfo(0, GL_RGBA8)
            # Stash the backend target on self — Skia requires it to
            # outlive the surface that wraps it.
            self._skia_backend_target = _skia.GrBackendRenderTarget(
                w, h, 0, 8, fb_info,
            )
            self._skia_surface = _skia.Surface.MakeFromBackendRenderTarget(
                self._grctx, self._skia_backend_target,
                _skia.GrSurfaceOrigin.kBottomLeft_GrSurfaceOrigin,
                _skia.kRGBA_8888_ColorType, None,
            )
            if self._skia_surface is None:
                # GL surface creation failed despite MakeGL having
                # succeeded — leave us alive on a CPU raster surface
                # (will read back via PhotoImage in derived methods,
                # not handled here — visible degradation but no crash).
                self._skia_surface = _skia.Surface(w, h)
            self._surface_w = w
            self._surface_h = h

        def _schedule_redraw(self) -> None:
            """Coalesce multiple state-change calls in the same Tk
            event into a single GL frame."""
            if self._redraw_scheduled or not self._gl_ready:
                # If GL isn't up yet, the first <Map> / <Configure>
                # will trigger initgl + tkExpose anyway.
                if not self._gl_ready:
                    # Force a paint as soon as the widget is realised.
                    pass
                if self._redraw_scheduled:
                    return
            self._redraw_scheduled = True
            self.after_idle(self._do_redraw)

        def _do_redraw(self) -> None:
            self._redraw_scheduled = False
            if not self.winfo_ismapped():
                return
            try:
                # _display() in OpenGLFrame: makes context current,
                # calls redraw(), swaps buffers.
                self._display()
            except Exception:
                traceback.print_exc()

        def _on_configure(self) -> None:
            # OpenGLFrame.tkResize updates self.width/height and calls
            # initgl. We just need to re-build the Skia surface to the
            # new dimensions and schedule a redraw.
            self._skia_surface = None
            self._schedule_redraw()

        def redraw(self) -> None:
            """Per-frame draw. Called by OpenGLFrame after the GL
            context has been made current. We render into the Skia
            surface; OpenGLFrame.tkSwapBuffers() is called for us."""
            w, h = self.winfo_width(), self.winfo_height()
            if w < 4 or h < 4:
                return
            if not self._gl_ready:
                return
            self._ensure_surface(w, h)
            _GL.glViewport(0, 0, w, h)
            canvas = self._skia_surface.getCanvas()
            canvas.clear(self._bg_color)

            dot_r = max(1.0, self.DOT_RADIUS * (self.zoom ** 0.4))
            # Compute and cache the projection scalars once per frame.
            # Hot loops (component pass + trace pass) read off this
            # snapshot so they don't re-derive _render_bounds and the
            # base scale per primitive.
            self._frame_proj = self._make_proj_state(w, h)

            if self._show_traces:
                self._draw_traces_gl(canvas, w, h)

            self._draw_components_gl(canvas, w, h, dot_r)

            if self._selected_refdes:
                c = self.board.components.get(self._selected_refdes)
                if c and c.layer == self._view_layer:
                    self._draw_pins_gl(canvas, c, w, h)

            self._draw_status_text(canvas, w, h)

            # Measurement overlay sits on top of everything. Drawing it
            # last in the paint pass mirrors what BoardCanvasCPU does at
            # the end of _redraw — keeps the line + label legible even
            # when it crosses dense trace areas.
            if self._measure_mode and (
                self._measure_pts or self._measure_hover
            ):
                self._draw_measurement_overlay_gl(canvas, w, h)

            self._skia_surface.flushAndSubmit()
            # Drop the per-frame projection cache so subsequent
            # hit-test / unproject calls don't pick up a stale view.
            self._frame_proj = None
            # Skia may have left GL state altered. Reset so the
            # subsequent SwapBuffers (Tk-driven) doesn't see stale
            # state. Cheap (~0.05 ms).
            try:
                self._grctx.resetContext()
            except Exception:
                pass

        def _make_proj_state(self, w: int, h: int):
            """Snapshot of all projection scalars used by hot loops.
            Returned as a flat tuple of plain floats so dict / attr
            lookup stays out of the inner code path."""
            x0, y0, x1, y1 = self.bounds
            cx_w = (x0 + x1) / 2
            cy_w = (y0 + y1) / 2
            mirror = (self._view_layer == "BOTTOM") ^ self._mirror_x
            quad = self._rotation_quadrant % 4
            rx0_, ry0_, rx1_, ry1_ = self._render_bounds()
            bw = max(rx1_ - rx0_, 1.0)
            bh = max(ry1_ - ry0_, 1.0)
            pad = 12
            base_scale = min((w - 2 * pad) / bw, (h - 2 * pad) / bh)
            base_ox = pad + (w - 2 * pad - bw * base_scale) / 2
            base_oy = pad + (h - 2 * pad - bh * base_scale) / 2
            return (x0, x1, cx_w, cy_w, bool(mirror), quad,
                    rx0_, ry1_, base_scale, base_ox, base_oy,
                    w / 2, h / 2, self.zoom, self.pan_x, self.pan_y)

        # ---- per-layer GL drawing ----------------------------------------

        def _ensure_comp_arrays(self) -> None:
            """Pre-compute per-component data once per board:
              - World-space polygon corners (numpy arrays, for the
                size-based per-frame classification + auto-labels).
              - World-space pre-built Skia Paths (one per layer)
                containing every component outline. Used to render
                the bulk of components in a single drawPath via
                canvas.concat(matrix).

            Y is negated at path-build time so the matrix from
            `_world_to_screen_matrix` works for both segments and
            components (both use the same world→screen affine).
            """
            if self._comp_arrays is not None:
                return
            comps = self._sorted_components
            n = len(comps)
            wx = _np.full((n, 4), _np.nan, dtype=_np.float32)
            wy = _np.full((n, 4), _np.nan, dtype=_np.float32)
            cx_arr = _np.empty(n, dtype=_np.float32)
            cy_arr = _np.empty(n, dtype=_np.float32)
            has_poly = _np.zeros(n, dtype=_np.bool_)
            layer_top = _np.zeros(n, dtype=_np.bool_)
            refdes = [None] * n
            world_size = _np.zeros(n, dtype=_np.float32)
            comp_path_top = _skia.Path()
            comp_path_bot = _skia.Path()
            for i, c in enumerate(comps):
                refdes[i] = c.refdes
                cx_arr[i] = c.x
                cy_arr[i] = c.y
                is_top = (c.layer == "TOP")
                layer_top[i] = is_top
                world = self._component_polygon_world(c)
                if world is not None:
                    has_poly[i] = True
                    xs = []
                    ys = []
                    for k, (px, py) in enumerate(world):
                        wx[i, k] = px
                        wy[i, k] = py
                        xs.append(px); ys.append(py)
                    world_size[i] = max(
                        max(xs) - min(xs), max(ys) - min(ys),
                    )
                    target = comp_path_top if is_top else comp_path_bot
                    target.moveTo(world[0][0], -world[0][1])
                    target.lineTo(world[1][0], -world[1][1])
                    target.lineTo(world[2][0], -world[2][1])
                    target.lineTo(world[3][0], -world[3][1])
                    target.close()
            self._comp_arrays = {
                "wx": wx, "wy": wy,
                "cx": cx_arr, "cy": cy_arr,
                "has_poly": has_poly,
                "layer_top": layer_top,
                "world_size": world_size,
                "refdes": refdes,
                "comp_path_top": comp_path_top,
                "comp_path_bot": comp_path_bot,
            }

        def _draw_components_gl(
            self, canvas, w: int, h: int, dot_r: float,
        ) -> None:
            """Draw all visible components into the Skia GL surface.
            Visual rules match BoardCanvasCPU._draw_one exactly.

            Optimisation: normal-mode components for the active layer
            are batched into TWO Skia paths (one per layer colour
            since this layer is always one of the two; we just bundle
            outlines for the active layer). The highlighted +
            selected components — rare — still get individual paths
            for fills + thicker outlines + labels. Auto-labels for
            big chips are emitted in a second per-component pass.
            """
            sel_refdes = self._selected_refdes
            highlight = self._highlight
            view_layer = self._view_layer

            top_color = self._hex_to_skia(self.TOP_COLOR)
            bot_color = self._hex_to_skia(self.BOTTOM_COLOR)
            highlight_fill = self._hex_to_skia(self.HIGHLIGHT)
            highlight_ring = self._hex_to_skia(self.HIGHLIGHT_RING)
            sel_outline = self._hex_to_skia(self.SELECTED_OUTLINE)
            label_top_fixed = self._hex_to_skia("#9fb6ff")
            label_bot_fixed = self._hex_to_skia("#ffaa9f")
            label_highlight_color = self._hex_to_skia("#ffffcc")
            label_selected_color = self._hex_to_skia("#aaffff")

            stroke_paint = _skia.Paint()
            stroke_paint.setStyle(_skia.Paint.Style.kStroke_Style)
            stroke_paint.setAntiAlias(True)
            stroke_paint.setStrokeWidth(1.0)

            stroke_paint_thick = _skia.Paint()
            stroke_paint_thick.setStyle(_skia.Paint.Style.kStroke_Style)
            stroke_paint_thick.setAntiAlias(True)
            stroke_paint_thick.setStrokeWidth(2.0)

            fill_paint = _skia.Paint()
            fill_paint.setStyle(_skia.Paint.Style.kFill_Style)
            fill_paint.setAntiAlias(True)

            text_paint = _skia.Paint()
            text_paint.setAntiAlias(True)

            # ---- Pass 1: bulk component outlines (matrix-transformed) ---
            # The pre-built world-space path contains every component
            # outline on this layer. Drawing it with canvas.concat(M)
            # is essentially a single GPU command — sub-millisecond
            # regardless of board size.
            #
            # Highlight/selected components and big-chip auto-labels
            # are handled in the next pass (per-frame projection so
            # the screen-pixel size threshold is exact).
            self._ensure_comp_arrays()
            arrs = self._comp_arrays
            (_, _, _, _, _, _, _, _, base_scale, _, _, _, _,
             zoom, _, _) = self._frame_proj
            effective_scale = base_scale * zoom

            view_is_inner = view_layer not in ("TOP", "BOTTOM")
            if view_is_inner:
                # Inner copper layer in view: components live on TOP/
                # BOTTOM only, so paint *both* outline paths in the
                # faint ghost colour and return early — no labels, no
                # highlight, no selection.
                ghost_paint = _skia.Paint()
                ghost_paint.setStyle(_skia.Paint.Style.kStroke_Style)
                ghost_paint.setAntiAlias(True)
                ghost_paint.setColor(self._hex_to_skia(self.GHOST_OUTLINE))
                ghost_paint.setStrokeWidth(
                    1.0 / effective_scale if effective_scale > 1e-6 else 1.0
                )
                matrix = self._world_to_screen_matrix()
                canvas.save()
                canvas.concat(matrix)
                canvas.drawPath(arrs["comp_path_top"], ghost_paint)
                canvas.drawPath(arrs["comp_path_bot"], ghost_paint)
                canvas.restore()
                return

            world_comp_path = (arrs["comp_path_top"] if view_layer == "TOP"
                               else arrs["comp_path_bot"])
            stroke_paint.setColor(layer_color := (
                top_color if view_layer == "TOP" else bot_color
            ))
            # 1px on screen regardless of zoom (matrix scales strokes).
            stroke_paint.setStrokeWidth(
                1.0 / effective_scale if effective_scale > 1e-6 else 1.0
            )
            matrix = self._world_to_screen_matrix()
            canvas.save()
            canvas.concat(matrix)
            canvas.drawPath(world_comp_path, stroke_paint)
            canvas.restore()
            # Restore stroke width to 1.0 for the rest of the pipeline.
            stroke_paint.setStrokeWidth(1.0)

            # ---- Pass 2: big-chip auto-labels + dot fallback ------------
            # We still need per-frame screen-pixel data for these:
            #   - Auto-labels for chips with screen size >= 18 px
            #   - Dots for components whose polygon is < 3 px on screen
            # Both require knowing the projected size, so we fall
            # through the same vectorised projection but only build
            # the small Python tails (labels + dots).
            wx = arrs["wx"]; wy = arrs["wy"]
            has_poly = arrs["has_poly"]; layer_top = arrs["layer_top"]
            cx_w = arrs["cx"]; cy_w = arrs["cy"]
            refdes_list = arrs["refdes"]

            want_top = (view_layer == "TOP")
            mask = (layer_top == want_top)
            if highlight or sel_refdes:
                exclude = set(highlight)
                if sel_refdes:
                    exclude.add(sel_refdes)
                if exclude:
                    excl_mask = _np.array(
                        [r in exclude for r in refdes_list],
                        dtype=_np.bool_,
                    )
                    mask &= ~excl_mask

            idx = _np.flatnonzero(mask)
            dot_records_x: List[float] = []
            dot_records_y: List[float] = []
            big_chip_labels: List[Tuple[str, float, float, float]] = []

            if idx.size > 0:
                wxv = wx[idx]
                wyv = wy[idx]
                flat_x = wxv.reshape(-1)
                flat_y = wyv.reshape(-1)
                nan_x = _np.isnan(flat_x)
                if nan_x.any():
                    cx_rep = _np.repeat(cx_w[idx], 4)
                    cy_rep = _np.repeat(cy_w[idx], 4)
                    flat_x = _np.where(nan_x, cx_rep, flat_x)
                    flat_y = _np.where(_np.isnan(flat_y), cy_rep, flat_y)
                psx, psy = self._project_arrays(flat_x, flat_y)
                psx = psx.reshape(-1, 4)
                psy = psy.reshape(-1, 4)
                cdx, cdy = self._project_arrays(cx_w[idx], cy_w[idx])
                pmin_x = psx.min(axis=1)
                pmax_x = psx.max(axis=1)
                pmin_y = psy.min(axis=1)
                pmax_y = psy.max(axis=1)
                poly_w_arr = pmax_x - pmin_x
                poly_h_arr = pmax_y - pmin_y
                onscreen = (
                    (pmax_x >= -10) & (pmin_x <= w + 10)
                    & (pmax_y >= -10) & (pmin_y <= h + 10)
                )
                draw_poly = (
                    has_poly[idx]
                    & onscreen
                    & ((poly_w_arr >= 3) | (poly_h_arr >= 3))
                )
                dot_only = onscreen & ~draw_poly
                big_mask = (
                    draw_poly
                    & (_np.maximum(poly_w_arr, poly_h_arr) >= 18)
                )

                idx_list = idx.tolist()
                if big_mask.any():
                    pmin_x_l = pmin_x.tolist()
                    pmax_x_l = pmax_x.tolist()
                    pmin_y_l = pmin_y.tolist()
                    pmax_y_l = pmax_y.tolist()
                    pw_l = poly_w_arr.tolist()
                    ph_l = poly_h_arr.tolist()
                    big_idx = _np.flatnonzero(big_mask).tolist()
                    for j in big_idx:
                        poly_w_v = pw_l[j]
                        poly_h_v = ph_l[j]
                        fs = max(8.0, min(11.0, min(poly_w_v, poly_h_v) / 12.0))
                        big_chip_labels.append((
                            refdes_list[idx_list[j]],
                            (pmin_x_l[j] + pmax_x_l[j]) / 2,
                            (pmin_y_l[j] + pmax_y_l[j]) / 2,
                            fs,
                        ))

                if dot_only.any():
                    cdx_l = cdx.tolist()
                    cdy_l = cdy.tolist()
                    dot_idx = _np.flatnonzero(dot_only).tolist()
                    for j in dot_idx:
                        sx = cdx_l[j]; sy = cdy_l[j]
                        if -10 <= sx <= w + 10 and -10 <= sy <= h + 10:
                            dot_records_x.append(sx)
                            dot_records_y.append(sy)

            # Dot-sized components — many small circles. The bulk
            # outline path drew them as 4-vertex squares; we still
            # add a small filled dot at the centre so very-small
            # parts have a visible "presence".
            if dot_records_x:
                fill_paint.setColor(layer_color)
                for sx, sy in zip(dot_records_x, dot_records_y):
                    canvas.drawCircle(sx, sy, dot_r, fill_paint)

            # Auto-labels for big chips — same colour as the original
            # CPU path (#9fb6ff / #ffaa9f) selected by the layer.
            if big_chip_labels:
                text_color = (label_top_fixed if view_layer == "TOP"
                              else label_bot_fixed)
                text_paint.setColor(text_color)
                # Cluster by font_size to reduce Font construction
                # overhead. The set is tiny (3-4 sizes typically).
                from collections import defaultdict
                by_size: Dict[float, List[Tuple[str, float, float]]] = defaultdict(list)
                for refdes, cx, cy, fs in big_chip_labels:
                    by_size[round(fs, 1)].append((refdes, cx, cy))
                for fs, items in by_size.items():
                    font = _skia.Font(self._typeface, fs)
                    font.setEdging(_skia.Font.Edging.kAntiAlias)
                    metrics = font.getMetrics()
                    baseline_off = -(metrics.fAscent + metrics.fDescent) / 2
                    for refdes, cx, cy in items:
                        try:
                            width = font.measureText(refdes)
                        except Exception:
                            width = len(refdes) * fs * 0.55
                        blob = _skia.TextBlob.MakeFromString(refdes, font)
                        canvas.drawTextBlob(
                            blob, cx - width / 2, cy + baseline_off,
                            text_paint,
                        )

            # ---- Pass 2: highlighted components (rare, individual draw).
            for refdes in highlight:
                if refdes == sel_refdes:
                    continue
                c = self.board.components.get(refdes)
                if c and c.layer == view_layer:
                    self._draw_one_gl(
                        canvas, c, w, h, dot_r,
                        mode="highlight",
                        layer_color=(top_color if c.layer == "TOP"
                                     else bot_color),
                        fill_paint=fill_paint,
                        stroke_paint=stroke_paint,
                        text_paint=text_paint,
                        label_color=label_highlight_color,
                        highlight_fill=highlight_fill,
                        highlight_ring=highlight_ring,
                        sel_outline=sel_outline,
                    )

            # ---- Pass 3: selected component — top-most.
            if sel_refdes:
                c = self.board.components.get(sel_refdes)
                if c and c.layer == view_layer:
                    self._draw_one_gl(
                        canvas, c, w, h, dot_r,
                        mode="selected",
                        layer_color=(top_color if c.layer == "TOP"
                                     else bot_color),
                        fill_paint=fill_paint,
                        stroke_paint=stroke_paint,
                        text_paint=text_paint,
                        label_color=label_selected_color,
                        highlight_fill=highlight_fill,
                        highlight_ring=highlight_ring,
                        sel_outline=sel_outline,
                    )

        def _draw_one_gl(
            self, canvas, c: Component, w: int, h: int, dot_r: float,
            *, mode: str, layer_color, fill_paint, stroke_paint, text_paint,
            label_color, highlight_fill, highlight_ring, sel_outline,
        ) -> None:
            if mode == "normal":
                fill, outline, outline_width = None, layer_color, 1.0
                want_label = False
            elif mode == "highlight":
                fill = highlight_fill
                outline = highlight_ring
                outline_width = 2.0
                want_label = True
            else:  # selected
                # No body fill on a plain selection — outline + label
                # carries the indicator and the trace overlay below
                # stays visible. Step-highlighted components keep their
                # bright fill since the user is actively tracking them.
                fill = (highlight_fill if c.refdes in self._highlight
                        else None)
                outline = sel_outline
                outline_width = 3.0
                want_label = True

            poly = self._component_polygon_screen(c, w, h)
            if poly:
                x0p, y0p, x1p, y1p = self._bbox_of_points(poly)
                if x1p < -10 or x0p > w + 10 or y1p < -10 or y0p > h + 10:
                    return
                poly_w = x1p - x0p
                poly_h = y1p - y0p
                if poly_w >= 3 or poly_h >= 3:
                    auto_label = (mode == "normal" and not want_label
                                  and max(poly_w, poly_h) >= 18)
                    if mode == "normal" and max(poly_w, poly_h) >= 18:
                        outline_width = 2.0
                    path = _skia.Path()
                    px, py = poly[0]
                    path.moveTo(px, py)
                    for px, py in poly[1:]:
                        path.lineTo(px, py)
                    path.close()
                    if fill is not None:
                        fill_paint.setColor(fill)
                        canvas.drawPath(path, fill_paint)
                    stroke_paint.setColor(outline)
                    stroke_paint.setStrokeWidth(outline_width)
                    canvas.drawPath(path, stroke_paint)
                    if want_label or auto_label:
                        if want_label:
                            text_color = label_color
                        else:
                            text_color = (self._hex_to_skia("#9fb6ff")
                                          if c.layer == "TOP"
                                          else self._hex_to_skia("#ffaa9f"))
                        font_size = 9.0 if want_label else max(
                            8.0, min(11.0, min(poly_w, poly_h) / 12.0),
                        )
                        font = self._font_label
                        if abs(font.getSize() - font_size) > 0.5:
                            font = _skia.Font(self._typeface, font_size)
                            font.setEdging(_skia.Font.Edging.kAntiAlias)
                        text_paint.setColor(text_color)
                        self._draw_text_centered(
                            canvas, c.refdes, font, text_paint,
                            (x0p + x1p) / 2, (y0p + y1p) / 2,
                        )
                    return

            # Fallback: tiny shape — render as a dot.
            sx, sy = self._project(c.x, c.y, w, h)
            if sx < -10 or sx > w + 10 or sy < -10 or sy > h + 10:
                return
            dot_color = fill if fill is not None else outline
            fill_paint.setColor(dot_color)
            canvas.drawCircle(sx, sy, dot_r, fill_paint)
            if want_label:
                text_paint.setColor(label_color)
                font = self._font_label
                # Anchor west; baseline-aligned manual offset.
                metrics = font.getMetrics()
                baseline = sy - (metrics.fAscent + metrics.fDescent) / 2
                blob = _skia.TextBlob.MakeFromString(c.refdes, font)
                canvas.drawTextBlob(
                    blob, sx + dot_r + 4, baseline, text_paint,
                )

        def _draw_text_centered(
            self, canvas, text: str, font, paint,
            cx: float, cy: float,
        ) -> None:
            """Centre the text both horizontally and vertically. Skia
            measures by ascent/descent, so we shift cy by (asc+desc)/2."""
            blob = _skia.TextBlob.MakeFromString(text, font)
            try:
                width = font.measureText(text)
            except Exception:
                # Older skia binding — measure via advance widths.
                widths = font.getWidths(font.textToGlyphs(text))
                width = sum(widths)
            metrics = font.getMetrics()
            baseline = cy - (metrics.fAscent + metrics.fDescent) / 2
            canvas.drawTextBlob(
                blob, cx - width / 2, baseline, paint,
            )

        def _draw_pins_gl(
            self, canvas, c: Component, w: int, h: int,
        ) -> None:
            shape = self.board.shapes.get(c.shape)
            if not shape:
                return
            theta = math.radians(c.rotation)
            ct, st = math.cos(theta), math.sin(theta)
            pin_r = max(0.8, 1.2 * (self.zoom ** 0.35))
            sel_pin_r = max(3.5, pin_r * 2.6)

            pin_paint = _skia.Paint()
            pin_paint.setAntiAlias(True)
            pin_paint.setColor(self._hex_to_skia(self.PIN_COLOR))

            sel_paint = _skia.Paint()
            sel_paint.setAntiAlias(True)
            sel_paint.setColor(self._hex_to_skia(self.SELECTED_PIN_COLOR))

            ring_paint = _skia.Paint()
            ring_paint.setAntiAlias(True)
            ring_paint.setStyle(_skia.Paint.Style.kStroke_Style)
            ring_paint.setStrokeWidth(2.0)
            ring_paint.setColor(self._hex_to_skia(self.SELECTED_PIN_RING))

            label_paint = _skia.Paint()
            label_paint.setAntiAlias(True)
            label_paint.setColor(self._hex_to_skia("#ffaadd"))

            for pin_name, dx, dy in shape.pins:
                wx = c.x + dx * ct - dy * st
                wy = c.y + dx * st + dy * ct
                sx, sy = self._project(wx, wy, w, h)
                if sx < -2 or sx > w + 2 or sy < -2 or sy > h + 2:
                    continue
                if pin_name == self._selected_pin:
                    canvas.drawCircle(sx, sy, sel_pin_r + 2, ring_paint)
                    canvas.drawCircle(sx, sy, sel_pin_r, sel_paint)
                    blob = _skia.TextBlob.MakeFromString(
                        pin_name, self._font_pin,
                    )
                    metrics = self._font_pin.getMetrics()
                    baseline = sy - (metrics.fAscent + metrics.fDescent) / 2
                    canvas.drawTextBlob(
                        blob, sx + sel_pin_r + 4, baseline, label_paint,
                    )
                else:
                    canvas.drawCircle(sx, sy, pin_r, pin_paint)

        def _segments_arrays(self, topo):
            """Return numpy arrays for the topology's segments plus one
            pre-built world-space Skia Path *per layer*, all cached on
            the topology object.

            Returns: dict with keys
                'x1','y1','x2','y2' : (N,) float32
                'net_id'             : (N,) int32
                'layer'              : (N,) object array of layer names
                'paths'              : Dict[str, skia.Path] keyed by layer
            Indexed identically to topo.segments.

            Building the world-space Paths takes ~5-10 ms once per
            board. With them cached, per-frame trace rendering becomes:
            apply view transform via canvas.concat() + drawPath() for
            the current layer. ~1-2 ms regardless of segment count.

            Multi-layer note: every layer gets its own Path. Inner-
            layer segments are indexed with the layer name from
            `topo._layer_names` so picking `paths[view_layer]` works
            for INNER_1..N just as it does for TOP/BOTTOM.
            """
            cache = getattr(topo, "_gl_seg_arrays", None)
            if cache is not None:
                return cache
            # Fast path: read directly from the topology's numpy
            # storage when present. Avoids materialising 43 K Segment
            # dataclass instances just to copy 6 fields out. Falls
            # back to the legacy list iteration for graphs without
            # `_seg_arrays` (cache-loaded from older format, GENCAD).
            seg_arr = getattr(topo, "_seg_arrays", None)
            layer_names = list(getattr(topo, "_layer_names", []) or [])
            if seg_arr is not None:
                # Cast int32 → float32 once with numpy (vectorised).
                x1 = seg_arr["x1"].astype(_np.float32, copy=True)
                y1 = seg_arr["y1"].astype(_np.float32, copy=True)
                x2 = seg_arr["x2"].astype(_np.float32, copy=True)
                y2 = seg_arr["y2"].astype(_np.float32, copy=True)
                net_id = seg_arr["net_id"].astype(_np.int32, copy=True)
                # `layer` is stored as uint8 indexed into `_layer_names`.
                # Map each byte to its name for the dict-keyed paths.
                layer_bytes = seg_arr["layer"]
                seg_layer = _np.empty(layer_bytes.shape[0], dtype=object)
                if layer_names:
                    n_names = len(layer_names)
                    lb_list = layer_bytes.tolist()
                    for i, b in enumerate(lb_list):
                        seg_layer[i] = (layer_names[b]
                                        if 0 <= b < n_names else "TOP")
                else:
                    # No layer table — assume the historical 2-layer
                    # encoding (0=TOP, 1=BOTTOM).
                    lb_list = layer_bytes.tolist()
                    for i, b in enumerate(lb_list):
                        seg_layer[i] = "TOP" if b == 0 else "BOTTOM"
                n = int(x1.shape[0])
            else:
                segs = topo.segments
                n = len(segs)
                x1 = _np.empty(n, dtype=_np.float32)
                y1 = _np.empty(n, dtype=_np.float32)
                x2 = _np.empty(n, dtype=_np.float32)
                y2 = _np.empty(n, dtype=_np.float32)
                net_id = _np.empty(n, dtype=_np.int32)
                seg_layer = _np.empty(n, dtype=object)
                for i, seg in enumerate(segs):
                    x1[i] = seg.x1
                    y1[i] = seg.y1
                    x2[i] = seg.x2
                    y2[i] = seg.y2
                    net_id[i] = seg.net_id
                    seg_layer[i] = seg.layer
            # Pre-build the world-space dimmed paths, one per layer.
            # We negate Y at build time so the Skia matrix is a pure
            # positive-scale transform — Skia y grows down, board y
            # grows up.
            #
            # Synthetic ratsnest: split each layer into two paths,
            # `paths[layer]` (solid edges) and `paths_dashed[layer]`
            # (cross-layer edges drawn dashed). Two drawPath calls per
            # layer in the GL render — negligible cost vs. the single
            # call for real-trace topology, but keeps the dashed style
            # entirely on the cross-layer minority. Real TVW topology
            # has no `dashed` field so we skip the split there.
            #
            # `_seg_arrays` is a dict-of-arrays in both TVW and the
            # synthetic ratsnest, so `in` is a simple key-membership
            # test, not a numpy structured-dtype lookup.
            dashed_arr = None
            if seg_arr is not None and "dashed" in seg_arr:
                dashed_arr = seg_arr["dashed"]
            elif seg_arr is None:
                if any(getattr(s, "dashed", False) for s in segs):
                    dashed_arr = _np.fromiter(
                        (1 if getattr(s, "dashed", False) else 0
                         for s in segs),
                        count=n, dtype=_np.uint8,
                    )
            has_dashed = (
                dashed_arr is not None and bool(dashed_arr.any())
            )
            paths: Dict[str, "_skia.Path"] = {}
            paths_dashed: Dict[str, "_skia.Path"] = {}
            x1_l = x1.tolist(); y1_l = y1.tolist()
            x2_l = x2.tolist(); y2_l = y2.tolist()
            if has_dashed:
                d_l = dashed_arr.tolist()
                for i in range(n):
                    ln = seg_layer[i]
                    bucket = paths_dashed if d_l[i] else paths
                    p = bucket.get(ln)
                    if p is None:
                        p = _skia.Path()
                        bucket[ln] = p
                    p.moveTo(x1_l[i], -y1_l[i])
                    p.lineTo(x2_l[i], -y2_l[i])
            else:
                for i in range(n):
                    ln = seg_layer[i]
                    p = paths.get(ln)
                    if p is None:
                        p = _skia.Path()
                        paths[ln] = p
                    p.moveTo(x1_l[i], -y1_l[i])
                    p.lineTo(x2_l[i], -y2_l[i])
            cache = {
                "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                "net_id": net_id, "layer": seg_layer,
                "paths": paths,
                "paths_dashed": paths_dashed,
                "has_dashed": has_dashed,
            }
            try:
                topo._gl_seg_arrays = cache
            except Exception:
                pass
            return cache

        def _project_arrays(self, x: "_np.ndarray", y: "_np.ndarray"):
            """Vectorised version of `_project`. Takes float32 arrays
            of shape (N,) and returns (sx, sy) float32 arrays.
            Reads the cached `_frame_proj` snapshot so the dispatch
            cost is amortised across the entire frame."""
            (x0, x1c, cx_w, cy_w, mirror, quad,
             rx0_, ry1_, base_scale, base_ox, base_oy,
             cx_s, cy_s, zoom, pan_x, pan_y) = self._frame_proj
            if mirror:
                x = (x0 + x1c) - x
            if quad == 0:
                rx, ry = x, y
            elif quad == 1:
                rx = cx_w + (y - cy_w)
                ry = cy_w - (x - cx_w)
            elif quad == 2:
                rx = (2 * cx_w) - x
                ry = (2 * cy_w) - y
            else:
                rx = cx_w - (y - cy_w)
                ry = cy_w + (x - cx_w)
            base_sx = base_ox + (rx - rx0_) * base_scale
            base_sy = base_oy + (ry1_ - ry) * base_scale
            sx = cx_s + (base_sx - cx_s) * zoom + pan_x
            sy = cy_s + (base_sy - cy_s) * zoom + pan_y
            return sx.astype(_np.float32), sy.astype(_np.float32)

        def _world_to_screen_matrix(self):
            """Build the 3x3 affine that takes world (x, -y) -> screen.
            We negated y at path-build time so the matrix here is a
            pure positive-scale + translate (with the rotation+mirror
            blended in for the active orientation).

            Decomposition of self._project at the moment _frame_proj
            was built:
                (rx, ry) = view_xform(x, y)               [mirror+rotate]
                base_sx = base_ox + (rx - rx0_) * base_scale
                base_sy = base_oy + (ry1_ - ry) * base_scale
                sx = cx_s + (base_sx - cx_s) * zoom + pan_x
                sy = cy_s + (base_sy - cy_s) * zoom + pan_y

            The view_xform is itself an affine in (x, y), so the
            entire pipeline is a 3x3 affine.  We work it out in pieces
            and post-multiply.
            """
            (x0, x1c, cx_w, cy_w, mirror, quad,
             rx0_, ry1_, base_scale, base_ox, base_oy,
             cx_s, cy_s, zoom, pan_x, pan_y) = self._frame_proj

            # The path was built with y' = -y_world. Start by undoing
            # that: the matrix's inputs are (x_world, -y_world). To
            # recover (x_world, y_world) for the rest of the chain we
            # multiply input.y by -1 — i.e. our matrix is going to
            # treat input (X, Y) where Y = -y_world.
            # That means: y_world = -Y. We'll fold it in below.

            # ---- view_xform matrix M_view applied to (x, y_world) ----
            # We need a 3x3 matrix M_view that maps (x, y_world, 1) →
            # (rx, ry, 1).
            # Mirror flips x: x' = (x0+x1c) - x
            # Rotation:
            #   q=0:  rx=x',  ry=y_w
            #   q=1:  rx = cx_w + (y_w - cy_w),   ry = cy_w - (x' - cx_w)
            #   q=2:  rx = 2*cx_w - x',           ry = 2*cy_w - y_w
            #   q=3:  rx = cx_w - (y_w - cy_w),   ry = cy_w + (x' - cx_w)
            #
            # Combine with the mirror substitution x = (x0+x1c) - x',
            # but here we're going FORWARD: input is (x, y_world), so
            # x' = (x0+x1c) - x if mirror else x.
            #
            # Easier path: pick the full affine A, B, C, D, E, F such
            # that (rx, ry) = (A*x + B*y_world + C, D*x + E*y_world + F).
            if mirror:
                # x' = (x0+x1c) - x  →  use coefficient -1 on x and
                # constant (x0+x1c).
                xc = (x0 + x1c)
                if quad == 0:
                    A, B, C = -1.0, 0.0, xc
                    D, E, F = 0.0, 1.0, 0.0
                elif quad == 1:
                    A, B, C = 0.0, 1.0, cx_w - cy_w
                    D, E, F = 1.0, 0.0, cy_w - (xc - cx_w)
                elif quad == 2:
                    A, B, C = 1.0, 0.0, 2 * cx_w - xc
                    D, E, F = 0.0, -1.0, 2 * cy_w
                else:  # 3
                    A, B, C = 0.0, -1.0, cx_w + cy_w
                    D, E, F = -1.0, 0.0, cy_w + (xc - cx_w)
            else:
                if quad == 0:
                    A, B, C = 1.0, 0.0, 0.0
                    D, E, F = 0.0, 1.0, 0.0
                elif quad == 1:
                    A, B, C = 0.0, 1.0, cx_w - cy_w
                    D, E, F = -1.0, 0.0, cy_w + cx_w
                elif quad == 2:
                    A, B, C = -1.0, 0.0, 2 * cx_w
                    D, E, F = 0.0, -1.0, 2 * cy_w
                else:
                    A, B, C = 0.0, -1.0, cx_w + cy_w
                    D, E, F = 1.0, 0.0, cy_w - cx_w

            # ---- screen-space affine on top of (rx, ry) ----
            # base_sx = base_ox + (rx - rx0_) * base_scale
            # sx = cx_s + (base_sx - cx_s) * zoom + pan_x
            #    = cx_s + (base_ox - cx_s + (rx - rx0_) * base_scale) * zoom + pan_x
            # → linear coefficient on rx: base_scale * zoom
            # → constant: cx_s + (base_ox - cx_s - rx0_ * base_scale) * zoom + pan_x
            ax = base_scale * zoom
            const_sx = cx_s + (base_ox - cx_s - rx0_ * base_scale) * zoom + pan_x
            # base_sy = base_oy + (ry1_ - ry) * base_scale
            # sy = cy_s + (base_sy - cy_s) * zoom + pan_y
            # → linear coefficient on ry: -base_scale * zoom
            # → constant: cy_s + (base_oy - cy_s + ry1_ * base_scale) * zoom + pan_y
            ay = -base_scale * zoom
            const_sy = cy_s + (base_oy - cy_s + ry1_ * base_scale) * zoom + pan_y

            # Compose: input is (x, y_world). The path uses input
            # (x_path, y_path) where y_path = -y_world. So substitute
            # y_world = -y_path everywhere.
            # rx = A*x + B*y_world + C = A*x - B*y_path + C
            # ry = D*x + E*y_world + F = D*x - E*y_path + F
            # sx = ax * rx + const_sx = ax*A*x - ax*B*y_path + ax*C + const_sx
            # sy = ay * ry + const_sy = ay*D*x - ay*E*y_path + ay*F + const_sy
            scaleX = ax * A
            skewX  = -ax * B
            transX = ax * C + const_sx
            skewY  = ay * D
            scaleY = -ay * E
            transY = ay * F + const_sy
            return _skia.Matrix.MakeAll(
                scaleX, skewX, transX,
                skewY, scaleY, transY,
                0.0, 0.0, 1.0,
            )

        def _draw_traces_gl(self, canvas, w: int, h: int) -> None:
            """Render the trace overlay onto the GPU surface.

            Strategy:
              Phase A (dimmed all-traces): use a pre-built world-space
              Skia Path (cached on the topology) and let the GPU apply
              the view transform via canvas.concat(matrix). This skips
              all per-frame Python work and pushes a single drawPath
              taking ~1-2 ms regardless of segment count. Skia clips
              off-screen geometry inside the rasteriser.

              Phase B (highlight): the selected net's geometry is
              small (~10-200 segments). We project per-frame and
              build a fresh Path. Cost ~0.5 ms.

              The bright highlight overlaps the dim line cleanly
              because the highlight is wider (2px vs 1px) and AA so
              the overlap is invisible.
            """
            topo = getattr(self.board, "topology", None)
            if topo is None:
                return
            layer = self._view_layer
            sel_net_id: Optional[int] = None
            if self._selected_net:
                try:
                    sel_net_id = topo.net_id_by_name(self._selected_net)
                except Exception:
                    sel_net_id = None

            arrs = self._segments_arrays(topo)

            # Synthetic ratsnest cues (real TVW topology has no
            # `is_synthetic` so this all branches off cleanly).
            is_synthetic = getattr(topo, "is_synthetic", False)
            synth_alpha_scale = 0.7 if is_synthetic else 1.0
            paths_dashed = arrs.get("paths_dashed") or {}
            has_dashed = bool(arrs.get("has_dashed"))

            # ---- Phase A: dimmed all-traces (matrix-transformed) ---------
            # Only renders the *current* layer. Inner-layer views show
            # the inner copper; rendering every layer's all-traces would
            # be visually overwhelming.
            #
            # For synthetic ratsnest the layer's geometry is split into
            # `paths[layer]` (solid) and `paths_dashed[layer]` (dashed
            # cross-layer hints). One drawPath each, both under the
            # same world-to-screen matrix. The dashed path uses Skia's
            # PathEffect — a fixed-on-screen dash period rather than a
            # world-space one, since the matrix transform would
            # otherwise stretch the dashes at high zoom.
            paths = arrs["paths"]
            if (self.zoom >= self.TRACE_DIMMED_ZOOM_THRESHOLD
                    and (layer in paths or layer in paths_dashed)):
                base_color = self._hex_to_skia(_layer_color(layer, dim=True))
                if is_synthetic:
                    a = (base_color >> 24) & 0xFF
                    rgb = base_color & 0x00FFFFFF
                    a = int(a * synth_alpha_scale)
                    base_color = (a << 24) | rgb
                paint = _skia.Paint()
                paint.setColor(base_color)
                paint.setStyle(_skia.Paint.Style.kStroke_Style)
                paint.setStrokeWidth(1.0)
                paint.setAntiAlias(False)
                # Stroke width scales WITH the matrix unless we use
                # `setStroke` mode that's matrix-independent. Skia
                # strokes are matrix-affected — at zoom 8.0 a 1px
                # stroke would render as 8px, which is wrong. We
                # compensate by setting the stroke width to 1/scale
                # so the on-screen stroke stays 1px.
                _, _, _, _, _, _, _, _, base_scale, _, _, _, _, zoom, _, _ = (
                    self._frame_proj
                )
                effective_scale = base_scale * zoom
                if effective_scale > 1e-6:
                    paint.setStrokeWidth(1.0 / effective_scale)
                matrix = self._world_to_screen_matrix()
                canvas.save()
                canvas.concat(matrix)
                if layer in paths:
                    canvas.drawPath(paths[layer], paint)
                if has_dashed and layer in paths_dashed:
                    on_off = 4.0
                    if effective_scale > 1e-6:
                        on_off = 4.0 / effective_scale
                    dash_paint = _skia.Paint()
                    dash_paint.setColor(base_color)
                    dash_paint.setStyle(_skia.Paint.Style.kStroke_Style)
                    dash_paint.setStrokeWidth(paint.getStrokeWidth())
                    dash_paint.setAntiAlias(False)
                    dash_paint.setPathEffect(
                        _skia.DashPathEffect.Make([on_off, on_off], 0.0))
                    canvas.drawPath(paths_dashed[layer], dash_paint)
                canvas.restore()

            # ---- Phase B: highlight for the selected net -----------------
            # Cross-layer: every layer the net touches gets rendered.
            # Current layer = bright TRACE_HIGHLIGHT (yellow), 2px.
            # Off-current layers = bright palette colour for that layer,
            # 1.5px. The graph already fuses connectivity through vias
            # (UF unions in tvw_topology.py); we just stop filtering by
            # layer here. For synthetic ratsnest, dashed cross-layer
            # edges keep their dash style even when highlighted.
            if sel_net_id is not None:
                cached_id, cached_geom = self._geometry_net_cache
                if cached_id == sel_net_id:
                    segs, polys = cached_geom
                else:
                    try:
                        segs, polys = topo.geometry_on_net(sel_net_id)
                    except Exception:
                        segs, polys = [], []
                    self._geometry_net_cache = (sel_net_id, (segs, polys))

                # Group by (layer, dashed). Two drawPath calls per
                # layer if any dashed segments are present; the dashed
                # bucket stays empty for real TVW topology.
                segs_by_bucket: Dict[Tuple[str, bool], List] = {}
                for seg in segs:
                    key = (seg.layer, bool(getattr(seg, "dashed", False)))
                    segs_by_bucket.setdefault(key, []).append(seg)

                for (seg_layer_name, is_dashed), seg_list in segs_by_bucket.items():
                    is_current = (seg_layer_name == layer)
                    color_hex = (self.TRACE_HIGHLIGHT if is_current
                                 else _layer_color(seg_layer_name, dim=False))
                    seg_paint = _skia.Paint()
                    seg_paint.setColor(self._hex_to_skia(color_hex))
                    seg_paint.setStyle(_skia.Paint.Style.kStroke_Style)
                    seg_paint.setStrokeWidth(2.0 if is_current else 1.5)
                    seg_paint.setAntiAlias(True)
                    if is_dashed:
                        seg_paint.setPathEffect(
                            _skia.DashPathEffect.Make([4.0, 4.0], 0.0))

                    sx1l: List[float] = []
                    sy1l: List[float] = []
                    sx2l: List[float] = []
                    sy2l: List[float] = []
                    for seg in seg_list:
                        sx1l.append(seg.x1); sy1l.append(seg.y1)
                        sx2l.append(seg.x2); sy2l.append(seg.y2)
                    if not sx1l:
                        continue
                    a1x = _np.asarray(sx1l, dtype=_np.float32)
                    a1y = _np.asarray(sy1l, dtype=_np.float32)
                    a2x = _np.asarray(sx2l, dtype=_np.float32)
                    a2y = _np.asarray(sy2l, dtype=_np.float32)
                    p1x, p1y = self._project_arrays(a1x, a1y)
                    p2x, p2y = self._project_arrays(a2x, a2y)
                    seg_path = _skia.Path()
                    p1xl = p1x.tolist()
                    p1yl = p1y.tolist()
                    p2xl = p2x.tolist()
                    p2yl = p2y.tolist()
                    for i in range(len(p1xl)):
                        seg_path.moveTo(p1xl[i], p1yl[i])
                        seg_path.lineTo(p2xl[i], p2yl[i])
                    canvas.drawPath(seg_path, seg_paint)

                polys_by_layer: Dict[str, List] = {}
                for poly in polys:
                    if len(poly.vertices) < 2:
                        continue
                    polys_by_layer.setdefault(poly.layer, []).append(poly)
                for poly_layer_name, poly_list in polys_by_layer.items():
                    is_current = (poly_layer_name == layer)
                    color_hex = (self.TRACE_HIGHLIGHT if is_current
                                 else _layer_color(poly_layer_name, dim=False))
                    poly_paint = _skia.Paint()
                    poly_paint.setColor(self._hex_to_skia(color_hex))
                    poly_paint.setStyle(_skia.Paint.Style.kStroke_Style)
                    poly_paint.setStrokeWidth(1.0)
                    poly_paint.setAntiAlias(True)
                    poly_path = _skia.Path()
                    all_vx: List[float] = []
                    all_vy: List[float] = []
                    breaks: List[int] = []
                    for poly in poly_list:
                        breaks.append(len(all_vx))
                        for vx, vy in poly.vertices:
                            all_vx.append(vx)
                            all_vy.append(vy)
                    if not all_vx:
                        continue
                    avx = _np.asarray(all_vx, dtype=_np.float32)
                    avy = _np.asarray(all_vy, dtype=_np.float32)
                    psx, psy = self._project_arrays(avx, avy)
                    psxl = psx.tolist()
                    psyl = psy.tolist()
                    breakset = set(breaks)
                    for i in range(len(psxl)):
                        if i in breakset:
                            poly_path.moveTo(psxl[i], psyl[i])
                        else:
                            poly_path.lineTo(psxl[i], psyl[i])
                    canvas.drawPath(poly_path, poly_paint)

                # ---- Phase C: pin-stub auto-completion -----------------
                # TVW trace polylines terminate at via/pad-edge, not at
                # pad centres. After master-fp made pin centres precise
                # the residual gap (~50-500 file units, ~16-160 µm) is
                # visible at zoom. Draw a short highlight-coloured stub
                # from each pin-on-net to its nearest same-layer segment
                # endpoint, capped at 500 file units. That cap is well
                # under half-pitch for any common geometry (LGA1200
                # pitch ~2625, DDR4 ~2656, 0.4 mm IC ~1250), so the
                # stub cannot land on a neighbour pin; same-net
                # restriction means even worst-case it'd point to a
                # legitimate connection.
                PINSTUB_MAX_SQ = 500.0 * 500.0
                net_name = self._selected_net
                sigs = getattr(self.board, "signals", None)
                if (net_name and sigs and net_name in sigs
                        and segs):
                    ex_list: List[float] = []
                    ey_list: List[float] = []
                    for seg in segs:
                        if seg.layer != layer:
                            continue
                        ex_list.append(seg.x1); ey_list.append(seg.y1)
                        ex_list.append(seg.x2); ey_list.append(seg.y2)
                    if ex_list:
                        ex_arr = _np.asarray(ex_list, dtype=_np.float32)
                        ey_arr = _np.asarray(ey_list, dtype=_np.float32)
                        stub_paint = _skia.Paint()
                        # Pin stubs are *current-layer* only (they bridge
                        # a current-layer pin to a current-layer segment
                        # endpoint), so the bright TRACE_HIGHLIGHT colour
                        # is correct regardless of which layer is in view.
                        stub_paint.setColor(self._hex_to_skia(self.TRACE_HIGHLIGHT))
                        stub_paint.setStyle(_skia.Paint.Style.kStroke_Style)
                        stub_paint.setStrokeWidth(2.0)
                        stub_paint.setAntiAlias(True)
                        stub_path = _skia.Path()
                        any_stub = False
                        for refdes, pin_name in sigs[net_name]:
                            comp = self.board.components.get(refdes)
                            if not comp or comp.layer != layer:
                                continue
                            shape = self.board.shapes.get(comp.shape)
                            if not shape or not shape.pins:
                                continue
                            pin_xy = next(
                                ((dx, dy) for nm, dx, dy in shape.pins
                                 if nm == pin_name),
                                None,
                            )
                            if pin_xy is None:
                                continue
                            theta_p = math.radians(comp.rotation)
                            ct_p = math.cos(theta_p)
                            st_p = math.sin(theta_p)
                            pdx, pdy = pin_xy
                            wx = comp.x + pdx * ct_p - pdy * st_p
                            wy = comp.y + pdx * st_p + pdy * ct_p
                            d2 = ((ex_arr - wx) ** 2
                                  + (ey_arr - wy) ** 2)
                            idx = int(_np.argmin(d2))
                            if float(d2[idx]) > PINSTUB_MAX_SQ:
                                continue
                            ex_w = float(ex_arr[idx])
                            ey_w = float(ey_arr[idx])
                            sx_pin, sy_pin = self._project(
                                wx, wy, w, h,
                            )
                            sx_end, sy_end = self._project(
                                ex_w, ey_w, w, h,
                            )
                            stub_path.moveTo(sx_end, sy_end)
                            stub_path.lineTo(sx_pin, sy_pin)
                            any_stub = True
                        if any_stub:
                            canvas.drawPath(stub_path, stub_paint)

            # ---- Phase D: via markers ---------------------------------
            # Open cyan rings at every via XY, viewport-culled. Vias
            # bridge TOP↔BOTTOM by definition so we draw them on every
            # layer view — clicking a via flips the active layer.
            # Drawn in screen space (per-frame project) rather than via
            # the matrix-concat trick used for the dimmed all-traces
            # path: vias are sparse (typically <2 % of pad count, tens
            # of thousands worst case), and culling + a single drawPath
            # of small ovals stays under 2 ms on a Z490 at zoom 8.
            #
            # Synthetic ratsnest topologies have no vias (`vias=[]`),
            # so the loop is a no-op there.
            if self.zoom >= self.TRACE_DIMMED_ZOOM_THRESHOLD:
                vias = getattr(topo, "vias", None) or []
                if vias:
                    rx0v, ry0v, rx1v, ry1v = self._viewport_world(w, h)
                    via_paint = _skia.Paint()
                    via_paint.setColor(self._hex_to_skia(self.VIA_COLOR))
                    via_paint.setStyle(_skia.Paint.Style.kStroke_Style)
                    via_paint.setStrokeWidth(self.VIA_MARKER_THICKNESS_PX)
                    via_paint.setAntiAlias(True)
                    via_paint_hl: Optional["_skia.Paint"] = None
                    if sel_net_id is not None:
                        via_paint_hl = _skia.Paint()
                        via_paint_hl.setColor(
                            self._hex_to_skia(self.TRACE_HIGHLIGHT))
                        via_paint_hl.setStyle(_skia.Paint.Style.kFill_Style)
                        via_paint_hl.setAntiAlias(True)
                    rpx = self.VIA_MARKER_R_PX
                    inner_r = max(1.0, rpx - 1.0)
                    if via_paint_hl is not None:
                        for v in vias:
                            if v.x < rx0v or v.x > rx1v: continue
                            if v.y < ry0v or v.y > ry1v: continue
                            if v.net_id != sel_net_id: continue
                            sx, sy = self._project(v.x, v.y, w, h)
                            canvas.drawCircle(sx, sy, inner_r, via_paint_hl)
                    for v in vias:
                        if v.x < rx0v or v.x > rx1v: continue
                        if v.y < ry0v or v.y > ry1v: continue
                        sx, sy = self._project(v.x, v.y, w, h)
                        canvas.drawCircle(sx, sy, rpx, via_paint)

        def _draw_status_text(self, canvas, w: int, h: int) -> None:
            zoom_pct = int(self.zoom * 100)
            view_is_inner = self._view_layer not in ("TOP", "BOTTOM")
            if view_is_inner:
                n_layer = len(self.board.components)
                layer_indicator = (
                    f"{self._view_layer} (inner copper, ghost components)"
                )
                comp_label = "ghost components"
            else:
                # Lazily fill the per-layer count cache. See the matching
                # block in BoardCanvasCPU._redraw for the rationale.
                n_layer = self._comp_count_by_layer.get(self._view_layer)
                if n_layer is None:
                    n_layer = sum(
                        1 for c in self.board.components.values()
                        if c.layer == self._view_layer
                    )
                    self._comp_count_by_layer[self._view_layer] = n_layer
                layer_indicator = (
                    "TOP (looking down)" if self._view_layer == "TOP"
                    else "BOTTOM (mirrored, as if board flipped)"
                )
                comp_label = "components on this layer"
            if not self._measure_mode:
                hint_extra = "  •  M=measure"
            else:
                d = self.measurement_distance_units()
                d_prev = self.measurement_distance_preview_units()
                if d is not None:
                    readout = f"  •  measured: {self._format_distance(d)}"
                elif d_prev is not None:
                    readout = (
                        f"  •  preview: {self._format_distance(d_prev)} "
                        "(click for 2nd pt)"
                    )
                else:
                    readout = "  •  click first point"
                hint_extra = (
                    "  •  measure mode" + readout
                    + "  •  Esc clears  •  M exits"
                )
            status = (
                f"{layer_indicator}  •  {n_layer} {comp_label}"
                f"  •  zoom {zoom_pct}%  •  drag to pan, wheel to zoom, "
                "click an IC, click a pin while selected, L=cycle layer, "
                "Home=reset" + hint_extra
            )
            paint = _skia.Paint()
            paint.setAntiAlias(True)
            paint.setColor(self._hex_to_skia("#aaaadd"))
            font = self._font_label  # 9pt — matches tk.Canvas size 8/9
            blob = _skia.TextBlob.MakeFromString(status, font)
            # Tk anchor=nw → baseline ≈ asc + offset
            metrics = font.getMetrics()
            canvas.drawTextBlob(blob, 8, 8 - metrics.fAscent, paint)

        def _draw_measurement_overlay_gl(
            self, canvas, w: int, h: int,
        ) -> None:
            """Skia equivalent of BoardCanvasCPU._draw_measurement_overlay.
            Draws endpoint dots, a halo+colored connecting line, and the
            distance label with a background pill on top of the GL frame."""
            MEAS_COLOR = self._hex_to_skia("#ffd24d")
            MEAS_OUTLINE = self._hex_to_skia("#000000")
            BG_COLOR = self._hex_to_skia("#1a1a1a")
            DOT_R = 4.0

            # Compose endpoints: placed pts plus the live hover (if any).
            endpoints: List[Tuple[float, float]] = list(self._measure_pts)
            if len(endpoints) == 1 and self._measure_hover is not None:
                endpoints = endpoints + [self._measure_hover]

            # Endpoint dots — placed pts get filled circles with a black
            # outline; the hover preview point (drawn separately below)
            # gets a hollow ring to differentiate.
            dot_fill = _skia.Paint()
            dot_fill.setAntiAlias(True)
            dot_fill.setColor(MEAS_COLOR)
            dot_fill.setStyle(_skia.Paint.kFill_Style)
            dot_outline = _skia.Paint()
            dot_outline.setAntiAlias(True)
            dot_outline.setColor(MEAS_OUTLINE)
            dot_outline.setStyle(_skia.Paint.kStroke_Style)
            dot_outline.setStrokeWidth(1.0)
            for wxy in self._measure_pts:
                sx, sy = self._project(wxy[0], wxy[1], w, h)
                canvas.drawCircle(sx, sy, DOT_R, dot_fill)
                canvas.drawCircle(sx, sy, DOT_R, dot_outline)

            # Line + label only when we have two endpoints.
            if len(endpoints) == 2:
                (x1, y1), (x2, y2) = endpoints
                sx1, sy1 = self._project(x1, y1, w, h)
                sx2, sy2 = self._project(x2, y2, w, h)
                halo = _skia.Paint()
                halo.setAntiAlias(True)
                halo.setColor(MEAS_OUTLINE)
                halo.setStyle(_skia.Paint.kStroke_Style)
                halo.setStrokeWidth(4.0)
                halo.setStrokeCap(_skia.Paint.kRound_Cap)
                line_paint = _skia.Paint()
                line_paint.setAntiAlias(True)
                line_paint.setColor(MEAS_COLOR)
                line_paint.setStyle(_skia.Paint.kStroke_Style)
                line_paint.setStrokeWidth(2.0)
                line_paint.setStrokeCap(_skia.Paint.kRound_Cap)
                canvas.drawLine(sx1, sy1, sx2, sy2, halo)
                canvas.drawLine(sx1, sy1, sx2, sy2, line_paint)

                # Hover-preview endpoint: hollow ring on top of the line.
                if len(self._measure_pts) == 1:
                    preview = _skia.Paint()
                    preview.setAntiAlias(True)
                    preview.setColor(MEAS_COLOR)
                    preview.setStyle(_skia.Paint.kStroke_Style)
                    preview.setStrokeWidth(2.0)
                    canvas.drawCircle(sx2, sy2, DOT_R, preview)

                # Label centred on segment midpoint, offset perpendicular.
                d_units = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
                label = self._format_distance(d_units)
                mx, my = (sx1 + sx2) / 2, (sy1 + sy2) / 2
                dx, dy = sx2 - sx1, sy2 - sy1
                seg_len = max((dx * dx + dy * dy) ** 0.5, 1.0)
                ox, oy = -dy / seg_len * 14.0, dx / seg_len * 14.0
                tx, ty = mx + ox, my + oy

                # Measure the text to size the pill.
                font = self._font_label
                metrics = font.getMetrics()
                text_width = font.measureText(label)
                ascent = -metrics.fAscent
                descent = metrics.fDescent
                pad = 3.0
                text_h = ascent + descent
                bx0 = tx - text_width / 2 - pad
                bx1 = tx + text_width / 2 + pad
                by0 = ty - text_h / 2 - pad
                by1 = ty + text_h / 2 + pad
                # Background pill.
                bg = _skia.Paint()
                bg.setAntiAlias(True)
                bg.setColor(BG_COLOR)
                bg.setStyle(_skia.Paint.kFill_Style)
                rect = _skia.Rect.MakeLTRB(bx0, by0, bx1, by1)
                canvas.drawRect(rect, bg)
                outline = _skia.Paint()
                outline.setAntiAlias(True)
                outline.setColor(MEAS_COLOR)
                outline.setStyle(_skia.Paint.kStroke_Style)
                outline.setStrokeWidth(1.0)
                canvas.drawRect(rect, outline)
                # Label glyphs on top.
                text_paint = _skia.Paint()
                text_paint.setAntiAlias(True)
                text_paint.setColor(MEAS_COLOR)
                blob = _skia.TextBlob.MakeFromString(label, font)
                baseline_y = by0 + pad + ascent
                canvas.drawTextBlob(
                    blob, tx - text_width / 2, baseline_y, text_paint,
                )

        # ---- input handlers ----------------------------------------------

        def _on_wheel(self, event: tk.Event) -> None:
            f = (self.WHEEL_FACTOR if event.delta > 0
                 else 1 / self.WHEEL_FACTOR)
            self._apply_zoom(event.x, event.y, f)

        def _on_wheel_x11(self, event: tk.Event) -> None:
            f = self.WHEEL_FACTOR if event.num == 4 else 1 / self.WHEEL_FACTOR
            self._apply_zoom(event.x, event.y, f)

        def _apply_zoom(
            self, cx: int, cy: int, factor_in: float,
        ) -> None:
            new_zoom = max(
                self.MIN_ZOOM, min(self.MAX_ZOOM, self.zoom * factor_in),
            )
            factor = new_zoom / self.zoom
            if factor == 1.0:
                return
            canvas_cx = self.winfo_width() / 2
            canvas_cy = self.winfo_height() / 2
            self.pan_x = (cx - canvas_cx) * (1 - factor) + self.pan_x * factor
            self.pan_y = (cy - canvas_cy) * (1 - factor) + self.pan_y * factor
            self.zoom = new_zoom
            self._schedule_redraw()

        def _on_press(self, event: tk.Event) -> None:
            self._drag_start = (event.x, event.y, self.pan_x, self.pan_y)
            self._has_dragged = False
            self.config(cursor="fleur")

        def _on_drag(self, event: tk.Event) -> None:
            if not self._drag_start:
                return
            x0, y0, p0x, p0y = self._drag_start
            dx, dy = event.x - x0, event.y - y0
            if (abs(dx) > self.DRAG_THRESHOLD_PX
                    or abs(dy) > self.DRAG_THRESHOLD_PX):
                self._has_dragged = True
            self.pan_x = p0x + dx
            self.pan_y = p0y + dy
            self._schedule_redraw()

        def _on_release(self, event: tk.Event) -> None:
            was_drag = self._has_dragged
            self._drag_start = None
            self._has_dragged = False
            self.config(cursor="")
            if not was_drag:
                self._handle_click(event.x, event.y)

        def _handle_click(self, cx: int, cy: int) -> None:
            # Measurement mode short-circuits component selection. Same
            # semantics as BoardCanvasCPU._handle_click — see that method's
            # docstring for the three-point capture behaviour.
            if self._measure_mode:
                wx, wy = self._unproject(cx, cy)
                if len(self._measure_pts) >= 2:
                    self._measure_pts = [(wx, wy)]
                    self._measure_hover = None
                else:
                    self._measure_pts.append((wx, wy))
                    if len(self._measure_pts) == 2:
                        self._measure_hover = None
                self._schedule_redraw()
                if self._on_measure_change:
                    self._on_measure_change()
                return

            # Via hit-test runs before component pick. See BoardCanvasCPU
            # ._handle_click for the rationale.
            via = self._find_via_at(cx, cy)
            if via is not None:
                self._flip_layer_for_via(via)
                return

            if self._selected_refdes:
                comp = self.board.components.get(self._selected_refdes)
                if comp and comp.layer == self._view_layer:
                    shape = self.board.shapes.get(comp.shape)
                    if shape:
                        pin = self._find_pin_at(comp, shape, cx, cy)
                        if pin:
                            if pin != self._selected_pin:
                                self._selected_pin = pin
                                self._schedule_redraw()
                                if self._on_pin_select:
                                    self._on_pin_select(pin)
                            return
            refdes = self._find_component_at(cx, cy)
            if refdes != self._selected_refdes:
                self._selected_refdes = refdes
                self._selected_pin = None
                self._schedule_redraw()
                if self._on_select:
                    self._on_select(refdes)
            elif refdes is None and self._selected_pin:
                self._selected_pin = None
                self._schedule_redraw()
                if self._on_pin_select:
                    self._on_pin_select(None)

        def _find_via_at(self, cx: int, cy: int) -> Optional[Any]:
            """GL-tier mirror of BoardCanvasCPU._find_via_at."""
            if not self._show_traces:
                return None
            topo = getattr(self.board, "topology", None)
            if topo is None:
                return None
            vias = getattr(topo, "vias", None) or []
            if not vias:
                return None
            w, h = self.winfo_width(), self.winfo_height()
            r = self.VIA_CLICK_RADIUS_PX
            r2 = r * r
            best = None
            best_d2 = r2 + 1
            for v in vias:
                sx, sy = self._project(v.x, v.y, w, h)
                ddx = sx - cx
                ddy = sy - cy
                if abs(ddx) > r or abs(ddy) > r:
                    continue
                d2 = ddx * ddx + ddy * ddy
                if d2 < best_d2:
                    best_d2 = d2
                    best = v
            return best

        def _flip_layer_for_via(self, via: Any) -> None:
            """GL-tier mirror of BoardCanvasCPU._flip_layer_for_via."""
            cur = self._view_layer
            target = "BOTTOM" if cur == "TOP" else "TOP"
            if target != cur:
                self.set_view_layer(target)

        def _on_motion(self, event: tk.Event) -> None:
            if not self._measure_mode or len(self._measure_pts) != 1:
                return
            wx, wy = self._unproject(event.x, event.y)
            prev = self._measure_hover
            if prev is not None:
                w, h = self.winfo_width(), self.winfo_height()
                psx, psy = self._project(prev[0], prev[1], w, h)
                if abs(psx - event.x) < 0.5 and abs(psy - event.y) < 0.5:
                    return
            self._measure_hover = (wx, wy)
            self._schedule_redraw()
            if self._on_measure_change:
                self._on_measure_change()

        # ---- Measurement public API (mirrors BoardCanvasCPU) ----

        @property
        def measure_mode(self) -> bool:
            return self._measure_mode

        def set_measure_mode(self, on: bool) -> None:
            if self._measure_mode == on:
                return
            self._measure_mode = on
            self._measure_pts = []
            self._measure_hover = None
            self.config(cursor="crosshair" if on else "")
            self._schedule_redraw()
            if self._on_measure_change:
                self._on_measure_change()

        def clear_measurement(self) -> None:
            if not self._measure_pts and not self._measure_hover:
                return
            self._measure_pts = []
            self._measure_hover = None
            self._schedule_redraw()
            if self._on_measure_change:
                self._on_measure_change()

        def set_measure_change_callback(
            self, cb: Optional[Callable[[], None]],
        ) -> None:
            self._on_measure_change = cb

        def measurement_distance_units(self) -> Optional[float]:
            if len(self._measure_pts) < 2:
                return None
            (x1, y1), (x2, y2) = self._measure_pts[0], self._measure_pts[1]
            return ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5

        def measurement_distance_preview_units(self) -> Optional[float]:
            if len(self._measure_pts) != 1 or self._measure_hover is None:
                return None
            (x1, y1) = self._measure_pts[0]
            x2, y2 = self._measure_hover
            return ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5

        def units_per_mm(self) -> float:
            cached = getattr(self, "_units_per_mm_cache", None)
            if cached is not None:
                return cached
            xs = [c.x for c in self.board.components.values()]
            ys = [c.y for c in self.board.components.values()]
            if not xs:
                scale = 39.37
            else:
                span = max(max(xs) - min(xs), max(ys) - min(ys))
                scale = 3937.0 if span > 50_000 else 39.37
            self._units_per_mm_cache = scale
            return scale

        def _format_distance(self, d_units: float) -> str:
            upm = self.units_per_mm()
            mm = d_units / upm
            mil = mm * 39.3701
            if mm >= 1.0:
                return f"{mm:.3f} mm  ({mil:.1f} mil)"
            return f"{mm * 1000:.1f} um  ({mil:.2f} mil)"

        def _find_pin_at(
            self, comp: Component, shape: Any, cx: int, cy: int,
        ) -> Optional[str]:
            w, h = self.winfo_width(), self.winfo_height()
            theta = math.radians(comp.rotation)
            ct, st = math.cos(theta), math.sin(theta)
            best_pin: Optional[str] = None
            best_dist = self.PIN_CLICK_RADIUS_PX
            for pin_name, dx, dy in shape.pins:
                wx = comp.x + dx * ct - dy * st
                wy = comp.y + dx * st + dy * ct
                sx, sy = self._project(wx, wy, w, h)
                if (abs(sx - cx) > self.PIN_CLICK_RADIUS_PX
                        or abs(sy - cy) > self.PIN_CLICK_RADIUS_PX):
                    continue
                d = ((sx - cx) ** 2 + (sy - cy) ** 2) ** 0.5
                if d < best_dist:
                    best_dist = d
                    best_pin = pin_name
            return best_pin

        def _find_component_at(self, cx: int, cy: int) -> Optional[str]:
            # See BoardCanvasCPU._find_component_at for the rationale on
            # the pin-density weighting — same fix applies here.
            w, h = self.winfo_width(), self.winfo_height()
            candidates = [c for c in self.board.components.values()
                          if c.layer == self._view_layer]
            best_refdes = None
            best_score = float("inf")
            for c in candidates:
                poly = self._component_polygon_screen(c, w, h)
                if poly and self._point_in_poly(cx, cy, poly):
                    area = self._poly_area(poly)
                    shape = self.board.shapes.get(c.shape)
                    n_pins = len(shape.pins) if shape else 0
                    if n_pins >= 8:
                        factor = 1.0
                    else:
                        factor = 8.0 / max(1, n_pins)
                    score = area * factor
                    if score < best_score:
                        best_score = score
                        best_refdes = c.refdes
            if best_refdes:
                return best_refdes
            best_dist = self.CLICK_RADIUS_PX
            for c in candidates:
                sx, sy = self._project(c.x, c.y, w, h)
                d = ((sx - cx) ** 2 + (sy - cy) ** 2) ** 0.5
                if d < best_dist:
                    best_dist = d
                    best_refdes = c.refdes
            return best_refdes

        @staticmethod
        def _point_in_poly(
            px: float, py: float, poly: List[Tuple[float, float]],
        ) -> bool:
            n = len(poly)
            inside = False
            j = n - 1
            for i in range(n):
                xi, yi = poly[i]
                xj, yj = poly[j]
                if ((yi > py) != (yj > py)) and \
                        (px < (xj - xi) * (py - yi) / (yj - yi + 1e-9) + xi):
                    inside = not inside
                j = i
            return inside

        @staticmethod
        def _poly_area(poly: List[Tuple[float, float]]) -> float:
            n = len(poly)
            total = 0.0
            for i in range(n):
                x1, y1 = poly[i]
                x2, y2 = poly[(i + 1) % n]
                total += x1 * y2 - x2 * y1
            return abs(total) / 2

else:
    # Stub so callers can `BoardCanvasGL` reference cleanly even when
    # the GL stack is absent. The factory will skip this branch.
    BoardCanvasGL = None  # type: ignore[assignment,misc]


# ----- Render-tier factory ------------------------------------------------

if _GL_AVAILABLE:
    class _GLProbeFrame(_OpenGLFrame):  # type: ignore[misc,valid-type]
        """Minimal OpenGLFrame subclass used only by `_probe_gl_canvas`.
        Doesn't actually draw anything — just lets pyopengltk run its
        Map → CreateContext → initgl flow so we can confirm the GL
        stack is alive and Skia can build a GrDirectContext on top."""

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.animate = 0
            self.probe_initgl_ok = False
            self.probe_initgl_err: Optional[Exception] = None

        def initgl(self):
            try:
                _GL.glViewport(0, 0, max(self.winfo_width(), 1),
                               max(self.winfo_height(), 1))
                _GL.glClearColor(0.0, 0.0, 0.0, 1.0)
                self.probe_initgl_ok = True
            except Exception as e:  # pragma: no cover
                self.probe_initgl_err = e

        def redraw(self):
            # Stub — we don't paint anything in the probe.
            return
else:
    _GLProbeFrame = None  # type: ignore[assignment,misc]


def _probe_gl_canvas(verbose: bool = False) -> bool:
    """Try to construct an OpenGLFrame + Skia GrDirectContext on a
    hidden Toplevel. Returns True iff the GL stack is fully usable on
    this box. Always destroys the probe widget before returning.

    Run once at app startup, before the main UI is built. Failure
    here just downgrades to the CPU canvas — never raises.

    The GL widget needs to be MAPPED on screen for tkMap to fire and
    tkCreateContext to run. We use overrideredirect + off-screen
    geometry so the probe never appears to the user.
    """
    if not _GL_AVAILABLE or BoardCanvasGL is None or _GLProbeFrame is None:
        return False
    probe_root: Optional[tk.Toplevel] = None
    try:
        probe_root = tk.Toplevel()
        probe_root.overrideredirect(True)  # no titlebar, no decorations
        probe_root.geometry("16x16-200-200")  # off-screen
        frame = _GLProbeFrame(probe_root, width=16, height=16)
        frame.pack()
        probe_root.update_idletasks()
        # Pump events until the widget is mapped (Map → CreateContext
        # → initgl). Bounded poll so a stuck event-loop doesn't hang.
        deadline = time.time() + 1.5
        while not frame.context_created and time.time() < deadline:
            probe_root.update()
        if not frame.context_created or not frame.probe_initgl_ok:
            if verbose:
                print(f"probe: context_created={frame.context_created} "
                      f"initgl_ok={frame.probe_initgl_ok} "
                      f"err={frame.probe_initgl_err}")
            return False
        frame.tkMakeCurrent()
        grctx = _skia.GrDirectContext.MakeGL()
        ok = grctx is not None
        if not ok and verbose:
            print("probe: MakeGL returned None")
        if ok:
            info = _skia.ImageInfo.Make(
                16, 16, _skia.kRGBA_8888_ColorType,
                _skia.kPremul_AlphaType,
            )
            surf = _skia.Surface.MakeRenderTarget(
                grctx, _skia.Budgeted.kNo, info,
            )
            ok = surf is not None
            if not ok and verbose:
                print("probe: MakeRenderTarget returned None")
        return ok
    except Exception:
        if verbose:
            traceback.print_exc()
        return False
    finally:
        if probe_root is not None:
            try:
                probe_root.destroy()
            except Exception:
                pass


_GL_PROBE_RESULT: Optional[bool] = None


def _gl_probe_cached() -> bool:
    global _GL_PROBE_RESULT
    if _GL_PROBE_RESULT is None:
        _GL_PROBE_RESULT = _probe_gl_canvas()
    return _GL_PROBE_RESULT


def make_board_canvas(parent: tk.Misc, board: BoardModel, **kw):
    """Pick the best available rendering backend at startup.

    Tier 1: Skia GL (pyopengltk OpenGLFrame). Probed by trying to
            create one and a GrDirectContext on a hidden Toplevel
            during the first call, with the result cached.
    Tier 2: Skia CPU + PPM (BoardCanvasCPU).
    Tier 3: tk.create_line fallback (BoardCanvasCPU when skia missing).

    Set env var WALKER_FORCE_CPU=1 to skip Tier 1 entirely (useful when
    debugging GL renderer issues).

    Returns a BoardCanvas-shaped widget either way. The widget reports
    its tier via .render_tier (str: 'gl' or 'cpu') for diagnostic /
    status-bar use.
    """
    force_cpu = os.environ.get("WALKER_FORCE_CPU", "").strip() in ("1", "true", "yes")
    if not force_cpu and _gl_probe_cached():
        try:
            widget = BoardCanvasGL(parent, board, **kw)  # type: ignore[misc]
            return widget
        except Exception:
            traceback.print_exc()
            # Fall through to CPU.
    cpu = BoardCanvasCPU(parent, board, **kw)
    cpu.render_tier = "cpu"  # type: ignore[attr-defined]
    return cpu


# ----- Step list ----------------------------------------------------------

class StepList(ttk.Frame):
    STATUS_CHARS = {"pass": "✓", "fail": "✗", "skip": "⊘"}

    def __init__(self, parent: tk.Misc, on_jump: Callable[[int], None]):
        super().__init__(parent)
        self.on_jump = on_jump
        cols = ("stage", "signal", "v", "status")
        self.tree = ttk.Treeview(self, columns=cols, show="tree headings")
        self.tree.heading("#0", text="#")
        self.tree.heading("stage", text="Stage")
        self.tree.heading("signal", text="Signal")
        self.tree.heading("v", text="V")
        self.tree.heading("status", text="✓✗")
        self.tree.column("#0", width=44, stretch=False, anchor="e")
        self.tree.column("stage", width=160, stretch=True)
        self.tree.column("signal", width=200, stretch=True)
        self.tree.column("v", width=80, stretch=False)
        self.tree.column("status", width=44, stretch=False, anchor="center")
        sb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("current",
                                background="#fff4b2", font=("Segoe UI", 9, "bold"))
        self.tree.tag_configure("pass", foreground="#1a8a1a")
        self.tree.tag_configure("fail", foreground="#cc2a2a")
        self.tree.tag_configure("skip", foreground="#888")
        self.tree.bind("<Button-1>", self._on_click)

    def populate(self, steps: List[Step]) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, step in enumerate(steps):
            stage = (step.stage_label or "")[:32]
            sig = step.raw or step.note or step.step_text or ""
            v = step.expected_voltage or ""
            self.tree.insert("", "end", iid=str(i), text=str(i + 1),
                             values=(stage, sig[:60], v, ""))

    def refresh_status(
        self, steps: List[Step], results: Dict[int, str], current_idx: int,
    ) -> None:
        for i in range(len(steps)):
            iid = str(i)
            r = results.get(i)
            status = self.STATUS_CHARS.get(r, "")
            vals = list(self.tree.item(iid, "values"))
            if len(vals) >= 4 and vals[3] != status:
                vals[3] = status
                self.tree.item(iid, values=vals)
            tags: List[str] = []
            if i == current_idx:
                tags.append("current")
            if r in ("pass", "fail", "skip"):
                tags.append(r)
            self.tree.item(iid, tags=tags)
        try:
            self.tree.see(str(current_idx))
        except tk.TclError:
            pass

    def _on_click(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        if not item:
            return
        try:
            idx = int(item)
        except ValueError:
            return
        self.after_idle(self.on_jump, idx)


# ----- Autocomplete entry widget ------------------------------------------

class AutocompleteEntry(ttk.Frame):
    """An Entry widget with a dropdown listbox of matching suggestions.

    Suggestions update on each keystroke. The dropdown floats just below
    the entry (uses an overrideredirect Toplevel). Down-arrow moves focus
    into the listbox; Return submits whatever the entry currently shows;
    clicking or pressing Return on a listbox item submits that item.

    Both `get_candidates(query)` and `on_submit(value)` are caller-supplied:
        - `get_candidates`: takes the current entry text, returns a list
          of strings to show in the dropdown (caller decides ranking and
          truncation).
        - `on_submit`: receives the chosen string when the user commits.
    """

    POPUP_HEIGHT_PX = 180

    def __init__(
        self,
        parent: tk.Misc,
        *,
        get_candidates: Callable[[str], List[str]],
        on_submit: Callable[[str], None],
        width: int = 14,
        placeholder: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self._get_candidates = get_candidates
        self._on_submit = on_submit
        self._placeholder = placeholder
        self._popup: Optional[tk.Toplevel] = None
        self._listbox: Optional[tk.Listbox] = None
        # Internal flag: set True when we're filling entry from a listbox
        # selection so the resulting KeyRelease/<Return> doesn't re-trigger.
        self._suppress_update = False

        self.entry = ttk.Entry(self, width=width)
        self.entry.pack(fill="x")
        self.entry.bind("<KeyRelease>", self._on_key_release)
        self.entry.bind("<Return>", self._on_return)
        self.entry.bind("<Down>", self._on_down)
        self.entry.bind("<Escape>", self._on_escape)
        self.entry.bind("<FocusOut>", self._on_focus_out)
        if placeholder:
            self._show_placeholder()
            self.entry.bind("<FocusIn>", self._on_focus_in)

    # ---- public passthrough -------------------------------------------------

    def get(self) -> str:
        v = self.entry.get()
        if self._placeholder and v == self._placeholder:
            return ""
        return v

    def set_text(self, text: str) -> None:
        self.entry.delete(0, "end")
        self.entry.insert(0, text)

    def clear(self) -> None:
        self.entry.delete(0, "end")
        if self._placeholder:
            self._show_placeholder()

    # ---- placeholder mgmt ---------------------------------------------------

    def _show_placeholder(self) -> None:
        if not self._placeholder:
            return
        self.entry.delete(0, "end")
        self.entry.insert(0, self._placeholder)
        self.entry.config(foreground="#888")

    def _on_focus_in(self, _evt: tk.Event) -> None:
        if self._placeholder and self.entry.get() == self._placeholder:
            self.entry.delete(0, "end")
            self.entry.config(foreground="")

    # ---- typing → popup -----------------------------------------------------

    def _on_key_release(self, event: tk.Event) -> None:
        if self._suppress_update:
            return
        # Navigation keys are handled elsewhere.
        if event.keysym in ("Return", "Up", "Down", "Escape", "Tab"):
            return
        self._refresh_popup()

    def _refresh_popup(self) -> None:
        query = self.get().strip()
        if not query:
            self._hide_popup()
            return
        try:
            candidates = self._get_candidates(query)
        except Exception:
            candidates = []
        if not candidates:
            self._hide_popup()
            return
        self._show_popup(candidates)

    def _show_popup(self, candidates: List[str]) -> None:
        if self._popup is None or not self._popup.winfo_exists():
            self._popup = tk.Toplevel(self)
            self._popup.wm_overrideredirect(True)
            self._popup.attributes("-topmost", True)
            self._listbox = tk.Listbox(
                self._popup, height=10, activestyle="dotbox",
                exportselection=False,
            )
            self._listbox.pack(fill="both", expand=True)
            self._listbox.bind("<Button-1>", self._on_listbox_click)
            self._listbox.bind("<Double-Button-1>", self._on_listbox_click)
            self._listbox.bind("<Return>", self._on_listbox_return)
            self._listbox.bind("<Escape>", self._on_escape)
        assert self._listbox is not None
        self._listbox.delete(0, "end")
        for c in candidates:
            self._listbox.insert("end", c)
        # Position just below the entry, matching its width.
        self.entry.update_idletasks()
        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() + self.entry.winfo_height()
        w = max(self.entry.winfo_width(), 200)
        self._popup.geometry(f"{w}x{self.POPUP_HEIGHT_PX}+{x}+{y}")
        self._popup.deiconify()

    def _hide_popup(self) -> None:
        if self._popup is not None and self._popup.winfo_exists():
            self._popup.withdraw()

    # ---- key handlers -------------------------------------------------------

    def _on_focus_out(self, _evt: tk.Event) -> None:
        # Delay so we don't tear down the popup before a click on it
        # registers. _maybe_hide checks the new focus.
        self.after(150, self._maybe_hide)
        if self._placeholder and not self.entry.get():
            self._show_placeholder()

    def _maybe_hide(self) -> None:
        try:
            cur = self.focus_get()
        except Exception:
            cur = None
        if cur is self.entry or cur is self._listbox:
            return
        self._hide_popup()

    def _on_down(self, _evt: tk.Event) -> str:
        if self._listbox is not None and self._listbox.size() > 0:
            self._listbox.focus_set()
            self._listbox.selection_clear(0, "end")
            self._listbox.selection_set(0)
            self._listbox.activate(0)
        return "break"

    def _on_escape(self, _evt: tk.Event) -> str:
        self._hide_popup()
        self.entry.focus_set()
        return "break"

    def _on_return(self, _evt: tk.Event) -> str:
        # If a listbox row is highlighted, prefer it.
        chosen: Optional[str] = None
        if self._listbox is not None and self._listbox.size() > 0:
            sel = self._listbox.curselection()
            if sel:
                chosen = self._listbox.get(sel[0])
        if chosen is None:
            chosen = self.get().strip()
        if not chosen:
            return "break"
        self._suppress_update = True
        self.set_text(chosen)
        self._suppress_update = False
        self._hide_popup()
        try:
            self._on_submit(chosen)
        finally:
            pass
        return "break"

    def _on_listbox_click(self, event: tk.Event) -> None:
        if self._listbox is None:
            return
        idx = self._listbox.nearest(event.y)
        if idx < 0:
            return
        chosen = self._listbox.get(idx)
        self._suppress_update = True
        self.set_text(chosen)
        self._suppress_update = False
        self._hide_popup()
        self._on_submit(chosen)

    def _on_listbox_return(self, _evt: tk.Event) -> str:
        if self._listbox is None:
            return "break"
        sel = self._listbox.curselection()
        if not sel:
            return "break"
        chosen = self._listbox.get(sel[0])
        self._suppress_update = True
        self.set_text(chosen)
        self._suppress_update = False
        self._hide_popup()
        self._on_submit(chosen)
        return "break"


# ----- Component info panel -----------------------------------------------

class ComponentInfoPanel(ttk.Frame):
    def __init__(
        self, parent: tk.Misc, board: BoardModel,
        on_pin_select: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(parent, padding=6)
        self.board = board
        self.on_pin_select = on_pin_select
        self.current_refdes: Optional[str] = None

        self.header_txt = tk.Text(
            self, height=8, font=("Consolas", 9), wrap="none",
            relief="flat", background="#f6f6f9",
        )
        self.header_txt.pack(fill="x", padx=2, pady=(2, 4))
        self.header_txt.config(state="disabled")
        self.header_txt.tag_configure("h1", font=("Segoe UI", 10, "bold"),
                                      foreground="#222")
        self.header_txt.tag_configure("dim", foreground="#666")
        self.header_txt.tag_configure("placeholder", foreground="#888",
                                      font=("Segoe UI", 10, "italic"))

        self.pins_lbl = ttk.Label(self, text="",
                                  font=("Segoe UI", 9, "bold"))
        self.pins_lbl.pack(anchor="w", padx=2, pady=(4, 2))

        pins_frame = ttk.Frame(self)
        pins_frame.pack(fill="both", expand=True, padx=2, pady=(0, 2))
        self.pins_tree = ttk.Treeview(
            pins_frame, columns=("net",), show="tree headings", height=10,
        )
        self.pins_tree.heading("#0", text="Pin")
        self.pins_tree.heading("net", text="Net")
        self.pins_tree.column("#0", width=80, stretch=False, anchor="w")
        self.pins_tree.column("net", width=240, stretch=True)
        sb = ttk.Scrollbar(pins_frame, orient="vertical",
                           command=self.pins_tree.yview)
        self.pins_tree.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.pins_tree.pack(side="left", fill="both", expand=True)
        self.pins_tree.tag_configure("selected_pin",
                                     background="#ff7b9c",
                                     foreground="#ffffff",
                                     font=("Consolas", 9, "bold"))
        self.pins_tree.bind("<Button-1>", self._on_pin_click)

        self.show_placeholder()

    def set_board(self, board: BoardModel) -> None:
        self.board = board
        self.show_placeholder()

    def show_placeholder(self) -> None:
        self.current_refdes = None
        self._set_header("(click any IC on the board view to see its details)\n",
                         tag="placeholder")
        self.pins_tree.delete(*self.pins_tree.get_children())
        self.pins_lbl.config(text="")

    def show_component(self, refdes: str) -> None:
        comp = self.board.components.get(refdes)
        if comp is None:
            self.show_placeholder()
            return
        self.current_refdes = refdes
        shape = self.board.shapes.get(comp.shape)

        pins_on_comp: List[Tuple[str, str]] = []
        for net, nodes in self.board.signals.items():
            for r, p in nodes:
                if r == refdes:
                    pins_on_comp.append((p, net))

        # If we have no signals data (e.g. TVW partial-parse), fall back
        # to listing pins straight from the shape so the user can still
        # click on a pin and locate it on the canvas.
        net_known = bool(pins_on_comp)
        if not net_known and shape and shape.pins:
            pins_on_comp = [(p[0], "—") for p in shape.pins]

        self.header_txt.config(state="normal")
        self.header_txt.delete("1.0", "end")
        self.header_txt.insert("end", f"{refdes}\n", "h1")
        self.header_txt.insert(
            "end",
            f"  layer:    {comp.layer}\n"
            f"  position: ({comp.x:.1f}, {comp.y:.1f})\n"
            f"  rotation: {comp.rotation:g}°\n"
            f"  shape:    {comp.shape}\n"
            f"  device:   {comp.device}\n",
            "dim",
        )
        if shape:
            x0, y0, x1, y1 = shape.bbox()
            self.header_txt.insert(
                "end",
                f"  size:     {x1 - x0:.1f} × {y1 - y0:.1f} (mil, from pin bbox)\n"
                f"  pins:     {len(shape.pins)} defined in shape\n",
                "dim",
            )
        self.header_txt.config(state="disabled")

        self.pins_tree.delete(*self.pins_tree.get_children())
        for pin, net in sorted(pins_on_comp, key=_pin_sort_key):
            iid = pin
            try:
                self.pins_tree.insert("", "end", iid=iid, text=pin, values=(net,))
            except tk.TclError:
                self.pins_tree.insert(
                    "", "end", iid=f"{pin}__{len(self.pins_tree.get_children())}",
                    text=pin, values=(net,),
                )

        if net_known:
            note = "click a row → focus pin on canvas"
        else:
            note = "no pin↔net mapping in this format — net column is blank"
        self.pins_lbl.config(
            text=f"Pins ({len(pins_on_comp)})  {note}"
        )

    def highlight_pin(self, pin_name: Optional[str]) -> None:
        for iid in self.pins_tree.get_children():
            tags = list(self.pins_tree.item(iid, "tags"))
            if "selected_pin" in tags:
                tags.remove("selected_pin")
                self.pins_tree.item(iid, tags=tags)
        if pin_name:
            target_iid = pin_name
            if not self.pins_tree.exists(target_iid):
                for iid in self.pins_tree.get_children():
                    if self.pins_tree.item(iid, "text") == pin_name:
                        target_iid = iid
                        break
                else:
                    return
            self.pins_tree.item(target_iid, tags=("selected_pin",))
            try:
                self.pins_tree.see(target_iid)
                self.pins_tree.selection_set(target_iid)
            except tk.TclError:
                pass

    def _on_pin_click(self, event: tk.Event) -> None:
        item = self.pins_tree.identify_row(event.y)
        if not item:
            return
        pin = self.pins_tree.item(item, "text")
        if pin and self.on_pin_select:
            self.after_idle(self.on_pin_select, pin)

    def _set_header(self, text: str, tag: Optional[str] = None) -> None:
        self.header_txt.config(state="normal")
        self.header_txt.delete("1.0", "end")
        if tag:
            self.header_txt.insert("1.0", text, tag)
        else:
            self.header_txt.insert("1.0", text)
        self.header_txt.config(state="disabled")


# ----- Net info panel (Side quest) ----------------------------------------

class NetInfoPanel(ttk.Frame):
    """Shows every (refdes, pin) on a single net. Click a row to jump to
    that pin on the canvas (auto-flips layer if needed)."""

    def __init__(
        self, parent: tk.Misc, board: BoardModel,
        on_pin_jump: Optional[Callable[[str, str], None]] = None,
    ):
        super().__init__(parent, padding=6)
        self.board = board
        self.on_pin_jump = on_pin_jump
        self.current_net: Optional[str] = None

        self.lbl_net = ttk.Label(self, text="", font=("Segoe UI", 11, "bold"))
        self.lbl_net.pack(anchor="w", padx=2, pady=(2, 2))
        self.lbl_meta = ttk.Label(self, text="", font=("Segoe UI", 9),
                                  foreground="#555")
        self.lbl_meta.pack(anchor="w", padx=2, pady=(0, 6))

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True, padx=2, pady=(0, 2))
        cols = ("pin", "layer", "device", "shape")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="tree headings")
        self.tree.heading("#0", text="Component")
        self.tree.heading("pin", text="Pin")
        self.tree.heading("layer", text="L")
        self.tree.heading("device", text="Device")
        self.tree.heading("shape", text="Shape")
        self.tree.column("#0", width=80, stretch=False, anchor="w")
        self.tree.column("pin", width=60, stretch=False)
        self.tree.column("layer", width=30, stretch=False, anchor="center")
        self.tree.column("device", width=120, stretch=True)
        self.tree.column("shape", width=140, stretch=True)
        sb = ttk.Scrollbar(tree_frame, orient="vertical",
                           command=self.tree.yview)
        self.tree.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("highlight",
                                background="#ff7b9c", foreground="#ffffff",
                                font=("Consolas", 9, "bold"))
        self.tree.tag_configure("layer_top", foreground="#003a8c")
        self.tree.tag_configure("layer_bottom", foreground="#8a2a22")
        self.tree.bind("<Button-1>", self._on_click)
        self._highlighted_pin_iid: Optional[str] = None

        self.show_placeholder()

    def set_board(self, board: BoardModel) -> None:
        self.board = board
        self.show_placeholder()

    def show_placeholder(self) -> None:
        self.current_net = None
        self.lbl_net.config(text="(no net selected)")
        self.lbl_meta.config(text="Click a pin on the canvas or in the Component "
                                  "tab to fill this view with the rest of the net.")
        self.tree.delete(*self.tree.get_children())
        self._highlighted_pin_iid = None

    def show_net(
        self, net_name: Optional[str], focus_pin: Optional[Tuple[str, str]] = None
    ) -> None:
        self.tree.delete(*self.tree.get_children())
        self._highlighted_pin_iid = None
        if not net_name or net_name not in self.board.signals:
            self.show_placeholder()
            return
        nodes = self.board.signals[net_name]
        n_top = sum(1 for r, p in nodes
                    if (c := self.board.components.get(r)) and c.layer == "TOP")
        n_bot = sum(1 for r, p in nodes
                    if (c := self.board.components.get(r)) and c.layer == "BOTTOM")
        n_unknown = len(nodes) - n_top - n_bot
        unique_refs = len({r for r, p in nodes})
        self.current_net = net_name
        self.lbl_net.config(text=f"Net: {net_name}")
        meta = (f"{len(nodes)} pin(s) on {unique_refs} component(s) — "
                f"{n_top} top / {n_bot} bottom"
                + (f" / {n_unknown} unknown" if n_unknown else ""))
        self.lbl_meta.config(text=meta)

        sorted_nodes = sorted(
            nodes, key=lambda rp: (rp[0], _pin_sort_key((rp[1], "")))
        )
        for refdes, pin in sorted_nodes:
            comp = self.board.components.get(refdes)
            if not comp:
                continue
            iid = f"{refdes}__{pin}"
            tags = ("layer_top",) if comp.layer == "TOP" else ("layer_bottom",)
            try:
                self.tree.insert(
                    "", "end", iid=iid, text=refdes,
                    values=(pin, comp.layer, comp.device, comp.shape),
                    tags=tags,
                )
            except tk.TclError:
                pass

        if focus_pin:
            ref, pn = focus_pin
            target = f"{ref}__{pn}"
            if self.tree.exists(target):
                cur_tags = list(self.tree.item(target, "tags"))
                if "highlight" not in cur_tags:
                    cur_tags.append("highlight")
                    self.tree.item(target, tags=cur_tags)
                self._highlighted_pin_iid = target
                try:
                    self.tree.see(target)
                    self.tree.selection_set(target)
                except tk.TclError:
                    pass

    def _on_click(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        if not item:
            return
        refdes, _, pin = item.partition("__")
        if refdes and pin and self.on_pin_jump:
            self.after_idle(self.on_pin_jump, refdes, pin)


# ----- Schematic PDF panel -----------------------------------------------

class SchematicPanel(ttk.Frame):
    """Renders a PDF schematic alongside the boardview.

    Pages are rasterised on demand with PyMuPDF at the current zoom level
    and shown on a scrollable canvas. Toolbar controls: Open / page nav /
    zoom / fit-to-page. Mouse: wheel to scroll vertically, Ctrl+wheel to
    zoom, middle-button drag to pan.

    Degrades to a setup hint if PyMuPDF (`pip install pymupdf`) isn't
    installed — the rest of the walker keeps working.
    """

    PLACEHOLDER_HINT = (
        "No schematic loaded.\n\n"
        "Open a PDF via the toolbar, or load a board\n"
        "with a same-named PDF beside it (auto-detected)."
    )

    def __init__(self, parent: tk.Misc, **kw):
        super().__init__(parent, **kw)
        self.doc = None  # type: ignore[assignment]
        self.path: Optional[Path] = None
        self.page_idx = 0
        self.zoom = 1.0
        self._photo: Optional[tk.PhotoImage] = None  # GC anchor
        self._fit_pending = False  # one-shot fit on first render
        # Caller (WalkerApp) gets notified whenever a new PDF lands so
        # it can rebuild the schematic-signal match index. Default is
        # None — the panel works fine standalone (e.g. for the viewer).
        self._on_loaded: Optional[Callable[[Path], None]] = None

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(2, 4), padx=4)
        ttk.Button(bar, text="Open PDF…", command=self._on_open).pack(
            side="left", padx=(0, 8))
        ttk.Button(bar, text="◀", width=3, command=self.prev_page).pack(side="left")
        self.page_var = tk.StringVar(value="—")
        page_entry = ttk.Entry(bar, textvariable=self.page_var, width=5,
                               justify="center")
        page_entry.pack(side="left", padx=2)
        page_entry.bind("<Return>", self._on_page_entry)
        self.lbl_total = ttk.Label(bar, text=" / —")
        self.lbl_total.pack(side="left")
        ttk.Button(bar, text="▶", width=3, command=self.next_page).pack(
            side="left", padx=(6, 12))
        ttk.Button(bar, text="−", width=3,
                   command=lambda: self.zoom_by(1 / 1.25)).pack(side="left")
        self.lbl_zoom = ttk.Label(bar, text="—", width=6, anchor="center")
        self.lbl_zoom.pack(side="left")
        ttk.Button(bar, text="+", width=3,
                   command=lambda: self.zoom_by(1.25)).pack(side="left")
        ttk.Button(bar, text="Fit", command=self.fit_page).pack(
            side="left", padx=(8, 0))
        self.lbl_path = ttk.Label(bar, text="", font=("Segoe UI", 8),
                                   foreground="#666", anchor="e")
        self.lbl_path.pack(side="right", fill="x", expand=True, padx=(8, 0))

        cvs_frame = ttk.Frame(self)
        cvs_frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(cvs_frame, bg="#222", highlightthickness=0)
        sb_v = ttk.Scrollbar(cvs_frame, orient="vertical",
                             command=self.canvas.yview)
        sb_h = ttk.Scrollbar(cvs_frame, orient="horizontal",
                             command=self.canvas.xview)
        self.canvas.config(yscrollcommand=sb_v.set, xscrollcommand=sb_h.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        sb_v.grid(row=0, column=1, sticky="ns")
        sb_h.grid(row=1, column=0, sticky="ew")
        cvs_frame.rowconfigure(0, weight=1)
        cvs_frame.columnconfigure(0, weight=1)

        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Control-MouseWheel>", self._on_ctrl_wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_shift_wheel)
        # Middle-click drag pan
        self.canvas.bind("<ButtonPress-2>",
                         lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind("<B2-Motion>",
                         lambda e: self.canvas.scan_dragto(e.x, e.y, gain=1))
        # Configure once we have a real size
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        self._show_placeholder()

    # --- Public API ---

    def open(self, path: Path) -> bool:
        """Load a PDF. Returns True on success. Fires the load callback
        (if set) after a successful load so listeners can rebuild
        derived state (e.g. the schematic text/signal index)."""
        if not _HAS_FITZ:
            messagebox.showerror(
                "PyMuPDF missing",
                "Install PyMuPDF to view schematics:\n\n    pip install pymupdf",
            )
            return False
        try:
            self.doc = fitz.open(str(path))
        except Exception as exc:
            messagebox.showerror("Failed to open PDF", f"{path}\n\n{exc}")
            return False
        self.path = Path(path)
        self.page_idx = 0
        self.lbl_total.config(text=f" / {len(self.doc)}")
        self.lbl_path.config(text=self.path.name)
        # Defer fit until canvas has a real size
        self._fit_pending = True
        self._render()
        if self._on_loaded is not None:
            try:
                self._on_loaded(self.path)
            except Exception:
                # The callback is an enrichment, not a critical path —
                # never let it block the user from viewing the PDF.
                traceback.print_exc()
        return True

    def set_load_callback(self, cb: Optional[Callable[[Path], None]]) -> None:
        """Register a function called with the PDF path each time
        `open()` succeeds. None unregisters."""
        self._on_loaded = cb

    def jump_to_page(self, n: int) -> None:
        """1-based page number."""
        if not self.doc:
            return
        self.page_idx = max(0, min(int(n) - 1, len(self.doc) - 1))
        self._render()

    def prev_page(self) -> None:
        if self.doc and self.page_idx > 0:
            self.page_idx -= 1
            self._render()

    def next_page(self) -> None:
        if self.doc and self.page_idx < len(self.doc) - 1:
            self.page_idx += 1
            self._render()

    def zoom_by(self, factor: float) -> None:
        if not self.doc:
            return
        self.zoom = max(0.1, min(8.0, self.zoom * factor))
        self._render()

    def fit_page(self) -> None:
        if not self.doc:
            return
        self._do_fit()
        self._render()

    # --- Internals ---

    def _do_fit(self) -> None:
        page = self.doc[self.page_idx]
        cvs_w = max(self.canvas.winfo_width(), 200)
        cvs_h = max(self.canvas.winfo_height(), 200)
        page_w, page_h = page.rect.width, page.rect.height
        if page_w > 0 and page_h > 0:
            self.zoom = min(cvs_w / page_w, cvs_h / page_h) * 0.95

    def _render(self) -> None:
        if not self.doc or not _HAS_FITZ:
            return
        if self._fit_pending and self.canvas.winfo_width() > 50:
            self._do_fit()
            self._fit_pending = False
        try:
            page = self.doc[self.page_idx]
            mat = fitz.Matrix(self.zoom, self.zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            png = pix.tobytes("png")
            self._photo = tk.PhotoImage(data=png)
        except Exception as exc:
            self._show_error(f"Render failed on page {self.page_idx + 1}:\n{exc}")
            return
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)
        self.canvas.config(scrollregion=(0, 0, pix.width, pix.height))
        self.page_var.set(str(self.page_idx + 1))
        self.lbl_zoom.config(text=f"{int(self.zoom * 100)}%")

    def _on_canvas_configure(self, _evt) -> None:
        # Run fit once the canvas has a real size after layout
        if self._fit_pending and self.doc:
            self.after_idle(self._render)

    def _on_open(self) -> None:
        initial = (_last_dir("schematic")
                   or (str(self.path.parent) if self.path else "."))
        path = filedialog.askopenfilename(
            title="Open schematic PDF",
            filetypes=[("PDF schematic", "*.pdf"), ("All files", "*.*")],
            initialdir=initial,
        )
        if not path:
            return
        _remember_dir("schematic", Path(path))
        self.open(Path(path))

    def _on_page_entry(self, _evt) -> None:
        try:
            self.jump_to_page(int(self.page_var.get()))
        except ValueError:
            self.page_var.set(
                str(self.page_idx + 1) if self.doc else "—"
            )

    def _on_wheel(self, evt) -> None:
        self.canvas.yview_scroll(int(-evt.delta / 120), "units")

    def _on_shift_wheel(self, evt) -> None:
        self.canvas.xview_scroll(int(-evt.delta / 120), "units")

    def _on_ctrl_wheel(self, evt) -> None:
        self.zoom_by(1.1 if evt.delta > 0 else 1 / 1.1)

    def _show_placeholder(self) -> None:
        self.canvas.delete("all")
        self.canvas.create_text(
            220, 120, text=self.PLACEHOLDER_HINT, fill="#888",
            font=("Segoe UI", 10), justify="center", anchor="center",
        )
        self.canvas.config(scrollregion=(0, 0, 0, 0))
        self.page_var.set("—")
        self.lbl_total.config(text=" / —")
        self.lbl_zoom.config(text="—")
        self.lbl_path.config(text="(no PDF)")

    def _show_error(self, msg: str) -> None:
        self.canvas.delete("all")
        self.canvas.create_text(
            220, 120, text=msg, fill="#c66", font=("Segoe UI", 10),
            justify="center", anchor="center",
        )


# ----- Diagnosis helper ---------------------------------------------------

class DiagnosisHelper(ttk.LabelFrame):
    def __init__(self, parent: tk.Misc):
        super().__init__(parent, text="Diagnosis helper", padding=8)
        self.txt = tk.Text(self, height=8, font=("Consolas", 9), wrap="word",
                           relief="flat", background="#f4f4f7")
        self.txt.pack(fill="both", expand=True)
        self.txt.config(state="disabled")
        self._configure_tags()

    def _configure_tags(self) -> None:
        self.txt.tag_configure("h1", font=("Segoe UI", 10, "bold"),
                               foreground="#222", spacing3=4)
        self.txt.tag_configure("dim", foreground="#666")
        self.txt.tag_configure("pass", foreground="#1a8a1a")
        self.txt.tag_configure("fail", foreground="#cc2a2a",
                               font=("Consolas", 9, "bold"))
        self.txt.tag_configure("skip", foreground="#888")
        self.txt.tag_configure("warn", foreground="#cc2a2a",
                               font=("Segoe UI", 10, "bold"))
        self.txt.tag_configure("current", background="#fff8c8")
        self.txt.tag_configure("section", font=("Segoe UI", 9, "italic"),
                               foreground="#555")

    def update_for(
        self, step: Step, all_steps: List[Step],
        results: Dict[int, str], current_idx: int, board: BoardModel,
    ) -> None:
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.insert("end", f"Section: {step.section_id}\n", "h1")
        diag = step.section_diagnosis.strip()
        if diag:
            self.txt.insert("end", diag + "\n", "section")
        self.txt.insert("end", "\n")
        self.txt.insert("end", "Stage progress in section:\n", "h1")
        for label, n_total, n_pass, n_fail, n_skip, is_current in \
                self._stage_status(step.section_id, all_steps, results, current_idx):
            marker = "▶ " if is_current else "  "
            line = (f"{marker}{label[:32]:<34s}  "
                    f"{n_pass}✓ / {n_fail}✗ / {n_skip}⊘  "
                    f"({n_pass + n_fail + n_skip}/{n_total})\n")
            tag = "current" if is_current else (
                "fail" if n_fail else
                "pass" if n_pass == n_total and n_total > 0 else
                "dim"
            )
            self.txt.insert("end", line, tag)
        self.txt.insert("end", "\n")
        if results.get(current_idx) == "fail":
            self.txt.insert("end", "⚠ FAIL — investigate next\n", "warn")
            if step.probe_candidates:
                refdeses = sorted({p["refdes"] for p in step.probe_candidates})
                self.txt.insert(
                    "end",
                    f"Net spans {len(refdeses)} components: "
                    f"{', '.join(refdeses[:12])}"
                    + ("..." if len(refdeses) > 12 else "") + "\n",
                    "dim",
                )
            else:
                self.txt.insert(
                    "end",
                    "No boardview match; consult schematic or chipset datasheet "
                    "for the relevant pin.\n",
                    "dim",
                )
            unresolved = self._unresolved_upstream(step, all_steps, results, current_idx)
            if unresolved:
                self.txt.insert(
                    "end",
                    f"Verify these upstream rails first: "
                    f"{', '.join(unresolved[:8])}\n",
                    "dim",
                )
        self.txt.config(state="disabled")

    @staticmethod
    def _stage_status(
        section_id: str, all_steps: List[Step],
        results: Dict[int, str], current_idx: int,
    ) -> List[Tuple[str, int, int, int, int, bool]]:
        order: List[str] = []
        buckets: Dict[str, Dict[str, int]] = {}
        current_label = ""
        for i, s in enumerate(all_steps):
            if s.section_id != section_id:
                continue
            if s.stage_label not in buckets:
                buckets[s.stage_label] = {"total": 0, "pass": 0, "fail": 0, "skip": 0}
                order.append(s.stage_label)
            buckets[s.stage_label]["total"] += 1
            r = results.get(i)
            if r in buckets[s.stage_label]:
                buckets[s.stage_label][r] += 1
            if i == current_idx:
                current_label = s.stage_label
        return [
            (lbl, b["total"], b["pass"], b["fail"], b["skip"], lbl == current_label)
            for lbl, b in ((l, buckets[l]) for l in order)
        ]

    @staticmethod
    def _unresolved_upstream(
        step: Step, all_steps: List[Step],
        results: Dict[int, str], current_idx: int,
    ) -> List[str]:
        out: List[str] = []
        for i in range(current_idx):
            s = all_steps[i]
            if s.section_id != step.section_id:
                continue
            if s.note or s.step_text or not s.raw:
                continue
            if results.get(i) != "pass":
                out.append(s.raw)
        return out


# ----- Claude chat panel --------------------------------------------------

class ChatPanel(ttk.LabelFrame):
    """Multi-backend chat panel (Anthropic / OpenAI / Ollama). Streams on a
    worker thread; chunks post back to the Tk main loop via after_idle.
    Per-provider model + effort selections persist across sessions."""

    QUICK_ASKS = [
        ("Explain signal", "Explain the current signal: what's it for, "
                           "what's the expected value, and what typically "
                           "causes it to fail?"),
        ("Failure modes", "Given the current step, what are the most likely "
                          "failure modes? Be specific about which components "
                          "to suspect."),
        ("What to check next", "Given the recent results, what should I "
                                "probe next? Name specific components and pins."),
        ("Read this measurement", "If I'm seeing a measurement that doesn't "
                                   "match the expected value (I'll describe "
                                   "it next), help me interpret it."),
    ]

    def __init__(self, parent: tk.Misc, app: "WalkerApp"):
        super().__init__(parent, text="Chat", padding=6)
        self.app = app
        self._messages: List[Dict[str, Any]] = []
        self._cancel = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._streaming = False
        self._expanded = True

        self._backends: Dict[str, ChatBackend] = _build_backends()
        self._chat_settings: Dict[str, Any] = _load_chat_settings()
        self._provider_var = tk.StringVar(
            value=BACKEND_LABELS[self._chat_settings["provider"]]
        )
        self._model_var = tk.StringVar()
        self._effort_var = tk.StringVar(value=NO_EFFORT_LABEL)

        self._build_ui()
        self._reload_for_provider()
        self._refresh_title()

    def _build_ui(self) -> None:
        header = ttk.Frame(self)
        header.pack(fill="x", padx=2, pady=(0, 4))
        self.btn_collapse = ttk.Button(
            header, text="▲ Hide", width=10, command=self._toggle_collapsed,
        )
        self.btn_collapse.pack(side="left")

        ttk.Label(header, text="Provider:",
                  font=("Segoe UI", 9)).pack(side="left", padx=(12, 2))
        self.cb_provider = ttk.Combobox(
            header, textvariable=self._provider_var,
            values=[BACKEND_LABELS[p] for p in BACKEND_ORDER],
            width=14, state="readonly",
        )
        self.cb_provider.pack(side="left")
        self.cb_provider.bind("<<ComboboxSelected>>", self._on_provider_changed)

        ttk.Label(header, text="Model:",
                  font=("Segoe UI", 9)).pack(side="left", padx=(10, 2))
        self.cb_model = ttk.Combobox(
            header, textvariable=self._model_var,
            width=18, state="readonly",
        )
        self.cb_model.pack(side="left")
        self.cb_model.bind("<<ComboboxSelected>>", self._on_model_changed)

        self.btn_refresh = ttk.Button(
            header, text="↻", width=3, command=self._refresh_models,
        )
        self.btn_refresh.pack(side="left", padx=(2, 0))

        ttk.Label(header, text="Effort:",
                  font=("Segoe UI", 9)).pack(side="left", padx=(10, 2))
        self.cb_effort = ttk.Combobox(
            header, textvariable=self._effort_var,
            width=8, state="readonly",
        )
        self.cb_effort.pack(side="left")
        self.cb_effort.bind("<<ComboboxSelected>>", self._on_effort_changed)

        self.lbl_status = ttk.Label(header, text="", font=("Segoe UI", 9),
                                    foreground="#555")
        self.lbl_status.pack(side="left", padx=(12, 0))

        self.btn_clear = ttk.Button(header, text="Clear chat", width=11,
                                    command=self._clear_chat)
        self.btn_clear.pack(side="right")

        # Body: log + quick asks + input row
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)

        log_frame = ttk.Frame(self.body)
        log_frame.pack(fill="both", expand=True, padx=2, pady=(0, 4))
        self.log = tk.Text(
            log_frame, height=8, font=("Segoe UI", 10), wrap="word",
            relief="solid", borderwidth=1, background="#fafafd", padx=8, pady=6,
        )
        sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.config(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("user_label", font=("Segoe UI", 9, "bold"),
                               foreground="#1a4a8a", spacing1=8, spacing3=2)
        self.log.tag_configure("user", foreground="#1a4a8a", spacing3=4)
        self.log.tag_configure("assistant_label", font=("Segoe UI", 9, "bold"),
                               foreground="#1a8a3a", spacing1=8, spacing3=2)
        self.log.tag_configure("assistant", foreground="#101820", spacing3=4)
        self.log.tag_configure("thinking_label", font=("Segoe UI", 8, "italic"),
                               foreground="#888", spacing1=4, spacing3=2)
        self.log.tag_configure("thinking", font=("Segoe UI", 9, "italic"),
                               foreground="#888", spacing3=4)
        self.log.tag_configure("error", foreground="#cc2a2a",
                               font=("Segoe UI", 9, "bold"), spacing1=6, spacing3=4)
        self.log.tag_configure("info", foreground="#666",
                               font=("Segoe UI", 9, "italic"), spacing3=4)

        quick = ttk.Frame(self.body)
        quick.pack(fill="x", padx=2, pady=(0, 4))
        for label, prompt in self.QUICK_ASKS:
            b = ttk.Button(quick, text=label, width=20,
                           command=lambda p=prompt: self.send_message(p))
            b.pack(side="left", padx=(0, 4))

        input_row = ttk.Frame(self.body)
        input_row.pack(fill="x", padx=2, pady=(0, 2))
        self.input = tk.Text(input_row, height=3, font=("Segoe UI", 10), wrap="word",
                             relief="solid", borderwidth=1)
        self.input.pack(side="left", fill="both", expand=True)
        self.input.bind("<Control-Return>", lambda e: self._on_send_pressed())
        btns = ttk.Frame(input_row)
        btns.pack(side="right", padx=(6, 0))
        self.btn_send = ttk.Button(btns, text="Send (Ctrl+↵)", width=14,
                                   command=self._on_send_pressed)
        self.btn_send.pack(fill="x", pady=(0, 2))
        self.btn_cancel = ttk.Button(btns, text="Cancel", width=14,
                                     command=self._cancel_stream, state="disabled")
        self.btn_cancel.pack(fill="x")

    # ---- Provider / model / effort dispatch ----

    def _current_provider_id(self) -> str:
        return BACKEND_LABEL_TO_ID.get(
            self._provider_var.get(), BACKEND_ANTHROPIC,
        )

    def _current_backend(self) -> ChatBackend:
        return self._backends[self._current_provider_id()]

    def _current_model_id(self) -> str:
        backend = self._current_backend()
        for lbl, mid in backend.list_models():
            if lbl == self._model_var.get():
                return mid
        return ""

    def _current_effort(self) -> str:
        v = self._effort_var.get()
        return v if v and v != NO_EFFORT_LABEL else ""

    def _reload_for_provider(self) -> None:
        prov = self._current_provider_id()
        backend = self._backends[prov]
        models = backend.list_models()
        labels = [m[0] for m in models]
        if not labels:
            labels = ["(no models)"]
        self.cb_model.config(values=labels)
        # Restore saved model for this provider
        saved_model = (self._chat_settings.get("providers", {})
                        .get(prov, {}).get("model", ""))
        chosen_label = ""
        for lbl, mid in models:
            if mid == saved_model:
                chosen_label = lbl
                break
        if not chosen_label:
            chosen_label = labels[0]
        self._model_var.set(chosen_label)
        # Refresh button only meaningful for Ollama
        if prov == BACKEND_OLLAMA:
            self.btn_refresh.state(["!disabled"])
        else:
            self.btn_refresh.state(["disabled"])
        self._reload_effort_choices()
        self._refresh_title()
        self._update_status()

    def _reload_effort_choices(self) -> None:
        backend = self._current_backend()
        model_id = self._current_model_id()
        options = backend.supports_effort(model_id) if model_id else []
        if not options:
            self.cb_effort.config(values=[NO_EFFORT_LABEL])
            self._effort_var.set(NO_EFFORT_LABEL)
            self.cb_effort.state(["disabled"])
            return
        self.cb_effort.config(values=options)
        self.cb_effort.state(["!disabled", "readonly"])
        prov = self._current_provider_id()
        saved = (self._chat_settings.get("providers", {})
                  .get(prov, {}).get("effort", ""))
        if saved in options:
            self._effort_var.set(saved)
        else:
            default_effort = DEFAULT_EFFORT.get(model_id, options[-1])
            self._effort_var.set(
                default_effort if default_effort in options else options[-1]
            )

    def _save_provider_setting(self) -> None:
        prov = self._current_provider_id()
        self._chat_settings.setdefault("providers", {})[prov] = {
            "model": self._current_model_id(),
            "effort": self._current_effort(),
        }
        self._chat_settings["provider"] = prov
        _save_chat_settings(self._chat_settings)

    def _on_provider_changed(self, _e: Optional[tk.Event] = None) -> None:
        self._chat_settings["provider"] = self._current_provider_id()
        _save_chat_settings(self._chat_settings)
        self._reload_for_provider()

    def _on_model_changed(self, _e: Optional[tk.Event] = None) -> None:
        self._save_provider_setting()
        self._reload_effort_choices()
        self._refresh_title()
        self._update_status()

    def _on_effort_changed(self, _e: Optional[tk.Event] = None) -> None:
        self._save_provider_setting()
        self._refresh_title()
        self._update_status()

    def _refresh_models(self) -> None:
        backend = self._current_backend()
        backend.list_models(refresh=True)
        self._reload_for_provider()

    def _refresh_title(self) -> None:
        prov_label = self._provider_var.get()
        model_label = self._model_var.get()
        effort = self._current_effort()
        if effort:
            self.config(text=f"Chat — {prov_label} • {model_label} • {effort} effort")
        else:
            self.config(text=f"Chat — {prov_label} • {model_label}")

    def _update_status(self) -> None:
        backend = self._current_backend()
        ok, msg = backend.is_configured()
        if not ok:
            self.lbl_status.config(text=msg, foreground="#883300")
            self._set_inputs_enabled(False)
            return
        self._set_inputs_enabled(True)
        bits = [self._provider_var.get(), self._model_var.get()]
        effort = self._current_effort()
        if effort:
            bits.append(f"effort: {effort}")
        if self._current_provider_id() == BACKEND_OLLAMA:
            bits.append(f"@ {_get_ollama_base_url()}")
        self.lbl_status.config(
            text="ready  •  " + "  •  ".join(bits), foreground="#1a5a1a",
        )

    def reload_client(self) -> None:
        """Re-read settings (after the Settings dialog saves) and refresh
        UI state. Preserves chat history."""
        if self._streaming:
            return
        self._chat_settings = _load_chat_settings()
        self._reload_for_provider()

    # ---- UI helpers ----

    def _toggle_collapsed(self) -> None:
        if self._expanded:
            self.body.pack_forget()
            self.btn_collapse.config(text="▼ Show")
        else:
            self.body.pack(fill="both", expand=True)
            self.btn_collapse.config(text="▲ Hide")
        self._expanded = not self._expanded

    def _set_inputs_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.input.config(state=state)
        self.btn_send.config(state=state)

    def _append(self, text: str, tag: Optional[str] = None) -> None:
        self.log.config(state="normal")
        if tag:
            self.log.insert("end", text, tag)
        else:
            self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    def _append_info(self, text: str) -> None:
        self._append(text + "\n", "info")

    def _append_error(self, text: str) -> None:
        self._append(text + "\n", "error")

    def _clear_chat(self) -> None:
        if self._streaming:
            return
        self._messages.clear()
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")
        self._update_status()

    # ---- Sending ----

    def _on_send_pressed(self) -> None:
        text = self.input.get("1.0", "end").strip()
        if not text or self._streaming:
            return
        ok, _ = self._current_backend().is_configured()
        if not ok:
            return
        self.input.delete("1.0", "end")
        self.send_message(text)

    def send_message(self, user_text: str) -> None:
        if self._streaming:
            return
        ok, msg = self._current_backend().is_configured()
        if not ok:
            self._append_error(f"Cannot send: {msg}")
            return
        if not self._expanded:
            self._toggle_collapsed()
        context = self._build_context_block()
        full_user = f"{context}\n\n{user_text}" if context else user_text

        self._messages.append({"role": "user", "content": full_user})
        self._append("You\n", "user_label")
        self._append(user_text + "\n", "user")
        self._begin_stream()

    def _begin_stream(self) -> None:
        self._streaming = True
        self._cancel.clear()
        self._set_inputs_enabled(False)
        self.btn_cancel.config(state="normal")
        self.btn_clear.config(state="disabled")
        # Lock provider/model/effort/refresh during streaming
        for w in (self.cb_provider, self.cb_model, self.cb_effort, self.btn_refresh):
            w.state(["disabled"])
        self.lbl_status.config(text="streaming…", foreground="#555")
        self._worker = threading.Thread(target=self._stream_response, daemon=True)
        self._worker.start()

    def _stream_response(self) -> None:
        backend = self._current_backend()
        model_id = self._current_model_id()
        effort = self._current_effort()
        cb = {
            "on_thinking_start": lambda: self._post(self._begin_thinking_block),
            "on_thinking_chunk": lambda t: self._post(self._append_thinking_chunk, t),
            "on_text_start":     lambda: None,
            "on_text_chunk":     lambda t: self._post(self._append_text_chunk, t),
            "on_complete":       lambda usage: self._post(self._on_response_complete, usage),
            "cancel":            self._cancel,
        }
        try:
            self._post(self._begin_assistant_block)
            backend.stream(CHAT_SYSTEM_PROMPT, self._messages, model_id, effort, cb)
        except Exception as exc:
            self._post(self._on_error, self._format_exception(exc))
        finally:
            self._post(self._end_stream)

    def _format_exception(self, exc: Exception) -> str:
        prov = self._current_provider_id()
        # Anthropic-specific errors
        if _HAS_ANTHROPIC and isinstance(exc, getattr(anthropic, "AuthenticationError", ())):
            return "Authentication failed. Check your Anthropic API key (Settings…)."
        if _HAS_ANTHROPIC and isinstance(exc, getattr(anthropic, "RateLimitError", ())):
            return "Rate limited. Try again in a moment."
        if _HAS_ANTHROPIC and isinstance(exc, getattr(anthropic, "NotFoundError", ())):
            return (f"Model not found: {exc}. "
                    f"Is `{self._current_model_id()}` available to your key?")
        if _HAS_ANTHROPIC and isinstance(exc, getattr(anthropic, "APIConnectionError", ())):
            return "Network error reaching Anthropic. Check your connection."
        # OpenAI/Ollama errors
        if _HAS_OPENAI and isinstance(exc, getattr(openai, "AuthenticationError", ())):
            label = "OpenAI" if prov == BACKEND_OPENAI else "the OpenAI-compatible endpoint"
            return f"Authentication failed for {label}. Check your API key."
        if _HAS_OPENAI and isinstance(exc, getattr(openai, "RateLimitError", ())):
            return "Rate limited. Try again in a moment."
        if _HAS_OPENAI and isinstance(exc, getattr(openai, "NotFoundError", ())):
            return f"Model not found: {exc}. Is `{self._current_model_id()}` installed?"
        if _HAS_OPENAI and isinstance(exc, getattr(openai, "APIConnectionError", ())):
            if prov == BACKEND_OLLAMA:
                return (f"Could not reach Ollama at {_get_ollama_base_url()}. "
                        "Is the daemon running?  (`ollama serve`)")
            return "Network error. Check your connection."
        return f"Unexpected error: {exc}\n{traceback.format_exc()}"

    def _post(self, fn: Callable[..., None], *args: Any) -> None:
        try:
            self.app.after_idle(fn, *args)
        except RuntimeError:
            pass  # window closed

    # ---- Stream UI ----

    def _begin_assistant_block(self) -> None:
        self._append(f"{self._provider_var.get()}\n", "assistant_label")

    def _begin_thinking_block(self) -> None:
        self._append("(thinking)\n", "thinking_label")

    def _begin_text_block(self) -> None:
        # No-op separator; the assistant label is already present.
        pass

    def _append_thinking_chunk(self, text: str) -> None:
        if text:
            self._append(text, "thinking")

    def _append_text_chunk(self, text: str) -> None:
        if text:
            self._append(text, "assistant")

    def _on_response_complete(self, usage: Dict[str, Any]) -> None:
        if self._cancel.is_set():
            self._append("\n[response cancelled]\n", "info")
        else:
            self._append("\n", "assistant")
        # Persist assistant content for multi-turn (Anthropic preserves the
        # full block list incl. thinking; OpenAI/Ollama emits a single text
        # block synthesized by the backend).
        msgs = usage.get("messages")
        if msgs is not None:
            self._messages.append({"role": "assistant", "content": msgs})
        # Show usage / cache info briefly
        in_t = usage.get("input_tokens", 0) or 0
        out_t = usage.get("output_tokens", 0) or 0
        cache_read = usage.get("cache_read_input_tokens", 0) or 0
        cache_create = usage.get("cache_creation_input_tokens", 0) or 0
        bits = [f"in={in_t}", f"out={out_t}"]
        if cache_read or cache_create:
            bits.append(f"cache_read={cache_read} cache_write={cache_create}")
        self.lbl_status.config(
            text="ready  •  " + "  •  ".join(bits), foreground="#1a5a1a",
        )

    def _on_error(self, msg: str) -> None:
        self._append("\n", "assistant")
        self._append_error(msg)
        # Drop the last user turn so the user can retry without a duplicate
        if self._messages and self._messages[-1].get("role") == "user":
            self._messages.pop()

    def _end_stream(self) -> None:
        self._streaming = False
        ok, _ = self._current_backend().is_configured()
        self._set_inputs_enabled(ok)
        self.btn_cancel.config(state="disabled")
        self.btn_clear.config(state="normal")
        # Re-enable provider/model/effort/refresh selection (effort gating
        # depends on whether the current model supports it; refresh only
        # for Ollama).
        for w in (self.cb_provider, self.cb_model):
            w.state(["!disabled", "readonly"])
        if self._current_backend().supports_effort(self._current_model_id()):
            self.cb_effort.state(["!disabled", "readonly"])
        else:
            self.cb_effort.state(["disabled"])
        if self._current_provider_id() == BACKEND_OLLAMA:
            self.btn_refresh.state(["!disabled"])
        else:
            self.btn_refresh.state(["disabled"])

    def _cancel_stream(self) -> None:
        if self._streaming:
            self._cancel.set()
            self.lbl_status.config(text="cancelling…")

    # ---- Per-turn context ----

    def _build_context_block(self) -> str:
        app = self.app
        if not app.steps:
            return ""
        step = app.steps[app.idx]
        lines = ["[Walker context]"]
        lines.append(f"Platform: {app.linked.get('platform', '?')}")
        lines.append(f"Step {app.idx + 1} of {len(app.steps)} "
                     f"(stage: {step.stage_label or '?'})")

        if step.note:
            lines.append(f"This step is an inline NOTE: {step.note}")
        elif step.step_text:
            lines.append(f"This step is a procedural STEP: {step.step_text}")
        else:
            sig = step.raw or step.net or "?"
            lines.append(f"Signal: {sig}")
            if step.expected_voltage:
                lines.append(f"Expected voltage: {step.expected_voltage}")
            if step.resistance_to_ground:
                lines.append(f"Expected R-to-ground: {step.resistance_to_ground}")
            if step.semantic:
                lines.append(f"Semantic flag: {step.semantic}")
            if step.section_id:
                lines.append(f"Section: {step.section_id}")
            if step.section_diagnosis:
                lines.append(f"Section failure mode: {step.section_diagnosis}")

        if step.boardview_net:
            lines.append(f"Matched boardview net: {step.boardview_net} "
                         f"({len(step.probe_candidates)} probe pt(s))")
            for p in step.probe_candidates[:5]:
                lines.append(f"  - {p['refdes']} pin {p['pin']} on {p['layer']} "
                             f"({p['x']:.0f}, {p['y']:.0f}) device={p['device']}")
        else:
            lines.append("No boardview net match for this signal.")

        sel = app.canvas.selected_refdes
        if sel:
            comp = app.board.components.get(sel)
            if comp:
                lines.append(f"Selected on canvas: {sel} "
                             f"(layer={comp.layer}, device={comp.device}, "
                             f"shape={comp.shape}, rotation={comp.rotation:g}°)")
            sel_pin = app.canvas.selected_pin
            if sel_pin:
                net = app.net_for_pin(sel, sel_pin)
                lines.append(f"Selected pin: {sel} pin {sel_pin}"
                             + (f" → net {net}" if net else " (net not found)"))

        n_pass = sum(1 for r in app.results.values() if r == "pass")
        n_fail = sum(1 for r in app.results.values() if r == "fail")
        n_skip = sum(1 for r in app.results.values() if r == "skip")
        if n_pass or n_fail or n_skip:
            lines.append(f"Results so far: {n_pass}✓ / {n_fail}✗ / {n_skip}⊘")
            fails = [(i, app.steps[i].raw)
                     for i, r in app.results.items()
                     if r == "fail" and app.steps[i].raw]
            if fails:
                fails.sort(key=lambda x: x[0])
                fails_str = ", ".join(raw for _, raw in fails[-6:])
                lines.append(f"Recent fails: {fails_str}")

        lines.append("[End context]")
        return "\n".join(lines)

# ----- Settings dialog ----------------------------------------------------

class _ProviderKeyRow:
    """One provider's API key UI row — entry + show/clear + status line."""

    def __init__(
        self, parent: tk.Misc, provider_id: str, label: str, key_prefix_hint: str,
    ):
        self.provider_id = provider_id
        self.key_prefix_hint = key_prefix_hint
        self._show_visible = False

        ttk.Label(parent, text=label, font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=16, pady=(10, 2),
        )

        entry_row = ttk.Frame(parent)
        entry_row.pack(fill="x", padx=16, pady=2)
        self.key_var = tk.StringVar(value=_get_stored_api_key(provider_id))
        self.entry = ttk.Entry(
            entry_row, textvariable=self.key_var, show="•",
            font=("Consolas", 10),
        )
        self.entry.pack(side="left", fill="x", expand=True)
        self.btn_show = ttk.Button(entry_row, text="Show", width=7,
                                   command=self._toggle_show)
        self.btn_show.pack(side="left", padx=(6, 0))
        ttk.Button(entry_row, text="Clear", width=7,
                   command=lambda: self.key_var.set("")).pack(side="left", padx=(4, 0))

        self.lbl_status = ttk.Label(parent, text="", font=("Segoe UI", 9),
                                    foreground="#444", justify="left")
        self.lbl_status.pack(anchor="w", padx=16, pady=(2, 0))
        self._refresh_status()

        if not _HAS_KEYRING:
            self.entry.config(state="disabled")
            self.btn_show.config(state="disabled")

    def _toggle_show(self) -> None:
        self._show_visible = not self._show_visible
        self.entry.config(show="" if self._show_visible else "•")
        self.btn_show.config(text="Hide" if self._show_visible else "Show")

    def _refresh_status(self) -> None:
        stored = _get_stored_api_key(self.provider_id)
        env_var = _PROVIDER_ENV_VARS.get(self.provider_id, "")
        env_key = (os.environ.get(env_var) or "").strip() if env_var else ""
        env_summary = (f"set, {self.key_prefix_hint}{_key_tail(env_key)}"
                       if env_key else "not set")
        if stored:
            self.lbl_status.config(text=(
                f"Active: stored key ({self.key_prefix_hint}{_key_tail(stored)})  "
                f"•  fallback: {env_var or 'n/a'} ({env_summary})"
            ))
        else:
            self.lbl_status.config(text=(
                f"Active: {env_var or 'env var'} ({env_summary})  "
                f"•  save a key to override"
            ))

    def save(self) -> None:
        """Persist this row's key to the keyring. Raises RuntimeError on
        keyring failure (caller catches and shows a messagebox)."""
        key = self.key_var.get().strip()
        if key and self.key_prefix_hint and not key.startswith(self.key_prefix_hint):
            ok = messagebox.askyesno(
                "Unusual key format",
                f"{self.provider_id.title()} keys typically start with "
                f"'{self.key_prefix_hint}'. Save anyway?",
                parent=self.entry.winfo_toplevel(),
            )
            if not ok:
                raise _SaveCancelled()
        _save_stored_api_key(key, self.provider_id)


class _SaveCancelled(Exception):
    pass


class SettingsDialog(tk.Toplevel):
    """Modal Settings dialog. Holds API keys for each provider plus the
    Ollama base URL. Anthropic + OpenAI keys live in the OS keyring; the
    Ollama base URL lives in walker_config.json (it isn't a secret)."""

    def __init__(self, parent: tk.Misc, on_saved: Callable[[], None]):
        super().__init__(parent)
        self.title("Settings")
        self.transient(parent)
        self.grab_set()
        self.geometry("640x600")
        self.resizable(False, False)
        self.on_saved = on_saved
        self._build_ui()

    def _build_ui(self) -> None:
        # Header banner
        ttk.Label(self, text="Provider settings",
                  font=("Segoe UI", 12, "bold")).pack(
            anchor="w", padx=16, pady=(14, 2),
        )
        ttk.Label(
            self,
            text=("Paste a key to override the corresponding env var, or leave "
                  "empty to fall back to it.\n"
                  "Local providers (Ollama) need only a base URL."),
            font=("Segoe UI", 9), foreground="#444", justify="left",
        ).pack(anchor="w", padx=16, pady=(0, 4))

        # Keyring availability banner
        if _HAS_KEYRING:
            backend = _keyring_backend_label() or "OS keyring"
            banner = ttk.Label(
                self,
                text=f"🔒  Keys stored in {backend} (encrypted, locked to "
                     "your OS user).",
                font=("Segoe UI", 9), foreground="#1a5a1a",
            )
            banner.pack(anchor="w", padx=16, pady=(0, 4))
        else:
            ttk.Label(
                self,
                text=("⚠  keyring not installed — in-app key storage disabled.\n"
                      "    pip install keyring  →  restart the walker."),
                font=("Segoe UI", 9), foreground="#883300", justify="left",
            ).pack(anchor="w", padx=16, pady=(0, 4))

        ttk.Separator(self).pack(fill="x", padx=12, pady=(4, 0))

        # Anthropic key row
        self.anthropic_row = _ProviderKeyRow(
            self, BACKEND_ANTHROPIC, "Anthropic API key", "sk-ant-",
        )
        # OpenAI key row
        self.openai_row = _ProviderKeyRow(
            self, BACKEND_OPENAI, "OpenAI API key", "sk-",
        )

        ttk.Separator(self).pack(fill="x", padx=12, pady=(10, 0))

        # Ollama base URL
        ttk.Label(self, text="Ollama base URL  (local)",
                  font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=16, pady=(10, 2),
        )
        ollama_row = ttk.Frame(self)
        ollama_row.pack(fill="x", padx=16, pady=2)
        self.ollama_url_var = tk.StringVar(value=_get_ollama_base_url())
        self.ollama_entry = ttk.Entry(
            ollama_row, textvariable=self.ollama_url_var, font=("Consolas", 10),
        )
        self.ollama_entry.pack(side="left", fill="x", expand=True)
        ttk.Button(
            ollama_row, text="Reset", width=8,
            command=lambda: self.ollama_url_var.set(OLLAMA_DEFAULT_BASE_URL),
        ).pack(side="left", padx=(6, 0))
        ttk.Label(
            self,
            text=("OpenAI-compatible endpoint (default "
                  f"{OLLAMA_DEFAULT_BASE_URL}). Used only when Provider=Ollama.\n"
                  "No key required — locally-running daemon."),
            font=("Segoe UI", 9), foreground="#666", justify="left",
        ).pack(anchor="w", padx=16, pady=(2, 0))

        # Buttons
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=16, pady=(20, 16))
        ttk.Button(btns, text="Save", width=10,
                   command=self._save).pack(side="right")
        ttk.Button(btns, text="Cancel", width=10,
                   command=self.destroy).pack(side="right", padx=(0, 6))

        self.bind("<Escape>", lambda e: self.destroy())

    def _save(self) -> None:
        # Persist API keys for both providers (no-op for empty)
        try:
            self.anthropic_row.save()
            self.openai_row.save()
        except _SaveCancelled:
            return
        except RuntimeError as exc:
            messagebox.showerror("Couldn't save key", str(exc), parent=self)
            return
        # Persist Ollama base URL
        url = self.ollama_url_var.get().strip()
        _set_ollama_base_url(url)
        self.on_saved()
        self.destroy()


# ----- Main app -----------------------------------------------------------

class WalkerApp(tk.Tk):
    # Panels the user can show/hide. First four live in the horizontal paned
    # window (and re-add in this order); last two are packed below it.
    _PANEL_LABELS = [
        ("steps",     "Steps"),
        ("probes",    "Signals"),
        ("board",     "Board"),
        ("schematic", "Schematic"),
        ("helper",    "Helper"),
        ("chat",      "Chat"),
    ]
    _PANED_KEYS = ("steps", "probes", "board", "schematic")
    _BOTTOM_KEYS = ("helper", "chat")
    # Hotkey hints shown in the View menu.
    _PANEL_ACCELERATORS = {
        "steps":     "Ctrl+Shift+P",
        "probes":    "Ctrl+Shift+A",
        "board":     "Ctrl+Shift+B",
        "schematic": "Ctrl+Shift+S",
        "helper":    "Ctrl+Shift+H",
        "chat":      "Ctrl+Shift+C",
    }
    # Alt+letter "focus this panel" hotkeys. Pressing the same Alt+letter
    # a second time restores the prior layout. Letters mirror the
    # _PANEL_ACCELERATORS letters so the muscle memory carries over —
    # Ctrl+Shift+B toggles the board panel, Alt+B focuses it. We avoid
    # Alt+F (File menu) and Alt+V (View menu); the rest of the alphabet
    # is unclaimed by Tk's menubar handling.
    _FOCUS_ACCELERATORS = {
        "steps":     "Alt+P",
        "probes":    "Alt+A",
        "board":     "Alt+B",
        "schematic": "Alt+S",
        "helper":    "Alt+H",
        "chat":      "Alt+C",
    }

    def __init__(
        self, linked: Dict[str, Any], board: BoardModel,
        state_path: Optional[Path] = None,
        rules_path: Optional[Path] = None,
        board_path: Optional[Path] = None,
    ):
        super().__init__()
        platform_label = linked.get("platform") or ""
        if platform_label:
            self.title(f"Power Sequence Walker — {platform_label}")
        else:
            # No-rules launch: the title still has to identify which board
            # is loaded so users with multiple windows can tell them apart.
            board_name = Path(board_path).name if board_path else "(no board)"
            self.title(f"Power Sequence Walker — (no rules) — {board_name}")
        self.geometry("1500x980")
        self.linked = linked
        self.board = board
        self.rules_path = rules_path
        self.board_path = board_path
        self.platform_key: str = platform_label
        self._rules_data_cache: Optional[Dict[str, Any]] = None
        self._pin_to_net: Dict[Tuple[str, str], str] = {}
        # Schematic match-index state. Populated by
        # `_on_schematic_loaded` once a PDF lands. When non-None the
        # step-display falls back to "schematic page X (sub-circuit
        # name, confidence Y)" hints whenever a rule signal can't be
        # resolved against board nets. Lazy-imported below so the
        # walker still launches if schematic_text / signal_match are
        # missing or broken.
        self._schematic_text_idx: Optional[Any] = None  # SchematicIndex
        self._schematic_match_idx: Optional[Dict[str, List[str]]] = None
        self._build_pin_to_net()
        self.steps = flatten_to_steps(linked)
        self.idx = 0
        self.results: Dict[int, str] = {}
        self.state_path = state_path
        # Defaults — _load_state may overwrite, _build_ui then reads these
        # to seed the BooleanVars.
        self._initial_panel_visibility: Dict[str, bool] = {
            key: True for key, _ in self._PANEL_LABELS
        }
        self._load_state()

        self._build_ui()

        self.bind("<Left>", lambda e: self._safe_action(self._prev))
        self.bind("<Right>", lambda e: self._safe_action(self._next))
        self.bind("<Home>", lambda e: self.canvas.reset_view())
        self.bind("p", lambda e: self._safe_action(lambda: self._mark("pass")))
        self.bind("f", lambda e: self._safe_action(lambda: self._mark("fail")))
        self.bind("s", lambda e: self._safe_action(lambda: self._mark("skip")))
        self.bind("l", lambda e: self._safe_action(self._toggle_layer))
        self.bind("t", lambda e: self._safe_action(self._toggle_traces))
        self.bind("T", lambda e: self._safe_action(self._toggle_traces))
        self.bind("m", lambda e: self._safe_action(self._toggle_measure))
        self.bind("M", lambda e: self._safe_action(self._toggle_measure))
        self.bind("<Escape>", lambda e: self._safe_action(self._on_escape))

        # Panel-toggle hotkeys (Ctrl+Shift+...). Tk reports the keysym as
        # uppercase when Shift is held, but bind both cases anyway in case
        # of caps-lock or layout quirks.
        for key, accel in self._PANEL_ACCELERATORS.items():
            letter = accel.split("+")[-1]  # e.g. "P"
            for ks in (letter.upper(), letter.lower()):
                self.bind(f"<Control-Shift-{ks}>",
                          lambda e, k=key: self._toggle_panel(k))

        # Focus-panel hotkeys (Alt+...). Same letters as the toggles
        # above so Ctrl+Shift+B and Alt+B are obviously related: one
        # toggles board visibility, the other focuses on it (hides
        # everything else). Pressing Alt+B again restores the prior
        # layout — see `_focus_panel` for the toggle semantics.
        for key, accel in self._FOCUS_ACCELERATORS.items():
            letter = accel.split("+")[-1]
            for ks in (letter.upper(), letter.lower()):
                self.bind(f"<Alt-{ks}>",
                          lambda e, k=key: self._focus_panel(k))

        self.canvas.set_select_callback(self._on_canvas_select)
        self.canvas.set_layer_change_callback(self._on_canvas_layer_change)
        self.canvas.set_pin_select_callback(self._on_canvas_pin_select)
        self.canvas.set_measure_change_callback(self._on_measure_change)
        # Rebuild the schematic match index whenever a PDF lands —
        # whether from the auto-load below, the File menu, drag-drop,
        # or the panel's own "Open PDF..." button. Single callback
        # entry point covers every load path.
        self.schematic.set_load_callback(self._on_schematic_loaded)
        self.steplist.populate(self.steps)
        self._update_display()

        if self.rules_path and self.board_path and self.platform_key:
            _add_recent(self.rules_path, self.board_path, self.platform_key)
        self._rebuild_recent_menu()

        if self.board_path:
            self._maybe_autoload_schematic(self.board_path)

        # Drag-drop wiring goes last so all targets exist. Failure to
        # set up DnD (e.g. tkinterdnd2 not installed) is non-fatal —
        # the user keeps the menu workflows.
        self._setup_drag_and_drop()

    def _schematic_page_hint(self, rule_token: str) -> str:
        """Return a short human-readable string describing the
        schematic page(s) most likely to cover `rule_token`, or "" if
        no schematic is loaded / no decent candidate.

        Format: "page 29 (POWER SEQUENCE, normalized=0.90), page 14 ...".
        We cap at 3 pages so the status line stays one row tall."""
        if not rule_token:
            return ""
        if self._schematic_match_idx is None or self._schematic_text_idx is None:
            return ""
        try:
            from .signal_match import find_signal_candidates
        except Exception:
            return ""

        candidates = find_signal_candidates(
            rule_token, self._schematic_match_idx,
            max_candidates=5, min_confidence=0.40,
        )
        if not candidates:
            return ""

        # Collapse candidates → distinct pages. Multiple schematic
        # signals can point to the same page (e.g. `PWR_GD` and `PWRGD`
        # both on page 14); we want one row per page, keyed by the
        # highest-confidence candidate that landed there.
        seen_pages: set = set()
        parts: List[str] = []
        for cand in candidates:
            pages = self._schematic_text_idx.pages_for_signal(cand.match)
            for p in pages:
                if p in seen_pages:
                    continue
                seen_pages.add(p)
                title = self._schematic_text_idx.title_for_page(p) or "?"
                # Trim long titles so the status line stays readable.
                short_title = title if len(title) <= 28 else title[:25] + "..."
                parts.append(
                    f"p.{p} ({short_title}, {cand.kind}={cand.confidence:.2f})"
                )
                if len(parts) >= 3:
                    break
            if len(parts) >= 3:
                break
        return ", ".join(parts)

    def _on_schematic_loaded(self, pdf_path: Path) -> None:
        """Triggered by `SchematicPanel.open()` whenever a PDF lands.

        Extracts the per-page signal index from the PDF and builds a
        normalized match-index for fuzzy lookups. Both helpers are
        imported lazily so a missing module (extraction failures,
        broken install) doesn't break basic schematic viewing — the
        only thing lost is the page-hint enrichment in _update_display.

        Cost: a few hundred ms once per PDF. Re-triggered on every
        new PDF load (different board → different schematic)."""
        self._schematic_text_idx = None
        self._schematic_match_idx = None
        try:
            from .schematic_text import extract_index
            from .signal_match import build_match_index
        except Exception:
            traceback.print_exc()
            return
        try:
            idx = extract_index(pdf_path)
        except Exception:
            traceback.print_exc()
            return
        if not idx.has_text:
            # Image-only PDF (e.g. older scanned schematics) — nothing
            # to feed the matcher. Leave the cached indices empty so
            # _update_display falls through to its original behaviour.
            return
        self._schematic_text_idx = idx
        self._schematic_match_idx = build_match_index(
            idx.pages_by_signal.keys()
        )
        # Refresh the current step so any page hints appear immediately
        # — useful when the user drops a schematic AFTER paging into a
        # step that previously had "no boardview match".
        if self.steps:
            self._update_display()

    def _setup_drag_and_drop(self) -> None:
        """Activate tkinterdnd2 on the existing Tk root and register
        drop targets for the board canvas and the schematic panel.

        We keep the dependency optional: a colleague who hasn't yet
        run `pip install tkinterdnd2` still gets a working walker, just
        without the drop affordance. The hint goes to stderr (visible
        for CLI launches, ignored by GUI shortcuts) so it doesn't
        spam a popup."""
        try:
            from tkinterdnd2 import TkinterDnD, DND_FILES
        except ImportError:
            import sys
            print(
                "[walker] tkinterdnd2 not installed -- drag/drop disabled. "
                "Install with: pip install tkinterdnd2",
                file=sys.stderr,
            )
            return
        try:
            # Activates the tkdnd Tcl extension on the existing Tk
            # interpreter. Pass the WIDGET (self), not self.tk — the
            # _require helper indexes off widget.tk internally.
            TkinterDnD._require(self)
        except Exception as exc:
            import sys
            print(
                f"[walker] tkdnd activation failed -- drag/drop disabled "
                f"({exc.__class__.__name__}: {exc})",
                file=sys.stderr,
            )
            return
        self._dnd_files_kind = DND_FILES
        # Board drop target — accepts boardview extensions only.
        self.canvas.drop_target_register(DND_FILES)
        self.canvas.dnd_bind("<<Drop>>", self._on_board_drop)
        # Schematic drop target — accepts .pdf only. Register on the
        # whole panel (Frame) so dropping anywhere inside the panel —
        # toolbar, canvas, scrollbars — counts as a hit.
        self.schematic.drop_target_register(DND_FILES)
        self.schematic.dnd_bind("<<Drop>>", self._on_schematic_drop)

    def _parse_drop_data(self, data: str) -> List[Path]:
        """Convert the raw `event.data` payload (a Tcl-list-encoded
        string of paths) into a list of Path objects. tkdnd quotes paths
        with spaces using braces; tk.splitlist handles that correctly
        where a naive `data.split()` would corrupt them."""
        try:
            raw = self.tk.splitlist(data)
        except Exception:
            raw = data.split()
        return [Path(p) for p in raw]

    def _on_board_drop(self, event) -> None:
        """Drop handler for the board canvas. Picks the first dropped
        file whose extension is a known boardview format. Wrong-type
        drops show a friendly hint instead of silently failing."""
        paths = self._parse_drop_data(event.data)
        match = next(
            (p for p in paths if p.suffix.lower() in self.BOARD_EXTS),
            None,
        )
        if match is None:
            messagebox.showinfo(
                "Not a boardview",
                "Drop a boardview file here. Supported extensions:\n\n  "
                + "  ".join(self.BOARD_EXTS),
            )
            return
        self._load_board_path(match, show_success_popup=False)

    def _on_schematic_drop(self, event) -> None:
        """Drop handler for the schematic panel. PDF only — anything
        else gets a hint. Multiple PDFs: take the first."""
        paths = self._parse_drop_data(event.data)
        match = next((p for p in paths if p.suffix.lower() == ".pdf"), None)
        if match is None:
            messagebox.showinfo(
                "Not a PDF",
                "Drop a .pdf schematic here.",
            )
            return
        _remember_dir("schematic", match)
        self.schematic.open(match)

    def _build_pin_to_net(self) -> None:
        self._pin_to_net = {}
        for net, nodes in self.board.signals.items():
            for refdes, pin in nodes:
                self._pin_to_net[(refdes, pin)] = net

    def net_for_pin(self, refdes: str, pin: str) -> Optional[str]:
        return self._pin_to_net.get((refdes, pin))

    def _is_typing(self) -> bool:
        focus = self.focus_get()
        if not isinstance(focus, (tk.Entry, ttk.Entry, tk.Text)):
            return False
        # ttk.Combobox subclasses ttk.Entry, so the isinstance check above
        # matches it. In readonly state it doesn't accept typed text — it
        # only does prefix-match navigation. Single-char shortcuts (T, L,
        # M, P, F, S) MUST still fire; otherwise after the user clicks
        # the layer dropdown to inspect it, every shortcut goes silently
        # dead — which is exactly the "T is completely broken" symptom
        # users hit on multi-layer GPU boards (where the natural flow is
        # open dropdown → see only TOP/BOTTOM → press T to populate
        # INNER_n → nothing happens because focus is on the combobox).
        if isinstance(focus, ttk.Combobox):
            try:
                if str(focus.cget("state")) == "readonly":
                    return False
            except tk.TclError:
                pass
        return True

    def _safe_action(self, action: Callable[[], None]) -> None:
        if self._is_typing():
            return
        action()

    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            # When `self.steps` is empty (no-rules launch), `len-1` is -1
            # and the original `min(idx, -1)` would clamp idx to -1 and
            # explode anywhere we used self.steps[self.idx]. Cap at 0.
            max_idx = max(0, len(self.steps) - 1)
            self.idx = max(0, min(int(data.get("idx", 0)), max_idx))
            self.results = {int(k): v for k, v in data.get("results", {}).items()}
            panels_data = data.get("panels")
            if isinstance(panels_data, dict):
                self._apply_loaded_panel_visibility(panels_data)
        except (json.JSONDecodeError, ValueError, OSError):
            pass

    def _apply_loaded_panel_visibility(self, panels_data: Dict[str, Any]) -> None:
        """Push loaded visibility into BooleanVars if they exist (mid-session
        platform switch); otherwise cache for _build_ui to seed from."""
        has_vars = hasattr(self, "_panel_visibility")
        for key, _ in self._PANEL_LABELS:
            if key not in panels_data:
                continue
            value = bool(panels_data[key])
            if has_vars:
                self._panel_visibility[key].set(value)
            else:
                self._initial_panel_visibility[key] = value
        if has_vars:
            self._refresh_paned_layout()
            self._refresh_bottom_packing()

    def _save_state(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data: Dict[str, Any] = {
            "platform": self.linked.get("platform", ""),
            "idx": self.idx,
            "results": self.results,
        }
        if hasattr(self, "_panel_visibility"):
            data["panels"] = {
                key: var.get() for key, var in self._panel_visibility.items()
            }
        self.state_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _build_ui(self) -> None:
        # Panel visibility — seeded from _initial_panel_visibility (which
        # _load_state may have overridden from the saved state file). Bound
        # to View-menu checkbuttons and the toolbar toggle buttons.
        self._panel_visibility: Dict[str, tk.BooleanVar] = {
            key: tk.BooleanVar(value=self._initial_panel_visibility[key])
            for key, _ in self._PANEL_LABELS
        }

        # Menu
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Open boardview…",
                              command=self._menu_open_board, accelerator="Ctrl+B")
        file_menu.add_command(label="Open rules…",
                              command=self._menu_open_rules, accelerator="Ctrl+R")
        file_menu.add_command(label="Open schematic (PDF)…",
                              command=self._menu_open_schematic,
                              accelerator="Ctrl+D")
        file_menu.add_command(label="Select platform…",
                              command=self._menu_select_platform, accelerator="Ctrl+P")
        self.recent_menu = tk.Menu(file_menu, tearoff=False)
        file_menu.add_cascade(label="Open recent", menu=self.recent_menu)
        file_menu.add_separator()
        file_menu.add_command(label="Settings…",
                              command=self._menu_settings,
                              accelerator="Ctrl+,")
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.quit, accelerator="Ctrl+Q")
        menubar.add_cascade(label="File", menu=file_menu)

        view_menu = tk.Menu(menubar, tearoff=False)
        for key, label in self._PANEL_LABELS:
            view_menu.add_checkbutton(
                label=label,
                accelerator=self._PANEL_ACCELERATORS.get(key, ""),
                variable=self._panel_visibility[key],
                command=lambda k=key: self._on_panel_toggle(k),
            )
        view_menu.add_separator()
        view_menu.add_command(label="Show all panels",
                              command=self._show_all_panels)
        # "Focus this panel" submenu — one entry per panel, accelerated
        # by Alt+letter (matches the toggle's letter on Ctrl+Shift+).
        # Pressing the accelerator a second time restores the prior
        # layout. Implemented in `_focus_panel`.
        focus_menu = tk.Menu(view_menu, tearoff=False)
        for key, label in self._PANEL_LABELS:
            focus_menu.add_command(
                label=f"Focus {label}",
                accelerator=self._FOCUS_ACCELERATORS.get(key, ""),
                command=lambda k=key: self._focus_panel(k),
            )
        view_menu.add_cascade(label="Focus panel", menu=focus_menu)
        view_menu.add_separator()
        view_menu.add_command(label="Toggle traces (T)",
                              command=self._toggle_traces)
        view_menu.add_command(label="Toggle measure (M)",
                              command=self._toggle_measure)
        menubar.add_cascade(label="View", menu=view_menu)

        self.config(menu=menubar)
        self.bind("<Control-b>", lambda e: self._menu_open_board())
        self.bind("<Control-B>", lambda e: self._menu_open_board())
        self.bind("<Control-r>", lambda e: self._menu_open_rules())
        self.bind("<Control-R>", lambda e: self._menu_open_rules())
        self.bind("<Control-p>", lambda e: self._menu_select_platform())
        self.bind("<Control-P>", lambda e: self._menu_select_platform())
        self.bind("<Control-d>", lambda e: self._menu_open_schematic())
        self.bind("<Control-D>", lambda e: self._menu_open_schematic())
        self.bind("<Control-q>", lambda e: self.quit())
        self.bind("<Control-Q>", lambda e: self.quit())
        self.bind("<Control-comma>", lambda e: self._menu_settings())

        # Header
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")

        # Right-aligned panel-toggle toolbar
        toolbar = ttk.Frame(top)
        toolbar.pack(side="right", anchor="ne")
        ttk.Label(toolbar, text="Show:",
                  font=("Segoe UI", 9)).pack(side="left", padx=(0, 4))
        for key, label in self._PANEL_LABELS:
            # Append the focus-hotkey letter in parens (e.g. "Steps (P)").
            # Pulled live from _FOCUS_ACCELERATORS so a future remap
            # updates the button caption automatically. The View menu
            # already shows the Ctrl+Shift+X hotkey in its own
            # accelerator column, so we keep _PANEL_LABELS itself clean
            # and only decorate the toolbar — which has no such column
            # and otherwise gives no hotkey hint at all.
            accel = self._FOCUS_ACCELERATORS.get(key, "")
            letter = accel.split("+")[-1] if accel else ""
            btn_text = f"{label} ({letter})" if letter else label
            ttk.Checkbutton(
                toolbar, text=btn_text, style="Toolbutton",
                variable=self._panel_visibility[key],
                command=lambda k=key: self._on_panel_toggle(k),
            ).pack(side="left", padx=1)

        # Left side — platform / progress labels
        label_col = ttk.Frame(top)
        label_col.pack(side="left", anchor="w", fill="x", expand=True)
        self.lbl_platform = ttk.Label(label_col, text="",
                                      font=("Segoe UI", 12, "bold"))
        self.lbl_platform.pack(anchor="w")
        self.lbl_progress = ttk.Label(label_col, text="",
                                      font=("Segoe UI", 9))
        self.lbl_progress.pack(anchor="w")

        ttk.Separator(self).pack(fill="x")

        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=6)

        # Pane 1: step list
        list_frame = ttk.Frame(paned)
        ttk.Label(list_frame, text="All steps  (click to jump)",
                  font=("Segoe UI", 10, "underline")).pack(anchor="w", padx=4, pady=(4, 2))
        self.steplist = StepList(list_frame, on_jump=self._jump)
        self.steplist.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        paned.add(list_frame, weight=2)

        # Pane 2: signal info + clickable probe list
        center = ttk.Frame(paned, padding=(8, 6))
        self.lbl_stage = ttk.Label(center, text="", font=("Segoe UI", 11, "bold"),
                                   foreground="#333")
        self.lbl_stage.pack(anchor="w")
        self.lbl_signal = tk.Label(center, text="", font=("Consolas", 16, "bold"),
                                   anchor="w", justify="left")
        self.lbl_signal.pack(anchor="w", pady=(8, 0), fill="x")
        self.lbl_voltage = ttk.Label(center, text="", font=("Segoe UI", 11))
        self.lbl_voltage.pack(anchor="w", pady=(8, 0))
        self.lbl_resistance = ttk.Label(center, text="", font=("Segoe UI", 11))
        self.lbl_resistance.pack(anchor="w")
        self.lbl_semantic = ttk.Label(center, text="", font=("Segoe UI", 9))
        self.lbl_semantic.pack(anchor="w", pady=(4, 0))

        ttk.Label(center, text="Probe locations  (click row → select on canvas)",
                  font=("Segoe UI", 10, "underline")).pack(anchor="w", pady=(12, 2))
        self.lbl_probe_status = ttk.Label(center, text="",
                                          font=("Segoe UI", 9, "italic"),
                                          foreground="#555")
        self.lbl_probe_status.pack(anchor="w", pady=(0, 4))

        probes_frame = ttk.Frame(center)
        probes_frame.pack(fill="both", expand=True)
        cols = ("refdes", "pin", "layer", "xy", "device")
        self.probes_tree = ttk.Treeview(
            probes_frame, columns=cols, show="tree headings", height=8,
        )
        self.probes_tree.heading("#0", text="#")
        self.probes_tree.heading("refdes", text="Refdes")
        self.probes_tree.heading("pin", text="Pin")
        self.probes_tree.heading("layer", text="L")
        self.probes_tree.heading("xy", text="(X, Y)")
        self.probes_tree.heading("device", text="Device")
        self.probes_tree.column("#0", width=30, stretch=False, anchor="e")
        self.probes_tree.column("refdes", width=70, stretch=False)
        self.probes_tree.column("pin", width=60, stretch=False)
        self.probes_tree.column("layer", width=30, stretch=False, anchor="center")
        self.probes_tree.column("xy", width=120, stretch=False)
        self.probes_tree.column("device", width=120, stretch=True)
        sb_p = ttk.Scrollbar(probes_frame, orient="vertical",
                             command=self.probes_tree.yview)
        self.probes_tree.config(yscrollcommand=sb_p.set)
        sb_p.pack(side="right", fill="y")
        self.probes_tree.pack(side="left", fill="both", expand=True)
        self.probes_tree.bind("<Button-1>", self._on_probe_click)
        paned.add(center, weight=2)

        # Pane 3: board canvas (top) + Component / Net tabs (bottom)
        right = ttk.Frame(paned)
        right_paned = ttk.Panedwindow(right, orient="vertical")
        right_paned.pack(fill="both", expand=True)

        canvas_frame = ttk.Frame(right_paned, padding=(6, 4))
        canvas_header = ttk.Frame(canvas_frame)
        canvas_header.pack(fill="x")
        ttk.Label(canvas_header, text="Board view",
                  font=("Segoe UI", 10, "underline")).pack(side="left")
        # Layer selector. On 2-layer boards (most TVW mobos / all
        # GENCAD / BRD / FZ / XZZ files) only TOP and BOTTOM are listed
        # and the dropdown is functionally identical to the old toggle
        # button. On multi-layer boards (GPU PCBs once their topology
        # is built) INNER_1..N appear too — the values list is rebuilt
        # on layer-change so the first time the user enables traces and
        # the topology populates the layer table, the inner layers
        # appear automatically.
        ttk.Label(canvas_header, text="Layer:").pack(side="left", padx=(15, 2))
        self.layer_combo = ttk.Combobox(canvas_header, state="readonly",
                                        width=11, values=["TOP", "BOTTOM"])
        self.layer_combo.set("TOP")
        self.layer_combo.bind("<<ComboboxSelected>>", self._on_layer_combo_pick)
        self.layer_combo.pack(side="left", padx=(0, 4))
        ttk.Button(canvas_header, text="Mirror ⇄", width=10,
                   command=lambda: self.canvas.toggle_mirror_x()).pack(
            side="left", padx=2)
        ttk.Button(canvas_header, text="↺ 90°", width=6,
                   command=lambda: self.canvas.rotate(1)).pack(
            side="left", padx=2)
        ttk.Button(canvas_header, text="↻ 90°", width=6,
                   command=lambda: self.canvas.rotate(-1)).pack(
            side="left", padx=2)
        ttk.Label(canvas_header, text="Part:").pack(side="left", padx=(10, 2))
        self.find_entry = AutocompleteEntry(
            canvas_header, width=14,
            get_candidates=self._part_candidates,
            on_submit=self._submit_find_part,
        )
        self.find_entry.pack(side="left")
        ttk.Label(canvas_header, text="Net:").pack(side="left", padx=(10, 2))
        self.find_net_entry = AutocompleteEntry(
            canvas_header, width=14,
            get_candidates=self._net_candidates,
            on_submit=self._submit_find_net,
        )
        self.find_net_entry.pack(side="left")
        ttk.Button(canvas_header, text="Reset view (Home)",
                   command=lambda: self.canvas.reset_view()).pack(side="right")
        # Measurement-mode toggle. Label tracks `canvas.measure_mode` via
        # `_on_measure_change`; mirrors viewer.py's toolbar button. Sits
        # to the left of "Reset view (Home)" since side="right" packs
        # right-to-left.
        self.measure_btn = ttk.Button(canvas_header, text="Measure: OFF",
                                      width=14,
                                      command=self._toggle_measure)
        self.measure_btn.pack(side="right", padx=(0, 4))
        # Pick the best available rendering backend (GL → CPU). The
        # factory probes the GL stack on a hidden Toplevel before
        # committing; a failed probe falls through to BoardCanvasCPU
        # silently. WalkerApp doesn't have to know which tier it got
        # beyond the .render_tier attribute.
        self.canvas = make_board_canvas(canvas_frame, self.board)
        self.canvas.pack(fill="both", expand=True, pady=(4, 0))
        right_paned.add(canvas_frame, weight=4)

        # Bottom of right pane: Notebook with Component + Net tabs
        notebook_frame = ttk.Frame(right_paned)
        notebook_frame.pack(fill="both", expand=True)
        self.right_notebook = ttk.Notebook(notebook_frame)
        self.right_notebook.pack(fill="both", expand=True)
        self.component_info = ComponentInfoPanel(
            self.right_notebook, self.board,
            on_pin_select=self._on_info_pin_click,
        )
        self.net_info = NetInfoPanel(
            self.right_notebook, self.board,
            on_pin_jump=self._on_net_pin_jump,
        )
        self.right_notebook.add(self.component_info, text="Component")
        self.right_notebook.add(self.net_info, text="Net")
        right_paned.add(notebook_frame, weight=2)

        paned.add(right, weight=4)

        # Pane 4: schematic PDF
        self.schematic = SchematicPanel(paned)
        paned.add(self.schematic, weight=3)

        # Snapshot widget refs for the show/hide machinery.
        self.paned = paned
        self._panel_widgets: Dict[str, Tuple[tk.Widget, Dict[str, Any]]] = {
            "steps":     (list_frame,     {"weight": 2}),
            "probes":    (center,         {"weight": 2}),
            "board":     (right,          {"weight": 4}),
            "schematic": (self.schematic, {"weight": 3}),
        }

        self.helper = DiagnosisHelper(self)
        self.helper.pack(fill="x", padx=8, pady=(0, 6))

        # Claude chat panel
        self.chat = ChatPanel(self, self)
        self.chat.pack(fill="x", padx=8, pady=(0, 6))

        self._sep_bottom = ttk.Separator(self)
        self._sep_bottom.pack(fill="x")
        btm = ttk.Frame(self, padding=10)
        btm.pack(fill="x")
        ttk.Button(btm, text="◀ Prev (←)", command=self._prev).pack(side="left")
        ttk.Button(btm, text="Pass ✓ (P)",
                   command=lambda: self._mark("pass")).pack(side="left", padx=(20, 5))
        ttk.Button(btm, text="Fail ✗ (F)",
                   command=lambda: self._mark("fail")).pack(side="left", padx=5)
        ttk.Button(btm, text="Skip (S)",
                   command=lambda: self._mark("skip")).pack(side="left", padx=5)
        ttk.Button(btm, text="Next ▶ (→)", command=self._next).pack(side="right")

        # Apply seeded visibility — hides any panel that loaded as False.
        self._refresh_paned_layout()
        self._refresh_bottom_packing()

    # ---- panel show/hide ----

    def _on_panel_toggle(self, key: str) -> None:
        # User manually changed a panel's visibility (View menu or
        # Ctrl+Shift+X). That breaks the focus-mode invariant ("only
        # `focused_on` is visible"), so clear `focused_on` — the next
        # Alt+X press will start a fresh focus from the current layout.
        # We leave the saved snapshot in place: it'll be overwritten
        # by the next focus entry, and an in-progress Alt+X cycle that
        # has no saved state still falls through to "show all" cleanly.
        state = getattr(self, "_focus_state", None)
        if state is not None:
            state["focused_on"] = None
        if key in self._PANED_KEYS:
            self._refresh_paned_layout()
        else:
            self._refresh_bottom_packing()
        self._save_state()

    def _toggle_panel(self, key: str) -> None:
        """Flip a panel's BooleanVar and apply. Used by Ctrl+Shift hotkeys."""
        var = self._panel_visibility.get(key)
        if var is None:
            return
        var.set(not var.get())
        self._on_panel_toggle(key)

    def _refresh_paned_layout(self) -> None:
        """Forget all paned children, then re-add the visible ones in
        canonical order so they keep their left-to-right positions.

        Idempotent: bails out early if the currently-mapped pane set
        already matches what `_panel_visibility` calls for. Toggle
        callbacks fire on every Checkbutton click even when the user
        didn't actually change the state, and a needless forget+re-add
        cycle visibly flickers panel widths on heavy boards."""
        current = set(self.paned.panes())
        desired = {
            str(self._panel_widgets[k][0])
            for k in self._PANED_KEYS
            if self._panel_visibility[k].get()
        }
        if current == desired:
            return
        for key in self._PANED_KEYS:
            widget, _ = self._panel_widgets[key]
            if str(widget) in current:
                self.paned.forget(widget)
        for key in self._PANED_KEYS:
            if self._panel_visibility[key].get():
                widget, opts = self._panel_widgets[key]
                self.paned.add(widget, **opts)

    def _refresh_bottom_packing(self) -> None:
        """Helper and Chat both pack just above the bottom separator. Forget
        both, then re-pack the visible ones in order — packing each `before`
        the separator preserves top-to-bottom ordering.

        Idempotent: same rationale as _refresh_paned_layout. We trust
        `winfo_ismapped()` here since after the first refresh-call the
        geometry manager has settled."""
        want_helper = self._panel_visibility["helper"].get()
        want_chat = self._panel_visibility["chat"].get()
        if (bool(self.helper.winfo_ismapped()) == want_helper
                and bool(self.chat.winfo_ismapped()) == want_chat):
            return
        for widget in (self.helper, self.chat):
            widget.pack_forget()
        for key, widget in (("helper", self.helper), ("chat", self.chat)):
            if self._panel_visibility[key].get():
                widget.pack(fill="x", padx=8, pady=(0, 6),
                            before=self._sep_bottom)

    def _show_all_panels(self) -> None:
        for var in self._panel_visibility.values():
            var.set(True)
        self._refresh_paned_layout()
        self._refresh_bottom_packing()
        # Manual layout change clears any active focus (so the next
        # Alt+X starts from a clean slate, not a stale "saved" state).
        self._focus_state = None
        self._save_state()

    def _hide_all_but_board(self) -> None:
        # Equivalent to Alt+B. Goes through `_focus_panel` so the View
        # menu item and the hotkey share state — pressing Alt+B after
        # using the menu will correctly restore the prior layout.
        self._focus_panel("board")

    def _focus_panel(self, key: str) -> None:
        """Alt+X hotkey: focus on panel `key`, hiding all others.

        Three cases:
          * not currently focused → save the current visibility state
            and hide everything except `key`.
          * already focused on `key` → restore the saved visibility
            (so Alt+X effectively toggles focus mode).
          * focused on a different panel → switch focus (hide everything
            but the new `key`); the saved "before focus" state stays
            put, so the user can still get back to a multi-panel layout.

        State is held in `self._focus_state`, a dict with `focused_on`
        (panel-key or None) and `saved` (dict of pre-focus visibility,
        or None). Lives in memory only — closing and reopening the app
        loses the saved layout, but the focused-state itself persists
        because panel visibility is serialised to disk.
        """
        if key not in self._panel_visibility:
            return
        state = getattr(self, "_focus_state", None)
        if state is None:
            state = {"focused_on": None, "saved": None}
            self._focus_state = state

        if state["focused_on"] == key:
            # Toggle off — restore prior layout. If we somehow lost the
            # saved snapshot (e.g. it was set to None by a manual
            # toggle), fall back to "show all" so the user isn't stuck.
            if state["saved"] is not None:
                for k, v in state["saved"].items():
                    self._panel_visibility[k].set(v)
                state["saved"] = None
            else:
                for var in self._panel_visibility.values():
                    var.set(True)
            state["focused_on"] = None
        else:
            # Entering focus mode (or switching the focused panel).
            if state["focused_on"] is None:
                # First time entering — snapshot current visibility so
                # we can restore it on the next Alt+X press.
                state["saved"] = {
                    k: var.get()
                    for k, var in self._panel_visibility.items()
                }
            for k, var in self._panel_visibility.items():
                var.set(k == key)
            state["focused_on"] = key

        self._refresh_paned_layout()
        self._refresh_bottom_packing()
        self._save_state()

    # ---- canvas / info / net wiring ----

    def _on_canvas_select(self, refdes: Optional[str]) -> None:
        if refdes:
            self.component_info.show_component(refdes)
        else:
            self.component_info.show_placeholder()
        # When a new component is selected, the Net tab loses its focused pin
        if not refdes:
            self.net_info.show_placeholder()
            self.canvas.set_selected_net(None)

    def _on_canvas_layer_change(self, layer: str) -> None:
        self._sync_layer_widgets(layer)

    def _on_layer_combo_pick(self, _event=None) -> None:
        """Toolbar Combobox callback — push selection into the canvas.

        The displayed value may carry a "(ratsnest)" suffix when the
        active topology is the synthetic MST; strip that before
        comparing against the canvas's `view_layer`."""
        picked = self.layer_combo.get()
        new_layer = picked.split(" (", 1)[0].strip() if picked else picked
        if new_layer and new_layer != self.canvas.view_layer:
            self.canvas.set_view_layer(new_layer)

    def _sync_layer_widgets(self, layer: str) -> None:
        """Refresh the toolbar layer dropdown after a layer change.
        Called from the canvas layer-change callback (covers both the
        L-key cycle and any auto-flip path triggered by component or
        net-jump selection), and from the trace-toggle handler so the
        first trace-enable on a multi-layer board picks up newly-
        available INNER_n entries from the topology.

        If traces are on and the topology is the synthetic MST
        (ratsnest), the displayed value gets a "(ratsnest)" suffix so
        the user never mistakes the straight-line illustration for
        actual routing. The dropdown VALUES stay clean (just the
        layer names) — `_on_layer_combo_pick` strips the suffix when
        reading the user's selection."""
        if hasattr(self, "layer_combo") and self.layer_combo is not None:
            layers = _available_layers_for(self.canvas.board)
            current_values = list(self.layer_combo["values"])
            if current_values != layers:
                self.layer_combo["values"] = layers
            display = layer
            if self.canvas.show_traces:
                topo = getattr(self.board, "_topology", None)
                if topo is not None and getattr(topo, "is_synthetic", False):
                    display = f"{layer} (ratsnest)"
            if self.layer_combo.get() != display:
                self.layer_combo.set(display)

    def _on_canvas_pin_select(self, pin_name: Optional[str]) -> None:
        self.component_info.highlight_pin(pin_name)
        # Resolve net and update Net tab
        if pin_name and self.canvas.selected_refdes:
            net = self.net_for_pin(self.canvas.selected_refdes, pin_name)
            if net:
                self.net_info.show_net(
                    net, focus_pin=(self.canvas.selected_refdes, pin_name)
                )
                self.canvas.set_selected_net(net)
                return
        self.net_info.show_placeholder()
        self.canvas.set_selected_net(None)

    def _toggle_traces(self) -> None:
        if not getattr(self.board, "topology_available", False):
            return
        self.canvas.toggle_traces()
        # First trace-enable on a multi-layer board builds the topology,
        # which is when `_layer_names` becomes readable. Re-sync so the
        # dropdown picks up newly-available INNER_n entries.
        self._sync_layer_widgets(self.canvas.view_layer)

    def _on_info_pin_click(self, pin_name: str) -> None:
        self.canvas.select_pin(pin_name, center=True)

    def _on_net_pin_jump(self, refdes: str, pin: str) -> None:
        # Select the component (auto-flips layer if needed) then the pin
        self.canvas.select_refdes(refdes, center=True)
        self.component_info.show_component(refdes)
        # Defer pin selection so the canvas/info finish updating first
        self.after_idle(lambda: self.canvas.select_pin(pin, center=True))

    def _toggle_layer(self) -> None:
        """Cycle through every available layer (TOP, BOTTOM, then any
        INNER_n that the trace topology has decoded). On 2-layer boards
        this is just the old TOP↔BOTTOM flip; on multi-layer GPU PCBs
        it walks through INNER_1, INNER_2, ... after BOTTOM and wraps."""
        layers = _available_layers_for(self.canvas.board)
        if not layers:
            return
        try:
            i = layers.index(self.canvas.view_layer)
        except ValueError:
            i = -1
        new_layer = layers[(i + 1) % len(layers)]
        self.canvas.set_view_layer(new_layer)

    def _toggle_measure(self) -> None:
        """Enter or leave measurement mode. Component selection clears
        on entry so the new mode-cursor is unambiguous; mode exits with
        another M press or via Esc-Esc (Esc once just clears placed pts)."""
        on = not self.canvas.measure_mode
        if on:
            # Drop any active component / pin selection so the cursor
            # change to crosshair is the unambiguous mode signal.
            self.canvas._selected_refdes = None
            self.canvas._selected_pin = None
        self.canvas.set_measure_mode(on)
        # No status-bar update here — _on_measure_change fires for that
        # via the callback set in __init__.

    def _on_escape(self) -> None:
        """Esc: in measure mode, clear placed points (mode stays on so
        the user can immediately start a new measurement); otherwise no-op."""
        if self.canvas.measure_mode:
            self.canvas.clear_measurement()

    def _on_measure_change(self) -> None:
        """Canvas callback. Fires whenever the measurement state changes
        (mode toggled, point placed, hover moved, cleared). Walker has
        no central status label — the on-canvas overlay text is the live
        distance readout — but we still need to update the toolbar
        button label so it reflects the current ON/OFF state."""
        btn = getattr(self, "measure_btn", None)
        if btn is not None:
            btn.config(
                text=f"Measure: {'ON' if self.canvas.measure_mode else 'OFF'}",
            )

    def _on_probe_click(self, event: tk.Event) -> None:
        item = self.probes_tree.identify_row(event.y)
        if not item:
            return
        vals = self.probes_tree.item(item, "values")
        if not vals:
            return
        refdes = vals[0]
        pin = vals[1] if len(vals) > 1 else None
        self._select_and_focus(refdes, pin=pin)

    def _on_find(self, event: Optional[tk.Event] = None) -> None:
        # Legacy entry-point retained in case any binding still calls it.
        # AutocompleteEntry now drives the search via _submit_find_part.
        self._submit_find_part(self.find_entry.get())

    # ---- autocomplete: parts ---------------------------------------------

    def _part_candidates(self, query: str) -> List[str]:
        """Refdes suggestions ranked: exact > prefix > substring."""
        q = query.strip().upper()
        if not q:
            return []
        all_refs = list(self.board.components)
        exact = [r for r in all_refs if r.upper() == q]
        prefix = sorted(r for r in all_refs
                        if r.upper().startswith(q) and r.upper() != q)
        contains = sorted(r for r in all_refs
                          if q in r.upper() and not r.upper().startswith(q))
        # Cap dropdown size; prefix matches are far more useful than fuzzy.
        out = exact + prefix[:30] + contains[:10]
        return out[:30]

    def _submit_find_part(self, value: str) -> None:
        query = value.strip().upper()
        if not query:
            return
        # Exact match first, then prefix, then substring.
        for refdes in self.board.components:
            if refdes.upper() == query:
                self._select_and_focus(refdes)
                return
        prefix = sorted(r for r in self.board.components
                        if r.upper().startswith(query))
        if prefix:
            self._select_and_focus(prefix[0])
            return
        contains = sorted(r for r in self.board.components
                          if query in r.upper())
        if contains:
            self._select_and_focus(contains[0])

    # ---- autocomplete: nets ----------------------------------------------

    def _net_candidates(self, query: str) -> List[str]:
        """Net-name suggestions ranked: exact > prefix > substring."""
        q = query.strip().upper()
        if not q:
            return []
        nets = list(self.board.signals)
        exact = [n for n in nets if n.upper() == q]
        prefix = sorted(n for n in nets
                        if n.upper().startswith(q) and n.upper() != q)
        contains = sorted(n for n in nets
                          if q in n.upper() and not n.upper().startswith(q))
        out = exact + prefix[:30] + contains[:10]
        return out[:30]

    def _submit_find_net(self, value: str) -> None:
        query = value.strip().upper()
        if not query or not getattr(self.board, "signals", None):
            return
        nets = list(self.board.signals)
        chosen: Optional[str] = None
        for n in nets:
            if n.upper() == query:
                chosen = n
                break
        if chosen is None:
            prefix = sorted(n for n in nets if n.upper().startswith(query))
            if prefix:
                chosen = prefix[0]
        if chosen is None:
            contains = sorted(n for n in nets if query in n.upper())
            if contains:
                chosen = contains[0]
        if chosen is None:
            return
        self._jump_to_net(chosen)

    def _jump_to_net(self, net_name: str) -> None:
        """Switch to the Net tab, populate it, and highlight the net."""
        try:
            self.right_notebook.select(self.net_info)
        except Exception:
            pass
        self.net_info.show_net(net_name)
        try:
            self.canvas.set_selected_net(net_name)
        except Exception:
            pass

    def _select_and_focus(self, refdes: str, pin: Optional[str] = None) -> None:
        if self.canvas.zoom < 3:
            self.canvas.zoom = 4.0
        self.canvas.select_refdes(refdes, center=True)
        self.component_info.show_component(refdes)
        if pin:
            self.after_idle(lambda: self.canvas.select_pin(pin, center=True))

    # ---- File menu handlers ----

    # Boardview extensions accepted by `parse_board()`. Kept as a class
    # attribute so the menu picker, the drop handler, and the wizard
    # all agree on what counts as a boardview file.
    BOARD_EXTS = (".cad", ".brd", ".brd2", ".bv", ".tvw", ".fz", ".pcb")

    def _load_board_path(self, path: Path, *,
                         show_success_popup: bool = True) -> bool:
        """Replace the current board with the one at `path`. Returns True
        on success. Used by both the File menu and the drop handler.

        `show_success_popup` is on by default for menu invocations
        (matches prior behaviour). The drop handler turns it off — the
        new board's file name in the title bar is enough confirmation
        when the user just intentionally dragged a file in."""
        try:
            new_board = parse_board(path)
        except Exception as exc:
            messagebox.showerror("Failed to load boardview",
                                 f"Could not parse {path}:\n{exc}")
            return False
        self.board_path = Path(path)
        _remember_dir("board", self.board_path)
        self.board = new_board
        self._build_pin_to_net()
        self.canvas.set_board(new_board)
        self.component_info.set_board(new_board)
        self.net_info.set_board(new_board)
        _surface_model_warnings(new_board, parent=self)
        self._maybe_autoload_schematic(self.board_path)
        if self.rules_path and self.platform_key:
            self._relink()
        else:
            self.title(f"Power Sequence Walker — (no rules) — {path.name}")
            if not show_success_popup:
                return True
            if is_stub_format(path) and len(new_board.signals) == 0:
                # TVW: components rendered, but no pin↔net mapping yet
                messagebox.showinfo(
                    "Boardview loaded (TVW partial)",
                    f"{path.name} is a TVW (Teboview) file. We extract "
                    f"{len(new_board.components)} components with positions, "
                    "but pin/net mapping isn't decoded — net-aware features "
                    "(probe highlighting, click-pin-to-see-net) are disabled.\n\n"
                    "Use the schematic alongside for net info. Open a rules "
                    "file (File → Open rules…) to walk diagnostic steps.",
                )
            else:
                messagebox.showinfo(
                    "Boardview loaded",
                    "Boardview loaded. Open a rules file (File → Open rules…) "
                    "to enable the step walker.",
                )
        return True

    def _menu_open_board(self) -> None:
        initial = (_last_dir("board")
                   or (str(self.board_path.parent) if self.board_path else "."))
        path = filedialog.askopenfilename(
            title="Open boardview",
            filetypes=[("Boardview", "*.cad *.brd *.brd2 *.bv *.tvw *.fz *.pcb"),
                       ("GENCAD", "*.cad"),
                       ("OpenBoardView ASCII", "*.brd *.brd2 *.bv"),
                       ("Teboview", "*.tvw"),
                       ("ASRock / ASUS Allegro Extracta", "*.fz"),
                       ("XZZPCB (MSI / repair shops)", "*.pcb"),
                       ("All files", "*.*")],
            initialdir=initial,
        )
        if not path:
            return
        self._load_board_path(Path(path))

    def _menu_open_schematic(self) -> None:
        """Open a PDF schematic via the SchematicPanel toolbar."""
        self.schematic._on_open()

    def _maybe_autoload_schematic(self, board_path: Path) -> None:
        """When a board loads, look for a sibling PDF and load it silently.

        Tries: same stem (Board.cad → Board.pdf), then any *.pdf in the
        same directory whose stem fuzzily matches (case-insensitive prefix
        of 6+ chars). Skips silently if PyMuPDF is missing or no match.
        """
        if not _HAS_FITZ:
            return
        try:
            folder = board_path.parent
            stem = board_path.stem.lower()
            # Exact stem match
            exact = board_path.with_suffix(".pdf")
            if exact.exists():
                self.schematic.open(exact)
                return
            # Fuzzy: a PDF in the same folder whose stem shares a 6-char prefix
            prefix = stem[:6]
            if len(prefix) >= 6:
                for cand in folder.glob("*.pdf"):
                    if cand.stem.lower().startswith(prefix):
                        self.schematic.open(cand)
                        return
        except Exception:
            pass

    def _menu_open_rules(self) -> None:
        initial = (_last_dir("rules")
                   or (str(self.rules_path.parent) if self.rules_path else "."))
        path = filedialog.askopenfilename(
            title="Open rules (.yaml)",
            filetypes=[("Rules YAML", "*.yaml *.yml"), ("All files", "*.*")],
            initialdir=initial,
        )
        if not path:
            return
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("Failed to load rules",
                                 f"Could not parse {path}:\n{exc}")
            return
        platforms = list((data or {}).get("platforms", {}).keys())
        if not platforms:
            messagebox.showerror("No platforms",
                                 f"{path} contains no platforms.")
            return
        chosen = self._platform_picker(
            platforms,
            current=self.platform_key if self.platform_key in platforms else None,
        )
        if not chosen:
            return
        self.rules_path = Path(path)
        _remember_dir("rules", self.rules_path)
        self.platform_key = chosen
        self._rules_data_cache = data
        if self.board_path:
            self._relink()
        else:
            messagebox.showinfo(
                "Rules loaded",
                "Rules loaded. Open a boardview (File → Open boardview…) to "
                "enable cross-referencing.",
            )

    def _menu_settings(self) -> None:
        SettingsDialog(self, on_saved=self.chat.reload_client)

    def _menu_select_platform(self) -> None:
        rules_path = self.rules_path
        if not rules_path:
            messagebox.showinfo("No rules loaded",
                                "Open a rules file first (File → Open rules…).")
            return
        data = self._rules_data_cache
        if data is None:
            try:
                data = yaml.safe_load(rules_path.read_text(encoding="utf-8"))
                self._rules_data_cache = data
            except Exception as exc:
                messagebox.showerror("Failed to read rules",
                                     f"Could not parse {rules_path}:\n{exc}")
                return
        platforms = list((data or {}).get("platforms", {}).keys())
        if not platforms:
            messagebox.showerror("No platforms",
                                 f"{rules_path} contains no platforms.")
            return
        chosen = self._platform_picker(platforms, current=self.platform_key)
        if not chosen or chosen == self.platform_key:
            return
        self.platform_key = chosen
        if self.board_path:
            self._relink()

    def _platform_picker(
        self, platforms: List[str], current: Optional[str] = None,
    ) -> Optional[str]:
        dlg = tk.Toplevel(self)
        dlg.title("Select platform")
        dlg.transient(self)
        dlg.minsize(400, 280)
        # Place the dialog centered over the main window. Tk's default
        # Toplevel placement can land off-screen or behind the parent on
        # multi-monitor / focus-stealing-prevention setups; combined with
        # grab_set + wait_window that produced an invisible-modal hang
        # (you can't dismiss what you can't see). Centering guarantees
        # we're on the same screen as `self` and visibly adjacent.
        self.update_idletasks()
        parent_x = self.winfo_rootx()
        parent_y = self.winfo_rooty()
        parent_w = self.winfo_width()
        parent_h = self.winfo_height()
        dlg_w, dlg_h = 560, 420
        x = parent_x + max(0, (parent_w - dlg_w) // 2)
        y = parent_y + max(0, (parent_h - dlg_h) // 2)
        dlg.geometry(f"{dlg_w}x{dlg_h}+{x}+{y}")
        # grab_set + lift + focus_force AFTER position is set — order
        # matters on Windows; lifting before geometry can flash the
        # window at (0,0) for one frame.
        dlg.grab_set()
        dlg.lift()
        dlg.focus_force()

        ttk.Label(dlg, text="Select a platform from the rules:",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        list_frame = ttk.Frame(dlg)
        list_frame.pack(fill="both", expand=True, padx=12, pady=4)
        listbox = tk.Listbox(list_frame, font=("Segoe UI", 10),
                             activestyle="dotbox", exportselection=False)
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
        listbox.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        listbox.pack(side="left", fill="both", expand=True)
        for i, p in enumerate(platforms):
            listbox.insert("end", p)
            if p == current:
                listbox.selection_set(i)
                listbox.see(i)
        if current is None and platforms:
            listbox.selection_set(0)

        result: List[Optional[str]] = [None]

        def on_ok():
            sel = listbox.curselection()
            if sel:
                result[0] = listbox.get(sel[0])
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.pack(fill="x", padx=12, pady=(4, 12))
        ttk.Button(btns, text="OK", command=on_ok).pack(side="right")
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right", padx=(0, 6))
        listbox.bind("<Double-Button-1>", lambda e: on_ok())
        listbox.bind("<Return>", lambda e: on_ok())
        listbox.focus_set()

        self.wait_window(dlg)
        return result[0]

    def _relink(self) -> None:
        if not (self.rules_path and self.board_path and self.platform_key):
            return
        try:
            self.linked = link_platform(
                self.rules_path, self.board_path, self.platform_key,
            )
        except SystemExit as exc:
            messagebox.showerror("Linking failed", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Linking failed", f"{exc}")
            return
        self.steps = flatten_to_steps(self.linked)
        self.idx = 0
        self.results = {}
        safe = self.platform_key.replace(" ", "_").replace("/", "_")
        self.state_path = Path("private") / f"walker_state_{safe}.json"
        self._load_state()
        self.title(f"Power Sequence Walker — {self.linked['platform']}")
        self.steplist.populate(self.steps)
        self.find_entry.clear()
        self.find_net_entry.clear()
        self._update_display()
        _add_recent(self.rules_path, self.board_path, self.platform_key)
        self._rebuild_recent_menu()

    # ---- Recent files ----

    def _rebuild_recent_menu(self) -> None:
        self.recent_menu.delete(0, "end")
        recents = _get_recent()
        if not recents:
            self.recent_menu.add_command(label="(no recent files)", state="disabled")
            return
        for i, item in enumerate(recents):
            rules = Path(item.get("rules", ""))
            board = Path(item.get("board", ""))
            plat = item.get("platform", "")
            tags = []
            if not rules.exists():
                tags.append("rules?")
            if not board.exists():
                tags.append("board?")
            label = f"{plat} — {rules.name} + {board.name}"
            if tags:
                label += "  [" + ", ".join(tags) + "]"
            self.recent_menu.add_command(
                label=label[:120],
                command=lambda i=i: self._load_recent(i),
            )
        self.recent_menu.add_separator()
        self.recent_menu.add_command(label="Clear recent",
                                     command=self._clear_recent)

    def _load_recent(self, idx: int) -> None:
        recents = _get_recent()
        if idx >= len(recents):
            return
        item = recents[idx]
        rules = Path(item.get("rules", ""))
        board = Path(item.get("board", ""))
        platform = item.get("platform", "")
        if not rules.exists() or not board.exists():
            messagebox.showerror(
                "File missing",
                "One or both files in this recent entry no longer exist:\n"
                f"  rules: {rules}\n  board: {board}",
            )
            return
        try:
            new_board = parse_board(board)
            data = yaml.safe_load(rules.read_text(encoding="utf-8"))
        except Exception as exc:
            messagebox.showerror("Failed to load", f"{exc}")
            return
        platforms = list((data or {}).get("platforms", {}).keys())
        if platform not in platforms:
            messagebox.showerror(
                "Platform missing",
                f"Platform {platform!r} not found in current {rules.name}. "
                f"Available: {platforms}",
            )
            return
        self.rules_path = rules
        self.board_path = board
        self.platform_key = platform
        self._rules_data_cache = data
        self.board = new_board
        self._build_pin_to_net()
        self.canvas.set_board(new_board)
        self.component_info.set_board(new_board)
        self.net_info.set_board(new_board)
        _surface_model_warnings(new_board, parent=self)
        self._maybe_autoload_schematic(board)
        _remember_dir("rules", rules)
        _remember_dir("board", board)
        self._relink()

    def _clear_recent(self) -> None:
        _clear_recent_persisted()
        self._rebuild_recent_menu()

    # ---- Display update ----

    def _update_display(self) -> None:
        if not self.steps:
            # No-rules launch (or rules with zero linked signals). Wipe the
            # wizard-row text to a neutral placeholder so stale content from
            # a previous board doesn't bleed through, and tell the user how
            # to enable the step walker.
            self.lbl_platform.config(text="Platform: (no rules loaded)")
            self.lbl_progress.config(text="", foreground="#888")
            self.lbl_stage.config(text="")
            self.lbl_signal.config(
                text="Open File → Open rules… to enable the step walker.",
                fg="#666",
            )
            self.lbl_voltage.config(text="")
            self.lbl_resistance.config(text="")
            self.lbl_semantic.config(text="", foreground="#666")
            self.lbl_probe_status.config(text="(no probes)", foreground="#666")
            self.probes_tree.delete(*self.probes_tree.get_children())
            self.canvas.highlight([])
            return
        step = self.steps[self.idx]

        self.lbl_platform.config(text=f"Platform: {self.linked['platform']}")
        result = self.results.get(self.idx)
        result_color = RESULT_COLORS.get(result, "#888")
        result_str = f"  •  result: {result.upper()}" if result else ""
        self.lbl_progress.config(
            text=f"Step {self.idx + 1} of {len(self.steps)}{result_str}",
            foreground=result_color,
        )

        self.lbl_stage.config(text=f"Stage: {step.stage_label}")
        if step.note:
            self.lbl_signal.config(text=f"NOTE  {step.note}", fg="#555")
            self.lbl_voltage.config(text="")
            self.lbl_resistance.config(text="")
            self.lbl_semantic.config(text="(inline note from Mr. Ren)")
        elif step.step_text:
            self.lbl_signal.config(text=f"STEP  {step.step_text}", fg="#555")
            self.lbl_voltage.config(text="")
            self.lbl_resistance.config(text="")
            self.lbl_semantic.config(text="(procedural step)")
        else:
            color = SEMANTIC_COLORS.get(step.semantic or "", "#000")
            self.lbl_signal.config(text=step.raw or step.net or "(no signal)", fg=color)
            self.lbl_voltage.config(
                text=f"Expected voltage:   {step.expected_voltage or '—'}")
            self.lbl_resistance.config(
                text=f"Resistance to GND:  {step.resistance_to_ground or '—'}")
            sem = step.semantic or "standard"
            self.lbl_semantic.config(
                text=f"semantic: {sem}",
                foreground=SEMANTIC_COLORS.get(sem, "#666"),
            )

        self.probes_tree.delete(*self.probes_tree.get_children())
        if step.probe_candidates:
            self.lbl_probe_status.config(
                text=f"Matched boardview net: {step.boardview_net}  "
                     f"({len(step.probe_candidates)} probe pts)",
                foreground="#1a5a1a",
            )
            for i, p in enumerate(step.probe_candidates, 1):
                xy = f"({p['x']:.0f}, {p['y']:.0f})"
                self.probes_tree.insert(
                    "", "end", iid=str(i), text=str(i),
                    values=(p["refdes"], p["pin"], p["layer"], xy, p["device"]),
                )
        elif step.note or step.step_text:
            self.lbl_probe_status.config(
                text="(no probe needed for this entry)", foreground="#666",
            )
        else:
            hint = self._schematic_page_hint(step.raw or step.net or "")
            if hint:
                self.lbl_probe_status.config(
                    text=f"No boardview match. Schematic: {hint}",
                    foreground="#7a5a1a",  # amber — "we have a lead"
                )
            else:
                self.lbl_probe_status.config(
                    text="No boardview match — check schematic or "
                         "chipset datasheet for pin",
                    foreground="#883333",
                )

        self.canvas.highlight([p["refdes"] for p in step.probe_candidates])
        self.steplist.refresh_status(self.steps, self.results, self.idx)
        self.helper.update_for(step, self.steps, self.results, self.idx, self.board)

    def _prev(self) -> None:
        if not self.steps:
            return
        if self.idx > 0:
            self.idx -= 1
            self._update_display()
            self._save_state()

    def _next(self) -> None:
        if not self.steps:
            return
        if self.idx < len(self.steps) - 1:
            self.idx += 1
            self._update_display()
            self._save_state()

    def _jump(self, idx: int) -> None:
        if not self.steps:
            return
        if 0 <= idx < len(self.steps) and idx != self.idx:
            self.idx = idx
            self._update_display()
            self._save_state()

    def _mark(self, result: str) -> None:
        # Guard the indexed write FIRST — without rules `self.steps` is
        # empty and `self.results[self.idx]` would seed a phantom result
        # at idx=0 that the next rules-load would treat as a real probe.
        if not self.steps:
            return
        self.results[self.idx] = result
        self._save_state()
        if result in ("pass", "skip") and self.idx < len(self.steps) - 1:
            self.idx += 1
        self._update_display()


def main() -> None:
    # Print a one-time perf warning if any of the native DLLs are missing.
    # Cheap (a couple of LoadLibrary attempts) and visible *before* the
    # user opens a board, so they can decide whether to wait or rebuild.
    _check_native_dlls()

    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    # CLI shapes accepted:
    #   walker.py                                       (empty walker — user
    #                                                    loads via File menu
    #                                                    or drag-drop)
    #   walker.py BOARD                                 (board only)
    #   walker.py RULES BOARD PLATFORM                  (full triple)
    #
    # The legacy 3-arg form is preserved so existing automation scripts
    # keep working unchanged.
    ap.add_argument("first", nargs="?",
                    help="Either BOARD (1-arg form) or RULES (3-arg form)")
    ap.add_argument("second", nargs="?",
                    help="BOARD (3-arg form only)")
    ap.add_argument("third", nargs="?",
                    help="PLATFORM_PREFIX (3-arg form only)")
    ap.add_argument("--smoke-test", action="store_true",
                    help="Initialize and exit (no mainloop)")
    args = ap.parse_args()

    rules_path: Optional[Path]
    platform_prefix: Optional[str]
    board_path: Optional[Path]
    if args.first and args.second and args.third:
        # Legacy 3-arg form: first=rules, second=board, third=platform.
        rules_path = Path(args.first)
        board_path = Path(args.second)
        platform_prefix = args.third
    elif args.first and not (args.second or args.third):
        # 1-arg form: just a board, no rules.
        rules_path = None
        board_path = Path(args.first)
        platform_prefix = None
    else:
        if args.smoke_test:
            ap.error(
                "--smoke-test needs either BOARD or "
                "RULES BOARD PLATFORM positional args")
        # No args — launch with an empty board. The user can load via
        # File → Open boardview, Ctrl+B, or by dragging a file onto the
        # canvas. The launch wizard used to do this with an OS file
        # picker, but that exposed an invisible-modal hang on 4K Windows
        # setups (Toplevel parented to a withdrawn root) — and since the
        # walker now has drag-drop, the wizard wasn't earning its keep.
        rules_path = None
        board_path = None
        platform_prefix = None

    if board_path is not None:
        board = parse_board(board_path)
    else:
        # Empty BoardModel — all default_factory fields, including
        # `components`, `signals`, `shapes`. Canvas / panels / status
        # text already handle this case for TVW partial parses.
        board = BoardModel()
    if rules_path and platform_prefix:
        linked = link_platform(rules_path, board_path, platform_prefix)
    else:
        # No-rules launch. WalkerApp + flatten_to_steps tolerate an empty
        # `sections` list; the wizard UI shows a placeholder and the user
        # can attach rules later via File → Open rules….
        linked = {"platform": "", "sections": []}

    state_dir = Path("private")
    if linked.get("platform"):
        safe = linked["platform"].replace(" ", "_").replace("/", "_")
        state_path: Optional[Path] = state_dir / f"walker_state_{safe}.json"
    else:
        # No platform → no per-platform state to load/save. We could key
        # the state on the board filename instead, but with no rules
        # there are no step-results worth persisting. Skip persistence.
        state_path = None

    app = WalkerApp(
        linked, board=board, state_path=state_path,
        rules_path=rules_path, board_path=board_path,
    )
    if args.smoke_test:
        app.update_idletasks()
        app.update()
        n_top = sum(1 for c in board.components.values() if c.layer == "TOP")
        n_bot = sum(1 for c in board.components.values() if c.layer == "BOTTOM")
        print("Walker initialized OK")
        print(f"  platform:    {linked.get('platform') or '(no rules)'}")
        print(f"  total steps: {len(app.steps)}")
        print(f"  components:  {len(board.components)} ({n_top} TOP, {n_bot} BOTTOM)")
        print(f"  initial view: {app.canvas.view_layer}")
        print(f"  pin->net index: {len(app._pin_to_net)} entries")
        print(f"  anthropic available: {_HAS_ANTHROPIC}")
        print(f"  ANTHROPIC_API_KEY set: {bool(os.environ.get('ANTHROPIC_API_KEY'))}")
        warnings = getattr(board, "warnings", None) or []
        if warnings:
            print(f"  parser warnings: {len(warnings)}")
            for w in warnings:
                print(f"    - {w}")
        app.destroy()
        return
    # Defer the warning dialog to after_idle so it appears once the
    # main window has rendered — popping it before mainloop() makes
    # it appear on top of an empty window, which looks broken.
    app.after_idle(lambda: _surface_model_warnings(board, parent=app))
    app.mainloop()


if __name__ == "__main__":
    main()
