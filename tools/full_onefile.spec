# -*- mode: python ; coding: utf-8 -*-
#
# Fully bundled onefile release spec.
#
# This produces one large EXE. It does not require Python, pip, a runtime
# installer, or extracted folders on the user side.
#
# App modules are compiled to .pyd first and bundled here. The original src/*.py
# files are not included.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
    copy_metadata,
)
from PyInstaller.building import splash_templates

sys.setrecursionlimit(sys.getrecursionlimit() * 8)

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent
PROTECTED_SRC = ROOT / "build_secure" / "protected_src"

project_modules = [
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
    "resources",
]

datas = []
for folder in ("resources", "markers"):
    p = ROOT / folder
    if p.exists():
        datas.append((str(p), folder))

for model_name in ("PP-OCRv5_server_det", "en_PP-OCRv5_mobile_rec"):
    model_dir = Path.home() / ".paddlex" / "official_models" / model_name
    if model_dir.exists():
        datas.append((str(model_dir), f"paddlex_models/{model_name}"))
    else:
        print(f"WARNING: OCR model cache not found, EXE will not bundle {model_name}: {model_dir}")

binaries = []
for p in PROTECTED_SRC.glob("*.pyd"):
    binaries.append((str(p), "."))

hiddenimports = list(project_modules)

# PyQt6: let PyInstaller's official hooks do most of the work, but keep
# submodules discoverable for plugin-heavy Qt runtime.
try:
    hiddenimports += collect_submodules("PyQt6")
except Exception:
    pass

# PaddleOCR/Paddle/PaddleX/OpenCV full runtime. This is intentionally large but
# prioritizes reliability over size.
for pkg in ("paddleocr", "paddlex", "paddle", "cv2"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        try:
            datas += collect_data_files(pkg, include_py_files=False)
        except Exception:
            pass
        try:
            binaries += collect_dynamic_libs(pkg)
        except Exception:
            pass
        try:
            hiddenimports += collect_submodules(pkg)
        except Exception:
            pass

# Supporting packages that are commonly imported dynamically by PaddleOCR/PaddleX.
for pkg in (
    "Cython",
    "numpy",
    "PIL",
    "imagesize",
    "mss",
    "requests",
    "rapidfuzz",
    "pyclipper",
    "pypdfium2",
    "shapely",
    "yaml",
    "pydantic",
    "pandas",
    "colorlog",
    "prettytable",
    "ujson",
    "ruamel",
    "ruamel.yaml",
    "filelock",
    "tqdm",
    "httpx",
    "bidi",
    "huggingface_hub",
    "aistudio_sdk",
):
    try:
        hiddenimports += collect_submodules(pkg)
    except Exception:
        pass
    try:
        datas += collect_data_files(pkg, include_py_files=False)
    except Exception:
        pass
    try:
        binaries += collect_dynamic_libs(pkg)
    except Exception:
        pass

# PaddleX gates OCR pipeline construction through importlib.metadata checks for
# the paddlex[ocr-core] extra. collect_all("paddlex") bundles the code, but the
# frozen runtime also needs these distribution metadata folders for
# is_extra_available("ocr-core") to pass.
for dist_name in (
    "paddlex",
    "paddleocr",
    "paddlepaddle",
    "opencv-contrib-python",
    "imagesize",
    "pyclipper",
    "pypdfium2",
    "python-bidi",
    "shapely",
):
    try:
        datas += copy_metadata(dist_name, recursive=True)
    except Exception:
        pass

# PaddleOCR imports Cython utilities dynamically while constructing parts of
# the OCR pipeline. In onefile builds those non-Python Utility/*.cpp resources
# are not guaranteed to be present unless Cython data files are collected.
try:
    datas += collect_data_files("Cython", include_py_files=False)
except Exception:
    pass

# pywin32 modules used by capture/window handling.
hiddenimports += [
    "win32gui",
    "win32con",
    "win32api",
    "win32ui",
    "pythoncom",
    "pywintypes",
    "pkgutil",
    "importlib",
    "importlib.util",
    "importlib.machinery",
]

# Torch is intentionally excluded and replaced with a stub runtime hook.
# TensorFlow and other huge optional stacks are also unused.
excludes = [
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
    "IPython",
    "jedi",
    "notebook",
    "jupyter",
    "matplotlib",
    "seaborn",
    "sklearn",
    "scipy",
    "easyocr",
    "nltk",
    "datasets",
    "transformers",
    "tokenizers",
    "modelscope",
    "wandb",
    "xformers",
    "pytest",
]

a = Analysis(
    [str(ROOT / "tools" / "launcher_full_onefile.py")],
    pathex=[str(PROTECTED_SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "tools" / "torch_stub_runtime_hook.py")],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)

splash_templates.splash_canvas_setup += r"""
wm title . "DFOGANG Raid Helper"
set spinner_cx [expr {$image_width / 2}]
set spinner_cy [expr {$image_height / 2}]
set spinner_r 30
set spinner_items {}
font create spinnerFont -family {Segoe UI} -size 17 -weight bold
for {set i 0} {$i < 12} {incr i} {
    set angle [expr {($i * 30.0 - 90.0) * acos(-1) / 180.0}]
    set x [expr {$spinner_cx + cos($angle) * $spinner_r}]
    set y [expr {$spinner_cy + sin($angle) * $spinner_r}]
    lappend spinner_items [.root.canvas create oval \
        [expr {$x - 4}] [expr {$y - 4}] \
        [expr {$x + 4}] [expr {$y + 4}] \
        -outline "" -fill "#2f3b4a"]
}
.root.canvas create text \
    [expr {$spinner_cx + 1}] [expr {$spinner_cy + 54}] \
    -text "Loading..." \
    -fill "#050505" \
    -font spinnerFont \
    -anchor center
.root.canvas create text \
    $spinner_cx [expr {$spinner_cy + 53}] \
    -text "Loading..." \
    -fill "#ffffff" \
    -font spinnerFont \
    -anchor center
set spinner_frame 0
proc animate_spinner {} {
    global spinner_items spinner_frame
    set colors {"#58a6ff" "#4b91df" "#407bc0" "#365f91" "#2f3b4a" "#2f3b4a" "#2f3b4a" "#2f3b4a" "#2f3b4a" "#2f3b4a" "#2f3b4a" "#2f3b4a"}
    set count [llength $spinner_items]
    for {set i 0} {$i < $count} {incr i} {
        set color_index [expr {($i - $spinner_frame) % $count}]
        .root.canvas itemconfigure [lindex $spinner_items $i] -fill [lindex $colors $color_index]
    }
    set spinner_frame [expr {($spinner_frame + 1) % $count}]
    after 80 animate_spinner
}
after 80 animate_spinner
"""

splash = Splash(
    str(ROOT / "resources" / "splash.png"),
    binaries=a.binaries,
    datas=a.datas,
    text_pos=None,
    minify_script=True,
    always_on_top=True,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    splash,
    splash.binaries,
    a.binaries,
    a.datas,
    [],
    name="DFOGANG_RaidHelper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "resources" / "ch49gangraidlogo.ico") if (ROOT / "resources" / "ch49gangraidlogo.ico").exists() else None,
)
