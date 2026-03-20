@echo off
setlocal enabledelayedexpansion

echo ================================================
echo  GIS Road Master -- EXE Builder
echo ================================================
echo.

:: Use the same Python that runs the project
set PYTHON=python

:: Check Python
%PYTHON% --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found on PATH.
    echo Make sure your virtual environment is activated.
    pause
    exit /b 1
)

:: Install / upgrade PyInstaller silently
echo [1/3] Ensuring PyInstaller is installed...
%PYTHON% -m pip install pyinstaller --quiet --disable-pip-version-check
if errorlevel 1 (
    echo ERROR: Failed to install PyInstaller.
    pause
    exit /b 1
)
echo       OK.
echo.

:: Clean previous build artefacts
echo [2/3] Cleaning previous build...
if exist build  rmdir /s /q build
if exist dist   rmdir /s /q dist
echo       OK.
echo.

:: Build
echo [3/3] Running PyInstaller (this may take a few minutes)...
echo.
%PYTHON% -m PyInstaller gis_road_master.spec --noconfirm
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller failed. Check the output above for details.
    pause
    exit /b 1
)

echo.
echo ================================================
echo  Build complete!
echo.
echo  Executable: dist\GIS Road Master\GIS Road Master.exe
echo.
echo  You can run it directly from there, or copy the
echo  entire "GIS Road Master" folder anywhere you like.
echo ================================================
echo.
pause
