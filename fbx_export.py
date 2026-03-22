"""
fbx_export.py – FBX ASCII 7.4.0 export for GIS Road Master centerlines.

Unreal Engine compatibility
---------------------------
Unreal's FBX importer does not import NurbsCurve geometry as splines;
it reads only mesh, skeleton, camera, and light objects.  To get Bezier
spline data into Unreal:

  1. This exporter writes every centerline as a null Model node that
     carries a custom "BezierControlPoints" string property containing
     JSON-encoded [x,y,z, x,y,z, ...] Catmull-Rom Bezier control points.

  2. A companion *_unreal_splines.py* script is written next to the FBX.
     Run it inside the Unreal Editor (Tools → Execute Python Script) to
     turn those null nodes into SplineActor objects in the current level.

  3. The NurbsCurve geometry is *also* written as a sibling geometry node
     so the file still opens correctly in Blender, Maya, and Cinema 4D.

FBX structure required by Unreal
----------------------------------
  FBXHeaderExtension   (present)
  GlobalSettings       (present)
  Documents            ← was missing; Unreal parser fails without it
  References           ← was missing
  Definitions          (present)
  Objects              (present)
  Connections          (present)
  Takes                ← was missing; Unreal parser fails without it
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Sequence

import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# CRS / REPROJECTION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _is_geographic(crs) -> bool:
    """Return True if *crs* is already a geographic (lat/lon) CRS."""
    try:
        from pyproj import CRS as _CRS
        return _CRS(crs).is_geographic
    except Exception:
        return False


def _reproject_to_wgs84(lines: list, src_crs) -> list:
    """
    Reproject a list of Shapely LineStrings to WGS 84 (EPSG:4326).
    Coordinates come out in (longitude, latitude) order.
    If *src_crs* is already geographic the lines are returned unchanged.
    """
    if _is_geographic(src_crs):
        return lines
    try:
        from pyproj import Transformer
        from shapely.ops import transform as _shp_transform
        tr = Transformer.from_crs(src_crs, "EPSG:4326", always_xy=True)
        return [_shp_transform(tr.transform, ln) for ln in lines]
    except Exception as exc:
        import warnings
        warnings.warn(f"CRS reprojection failed ({exc}); coordinates kept as-is.")
        return lines


# ─────────────────────────────────────────────────────────────────────────────
# CURVE MATH
# ─────────────────────────────────────────────────────────────────────────────

def _catmull_rom_to_bezier(coords: Sequence) -> list[tuple]:
    """
    Convert a 2-D polyline to cubic Bezier segment tuples.

    Returns [(p0, cp1, cp2, p3), …] – one tuple per segment.
    Uses Catmull-Rom parameterisation for C1-continuous (smooth) joins:
        cp1_i = P[i]   + (P[i+1] − P[i−1]) / 6
        cp2_i = P[i+1] − (P[i+2] − P[i]  ) / 6
    Ghost points at boundaries are reflected end-points.
    """
    pts = [np.array(c[:2], dtype=float) for c in coords]
    n = len(pts)
    if n < 2:
        return []
    segs: list[tuple] = []
    for i in range(n - 1):
        p0 = pts[i]
        p3 = pts[i + 1]
        prev = pts[i - 1] if i > 0     else 2.0 * p0 - p3
        nxt  = pts[i + 2] if i < n - 2 else 2.0 * p3 - p0
        cp1 = p0 + (p3 - prev) / 6.0
        cp2 = p3 - (nxt  - p0) / 6.0
        segs.append((p0, cp1, cp2, p3))
    return segs


def _segs_to_nurbs(
    segs: list[tuple],
    z: float = 0.0,
) -> tuple[list[tuple], list[int]]:
    """
    Pack cubic Bezier segments into degree-3 NURBS representation.

    Triple interior knots → one full Bezier segment per knot span (C0).
    The Catmull-Rom CP layout already gives visually C1-smooth joins.

    Returns
    -------
    ctrl_pts : list of (x, y, z, w)  with w=1 (non-rational)
    knots    : integer knot vector satisfying len = len(ctrl)+degree+1
    """
    ctrl: list[tuple] = []
    for i, (p0, cp1, cp2, p3) in enumerate(segs):
        if i == 0:
            ctrl.append((float(p0[0]),  float(p0[1]),  z, 1.0))
        ctrl.append((float(cp1[0]), float(cp1[1]), z, 1.0))
        ctrl.append((float(cp2[0]), float(cp2[1]), z, 1.0))
        ctrl.append((float(p3[0]),  float(p3[1]),  z, 1.0))

    n_seg = len(segs)
    knots: list[int] = [0, 0, 0, 0]
    for k in range(1, n_seg):
        knots += [k, k, k]
    knots += [n_seg, n_seg, n_seg, n_seg]
    assert len(knots) == len(ctrl) + 4, "NURBS knot count mismatch"
    return ctrl, knots


def _ctrl_to_json_flat(ctrl: list[tuple], z: float = 0.0) -> str:
    """Flatten control points to a compact JSON array [x,y,z, x,y,z, ...]."""
    flat: list[float] = []
    for pt in ctrl:
        flat.extend([round(pt[0], 8), round(pt[1], 8), round(pt[2] if len(pt) > 2 else z, 8)])
    return json.dumps(flat, separators=(",", ":"))


# ─────────────────────────────────────────────────────────────────────────────
# FBX ASCII WRITING
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_array(values: list, per_line: int = 8) -> str:
    strs = [f"{v:.10g}" for v in values]
    lines = []
    for i in range(0, len(strs), per_line):
        lines.append(",".join(strs[i: i + per_line]))
    return ",\n\t\t\t".join(lines)


def _fbx_header(creator: str = "GIS Road Master") -> str:
    t = time.localtime()
    return (
        f"; FBX 7.4.0 project file\n"
        f"; Created by {creator}\n\n"
        f"FBXHeaderExtension:  {{\n"
        f"\tFBXHeaderVersion: 1003\n"
        f"\tFBXVersion: 7400\n"
        f"\tCreationTimeStamp:  {{\n"
        f"\t\tVersion: 1000\n"
        f"\t\tYear: {t.tm_year}\n"
        f"\t\tMonth: {t.tm_mon}\n"
        f"\t\tDay: {t.tm_mday}\n"
        f"\t\tHour: {t.tm_hour}\n"
        f"\t\tMinute: {t.tm_min}\n"
        f"\t\tSecond: {t.tm_sec}\n"
        f"\t\tMillisecond: 0\n"
        f"\t}}\n"
        f"\tCreator: \"{creator}\"\n"
        f"\tOtherFlags:  {{\n"
        f"\t\tFlagPLE: 0\n"
        f"\t}}\n"
        f"}}\n\n"
        f"FileId: \"GISRoadMaster\"\n"
        f"CreationTime: \"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d} "
        f"{t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}:000\"\n"
        f"Creator: \"{creator}\"\n\n"
        f"GlobalSettings:  {{\n"
        f"\tVersion: 1000\n"
        f"\tProperties70:  {{\n"
        f"\t\tP: \"UpAxis\", \"int\", \"Integer\", \"\",1\n"
        f"\t\tP: \"UpAxisSign\", \"int\", \"Integer\", \"\",1\n"
        f"\t\tP: \"FrontAxis\", \"int\", \"Integer\", \"\",2\n"
        f"\t\tP: \"FrontAxisSign\", \"int\", \"Integer\", \"\",1\n"
        f"\t\tP: \"CoordAxis\", \"int\", \"Integer\", \"\",0\n"
        f"\t\tP: \"CoordAxisSign\", \"int\", \"Integer\", \"\",1\n"
        f"\t\tP: \"InheritType\", \"int\", \"Integer\", \"\",1\n"
        f"\t\tP: \"UnitScaleFactor\", \"double\", \"Number\", \"\",1\n"
        f"\t}}\n"
        f"}}\n\n"
    )


def _fbx_documents(doc_id: int = 1000000000) -> str:
    """Documents section – required by Unreal's FBX parser."""
    return (
        f"Documents:  {{\n"
        f"\tCount: 1\n"
        f"\tDocument: {doc_id}, \"\", \"Scene\" {{\n"
        f"\t\tProperties70:  {{\n"
        f"\t\t\tP: \"SourceObject\", \"object\", \"\", \"\"\n"
        f"\t\t\tP: \"ActiveAnimStackName\", \"KString\", \"\", \"\", \"\"\n"
        f"\t\t}}\n"
        f"\t\tRootNode: 0\n"
        f"\t}}\n"
        f"}}\n\n"
        f"References:  {{\n"
        f"}}\n\n"
    )


