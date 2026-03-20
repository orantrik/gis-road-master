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
=====================================================================
HOW TO RUN (two options):

  Option A - Execute Python Script
    Tools -> Execute Python Script -> select this file
    (requires Python Editor Script Plugin enabled + editor restart)

  Option B - Python Console (always works)
    Window -> Output Log
    Change the left dropdown from "Cmd" to "Python"
    Paste the contents of this file and press Enter

CONFIGURATION - edit the two lines marked CONFIGURE below.

What it does
------------
Reads the companion JSON file (exported alongside the FBX), then
creates one Actor with a SplineComponent per centerline in the
currently open level, named Road_0000, Road_0001, etc.

Requirements
------------
  Unreal Engine 5.x
  Python Editor Script Plugin enabled
  Editor Scripting Utilities Plugin enabled
"""

import json, os, unreal

# ── CONFIGURE ─────────────────────────────────────────────────────
JSON_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "{{json_name}}")
SCALE     = 100000.0   # multiply GIS coordinates -> Unreal cm
#   1 geographic degree ~ 111 km ~ 11,100,000 cm, but tune to your data.
#   If roads appear tiny: increase SCALE.  If they are huge: decrease it.
# ──────────────────────────────────────────────────────────────────

with open(JSON_FILE, encoding="utf-8") as _f:
    roads = json.load(_f)

def _spawn_road(idx, flat_pts):
    pts = [(flat_pts[i], flat_pts[i+1], flat_pts[i+2])
           for i in range(0, len(flat_pts), 3)]
    if len(pts) < 2:
        return False

    ref_x, ref_y = pts[0][0], pts[0][1]

    # Spawn a plain Actor then register a SplineComponent onto it.
    # EditorLevelLibrary works in UE5 with Editor Scripting Utilities.
    actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
        unreal.Actor,
        unreal.Vector(0.0, 0.0, 0.0),
        unreal.Rotator(0.0, 0.0, 0.0),
    )
    actor.set_actor_label("Road_{:04d}".format(idx))

    spline = actor.add_component_by_class(
        unreal.SplineComponent, False, unreal.Transform(), True)

    if spline is None:
        unreal.log_warning("Road_{:04d}: SplineComponent could not be added".format(idx))
        actor.destroy_actor()
        return False

    spline.clear_spline_points(True)
    for x, y, z in pts:
        spline.add_spline_world_point(
            unreal.Vector((x - ref_x) * SCALE,
                          (y - ref_y) * SCALE,
                          z * SCALE))
    spline.update_spline()
    return True

created = 0
with unreal.ScopedEditorTransaction("GIS Road Master: Import Splines"):
    for i, flat in enumerate(roads):
        if _spawn_road(i, flat):
            created += 1

unreal.log("GIS Road Master: created {} SplineActors (scale={})".format(created, SCALE))
unreal.log("Tip: select all Road_* actors and adjust scale in Details if roads look too big/small.")
'''


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
    valid_lines = [
        ln for ln in lines
        if ln is not None and not ln.is_empty
        and len(list(ln.coords)) >= min_points
    ]

    # ── Collect geometry data ─────────────────────────────────────────
    curves: list[tuple] = []    # (ctrl_pts, knots, bezier_json) per line
    all_ctrl_flat: list[list] = []

    for line in valid_lines:
        coords = list(line.coords)
        segs = _catmull_rom_to_bezier(coords)
        if not segs:
            continue
        ctrl_pts, knots = _segs_to_nurbs(segs, z=z_value)
        bzjson = _ctrl_to_json_flat(ctrl_pts, z=z_value)
        curves.append((ctrl_pts, knots, bzjson))
        flat = [v for pt in ctrl_pts for v in [pt[0], pt[1], pt[2]]]
        all_ctrl_flat.append(flat)

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
    json_path.write_text(
        json.dumps(all_ctrl_flat, separators=(",", ":")),
        encoding="utf-8")

    # ── Write Unreal companion script ─────────────────────────────────
    script_path = fbx_path.with_name(fbx_path.stem + "_unreal_splines.py")
    script_path.write_text(
        _UNREAL_SCRIPT_TEMPLATE.replace("{{json_name}}", json_path.name),
        encoding="utf-8")

    return len(curves)
