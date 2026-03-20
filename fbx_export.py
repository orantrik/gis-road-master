"""
fbx_export.py – FBX ASCII 7.4.0 export for GIS Road Master centerlines.

Each LineString is exported as a cubic Bezier NURBS curve using the
Catmull-Rom → Bezier conversion so the spline passes through every
original vertex with C1-continuous (smooth) joins between segments.

FBX representation
------------------
FBX has no native "Bezier" primitive; it uses NurbsCurve (degree 3).
A composite cubic Bezier with n-1 segments maps to NURBS with:
  • 3(n-1)+1 control points  (shared endpoints, not duplicated)
  • triple interior knots     (gives C0 continuity at joins – one full
                               Bezier segment per knot span)

For C1-smooth joins the Catmull-Rom control points P1/P2 at each join
are already collinear with the shared endpoint, so the curve visually
reads as fully smooth.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Sequence

import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# CURVE MATH
# ─────────────────────────────────────────────────────────────────────────────

def _catmull_rom_to_bezier(coords: Sequence) -> list[tuple]:
    """
    Convert a 2-D polyline to cubic Bezier segment tuples.

    Parameters
    ----------
    coords : sequence of (x, y) or (x, y, ...) points

    Returns
    -------
    list of (p0, cp1, cp2, p3) tuples – one per segment.
    Each point is a 1-D numpy array [x, y].

    Uses Catmull-Rom parameterisation:
        cp1_i = P[i]   + (P[i+1] − P[i−1]) / 6
        cp2_i = P[i+1] − (P[i+2] − P[i]  ) / 6
    Boundary tangents are reflected (ghost points).
    """
    pts = [np.array(c[:2], dtype=float) for c in coords]
    n = len(pts)
    if n < 2:
        return []

    segs: list[tuple] = []
    for i in range(n - 1):
        p0 = pts[i]
        p3 = pts[i + 1]
        prev = pts[i - 1] if i > 0     else 2.0 * p0 - p3   # ghost before start
        nxt  = pts[i + 2] if i < n - 2 else 2.0 * p3 - p0   # ghost after end
        cp1 = p0 + (p3 - prev) / 6.0
        cp2 = p3 - (nxt  - p0) / 6.0
        segs.append((p0, cp1, cp2, p3))

    return segs


def _segs_to_nurbs(
    segs: list[tuple],
    z: float = 0.0,
) -> tuple[list[tuple[float, float, float, float]], list[int]]:
    """
    Pack cubic Bezier segments into NURBS form (degree 3).

    Control points are listed without duplicating shared endpoints.
    Triple interior knots ensure each knot span is one full Bezier
    segment (C0 continuity at the join point between segments).

    Returns
    -------
    ctrl_pts : list of (x, y, z, w) with w = 1  (non-rational)
    knots    : integer knot vector
    """
    ctrl: list[tuple[float, float, float, float]] = []
    for i, (p0, cp1, cp2, p3) in enumerate(segs):
        if i == 0:
            ctrl.append((float(p0[0]),  float(p0[1]),  z, 1.0))
        ctrl.append((float(cp1[0]), float(cp1[1]), z, 1.0))
        ctrl.append((float(cp2[0]), float(cp2[1]), z, 1.0))
        ctrl.append((float(p3[0]),  float(p3[1]),  z, 1.0))

    n_seg = len(segs)
    knots: list[int] = [0, 0, 0, 0]
    for k in range(1, n_seg):
        knots += [k, k, k]                     # triple interior knot
    knots += [n_seg, n_seg, n_seg, n_seg]       # clamped end

    # Sanity check: len(knots) == len(ctrl) + degree + 1
    assert len(knots) == len(ctrl) + 4, "NURBS knot count mismatch"

    return ctrl, knots


# ─────────────────────────────────────────────────────────────────────────────
# FBX ASCII WRITING
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_array(values: list, per_line: int = 8) -> str:
    """Format a flat list of numbers for FBX 'a: ...' arrays."""
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
        f"FileId: \"\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00"
        f"\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\"\n"
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


def _fbx_definitions(n_curves: int) -> str:
    total = 1 + n_curves * 2   # root + (geometry + model) per curve
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
    """Write one NurbsCurve Geometry node."""
    n_ctrl = len(ctrl_pts)
    n_knots = len(knots)

    # Flatten control points: x,y,z,w per point
    flat_ctrl: list[float] = []
    for pt in ctrl_pts:
        flat_ctrl.extend(pt)

    ctrl_str  = _fmt_array(flat_ctrl, per_line=4)
    knot_str  = _fmt_array(knots,     per_line=8)

    return (
        f"\tGeometry: {geo_id}, \"Geometry::{name}\", \"NurbsCurve\" {{\n"
        f"\t\tVersion: 100\n"
        f"\t\tNurbsCurveVersion: 100\n"
        f"\t\tOrder: 4\n"               # degree 3  → order 4
        f"\t\tDimensions: 3\n"
        f"\t\tStep: 4\n"
        f"\t\tClosed: 0\n"
        f"\t\tPoints: *{n_ctrl * 4} {{\n"
        f"\t\t\ta: {ctrl_str}\n"
        f"\t\t}}\n"
        f"\t\tKnotVector: *{n_knots} {{\n"
        f"\t\t\ta: {knot_str}\n"
        f"\t\t}}\n"
        f"\t}}\n"
    )


def _fbx_model(model_id: int, name: str) -> str:
    """Write one null Model node that holds a curve geometry."""
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
        f"\t\t}}\n"
        f"\t\tShading: Y\n"
        f"\t\tCulling: \"CullingOff\"\n"
        f"\t}}\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def export_fbx(
    lines: list,
    output_path: str,
    z_value: float = 0.0,
    min_points: int = 2,
) -> int:
    """
    Export a list of Shapely LineStrings as FBX Bezier curves.

    Each LineString is converted to a cubic C1-continuous Bezier spline
    via Catmull-Rom parameterisation and written as a degree-3 NurbsCurve
    in FBX ASCII 7.4.0 format.

    Parameters
    ----------
    lines       : list of shapely.geometry.LineString
    output_path : destination .fbx file path
    z_value     : Z coordinate for all vertices (default 0 – flat 2-D)
    min_points  : skip lines with fewer than this many vertices

    Returns
    -------
    Number of curves written.
    """
    valid_lines = [
        ln for ln in lines
        if ln is not None and not ln.is_empty
        and len(list(ln.coords)) >= min_points
    ]

    parts: list[str] = [_fbx_header()]
    parts.append(_fbx_definitions(len(valid_lines)))
    parts.append("Objects:  {\n")

    connections: list[str] = []
    written = 0

    for idx, line in enumerate(valid_lines):
        coords = list(line.coords)

        segs = _catmull_rom_to_bezier(coords)
        if not segs:
            continue

        ctrl_pts, knots = _segs_to_nurbs(segs, z=z_value)

        geo_id   = 100000 + idx * 2
        model_id = 100001 + idx * 2
        name     = f"road_{idx}"

        parts.append(_fbx_geometry(geo_id, name, ctrl_pts, knots))
        parts.append(_fbx_model(model_id, name))

        # Model → scene root (0), Geometry → Model
        connections.append(f"\tC: \"OO\",{model_id},0")
        connections.append(f"\tC: \"OO\",{geo_id},{model_id}")

        written += 1

    parts.append("}\n\n")
    parts.append("Connections:  {\n")
    parts.extend(ln + "\n" for ln in connections)
    parts.append("}\n")

    Path(output_path).write_text("".join(parts), encoding="utf-8")
    return written
