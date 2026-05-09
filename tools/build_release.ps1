<#
.SYNOPSIS
  Build the protected onedir release of DFOGANG Raid Helper.

.DESCRIPTION
  Replaces the legacy build_secure_release.bat. Pure PowerShell so we no
  longer have to fight cmd.exe over `${env:ProgramFiles(x86)}` parsing,
  which was the cause of the "\Microsoft was unexpected at this time."
  failure.

  Steps:
    1. Locate Visual Studio's vcvars64.bat via vswhere (required for
       Cython's MSVC compile step).
    2. Clean previous build/dist/build_secure/release_dist outputs.
    3. compileall src/ as a smoke test that the source still parses.
    4. Mirror src/*.py to build_secure/protected_src/.
    5. Cythonize each module into .pyd inside that workspace.
    6. Strip .py and .c files so only .pyd remain.
    7. Run PyInstaller with tools/release.spec.
    8. Verify the dist contains no plain src/*.py files.
    9. Zip dist/DFOGANG_RaidHelper into release_dist/.

.NOTES
  Run from the repo root:
      powershell -ExecutionPolicy Bypass -File tools\build_release.ps1
#>

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

# Always operate from the repo root regardless of caller CWD.
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $RepoRoot

$AppName = 'DFOGANG_RaidHelper'
$AppVersion = 'v1.0beta'
$ZipName = "${AppName}_${AppVersion}.zip"

$DistApp = Join-Path $RepoRoot "dist\$AppName"
$ReleaseDir = Join-Path $RepoRoot 'release_dist'
$ZipPath = Join-Path $ReleaseDir $ZipName
$ProtectedSrc = Join-Path $RepoRoot 'build_secure\protected_src'
$CythonTemp = Join-Path $RepoRoot 'build_secure\cython_temp'

function Write-Step {
    param([string]$Index, [string]$Message)
    Write-Host ''
    Write-Host "[$Index] $Message" -ForegroundColor Cyan
}

function Fail {
    param([string]$Message)
    Write-Host ''
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

function Find-VcVars {
    $vswhereCandidates = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft Visual Studio\Installer\vswhere.exe')
    )

    $candidates = @()

    foreach ($vswhere in $vswhereCandidates) {
        if (Test-Path $vswhere) {
            $install = & $vswhere -latest -products '*' `
                -requires 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64' `
                -property installationPath
            if ($install) {
                $candidates += (Join-Path $install 'VC\Auxiliary\Build\vcvars64.bat')
            }
        }
    }

    $candidates += @(
        (Join-Path $env:ProgramFiles 'Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat'),
        (Join-Path $env:ProgramFiles 'Microsoft Visual Studio\2022\Professional\VC\Auxiliary\Build\vcvars64.bat'),
        (Join-Path $env:ProgramFiles 'Microsoft Visual Studio\2022\Enterprise\VC\Auxiliary\Build\vcvars64.bat'),
        (Join-Path $env:ProgramFiles 'Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat'),
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat'),
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat'),
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\2019\Community\VC\Auxiliary\Build\vcvars64.bat'),
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\2019\BuildTools\VC\Auxiliary\Build\vcvars64.bat')
    )

    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) {
            return $p
        }
    }
    return $null
}

function Invoke-CmdWithVcVars {
    param(
        [Parameter(Mandatory)] [string]$VcVars,
        [Parameter(Mandatory)] [string]$Command,
        [string]$WorkingDirectory
    )
    $cwd = if ($WorkingDirectory) { $WorkingDirectory } else { (Get-Location).Path }
    # Use cmd /s /c so `call vcvars64.bat` and the follow-up command share env.
    $full = "pushd `"$cwd`" && call `"$VcVars`" >NUL && $Command && popd"
    & cmd.exe /s /c $full
    if ($LASTEXITCODE -ne 0) {
        Fail "Command failed (exit $LASTEXITCODE): $Command"
    }
}


Write-Host '============================================================'
Write-Host " DFOGANG Raid Helper $AppVersion - Protected Onedir Build"
Write-Host '============================================================'

# Force the runtime profile we ship with v1.0beta.
$env:DFO_OCR_PROFILE = ''
$env:DFO_OCR_RECOGNITION_MODEL = ''

Write-Step '0/8' 'Locating Visual Studio C++ build environment...'
$VcVars = Find-VcVars
if (-not $VcVars) {
    Fail @"
Could not find vcvars64.bat.
Install "Desktop development with C++" via the Visual Studio Installer.
Required components:
  - MSVC v143 or v142 x64/x86 build tools
  - Windows 10 or Windows 11 SDK
"@
}
Write-Host "  Using: $VcVars"

# Verify the Windows SDK UCRT headers are present. A VS install that has the
# MSVC tools but no Windows SDK will fail on `#include <io.h>` inside
# pyconfig.h, producing a confusing cl.exe exit-code-2 failure later.
$sdkIncludeDirs = Get-ChildItem "C:\Program Files (x86)\Windows Kits\10\include" `
    -Directory -ErrorAction SilentlyContinue |
    Where-Object { Test-Path (Join-Path $_.FullName "ucrt\io.h") }
if (-not $sdkIncludeDirs) {
    Fail @"
Windows SDK UCRT headers not found.
The Windows 10/11 SDK headers are required to compile Cython extensions.

Fix:
  1. Open the Visual Studio Installer.
  2. Click 'Modify' on your Visual Studio / Build Tools installation.
  3. Under 'Desktop development with C++', make sure at least one
     'Windows 10 SDK' or 'Windows 11 SDK' item is checked.
  4. Click Modify and wait for the install to complete.
  5. Re-run this script.
"@
}
Write-Host "  Windows SDK UCRT headers: OK"


Write-Step '1/8' 'Cleaning previous build outputs...'
foreach ($d in @('build', 'dist', 'build_secure', 'release_dist')) {
    $p = Join-Path $RepoRoot $d
    if (Test-Path $p) {
        Remove-Item -LiteralPath $p -Recurse -Force
    }
}
New-Item -ItemType Directory -Path (Join-Path $RepoRoot 'build_secure') | Out-Null
New-Item -ItemType Directory -Path $ProtectedSrc | Out-Null
New-Item -ItemType Directory -Path $ReleaseDir | Out-Null


Write-Step '2/8' 'Verifying Python is available...'
$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) {
    Fail 'python is not on PATH.'
}
& python --version
if ($LASTEXITCODE -ne 0) {
    Fail 'python --version failed.'
}


Write-Step '3/8' 'Installing/upgrading build tools...'
& python -m pip install --disable-pip-version-check --upgrade pyinstaller cython setuptools wheel
if ($LASTEXITCODE -ne 0) {
    Fail 'pip install for build tools failed.'
}


Write-Step '4/8' 'compileall src/ as a parse smoke test...'
& python -m compileall -q src
if ($LASTEXITCODE -ne 0) {
    Fail 'src/ failed to byte-compile.'
}


Write-Step '5/8' 'Mirroring src/*.py to protected workspace...'
Copy-Item -Path (Join-Path $RepoRoot 'src\*.py') -Destination $ProtectedSrc
$pyCount = (Get-ChildItem -LiteralPath $ProtectedSrc -Filter *.py | Measure-Object).Count
if ($pyCount -lt 1) {
    Fail 'No .py files were copied to build_secure/protected_src.'
}
Write-Host "  Copied $pyCount .py file(s)."


Write-Step '6/8' 'Cythonizing modules into .pyd...'
$cythonSetup = Join-Path $RepoRoot 'tools\cython_setup.py'
Invoke-CmdWithVcVars -VcVars $VcVars `
    -WorkingDirectory $ProtectedSrc `
    -Command "python `"$cythonSetup`" build_ext --inplace --build-temp `"$CythonTemp`""

$pydCount = (Get-ChildItem -LiteralPath $ProtectedSrc -Filter *.pyd | Measure-Object).Count
if ($pydCount -lt 1) {
    Fail 'Cython did not produce any .pyd files.'
}
Write-Host "  Produced $pydCount .pyd file(s)."

# Now strip plain .py and .c so only the compiled .pyd remain. After this
# point the protected workspace must contain no original sources.
Get-ChildItem -LiteralPath $ProtectedSrc -Filter *.py | Remove-Item -Force
Get-ChildItem -LiteralPath $ProtectedSrc -Filter *.c  | Remove-Item -Force
Get-ChildItem -LiteralPath $ProtectedSrc -Filter __pycache__ -Directory -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force


Write-Step '7/8' 'Running PyInstaller (onedir)...'
$spec = Join-Path $RepoRoot 'tools\release.spec'
& python -m PyInstaller --noconfirm --clean --distpath (Join-Path $RepoRoot 'dist') `
    --workpath (Join-Path $RepoRoot 'build') $spec
if ($LASTEXITCODE -ne 0) {
    Fail 'PyInstaller build failed.'
}
if (-not (Test-Path (Join-Path $DistApp "$AppName.exe"))) {
    Fail "Expected EXE not produced: $DistApp\$AppName.exe"
}


Write-Step '8/8' 'Verifying dist and packaging release ZIP...'
# Project-internal modules that must NEVER appear as plain .py in the dist.
# Third-party packages (PyQt6, paddle, etc.) are allowed to ship their own
# .py files — that is normal PyInstaller behavior.
$ProtectedNames = @(
    'app', 'bake_library', 'capture', 'debug_capture', 'detect',
    'dfogang', 'extract', 'general_ocr', 'gui_app', 'match',
    'neople', 'overlay', 'party_apply', 'qt_dpi', 'recognize',
    'resources', 'segment', 'templates'
)
$leaked = Get-ChildItem -LiteralPath $DistApp -Recurse -File -Filter *.py |
    Where-Object { $ProtectedNames -contains [System.IO.Path]::GetFileNameWithoutExtension($_.Name) }
if ($leaked) {
    $leaked | ForEach-Object { Write-Host "  EXPOSED: $($_.FullName)" -ForegroundColor Red }
    Fail "$(@($leaked).Count) project .py file(s) leaked into dist. Aborting."
}
Write-Host '  dist contains no project .py source files.'

# Also confirm our compiled .pyd modules made it into the bundle.
$pydInDist = Get-ChildItem -LiteralPath $DistApp -Recurse -File -Filter *.pyd |
    Where-Object { $ProtectedNames -contains [System.IO.Path]::GetFileNameWithoutExtension($_.Name) }
Write-Host "  Bundled $(@($pydInDist).Count) protected .pyd module(s)."

if (Test-Path $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}
Compress-Archive -Path $DistApp -DestinationPath $ZipPath -CompressionLevel Optimal
if (-not (Test-Path $ZipPath)) {
    Fail "ZIP not produced at $ZipPath"
}

$distSize = '{0:N1} MB' -f ((Get-ChildItem -LiteralPath $DistApp -Recurse -File |
    Measure-Object Length -Sum).Sum / 1MB)
$zipSize  = '{0:N1} MB' -f ((Get-Item -LiteralPath $ZipPath).Length / 1MB)

Write-Host ''
Write-Host '============================================================' -ForegroundColor Green
Write-Host ' Build complete.' -ForegroundColor Green
Write-Host '============================================================' -ForegroundColor Green
Write-Host "  Onedir:   $DistApp ($distSize)"
Write-Host "  Release:  $ZipPath ($zipSize)"
Write-Host ''
Write-Host 'User flow:'
Write-Host '  1. Extract the ZIP'
Write-Host "  2. Run $AppName.exe"
