# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for GIS Road Master.

Build with:
    python -m PyInstaller gis_road_master.spec --noconfirm

Output: dist/GIS Road Master/GIS Road Master.exe
"""

from PyInstaller.utils.hooks import collect_all, collect_data_files

datas      = []
binaries   = []
hiddenimports = []

# ── Geospatial stack (includes GDAL/GEOS/PROJ native data files) ───────────
for pkg in [
    "geopandas",
    "pyogrio",   # geopandas I/O backend (replaces fiona on this install)
    "pyproj",
    "shapely",
    "pygeoops",
]:
    d, b, h = collect_all(pkg)
    datas         += d
    binaries      += b
    hiddenimports += h

# ── Numerics ────────────────────────────────────────────────────────────────
for pkg in ["numpy", "pandas", "PIL"]:
    d, b, h = collect_all(pkg)
    datas         += d
    binaries      += b
    hiddenimports += h

# ── Matplotlib (fonts, style sheets, locators …) ───────────────────────────
datas += collect_data_files("matplotlib")
hiddenimports += [
    "matplotlib.backends.backend_tkagg",
    "matplotlib.backends.backend_agg",
    "matplotlib.figure",
    "matplotlib.widgets",
]

# ── ttkbootstrap (optional – bundled when available) ───────────────────────
try:
    d, b, h = collect_all("ttkbootstrap")
    datas         += d
    binaries      += b
    hiddenimports += h
except Exception:
    pass  # app falls back to ttk/clam automatically

# ── tkinter (usually already present in the Python install) ────────────────
hiddenimports += [
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "_tkinter",
]

# ───────────────────────────────────────────────────────────────────────────
a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim unused heavy packages to keep the build smaller
        "IPython", "jupyter", "notebook", "scipy", "sklearn",
        "cv2", "PyQt5", "PyQt6", "wx",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GIS Road Master",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,        # UPX can break GDAL DLLs – leave off
    console=False,    # no black terminal window
    icon=None,        # replace with "icon.ico" if you add one
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="GIS Road Master",
)
