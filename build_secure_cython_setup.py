# build_secure_cython_setup.py
#
# Compile src/*.py into native .pyd extension modules for release packaging.
# This is source-protection/obfuscation, not cryptographic encryption.

from __future__ import annotations

from pathlib import Path

from Cython.Build import cythonize
from setuptools import Extension, setup

ROOT = Path(__file__).resolve().parent
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

extensions = []
for name in MODULES:
    src = PROTECTED_SRC / f"{name}.py"
    if src.exists():
        extensions.append(Extension(name, [str(src)]))

setup(
    name="dfogang_raid_helper_protected",
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            # Keep wraparound enabled for safety; several modules intentionally
            # use negative Python indexes. The speed difference is irrelevant for
            # this release-protection build.
            "wraparound": True,
            "initializedcheck": False,
            "embedsignature": False,
        },
        annotate=False,
    ),
)
