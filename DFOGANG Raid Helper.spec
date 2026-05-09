# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


root = Path.cwd()
portable = Path(r"C:\Users\Noble\Desktop\works\DFOGANG")

datas = [
    ("templates", "templates"),
    ("markers", "markers"),
]

for filename in ("DNFForgedBlade-Bold.ttf", "logo.png", "favicon.ico"):
    path = portable / filename
    if path.exists():
        datas.append((str(path), "."))

icon_path = portable / "favicon.ico"
icon = [str(icon_path)] if icon_path.exists() else None


# Both PaddleOCR and EasyOCR cache weights in the user's HOME on first run
# rather than bundling inside site-packages, so we don't need data files for
# the model weights — but we DO need a fat hidden-imports list because
# PaddleOCR/PaddlePaddle and EasyOCR/Torch resolve many submodules
# dynamically. PaddleOCR is the primary engine; EasyOCR stays as fallback.
try:
    from PyInstaller.utils.hooks import collect_submodules
    paddleocr_hidden = collect_submodules("paddleocr")
    paddle_hidden = collect_submodules("paddle")
    easyocr_hidden = collect_submodules("easyocr")
    torch_hidden = collect_submodules("torch")
    torchvision_hidden = collect_submodules("torchvision")
except Exception:
    paddleocr_hidden = ["paddleocr"]
    paddle_hidden = ["paddle"]
    easyocr_hidden = ["easyocr"]
    torch_hidden = ["torch"]
    torchvision_hidden = ["torchvision"]

a = Analysis(
    ["src/gui_app.py"],
    pathex=[str(root / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "cv2",
        "mss",
        "numpy",
        "PIL.Image",
        "requests",
        "win32con",
        "win32gui",
    ] + paddleocr_hidden + paddle_hidden
      + easyocr_hidden + torch_hidden + torchvision_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DFOGANG Raid Helper",
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
    icon=icon,
)
