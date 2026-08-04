# SPDX-License-Identifier: LGPL-3.0-or-later
# Copyright (C) 2026 Thermetery Technology LLC

"""Shared ttk info widgets: the autocomplete search entry, the
component pin list, and the net node list. Used by both the viewer and
the walker; extracted from their previously duplicated copies."""

from typing import Callable, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk

from .app_common import pin_sort_key
from .parsers.boardview import BoardModel


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
        for pin, net in sorted(pins_on_comp, key=pin_sort_key):
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
            nodes, key=lambda rp: (rp[0], pin_sort_key((rp[1], "")))
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


__all__ = ["AutocompleteEntry", "ComponentInfoPanel", "NetInfoPanel"]
