"""
ui_components.py – Reusable UI widgets for GIS Road Master.

Supports ttkbootstrap (dark/modern theme) with a clean ttk fallback.
Hebrew text is handled by relying on Segoe UI, which ships with Windows.
"""

from __future__ import annotations

import tkinter as tk

try:
    import ttkbootstrap as ttk
    from ttkbootstrap.constants import *  # noqa: F401,F403
    BOOTSTRAP = True
except ImportError:
    from tkinter import ttk  # type: ignore[no-redef]
    BOOTSTRAP = False


# ─────────────────────────────────────────────────────────────────────────────
# HELPER
# ─────────────────────────────────────────────────────────────────────────────

def make_button(parent, text: str, command, style: str = "primary",
                width: int | None = None, **kw) -> ttk.Button:
    """Create a ttkbootstrap-styled button (falls back to plain ttk.Button)."""
    kwargs: dict = {"text": text, "command": command, **kw}
    if width is not None:
        kwargs["width"] = width
    if BOOTSTRAP:
        kwargs["bootstyle"] = style
    return ttk.Button(parent, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# DRAG-SELECT CHECKLIST
# ─────────────────────────────────────────────────────────────────────────────

class DragSelectChecklist:
    """
    Scrollable checklist that supports click-and-drag multi-select.

    Drag downward to toggle multiple items in one gesture, similar to
    how file managers handle shift-click selection.
    """

    def __init__(self, parent, title: str):
        frame_kw: dict = {"text": title, "padding": "5"}
        if BOOTSTRAP:
            frame_kw["bootstyle"] = "secondary"
        self.frame = ttk.LabelFrame(parent, **frame_kw)
        self.frame.pack(side="left", expand=True, fill="both", padx=4)

        btn_row = ttk.Frame(self.frame)
        btn_row.pack(fill="x", pady=(0, 4))
        make_button(btn_row, "All", self.select_all,
                    "outline-success", width=5).pack(side="left", padx=1)
        make_button(btn_row, "None", self.deselect_all,
                    "outline-danger", width=5).pack(side="left", padx=1)

        self._canvas = tk.Canvas(self.frame, highlightthickness=0)
        self._sb = ttk.Scrollbar(self.frame, command=self._canvas.yview)
        self.inner = ttk.Frame(self._canvas)

        self.inner.bind(
            "<Configure>",
            lambda _: self._canvas.configure(
                scrollregion=self._canvas.bbox("all")))

        self._canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._canvas.configure(yscrollcommand=self._sb.set)

        self._canvas.pack(side="left", expand=True, fill="both")
        self._sb.pack(side="right", fill="y")

        self._canvas.bind("<MouseWheel>",
                          lambda e: self._canvas.yview_scroll(
                              -1 * (e.delta // 120), "units"))

        self.vars: dict[str, tk.BooleanVar] = {}
        self._buttons: list[tuple[ttk.Checkbutton, tk.BooleanVar]] = []
        self._drag_state: bool | None = None

    # ── population ──────────────────────────────────────────────────────────

    def populate(self, items: list[str], default_fn) -> None:
        """Rebuild the list. default_fn(item) → bool for initial checked state."""
        for w in self.inner.winfo_children():
            w.destroy()
        self.vars.clear()
        self._buttons.clear()

        for item in items:
            var = tk.BooleanVar(value=bool(default_fn(item)))
            cb_kw: dict = {"text": item, "variable": var}
            if BOOTSTRAP:
                cb_kw["bootstyle"] = "round-toggle"
            cb = ttk.Checkbutton(self.inner, **cb_kw)
            cb.pack(anchor="w", fill="x", pady=1)
            self.vars[item] = var
            self._buttons.append((cb, var))
            cb.bind("<ButtonPress-1>", lambda e, v=var: self._drag_start(v))
            cb.bind("<B1-Motion>", self._drag_move)
            cb.bind("<ButtonRelease-1>", self._drag_end)

    # ── public helpers ───────────────────────────────────────────────────────

    def select_all(self) -> None:
        for v in self.vars.values():
            v.set(True)

    def deselect_all(self) -> None:
        for v in self.vars.values():
            v.set(False)

    def get_selected(self) -> list[str]:
        return [k for k, v in self.vars.items() if v.get()]

    # ── drag logic ───────────────────────────────────────────────────────────

    def _drag_start(self, var: tk.BooleanVar) -> str:
        self._drag_state = not var.get()
        var.set(self._drag_state)
        return "break"

    def _drag_move(self, event: tk.Event) -> None:
        if self._drag_state is None:
            return
        target = self.inner.winfo_containing(event.x_root, event.y_root)
        for cb, var in self._buttons:
            if target == cb or target in cb.winfo_children():
                var.set(self._drag_state)

    def _drag_end(self, _: tk.Event) -> None:
        self._drag_state = None


# ─────────────────────────────────────────────────────────────────────────────
# LABELED SLIDER
# ─────────────────────────────────────────────────────────────────────────────

class SliderRow:
    """
    A horizontal Scale with a bold label on the left and a live value
    readout on the right.  Optionally calls on_change(value) on every move.
    """

    def __init__(self, parent, label: str, from_: float, to: float,
                 initial: float, resolution: float, fmt: str = "{:.6g}",
                 on_change=None):
        self._fmt = fmt
        self._cb = on_change

        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=3)

        top = ttk.Frame(frame)
        top.pack(fill="x")
        ttk.Label(top, text=label, font=("Segoe UI", 9, "bold")).pack(side="left")
        self._lbl = ttk.Label(top, text=fmt.format(initial))
        self._lbl.pack(side="right")

        self.scale = tk.Scale(
            frame, from_=from_, to=to, resolution=resolution,
            orient="horizontal", showvalue=False,
            command=self._on_move)
        self.scale.set(initial)
        self.scale.pack(fill="x")

    def _on_move(self, val: str) -> None:
        try:
            fval = float(val)
            self._lbl.config(text=self._fmt.format(fval))
            if self._cb:
                self._cb(fval)
        except Exception:
            pass

    def get(self) -> float:
        return float(self.scale.get())

    def set(self, val: float) -> None:
        self.scale.set(val)

    def configure(self, **kw) -> None:
        self.scale.configure(**kw)


# ─────────────────────────────────────────────────────────────────────────────
# STATUS BAR
# ─────────────────────────────────────────────────────────────────────────────

class StatusBar:
    """
    Thin status bar (bottom of window) with a text label and a progress bar.
    """

    def __init__(self, parent):
        frame = ttk.Frame(parent)
        frame.pack(fill="x", side="bottom", padx=6, pady=(2, 4))

        self._msg = ttk.Label(frame, text="Ready", anchor="w")
        self._msg.pack(side="left", fill="x", expand=True)

        bar_kw: dict = {"length": 220, "mode": "determinate"}
        if BOOTSTRAP:
            bar_kw["bootstyle"] = "success-striped"
        self.bar = ttk.Progressbar(frame, **bar_kw)
        self.bar.pack(side="right", padx=(6, 0))
        self.bar["value"] = 0

    def set_message(self, msg: str) -> None:
        self._msg.config(text=msg)

    def set_progress(self, pct: float, msg: str | None = None) -> None:
        self.bar["value"] = max(0.0, min(100.0, pct))
        if msg:
            self.set_message(msg)

    def reset(self, msg: str = "Ready") -> None:
        self.bar["value"] = 0
        self.set_message(msg)
