"""
main.py – GIS Road Master: modern, professional centerline extraction tool.

Architecture:
    Left sidebar (340 px fixed)  → file loading, filters, parameters, actions
    Right pane (expandable)      → embedded Matplotlib canvas (live preview)
    Bottom                       → status bar + progress

Processing runs in a background thread; the UI stays responsive and shows
per-segment progress.  ttkbootstrap (darkly theme) is used when available,
with a clean ttk/clam fallback.
"""

from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

import geopandas as gpd
import numpy as np
import pandas as pd
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from matplotlib.widgets import RectangleSelector
from shapely.geometry import LineString, MultiLineString, Point, box as shapely_box
from shapely.ops import snap, unary_union

try:
    import ttkbootstrap as ttk
    from ttkbootstrap.constants import *  # noqa: F401,F403
    BOOTSTRAP = True
    THEME = "darkly"
except ImportError:
    from tkinter import ttk  # type: ignore[no-redef]
    BOOTSTRAP = False
    THEME = None

from algorithms import (
    apply_smoothing,
    chaikins_corner_cutting,
    export_geojson,
    lines_to_gdf,
    METHOD_LABELS,
    process_segments,
    prune_dead_ends,
    snap_endpoints,
)
from ui_components import DragSelectChecklist, SliderRow, StatusBar, make_button

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

FONT_MAIN   = ("Segoe UI", 10)
FONT_BOLD   = ("Segoe UI", 10, "bold")
FONT_HEADER = ("Segoe UI", 13, "bold")

# Map colours
C_GLOBAL      = "#4a9eff"   # blue     – global centerlines
C_PRECISION   = "#2ecc71"   # green    – precision edits
C_BOX         = "#e74c3c"   # red      – edit-box outline
C_CONNECTOR   = "#f39c12"   # orange   – connector preview
C_CONN_REJ    = "#555577"   # slate    – rejected connector
C_HINT        = "#c678dd"   # purple   – routing hint strokes
C_POLY_ACT    = "#2980b9"   # blue     – active polygon in shape builder
C_POLY_SEL    = "#e67e22"   # orange   – selected polygon
C_POLY_EXC    = "#922b21"   # red      – excluded polygon
C_BG_DARK     = "#16213e"
C_BG_LIGHT    = "#f4f4f4"


# ─────────────────────────────────────────────────────────────────────────────
# MAIN APPLICATION
# ─────────────────────────────────────────────────────────────────────────────

