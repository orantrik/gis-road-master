"""
algorithms.py – Pure-Python geometry algorithms for GIS Road Master.

No UI code lives here; every function is independently testable.

Centerline dispatch hierarchy (auto-selected per polygon):
  straight_skeleton  – pygeoops medial axis; fast, clean for convex corridors
  voronoi_density    – Voronoi of dense boundary points; good for branchy shapes
  hatching           – multi-angle hatch midpoints; noise-immune
  edt_ridge          – distance-transform ridge; handles narrow passages (scipy)
"""

from __future__ import annotations

import math
import numpy as np
import geopandas as gpd
from shapely.geometry import LineString, MultiLineString, Point, MultiPoint
from shapely.ops import unary_union, snap, voronoi_diagram
import pygeoops

# Optional scipy for EDT method
try:
    from scipy import ndimage as _ndimage  # type: ignore[import]
    _SCIPY = True
except ImportError:
    _SCIPY = False


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
# GEOMETRY ANALYSIS HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def estimate_width(polygon) -> float:
    """Hydraulic radius approximation: 4 * area / perimeter."""
    area = polygon.area
    perim = polygon.length
    return (4.0 * area / perim) if perim > 0 else 0.0


def complexity_index(polygon) -> float:
    """
    Area / convex hull area.
    1.0 = perfectly convex, <0.5 = highly branchy / tortuous.
    """
    area = polygon.area
    hull_area = polygon.convex_hull.area if not polygon.convex_hull.is_empty else area
    return (area / hull_area) if hull_area > 0 else 1.0


def reflex_vertex_count(polygon) -> int:
    """Count exterior vertices with interior angle > π (reflex angles)."""
    coords = list(polygon.exterior.coords)
    n = len(coords) - 1  # last coord == first
    count = 0
    for i in range(n):
        a = coords[(i - 1) % n]
        b = coords[i]
        c = coords[(i + 1) % n]
        # Signed cross product: positive → left turn (reflex for CCW-wound exterior)
        cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if cross > 0:
            count += 1
    return count


def boundary_noise_index(polygon, tol_factor: float = 0.05) -> float:
    """
    Actual perimeter / simplified perimeter.
    > 1.0 means the boundary is noisier than expected for its width.
    tol_factor: simplification tolerance as fraction of estimated road width.
    """
    w = estimate_width(polygon)
    tol = max(1e-10, w * tol_factor)
    simplified = polygon.simplify(tol, preserve_topology=True)
    actual = polygon.length
    simple_len = simplified.length
    return (actual / simple_len) if simple_len > 0 else 1.0


