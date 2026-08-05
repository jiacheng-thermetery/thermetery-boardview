# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""
Boardview viewer — pan/zoom canvas + component & net browser.

Loads a boardview (.cad / .brd / .brd2 / .bv / .tvw / .fz / .pcb, or an
eM-Test Expert .asc directory set) and renders it in an interactive Tk
window. Drag to pan, mouse wheel to zoom,
Home or "Reset view" to fit-to-window. Click an IC to see its pins
and per-pin nets; click a row in the Net tab to jump to that pin
on the other side of the board (auto-flips layer).

Two render tiers, picked at startup based on what's installed:
  Tier 1 (BoardCanvasGL): pyopengltk + Skia GL backend  — sub-10 ms
                          frames at heavy zoom on 13k+ trace boards.
  Tier 2 (BoardCanvasCPU): tk.Canvas + optional Skia CPU surface for
                           the trace layer. Falls back to per-segment
                           tk lines if Skia/numpy aren't present.

Usage:
    python viewer.py [board_file]
"""

import argparse
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import key_store
from .parsers.boardview import BoardModel, parse as parse_board, FZKeyError
from .app_common import (ConfigStore, check_native_dlls,
                         surface_model_warnings)
from .board_canvas import available_layers_for, make_board_canvas
from .runtime_paths import config_path, key_path
from .ui_panels import AutocompleteEntry, ComponentInfoPanel, NetInfoPanel


# ----- Persisted config (last-used dir + recent file list) ----------------

_RECENT_LIMIT = 10
_config = ConfigStore(config_path)


def _load_config() -> Dict[str, Any]:
    return _config.load()


def _save_config(config: Dict[str, Any]) -> None:
    _config.save(config)


def _last_dir() -> Optional[str]:
    return _load_config().get("last_dir")


def _remember_dir(path: Path) -> None:
    config = _load_config()
    config["last_dir"] = str(path.parent if path.is_file() else path)
    _save_config(config)


def _remember_board_opened(board: Path) -> None:
    """Update last_dir and the recent list with one config read+write
    (opening a board previously loaded and rewrote the file twice)."""
    config = _load_config()
    config["last_dir"] = str(board.parent if board.is_file() else board)
    recent = [r for r in config.get("recent", []) if isinstance(r, str)]
    s = str(board)
    if s in recent:
        recent.remove(s)
    recent.insert(0, s)
    config["recent"] = recent[:_RECENT_LIMIT]
    _save_config(config)


def _get_recent() -> List[str]:
    raw = _load_config().get("recent", [])
    return [r for r in raw if isinstance(r, str)]


def _add_recent(board: Path) -> None:
    config = _load_config()
    recent = [r for r in config.get("recent", []) if isinstance(r, str)]
    s = str(board)
    if s in recent:
        recent.remove(s)
    recent.insert(0, s)
    config["recent"] = recent[:_RECENT_LIMIT]
    _save_config(config)


def _clear_recent_persisted() -> None:
    config = _load_config()
    config["recent"] = []
    _save_config(config)


# ----- Main app -----------------------------------------------------------

class ViewerApp(tk.Tk):
    def __init__(self, board: BoardModel, board_path: Optional[Path] = None):
        super().__init__()
        self.board = board
        self.board_path = board_path
        self.title(self._title_for(board_path))
        self.geometry("1500x900")

        self._pin_to_net: Dict[Tuple[str, str], str] = {}
        self._build_pin_to_net()

        self._build_ui()

        self.bind("<Home>", lambda e: self._safe(self.canvas.reset_view))
        self.bind("l", lambda e: self._safe(self._toggle_layer))
        self.bind("L", lambda e: self._safe(self._toggle_layer))
        self.bind("t", lambda e: self._safe(self._toggle_traces))
        self.bind("T", lambda e: self._safe(self._toggle_traces))
        self.bind("m", lambda e: self._safe(self._toggle_measure))
        self.bind("M", lambda e: self._safe(self._toggle_measure))
        self.bind("<Escape>", lambda e: self._safe(self._on_escape))
        self.bind("<Control-o>", lambda e: self._menu_open_board())
        self.bind("<Control-O>", lambda e: self._menu_open_board())
        self.bind("<Control-q>", lambda e: self.quit())
        self.bind("<Control-Q>", lambda e: self.quit())

        self.canvas.set_select_callback(self._on_canvas_select)
        self.canvas.set_layer_change_callback(self._on_canvas_layer_change)
        self.canvas.set_pin_select_callback(self._on_canvas_pin_select)
        self.canvas.set_measure_change_callback(self._on_measure_change)

        if board_path:
            _add_recent(board_path)
        self._rebuild_recent_menu()
        self._count_board_layers()
        self._update_status()

        # Drag-drop wiring goes last so all targets exist. Failure to
        # set up DnD (e.g. tkinterdnd2 not installed) is non-fatal —
        # the user keeps the menu workflow.
        self._setup_drag_and_drop()

    # Boardview extensions accepted by `parse_board()`. Single source of
    # truth shared between the menu picker and the drop handler.
    BOARD_EXTS = (".cad", ".brd", ".brd2", ".bv", ".tvw", ".fz", ".pcb", ".asc")

    def _setup_drag_and_drop(self) -> None:
        """Activate tkinterdnd2 on the existing Tk root and register a
        drop target on the board canvas.

        Optional dependency: a colleague who hasn't run
        `pip install tkinterdnd2` still gets a working viewer, just
        without the drop affordance. The hint goes to stderr (visible
        on CLI launches) so it doesn't spam a popup."""
        try:
            from tkinterdnd2 import TkinterDnD, DND_FILES
        except ImportError:
            import sys
            print(
                "[viewer] tkinterdnd2 not installed -- drag/drop disabled. "
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
                f"[viewer] tkdnd activation failed -- drag/drop disabled "
                f"({exc.__class__.__name__}: {exc})",
                file=sys.stderr,
            )
            return
        self.canvas.drop_target_register(DND_FILES)
        self.canvas.dnd_bind("<<Drop>>", self._on_board_drop)

    def _parse_drop_data(self, data: str) -> List[Path]:
        """Convert raw `event.data` (a Tcl-list-encoded string of paths)
        into Path objects. tkdnd brace-quotes paths with spaces; using
        tk.splitlist handles that correctly where naive `.split()`
        would corrupt them."""
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
        self._open_board_path(match)

    @staticmethod
    def _title_for(path: Optional[Path]) -> str:
        if path:
            return f"Boardviewer — {path.name}"
        return "Boardviewer"

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
        # only does prefix-match navigation. Single-char shortcuts (L, T,
        # M) MUST still fire when the layer dropdown has focus, otherwise
        # users get the "T does nothing after I clicked the dropdown"
        # symptom on multi-layer boards.
        if isinstance(focus, ttk.Combobox):
            try:
                if str(focus.cget("state")) == "readonly":
                    return False
            except tk.TclError:
                pass
        return True

    def _safe(self, action: Callable[[], None]) -> None:
        if self._is_typing():
            return
        action()

    def _build_ui(self) -> None:
        # Menu
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Open boardview…",
                              command=self._menu_open_board, accelerator="Ctrl+O")
        self.recent_menu = tk.Menu(file_menu, tearoff=False)
        file_menu.add_cascade(label="Open recent", menu=self.recent_menu)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.quit, accelerator="Ctrl+Q")
        menubar.add_cascade(label="File", menu=file_menu)

        view_menu = tk.Menu(menubar, tearoff=False)
        view_menu.add_command(label="Reset view (Home)",
                              command=lambda: self.canvas.reset_view())
        view_menu.add_command(label="Cycle layer (L)",
                              command=self._toggle_layer)
        view_menu.add_command(label="Toggle traces (T)",
                              command=self._toggle_traces)
        view_menu.add_command(label="Toggle measure (M)",
                              command=self._toggle_measure)
        view_menu.add_separator()
        view_menu.add_command(label="Mirror X",
                              command=lambda: self.canvas.toggle_mirror_x())
        view_menu.add_command(label="Rotate 90°  CCW",
                              command=lambda: self.canvas.rotate(1))
        view_menu.add_command(label="Rotate 90°  CW",
                              command=lambda: self.canvas.rotate(-1))
        menubar.add_cascade(label="View", menu=view_menu)

        settings_menu = tk.Menu(menubar, tearoff=False)
        settings_menu.add_command(label="Decryption keys…",
                                  command=self._open_key_manager)
        menubar.add_cascade(label="Settings", menu=settings_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)

        self.config(menu=menubar)

        # Toolbar with search
        toolbar = ttk.Frame(self, padding=(8, 4))
        toolbar.pack(fill="x")

        ttk.Label(toolbar, text="Component:").pack(side="left", padx=(0, 4))
        self.comp_search = AutocompleteEntry(
            toolbar,
            get_candidates=self._comp_candidates,
            on_submit=self._on_component_search_pick,
            placeholder="(refdes)",
            width=18,
        )
        self.comp_search.pack(side="left", padx=(0, 12))

        ttk.Label(toolbar, text="Net:").pack(side="left", padx=(0, 4))
        self.net_search = AutocompleteEntry(
            toolbar,
            get_candidates=self._net_candidates,
            on_submit=self._on_net_search_pick,
            placeholder="(net name)",
            width=22,
        )
        self.net_search.pack(side="left", padx=(0, 12))

        # Layer selector. On 2-layer boards (most TVW mobos / all GENCAD /
        # BRD / XZZ files) only TOP and BOTTOM are listed and the dropdown
        # is functionally identical to the old toggle button. On multi-
        # layer boards (GPU PCBs once their topology is built) INNER_1..N
        # appear too. The values list is rebuilt on layer-change so the
        # first time the user enables traces and the topology populates
        # the layer table, the inner layers appear automatically.
        self.layer_combo = ttk.Combobox(toolbar, state="readonly", width=11,
                                        values=["TOP", "BOTTOM"])
        self.layer_combo.set("TOP")
        self.layer_combo.bind("<<ComboboxSelected>>", self._on_layer_combo_pick)
        self.layer_combo.pack(side="right", padx=(4, 0))
        ttk.Label(toolbar, text="Layer:").pack(side="right", padx=(4, 4))
        # Traces start OFF (canvas builds topology on first enable, which
        # is a 3-6 s scan we don't want to pay on every load). The button
        # label is auto-corrected by `_on_traces_change` whenever the
        # canvas flips state — so it only needs to start matching that
        # initial OFF state.
        self.traces_btn = ttk.Button(toolbar, text="Traces: OFF",
                                     command=self._toggle_traces, width=14)
        self.traces_btn.pack(side="right", padx=(4, 0))
        # Measurement-mode toggle. Label tracks `_measure_mode` via
        # `_on_measure_change` (also wired to `_update_status` for the
        # in-canvas readout). Width matched to the traces button so the
        # two side-by-side toggles read as a unit.
        self.measure_btn = ttk.Button(toolbar, text="Measure: OFF",
                                      command=self._toggle_measure, width=14)
        self.measure_btn.pack(side="right", padx=(4, 0))
        self.reset_btn = ttk.Button(toolbar, text="Reset view",
                                    command=lambda: self.canvas.reset_view())
        self.reset_btn.pack(side="right", padx=(4, 0))
        # Rotation buttons. canvas.rotate(steps) is screen-CCW so positive
        # `steps` is counter-clockwise; negative is clockwise. The arrow
        # glyphs (↺ / ↻) render reliably on Tk's default Windows / Linux /
        # macOS fonts. Packed in reverse visual order — first packed sits
        # rightmost on the side="right" stack, so to get the visual layout
        # "Rotate↺  Rotate↻  Reset view ..." we pack CW first, then CCW.
        self.rotate_cw_btn = ttk.Button(
            toolbar, text="Rotate ↻", width=10,
            command=lambda: self.canvas.rotate(-1))
        self.rotate_cw_btn.pack(side="right", padx=(4, 0))
        self.rotate_ccw_btn = ttk.Button(
            toolbar, text="Rotate ↺", width=10,
            command=lambda: self.canvas.rotate(1))
        self.rotate_ccw_btn.pack(side="right", padx=(4, 0))

        # Main paned layout: left = info tabs, right = board canvas
        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=4, pady=4)

        info_frame = ttk.Frame(paned)
        paned.add(info_frame, weight=1)
        self.info_tabs = ttk.Notebook(info_frame)
        self.info_tabs.pack(fill="both", expand=True)

        self.comp_panel = ComponentInfoPanel(
            self.info_tabs, self.board,
            on_pin_select=self._on_comp_panel_pin_pick,
        )
        self.info_tabs.add(self.comp_panel, text="Component")

        self.net_panel = NetInfoPanel(
            self.info_tabs, self.board,
            on_pin_jump=self._on_net_panel_jump,
        )
        self.info_tabs.add(self.net_panel, text="Net")

        canvas_frame = ttk.Frame(paned)
        paned.add(canvas_frame, weight=4)
        self.canvas = make_board_canvas(canvas_frame, self.board)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.set_traces_change_callback(self._on_traces_change)

        # Status bar
        self.status = ttk.Label(
            self, text="", anchor="w", relief="sunken", padding=(6, 2),
        )
        self.status.pack(fill="x", side="bottom")

    # ----- recent files menu ------------------------------------------------

    def _rebuild_recent_menu(self) -> None:
        self.recent_menu.delete(0, "end")
        recent = _get_recent()
        if not recent:
            self.recent_menu.add_command(label="(no recent files)", state="disabled")
            return
        for path_str in recent:
            label = Path(path_str).name
            self.recent_menu.add_command(
                label=label,
                command=lambda p=path_str: self._open_board_path(Path(p)),
            )
        self.recent_menu.add_separator()
        self.recent_menu.add_command(
            label="Clear recent",
            command=lambda: (_clear_recent_persisted(), self._rebuild_recent_menu()),
        )

    # ----- file open --------------------------------------------------------

    def _menu_open_board(self) -> None:
        path = filedialog.askopenfilename(
            title="Open boardview",
            filetypes=[
                ("Boardview", "*.cad *.brd *.brd2 *.bv *.tvw *.fz *.pcb *.asc"),
                ("GENCAD", "*.cad"),
                ("OpenBoardView ASCII", "*.brd *.brd2 *.bv"),
                ("Teboview", "*.tvw"),
                ("Allegro Extracta (ASRock / ASUS)", "*.fz"),
                ("XZZPCB (MSI / repair shops)", "*.pcb"),
                ("eM-Test Expert ICT set (pick any member)", "*.asc"),
                ("All files", "*.*"),
            ],
            initialdir=_last_dir() or ".",
        )
        if not path:
            return
        self._open_board_path(Path(path))

    def _open_board_path(self, path: Path, key=None) -> None:
        try:
            board = parse_board(path, key=key)
        except FZKeyError as exc:
            # ASUS (RC6) .fz with a missing or bad key - prompt and retry.
            board = self._load_with_key_prompt(path, fmt="fz",
                                               initial_error=exc)
            if board is None:
                return
        except Exception as exc:
            messagebox.showerror("Could not load boardview",
                                 f"{path}\n\n{exc}")
            return
        # XZZPCB loads even without a key, but only the cleartext sections.
        # Offer to supply one so the encrypted part/pin records come in too.
        if getattr(board, "key_required", False):
            better = self._load_with_key_prompt(path, fmt="xzz")
            if better is not None:
                board = better
        _remember_board_opened(path)
        self.board = board
        self.board_path = path
        self._count_board_layers()
        self.title(self._title_for(path))
        self._build_pin_to_net()
        self.canvas.set_board(board)
        self.comp_panel.set_board(board)
        self.net_panel.set_board(board)
        self.comp_search.clear()
        surface_model_warnings(board, parent=self)
        self.net_search.clear()
        self._rebuild_recent_menu()
        self._update_status()

    def _load_with_key_prompt(self, path: Path, *, fmt: str,
                              initial_error=None):
        """Prompt for a decryption key and re-parse `path` with it. Returns a
        BoardModel on success, or None if the user cancels or gives up.
        `fmt` is "fz" (ASUS, 44 hex words) or "xzz" (16 hex digits)."""
        nl = chr(10)
        if fmt == "fz":
            title = "ASUS FZ key required"
            ask = (f"{path.name} is an RC6-encrypted ASUS .fz file and no key "
                   f"was found ({key_path('fz')} or the FZ_KEY env var)."
                   + nl + nl + "Paste the FZKey (44 x 32-bit hex words):")
        else:
            title = "XZZ key required"
            ask = (f"{path.name} is DES-encrypted and no valid key was found "
                   f"({key_path('xzz')} or the XZZPCB_KEY env var)."
                   + nl + nl + "Paste the XZZ key (16 hex digits):")
        prompt = (str(initial_error) + nl + nl + ask
                  if initial_error is not None else ask)
        for _ in range(3):
            entered = simpledialog.askstring(title, prompt, parent=self)
            if not entered or not entered.strip():
                return None
            entered = entered.strip()
            try:
                board = parse_board(path, key=entered)
            except FZKeyError as exc:
                prompt = f"That key did not work - {exc}" + nl + nl + ask
                continue
            except Exception as exc:
                messagebox.showerror("Could not load boardview",
                                     f"{path}{nl}{nl}{exc}", parent=self)
                return None
            if getattr(board, "key_required", False):
                # XZZ: the key parsed but failed its parity check.
                prompt = ("That key did not validate (parity check failed)."
                          + nl + nl + ask)
                continue
            self._maybe_save_key(fmt, entered)
            return board
        messagebox.showwarning(
            title,
            ("Giving up after several attempts - the board cannot open "
             "without a valid key.") if fmt == "fz" else
            ("Giving up after several attempts - opening without the "
             "encrypted records."),
            parent=self)
        return None

    def _maybe_save_key(self, fmt: str, entered: str) -> None:
        """Offer to persist a working key locally so the user is not asked
        again. Opt-in; declining keeps the key for this session only."""
        nl = chr(10)
        destination = key_path(fmt)
        if not messagebox.askyesno(
                "Remember this key?",
                f"The key worked. Save it to {destination} so you are not "
                f"asked again?" + nl + nl
                + "Official releases never contain keys. This is a plaintext local "
                + "file; remove data/private before sharing a portable app folder.",
                parent=self):
            return
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(entered + nl, encoding="utf-8")
            messagebox.showinfo("Key saved",
                                f"Saved to {destination.resolve()}",
                                parent=self)
        except OSError as exc:
            messagebox.showwarning(
                "Could not save key",
                f"{exc}" + nl + nl + "The key still works for this session.",
                parent=self)

    # ----- search callbacks -------------------------------------------------

    def _comp_candidates(self, query: str) -> List[str]:
        q = query.strip().upper()
        if not q:
            return sorted(self.board.components.keys())[:200]
        out_prefix: List[str] = []
        out_substr: List[str] = []
        for refdes in self.board.components:
            up = refdes.upper()
            if up.startswith(q):
                out_prefix.append(refdes)
            elif q in up:
                out_substr.append(refdes)
        out_prefix.sort()
        out_substr.sort()
        return (out_prefix + out_substr)[:200]

    def _net_candidates(self, query: str) -> List[str]:
        q = query.strip().upper()
        if not q:
            return sorted(self.board.signals.keys())[:200]
        out_prefix: List[str] = []
        out_substr: List[str] = []
        for net in self.board.signals:
            up = net.upper()
            if up.startswith(q):
                out_prefix.append(net)
            elif q in up:
                out_substr.append(net)
        out_prefix.sort()
        out_substr.sort()
        return (out_prefix + out_substr)[:200]

    def _on_component_search_pick(self, refdes: str) -> None:
        if refdes not in self.board.components:
            return
        self.canvas.select_refdes(refdes, center=True)
        self.comp_panel.show_component(refdes)
        self.info_tabs.select(self.comp_panel)
        self._update_status()

    def _on_net_search_pick(self, net: str) -> None:
        if net not in self.board.signals:
            return
        self.canvas.set_selected_net(net)
        self.net_panel.show_net(net)
        self.info_tabs.select(self.net_panel)
        self._update_status()

    # ----- canvas → app callbacks -------------------------------------------

    def _on_canvas_select(self, refdes: Optional[str]) -> None:
        if refdes:
            self.comp_panel.show_component(refdes)
            self.info_tabs.select(self.comp_panel)
        else:
            self.comp_panel.show_placeholder()
        self._update_status()

    def _on_canvas_layer_change(self, layer: str) -> None:
        self._sync_layer_widgets(layer)
        self._update_status()

    def _on_layer_combo_pick(self, _event=None) -> None:
        """Toolbar Combobox callback — push selection into the canvas."""
        new_layer = self.layer_combo.get()
        if new_layer and new_layer != self.canvas.view_layer:
            self.canvas.set_view_layer(new_layer)
        self._update_status()

    def _on_canvas_pin_select(self, pin: Optional[str]) -> None:
        refdes = self.canvas.selected_refdes
        self.comp_panel.highlight_pin(pin)
        if refdes and pin:
            net = self.net_for_pin(refdes, pin)
            if net:
                self.canvas.set_selected_net(net)
                self.net_panel.show_net(net, focus_pin=(refdes, pin))
            else:
                self.canvas.set_selected_net(None)
                self.net_panel.show_placeholder()
        self._update_status()

    def _on_traces_change(self, on: bool) -> None:
        self.traces_btn.config(text=f"Traces: {'ON' if on else 'OFF'}")
        # First trace-enable on a multi-layer board builds the topology,
        # which is when `_layer_names` becomes readable. Re-sync so the
        # dropdown picks up newly-available INNER_n entries.
        self._sync_layer_widgets(self.canvas.view_layer)

    # ----- info panels → app callbacks --------------------------------------

    def _on_comp_panel_pin_pick(self, pin: str) -> None:
        self.canvas.select_pin(pin, center=True)

    def _on_net_panel_jump(self, refdes: str, pin: str) -> None:
        comp = self.board.components.get(refdes)
        if comp and comp.layer != self.canvas.view_layer:
            self.canvas.set_view_layer(comp.layer)
        self.canvas.select_refdes(refdes, center=False)
        self.canvas.select_pin(pin, center=True)
        self.comp_panel.show_component(refdes)
        self.comp_panel.highlight_pin(pin)
        self.info_tabs.select(self.comp_panel)
        self._update_status()

    # ----- view menu --------------------------------------------------------

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
        self._sync_layer_widgets(new_layer)
        self._update_status()

    def _sync_layer_widgets(self, layer: str) -> None:
        """Refresh the toolbar layer dropdown / button label after a
        layer change. Called from both the L-key cycle and any callback
        path that might mutate `view_layer` (component-pick auto-flip,
        net-jump auto-flip, board reload)."""
        if hasattr(self, "layer_combo") and self.layer_combo is not None:
            layers = available_layers_for(self.canvas.board)
            current_values = list(self.layer_combo["values"])
            if current_values != layers:
                self.layer_combo["values"] = layers
            if self.layer_combo.get() != layer:
                self.layer_combo.set(layer)

    def _toggle_traces(self) -> None:
        # Button text and the layer-dropdown refresh both happen in
        # `_on_traces_change` (wired via `set_traces_change_callback`),
        # which the canvas fires after `toggle_traces()` flips state.
        # No explicit button update needed here — and the previous
        # explicit version called `show_traces()` which is a @property,
        # not a method, so it raised TypeError on every press of T.
        self.canvas.toggle_traces()

    def _toggle_measure(self) -> None:
        """Enter or leave measurement mode. Component selection clears
        on entry so the new mode-cursor is unambiguous; mode exits with
        another M press or via Esc.

        Button text + status bar both update from `_on_measure_change`
        which the canvas fires after `set_measure_mode()` flips state."""
        on = not self.canvas.measure_mode
        if on:
            # Drop any active component / pin selection so the cursor
            # change to crosshair is the unambiguous mode signal.
            self.canvas._selected_refdes = None
            self.canvas._selected_pin = None
        self.canvas.set_measure_mode(on)

    def _on_measure_change(self) -> None:
        """Canvas callback: keep the toolbar Measure button label and the
        status-bar readout in sync with `canvas.measure_mode` and the
        current measurement points. Fired on mode toggle, point placed,
        hover moved, and clear."""
        self.measure_btn.config(
            text=f"Measure: {'ON' if self.canvas.measure_mode else 'OFF'}",
        )
        self._update_status()

    def _on_escape(self) -> None:
        """Esc: in measure mode, clear placed points (mode stays on so
        the user can immediately start a new measurement); otherwise no-op."""
        if self.canvas.measure_mode:
            self.canvas.clear_measurement()

    # ----- status / about ---------------------------------------------------

    def _count_board_layers(self) -> None:
        """Cache the TOP-side component count for the status bar.
        _update_status fires on nearly every interaction and previously
        re-scanned all components each time."""
        self._n_top_components = sum(
            1 for c in self.board.components.values() if c.layer == "TOP")

    def _update_status(self) -> None:
        n_comp = len(self.board.components)
        n_net = len(self.board.signals)
        n_top = self._n_top_components
        n_bot = n_comp - n_top
        layer = self.canvas.view_layer
        # Synthetic-ratsnest cue: when traces are on AND the active
        # topology is the synthetic MST (no real routed-trace data on
        # this board), tag the layer label so the user never mistakes
        # the straight-line illustration for actual routing.
        layer_label = layer
        if self.canvas.show_traces:
            topo = getattr(self.board, "_topology", None)
            if topo is not None and getattr(topo, "is_synthetic", False):
                layer_label = f"{layer} (ratsnest)"
        sel = self.canvas.selected_refdes
        pin = self.canvas.selected_pin
        bits = [
            f"layer: {layer_label}",
            f"components: {n_comp} ({n_top} TOP / {n_bot} BOTTOM)",
            f"nets: {n_net}",
        ]
        if sel:
            bits.append(f"selected: {sel}" + (f" pin {pin}" if pin else ""))
        # Measurement readout. Three states:
        #   * mode on, 0 pts placed     -> "measure: click first point"
        #   * mode on, 1 pt + hover     -> "measure: <distance> (preview)"
        #   * mode on, 2 pts            -> "measure: <distance>"
        if self.canvas.measure_mode:
            d = self.canvas.measurement_distance_units()
            if d is not None:
                bits.append(
                    f"measure: {self.canvas._format_distance(d)}")
            else:
                d_prev = self.canvas.measurement_distance_preview_units()
                if d_prev is not None:
                    bits.append(
                        f"measure: {self.canvas._format_distance(d_prev)}"
                        " (preview)")
                else:
                    bits.append("measure: click first point")
        self.status.config(text="   ".join(bits))

    def _show_about(self) -> None:
        messagebox.showinfo(
            "About Boardviewer",
            "Boardviewer\n"
            "Pan/zoom boardview viewer with component & net browsing.\n\n"
            "Supported formats:\n"
            "  • GENCAD 1.4   (.cad)\n"
            "  • OpenBoardView ASCII   (.brd / .brd2 / .bv)\n"
            "  • Teboview   (.tvw)\n"
            "  • XZZPCB V1.0   (.pcb, MSI / repair shops)\n",
        )

    def _open_key_manager(self) -> None:
        # Reuse a single instance so repeated menu clicks just refocus it.
        existing = getattr(self, "_key_manager", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_set()
            return
        self._key_manager = KeyManagerDialog(self)


# Decryption-key slots shown in the key manager: (format, title, hint).
_KEY_MANAGER_SLOTS = (
    ("fz", "ASUS  ·  .fz  (RC6)",
     "Paste the FZKey — 44 × 32-bit hex words. Fully verified here."),
    ("xzz", "XZZ  ·  .pcb  (DES)",
     "Paste the XZZ key (hex). Confirmed only by opening a board."),
)

_STATUS_COLORS = {
    "good": "#1a7f37",
    "warn": "#9a6700",
    "bad": "#b42318",
    "neutral": "#555555",
}


class KeyManagerDialog(tk.Toplevel):
    """Standalone decryption-key manager (Settings → Decryption keys…).

    Desktop counterpart of the Android key screen: provision the ASUS ``.fz``
    (RC6) and XZZ ``.pcb`` (DES) keys up front — paste or load from a file,
    validate offline, save, or clear — without needing to open a board first.
    Keys persist to the same plaintext files the open flow reads (each slot
    shows its exact on-disk path), and validation is shared with the Android
    bridge via :mod:`key_store`.
    """

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.title("Decryption keys")
        self.transient(master)
        self.resizable(False, False)
        self._texts: Dict[str, tk.Text] = {}
        self._statuses: Dict[str, ttk.Label] = {}
        self._build()
        self._refresh_all()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.update_idletasks()
        self.grab_set()

    # ----- construction ----------------------------------------------------

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        ttk.Label(
            outer, wraplength=540, justify="left",
            text=("Decryption keys are stored only on this computer and "
                  "supplied automatically when you open an encrypted board."),
        ).pack(anchor="w")
        ttk.Label(
            outer, wraplength=540, justify="left", foreground="#777777",
            text=("Official releases never contain keys. Each key is a "
                  "plaintext file; remove the private folder before sharing a "
                  "portable app folder."),
        ).pack(anchor="w", pady=(2, 10))

        for fmt, title, hint in _KEY_MANAGER_SLOTS:
            self._build_slot(outer, fmt, title, hint)

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(2, 0))
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right")

    def _build_slot(self, parent: tk.Misc, fmt: str, title: str,
                    hint: str) -> None:
        frame = ttk.LabelFrame(parent, text=title, padding=10)
        frame.pack(fill="x", pady=(0, 10))

        ttk.Label(frame, text=hint, wraplength=520,
                  justify="left").pack(anchor="w")
        ttk.Label(frame, text=f"Stored at:  {key_path(fmt)}", wraplength=520,
                  justify="left", foreground="#777777").pack(anchor="w",
                                                             pady=(1, 6))

        text = tk.Text(frame, height=3, width=68, wrap="word",
                       font="TkFixedFont")
        text.pack(fill="x")
        self._texts[fmt] = text

        status = ttk.Label(frame, text="", wraplength=520, justify="left")
        status.pack(anchor="w", pady=(6, 6))
        self._statuses[fmt] = status

        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Button(row, text="Load file…",
                   command=lambda: self._load_file(fmt)).pack(side="left")
        ttk.Button(row, text="Validate",
                   command=lambda: self._validate(fmt)).pack(side="left",
                                                             padx=(6, 0))
        ttk.Button(row, text="Save",
                   command=lambda: self._save(fmt)).pack(side="left",
                                                         padx=(6, 0))
        ttk.Button(row, text="Clear",
                   command=lambda: self._clear(fmt)).pack(side="left",
                                                          padx=(6, 0))

    # ----- helpers ---------------------------------------------------------

    def _get(self, fmt: str) -> str:
        return self._texts[fmt].get("1.0", "end").strip()

    def _set(self, fmt: str, value: str) -> None:
        widget = self._texts[fmt]
        widget.delete("1.0", "end")
        if value:
            widget.insert("1.0", value)

    def _set_status(self, fmt: str, message: str, kind: str) -> None:
        self._statuses[fmt].configure(text=message,
                                      foreground=_STATUS_COLORS[kind])

    def _refresh_all(self) -> None:
        for fmt, _title, _hint in _KEY_MANAGER_SLOTS:
            saved = key_store.read_key(fmt)
            if saved:
                self._set(fmt, saved)
                self._set_status(fmt, "Saved on this computer.", "good")
            else:
                self._set_status(fmt, "Not set.", "neutral")

    # ----- actions ---------------------------------------------------------

    def _load_file(self, fmt: str) -> None:
        path = filedialog.askopenfilename(
            parent=self, title="Choose a key file",
            filetypes=[("Text / key files", "*.txt *.key"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            messagebox.showwarning("Could not read file", str(exc), parent=self)
            return
        self._set(fmt, text.strip())
        self._validate(fmt)

    def _validate(self, fmt: str) -> str:
        text = self._get(fmt)
        if not text:
            self._set_status(fmt, "Not set.", "neutral")
            return "empty"
        status, message = key_store.validate_key_text(fmt, text)
        kind = {"valid": "good", "unverified": "warn"}.get(status, "bad")
        self._set_status(fmt, message, kind)
        return status

    def _save(self, fmt: str) -> None:
        text = self._get(fmt)
        if not text:
            messagebox.showinfo("Nothing to save",
                                "Paste or load a key first.", parent=self)
            return
        status = self._validate(fmt)
        if not key_store.is_savable(status):
            messagebox.showwarning(
                "Not saved",
                "The key is not well-formed, so it was not saved.",
                parent=self)
            return
        try:
            dest = key_store.write_key(fmt, text)
        except OSError as exc:
            messagebox.showwarning("Could not save key", str(exc), parent=self)
            return
        self._set_status(fmt, f"Saved to {dest}.", "good")

    def _clear(self, fmt: str) -> None:
        had = key_store.clear_key(fmt)
        self._set(fmt, "")
        self._set_status(fmt, "Cleared." if had else "Not set.", "neutral")


def main() -> None:
    # Print a one-time perf warning if any of the native DLLs are missing.
    # Cheap (a couple of LoadLibrary attempts) and visible *before* the
    # user opens a board, so they can decide whether to wait or rebuild.
    check_native_dlls("viewer")

    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("board", nargs="?",
                    help="Path to a boardview file (.cad/.brd/.brd2/.bv/.tvw/.fz/.pcb). "
                         "If omitted you'll be prompted.")
    ap.add_argument("--smoke-test", action="store_true",
                    help="Initialize and exit (no mainloop)")
    ap.add_argument("--key", default=None,
                    help="Decryption key for an encrypted board when the "
                         "local key file is missing: ASUS .fz wants 44 hex "
                         "words, XZZ .pcb wants 16 hex digits. Also settable "
                         "via the FZ_KEY / XZZPCB_KEY environment variables.")
    args = ap.parse_args()

    if args.board:
        board_path: Optional[Path] = Path(args.board)
    else:
        if args.smoke_test:
            ap.error("--smoke-test requires a board path")
        root = tk.Tk()
        root.withdraw()
        try:
            picked = filedialog.askopenfilename(
                title="Open boardview",
                filetypes=[
                    ("Boardview", "*.cad *.brd *.brd2 *.bv *.tvw *.fz *.pcb"),
                    ("All files", "*.*"),
                ],
                initialdir=_last_dir() or ".",
            )
        finally:
            root.destroy()
        if not picked:
            return
        board_path = Path(picked)
        _remember_dir(board_path)

    try:
        board = parse_board(board_path, key=args.key)
    except FZKeyError as exc:
        import sys
        print(f"[viewer] {exc}", file=sys.stderr)
        print("[viewer] Supply it with --key, set FZ_KEY in the environment, "
              "or open the file from the GUI to be prompted.", file=sys.stderr)
        sys.exit(2)

    app = ViewerApp(board, board_path=board_path)
    if args.smoke_test:
        app.update_idletasks()
        app.update()
        n_top = sum(1 for c in board.components.values() if c.layer == "TOP")
        n_bot = sum(1 for c in board.components.values() if c.layer == "BOTTOM")
        print("Viewer initialized OK")
        print(f"  board:        {board_path}")
        print(f"  components:   {len(board.components)} ({n_top} TOP / {n_bot} BOTTOM)")
        print(f"  nets:         {len(board.signals)}")
        print(f"  pin->net:     {len(app._pin_to_net)} entries")
        print(f"  initial view: {app.canvas.view_layer}")
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