def _fbx_definitions(n_curves: int) -> str:
    # scene root + geometry + model per curve
    total = 1 + n_curves * 2
    return (
        f"Definitions:  {{\n"
        f"\tVersion: 100\n"
        f"\tCount: {total}\n"
        f"\tObjectType: \"Geometry\" {{\n"
        f"\t\tCount: {n_curves}\n"
        f"\t\tPropertyTemplate: \"FbxNurbsCurve\" {{\n"
        f"\t\t\tProperties70:  {{\n"
        f"\t\t\t}}\n"
        f"\t\t}}\n"
        f"\t}}\n"
        f"\tObjectType: \"Model\" {{\n"
        f"\t\tCount: {n_curves}\n"
        f"\t\tPropertyTemplate: \"FbxNull\" {{\n"
        f"\t\t\tProperties70:  {{\n"
        f"\t\t\t}}\n"
        f"\t\t}}\n"
        f"\t}}\n"
        f"}}\n\n"
    )


def _fbx_geometry(geo_id: int, name: str,
                  ctrl_pts: list, knots: list) -> str:
    n_ctrl  = len(ctrl_pts)
    n_knots = len(knots)
    flat_ctrl: list[float] = [v for pt in ctrl_pts for v in pt]
    ctrl_str = _fmt_array(flat_ctrl, per_line=4)
    knot_str = _fmt_array(knots,     per_line=8)
    return (
        f"\tGeometry: {geo_id}, \"Geometry::{name}\", \"NurbsCurve\" {{\n"
        f"\t\tVersion: 100\n"
        f"\t\tNurbsCurveVersion: 100\n"
        f"\t\tOrder: 4\n"
        f"\t\tDimensions: 3\n"
        f"\t\tStep: 1\n"           # 1 = open curve
        f"\t\tClosed: 0\n"
        f"\t\tPoints: *{n_ctrl * 4} {{\n"
        f"\t\t\ta: {ctrl_str}\n"
        f"\t\t}}\n"
        f"\t\tKnotVector: *{n_knots} {{\n"
        f"\t\t\ta: {knot_str}\n"
        f"\t\t}}\n"
        f"\t}}\n"
    )


