@echo off
setlocal EnableExtensions

REM DFOGANG Raid Helper v1.0beta protected onedir release build.
REM v3: Visual Studio path discovery is delegated to PowerShell to avoid batch
REM parser errors from environment variable names such as ProgramFiles(x86).

cd /d "%~dp0"

echo.
echo ============================================================
echo  DFOGANG Raid Helper v1.0beta - Protected Onedir Build
echo ============================================================
echo.

set DFO_OCR_PROFILE=
set DFO_OCR_RECOGNITION_MODEL=

echo [0/8] Initializing Visual Studio C++ build environment...

set "VCVARS_FILE=%TEMP%\dfogang_vcvars_path.txt"
if exist "%VCVARS_FILE%" del /q "%VCVARS_FILE%"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$candidates = @();" ^
  "$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe';" ^
  "if (Test-Path $vswhere) {" ^
  "  $install = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath;" ^
  "  if ($install) { $candidates += (Join-Path $install 'VC\Auxiliary\Build\vcvars64.bat') }" ^
  "}" ^
  "$candidates += @(" ^
  "  (Join-Path ${env:ProgramFiles} 'Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat')," ^
  "  (Join-Path ${env:ProgramFiles} 'Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat')," ^
  "  (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat')," ^
  "  (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat')," ^
  "  (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\2019\Community\VC\Auxiliary\Build\vcvars64.bat')," ^
  "  (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\2019\BuildTools\VC\Auxiliary\Build\vcvars64.bat')" ^
  ");" ^
  "$found = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1;" ^
  "if ($found) { Set-Content -LiteralPath '%VCVARS_FILE%' -Value $found -NoNewline; exit 0 } else { exit 1 }"

if errorlevel 1 (
    echo.
    echo ERROR: Could not find vcvars64.bat.
    echo Install "Desktop development with C++" using Visual Studio Installer.
    echo Required components:
    echo   - MSVC v143 or v142 x64/x86 build tools
    echo   - Windows 10 or Windows 11 SDK
    echo.
    pause
    exit /b 1
)

set /p VCVARS=<"%VCVARS_FILE%"
del /q "%VCVARS_FILE%" 2>nul

if not exist "%VCVARS%" (
    echo.
    echo ERROR: Found vcvars path does not exist:
    echo   %VCVARS%
    pause
    exit /b 1
)

echo Using:
echo   %VCVARS%

call "%VCVARS%" >nul
if errorlevel 1 (
    echo ERROR: Failed to initialize Visual Studio build environment.
    pause
    exit /b 1
)

where cl >nul 2>nul
if errorlevel 1 (
    echo ERROR: cl.exe is not available after vcvars64.
    pause
    exit /b 1
)

echo Checking Windows SDK UCRT headers...
echo #include ^<io.h^>> build_secure_check_io.c
cl /nologo /c build_secure_check_io.c /Fobuild_secure_check_io.obj >nul 2>nul
if errorlevel 1 (
    del /q build_secure_check_io.c build_secure_check_io.obj 2>nul
    echo.
    echo ERROR: Windows SDK UCRT header io.h is not available.
    echo This is the cause of: pyconfig.h fatal error C1083: 'io.h': No such file or directory
    echo.
    echo Fix:
    echo   Open Visual Studio Installer
    echo   Modify your Visual Studio/Build Tools installation
    echo   Install "Desktop development with C++"
    echo   Ensure "Windows 10 SDK" or "Windows 11 SDK" is selected
    echo.
    pause
    exit /b 1
)
del /q build_secure_check_io.c build_secure_check_io.obj 2>nul

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
