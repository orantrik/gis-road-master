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
from shapely.geometry import LineString, box as shapely_box
from shapely.ops import snap, unary_union

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
    "gdf":              None,
    "raw_lines":        [],
    "master_lines":     [],
    "method_report":    [],
    # Shape Builder
    "shape_items":      [],     # list of {"geom": shapely_geom, "sel": bool, "excl": bool}
    "shape_history":    [],     # undo stack for merges
    "shape_built_geoms": None,  # committed polygons from shape builder
    # Routing Hints
    "hint_lines":       [],     # list of LineString
    "hint_snap_tol":    0.0003,
    # Precision Editor
    "selected_box":     None,   # shapely box geom
    "precision_lines":  [],     # lines in precision editor
    "eraser_history":   [],     # undo stack for eraser
    # Map
    "last_extent":      None,   # [xmin, ymin, xmax, ymax]
    "view_extent":      None,   # user-set zoom/pan window [xmin,ymin,xmax,ymax] or None=auto
}


# ── Helpers ───────────────────────────────────────────────────────
def _get_extent(ax):
    """Return [xmin, ymin, xmax, ymax] from a matplotlib Axes."""
    xlim = ax.get_xlim()
    ylim = ax.get_ylim()
    return [xlim[0], ylim[0], xlim[1], ylim[1]]


def _render_map(width: int, height: int,
                show_polys: bool = True, show_lines: bool = True):
    """Render current GDF + centerlines to a base-64 PNG data-URL.
    Returns (image_b64, extent) where extent = [xmin, ymin, xmax, ymax].
    """
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
        box   = _state["selected_box"]

    if gdf is not None and show_polys:
        gdf.plot(ax=ax, color="#0d1e38", edgecolor="#1e3a5f",
                 linewidth=0.6, alpha=0.85)

    if lines and show_lines:
        from geopandas import GeoSeries
        GeoSeries(lines).plot(ax=ax, color="#00d4ff",
                              linewidth=1.4, alpha=0.92)

    if box is not None:
        from geopandas import GeoSeries
        GeoSeries([box.boundary]).plot(ax=ax, color="#e74c3c",
                                       linewidth=2.0, linestyle="--", alpha=0.9)

    # Apply user zoom/pan window before capturing extent
    with _lock:
        view = _state.get("view_extent")
    if view:
        ax.set_xlim(view[0], view[2])
        ax.set_ylim(view[1], view[3])

    extent = _get_extent(ax)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight",
                facecolor="#060912", dpi=dpi, pad_inches=0.02)
    plt.close(fig)
    buf.seek(0)
    img_b64 = "data:image/png;base64," + base64.b64encode(buf.read()).decode()

    with _lock:
        _state["last_extent"] = extent

    return img_b64, extent


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