def _fbx_model(model_id: int, name: str, bezier_json: str) -> str:
    """
    Null Model node with a custom 'BezierControlPoints' string property.

    The custom property stores the Bezier control points as a compact
    JSON array [x,y,z, x,y,z, …] so that an Unreal Python script can
    read them and build SplineActor objects.
    """
    # Escape quotes inside the JSON string for FBX embedding
    safe_json = bezier_json.replace('"', '\\"')
    return (
        f"\tModel: {model_id}, \"Model::{name}\", \"null\" {{\n"
        f"\t\tVersion: 232\n"
        f"\t\tProperties70:  {{\n"
        f"\t\t\tP: \"RotationActive\", \"bool\", \"\", \"\",1\n"
        f"\t\t\tP: \"InheritType\", \"enum\", \"\", \"\",1\n"
        f"\t\t\tP: \"ScalingMax\", \"Vector3D\", \"Vector\", \"\",0,0,0\n"
        f"\t\t\tP: \"DefaultAttributeIndex\", \"int\", \"Integer\", \"\",0\n"
        f"\t\t\tP: \"Lcl Translation\", \"Lcl Translation\", \"\", \"A\",0,0,0\n"
        f"\t\t\tP: \"Lcl Rotation\", \"Lcl Rotation\", \"\", \"A\",0,0,0\n"
        f"\t\t\tP: \"Lcl Scaling\", \"Lcl Scaling\", \"\", \"A\",1,1,1\n"
        f"\t\t\tP: \"BezierControlPoints\", \"KString\", \"\", \"\", \"{safe_json}\"\n"
        f"\t\t}}\n"
        f"\t\tShading: Y\n"
        f"\t\tCulling: \"CullingOff\"\n"
        f"\t}}\n"
    )


