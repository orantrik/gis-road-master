"""
algorithms.py – Pure-Python geometry algorithms for GIS Road Master.

No UI code lives here; every function is independently testable.
"""

from __future__ import annotations

import numpy as np
import geopandas as gpd
from shapely.geometry import LineString, MultiLineString, Point
from shapely.ops import unary_union, snap
import pygeoops


# ─────────────────────────────────────────────────────────────────────────────
# SMOOTHING
# ─────────────────────────────────────────────────────────────────────────────

def chaikins_corner_cutting(coords: list, refinements: int = 3) -> np.ndarray:
    """Wave-free smoothing via Chaikin's corner-cutting algorithm."""
    pts = np.array(coords)
    if len(pts) < 3:
        return pts
    for _ in range(refinements):
        L, R = pts[:-1], pts[1:]
        new = np.empty((len(L) * 2, 2))
        new[0::2] = L * 0.75 + R * 0.25
        new[1::2] = L * 0.25 + R * 0.75
        pts = np.vstack([pts[0], new, pts[-1]])
    return pts


def apply_smoothing(lines: list[LineString], smooth: int) -> list[LineString]:
    """Re-apply Chaikin smoothing to existing lines (no centerline recompute)."""
    result = []
    s = max(0, int(smooth))
    for line in lines:
        coords = list(line.coords)
        if s > 0 and len(coords) >= 3:
            new_coords = chaikins_corner_cutting(coords, s)
            result.append(LineString(new_coords) if len(new_coords) >= 2 else line)
        else:
            result.append(line)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-TUNE
# ─────────────────────────────────────────────────────────────────────────────

def estimate_polygon_metrics(geom) -> dict:
    """
    Compute scale-invariant geometric metrics for a polygon.

    Returns:
        width      – estimated average road width  (area / perimeter * 4)
        complexity – fill ratio vs convex hull  (1 = convex, 0 = very tortuous)
        area       – polygon area
        perimeter  – polygon perimeter
    """
    area = geom.area
    perim = geom.length
    hull_area = geom.convex_hull.area if not geom.convex_hull.is_empty else area
    width = (4.0 * area / perim) if perim > 0 else 0.0
    complexity = (area / hull_area) if hull_area > 0 else 1.0
    return {"width": width, "complexity": complexity, "area": area, "perimeter": perim}


def auto_tune_params(geom) -> dict:
    """
    Derive optimal centerline-extraction parameters from polygon geometry.

    Strategy (scale-invariant, works with any CRS):
    • Pruning     ≈ 1.5 × estimated road width  (kills spurious short branches)
    • Straighten  ≈ width × complexity-factor    (less simplification for curvy roads)
    • Smoothing   driven by exterior vertex count (proxy for shape intricacy)
    """
    m = estimate_polygon_metrics(geom)
    w = m["width"]

    # Pruning
    prune = max(1e-9, w * 1.5)

    # Straightening
    if m["complexity"] > 0.85:
        sf = 0.50          # simple convex shape → more simplification is safe
    elif m["complexity"] > 0.60:
        sf = 0.20
    else:
        sf = 0.05          # very complex / tortuous → preserve shape
    straight = max(1e-10, w * sf)

    # Smoothing passes
    nverts = len(geom.exterior.coords) if hasattr(geom, "exterior") else 10
    smooth = 1 if nverts < 20 else (2 if nverts < 60 else (3 if nverts < 120 else 4))

    return {"prune": prune, "straight": straight, "smooth": smooth, "metrics": m}


# ─────────────────────────────────────────────────────────────────────────────
# SINGLE-GEOMETRY PROCESSING
# ─────────────────────────────────────────────────────────────────────────────

def process_single(geom, prune: float, straight: float, smooth: int):
    """
    Extract the centerline of one road polygon.

    Returns a LineString / MultiLineString, or None on failure.
    """
    try:
        line = pygeoops.centerline(geom, densify_distance=-1, min_branch_length=prune)
    except Exception:
        return None

    if line is None or line.is_empty:
        return None

    line = line.simplify(straight)

    s = int(smooth)
    if s > 0:
        if isinstance(line, LineString):
            coords = chaikins_corner_cutting(list(line.coords), s)
            if len(coords) >= 2:
                line = LineString(coords)
        elif isinstance(line, MultiLineString):
            parts = []
            for seg in line.geoms:
                coords = chaikins_corner_cutting(list(seg.coords), s)
                if len(coords) >= 2:
                    parts.append(LineString(coords))
            if parts:
                line = MultiLineString(parts) if len(parts) > 1 else parts[0]

    return line