def _geom_to_rings(geom, tolerance=0.0):
    """Convert a shapely geometry to a list of coordinate rings for JS rendering."""
    rings = []
    if geom is None or geom.is_empty:
        return rings
    if geom.geom_type == "Polygon":
        g = geom.simplify(tolerance, preserve_topology=True) if tolerance > 0 else geom
        if not g.is_empty and g.geom_type == "Polygon":
            rings.append(list(g.exterior.coords))
    elif geom.geom_type in ("MultiPolygon", "GeometryCollection"):
        for part in geom.geoms:
            rings.extend(_geom_to_rings(part, tolerance))
    return rings


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
            _state["gdf"]               = gdf
            _state["raw_lines"]         = []
            _state["master_lines"]      = []
            _state["method_report"]     = []
            _state["shape_items"]       = []
            _state["shape_history"]     = []
            _state["shape_built_geoms"] = None
            _state["hint_lines"]        = []
            _state["selected_box"]      = None
            _state["precision_lines"]   = []
            _state["eraser_history"]    = []
        return jsonify(
            ok        = True,
            path      = path,
            name      = Path(path).name,
            rows      = len(gdf),
            crs       = str(gdf.crs),
            plans     = _unique_col(gdf, ["pl_number", "PLAN_NAME", "plan_name", "Plan", "PLAN"]),
            road_types= _unique_col(gdf, ["mavat_name", "ROAD_TYPE", "road_type", "RoadType", "type", "Type"]),
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
    img, extent = _render_map(w, h, show_polys, show_lines)
    return jsonify(image=img, extent=extent)


@app.route("/api/map_zoom", methods=["POST"])
def api_map_zoom():
    """
    Zoom/pan the map view.
    Body: { action: "zoom_in"|"zoom_out"|"pan"|"reset",
            cx, cy,          # zoom centre in GIS coords (for zoom actions)
            dx, dy }         # pan delta in GIS units (for pan action)
    """
    body = request.get_json(force=True) or {}
    action = body.get("action", "zoom_in")
    w = int(body.get("width", 900))
    h = int(body.get("height", 600))

    with _lock:
        current = list(_state.get("view_extent") or _state.get("last_extent") or [0,0,1,1])

    xmin, ymin, xmax, ymax = current
    cx = float(body.get("cx", (xmin + xmax) / 2))
    cy = float(body.get("cy", (ymin + ymax) / 2))
    dx = float(body.get("dx", 0))
    dy = float(body.get("dy", 0))

    if action == "zoom_in":
        factor = 0.65
        hw = (xmax - xmin) * factor / 2
        hh = (ymax - ymin) * factor / 2
        new_ext = [cx - hw, cy - hh, cx + hw, cy + hh]
    elif action == "zoom_out":
        factor = 1.5
        hw = (xmax - xmin) * factor / 2
        hh = (ymax - ymin) * factor / 2
        new_ext = [cx - hw, cy - hh, cx + hw, cy + hh]
    elif action == "pan":
        new_ext = [xmin + dx, ymin + dy, xmax + dx, ymax + dy]
    else:  # reset
        new_ext = None

    with _lock:
        _state["view_extent"] = new_ext

    img, extent = _render_map(w, h)
    return jsonify(image=img, extent=extent)


@app.route("/api/map_reset_view", methods=["POST"])
def api_map_reset_view():
    with _lock:
        _state["view_extent"] = None
    body = request.get_json(force=True) or {}
    w = int(body.get("width", 900))
    h = int(body.get("height", 600))
    img, extent = _render_map(w, h)
    return jsonify(image=img, extent=extent)


@app.route("/api/process", methods=["POST"])
def api_process():
    with _lock:
        gdf = _state["gdf"]
        shape_geoms = _state.get("shape_built_geoms")
        hints = list(_state.get("hint_lines", []))
        snap_tol = _state.get("hint_snap_tol", 0.0003)
    if gdf is None:
        return jsonify(ok=False, msg="No file loaded")

    body     = request.get_json(force=True) or {}
    smooth   = int(body.get("smooth",   0))
    minlen   = float(body.get("minlen", 0))
    prune    = float(body.get("prune",  0))
    cutback  = float(body.get("cutback",0))
    straight = float(body.get("straight", 0.0))
    algo     = body.get("algo", "auto")
    plans    = body.get("plans",  [])
    types    = body.get("types",  [])
    use_auto = bool(body.get("use_auto", False))

    try:
        # Filter GDF — use shape override if available
        if shape_geoms:
            filtered = gpd.GeoDataFrame(geometry=shape_geoms, crs=gdf.crs)
        else:
            filtered = gdf.copy()
            if plans:
                for col in ("pl_number", "PLAN_NAME", "plan_name", "Plan", "PLAN"):
                    if col in filtered.columns:
                        filtered = filtered[filtered[col].astype(str).isin(plans)]
                        break
            if types:
                for col in ("mavat_name", "ROAD_TYPE", "road_type", "RoadType", "type", "Type"):
                    if col in filtered.columns:
                        filtered = filtered[filtered[col].astype(str).isin(types)]
                        break

        # Run centerline extraction
        manual = None if (algo == "auto" or use_auto) else algo
        try:
            result = process_segments(
                filtered,
                manual_algorithm=manual,
                hint_lines=hints if hints else None,
                hint_snap_tol=snap_tol,
            )
        except TypeError:
            # Older algorithms.py without hint_lines parameter
            result = process_segments(filtered, manual_algorithm=manual)

        lines, report = result if isinstance(result, tuple) else (result, [])

        raw = [l for l in lines if l is not None and not l.is_empty]

        # Post-processing chain
        processed = apply_smoothing(raw, smooth) if smooth > 0 else raw
        if minlen   > 0: processed = [l for l in processed if l.length >= minlen]
        if prune    > 0: processed = prune_dead_ends(processed, prune)
        if straight > 0:
            try:
                from algorithms import straighten_lines
                processed = straighten_lines(processed, straight)
            except (ImportError, AttributeError):
                pass
        if cutback  > 0:
            try:
                from algorithms import cut_intersections
                processed = cut_intersections(processed, cutback)
            except (ImportError, AttributeError):
                pass

        with _lock:
            _state["raw_lines"]     = raw
            _state["master_lines"]  = processed
            _state["method_report"] = report

        # Build method count
        counts: dict[str, int] = {}
        for r in report:
            m = r.get("method", "unknown")
            counts[m] = counts.get(m, 0) + 1

        w = int(body.get("width",  900))
        h = int(body.get("height", 600))

        img, extent = _render_map(w, h)
        return jsonify(
            ok            = True,
            count         = len(processed),
            method_report = counts,
            image         = img,
            extent        = extent,
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
        from geopandas import GeoDataFrame
        out = GeoDataFrame(geometry=lines, crs=gdf.crs if gdf is not None else None)
        if path.endswith(".gpkg"):
            out.to_file(path, driver="GPKG")
        else:
            out.to_file(path)
        return jsonify(ok=True, path=path)
    except Exception as exc:
        return jsonify(ok=False, msg=str(exc))


@app.route("/api/export_geojson", methods=["POST"])
def api_export_geojson():
    with _lock:
        lines = _state["master_lines"]
        gdf   = _state["gdf"]
    if not lines:
        return jsonify(ok=False, msg="No centerlines — process first")
    path = _tk_save_file("Save GeoJSON", ".geojson", "centerlines.geojson")
    if not path:
        return jsonify(ok=True, cancelled=True)
    try:
        from geopandas import GeoDataFrame
        out = GeoDataFrame(geometry=lines, crs=gdf.crs if gdf is not None else None)
        out.to_file(path, driver="GeoJSON")
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
# SHAPE BUILDER ENDPOINTS
# ─────────────────────────────────────────────────────────────────

@app.route("/api/get_shape_data", methods=["POST"])
def api_get_shape_data():
    """Return polygon coords + extent for shape builder canvas."""
    with _lock:
        gdf        = _state["gdf"]
        shape_items = list(_state["shape_items"])
        last_extent = _state.get("last_extent")

    if gdf is None:
        return jsonify(ok=False, msg="No file loaded")

    try:
        # If shape_items is empty, initialise from GDF
        if not shape_items:
            items = []
            for geom in gdf.geometry:
                if geom is None or geom.is_empty:
                    continue
                items.append({"geom": geom, "sel": False, "excl": False})
            with _lock:
                _state["shape_items"] = items
            shape_items = items

        # Compute extent from all geoms
        from shapely.ops import unary_union
        all_geoms = [it["geom"] for it in shape_items if it["geom"] is not None]
        if not all_geoms:
            return jsonify(ok=False, msg="No geometries")
        union = unary_union(all_geoms)
        b = union.bounds  # (minx, miny, maxx, maxy)
        extent = [b[0], b[1], b[2], b[3]]
        ext_w = b[2] - b[0]
        tol = ext_w / 5000 if ext_w > 0 else 0

        polys = []
        for idx, item in enumerate(shape_items):
            rings = _geom_to_rings(item["geom"], tol)
            polys.append({
                "idx":  idx,
                "rings": rings,
                "sel":  item["sel"],
                "excl": item["excl"],
            })

        # Compute total area of non-excluded geoms
        total_area = sum(
            it["geom"].area for it in shape_items
            if not it["excl"] and it["geom"] is not None
        )

        return jsonify(
            ok=True,
            polys=polys,
            extent=extent,
            count=len(shape_items),
            area=total_area,
        )
    except Exception as exc:
        import traceback
        return jsonify(ok=False, msg=str(exc) + "\n" + traceback.format_exc())


@app.route("/api/shape_toggle", methods=["POST"])
def api_shape_toggle():
    """Toggle polygon selection or exclusion state."""
    body   = request.get_json(force=True) or {}
    idx    = int(body.get("idx", -1))
    action = body.get("action", "sel")  # "sel" or "excl"

    with _lock:
        items = _state["shape_items"]
        if idx < 0 or idx >= len(items):
            return jsonify(ok=False, msg="Invalid index")
        if action == "sel":
            items[idx]["sel"] = not items[idx]["sel"]
        elif action == "excl":
            items[idx]["excl"] = not items[idx]["excl"]
            if items[idx]["excl"]:
                items[idx]["sel"] = False

    return jsonify(ok=True, idx=idx,
                   sel=items[idx]["sel"],
                   excl=items[idx]["excl"])


@app.route("/api/shape_merge", methods=["POST"])
def api_shape_merge():
    """Merge selected polygons server-side."""
    body    = request.get_json(force=True) or {}
    indices = body.get("indices", [])

    with _lock:
        items = _state["shape_items"]
        if len(indices) < 2:
            return jsonify(ok=False, msg="Need at least 2 polygons to merge")

        valid_indices = [i for i in indices if 0 <= i < len(items)]
        if len(valid_indices) < 2:
            return jsonify(ok=False, msg="Invalid indices")

        # Save undo snapshot
        snapshot = [dict(it) for it in items]
        _state["shape_history"].append(snapshot)

        # Merge geometries
        geoms_to_merge = [items[i]["geom"] for i in valid_indices]
        merged = unary_union(geoms_to_merge)

        # Remove merged items (highest index first to preserve order)
        for i in sorted(valid_indices, reverse=True):
            del items[i]

        # Insert merged at position of first selected
        insert_pos = min(valid_indices)
        items.insert(insert_pos, {"geom": merged, "sel": True, "excl": False})

    return jsonify(ok=True, count=len(_state["shape_items"]))


@app.route("/api/shape_undo", methods=["POST"])
def api_shape_undo():
    """Undo last merge."""
    with _lock:
        history = _state["shape_history"]
        if not history:
            return jsonify(ok=False, msg="Nothing to undo")
        _state["shape_items"] = history.pop()
    return jsonify(ok=True, count=len(_state["shape_items"]))


@app.route("/api/shape_reset", methods=["POST"])
def api_shape_reset():
    """Reset to filtered GDF."""
    with _lock:
        gdf = _state["gdf"]
        if gdf is None:
            return jsonify(ok=False, msg="No file loaded")
        items = []
        for geom in gdf.geometry:
            if geom is None or geom.is_empty:
                continue
            items.append({"geom": geom, "sel": False, "excl": False})
        _state["shape_items"]   = items
        _state["shape_history"] = []
    return jsonify(ok=True, count=len(_state["shape_items"]))


@app.route("/api/shape_use", methods=["POST"])
def api_shape_use():
    """Commit selected polygons as shape override."""
    body    = request.get_json(force=True) or {}
    indices = body.get("indices", [])

    with _lock:
        items = _state["shape_items"]
        if indices:
            geoms = [items[i]["geom"] for i in indices
                     if 0 <= i < len(items) and not items[i]["excl"]]
        else:
            geoms = [it["geom"] for it in items if not it["excl"]]

        if not geoms:
            return jsonify(ok=False, msg="No polygons selected")

        _state["shape_built_geoms"] = geoms

    area = sum(g.area for g in geoms)
    return jsonify(ok=True, count=len(geoms), area=area)


@app.route("/api/shape_clear", methods=["POST"])
def api_shape_clear():
    """Clear shape override."""
    with _lock:
        _state["shape_built_geoms"] = None
    return jsonify(ok=True)


# ─────────────────────────────────────────────────────────────────
# ROUTING HINTS ENDPOINTS
# ─────────────────────────────────────────────────────────────────

@app.route("/api/add_hint", methods=["POST"])
def api_add_hint():
    """Add a hint stroke (list of [x,y] coords)."""
    body   = request.get_json(force=True) or {}
    coords = body.get("coords", [])
    if len(coords) < 2:
        return jsonify(ok=False, msg="Need at least 2 points")
    line = LineString(coords)
    with _lock:
        _state["hint_lines"].append(line)
        count = len(_state["hint_lines"])
    return jsonify(ok=True, count=count)


@app.route("/api/clear_last_hint", methods=["POST"])
def api_clear_last_hint():
    """Remove last hint stroke."""
    with _lock:
        if not _state["hint_lines"]:
            return jsonify(ok=False, msg="No hints")
        _state["hint_lines"].pop()
        count = len(_state["hint_lines"])
    return jsonify(ok=True, count=count)


@app.route("/api/clear_hints", methods=["POST"])
def api_clear_hints():
    """Remove all hint strokes."""
    with _lock:
        _state["hint_lines"] = []
    return jsonify(ok=True, count=0)


@app.route("/api/set_hint_tol", methods=["POST"])
def api_set_hint_tol():
    """Set snap tolerance for hints."""
    body = request.get_json(force=True) or {}
    tol  = float(body.get("tol", 0.0003))
    with _lock:
        _state["hint_snap_tol"] = tol
    return jsonify(ok=True, tol=tol)


# ─────────────────────────────────────────────────────────────────
# PRECISION EDITOR ENDPOINTS
# ─────────────────────────────────────────────────────────────────

@app.route("/api/set_box", methods=["POST"])
def api_set_box():
    """Set the precision edit box from GIS coordinates."""
    body = request.get_json(force=True) or {}
    try:
        xmin = float(body["xmin"])
        ymin = float(body["ymin"])
        xmax = float(body["xmax"])
        ymax = float(body["ymax"])
    except (KeyError, ValueError) as e:
        return jsonify(ok=False, msg=f"Invalid box coords: {e}")

    box_geom = shapely_box(xmin, ymin, xmax, ymax)
    with _lock:
        _state["selected_box"] = box_geom

    return jsonify(ok=True, xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax)


@app.route("/api/precision_init", methods=["POST"])
def api_precision_init():
    """Process lines within the box area, return lines as coord arrays + image."""
    body = request.get_json(force=True) or {}
    w    = int(body.get("width",  900))
    h    = int(body.get("height", 600))

    with _lock:
        box_geom     = _state["selected_box"]
        master_lines = list(_state["master_lines"])
        gdf          = _state["gdf"]

    if box_geom is None:
        return jsonify(ok=False, msg="No edit box set")

    try:
        # Find lines that intersect the box
        precision = []
        for ln in master_lines:
            if ln is not None and not ln.is_empty and ln.intersects(box_geom):
                clipped = ln.intersection(box_geom)
                if not clipped.is_empty:
                    if clipped.geom_type == "LineString":
                        precision.append(clipped)
                    elif clipped.geom_type == "MultiLineString":
                        precision.extend(list(clipped.geoms))

        with _lock:
            _state["precision_lines"] = precision
            _state["eraser_history"]  = []

        # Build response: lines as coord arrays
        line_data = []
        for ln in precision:
            line_data.append(list(ln.coords))

        # Render map showing box region
        img, extent = _render_map(w, h, show_polys=True, show_lines=True)

        return jsonify(ok=True, lines=line_data, image=img, extent=extent,
                       count=len(precision))
    except Exception as exc:
        import traceback
        return jsonify(ok=False, msg=str(exc) + "\n" + traceback.format_exc())


@app.route("/api/precision_erase", methods=["POST"])
def api_precision_erase():
    """Remove a precision line by index."""
    body = request.get_json(force=True) or {}
    idx  = int(body.get("idx", -1))

    with _lock:
        lines = _state["precision_lines"]
        if idx < 0 or idx >= len(lines):
            return jsonify(ok=False, msg="Invalid index")
        erased = lines.pop(idx)
        _state["eraser_history"].append((idx, erased))
        count = len(lines)

    return jsonify(ok=True, count=count)


@app.route("/api/precision_undo", methods=["POST"])
def api_precision_undo():
    """Undo last erase."""
    with _lock:
        history = _state["eraser_history"]
        if not history:
            return jsonify(ok=False, msg="Nothing to undo")
        idx, line = history.pop()
        lines = _state["precision_lines"]
        lines.insert(idx, line)
        count = len(lines)

    return jsonify(ok=True, count=count)


@app.route("/api/precision_apply", methods=["POST"])
def api_precision_apply():
    """Merge precision edits into global map, return new map image."""
    body = request.get_json(force=True) or {}
    w    = int(body.get("width",  900))
    h    = int(body.get("height", 600))

    with _lock:
        box_geom       = _state["selected_box"]
        master_lines   = list(_state["master_lines"])
        precision_lines= list(_state["precision_lines"])

    if box_geom is None:
        return jsonify(ok=False, msg="No edit box set")

    try:
        # Remove global lines that are within the box
        outside_lines = []
        for ln in master_lines:
            if ln is None or ln.is_empty:
                continue
            if not ln.intersects(box_geom):
                outside_lines.append(ln)
            else:
                # Keep the part outside the box
                diff = ln.difference(box_geom)
                if not diff.is_empty:
                    if diff.geom_type == "LineString":
                        outside_lines.append(diff)
                    elif diff.geom_type == "MultiLineString":
                        outside_lines.extend(list(diff.geoms))

        # Add precision lines (snap endpoints to nearby global lines)
        snap_tol = 0.0001
        snapped_precision = []
        for pl in precision_lines:
            if pl is None or pl.is_empty:
                continue
            try:
                if outside_lines:
                    nearby = unary_union(outside_lines)
                    pl = snap(pl, nearby, snap_tol)
            except Exception:
                pass
            snapped_precision.append(pl)

        new_master = outside_lines + snapped_precision

        with _lock:
            _state["master_lines"]   = new_master
            _state["selected_box"]   = None
            _state["precision_lines"]= []
            _state["eraser_history"] = []

        img, extent = _render_map(w, h)
        return jsonify(ok=True, count=len(new_master), image=img, extent=extent)
    except Exception as exc:
        import traceback
        return jsonify(ok=False, msg=str(exc) + "\n" + traceback.format_exc())


# ─────────────────────────────────────────────────────────────────
# PENCIL TOOL ENDPOINTS
# ─────────────────────────────────────────────────────────────────

@app.route("/api/pencil_add_lines", methods=["POST"])
def api_pencil_add_lines():
    """Append user-drawn lines to master_lines."""
    body  = request.get_json(force=True) or {}
    lines = body.get("lines", [])
    body2 = body
    w     = int(body2.get("width",  900))
    h     = int(body2.get("height", 600))

    if not lines:
        return jsonify(ok=False, msg="No lines provided")

    try:
        new_lines = []
        for coords in lines:
            if len(coords) >= 2:
                new_lines.append(LineString(coords))

        with _lock:
            _state["master_lines"].extend(new_lines)
            total = len(_state["master_lines"])

        img, extent = _render_map(w, h)
        return jsonify(ok=True, added=len(new_lines), total=total,
                       image=img, extent=extent)
    except Exception as exc:
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
    port = int(os.environ.get("GIS_PORT", 0)) or _find_free_port()
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
