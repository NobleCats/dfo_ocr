# -*- mode: python ; coding: utf-8 -*-
#
# Protected onedir release spec for DFOGANG Raid Helper.
#
# Build flow:
# 1. build_secure_release.bat copies src/*.py to build_secure/protected_src.
# 2. Cython compiles those modules into .pyd files.
# 3. Original .py files are deleted from the protected build folder.
# 4. This spec packages a tiny launcher + .pyd modules + resources + dependencies.
#
# Result:
# - Distribution does not contain src/*.py.
# - Internal modules are native .pyd extensions.
# - This is stronger than plain PyInstaller bytecode, but not unbreakable DRM.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

sys.setrecursionlimit(sys.getrecursionlimit() * 5)

ROOT = Path.cwd()
PROTECTED_SRC = ROOT / "build_secure" / "protected_src"

MODULES = [
    "app",
    "bake_library",
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
]

datas = [
    (str(ROOT / "resources"), "resources"),
]

# Runtime assets for Paddle/PaddleOCR.
# This may produce a large onedir folder, but it should behave closest to
# python src/gui_app.py and does not require end-user Python/pip/bat.
try:
    datas += collect_data_files("paddleocr", include_py_files=False)
except Exception:
    pass

try:
    datas += collect_data_files("paddle", include_py_files=False)
except Exception:
    pass

binaries = []

# Include compiled private modules.
for p in PROTECTED_SRC.glob("*.pyd"):
    binaries.append((str(p), "."))

try:
    binaries += collect_dynamic_libs("paddle")
except Exception:
    pass

try:
    binaries += collect_dynamic_libs("cv2")
except Exception:
    pass

hiddenimports = MODULES + [
    "PyQt6",
    "PyQt6.QtCore",
    "PyQt6.QtGui",
    "PyQt6.QtWidgets",
    "paddle",
    "paddleocr",
    "cv2",
    "mss",
    "win32gui",
    "win32con",
    "win32api",
    "win32ui",
    "pyclipper",
    "shapely",
    "numpy",
    "PIL",
    "requests",
    "rapidfuzz",
]

for pkg in ("pyclipper", "shapely"):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass

excludes = [
    # Large optional stacks not used by the release GUI.
    "torch",
    "torchvision",
    "torchaudio",
    "tensorflow",
    "tensorboard",
    "onnx",
    "onnxruntime",
    "openvino",
    "tensorrt",
    "paddle.tensorrt",
    "paddlex.inference.serving",

    # Dev/science stacks not used by the release GUI.
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

    # Test/doc packages.
    "pytest",
    "unittest",
    "pydoc",
]

a = Analysis(
    ["build_secure/launcher.py"],
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

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DFOGANG_RaidHelper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "resources" / "favicon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="DFOGANG_RaidHelper",
)
