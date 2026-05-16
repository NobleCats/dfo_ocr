# DFOGANG Raid Helper v1.0beta Release

This project now ships as a single fully bundled Windows EXE:

```text
release_dist\DFOGANG_RaidHelper_v1.0beta_<commit>.exe
```

Users do not need Python, pip, a `.bat` launcher, a runtime installer, or ZIP extraction. They run the EXE directly.

## Build

Run from the repository root on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File tools\build_full_onefile_release.ps1
```

The build script:

1. Initializes the Visual Studio C++ build environment.
2. Cleans `build`, `dist`, and `build_secure`.
3. Verifies `src` syntax with `compileall`.
4. Stages `src/*.py` into `build_secure/protected_src`.
5. Generates `build_info.py` with version and commit metadata.
6. Compiles app modules to `.pyd` with Cython.
7. Removes staged `.py` and `.c` files.
8. Packages a PyInstaller onefile EXE with `tools/full_onefile.spec`.
9. Writes the final EXE to `release_dist`.

## Required Build Tools

- Windows 10/11 x64
- Python 3.10
- Visual Studio 2019/2022 or Build Tools with Desktop development with C++
- Windows 10 or Windows 11 SDK

The script upgrades or verifies PyInstaller, PyInstaller hooks, Cython, setuptools, and wheel.

## Release Files

Keep these release build files:

- `tools/build_full_onefile_release.ps1`
- `tools/full_onefile.spec`
- `tools/cython_setup_full_onefile.py`
- `tools/launcher_full_onefile.py`
- `tools/torch_stub_runtime_hook.py`

The old onedir ZIP build and external runtime installer experiments have been removed.

## Notes

- App modules are bundled as Cython-compiled `.pyd` files, not as plaintext `src/*.py`.
- The EXE bundles PyQt6, PaddleOCR, PaddlePaddle, OpenCV, markers, and resources.
- `torch`, ModelScope, Hugging Face, and other large optional stacks are excluded. `modelscope` is stubbed by `src/general_ocr.py`, and `torch` is stubbed by the PyInstaller runtime hook.
- First startup can be slow because PyInstaller onefile extracts to a temporary directory.
