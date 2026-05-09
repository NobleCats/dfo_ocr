# Full onefile release

This build creates one large EXE:

```text
release_dist\DFOGANG_RaidHelper_v1.0beta_<commit>.exe
```

End users only run that file. No runtime installer, Python, pip, ZIP extraction, or folder structure is required.

Build:

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_full_onefile_release.ps1
```

Notes:

- App modules are staged, compiled to `.pyd` with Cython, and the staged `.py/.c` files are removed before PyInstaller packaging.
- The EXE bundles PyQt6, PaddleOCR, PaddlePaddle, OpenCV, and resources.
- `torch` is excluded and replaced by a small runtime stub because this app uses Paddle inference; ModelScope may import torch only for optional utility checks.
- The EXE will be large and first startup can be slow because PyInstaller onefile extracts to a temp directory.
