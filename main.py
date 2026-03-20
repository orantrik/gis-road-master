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
from shapely.geometry import LineString, MultiLineString, Point
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
    export_geojson,
    lines_to_gdf,
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
C_GLOBAL    = "#4a9eff"   # blue   – global centerlines
C_PRECISION = "#2ecc71"   # green  – precision edits
C_BOX       = "#e74c3c"   # red    – edit-box outline
C_BG_DARK   = "#16213e"
C_BG_LIGHT  = "#f4f4f4"


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

        make_button(act_frame, "PROCESS GLOBAL MAP",
                    self._process_global, "primary").pack(fill="x", pady=2)
        ttk.Separator(act_frame).pack(fill="x", pady=3)
        make_button(act_frame, "DRAW EDIT BOX",
                    self._start_box_draw, "warning").pack(fill="x", pady=2)
        make_button(act_frame, "OPEN PRECISION EDITOR",
                    self._open_precision_editor, "success").pack(fill="x", pady=2)
        make_button(act_frame, "APPLY EDITS TO GLOBAL MAP",
                    self._apply_precision, "success").pack(fill="x", pady=2)
        ttk.Separator(act_frame).pack(fill="x", pady=3)
        make_button(act_frame, "EXPORT FINAL GeoJSON",
                    self._export, "secondary").pack(fill="x", pady=2)

        # Parameters section (bottom, fixed) – also before filters
        param_frame = ttk.LabelFrame(lf, text=" Processing Parameters ", padding="6")
        param_frame.pack(fill="x", side="bottom", **pad)

        # Auto-tune toggle
        self._auto_var = tk.BooleanVar(value=True)
        cb_kw: dict = {
            "text": "Auto-Tune per segment",
            "variable": self._auto_var,
            "command": self._toggle_auto,
        }
        if BOOTSTRAP:
            cb_kw["bootstyle"] = "success-round-toggle"
        ttk.Checkbutton(param_frame, **cb_kw).pack(anchor="w", pady=(0, 6))

        self._s_prune    = SliderRow(param_frame, "Pruning",
                                     0.0,    1.0,    0.15,   0.005,   "{:.3f}")
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

        def _work():
            def _cb(cur, tot, _):
                self._queue.put(("progress", cur / tot * 100,
                                 f"Segment {cur} / {tot}"))
            return process_segments(
                df, use_auto=use_auto,
                manual_prune=prune, manual_straight=straight,
                manual_smooth=smooth, progress_cb=_cb)

        self._launch_thread(_work, self._on_global_done)

    def _on_global_done(self, lines: list) -> None:
        self._raw_lines   = [l for l in lines if l is not None and not l.is_empty]
        self.master_lines = self._apply_minlen(self._raw_lines)
        n = len(self.master_lines)
        self._set_busy(False, f"Done — {n} centerline segment{'s' if n != 1 else ''}")
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
            def _cb(c, t, _):
                self._queue.put(("progress", c / t * 100, f"Segment {c}/{t}"))
            return process_segments(
                clipped, use_auto=use_auto,
                manual_prune=prune, manual_straight=straight,
                manual_smooth=smooth, progress_cb=_cb)

        def _done(lines: list) -> None:
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
