"""Compile the staged src/*.py copies into native .pyd modules.

Invoked by tools/build_release.ps1 after the source tree has been mirrored to
build_secure/protected_src/. The MSVC toolchain must already be initialized in
the current shell (the PowerShell driver does this via vcvars64.bat).

Run with --inplace and CWD = build_secure/protected_src so the generated .pyd
files land next to their source .py copies.
"""

from __future__ import annotations

import sys
from pathlib import Path

from Cython.Build import cythonize
from setuptools import Extension, setup

# Modules to compile. Anything in src/ that is imported at runtime should be
# listed here so plain .py copies are not shipped.
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


def _build_extensions() -> list[Extension]:
    cwd = Path.cwd()
    extensions: list[Extension] = []
    missing: list[str] = []
    for name in MODULES:
        src = cwd / f"{name}.py"
        if src.exists():
            extensions.append(Extension(name, [str(src)]))
        else:
            missing.append(name)
    if missing:
        print(
            "WARNING: missing modules in "
            f"{cwd} (skipped): {', '.join(missing)}",
            file=sys.stderr,
        )
    if not extensions:
        print("ERROR: no modules to compile.", file=sys.stderr)
        sys.exit(1)
    return extensions


setup(
    name="dfogang_raid_helper_protected",
    ext_modules=cythonize(
        _build_extensions(),
        compiler_directives={
            "language_level": "3",
            "boundscheck": False,
            # wraparound=True intentionally — several modules use negative
            # Python indexes, and the compile-time speedup from disabling it
            # is irrelevant for this protection build.
            "wraparound": True,
            "initializedcheck": False,
            "embedsignature": False,
        },
        annotate=False,
    ),
)
