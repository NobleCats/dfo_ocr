@echo off
setlocal EnableExtensions

REM Package protected onedir distribution into a user-facing ZIP.

cd /d "%~dp0"

set APPDIR=dist\DFOGANG_RaidHelper
set OUTZIP=release_dist\DFOGANG_RaidHelper_v1.0beta_protected_onedir.zip

if not exist "%APPDIR%\DFOGANG_RaidHelper.exe" (
    echo ERROR: Protected onedir build not found.
    echo Run build_secure_release.bat first.
    pause
    exit /b 1
)

if not exist release_dist mkdir release_dist

echo Checking for exposed .py source files...
for /f %%F in ('powershell -NoProfile -Command "(Get-ChildItem -Recurse '%APPDIR%' -Filter *.py | Measure-Object).Count"') do set PYCOUNT=%%F

if not "%PYCOUNT%"=="0" (
    echo ERROR: Found %PYCOUNT% .py files in distribution. Aborting.
    powershell -NoProfile -Command "Get-ChildItem -Recurse '%APPDIR%' -Filter *.py | Select-Object FullName"
    pause
    exit /b 1
)

echo Creating ZIP...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Compress-Archive -Force -Path '%APPDIR%' -DestinationPath '%OUTZIP%'"

echo.
echo Package complete:
echo   %OUTZIP%
echo.
echo User flow:
echo   1. Extract ZIP
echo   2. Run DFOGANG_RaidHelper.exe
echo.

pause
endlocal
