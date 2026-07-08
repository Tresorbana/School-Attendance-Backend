@echo off
setlocal enabledelayedexpansion

:: ============================================================
::  Build SAMS relay → dist\sams-relay\sams-relay.exe
::  Run this from the  backend\  directory.
:: ============================================================

echo.
echo  ============================================================
echo   SAMS Relay — PyInstaller Build
echo  ============================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found. Install Python 3.10+ and add to PATH.
    pause & exit /b 1
)

python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo  Installing PyInstaller...
    pip install pyinstaller
)

echo  Installing relay dependencies...
pip install fastapi uvicorn[standard] httpx pillow python-dotenv --quiet

if exist "dist\sams-relay" (
    echo  Cleaning previous build...
    rmdir /s /q "dist\sams-relay"
)
if exist "build\sams-relay" (
    rmdir /s /q "build\sams-relay"
)

echo  Running PyInstaller...
python -m PyInstaller sams-relay.spec --noconfirm

if errorlevel 1 (
    echo.
    echo  ERROR: PyInstaller build failed.
    pause & exit /b 1
)

echo.
echo  ============================================================
echo   Build successful!
echo   Output: backend\dist\sams-relay\sams-relay.exe
echo.
echo   Next step: run  installer\build.bat  to create the installer.
echo  ============================================================
echo.
pause
