@echo off
setlocal enabledelayedexpansion

:: ============================================================
::  Build SAMS backend → dist\sams-backend\sams-backend.exe
::  Run this from the  backend\  directory.
:: ============================================================

echo.
echo  ============================================================
echo   SAMS Backend — PyInstaller Build
echo  ============================================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found. Install Python 3.10+ and add to PATH.
    pause & exit /b 1
)

:: Check PyInstaller
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo  Installing PyInstaller...
    pip install pyinstaller
)

:: Install/upgrade all backend dependencies (excluding torch for lean build)
echo  Installing dependencies...
pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo  WARNING: Some dependencies failed. Build may still work.
)

:: Clean previous build
if exist "dist\sams-backend" (
    echo  Cleaning previous build...
    rmdir /s /q "dist\sams-backend"
)
if exist "build\sams-backend" (
    rmdir /s /q "build\sams-backend"
)

:: Run PyInstaller
echo  Running PyInstaller...
python -m PyInstaller sams-backend.spec --noconfirm

if errorlevel 1 (
    echo.
    echo  ERROR: PyInstaller build failed.
    pause & exit /b 1
)

echo.
echo  ============================================================
echo   Build successful!
echo   Output: backend\dist\sams-backend\sams-backend.exe
echo.
echo   Next step: run  installer\build.bat  to create the installer.
echo  ============================================================
echo.
pause