def estimate_polygon_metrics(geom) -> dict:
    """
    Compute scale-invariant geometric metrics for a polygon.

    Returns:
        width      – estimated average road width  (4 * area / perimeter)
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


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-TUNE  (parameters + algorithm selection)
# ─────────────────────────────────────────────────────────────────────────────

def auto_tune_params(geom) -> dict:
    """
    Derive optimal centerline-extraction parameters AND algorithm choice.

    Algorithm selection logic (scale-invariant):
      edt_ridge       – very elongated/thin shapes (requires scipy)
      voronoi_density – branchy / complex (complexity < 0.40)
      hatching        – noisy/irregular boundaries (noise_index > 1.40)
      straight_skeleton – regular convex corridors (default)

    Returns dict with keys:
        prune, straight, smooth  – extraction parameters
        algorithm                – one of the four method names above
        reason                   – human-readable explanation string
        metrics                  – raw geometry metrics dict
    """
    m = estimate_polygon_metrics(geom)
    w = m["width"]
    comp = m["complexity"]
    area = m["area"]
    perim = m["perimeter"]

    # ── extraction parameters (unchanged from original logic) ──────────────
    prune = max(1e-9, w * 1.5)
    if comp > 0.85:
        sf = 0.50
    elif comp > 0.60:
        sf = 0.20
    else:
        sf = 0.05
    straight = max(1e-10, w * sf)
    nverts = len(geom.exterior.coords) if hasattr(geom, "exterior") else 10
    smooth = 1 if nverts < 20 else (2 if nverts < 60 else (3 if nverts < 120 else 4))

    # ── algorithm selection ────────────────────────────────────────────────
    noise = boundary_noise_index(geom)
    # Thinness = perimeter / (4 * sqrt(area)):
    #   circle≈0.89, square=1.0, 10:1 rect≈1.74, very thin > 5
    sqrt_area = math.sqrt(area) if area > 1e-20 else 1e-10
    thinness = perim / (4.0 * sqrt_area)

    if _SCIPY and thinness > 8.0:
        algo = "edt_ridge"
        reason = f"narrow passage (thinness={thinness:.1f})"
    elif comp < 0.40:
        algo = "voronoi_density"
        reason = f"branchy/complex (complexity={comp:.2f})"
    elif noise > 1.40:
        algo = "hatching"
        reason = f"noisy boundary (noise_index={noise:.2f})"
    else:
        algo = "straight_skeleton"
        reason = f"regular corridor (complexity={comp:.2f}, noise={noise:.2f})"

    return {
        "prune": prune, "straight": straight, "smooth": smooth,
        "algorithm": algo, "reason": reason,
        "metrics": {**m, "noise_index": noise, "thinness": thinness},
    }


# ─────────────────────────────────────────────────────────────────────────────
# SHARED HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _apply_line_smooth(line, straight: float, smooth: int):
    """Simplify + Chaikin-smooth a LineString or MultiLineString."""
    if line is None or line.is_empty:
        return line
    line = line.simplify(straight)
    s = int(smooth)
    if s <= 0:
        return line
    if isinstance(line, LineString):
        c = chaikins_corner_cutting(list(line.coords), s)
        return LineString(c) if len(c) >= 2 else line
    if isinstance(line, MultiLineString):
        parts = []
        for seg in line.geoms:
            c = chaikins_corner_cutting(list(seg.coords), s)
            if len(c) >= 2:
                parts.append(LineString(c))
        if not parts:
            return line
        return MultiLineString(parts) if len(parts) > 1 else parts[0]
    return line


# ─────────────────────────────────────────────────────────────────────────────
# CENTERLINE METHOD IMPLEMENTATIONS
# ─────────────────────────────────────────────────────────────────────────────

def _centerline_straight_skeleton(geom, prune: float, straight: float, smooth: int):
    """
    pygeoops medial axis (straight skeleton approximation).

    Fastest method; produces clean straight-line segments for simple
    convex corridors.  Pre-simplifies to remove micro-loop artifacts.
    """
    try:
        m = estimate_polygon_metrics(geom)
        pre_simplify_tol = max(1e-10, m["width"] * 0.10)
        clean = geom.simplify(pre_simplify_tol, preserve_topology=True)
        if clean.is_empty:
            clean = geom
        line = pygeoops.centerline(clean, densify_distance=-1, min_branch_length=prune)
    except Exception:
        return None
    if line is None or line.is_empty:
        return None
    return _apply_line_smooth(line, straight, smooth)


def _centerline_voronoi_density(geom, prune: float, straight: float, smooth: int):
    """
    Voronoi-density medial axis for complex / branchy polygons.

    Densely samples the polygon boundary, computes the Voronoi diagram,
    then keeps only the Voronoi edges whose midpoints fall inside the
    polygon.  Those interior edges approximate the medial axis / skeleton.
    """
    try:
        m = estimate_polygon_metrics(geom)
        w = m["width"]
        density = max(1e-9, w * 0.25)

        boundary = geom.exterior
        n_pts = max(60, int(boundary.length / density))
        pts = [boundary.interpolate(i / n_pts, normalized=True) for i in range(n_pts)]
        for ring in geom.interiors:
            n_int = max(10, int(ring.length / density))
            pts += [ring.interpolate(i / n_int, normalized=True) for i in range(n_int)]
        if len(pts) < 4:
            return None

        mp = MultiPoint(pts)
        regions = voronoi_diagram(mp, envelope=geom.envelope.buffer(density * 2))

        interior_segs: list[LineString] = []
        seen: set = set()
        for region in regions.geoms:
            ring_coords = list(region.exterior.coords)
            for i in range(len(ring_coords) - 1):
                c0, c1 = ring_coords[i], ring_coords[i + 1]
                key = tuple(sorted([
                    (round(c0[0], 10), round(c0[1], 10)),
                    (round(c1[0], 10), round(c1[1], 10)),
                ]))
                if key in seen:
                    continue
                seen.add(key)
                seg = LineString([c0, c1])
                try:
                    if seg.length > 0 and geom.contains(seg.centroid):
                        interior_segs.append(seg)
                except Exception:
                    pass

        if not interior_segs:
            return None

        result = unary_union(interior_segs)
        return _apply_line_smooth(result, straight, smooth)
    except Exception:
        return None


def _centerline_hatching(
    geom, prune: float, straight: float, smooth: int, n_angles: int = 3
):
    """
    Multi-angle hatch-midpoint centerline for noisy / irregular boundaries.

    Rotates the polygon at n_angles evenly-spaced angles, slices it with
    parallel horizontal hatch lines, records each slice midpoint, then
    assembles all midpoints into a polyline sorted along the major axis.
    Noise in the boundary averages out across rotations.
    """
    try:
        from shapely.geometry import Polygon  # local to avoid circular import risk

        m = estimate_polygon_metrics(geom)
        w = m["width"]
        spacing = max(1e-9, w * 0.20)
        minx, miny, maxx, maxy = geom.bounds
        cx, cy = (minx + maxx) / 2, (miny + maxy) / 2

        all_mids: list[tuple] = []

        for angle_deg in [i * 180.0 / n_angles for i in range(n_angles)]:
            rad = math.radians(angle_deg)
            cos_a, sin_a = math.cos(rad), math.sin(rad)

            def rot(x, y, c=cos_a, s=sin_a):
                dx, dy = x - cx, y - cy
                return cx + dx * c + dy * s, cy - dx * s + dy * c

            def unrot(x, y, c=cos_a, s=sin_a):
                dx, dy = x - cx, y - cy
                return cx + dx * c - dy * s, cy + dx * s + dy * c

            rot_ext = [rot(x, y) for x, y in geom.exterior.coords]
            rot_int = [[rot(x, y) for x, y in ring.coords]
                       for ring in geom.interiors]

            try:
                rot_geom = Polygon(rot_ext, rot_int) if rot_int else Polygon(rot_ext)
                if not rot_geom.is_valid:
                    rot_geom = rot_geom.buffer(0)
            except Exception:
                continue

            rminx, rminy, rmaxx, rmaxy = rot_geom.bounds
            y_pos = rminy + spacing / 2
            while y_pos <= rmaxy:
                hatch = LineString([(rminx - 1e-6, y_pos), (rmaxx + 1e-6, y_pos)])
                try:
                    inter = rot_geom.intersection(hatch)
                except Exception:
                    y_pos += spacing
                    continue

                if inter.is_empty:
                    y_pos += spacing
                    continue

                segs = ([inter] if isinstance(inter, LineString)
                        else [g for g in inter.geoms if isinstance(g, LineString)]
                        if hasattr(inter, "geoms") else [])

                for seg in segs:
                    sc = list(seg.coords)
                    if len(sc) >= 2:
                        mx = (sc[0][0] + sc[-1][0]) / 2
                        my_r = (sc[0][1] + sc[-1][1]) / 2
                        ox, oy = unrot(mx, my_r)
                        all_mids.append((ox, oy))

                y_pos += spacing

        if len(all_mids) < 2:
            return None

        # Sort along polygon's major axis via PCA
        pts_arr = np.array(all_mids)
        centered = pts_arr - pts_arr.mean(axis=0)
        cov = np.cov(centered.T)
        if cov.ndim < 2:
            return None
        _, vecs = np.linalg.eigh(cov)
        major = vecs[:, -1]
        proj = centered @ major
        ordered = pts_arr[np.argsort(proj)]

        # Remove duplicates closer than half-spacing
        min_d = spacing * 0.45
        filtered = [ordered[0]]
        for pt in ordered[1:]:
            if np.linalg.norm(pt - filtered[-1]) >= min_d:
                filtered.append(pt)

        if len(filtered) < 2:
            return None

        line = LineString(filtered)
        return _apply_line_smooth(line, straight, smooth)
    except Exception:
        return None


def _centerline_edt_ridge(geom, prune: float, straight: float, smooth: int):
    """
    EDT ridge extraction for narrow / elongated passages.

    Rasterizes the polygon at ~15 pixels per road-width, computes the
    Euclidean Distance Transform, extracts local-maxima (ridge) pixels,
    then vectorizes them as a sorted polyline.

    Requires: scipy (ndimage.distance_transform_edt)
              matplotlib (Path.contains_points for fast rasterization)
    Falls back to straight_skeleton if either is missing.
    """
    if not _SCIPY:
        return None
    try:
        from matplotlib.path import Path as MplPath
    except ImportError:
        return None

    try:
        m = estimate_polygon_metrics(geom)
        w = m["width"]
        minx, miny, maxx, maxy = geom.bounds

        # Resolution: ~15 pixels across the estimated road width
        res = max(w / 15.0, max(maxx - minx, maxy - miny) / 300.0, 1e-10)
        nx = min(600, max(10, int((maxx - minx) / res) + 2))
        ny = min(600, max(10, int((maxy - miny) / res) + 2))
        res_x = (maxx - minx) / max(nx - 1, 1)
        res_y = (maxy - miny) / max(ny - 1, 1)

        # Vectorised rasterization via matplotlib Path
        path = MplPath(np.array(geom.exterior.coords))
        jj, ii = np.meshgrid(np.arange(nx), np.arange(ny))
        world_x = minx + jj * res_x
        world_y = miny + ii * res_y
        flat = np.column_stack([world_x.ravel(), world_y.ravel()])
        grid = path.contains_points(flat).reshape(ny, nx)
        for ring in geom.interiors:
            hole = MplPath(np.array(ring.coords))
            grid &= ~hole.contains_points(flat).reshape(ny, nx)

        if not grid.any():
            return None

        dt = _ndimage.distance_transform_edt(grid)

        # Ridge pixels: local maxima of dt (≥ all 8 neighbours)
        padded = np.pad(dt, 1, mode="constant", constant_values=0)
        is_ridge = np.ones_like(grid, dtype=bool)
        for di, dj in [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]:
            is_ridge &= (dt >= padded[1+di: ny+1+di, 1+dj: nx+1+dj])
        is_ridge &= grid

        ridgei, ridgej = np.where(is_ridge)
        if len(ridgei) < 2:
            return None

        world_pts = np.column_stack([
            minx + ridgej.astype(float) * res_x,
            miny + ridgei.astype(float) * res_y,
        ])
        centered = world_pts - world_pts.mean(axis=0)
        cov = np.cov(centered.T)
        if cov.ndim < 2:
            return None
        _, vecs = np.linalg.eigh(cov)
        major = vecs[:, -1]
        proj = centered @ major
        ordered = world_pts[np.argsort(proj)]

        min_d = min(res_x, res_y) * 0.8
        out = [ordered[0]]
        for pt in ordered[1:]:
            if np.linalg.norm(pt - out[-1]) >= min_d:
                out.append(pt)

        if len(out) < 2:
            return None

        line = LineString(out)
        return _apply_line_smooth(line, max(straight, res_x), smooth)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# DISPATCHER
# ─────────────────────────────────────────────────────────────────────────────

_METHOD_FNS: dict = {
    "straight_skeleton": _centerline_straight_skeleton,
    "voronoi_density":   _centerline_voronoi_density,
    "hatching":          _centerline_hatching,
    "edt_ridge":         _centerline_edt_ridge,
}

METHOD_LABELS: list[str] = list(_METHOD_FNS.keys())


def process_single(
    geom,
    prune: float,
    straight: float,
    smooth: int,
    algorithm: str = "straight_skeleton",
):
    """
    Dispatch centerline extraction to the chosen algorithm.
    Falls back to straight_skeleton on failure.
    """
    fn = _METHOD_FNS.get(algorithm, _centerline_straight_skeleton)
    result = fn(geom, prune, straight, smooth)
    if (result is None or result.is_empty) and algorithm != "straight_skeleton":
        result = _centerline_straight_skeleton(geom, prune, straight, smooth)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# INTERSECTION CUTTER
# ─────────────────────────────────────────────────────────────────────────────

def cut_intersections(
    lines: list[LineString],
    cutback: float,
    min_seg: float | None = None,
) -> list[LineString]:
    """
    Trim every centerline ``cutback`` coordinate-units back from each
    intersection it participates in.

    * At **T-junctions** only the dangling end is trimmed inward.
    * At **X-crossings** both roads are trimmed on each side of the crossing,
      effectively splitting each road into two shorter segments.
    * **Shared endpoints** (Y-forks) trim all participating roads inward by
      ``cutback``, leaving a clean gap at the junction.

    Parameters
    ----------
    lines   : list of shapely LineString (centerlines)
    cutback : distance in the same units as the coordinate system
              (metres for projected CRS, degrees for geographic CRS)
    min_seg : minimum length a surviving sub-segment must have to be kept;
              defaults to ``cutback``.  Raise this to suppress tiny stubs.

    Returns
    -------
    list of LineString – may be *longer* than the input when X-crossings
    are encountered (each crossing splits a road in two).
    """
    from shapely.strtree import STRtree
    from shapely.ops import substring
    from shapely.geometry import Point as _Pt

    if not lines or cutback <= 0:
        return list(lines)
    if min_seg is None:
        min_seg = cutback

    n = len(lines)
    # hit_dists[i] accumulates distances along lines[i] at each intersection
    hit_dists: list[list[float]] = [[] for _ in range(n)]

    tree = STRtree(lines)

    for i, line in enumerate(lines):
        for j in tree.query(line, predicate="intersects"):
            if j <= i:
                continue
            other = lines[j]
            inter = line.intersection(other)
            if inter.is_empty:
                continue

            # Collect intersection points
            gtype = inter.geom_type
            if gtype == "Point":
                pts = [inter]
            elif gtype == "MultiPoint":
                pts = list(inter.geoms)
            elif gtype in ("LineString", "MultiLineString"):
                # collinear overlap – use both endpoints of the overlap
                if gtype == "LineString":
                    coords = list(inter.coords)
                else:
                    coords = [c for g in inter.geoms for c in g.coords]
                pts = [_Pt(coords[0]), _Pt(coords[-1])]
            elif gtype == "GeometryCollection":
                pts = []
                for g in inter.geoms:
                    if g.geom_type == "Point":
                        pts.append(g)
                    elif g.geom_type == "LineString":
                        cc = list(g.coords)
                        pts += [_Pt(cc[0]), _Pt(cc[-1])]
            else:
                continue

            for pt in pts:
                hit_dists[i].append(line.project(pt))
                hit_dists[j].append(other.project(pt))

    result: list[LineString] = []
    for i, line in enumerate(lines):
        dists = sorted(set(hit_dists[i]))
        if not dists:
            result.append(line)
            continue

        L = line.length

        # Build cut zones: [d-cutback … d+cutback] clamped to [0, L]
        zones: list[list[float]] = []
        for d in dists:
            zones.append([max(0.0, d - cutback), min(L, d + cutback)])

        # Merge overlapping zones
        zones.sort()
        merged: list[list[float]] = []
        for zs, ze in zones:
            if merged and zs <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], ze)
            else:
                merged.append([zs, ze])

        # Extract surviving segments between the cut zones
        segs: list[LineString] = []
        prev = 0.0
        for zs, ze in merged:
            if zs - prev >= min_seg:
                try:
                    seg = substring(line, prev, zs)
                    if not seg.is_empty and seg.length >= min_seg:
                        segs.append(seg)
                except Exception:
                    pass
            prev = ze
        # trailing segment after the last cut zone
        if L - prev >= min_seg:
            try:
                seg = substring(line, prev, L)
                if not seg.is_empty and seg.length >= min_seg:
                    segs.append(seg)
            except Exception:
                pass

        result.extend(segs if segs else [line])

    return result


# ─────────────────────────────────────────────────────────────────────────────
# JUNCTION SMOOTHING
# ─────────────────────────────────────────────────────────────────────────────

def smooth_junctions(
    lines: list[LineString],
    refinements: int = 1,
    coord_decimals: int = 5,
) -> list[LineString]:
    """
    Smooth approach arms at junction nodes (degree ≥ 3).

    Applies one Chaikin pass to lines that terminate at a junction so
    that the corner is softened without deep corner-cutting.
    Lines that are not at a junction are returned unchanged.
    """
    if not lines or refinements <= 0:
        return lines

    def rp(p):
        return (round(p[0], coord_decimals), round(p[1], coord_decimals))

    deg: dict = {}
    for line in lines:
        c = list(line.coords)
        if len(c) >= 2:
            for pt in (rp(c[0]), rp(c[-1])):
                deg[pt] = deg.get(pt, 0) + 1

    result: list[LineString] = []
    for line in lines:
        c = list(line.coords)
        if len(c) < 3:
            result.append(line)
            continue
        s, e = rp(c[0]), rp(c[-1])
        if deg.get(s, 1) >= 3 or deg.get(e, 1) >= 3:
            new_c = chaikins_corner_cutting(c, refinements)
            result.append(LineString(new_c) if len(new_c) >= 2 else line)
        else:
            result.append(line)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# ROUTING HINTS  (potential-field guided snapping)
# ─────────────────────────────────────────────────────────────────────────────

def apply_hints(
    lines: list[LineString],
    hint_lines: list[LineString],
    snap_tol: float = 0.0002,
) -> list[LineString]:
    """
    Potential-field guided hint application.

    For each centerline segment within 5 × snap_tol of a hint stroke:
    - If the stroke is roughly parallel (|dot product| > 0.7), apply a
      weighted blend:  final = (1 - w) * centerline + w * snapped_to_hint
      where w = min(0.85,  dot * snap_tol / distance_to_hint)
    - Otherwise fall back to plain shapely.snap().

    Lines farther than 5 × snap_tol from any hint are returned unchanged.
    """
    if not hint_lines or not lines:
        return lines
    valid = [h for h in hint_lines if h is not None and not h.is_empty]
    if not valid:
        return lines

    hint_geom = unary_union(valid)

    def _direction(ls: LineString) -> np.ndarray:
        c = list(ls.coords)
        if len(c) < 2:
            return np.array([1.0, 0.0])
        v = np.array(c[-1]) - np.array(c[0])
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else np.array([1.0, 0.0])

    result: list[LineString] = []
    for line in lines:
        if line is None or line.is_empty:
            continue

        if line.distance(hint_geom) > snap_tol * 5:
            result.append(line)
            continue

        # Find nearest hint stroke
        best_hint, best_dist = None, float("inf")
        for h in valid:
            d = line.distance(h)
            if d < best_dist:
                best_dist, best_hint = d, h

        if best_hint is None or best_dist <= 0:
            result.append(snap(line, hint_geom, snap_tol))
            continue

        d_line = _direction(line)
        d_hint = _direction(best_hint)
        dot = abs(float(np.dot(d_line, d_hint)))

        if dot > 0.7:
            # Parallel → weighted interpolation
            w = min(0.85, dot * snap_tol / best_dist)
            snapped = snap(line, best_hint, snap_tol)
            c_orig = np.array(line.coords)
            c_snap = np.array(snapped.coords)
            if len(c_orig) == len(c_snap) and w > 0.05:
                blended = (1 - w) * c_orig + w * c_snap
                result.append(LineString(blended.tolist()))
            else:
                result.append(snapped)
        else:
            # Not parallel → plain snap
            result.append(snap(line, hint_geom, snap_tol))

    return result


# ─────────────────────────────────────────────────────────────────────────────
# SEGMENT-AWARE BATCH PROCESSING
# ─────────────────────────────────────────────────────────────────────────────

def process_segments(
    gdf: gpd.GeoDataFrame,
    use_auto: bool = True,
    manual_prune: float | None = None,
    manual_straight: float | None = None,
    manual_smooth: int = 2,
    manual_algorithm: str = "straight_skeleton",
    min_area: float = 0.0,
    progress_cb=None,
    hint_lines: list[LineString] | None = None,
    hint_snap_tol: float = 0.0002,
) -> tuple[list[LineString], list[dict]]:
    """
    Process each road polygon individually (segment-aware).

    Returns
    -------
    lines         : flat list of extracted LineString geometries
    method_report : list of dicts, one per processed polygon:
                    {idx, algorithm, reason, metrics}
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
    method_report: list[dict] = []

    for k, (_, row) in enumerate(rows):
        geom = row.geometry

        if use_auto:
            p = auto_tune_params(geom)
            prune     = p["prune"]
            straight  = p["straight"]
            smooth    = p["smooth"]
            algorithm = p["algorithm"]
            reason    = p["reason"]
            metrics   = p.get("metrics", {})
            info = {**p, "idx": k, "total": total}
        else:
            prune     = manual_prune    if manual_prune    is not None else 0.1
            straight  = manual_straight if manual_straight is not None else 0.00002
            smooth    = manual_smooth
            algorithm = manual_algorithm
            reason    = f"manual ({algorithm})"
            metrics   = {}
            info = {"prune": prune, "straight": straight, "smooth": smooth,
                    "algorithm": algorithm, "idx": k, "total": total}

        if progress_cb:
            progress_cb(k + 1, total, info)

        method_report.append({
            "idx": k,
            "algorithm": algorithm,
            "reason": reason,
            "metrics": metrics,
        })

        line = process_single(geom, prune, straight, smooth, algorithm)
        if line is None:
            continue

        if isinstance(line, MultiLineString):
            results.extend(g for g in line.geoms if not g.is_empty)
        elif not line.is_empty:
            results.append(line)

    # Post-processing: smooth approach arms at junctions
    results = smooth_junctions(results)

    # Apply routing hints as a post-processing guided snap
    if hint_lines:
        results = apply_hints(results, hint_lines, hint_snap_tol)

    return results, method_report


