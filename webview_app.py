"""
webview_app.py – Web UI entry point for GIS Road Master.

Starts a Flask server (localhost, random port) then opens it in a
native pywebview window.  All business logic is delegated to the
existing algorithms.py and fbx_export.py — none of those files are
touched.
"""

from __future__ import annotations

import io
import base64
import json
import os
import sys
import threading
import tkinter
import tkinter.filedialog as fd
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import geopandas as gpd
from flask import Flask, jsonify, request, send_from_directory

# Add project root to path so we can import siblings
sys.path.insert(0, str(Path(__file__).parent))
from algorithms import (
    process_segments, apply_smoothing, prune_dead_ends, METHOD_LABELS,
)
from fbx_export import export_fbx

# ─────────────────────────────────────────────────────────────────
# FLASK APP
# ─────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="web", static_url_path="/static")

# ── Shared mutable state (protected by a lock) ────────────────────
import threading as _t
_lock = _t.Lock()
_state: dict = {
    "gdf":          None,
    "raw_lines":    [],
    "master_lines": [],
    "method_report":[],
}


# ── Helpers ───────────────────────────────────────────────────────
def _render_map(width: int, height: int,
                show_polys: bool = True, show_lines: bool = True) -> str:
    """Render current GDF + centerlines to a base-64 PNG data-URL."""
    dpi   = 120
    fw    = max(width,  200) / dpi
    fh    = max(height, 200) / dpi

    fig, ax = plt.subplots(figsize=(fw, fh), dpi=dpi)
    fig.patch.set_facecolor("#060912")
    ax.set_facecolor("#060912")
    ax.set_axis_off()
    for sp in ax.spines.values():
        sp.set_visible(False)

    with _lock:
        gdf   = _state["gdf"]
        lines = _state["master_lines"]

    if gdf is not None and show_polys:
        gdf.plot(ax=ax, color="#0d1e38", edgecolor="#1e3a5f",
                 linewidth=0.6, alpha=0.85)

    if lines and show_lines:
        from geopandas import GeoSeries
        GeoSeries(lines).plot(ax=ax, color="#00d4ff",
                              linewidth=1.4, alpha=0.92)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight",
                facecolor="#060912", dpi=dpi, pad_inches=0.02)
    plt.close(fig)
    buf.seek(0)
    return "data:image/png;base64," + base64.b64encode(buf.read()).decode()


def _unique_col(gdf, names: list[str]) -> list:
    for n in names:
        if n in gdf.columns:
            return sorted(str(v) for v in gdf[n].dropna().unique())
    return []


def _tk_open_file():
    """Open a native file dialog on the hidden Tk root."""
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = fd.askopenfilename(
        title="Open GIS File",
        filetypes=[
            ("GIS Files", "*.shp *.gpkg *.geojson *.json *.gdb"),
            ("All Files", "*.*"),
        ],
        parent=root,
    )
    root.destroy()
    return path or None


def _tk_save_file(title: str, ext: str, default: str) -> str | None:
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = fd.asksaveasfilename(
        title=title,
        defaultextension=ext,
        initialfile=default,
        filetypes=[(f"{ext.upper()} Files", f"*{ext}"), ("All Files", "*.*")],
        parent=root,
    )
    root.destroy()
    return path or None


# ─────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("web", "index.html")


@app.route("/api/load_file", methods=["POST"])
def api_load_file():
    path = _tk_open_file()
    if not path:
        return jsonify(ok=False, msg="Cancelled")
    try:
        gdf = gpd.read_file(path)
        with _lock:
            _state["gdf"]          = gdf
            _state["raw_lines"]    = []
            _state["master_lines"] = []
            _state["method_report"]= []
        return jsonify(
            ok        = True,
            path      = path,
            name      = Path(path).name,
            rows      = len(gdf),
            crs       = str(gdf.crs),
            plans     = _unique_col(gdf, ["PLAN_NAME","plan_name","Plan","PLAN"]),
            road_types= _unique_col(gdf, ["ROAD_TYPE","road_type","RoadType","type","Type"]),
        )
    except Exception as exc:
        return jsonify(ok=False, msg=str(exc))


@app.route("/api/map_image", methods=["POST"])
def api_map_image():
    body       = request.get_json(force=True) or {}
    show_polys = body.get("show_polys", True)
    show_lines = body.get("show_lines", True)
    w          = int(body.get("width",  900))
    h          = int(body.get("height", 600))
    return jsonify(image=_render_map(w, h, show_polys, show_lines))


