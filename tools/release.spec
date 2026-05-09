# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the protected onedir release.

Build flow (driven by tools/build_release.ps1):
  1. Stage src/*.py into build_secure/protected_src/.
  2. Cythonize them into .pyd modules in the same directory.
  3. Delete the staged .py and generated .c files so only .pyd remain.
  4. Run PyInstaller with this spec to bundle launcher + .pyd + resources.

Result:
  - dist/DFOGANG_RaidHelper/DFOGANG_RaidHelper.exe + _internal/
  - No project source .py files are shipped; private modules are .pyd.
  - This is obfuscation, not unbreakable DRM.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

sys.setrecursionlimit(sys.getrecursionlimit() * 5)

ROOT = Path.cwd()
PROTECTED_SRC = ROOT / "build_secure" / "protected_src"
LAUNCHER = ROOT / "tools" / "launcher.py"

# External assets the GUI loads from the bundle root via resource_path("...").
# The dev workstation keeps the originals in PORTABLE_DIR; the repo's
# resources/ folder provides fallbacks where it can.
PORTABLE_DIR = Path(r"C:\Users\Noble\Desktop\works\DFOGANG")
ROOT_ASSETS = ("DNFForgedBlade-Bold.ttf", "logo.png", "favicon.ico")


def _resolve_asset(name: str) -> Path | None:
    candidates = [
        PORTABLE_DIR / name,
        ROOT / "resources" / name,
        ROOT / name,
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


datas = []

# Runtime data: marker images + template libraries.
for sub in ("markers", "templates", "resources"):
    if (ROOT / sub).exists():
        datas.append((str(ROOT / sub), sub))

# Loose top-level assets (font, logo, favicon).
for name in ROOT_ASSETS:
    found = _resolve_asset(name)
    if found is not None:
        datas.append((str(found), "."))

# PaddleOCR / PaddlePaddle ship runtime assets that PyInstaller cannot infer.
for pkg in ("paddleocr", "paddle"):
    try:
        datas += collect_data_files(pkg, include_py_files=False)
    except Exception:
        pass


# Cython-compiled private modules.
binaries = []
if PROTECTED_SRC.exists():
    for pyd in PROTECTED_SRC.glob("*.pyd"):
        binaries.append((str(pyd), "."))

for pkg in ("paddle", "cv2"):
    try:
        binaries += collect_dynamic_libs(pkg)
    except Exception:
        pass


hiddenimports = [
    # Compiled private modules — declared as hidden imports so PyInstaller
    # registers them even though no .py is visible during analysis.
    "app",
    "bake_library",
    "build_info",
    "capture",
    "debug_capture",
    "detect",
    "dfogang",
    "extract",
    "general_ocr",
    "gui_app",
    "match",
    "neople",
    "overlay",
    "party_apply",
    "qt_dpi",
    "recognize",
    "resources",
    "segment",
    "templates",
    # Third-party runtime imports.
    "PyQt6",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "paddle",
    "paddleocr",
    "cv2",
    "mss",
    "win32api",
    "win32con",
    "win32gui",
    "win32ui",
    "pyclipper",
    "shapely",
    "numpy",
    "PIL",
    "PIL.Image",
    "requests",
    "rapidfuzz",
]

for pkg in ("pyclipper", "shapely"):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass


# Heavy optional stacks not used by the release GUI. Excluding these keeps
# the dist out of the multi-gigabyte territory.
excludes = [
    "torch",
    "torchvision",
    "torchaudio",
    "easyocr",
    "tensorflow",
    "tensorboard",
    "onnx",
    "onnxruntime",
    "openvino",
    "tensorrt",
    "paddle.tensorrt",
    "paddlex.inference.serving",
    "IPython",
    "jedi",
    "notebook",
    "jupyter",
    "matplotlib",
    "pandas",
    "scipy",
    "sklearn",
    "seaborn",
    "sympy",
    "numba",
    "pytest",
]


a = Analysis(
    [str(LAUNCHER)],
    pathex=[str(PROTECTED_SRC), str(ROOT), str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

icon_path = _resolve_asset("favicon.ico")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DFOGANG_RaidHelper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path) if icon_path else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DFOGANG_RaidHelper",
)
