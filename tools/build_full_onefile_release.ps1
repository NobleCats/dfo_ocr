param(
    [string]$Python = "python",
    [string]$Version = "v1.0beta"
)

$ErrorActionPreference = "Stop"

function Import-MsvcEnvironment {
    Write-Host "[0/9] Initializing Visual Studio C++ environment..."

    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    $candidates = @()

    if (Test-Path $vswhere) {
        $install = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
        if ($install) {
            $candidates += (Join-Path $install "VC\Auxiliary\Build\vcvars64.bat")
        }
    }

    $candidates += @(
        (Join-Path ${env:ProgramFiles} "Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"),
        (Join-Path ${env:ProgramFiles} "Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"),
        (Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"),
        (Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat"),
        (Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\2019\Community\VC\Auxiliary\Build\vcvars64.bat"),
        (Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\2019\BuildTools\VC\Auxiliary\Build\vcvars64.bat")
    )

    $vcvars = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (!$vcvars) {
        throw "vcvars64.bat not found. Install Visual Studio Build Tools with Desktop development with C++ and Windows SDK."
    }

    Write-Host "Using $vcvars"
    $envDump = & cmd /s /c "`"$vcvars`" >nul && set"
    foreach ($line in $envDump) {
        if ($line -match "^(.*?)=(.*)$") {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
        }
    }

    & cl /? 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "MSVC cl.exe is not available. Install Visual Studio Build Tools with Desktop development with C++ and Windows SDK."
    }

    "#include <io.h>" | Set-Content -Encoding ASCII "build_secure_check_io.c"
    & cl /nologo /c build_secure_check_io.c /Fobuild_secure_check_io.obj 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Windows SDK headers are not available. Install Visual Studio Build Tools with Windows 10/11 SDK."
    }
    Remove-Item build_secure_check_io.c, build_secure_check_io.obj -Force -ErrorAction SilentlyContinue
}

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

Write-Host "============================================================"
Write-Host " DFOGANG Raid Helper - Full Onefile Build"
Write-Host "============================================================"
Write-Host ""

Import-MsvcEnvironment

$Commit = "dev"
try {
    $Commit = (& git rev-parse --short HEAD).Trim()
} catch {}

$AppName = "DFOGANG_RaidHelper_${Version}_${Commit}.exe"
$BuildSecure = Join-Path $Root "build_secure"
$ProtectedSrc = Join-Path $BuildSecure "protected_src"
$ReleaseDist = Join-Path $Root "release_dist"
$TclTkBuildRoot = Join-Path ([System.IO.Path]::GetTempPath()) "python310_tcltk"
$ProjectModules = @(
    "app",
    "build_info",
    "capture",
    "debug_capture",
    "detect",
    "dfogang",
    "general_ocr",
    "gui_app",
    "neople",
    "overlay",
    "party_apply",
    "qt_dpi",
    "resources"
)

Write-Host "[1/9] Cleaning build outputs..."
Remove-Item $BuildSecure -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "build" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "dist" -Recurse -Force -ErrorAction SilentlyContinue
foreach ($Module in $ProjectModules) {
    Remove-Item "${Module}.cp*.pyd" -Force -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Force $ProtectedSrc | Out-Null
New-Item -ItemType Directory -Force $ReleaseDist | Out-Null

Write-Host "[2/9] Checking Python..."
& $Python --version

Write-Host "[2/9] Preparing Tcl/Tk splash runtime..."
$PythonPrefix = (& $Python -c "import sys; print(sys.prefix)").Trim()
$SourceTcl = Join-Path $PythonPrefix "tcl\tcl8.6"
$SourceTk = Join-Path $PythonPrefix "tcl\tk8.6"
if (!(Test-Path $SourceTcl) -or !(Test-Path $SourceTk)) {
    throw "Tcl/Tk runtime folders not found under Python prefix: $PythonPrefix"
}
New-Item -ItemType Directory -Force $TclTkBuildRoot | Out-Null
Copy-Item -LiteralPath $SourceTcl -Destination $TclTkBuildRoot -Recurse -Force
Copy-Item -LiteralPath $SourceTk -Destination $TclTkBuildRoot -Recurse -Force
$env:TCL_LIBRARY = Join-Path $TclTkBuildRoot "tcl8.6"
$env:TK_LIBRARY = Join-Path $TclTkBuildRoot "tk8.6"
& $Python -c "import tkinter; t=tkinter.Tcl(); print('Tcl/Tk', t.eval('info patchlevel'), t.eval('info library'))"

Write-Host "[3/9] Installing/upgrading build tools..."
& $Python -m pip install --upgrade pyinstaller pyinstaller-hooks-contrib cython setuptools wheel
& $Python -m pip install --upgrade PyQt6 mss requests pillow numpy opencv-contrib-python paddleocr paddlepaddle pywin32 colorlog

Write-Host "[3/9] Preparing bundled OCR models..."
& $Python -c "from paddleocr import PaddleOCR; PaddleOCR(lang='en', use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False, enable_mkldnn=False)"
$PaddlexModelRoot = Join-Path $env:USERPROFILE ".paddlex\official_models"
$RequiredOcrModels = @("PP-OCRv5_server_det", "en_PP-OCRv5_mobile_rec")
foreach ($ModelName in $RequiredOcrModels) {
    $ModelDir = Join-Path $PaddlexModelRoot $ModelName
    if (!(Test-Path $ModelDir)) {
        throw "Required OCR model cache not found: $ModelDir"
    }
    foreach ($RequiredFile in @("inference.json", "inference.pdiparams")) {
        $RequiredPath = Join-Path $ModelDir $RequiredFile
        if (!(Test-Path $RequiredPath)) {
            throw "Required OCR model file not found: $RequiredPath"
        }
    }
}

Write-Host "[4/9] Verifying source syntax..."
& $Python -m compileall src

Write-Host "[5/9] Staging protected source..."
Copy-Item "src\*.py" $ProtectedSrc -Force

@"
APP_VERSION = "$Version"
GIT_COMMIT = "$Commit"
BUILD_LABEL = "$Version ($Commit)"
"@ | Set-Content -Encoding UTF8 (Join-Path $ProtectedSrc "build_info.py")

Write-Host "[6/9] Cythonizing protected modules..."
& $Python "tools\cython_setup_full_onefile.py" build_ext --inplace
foreach ($Module in $ProjectModules) {
    $BuiltPyd = Get-ChildItem -Path $Root -Filter "${Module}.cp*.pyd" -File | Select-Object -First 1
    if (!$BuiltPyd) {
        throw "Cython output not found for module: $Module"
    }
    Move-Item -LiteralPath $BuiltPyd.FullName -Destination $ProtectedSrc -Force
}

Write-Host "[7/9] Removing staged plaintext source..."
Remove-Item (Join-Path $ProtectedSrc "*.py") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $ProtectedSrc "*.c") -Force -ErrorAction SilentlyContinue
Get-ChildItem $BuildSecure -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $BuildSecure -Recurse -Filter "*.pyc" | Remove-Item -Force -ErrorAction SilentlyContinue

Write-Host "[8/9] Building fully bundled onefile EXE..."
& $Python -m PyInstaller --noconfirm --clean "tools\full_onefile.spec"

$Exe = Join-Path $Root "dist\DFOGANG_RaidHelper.exe"
if (!(Test-Path $Exe)) {
    throw "EXE not found: $Exe"
}

$OutExe = Join-Path $ReleaseDist $AppName
Copy-Item $Exe $OutExe -Force

Write-Host "[9/9] Verifying output..."
$sizeMb = [math]::Round((Get-Item $OutExe).Length / 1MB, 1)
Write-Host "Full onefile EXE built:"
Write-Host "  $OutExe"
Write-Host "  $sizeMb MB"
Write-Host ""
Write-Host "Test this exact file:"
Write-Host "  $OutExe"
