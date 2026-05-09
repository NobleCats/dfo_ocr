"""Runtime resource path helpers for source and PyInstaller one-file builds."""
from __future__ import annotations

import sys
from pathlib import Path


def app_root() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    return app_root().joinpath(*parts)
