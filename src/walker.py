# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC
#
# Power-sequencing diagnostic walker. The UI, signal-resolution engine,
# and Claude integration are this project's. Diagnostic rules (the
# per-chipset YAML the walker consumes) are loaded at runtime from a
# user-supplied file and are not shipped with the repository.
#
# The board canvas, info panels, and app plumbing are shared with the
# viewer via src/board_canvas.py, src/ui_panels.py and src/app_common.py.

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
import os
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import yaml

from .app_common import (ConfigStore, check_native_dlls,
                         surface_model_warnings)
from .board_canvas import available_layers_for, make_board_canvas
from .parsers.boardview import BoardModel, parse as parse_board, is_stub_format
from .runtime_paths import managed_data_dir
from .ui_panels import AutocompleteEntry, ComponentInfoPanel, NetInfoPanel
from .linker import link_platform


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


# ----- Persisted config (last-used dirs + recent file list) ---------------
#
# Historically this lived at private/walker_config.json *relative to the
# process CWD*, which scattered configs depending on where the walker was
# launched from. It now uses the same runtime_paths policy as the viewer
# (managed dir in frozen builds, ~/.boardviewer-walker.json from source),
# falling back to reading the legacy location once so old configs migrate
# on the first save.

_LEGACY_CONFIG_PATH = Path("private") / "walker_config.json"
_RECENT_LIMIT = 10


def _walker_config_path() -> Path:
    root = managed_data_dir()
    if root is not None:
        return root / "walker_config.json"
    return Path.home() / ".boardviewer-walker.json"


_config = ConfigStore(_walker_config_path)


def _load_config() -> Dict[str, Any]:
    config = _config.load()
    if config:
        return config
    try:
        return json.loads(_LEGACY_CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_config(config: Dict[str, Any]) -> None:
    _config.save(config)


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
        self.canvas = make_board_canvas(canvas_frame, self.board,
                                force_cpu_env="WALKER_FORCE_CPU")
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
            layers = available_layers_for(self.canvas.board)
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
        layers = available_layers_for(self.canvas.board)
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
    BOARD_EXTS = (".cad", ".brd", ".brd2", ".bv", ".tvw", ".fz", ".pcb", ".asc")

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
        surface_model_warnings(new_board, parent=self)
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
            filetypes=[("Boardview", "*.cad *.brd *.brd2 *.bv *.tvw *.fz *.pcb *.asc"),
                       ("GENCAD", "*.cad"),
                       ("OpenBoardView ASCII", "*.brd *.brd2 *.bv"),
                       ("Teboview", "*.tvw"),
                       ("ASRock / ASUS Allegro Extracta", "*.fz"),
                       ("XZZPCB (MSI / repair shops)", "*.pcb"),
                       ("eM-Test Expert ICT set (pick any member)", "*.asc"),
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
        surface_model_warnings(new_board, parent=self)
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
    check_native_dlls("walker")

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
    app.after_idle(lambda: surface_model_warnings(board, parent=app))
    app.mainloop()


if __name__ == "__main__":
    main()