# ─────────────────────────────────────────────────────────────────────────────
# CONNECTIVITY / EXPORT
# ─────────────────────────────────────────────────────────────────────────────

def prune_dead_ends(
    lines: list[LineString],
    threshold: float,
    coord_decimals: int = 5,
) -> list[LineString]:
    """
    Iteratively remove dangling branch segments shorter than *threshold*.

    A dead end is a segment whose start OR end point connects to nothing else
    (degree-1 node in the line network).  Removing it may expose new dead ends,
    so the loop runs until no more removals occur.
    """
    if threshold <= 0 or not lines:
        return lines

    def rp(p):
        return (round(p[0], coord_decimals), round(p[1], coord_decimals))

    current = list(lines)
    while True:
        deg: dict = {}
        for line in current:
            c = list(line.coords)
            if len(c) < 2:
                continue
            for pt in (rp(c[0]), rp(c[-1])):
                deg[pt] = deg.get(pt, 0) + 1

        next_lines = []
        pruned_any = False
        for line in current:
            c = list(line.coords)
            if len(c) < 2:
                next_lines.append(line)
                continue
            s, e = rp(c[0]), rp(c[-1])
            if line.length < threshold and (deg.get(s, 1) == 1 or deg.get(e, 1) == 1):
                pruned_any = True
            else:
                next_lines.append(line)

        current = next_lines
        if not pruned_any:
            break

    return current


def snap_endpoints(
    lines: list[LineString], tolerance: float | None = None
) -> list[LineString]:
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
            pass
    out.to_file(output_path, driver="GeoJSON")
    return len(out)