class GISRoadMaster:
    def __init__(self, root: tk.Tk | ttk.Window):
        self.root = root
        self.root.title("GIS Road Master")
        self.root.geometry("1340x920")
        self.root.minsize(900, 640)

        # ── data state ──────────────────────────────────────────────────────
        self.gdf: gpd.GeoDataFrame | None = None
        self.master_lines: list[LineString] = []   # current global result
        self._raw_lines:   list[LineString] = []   # pre-smooth cache for live slider
        self.precision_lines: list[LineString] = []
        self.eraser_history: list[tuple[int, LineString]] = []
        self.selected_box = None                   # shapely box geom
        # Pencil tool
        self._pencil_lines: list[LineString] = []
        # Shape Builder – polygons after user merge/exclude (None = use filters)
        self._shape_built_geoms: list | None = None
        # Routing Hints – drawn strokes applied as post-snap during processing
        self._hint_lines: list[LineString] = []
        self._hint_snap_tol: float = 0.0003
        # Method report from last process_segments call
        self._method_report: list[dict] = []
        # Manual algorithm selection (combobox)
        self._manual_algo = tk.StringVar(value="straight_skeleton")

        # ── async state ──────────────────────────────────────────────────────
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._done_cb = None
        self._rs = None          # RectangleSelector
        self._key_cid = None     # canvas key-press connection id

        self._build_ui()

    # ═════════════════════════════════════════════════════════════════════════
    # UI CONSTRUCTION
    # ═════════════════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        self.statusbar = StatusBar(self.root)

        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=4, pady=4)

        self._left = ttk.Frame(paned, width=340)
        self._left.pack_propagate(False)
        paned.add(self._left, weight=0)

        self._right = ttk.Frame(paned)
        paned.add(self._right, weight=1)

        self._build_left()
        self._build_canvas()

    # ── left sidebar ──────────────────────────────────────────────────────────

    def _build_left(self) -> None:
        lf = self._left
        pad = {"padx": 8, "pady": 3}

        # Title
        ttk.Label(lf, text="GIS Road Master", font=FONT_HEADER).pack(**pad)
        ttk.Separator(lf).pack(fill="x", padx=8, pady=2)

        # File section (top, fixed)
        file_frame = ttk.LabelFrame(lf, text=" Data Source ", padding="6")
        file_frame.pack(fill="x", **pad)
        make_button(file_frame, "Load GeoJSON", self._load_file, "info").pack(fill="x")
        self._file_lbl = ttk.Label(file_frame, text="No file loaded",
                                   wraplength=295, font=("Segoe UI", 8))
        self._file_lbl.pack(anchor="w", pady=(3, 0))

        # Actions section (bottom, fixed) – pack BEFORE the expanding filters
        act_frame = ttk.LabelFrame(lf, text=" Actions ", padding="6")
        act_frame.pack(fill="x", side="bottom", **pad)

        # ── Step 1: Prepare ──────────────────────────────────────────────
        ttk.Label(act_frame, text="① PREPARE",
                  font=("Segoe UI", 7, "bold"),
                  foreground="#888888").pack(anchor="w", pady=(0, 1))
        make_button(act_frame, "◈  SHAPE BUILDER",
                    self._open_shape_builder, "info").pack(fill="x", pady=1)
        self._shape_lbl = ttk.Label(act_frame, text="  no shape override",
                                    font=("Segoe UI", 7), foreground="#666666")
        self._shape_lbl.pack(anchor="w")
        make_button(act_frame, "〰  DRAW ROUTING HINTS",
                    self._open_hint_tool, "info").pack(fill="x", pady=1)
        self._hint_lbl = ttk.Label(act_frame, text="  no hints",
                                   font=("Segoe UI", 7), foreground="#666666")
        self._hint_lbl.pack(anchor="w")
        ttk.Separator(act_frame).pack(fill="x", pady=3)
        # ── Step 2: Process ──────────────────────────────────────────────
        ttk.Label(act_frame, text="② PROCESS",
                  font=("Segoe UI", 7, "bold"),
                  foreground="#888888").pack(anchor="w", pady=(0, 1))
        make_button(act_frame, "PROCESS GLOBAL MAP",
                    self._process_global, "primary").pack(fill="x", pady=2)
        ttk.Separator(act_frame).pack(fill="x", pady=3)
        # ── Step 3: Refine ───────────────────────────────────────────────
        ttk.Label(act_frame, text="③ REFINE",
                  font=("Segoe UI", 7, "bold"),
                  foreground="#888888").pack(anchor="w", pady=(0, 1))
        make_button(act_frame, "DRAW EDIT BOX",
                    self._start_box_draw, "warning").pack(fill="x", pady=2)
        make_button(act_frame, "OPEN PRECISION EDITOR",
                    self._open_precision_editor, "success").pack(fill="x", pady=2)
        make_button(act_frame, "APPLY EDITS TO GLOBAL MAP",
                    self._apply_precision, "success").pack(fill="x", pady=2)
        ttk.Separator(act_frame).pack(fill="x", pady=3)
        # ── Step 4: Draw ─────────────────────────────────────────────────
        ttk.Label(act_frame, text="④ DRAW",
                  font=("Segoe UI", 7, "bold"),
                  foreground="#888888").pack(anchor="w", pady=(0, 1))
        make_button(act_frame, "✏  PENCIL TOOL — Draw Lines",
                    self._open_pencil_tool, "warning").pack(fill="x", pady=2)
        ttk.Separator(act_frame).pack(fill="x", pady=3)
        # ── Step 5: Export ───────────────────────────────────────────────
        make_button(act_frame, "EXPORT FINAL GeoJSON",
                    self._export, "secondary").pack(fill="x", pady=2)

        # Parameters section (bottom, fixed) – also before filters
        param_frame = ttk.LabelFrame(lf, text=" Processing Parameters ", padding="6")
        param_frame.pack(fill="x", side="bottom", **pad)

        # Auto-tune toggle – OFF by default so manual sliders are active
        self._auto_var = tk.BooleanVar(value=False)
        cb_kw: dict = {
            "text": "Auto-Tune per segment",
            "variable": self._auto_var,
            "command": self._toggle_auto,
        }
        if BOOTSTRAP:
            cb_kw["bootstyle"] = "success-round-toggle"
        ttk.Checkbutton(param_frame, **cb_kw).pack(anchor="w", pady=(0, 4))

        # Algorithm selector (shown only in manual mode)
        algo_row = ttk.Frame(param_frame)
        algo_row.pack(fill="x", pady=(0, 4))
        ttk.Label(algo_row, text="Algorithm:", font=("Segoe UI", 8)).pack(
            side="left", padx=(0, 4))
        self._algo_combo = ttk.Combobox(
            algo_row, textvariable=self._manual_algo,
            values=METHOD_LABELS, state="readonly", width=18)
        self._algo_combo.pack(side="left")
        self._algo_row = algo_row

        self._s_prune    = SliderRow(param_frame, "Pruning",
                                     0.0,    1.0,    0.10,   0.005,   "{:.3f}")
        self._s_straight = SliderRow(param_frame, "Straighten",
                                     0.0,    0.0002, 0.00002, 0.000002, "{:.6f}")
        self._s_smooth   = SliderRow(param_frame, "Smoothing",
                                     0,      5,      2,       1,       "{:.0f}",
                                     on_change=self._on_smooth_change)
        self._s_minlen   = SliderRow(param_frame, "Min Line Length",
                                     0.0,    0.01,   0.0,    0.0001,  "{:.4f}",
                                     on_change=self._on_minlen_change)
        self._s_deadend  = SliderRow(param_frame, "Dead-end Branch Pruning",
                                     0.0,    0.02,   0.0,    0.0002,  "{:.4f}",
                                     on_change=self._on_minlen_change)
        self._toggle_auto()

        # Filters section (expands to fill remaining space)
        filt_frame = ttk.LabelFrame(lf, text=" Filters ", padding="6")
        filt_frame.pack(fill="both", expand=True, **pad)
        list_box = ttk.Frame(filt_frame)
        list_box.pack(fill="both", expand=True)
        self.plan_list = DragSelectChecklist(list_box, "Plans")
        self.type_list = DragSelectChecklist(list_box, "Road Types")

    # ── right canvas ──────────────────────────────────────────────────────────

    def _build_canvas(self) -> None:
        bg = C_BG_DARK if BOOTSTRAP else "white"
        self._fig = Figure(figsize=(8, 6), facecolor=bg)
        ax = self._fig.add_subplot(111)
        ax.set_facecolor(C_BG_DARK if BOOTSTRAP else C_BG_LIGHT)
        ax.tick_params(colors="#aaaaaa")
        ax.text(0.5, 0.5, "Load a GeoJSON file to begin",
                ha="center", va="center",
                color="#666666", fontsize=13, transform=ax.transAxes)
        self._ax = ax

        self._cv = FigureCanvasTkAgg(self._fig, master=self._right)
        try:
            toolbar = NavigationToolbar2Tk(self._cv, self._right, pack_toolbar=False)
        except TypeError:
            toolbar = NavigationToolbar2Tk(self._cv, self._right)
        toolbar.update()
        toolbar.pack(side="bottom", fill="x")
        self._cv.get_tk_widget().pack(fill="both", expand=True)
        self._cv.draw()

    # ═════════════════════════════════════════════════════════════════════════
    # FILE LOADING
    # ═════════════════════════════════════════════════════════════════════════

    def _load_file(self) -> None:
        path = filedialog.askopenfilename(
            filetypes=[("GeoJSON / JSON", "*.geojson *.json"),
                       ("All files", "*.*")])
        if not path:
            return
        try:
            self.gdf = gpd.read_file(path)
        except Exception as exc:
            messagebox.showerror("Load Error", str(exc))
            return

        self._file_lbl.config(text=os.path.basename(path))
        self.statusbar.set_message(f"Loaded {len(self.gdf)} features")

        plans = (sorted(self.gdf["pl_number"].dropna().unique().astype(str))
                 if "pl_number" in self.gdf.columns else [])
        types = (sorted(self.gdf["mavat_name"].dropna().unique().astype(str))
                 if "mavat_name" in self.gdf.columns else [])

        self.plan_list.populate(plans, lambda _: True)
        self.type_list.populate(types, lambda x: "דרך" in x)

    # ═════════════════════════════════════════════════════════════════════════
    # PARAMETERS / AUTO-TUNE
    # ═════════════════════════════════════════════════════════════════════════

    def _toggle_auto(self) -> None:
        state = "disabled" if self._auto_var.get() else "normal"
        for sl in (self._s_prune, self._s_straight, self._s_smooth):
            sl.configure(state=state)
        # Algorithm selector only useful in manual mode
        self._algo_combo.configure(
            state="disabled" if self._auto_var.get() else "readonly")

    def _on_smooth_change(self, val: float) -> None:
        """Live re-smooth without re-running the expensive centerline step."""
        if self._auto_var.get() or not self._raw_lines:
            return
        smoothed = apply_smoothing(self._raw_lines, int(val))
        self.master_lines = self._apply_minlen(smoothed)
        self._redraw_map()

    def _on_minlen_change(self, val: float) -> None:
        """Live length filter — drops spikes below the threshold instantly."""
        if not self._raw_lines:
            return
        smoothed = apply_smoothing(self._raw_lines, int(self._s_smooth.get()))
        self.master_lines = self._apply_minlen(smoothed)
        self._redraw_map()

    def _apply_minlen(self, lines: list) -> list:
        """Apply min-length filter then iterative dead-end branch pruning."""
        # 1. Drop all lines shorter than the absolute minimum
        minlen = self._s_minlen.get()
        if minlen > 0:
            lines = [l for l in lines if l.length >= minlen]

        # 2. Iteratively prune dangling branches (combs/spikes at junctions)
        deadend = self._s_deadend.get()
        if deadend > 0:
            lines = prune_dead_ends(lines, deadend)

        return lines

    # ═════════════════════════════════════════════════════════════════════════
    # DATA HELPERS
    # ═════════════════════════════════════════════════════════════════════════

    def _get_filtered_gdf(self) -> gpd.GeoDataFrame | None:
        if self.gdf is None:
            return None
        plans = self.plan_list.get_selected()
        types = self.type_list.get_selected()
        mask = pd.Series(True, index=self.gdf.index)
        if plans and "pl_number" in self.gdf.columns:
            mask &= self.gdf["pl_number"].astype(str).isin(plans)
        if types and "mavat_name" in self.gdf.columns:
            mask &= self.gdf["mavat_name"].astype(str).isin(types)
        df = self.gdf[mask].copy()
        return df if not df.empty else None

    # ═════════════════════════════════════════════════════════════════════════
    # GLOBAL PROCESSING
    # ═════════════════════════════════════════════════════════════════════════

    def _process_global(self) -> None:
        # Use shape-builder result if available, otherwise fall back to filters
        if self._shape_built_geoms is not None:
            crs = self.gdf.crs if self.gdf is not None else None
            df = gpd.GeoDataFrame(geometry=self._shape_built_geoms, crs=crs)
            if df.empty:
                messagebox.showwarning("No data", "Shape Builder has no active polygons.")
                return
        else:
            df = self._get_filtered_gdf()
            if df is None:
                messagebox.showwarning("No data",
                                       "No features match the current filters.")
                return

        self._set_busy(True, "Processing road segments…")
        use_auto  = self._auto_var.get()
        prune     = self._s_prune.get()
        straight  = self._s_straight.get()
        smooth    = int(self._s_smooth.get())
        algo      = self._manual_algo.get()
        hints     = list(self._hint_lines)
        snap_tol  = self._hint_snap_tol

        def _work():
            def _cb(cur, tot, info):
                a = info.get("algorithm", "")
                label = f"Segment {cur}/{tot}" + (f" · {a}" if a else "")
                self._queue.put(("progress", cur / tot * 100, label))
            return process_segments(
                df, use_auto=use_auto,
                manual_prune=prune, manual_straight=straight,
                manual_smooth=smooth, manual_algorithm=algo,
                progress_cb=_cb,
                hint_lines=hints, hint_snap_tol=snap_tol)

        self._launch_thread(_work, self._on_global_done)

    def _on_global_done(self, result) -> None:
        lines, report = result if isinstance(result, tuple) else (result, [])
        self._method_report = report
        self._raw_lines   = [l for l in lines if l is not None and not l.is_empty]
        self.master_lines = self._apply_minlen(self._raw_lines)
        n = len(self.master_lines)

        # Build method summary for status bar
        from collections import Counter
        algo_counts = Counter(r["algorithm"] for r in report)
        if algo_counts:
            summary = "  ·  ".join(
                f"{cnt}× {algo}" for algo, cnt in algo_counts.most_common())
        else:
            summary = ""
        msg = f"Done — {n} segment{'s' if n != 1 else ''}"
        if summary:
            msg += f"  [{summary}]"

        self._set_busy(False, msg)
        if not self.master_lines:
            messagebox.showwarning("No results",
                                   "No centerlines could be extracted.\n"
                                   "Try adjusting filters or parameters.")
            return
        self._redraw_map()

    # ═════════════════════════════════════════════════════════════════════════
    # MAP RENDERING (embedded canvas)
    # ═════════════════════════════════════════════════════════════════════════

    def _redraw_map(self) -> None:
        ax = self._ax
        ax.clear()
        ax.set_facecolor(C_BG_DARK if BOOTSTRAP else C_BG_LIGHT)

        if self.master_lines:
            for line in self.master_lines:
                x, y = line.xy
                ax.plot(x, y, color=C_GLOBAL, linewidth=1.2, alpha=0.85)

        if self.precision_lines:
            for line in self.precision_lines:
                x, y = line.xy
                ax.plot(x, y, color=C_PRECISION, linewidth=2.0, alpha=0.9)

        if self._hint_lines:
            for hl in self._hint_lines:
                x, y = hl.xy
                ax.plot(x, y, color=C_HINT, linewidth=1.8,
                        linestyle="--", alpha=0.80, zorder=4)

        if self.selected_box is not None:
            bx, by = self.selected_box.exterior.xy
            ax.plot(bx, by, color=C_BOX, linewidth=1.5,
                    linestyle="--", alpha=0.75)

        title_color = "#cccccc" if BOOTSTRAP else "black"
        n_global = len(self.master_lines)
        n_prec   = len(self.precision_lines)
        ax.set_title(
            f"Road Centerlines  ·  {n_global} global  ·  {n_prec} precision",
            color=title_color, fontsize=10)
        ax.tick_params(colors="#888888")
        self._fig.tight_layout()
        self._cv.draw()

    # ═════════════════════════════════════════════════════════════════════════
    # BOX SELECTION (in-canvas RectangleSelector)
    # ═════════════════════════════════════════════════════════════════════════

    def _start_box_draw(self) -> None:
        if not self.master_lines:
            messagebox.showinfo("Info", "Process the global map first.")
            return

        # Temporarily update title to guide the user
        self._ax.set_title(
            "Draw a box over the area to refine  ·  then press ENTER",
            color="#f39c12", fontsize=10)
        self._cv.draw()

        self._temp_box = None

        def _on_select(eclick, erelease):
            x0, y0 = eclick.xdata, eclick.ydata
            x1, y1 = erelease.xdata, erelease.ydata
            if None in (x0, y0, x1, y1):
                return
            from shapely.geometry import box
            self._temp_box = box(min(x0, x1), min(y0, y1),
                                 max(x0, x1), max(y0, y1))

        def _on_key(event):
            if event.key == "enter" and self._temp_box is not None:
                self.selected_box = self._temp_box
                if self._rs:
                    self._rs.set_active(False)
                self._fig.canvas.mpl_disconnect(self._key_cid)
                self._redraw_map()
                messagebox.showinfo(
                    "Edit box confirmed",
                    "Box saved.  Open Precision Editor to process this area.")

        self._rs = RectangleSelector(
            self._ax, _on_select,
            useblit=True, button=[1],
            minspanx=0, minspany=0, spancoords="data", interactive=True)
        self._key_cid = self._fig.canvas.mpl_connect("key_press_event", _on_key)

    # ═════════════════════════════════════════════════════════════════════════
    # PRECISION EDITOR (Toplevel with embedded Matplotlib eraser)
    # ═════════════════════════════════════════════════════════════════════════

    def _open_precision_editor(self) -> None:
        if self.selected_box is None:
            messagebox.showinfo("Info", "Draw an edit box first.")
            return
        df = self._get_filtered_gdf()
        if df is None:
            messagebox.showinfo("Info", "No features match the filters.")
            return

        clipped = gpd.clip(df, self.selected_box.buffer(1e-7))
        if clipped.empty:
            messagebox.showinfo("Info", "No features inside the selected box.")
            return

        use_auto = self._auto_var.get()
        prune    = self._s_prune.get()
        straight = self._s_straight.get()
        smooth   = int(self._s_smooth.get())

        self._set_busy(True, "Processing precision area…")

        def _work():
            def _cb(c, t, info):
                a = info.get("algorithm", "")
                label = f"Segment {c}/{t}" + (f" · {a}" if a else "")
                self._queue.put(("progress", c / t * 100, label))
            return process_segments(
                clipped, use_auto=use_auto,
                manual_prune=prune, manual_straight=straight,
                manual_smooth=smooth,
                manual_algorithm=self._manual_algo.get(),
                progress_cb=_cb)

        def _done(result) -> None:
            lines, _ = result if isinstance(result, tuple) else (result, [])
            raw = [l for l in lines if l is not None and not l.is_empty]
            self.precision_lines = self._apply_minlen(raw)
            self.eraser_history.clear()
            self._set_busy(False,
                           f"Precision: {len(self.precision_lines)} lines")
            self._show_eraser(clipped)

        self._launch_thread(_work, _done)

    def _show_eraser(self, background_gdf: gpd.GeoDataFrame) -> None:
        """Open a Toplevel eraser window with pick-to-delete and Ctrl+Z undo."""
        top = tk.Toplevel(self.root)
        top.title("Precision Eraser")
        top.geometry("920x720")

        # Info bar
        info_bar = ttk.Frame(top, padding="4")
        info_bar.pack(fill="x")
        ttk.Label(info_bar,
                  text="  Click a line to erase it  ·  Ctrl+Z (or Z) to undo  ·  "
                       "Close window, then click 'Apply Edits to Global Map'",
                  font=("Segoe UI", 9)).pack(side="left")

        # Matplotlib canvas
        bg = C_BG_DARK if BOOTSTRAP else "white"
        fig = Figure(figsize=(8, 6), facecolor=bg)
        ax  = fig.add_subplot(111)
        ax.set_facecolor(C_BG_DARK if BOOTSTRAP else C_BG_LIGHT)

        cv = FigureCanvasTkAgg(fig, master=top)
        try:
            tb = NavigationToolbar2Tk(cv, top, pack_toolbar=False)
        except TypeError:
            tb = NavigationToolbar2Tk(cv, top)
        tb.update()
        tb.pack(side="bottom", fill="x")
        cv.get_tk_widget().pack(fill="both", expand=True)

        def redraw():
            ax.clear()
            ax.set_facecolor(C_BG_DARK if BOOTSTRAP else C_BG_LIGHT)

            # Show clipped polygon outlines as background
            try:
                background_gdf.plot(ax=ax, color="#2a3a5a", alpha=0.35,
                                    edgecolor="#444466", linewidth=0.5)
            except Exception:
                pass

            # Edit-box boundary
            if self.selected_box is not None:
                bx, by = self.selected_box.exterior.xy
                ax.plot(bx, by, color=C_BOX, linewidth=1.2, linestyle="--")

            # Clickable lines
            for i, line in enumerate(self.precision_lines):
                x, y = line.xy
                ax.plot(x, y, color=C_PRECISION, linewidth=2.5,
                        picker=6, label=str(i))

            n = len(self.precision_lines)
            ax.set_title(f"Eraser  ·  {n} line{'s' if n != 1 else ''} remaining",
                         color="#eeeeee" if BOOTSTRAP else "black")
            cv.draw()

        def on_pick(event):
            try:
                idx = int(event.artist.get_label())
            except (ValueError, AttributeError):
                return
            if 0 <= idx < len(self.precision_lines):
                self.eraser_history.append((idx, self.precision_lines[idx]))
                self.precision_lines.pop(idx)
                redraw()

        def on_key(event):
            if event.key in ("ctrl+z", "z") and self.eraser_history:
                idx, line = self.eraser_history.pop()
                self.precision_lines.insert(idx, line)
                redraw()

        fig.canvas.mpl_connect("pick_event", on_pick)
        fig.canvas.mpl_connect("key_press_event", on_key)
        redraw()

    # ═════════════════════════════════════════════════════════════════════════
    # APPLY PRECISION → GLOBAL
    # ═════════════════════════════════════════════════════════════════════════

    def _apply_precision(self) -> None:
        if self.selected_box is None or not self.precision_lines:
            messagebox.showinfo("Info",
                                "Nothing to apply.\n"
                                "Draw a box, open the Precision Editor, "
                                "then return here.")
            return

        # 1. Cut the edit region out of the global master
        if self.master_lines:
            master_union = unary_union(self.master_lines)
            remaining_geom = master_union.difference(
                self.selected_box.buffer(1e-8))
        else:
            remaining_geom = None

        # 2. Flatten remaining geometry to individual LineStrings
        remaining: list[LineString] = []
        if remaining_geom and not remaining_geom.is_empty:
            geoms = (list(remaining_geom.geoms)
                     if hasattr(remaining_geom, "geoms")
                     else [remaining_geom])
            for g in geoms:
                if isinstance(g, LineString):
                    remaining.append(g)
                elif isinstance(g, MultiLineString):
                    remaining.extend(g.geoms)

        # 3. Snap precision lines to endpoints of the remaining blue network
        if remaining:
            pts: list[Point] = []
            for g in remaining:
                c = list(g.coords)
                if len(c) >= 2:
                    pts.extend([Point(c[0]), Point(c[-1])])
            cloud = unary_union(pts)
            snapped = [snap(l, cloud, 0.0001) for l in self.precision_lines]
        else:
            snapped = list(self.precision_lines)

        # 4. Merge back
        self.master_lines = remaining + snapped
        self._raw_lines   = list(self.master_lines)
        self.precision_lines = []
        self._redraw_map()
        messagebox.showinfo("Applied",
                            f"Merged {len(snapped)} precision lines "
                            f"into the global map.")

    # ═════════════════════════════════════════════════════════════════════════
    # SHAPE BUILDER  (polygon preview + merge before centerline extraction)
    # ═════════════════════════════════════════════════════════════════════════

    def _open_shape_builder(self) -> None:
        """
        Interactive polygon preview with Illustrator Shape-Builder-style merging.

        Interactions
        ------------
        Left-click       → select / deselect a polygon
        Left-drag        → box-select all polygons whose centroid falls inside
        Right-click      → toggle exclude (red) / restore a polygon
        Ctrl+click       → add to selection without clearing others
        Merge Selected   → union all selected polygons into one
        Ctrl+Z           → undo last merge
        Reset            → restore original filtered polygons
        ▶ Use These      → send current polygons to centerline processing
        """
        df = self._get_filtered_gdf()
        if df is None or df.empty:
            messagebox.showinfo("Info", "Load a file and apply filters first.")
            return

        top = tk.Toplevel(self.root)
        top.title("◈  Shape Builder — Merge / Exclude Polygons")
        top.geometry("1100x840")
        top.resizable(True, True)

        # ── mutable state ─────────────────────────────────────────────────
        items: list[dict] = [
            {"geom": geom, "sel": False, "excl": False}
            for geom in df.geometry
            if geom is not None and not geom.is_empty
        ]
        history: list[list[dict]] = []   # undo stack (full snapshots)
        drag_origin: list = [None]        # [x0, y0, button] or None
        drag_rect_art: list = [None]      # matplotlib Rectangle artist

        # ── canvas ────────────────────────────────────────────────────────
        fig_bg = C_BG_DARK if BOOTSTRAP else "white"
        fig    = Figure(figsize=(10, 7), facecolor=fig_bg)
        ax     = fig.add_subplot(111)
        ax.set_facecolor(C_BG_DARK if BOOTSTRAP else C_BG_LIGHT)

        cv = FigureCanvasTkAgg(fig, master=top)
        try:
            tb_w = NavigationToolbar2Tk(cv, top, pack_toolbar=False)
        except TypeError:
            tb_w = NavigationToolbar2Tk(cv, top)
        tb_w.update()
        tb_w.pack(side="bottom", fill="x")

        # ── info bar ──────────────────────────────────────────────────────
        info = ttk.Frame(top, padding="4 3")
        info.pack(fill="x", side="top")
        ttk.Label(
            info,
            text="  Left-click = select  ·  Drag = box-select  ·  "
                 "Right-click = exclude/restore  ·  Ctrl+Z = undo merge",
            font=("Segoe UI", 9),
        ).pack(side="left")
        stat_lbl = ttk.Label(info, font=("Segoe UI", 9, "bold"))
        stat_lbl.pack(side="right", padx=8)

        # ── button bar ────────────────────────────────────────────────────
        btn_bar = ttk.Frame(top, padding="6")
        btn_bar.pack(fill="x", side="bottom")
        cv.get_tk_widget().pack(fill="both", expand=True)

        # ── helpers ───────────────────────────────────────────────────────

        def _snapshot():
            import copy
            return [{"geom": it["geom"],
                     "sel": it["sel"], "excl": it["excl"]} for it in items]

        def _full_redraw():
            ax.clear()
            ax.set_facecolor(C_BG_DARK if BOOTSTRAP else C_BG_LIGHT)
            tc = "#dddddd" if BOOTSTRAP else "black"

            n_sel  = sum(1 for it in items if it["sel"] and not it["excl"])
            n_act  = sum(1 for it in items if not it["excl"])
            n_excl = sum(1 for it in items if it["excl"])
            area   = sum(it["geom"].area for it in items if not it["excl"])

            for it in items:
                try:
                    gdf_tmp = gpd.GeoDataFrame(geometry=[it["geom"]])
                    if it["excl"]:
                        gdf_tmp.plot(ax=ax, color=C_POLY_EXC,
                                     edgecolor="#ff8888", alpha=0.40, linewidth=0.8)
                    elif it["sel"]:
                        gdf_tmp.plot(ax=ax, color=C_POLY_SEL,
                                     edgecolor="#ffffff", alpha=0.80, linewidth=1.2)
                    else:
                        gdf_tmp.plot(ax=ax, color=C_POLY_ACT,
                                     edgecolor="#88bbdd", alpha=0.50, linewidth=0.6)
                except Exception:
                    pass

            ax.set_title(
                f"{n_act} active  ·  {n_excl} excluded  ·  "
                f"{n_sel} selected  ·  area = {area:.6f}",
                color=tc, fontsize=9)
            stat_lbl.config(
                text=f"{n_act} polygons  |  {n_sel} selected  |  area {area:.5f}")
            cv.draw()

        def _hit(x, y) -> int | None:
            pt = Point(x, y)
            for i, it in enumerate(items):
                try:
                    if it["geom"].contains(pt):
                        return i
                    # Tolerance for thin / boundary clicks
                    if it["geom"].distance(pt) < 1e-6:
                        return i
                except Exception:
                    pass
            return None

        def _on_press(event):
            if tb_w.mode or event.inaxes != ax:
                return
            if event.xdata is None:
                return
            drag_origin[0] = (event.xdata, event.ydata, event.button)

        def _on_release(event):
            if tb_w.mode or event.inaxes != ax:
                drag_origin[0] = None
                return
            if drag_origin[0] is None or event.xdata is None:
                return

            x0, y0, btn = drag_origin[0]
            drag_origin[0] = None
            dx = abs(event.xdata - x0)
            dy = abs(event.ydata - y0)
            ctrl = (event.key == "control") if hasattr(event, "key") else False

            if dx < 1e-9 and dy < 1e-9:
                # Single click
                idx = _hit(event.xdata, event.ydata)
                if idx is not None:
                    if btn == 1:
                        if not ctrl:
                            # Clear selection unless ctrl held
                            for it in items:
                                it["sel"] = False
                        items[idx]["sel"] = not items[idx]["sel"]
                        items[idx]["excl"] = False
                    elif btn == 3:
                        items[idx]["excl"] = not items[idx]["excl"]
                        items[idx]["sel"] = False
            else:
                # Drag → box-select
                bx = shapely_box(min(x0, event.xdata), min(y0, event.ydata),
                                 max(x0, event.xdata), max(y0, event.ydata))
                if btn == 1:
                    if not ctrl:
                        for it in items:
                            it["sel"] = False
                    for it in items:
                        if not it["excl"]:
                            try:
                                if bx.contains(it["geom"].centroid):
                                    it["sel"] = True
                            except Exception:
                                pass
            _full_redraw()

        def _on_key_sb(event):
            if event.key in ("ctrl+z", "z"):
                _undo()

        def _merge_selected():
            sel = [it for it in items if it["sel"] and not it["excl"]]
            if len(sel) < 2:
                messagebox.showinfo("Shape Builder",
                                    "Select at least 2 polygons to merge.")
                return
            history.append(_snapshot())
            merged = unary_union([it["geom"] for it in sel])
            new_items = [it for it in items
                         if not (it["sel"] and not it["excl"])]
            new_items.append({"geom": merged, "sel": False, "excl": False})
            items.clear()
            items.extend(new_items)
            _full_redraw()

        def _undo():
            if history:
                items.clear()
                items.extend(history.pop())
                _full_redraw()

        def _toggle_all(state: bool):
            for it in items:
                if not it["excl"]:
                    it["sel"] = state
            _full_redraw()

        def _reset():
            history.clear()
            items.clear()
            items.extend(
                {"geom": geom, "sel": False, "excl": False}
                for geom in df.geometry
                if geom is not None and not geom.is_empty
            )
            _full_redraw()

        def _use_these():
            active = [it["geom"] for it in items if not it["excl"]]
            if not active:
                messagebox.showinfo("Shape Builder", "No active polygons.")
                return
            self._shape_built_geoms = active
            n   = len(active)
            area = sum(g.area for g in active)
            self._shape_lbl.config(
                text=f"  {n} polygon{'s' if n != 1 else ''}  area={area:.5f}",
                foreground=C_POLY_SEL)
            self.statusbar.set_message(
                f"Shape Builder: {n} polygons ready — click 'Process Global Map'")
            top.destroy()

        def _clear_override():
            self._shape_built_geoms = None
            self._shape_lbl.config(text="  no shape override",
                                   foreground="#666666")
            _full_redraw()

        # Build button bar now (functions defined)
        make_button(btn_bar, "Select All",
                    lambda: _toggle_all(True),  "outline-success").pack(side="left", padx=2)
        make_button(btn_bar, "Deselect All",
                    lambda: _toggle_all(False), "outline-secondary").pack(side="left", padx=2)
        make_button(btn_bar, "Merge Selected",
                    _merge_selected, "warning").pack(side="left", padx=6)
        make_button(btn_bar, "Undo Merge",
                    _undo,          "outline-warning").pack(side="left", padx=2)
        make_button(btn_bar, "Reset",
                    _reset,         "danger").pack(side="left", padx=6)
        make_button(btn_bar, "Clear Override",
                    _clear_override, "outline-secondary").pack(side="left", padx=2)
        make_button(btn_bar, "▶  Use These Polygons",
                    _use_these,     "primary").pack(side="right", padx=4)

        fig.canvas.mpl_connect("button_press_event",   _on_press)
        fig.canvas.mpl_connect("button_release_event", _on_release)
        fig.canvas.mpl_connect("key_press_event",      _on_key_sb)
        _full_redraw()

    # ═════════════════════════════════════════════════════════════════════════
    # HINT TOOL  (draw routing hints applied as skeleton guidance)
    # ═════════════════════════════════════════════════════════════════════════

    def _open_hint_tool(self) -> None:
        """
        Draw routing hint strokes on top of the map.

        Hint strokes are stored as LineStrings and snapped-to during
        centerline extraction (post-processing via shapely.snap).
        They work in both Auto and Manual processing modes.

        Interactions
        ------------
        Click to place vertices  ·  Double-click / Enter = commit stroke
        Z = undo vertex  ·  Esc = cancel current stroke
        Snap Tolerance slider controls how strongly hints pull centerlines.
        """
        top = tk.Toplevel(self.root)
        top.title("〰  Draw Routing Hints")
        top.geometry("1100x840")
        top.resizable(True, True)

        # ── mutable state ─────────────────────────────────────────────────
        hints: list[LineString]        = list(self._hint_lines)
        pts:   list[tuple[float,float]] = []
        bg_cache:  list = [None]
        rubber_ln: list = [None]

        # ── canvas ────────────────────────────────────────────────────────
        fig_bg = C_BG_DARK if BOOTSTRAP else "white"
        fig    = Figure(figsize=(10, 7), facecolor=fig_bg)
        ax     = fig.add_subplot(111)
        ax.set_facecolor(C_BG_DARK if BOOTSTRAP else C_BG_LIGHT)

        cv = FigureCanvasTkAgg(fig, master=top)
        try:
            tb_h = NavigationToolbar2Tk(cv, top, pack_toolbar=False)
        except TypeError:
            tb_h = NavigationToolbar2Tk(cv, top)
        tb_h.update()
        tb_h.pack(side="bottom", fill="x")

        # ── info bar ──────────────────────────────────────────────────────
        info = ttk.Frame(top, padding="4 3")
        info.pack(fill="x", side="top")
        ttk.Label(
            info,
            text="  Click to place hint vertices  ·  Double-click or Enter to commit  "
                 "·  Z = undo vertex  ·  Esc = cancel stroke",
            font=("Segoe UI", 9),
        ).pack(side="left")
        cnt_lbl = ttk.Label(info, font=("Segoe UI", 9, "bold"))
        cnt_lbl.pack(side="right", padx=8)

        # ── snap-tolerance row ────────────────────────────────────────────
        tol_row = ttk.Frame(top, padding="6 2")
        tol_row.pack(fill="x", side="top")
        ttk.Label(tol_row, text="Snap Tolerance:",
                  font=("Segoe UI", 8, "bold")).pack(side="left", padx=(4, 6))
        tol_val = ttk.Label(tol_row, text=f"{self._hint_snap_tol:.4f}")
        tol_val.pack(side="left")
        tol_scale = tk.Scale(
            tol_row, from_=0.00005, to=0.002, resolution=0.00005,
            orient="horizontal", showvalue=False, length=180,
            command=lambda v: (
                tol_val.config(text=f"{float(v):.5f}"),
                self.__setattr__("_hint_snap_tol", float(v)),
            ))
        tol_scale.set(self._hint_snap_tol)
        tol_scale.pack(side="left", padx=8)
        ttk.Label(tol_row,
                  text="(higher = centerlines pulled harder toward hints)",
                  font=("Segoe UI", 7), foreground="#888888").pack(side="left")

        # ── button bar ────────────────────────────────────────────────────
        btn_bar = ttk.Frame(top, padding="6")
        btn_bar.pack(fill="x", side="bottom")
        cv.get_tk_widget().pack(fill="both", expand=True)

        # ── drawing helpers ───────────────────────────────────────────────

        def _full_redraw():
            ax.clear()
            ax.set_facecolor(C_BG_DARK if BOOTSTRAP else C_BG_LIGHT)
            tc = "#dddddd" if BOOTSTRAP else "black"

            # Background: master lines (if processed)
            for line in self.master_lines:
                x, y = line.xy
                ax.plot(x, y, color=C_GLOBAL, linewidth=1.0, alpha=0.35, zorder=1)

            # Existing hint strokes
            for hl in hints:
                x, y = hl.xy
                ax.plot(x, y, color=C_HINT, linewidth=2.2,
                        linestyle="--", alpha=0.90, zorder=3)

            # In-progress stroke
            if pts:
                px = [p[0] for p in pts]
                py = [p[1] for p in pts]
                ax.plot(px, py, color=C_HINT, linewidth=2.2, zorder=4)
                ax.plot(px, py, "o", color="#ffffff", markersize=6, zorder=5)

            n  = len(hints)
            st = (f"drawing… {len(pts)} point{'s' if len(pts) != 1 else ''}"
                  if pts else "click to start a new hint stroke")
            ax.set_title(
                f"{n} hint stroke{'s' if n != 1 else ''}  ·  {st}  "
                f"·  snap tol = {self._hint_snap_tol:.5f}",
                color=tc, fontsize=9)
            cnt_lbl.config(text=f"{n} stroke{'s' if n != 1 else ''}")

            rubber_ln[0], = ax.plot(
                [], [], color=C_HINT, linewidth=1.5,
                linestyle=":", alpha=0.80, zorder=6)
            cv.draw()
            bg_cache[0] = fig.canvas.copy_from_bbox(ax.bbox)

        def _rubber(x, y):
            if bg_cache[0] is None or not pts or rubber_ln[0] is None:
                return
            fig.canvas.restore_region(bg_cache[0])
            rubber_ln[0].set_data([pts[-1][0], x], [pts[-1][1], y])
            ax.draw_artist(rubber_ln[0])
            fig.canvas.blit(ax.bbox)

        def _commit():
            if len(pts) < 2:
                pts.clear()
                _full_redraw()
                return
            hints.append(LineString(list(pts)))
            pts.clear()
            _full_redraw()

        def _on_click(event):
            if tb_h.mode or event.inaxes != ax:
                return
            if event.xdata is None:
                return
            if event.dblclick:
                _commit()
            else:
                pts.append((event.xdata, event.ydata))
                _full_redraw()

        def _on_move(event):
            if event.inaxes != ax or not pts:
                return
            if event.xdata is None:
                return
            _rubber(event.xdata, event.ydata)

        def _on_key_h(event):
            k = event.key
            if k == "enter" and len(pts) >= 2:
                _commit()
            elif k in ("z", "ctrl+z") and pts:
                pts.pop()
                _full_redraw()
            elif k == "escape":
                pts.clear()
                _full_redraw()

        def _undo_stroke():
            if hints:
                hints.pop()
                _full_redraw()

        def _clear_hints():
            hints.clear()
            pts.clear()
            _full_redraw()

        def _apply_close():
            self._hint_lines = list(hints)
            n = len(hints)
            if n:
                self._hint_lbl.config(
                    text=f"  {n} hint stroke{'s' if n != 1 else ''}  "
                         f"tol={self._hint_snap_tol:.5f}",
                    foreground=C_HINT)
            else:
                self._hint_lbl.config(text="  no hints",
                                      foreground="#666666")
            self._redraw_map()
            top.destroy()

        # Build button bar (functions defined above)
        make_button(btn_bar, "Undo Last Stroke",
                    _undo_stroke, "warning").pack(side="left", padx=4)
        make_button(btn_bar, "Clear All Hints",
                    _clear_hints, "danger").pack(side="left", padx=4)
        make_button(btn_bar, "Apply & Close",
                    _apply_close, "primary").pack(side="right", padx=4)

        fig.canvas.mpl_connect("button_press_event",  _on_click)
        fig.canvas.mpl_connect("motion_notify_event", _on_move)
        fig.canvas.mpl_connect("key_press_event",     _on_key_h)
        _full_redraw()

    # ═════════════════════════════════════════════════════════════════════════
    # PENCIL TOOL  (hand-drawn line completion)
    # ═════════════════════════════════════════════════════════════════════════

    def _open_pencil_tool(self) -> None:
        """
        Open a dedicated drawing window.

        Interaction model (Illustrator-style polyline pen):
          Left-click          → place a vertex
          Double-click        → finish & commit the current segment
          Enter               → finish & commit
          Z  /  Ctrl+Z        → undo the last placed vertex
          Escape              → cancel the segment being drawn (keep committed ones)
          Pan / Zoom toolbar  → suspended while a segment is in progress

        Visual feedback:
          Blue  (dim)  → existing master lines (background)
          Yellow line  → segment being drawn (vertices placed so far)
          Red dots     → individual vertices placed
          Dashed line  → rubber-band preview from last vertex to cursor
          Green        → committed segments this session
        """
        if not self.master_lines:
            messagebox.showinfo("Info", "Process the global map first.")
            return

        top = tk.Toplevel(self.root)
        top.title("✏  Pencil Tool — Draw Road Lines")
        top.geometry("1100x840")
        top.resizable(True, True)

        # ── mutable drawing state ─────────────────────────────────────────
        pts: list[tuple[float, float]] = []    # vertices of current segment
        drawn: list[LineString] = []           # committed segments this session
        bg_cache: list = [None]                # saved rasterised background
        rubber_ln: list = [None]               # persistent rubber-band artist

        # ── info bar ──────────────────────────────────────────────────────
        info = ttk.Frame(top, padding="4 3")
        info.pack(fill="x", side="top")
        hint_lbl = ttk.Label(
            info,
            text="  Click to place vertices  ·  Double-click or Enter to finish  "
                 "·  Z = undo vertex  ·  Esc = cancel segment",
            font=("Segoe UI", 9),
        )
        hint_lbl.pack(side="left")
        count_lbl = ttk.Label(info, font=("Segoe UI", 9, "bold"))
        count_lbl.pack(side="right", padx=8)

        # ── bottom button bar ─────────────────────────────────────────────
        btn_bar = ttk.Frame(top, padding="6")
        btn_bar.pack(fill="x", side="bottom")
        make_button(btn_bar, "Undo Last Segment",
                    lambda: _undo_segment(), "warning").pack(side="left", padx=4)
        make_button(btn_bar, "Clear All Drawn",
                    lambda: _clear_all(),   "danger").pack(side="left",  padx=4)
        make_button(btn_bar, "Apply & Close",
                    lambda: _apply_close(), "primary").pack(side="right", padx=4)

        # ── matplotlib canvas ─────────────────────────────────────────────
        fig_bg = C_BG_DARK if BOOTSTRAP else "white"
        fig    = Figure(figsize=(10, 7), facecolor=fig_bg)
        ax     = fig.add_subplot(111)
        ax.set_facecolor(C_BG_DARK if BOOTSTRAP else C_BG_LIGHT)

        cv = FigureCanvasTkAgg(fig, master=top)
        try:
            tb = NavigationToolbar2Tk(cv, top, pack_toolbar=False)
        except TypeError:
            tb = NavigationToolbar2Tk(cv, top)
        tb.update()
        tb.pack(side="bottom", fill="x")
        cv.get_tk_widget().pack(fill="both", expand=True)

        # ── redraw helpers ────────────────────────────────────────────────

        def _full_redraw() -> None:
            """Redraw everything and cache the background for blit rubber-band."""
            ax.clear()
            ax.set_facecolor(C_BG_DARK if BOOTSTRAP else C_BG_LIGHT)
            tc = "#dddddd" if BOOTSTRAP else "black"

            # Existing master lines – dimmed background
            for line in self.master_lines:
                x, y = line.xy
                ax.plot(x, y, color=C_GLOBAL, linewidth=1.0, alpha=0.45, zorder=1)

            # Committed session segments
            for line in drawn:
                x, y = line.xy
                ax.plot(x, y, color=C_PRECISION, linewidth=2.5, alpha=0.95, zorder=3)

            # Current in-progress segment
            if pts:
                px = [p[0] for p in pts]
                py = [p[1] for p in pts]
                ax.plot(px, py, color="#ffd93d", linewidth=2.2, zorder=4)
                ax.plot(px, py, "o", color="#ff6b6b", markersize=7, zorder=5)

            n   = len(drawn)
            st  = (f"drawing… {len(pts)} vertex/vertices — "
                   "double-click or Enter to finish"
                   if pts else "click to start a new segment")
            ax.set_title(
                f"{n} segment{'s' if n != 1 else ''} committed  ·  {st}",
                color=tc, fontsize=9)
            count_lbl.config(text=f"{n} segment{'s' if n != 1 else ''}")

            # Add the rubber-band artist (invisible) BEFORE caching background
            rubber_ln[0], = ax.plot(
                [], [], color="#ffd93d", linewidth=1.5,
                linestyle="--", alpha=0.85, zorder=6)

            cv.draw()
            bg_cache[0] = fig.canvas.copy_from_bbox(ax.bbox)

        def _update_rubber(x: float, y: float) -> None:
            """Blit only the rubber-band line — no full redraw needed."""
            if bg_cache[0] is None or not pts or rubber_ln[0] is None:
                return
            fig.canvas.restore_region(bg_cache[0])
            rubber_ln[0].set_data([pts[-1][0], x], [pts[-1][1], y])
            ax.draw_artist(rubber_ln[0])
            fig.canvas.blit(ax.bbox)

        # ── finish / commit ────────────────────────────────────────────────

        def _finish_segment() -> None:
            if len(pts) < 2:
                pts.clear()
                _full_redraw()
                return
            coords = list(pts)
            pts.clear()
            # Smooth with the current Smoothing slider value
            smooth = int(self._s_smooth.get())
            if smooth > 0:
                coords = [tuple(p) for p in
                          chaikins_corner_cutting(coords, smooth)]
            drawn.append(LineString(coords))
            _full_redraw()

        # ── matplotlib event handlers ─────────────────────────────────────

        def _on_click(event: object) -> None:
            if tb.mode:                  # pan/zoom tool active → ignore
                return
            if event.inaxes != ax:
                return
            if event.xdata is None or event.ydata is None:
                return

            if event.dblclick:
                # Single-click already added the point; just finish.
                _finish_segment()
            else:
                pts.append((event.xdata, event.ydata))
                _full_redraw()

        def _on_move(event: object) -> None:
            if event.inaxes != ax or not pts:
                return
            if event.xdata is None or event.ydata is None:
                return
            _update_rubber(event.xdata, event.ydata)

        def _on_key(event: object) -> None:
            k = event.key
            if k == "enter" and len(pts) >= 2:
                _finish_segment()
            elif k in ("z", "ctrl+z") and pts:
                pts.pop()
                _full_redraw()
            elif k == "escape":
                pts.clear()
                _full_redraw()

        # ── button callbacks ──────────────────────────────────────────────

        def _undo_segment() -> None:
            if drawn:
                drawn.pop()
                _full_redraw()

        def _clear_all() -> None:
            drawn.clear()
            pts.clear()
            _full_redraw()

        def _apply_close() -> None:
            if drawn:
                self.master_lines = list(self.master_lines) + drawn
                self._raw_lines   = list(self.master_lines)
                self._pencil_lines.extend(drawn)
                self._redraw_map()
                n = len(drawn)
                self.statusbar.set_message(
                    f"Added {n} hand-drawn segment{'s' if n != 1 else ''} — "
                    f"{len(self.master_lines)} total")
            top.destroy()

        # ── wire up events ────────────────────────────────────────────────
        fig.canvas.mpl_connect("button_press_event",  _on_click)
        fig.canvas.mpl_connect("motion_notify_event", _on_move)
        fig.canvas.mpl_connect("key_press_event",     _on_key)
        _full_redraw()

    # ═════════════════════════════════════════════════════════════════════════
    # EXPORT
    # ═════════════════════════════════════════════════════════════════════════

    def _export(self) -> None:
        if not self.master_lines:
            messagebox.showinfo("Info",
                                "Nothing to export — process the map first.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".geojson",
            filetypes=[("GeoJSON", "*.geojson"), ("All files", "*.*")],
            initialfile="road_centerlines.geojson")
        if not path:
            return

        self._set_busy(True, "Snapping and exporting…")

        crs = self.gdf.crs if self.gdf is not None else None

        def _work():
            snapped = snap_endpoints(self.master_lines)
            gdf = lines_to_gdf(snapped, crs=crs)
            return export_geojson(gdf, path)

        def _done(n: int) -> None:
            self._set_busy(False, "Export complete")
            messagebox.showinfo("Exported",
                                f"Saved {n} features to:\n{path}")

        self._launch_thread(_work, _done)

    # ═════════════════════════════════════════════════════════════════════════
    # THREADING HELPERS
    # ═════════════════════════════════════════════════════════════════════════

    def _launch_thread(self, work_fn, done_cb) -> None:
        self._done_cb = done_cb

        def _wrapper():
            try:
                result = work_fn()
                self._queue.put(("done", result))
            except Exception as exc:  # noqa: BLE001
                self._queue.put(("error", str(exc)))

        self._thread = threading.Thread(target=_wrapper, daemon=True)
        self._thread.start()
        self.root.after(100, self._poll_queue)

    def _poll_queue(self) -> None:
        last_progress: tuple | None = None
        try:
            while True:
                msg = self._queue.get_nowait()
                if msg[0] == "progress":
                    last_progress = msg
                elif msg[0] == "done":
                    if last_progress:
                        self.statusbar.set_progress(last_progress[1],
                                                    last_progress[2])
                    self._done_cb(msg[1])
                    return
                elif msg[0] == "error":
                    self._set_busy(False, "Error")
                    messagebox.showerror("Processing Error", msg[1])
                    return
        except queue.Empty:
            pass

        if last_progress:
            self.statusbar.set_progress(last_progress[1], last_progress[2])

        if self._thread and self._thread.is_alive():
            self.root.after(100, self._poll_queue)

    def _set_busy(self, active: bool, message: str = "") -> None:
        if message:
            self.statusbar.set_message(message)
        if not active:
            self.root.after(1500, lambda: self.statusbar.set_progress(0))


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if BOOTSTRAP:
        root = ttk.Window(themename=THEME)
    else:
        root = tk.Tk()
        style = ttk.Style(root)
        style.theme_use("clam")

    # Ensure a Unicode-capable font is used everywhere (supports Hebrew)
    root.option_add("*Font", FONT_MAIN)

    app = GISRoadMaster(root)  # noqa: F841
    root.mainloop()


if __name__ == "__main__":
    main()
