# Build protected .pyd modules for the full onefile release.

from __future__ import annotations

from pathlib import Path

from Cython.Build import cythonize
from setuptools import Extension, setup

ROOT = Path(__file__).resolve().parent.parent
PROTECTED_SRC = ROOT / "build_secure" / "protected_src"

MODULES = [
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

extensions = []
for name in MODULES:
    src = PROTECTED_SRC / f"{name}.py"
    if src.exists():
        extensions.append(Extension(name, [str(src)]))

setup(
    name="dfogang_raid_helper_full_onefile",
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            "wraparound": True,
            "initializedcheck": False,
            "embedsignature": False,
        },
        annotate=False,
    ),
)
