# GIS Road Master

A professional desktop application for extracting and editing road centrelines from polygon road-plan data. Built in Python with a modern dark UI (ttkbootstrap) and an embedded live-preview map.

---

## Features

| Feature | Description |
|---|---|
| **Segment-aware processing** | Each road polygon is processed individually, not dissolved into one blob — far cleaner results |
| **Auto-Tune** | Automatically selects pruning, simplification, and smoothing per segment based on polygon width, area, and convex-hull complexity |
| **Live smooth preview** | Drag the Smoothing slider and the map updates instantly (no re-running the expensive centerline step) |
| **Precision Editor** | Draw an edit box, open an interactive eraser, click lines to delete, Ctrl+Z to undo |
| **Snap on merge** | Precision lines are automatically snapped to the endpoints of the surrounding global network |
| **Clean GeoJSON export** | Reprojects to WGS 84 (EPSG:4326) for standard GeoJSON compliance |
| **Hebrew support** | `mavat_name` column displayed correctly via Segoe UI |

---

## Installation

```bash
pip install -r requirements.txt
```

`ttkbootstrap` is optional — if it is not installed the app falls back to a clean `ttk/clam` theme.

---

## Running

```bash
python main.py
```

---

## Typical Workflow

1. **Load** a GeoJSON file with road polygon geometries.
2. **Filter** by plan number (`pl_number`) and road type (`mavat_name`) using the drag-select checklists.
3. **Select parameters** — leave *Auto-Tune* enabled (recommended) or switch to Manual and adjust the sliders.
4. Click **PROCESS GLOBAL MAP** — centerlines appear in the embedded map.
5. *(Optional)* Click **DRAW EDIT BOX**, drag a rectangle over a region that needs correction, press **Enter**.
6. Click **OPEN PRECISION EDITOR** — a popup shows the local lines; click to erase bad ones, Ctrl+Z to undo.
7. Close the editor and click **APPLY EDITS TO GLOBAL MAP**.
8. Click **EXPORT FINAL GeoJSON** to save the result.

---

## Input Data Format

The GeoJSON must contain **road polygon geometries** plus (optionally):

| Column | Description |
|---|---|
| `pl_number` | Plan/project number (used for filtering) |
| `mavat_name` | Road type label in Hebrew (used for filtering; roads containing `דרך` are pre-selected) |

---

## Algorithm Details

### Auto-Tune Logic

Each polygon is analysed for:
- **Width** = `4 × area / perimeter` (scale-invariant estimate of road width)
- **Complexity** = `area / convex_hull_area` (1 = convex shape, 0 = very tortuous)
- **Vertex count** of the exterior ring

Parameters are derived as:
| Parameter | Formula |
|---|---|
| Pruning | `1.5 × width` — removes branches shorter than 1.5× the road width |
| Straighten | `width × factor` where factor ∈ {0.05, 0.20, 0.50} based on complexity |
| Smoothing | 1–4 passes of Chaikin's algorithm based on vertex count |

### Centerline Extraction

Uses [pygeoops](https://github.com/theroggy/pygeoops) for robust Voronoi-based centreline extraction.

### Smoothing

[Chaikin's corner-cutting algorithm](https://www.cs.unc.edu/~dm/UNC/COMP258/LECTURES/Chaikins-Algorithm.pdf) — produces wave-free curves with no ringing artefacts.

---

## Project Structure

```
gis-road-master/
├── main.py           # Application entry point and main UI class
├── algorithms.py     # Pure-Python geometry processing (no UI)
├── ui_components.py  # Reusable tkinter widgets
├── requirements.txt
└── README.md
```

---

## License

MIT