def _fbx_takes() -> str:
    """Takes section – required by Unreal's FBX parser (can be empty)."""
    return (
        f"Takes:  {{\n"
        f"\tCurrent: \"\"\n"
        f"}}\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
# UNREAL PYTHON COMPANION SCRIPT
# ─────────────────────────────────────────────────────────────────────────────

_UNREAL_SCRIPT_TEMPLATE = '''\
"""
unreal_import_splines.py  -  GIS Road Master companion script
Generated by: GIS Road Master v1.1  (Cesium Georeferencer support)
Run via:  Tools → Execute Python Script → select this file
Requires: Python Editor Script Plugin + Editor Scripting Utilities Plugin

Features
--------
- Auto-detects CesiumGeoreference actor → uses lon/lat transform for
  pixel-accurate geo-registration on the Cesium globe.
- Falls back to scale-based positioning when Cesium is not present.
- Reads both v1.0 (bare list) and v1.1 (dict with CRS metadata) JSON.
- Two-pass spawn: fills all spline points before running construction
  scripts, preventing BP_Road_Creator from overwriting earlier actors.
- Skips actors that already exist in the level (preserves your edits).
"""

import json, math, os, sys, unreal

# ── Defaults written by GIS Road Master (editable in the GUI) ─────
_DEFAULT_JSON      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "{{json_name}}")
_DEFAULT_BP        = "{{blueprint_path}}"
_DEFAULT_SCALE     = {{scale}}
_DEFAULT_MERGE     = {{merge}}
# ──────────────────────────────────────────────────────────────────


# ═══════════════════════════════════════════════════════════════════
#  IMPORT LOGIC
# ═══════════════════════════════════════════════════════════════════

def _to_pts(flat):
    return [(flat[i], flat[i+1], flat[i+2]) for i in range(0, len(flat), 3)]

def _dist2d(a, b):
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2)

def _road_length(road):
    return sum(_dist2d(road[i], road[i+1]) for i in range(len(road)-1))

def _chain_roads(road_list, max_dist_gis):
    remaining = list(road_list)
    chains, current = [], [remaining.pop(0)]
    while remaining:
        last = current[-1][-1]
        best_i, best_rev, best_d = 0, False, float("inf")
        for i, r in enumerate(remaining):
            d_s = _dist2d(last, r[0])
            d_e = _dist2d(last, r[-1])
            if d_s < best_d: best_d, best_i, best_rev = d_s, i, False
            if d_e < best_d: best_d, best_i, best_rev = d_e, i, True
        if max_dist_gis > 0 and best_d > max_dist_gis:
            chains.append([p for road in current for p in road])
            current = [remaining.pop(best_i)]
        else:
            nxt = remaining.pop(best_i)
            current.append(nxt[::-1] if best_rev else nxt)
    chains.append([p for road in current for p in road])
    return chains


def _find_cesium_georeference():
    """
    Return the CesiumGeoreference actor in the current level, or None.
    Tries both the Python class name and a string-based fallback so it
    works even if the Cesium plugin is loaded but the class is unknown.
    """
    try:
        for actor in unreal.EditorLevelLibrary.get_all_level_actors():
            cls_name = actor.get_class().get_name()
            if "CesiumGeoreference" in cls_name:
                return actor
    except Exception:
        pass
    return None


def _make_coord_converter(georeference, ref_x, ref_y, scale, is_geographic, log_fn):
    """
    Return a function  f(x, y, z) -> unreal.Vector  that converts
    one GIS point to Unreal world-space coordinates.

    If *georeference* is not None AND the data is geographic (lon/lat):
        Uses CesiumGeoreference.transform_longitude_latitude_height_to_unreal()
        → perfectly geo-registered in the Cesium world.

    Otherwise:
        Falls back to the origin-relative scale approach.
    """
    if georeference is not None and is_geographic:
        log_fn("Cesium Georeference found — using geographic transform (lon/lat → world).")
        def _cesium(x, y, z):
            # x = longitude, y = latitude, z = height in metres
            try:
                return georeference.transform_longitude_latitude_height_to_unreal(
                    unreal.Vector(x, y, z))
            except Exception:
                # Some Cesium versions expose it via BlueprintLibrary
                return unreal.CesiumBlueprintLibrary \
                    .transform_longitude_latitude_height_to_unreal(
                        georeference, x, y, z)
        return _cesium
    else:
        if georeference is not None and not is_geographic:
            log_fn("WARNING: Cesium Georeference found but data CRS is projected (not lat/lon).")
            log_fn("         Re-export from GIS Road Master to embed WGS-84 coordinates,")
            log_fn("         then the Cesium transform will be used automatically.")
        elif is_geographic:
            log_fn("No Cesium Georeference actor in level — using scale-based positioning.")
            log_fn("Add a CesiumGeoreference actor for accurate geo-registration.")
        def _scale(x, y, z):
            return unreal.Vector((x - ref_x) * scale,
                                 (y - ref_y) * scale,
                                 z * scale)
        return _scale


def run_import(json_file, blueprint_path, scale, merge, max_connect_dist, log_fn):
    """Core import. log_fn(msg) is called for each status line."""
    try:
        with open(json_file, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        log_fn("ERROR loading JSON: {}".format(e))
        return

    # Support both v1.0 format (bare list) and v1.1+ format (dict with metadata)
    if isinstance(raw, dict):
        roads        = raw.get("roads", [])
        file_crs     = raw.get("crs", "unknown")
        is_geographic = raw.get("coord_type", "geographic") == "geographic"
        log_fn("JSON v{}  CRS: {}".format(raw.get("version", "?"), file_crs))
    else:
        roads        = raw           # legacy bare list
        file_crs     = "unknown"
        is_geographic = False        # unknown — use scale fallback
        log_fn("JSON v1.0 (legacy format) — no CRS metadata, using scale-based positioning.")

    if not roads:
        log_fn("ERROR: no roads found in JSON.")
        return

    ref_x, ref_y = roads[0][0], roads[0][1]
    log_fn("Reference origin: ({:.6f}, {:.6f})".format(ref_x, ref_y))

    bp_class = unreal.EditorAssetLibrary.load_blueprint_class(blueprint_path)
    if bp_class is None:
        log_fn("ERROR: Could not load Blueprint '{}'".format(blueprint_path))
        log_fn("Check the Content Browser path and try again.")
        return
    log_fn("Blueprint loaded: {}".format(blueprint_path))

    # ── Cesium Georeference detection ──────────────────────────────────────
    georeference = _find_cesium_georeference()
    to_ue = _make_coord_converter(georeference, ref_x, ref_y, scale,
                                  is_geographic, log_fn)

    eas = unreal.EditorLevelLibrary
    existing = {a.get_actor_label() for a in eas.get_all_level_actors()}

    def _centroid(pts):
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (sum(xs)/len(xs), sum(ys)/len(ys))

    def _spawn_actor(label, pts):
        """
        Spawn one Blueprint actor, populate its SplineComponent, and return
        the actor.  Construction script is NOT called here — caller must call
        actor.run_construction_script() after ALL actors have been populated.
        This prevents BP_Road_Creator (and similar tools) from triggering a
        global road-rebuild that overwrites previously spawned actors.
        """
        cx, cy = _centroid(pts)
        spawn_loc = to_ue(cx, cy, 0.0)
        try:
            actor = eas.spawn_actor_from_class(
                bp_class, spawn_loc, unreal.Rotator(0, 0, 0))
        except Exception as exc:
            log_fn("  ERROR spawning {}: {}".format(label, exc))
            return None

        if actor is None:
            log_fn("  ERROR: spawn_actor_from_class returned None for {}".format(label))
            return None

        actor.set_actor_label(label)

        spline = actor.get_component_by_class(unreal.SplineComponent)
        if spline is None:
            log_fn("  WARNING {}: Blueprint has no SplineComponent".format(label))
            actor.destroy_actor()
            return None

        spline.clear_spline_points(True)
        for x, y, z in pts:
            spline.add_spline_world_point(to_ue(x, y, z))
        spline.update_spline()
        return actor

    created = skipped = errors = 0
    new_actors = []   # collect here; run construction scripts in one final pass

    # ── Build the road list (merge or individual) ──────────────────────────
    if merge:
        road_pts = [_to_pts(flat) for flat in roads if len(flat) >= 6]
        threshold = max_connect_dist / scale if max_connect_dist > 0 else 0
        if threshold == 0 and road_pts:
            lengths = sorted(_road_length(r) for r in road_pts)
            threshold = lengths[len(lengths) // 2] * 3.0
        chains = _chain_roads(road_pts, threshold)
        log_fn("{} road(s) → {} cluster(s)  (gap threshold {:.6f} GIS units)".format(
            len(road_pts), len(chains), threshold))
        work = [("Road_Merged_{:04d}".format(ci), pts)
                for ci, pts in enumerate(chains)]
    else:
        work = []
        for i, flat in enumerate(roads):
            pts = _to_pts(flat)
            if len(pts) >= 2:
                work.append(("Road_{:04d}".format(i), pts))
        log_fn("{} road(s) to import".format(len(work)))

    # ── Pass 1: spawn actors + fill splines (no construction scripts yet) ──
    with unreal.ScopedEditorTransaction("GIS Road Master: Spawn Actors"):
        for label, pts in work:
            if label in existing:
                log_fn("  skipped {} (already in level — delete to reimport)".format(label))
                skipped += 1
                continue
            actor = _spawn_actor(label, pts)
            if actor is not None:
                new_actors.append(actor)
                created += 1
                log_fn("  spawned {}  ({} pts)".format(label, len(pts)))
            else:
                errors += 1

    # ── Pass 2: trigger construction scripts now that all splines are set ──
    if new_actors:
        log_fn("Running construction scripts on {} actor(s)…".format(len(new_actors)))
        with unreal.ScopedEditorTransaction("GIS Road Master: Construction Scripts"):
            for actor in new_actors:
                try:
                    actor.run_construction_script()
                except Exception as exc:
                    log_fn("  WARNING construction script failed: {}".format(exc))

    log_fn("")
    log_fn("── Summary ──────────────────────────────────────")
    log_fn("  Created : {}".format(created))
    log_fn("  Skipped : {}  (already existed)".format(skipped))
    log_fn("  Errors  : {}".format(errors))
    if skipped:
        log_fn("  Tip: delete Road_* actors you want to reimport, then run again.")
    unreal.log("GIS Road Master: created={} skipped={} errors={} scale={}".format(
        created, skipped, errors, scale))


# ═══════════════════════════════════════════════════════════════════
#  GUI  (PySide2 — bundled with Unreal Engine 5)
# ═══════════════════════════════════════════════════════════════════

try:
    from PySide2 import QtWidgets, QtCore, QtGui
except ImportError:
    # Fallback: run headless with defaults
    unreal.log_warning("PySide2 not available — running with default settings.")
    run_import(_DEFAULT_JSON, _DEFAULT_BP, _DEFAULT_SCALE, _DEFAULT_MERGE, 0,
               lambda m: unreal.log(m))
    raise SystemExit


class ImportDialog(QtWidgets.QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GIS Road Master — Unreal Spline Import")
        self.setMinimumWidth(560)
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)

        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(14, 14, 14, 14)

        # ── JSON file ──────────────────────────────────────────────
        grp_file = QtWidgets.QGroupBox("Data File")
        fl = QtWidgets.QHBoxLayout(grp_file)
        self.json_edit = QtWidgets.QLineEdit(_DEFAULT_JSON)
        browse_btn = QtWidgets.QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse_json)
        fl.addWidget(self.json_edit)
        fl.addWidget(browse_btn)
        root.addWidget(grp_file)

        # ── Blueprint ──────────────────────────────────────────────
        grp_bp = QtWidgets.QGroupBox("Unreal Blueprint")
        bl = QtWidgets.QFormLayout(grp_bp)
        self.bp_edit = QtWidgets.QLineEdit(_DEFAULT_BP)
        self.bp_edit.setPlaceholderText("/Game/MyBP/BP_Road")
        bl.addRow("Content path:", self.bp_edit)
        root.addWidget(grp_bp)

        # ── Scale + Merge ──────────────────────────────────────────
        grp_opts = QtWidgets.QGroupBox("Import Options")
        ol = QtWidgets.QFormLayout(grp_opts)
        ol.setSpacing(8)

        self.scale_spin = QtWidgets.QDoubleSpinBox()
        self.scale_spin.setRange(0.001, 1e9)
        self.scale_spin.setDecimals(1)
        self.scale_spin.setSingleStep(1000)
        self.scale_spin.setValue(_DEFAULT_SCALE)
        self.scale_spin.setToolTip(
            "1 degree ≈ 11 100 000 cm\\n"
            "1 metre  ≈ 100 cm\\n"
            "Increase if roads look tiny; decrease if they are huge.")
        ol.addRow("Scale (GIS → Unreal cm):", self.scale_spin)

        self.merge_chk = QtWidgets.QCheckBox("Merge roads into clusters (fewer actors)")
        self.merge_chk.setChecked(_DEFAULT_MERGE)
        self.merge_chk.toggled.connect(self._on_merge_toggle)
        ol.addRow("", self.merge_chk)

        self.maxdist_spin = QtWidgets.QDoubleSpinBox()
        self.maxdist_spin.setRange(0, 1e12)
        self.maxdist_spin.setDecimals(0)
        self.maxdist_spin.setSingleStep(10000)
        self.maxdist_spin.setValue(0)
        self.maxdist_spin.setSpecialValueText("Auto (3× median road length)")
        self.maxdist_spin.setToolTip(
            "Maximum gap (Unreal cm) allowed when chaining roads together.\\n"
            "Roads farther apart than this become separate actors.\\n"
            "0 = auto-calculate from your data.")
        self.maxdist_spin.setEnabled(_DEFAULT_MERGE)
        ol.addRow("Max connect distance (cm):", self.maxdist_spin)

        root.addWidget(grp_opts)

        # ── Log ────────────────────────────────────────────────────
        grp_log = QtWidgets.QGroupBox("Log")
        ll = QtWidgets.QVBoxLayout(grp_log)
        self.log_box = QtWidgets.QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(160)
        self.log_box.setFont(QtGui.QFont("Consolas", 9))
        ll.addWidget(self.log_box)
        root.addWidget(grp_log)

        # ── Buttons ────────────────────────────────────────────────
        btn_row = QtWidgets.QHBoxLayout()
        self.import_btn = QtWidgets.QPushButton("Import")
        self.import_btn.setDefault(True)
        self.import_btn.setFixedHeight(32)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.setFixedHeight(32)
        self.import_btn.clicked.connect(self._do_import)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.import_btn)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    # ── slots ──────────────────────────────────────────────────────

    def _browse_json(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select road curves JSON", self.json_edit.text(),
            "JSON files (*.json);;All files (*.*)")
        if path:
            self.json_edit.setText(path)

    def _on_merge_toggle(self, checked):
        self.maxdist_spin.setEnabled(checked)

    def _log(self, msg):
        self.log_box.appendPlainText(msg)
        QtWidgets.QApplication.processEvents()

    def _do_import(self):
        self.import_btn.setEnabled(False)
        self.log_box.clear()
        self._log("Starting import…")
        try:
            run_import(
                json_file       = self.json_edit.text().strip(),
                blueprint_path  = self.bp_edit.text().strip(),
                scale           = self.scale_spin.value(),
                merge           = self.merge_chk.isChecked(),
                max_connect_dist= self.maxdist_spin.value(),
                log_fn          = self._log,
            )
        except Exception as exc:
            self._log("EXCEPTION: {}".format(exc))
        finally:
            self.import_btn.setEnabled(True)


app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
dlg = ImportDialog()
dlg.exec_()
'''


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def export_fbx(
    lines: list,
    output_path: str,
    z_value: float = 0.0,
    min_points: int = 2,
    blueprint_path: str = "/Game/Road_Creator_Pro/Blueprints/BP_Road_Creator",
    scale: float = 100000.0,
    merge: bool = False,
    crs=None,
) -> int:
    """
    Export a list of Shapely LineStrings as FBX Bezier curves.

    Writes three files next to *output_path*:
      *.fbx                  – FBX ASCII 7.4.0, fully Unreal-compatible
      *_curves.json          – flat JSON array of Bezier control points
      *_unreal_splines.py    – Unreal Editor Python script to create splines

    Parameters
    ----------
    lines       : list of shapely.geometry.LineString
    output_path : destination .fbx path
    z_value     : Z coordinate for all vertices (default 0)
    min_points  : skip lines with fewer vertices than this

    Returns
    -------
    Number of curves written.
    """
    # ── Reproject to WGS84 if a source CRS is provided ───────────────
    geo_crs_str = None
    if crs is not None:
        lines = _reproject_to_wgs84(lines, crs)
        geo_crs_str = "EPSG:4326"   # output is always WGS84 after reprojection

    valid_lines = [
        ln for ln in lines
        if ln is not None and not ln.is_empty
        and len(list(ln.coords)) >= min_points
    ]

    # ── Collect geometry data ─────────────────────────────────────────
    curves: list[tuple] = []    # (ctrl_pts, knots, bezier_json) per line
    road_waypoints: list[list] = []   # original vertex coords per road (for JSON)

    for line in valid_lines:
        coords = list(line.coords)
        segs = _catmull_rom_to_bezier(coords)
        if not segs:
            continue
        ctrl_pts, knots = _segs_to_nurbs(segs, z=z_value)
        bzjson = _ctrl_to_json_flat(ctrl_pts, z=z_value)
        curves.append((ctrl_pts, knots, bzjson))
        # Store original vertices (not bezier ctrl pts) for the companion JSON
        if len(coords[0]) == 2:
            flat = [v for x, y in coords for v in (x, y, z_value)]
        else:
            flat = [v for x, y, z in coords for v in (x, y, z)]
        road_waypoints.append(flat)

    # ── Build FBX text ────────────────────────────────────────────────
    parts: list[str] = [
        _fbx_header(),
        _fbx_documents(),
        _fbx_definitions(len(curves)),
        "Objects:  {\n",
    ]
    connections: list[str] = []

    for idx, (ctrl_pts, knots, bzjson) in enumerate(curves):
        geo_id   = 100000 + idx * 2
        model_id = 100001 + idx * 2
        name     = f"road_{idx}"

        parts.append(_fbx_geometry(geo_id,   name, ctrl_pts, knots))
        parts.append(_fbx_model(  model_id,  name, bzjson))

        connections.append(f"\tC: \"OO\",{model_id},0\n")
        connections.append(f"\tC: \"OO\",{geo_id},{model_id}\n")

    parts.append("}\n\n")
    parts.append("Connections:  {\n")
    parts.extend(connections)
    parts.append("}\n\n")
    parts.append(_fbx_takes())

    # ── Write FBX ─────────────────────────────────────────────────────
    fbx_path = Path(output_path)
    fbx_path.write_text("".join(parts), encoding="utf-8")

    # ── Write companion JSON ──────────────────────────────────────────
    json_path = fbx_path.with_name(fbx_path.stem + "_curves.json")
    json_payload: dict | list
    if geo_crs_str:
        # v1.1 structured format — includes CRS so Unreal script can use Cesium
        json_payload = {
            "version": "1.1",
            "crs": geo_crs_str,
            "coord_type": "geographic",   # lon/lat/height
            "roads": road_waypoints,
        }
    else:
        # v1.0 legacy bare-list format (no CRS reprojection was done)
        json_payload = road_waypoints
    json_path.write_text(
        json.dumps(json_payload, separators=(",", ":")),
        encoding="utf-8")

    # ── Write Unreal companion script ─────────────────────────────────
    script_path = fbx_path.with_name(fbx_path.stem + "_unreal_splines.py")
    script_path.write_text(
        _UNREAL_SCRIPT_TEMPLATE
            .replace("{{json_name}}", json_path.name)
            .replace("{{blueprint_path}}", blueprint_path)
            .replace("{{scale}}", repr(float(scale)))
            .replace("{{merge}}", "True" if merge else "False"),
        encoding="utf-8")

    return len(curves)
