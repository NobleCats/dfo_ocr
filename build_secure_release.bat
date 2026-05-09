@echo off
setlocal EnableExtensions

REM DFOGANG Raid Helper v1.0beta protected onedir release build.
REM This builds a ZIP-ready onedir distribution without exposing src/*.py.

cd /d "%~dp0"

echo.
echo ============================================================
echo  DFOGANG Raid Helper v1.0beta - Protected Onedir Build
echo ============================================================
echo.

set DFO_OCR_PROFILE=
set DFO_OCR_RECOGNITION_MODEL=

if exist cleanup_distribution_cache.ps1 (
    echo [1/8] Cleaning local cache and previous build outputs...
    powershell -NoProfile -ExecutionPolicy Bypass -File ".\cleanup_distribution_cache.ps1"
) else (
    echo [1/8] Cleaning build outputs...
    if exist build rmdir /s /q build
    if exist dist rmdir /s /q dist
)

if exist build_secure rmdir /s /q build_secure
mkdir build_secure
mkdir build_secure\protected_src

echo.
echo [2/8] Checking Python...
python --version
if errorlevel 1 (
    echo ERROR: Python is not available on PATH.
    pause
    exit /b 1
)

echo.
echo [3/8] Installing/upgrading build tools...
python -m pip install --upgrade pyinstaller cython setuptools wheel

echo.
echo [4/8] Verifying source compilation...
python -m compileall src
if errorlevel 1 (
    echo ERROR: Python compilation failed.
    pause
    exit /b 1
)

echo.
echo [5/8] Copying source to protected build workspace...
xcopy /y /q src\*.py build_secure\protected_src\ >nul
if errorlevel 1 (
    echo ERROR: Failed to copy src files.
    pause
    exit /b 1
)

echo from gui_app import main> build_secure\launcher.py
echo raise SystemExit(main())>> build_secure\launcher.py

echo.
echo [6/8] Compiling internal modules with Cython...
python build_secure_cython_setup.py build_ext --inplace
if errorlevel 1 (
    echo ERROR: Cython build failed.
    pause
    exit /b 1
)

echo.
echo [7/8] Removing plain Python sources from protected workspace...
del /q build_secure\protected_src\*.py
del /q build_secure\protected_src\*.c
for /d /r build_secure %%D in (__pycache__) do @if exist "%%D" rmdir /s /q "%%D"
del /s /q build_secure\*.pyc >nul 2>nul

echo.
echo [8/8] Building PyInstaller protected onedir distribution...
python -m PyInstaller --noconfirm --clean ".\DFOGANG_RaidHelper_protected_onedir.spec"
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller protected build failed.
    pause
    exit /b 1
)

echo.
echo Build complete.
echo.
echo Output folder:
echo   %CD%\dist\DFOGANG_RaidHelper
echo.
echo Verify no source files are exposed:
echo   powershell -NoProfile -Command "Get-ChildItem -Recurse '.\dist\DFOGANG_RaidHelper' -Filter *.py"
echo.
echo Test:
echo   dist\DFOGANG_RaidHelper\DFOGANG_RaidHelper.exe
echo.

pause
endlocal
