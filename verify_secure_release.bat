@echo off
setlocal EnableExtensions

REM Quick distribution security sanity check.

cd /d "%~dp0"

set APPDIR=dist\DFOGANG_RaidHelper

if not exist "%APPDIR%" (
    echo ERROR: %APPDIR% not found.
    pause
    exit /b 1
)

echo.
echo Checking exposed Python source files...
powershell -NoProfile -Command "Get-ChildItem -Recurse '%APPDIR%' -Filter *.py | Select-Object FullName"

echo.
echo Checking compiled private modules...
powershell -NoProfile -Command "Get-ChildItem -Recurse '%APPDIR%' -Filter *.pyd | Where-Object { $_.Name -match '^(app|party_apply|neople|dfogang|general_ocr|gui_app)' } | Select-Object Name,Length"

echo.
echo Distribution size:
powershell -NoProfile -Command "'{0:N1} MB' -f ((Get-ChildItem -Recurse '%APPDIR%' -File | Measure-Object Length -Sum).Sum / 1MB)"

echo.
pause
endlocal