@app.route("/api/process", methods=["POST"])
def api_process():
    with _lock:
        gdf = _state["gdf"]
    if gdf is None:
        return jsonify(ok=False, msg="No file loaded")

    body    = request.get_json(force=True) or {}
    smooth  = int(body.get("smooth",  0))
    minlen  = float(body.get("minlen", 0))
    prune   = float(body.get("prune",  0))
    cutback = float(body.get("cutback",0))
    algo    = body.get("algo", "auto")
    plans   = body.get("plans",  [])
    types   = body.get("types",  [])

    try:
        # Filter GDF
        filtered = gdf.copy()
        if plans:
            for col in ("PLAN_NAME","plan_name","Plan","PLAN"):
                if col in filtered.columns:
                    filtered = filtered[filtered[col].astype(str).isin(plans)]
                    break
        if types:
            for col in ("ROAD_TYPE","road_type","RoadType","type","Type"):
                if col in filtered.columns:
                    filtered = filtered[filtered[col].astype(str).isin(types)]
                    break

        # Run centerline extraction
        manual = None if algo == "auto" else algo
        result = process_segments(filtered, manual_algo=manual)
        lines, report = result if isinstance(result, tuple) else (result, [])

        raw = [l for l in lines if l is not None and not l.is_empty]

        # Post-processing chain
        processed = apply_smoothing(raw, smooth) if smooth > 0 else raw
        if minlen  > 0: processed = [l for l in processed if l.length >= minlen]
        if prune   > 0: processed = prune_dead_ends(processed, prune)
        # cutback (intersection cut) — import if available
        if cutback > 0:
            try:
                from algorithms import cut_intersections
                processed = cut_intersections(processed, cutback)
            except (ImportError, AttributeError):
                pass

        with _lock:
            _state["raw_lines"]    = raw
            _state["master_lines"] = processed
            _state["method_report"]= report

        # Build method count
        counts: dict[str, int] = {}
        for r in report:
            m = r.get("method", "unknown")
            counts[m] = counts.get(m, 0) + 1

        w = int(body.get("width",  900))
        h = int(body.get("height", 600))

        return jsonify(
            ok            = True,
            count         = len(processed),
            method_report = counts,
            image         = _render_map(w, h),
        )
    except Exception as exc:
        import traceback
        return jsonify(ok=False, msg=str(exc) + "\n" + traceback.format_exc())


@app.route("/api/export_shp", methods=["POST"])
def api_export_shp():
    with _lock:
        lines = _state["master_lines"]
        gdf   = _state["gdf"]
    if not lines:
        return jsonify(ok=False, msg="No centerlines — process first")
    path = _tk_save_file("Save Shapefile", ".shp", "centerlines.shp")
    if not path:
        return jsonify(ok=True, cancelled=True)
    try:
        from shapely.geometry import MultiLineString
        from geopandas import GeoDataFrame
        out = GeoDataFrame(geometry=lines, crs=gdf.crs if gdf is not None else None)
        if path.endswith(".gpkg"):
            out.to_file(path, driver="GPKG")
        else:
            out.to_file(path)
        return jsonify(ok=True, path=path)
    except Exception as exc:
        return jsonify(ok=False, msg=str(exc))


@app.route("/api/export_fbx", methods=["POST"])
def api_export_fbx():
    with _lock:
        lines = _state["master_lines"]
        gdf   = _state["gdf"]
    if not lines:
        return jsonify(ok=False, msg="No centerlines — process first")

    path = _tk_save_file("Save FBX", ".fbx", "road_centerlines.fbx")
    if not path:
        return jsonify(ok=True, cancelled=True)

    body    = request.get_json(force=True) or {}
    bp_path = body.get("bp_path",  "/Game/Road_Creator_Pro/Blueprints/BP_Road_Creator")
    scale   = float(body.get("scale", 100000))
    merge   = bool(body.get("merge", False))

    try:
        n = export_fbx(
            lines,
            path,
            blueprint_path = bp_path,
            scale          = scale,
            merge          = merge,
            crs            = gdf.crs if gdf is not None else None,
        )
        return jsonify(ok=True, path=path, count=n)
    except Exception as exc:
        import traceback
        return jsonify(ok=False, msg=str(exc))


# ─────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────
def _find_free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def main():
    port = _find_free_port()
    url  = f"http://127.0.0.1:{port}"

    # Start Flask in a daemon thread
    server_thread = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port,
                               debug=False, use_reloader=False),
        daemon=True,
    )
    server_thread.start()

    # Small delay so Flask is ready before the window opens
    import time; time.sleep(0.4)

    # Open native window
    import webview
    window = webview.create_window(
        title      = "GIS Road Master",
        url        = url,
        width      = 1280,
        height     = 780,
        min_size   = (900, 600),
        background_color = "#060912",
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
