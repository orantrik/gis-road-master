"""
build_installer.py
==================
Builds GIS_Road_Master_Setup.exe — a single-file Windows installer that
bundles the complete built app and extracts it on the target machine.

Usage
-----
    cd <project root>
    python installer/build_installer.py

Output
------
    dist/GIS_Road_Master_Setup.exe   (~350 MB)
"""
import os
import sys
import shutil
import zipfile
import subprocess
from pathlib import Path

ROOT     = Path(__file__).parent.parent
DIST_APP = ROOT / "dist" / "GIS Road Master"
INST_DIR = ROOT / "installer"
WORK_DIR = INST_DIR / "_build_work"
ZIP_PATH = INST_DIR / "app_bundle.zip"

# ── Step 1: verify the app has been built ─────────────────────────
print("=" * 60)
print("GIS Road Master — Installer Builder")
print("=" * 60)

if not DIST_APP.exists():
    print(f"\n[!] App not found at: {DIST_APP}")
    print("    Run PyInstaller first:  python -m PyInstaller gis_road_master.spec --noconfirm")
    sys.exit(1)

# ── Step 2: zip the built app ─────────────────────────────────────
print(f"\n[1/3] Zipping app from: {DIST_APP}")
if ZIP_PATH.exists():
    ZIP_PATH.unlink()

files = list(DIST_APP.rglob("*"))
n = len(files)
with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED,
                     compresslevel=6) as zf:
    for i, f in enumerate(files):
        if f.is_file():
            arc = Path("GIS Road Master") / f.relative_to(DIST_APP)
            zf.write(f, arc)
            if i % 20 == 0:
                print(f"    [{i:4d}/{n}] {f.name}", end="\r")

size_mb = ZIP_PATH.stat().st_size / 1024 / 1024
print(f"\n    Done — {ZIP_PATH.name}  ({size_mb:.1f} MB)")

# ── Step 3: build installer exe with PyInstaller ──────────────────
print(f"\n[2/3] Compiling installer with PyInstaller…")
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Build the --add-data argument: bundle the zip into the one-file exe
add_data = f"{ZIP_PATH};."   # Windows separator

cmd = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--windowed",
    "--clean",
    "--noconfirm",
    f"--name=GIS_Road_Master_Setup",
    f"--distpath={ROOT / 'dist'}",
    f"--workpath={WORK_DIR}",
    f"--specpath={WORK_DIR}",
    f"--add-data={add_data}",
    "--hidden-import=tkinter",
    "--hidden-import=tkinter.ttk",
    str(INST_DIR / "installer_main.py"),
]

result = subprocess.run(cmd, cwd=ROOT)
if result.returncode != 0:
    print("\n[!] PyInstaller failed — see output above.")
    sys.exit(result.returncode)

# ── Step 4: clean up build artefacts ─────────────────────────────
print(f"\n[3/3] Cleaning up build artefacts…")
shutil.rmtree(WORK_DIR, ignore_errors=True)
# Remove the intermediate zip (the exe already contains it)
ZIP_PATH.unlink(missing_ok=True)

setup_exe = ROOT / "dist" / "GIS_Road_Master_Setup.exe"
size_mb   = setup_exe.stat().st_size / 1024 / 1024

print()
print("=" * 60)
print(f"  SUCCESS")
print(f"  Output : {setup_exe}")
print(f"  Size   : {size_mb:.1f} MB")
print("=" * 60)
print()
print("Send GIS_Road_Master_Setup.exe to your friend.")
print("They just double-click it — no Python needed.")