# ─────────────────────────────────────────────────────────────────────────────
# SEGMENT-AWARE BATCH PROCESSING
# ─────────────────────────────────────────────────────────────────────────────

def process_segments(
    gdf: gpd.GeoDataFrame,
    use_auto: bool = True,
    manual_prune: float | None = None,
    manual_straight: float | None = None,
    manual_smooth: int = 2,
    min_area: float = 0.0,
    progress_cb=None,
) -> list[LineString]:
    """
    Process each road polygon individually (segment-aware).

    Unlike dissolving everything into one blob, this extracts a per-polygon
    centerline with geometry-appropriate parameters, which is far cleaner.

    Args:
        gdf            : GeoDataFrame of road polygons.
        use_auto       : If True, auto-tune parameters per polygon.
        manual_*       : Fallback values when use_auto=False.
        min_area       : Skip polygons smaller than this area.
        progress_cb    : Optional callable(current, total, info_dict).

    Returns:
        Flat list of LineString geometries.
    """
    rows = [
        (i, row)
        for i, row in gdf.iterrows()
        if row.geometry is not None
        and not row.geometry.is_empty
        and row.geometry.area >= min_area
    ]
    total = len(rows)
    results: list[LineString] = []

    for k, (_, row) in enumerate(rows):
        geom = row.geometry

        if use_auto:
            p = auto_tune_params(geom)
            prune, straight, smooth = p["prune"], p["straight"], p["smooth"]
            info = {**p, "idx": k, "total": total}
        else:
            prune = manual_prune if manual_prune is not None else 0.1
            straight = manual_straight if manual_straight is not None else 0.00002
            smooth = manual_smooth
            info = {"prune": prune, "straight": straight, "smooth": smooth,
                    "idx": k, "total": total}

        if progress_cb:
            progress_cb(k + 1, total, info)

        line = process_single(geom, prune, straight, smooth)
        if line is None:
            continue

        if isinstance(line, MultiLineString):
            results.extend(g for g in line.geoms if not g.is_empty)
        elif not line.is_empty:
            results.append(line)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# CONNECTIVITY / EXPORT
# ─────────────────────────────────────────────────────────────────────────────

def snap_endpoints(lines: list[LineString], tolerance: float | None = None) -> list[LineString]:
    """Snap nearby line endpoints together for better network connectivity."""
    if not lines:
        return lines

    if tolerance is None:
        lengths = [l.length for l in lines if l.length > 0]
        tolerance = np.mean(lengths) * 0.005 if lengths else 1e-5

    pts: list[Point] = []
    for line in lines:
        c = list(line.coords)
        if len(c) >= 2:
            pts.extend([Point(c[0]), Point(c[-1])])

    if not pts:
        return lines

    cloud = unary_union(pts)
    return [snap(line, cloud, tolerance) for line in lines]


def lines_to_gdf(lines: list, crs) -> gpd.GeoDataFrame:
    """Convert a list of LineStrings to a GeoDataFrame."""
    valid = [l for l in lines if l is not None and not l.is_empty]
    return gpd.GeoDataFrame(geometry=valid, crs=crs)


def export_geojson(gdf: gpd.GeoDataFrame, output_path: str) -> int:
    """
    Save GeoDataFrame as GeoJSON.

    Reprojects to WGS84 (EPSG:4326) for standard GeoJSON compliance.
    Returns the number of features written.
    """
    out = gdf[~gdf.geometry.is_empty].copy()
    if out.crs and not out.crs.equals("EPSG:4326"):
        try:
            out = out.to_crs("EPSG:4326")
        except Exception:
            pass  # keep original CRS if reprojection fails
    out.to_file(output_path, driver="GeoJSON")
    return len(out)
