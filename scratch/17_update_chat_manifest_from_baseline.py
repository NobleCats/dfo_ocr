"""Update chat template metadata after manual PNG edits.

The PNG glyphs in templates/chat/default are hand-maintained. This script
keeps manifest.json in sync with the actual image dimensions and applies
baseline offsets measured from samples/chat_ascii_baseline.png.
"""
from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CHAT_DIR = ROOT / "templates" / "chat" / "default"
MANIFEST = CHAT_DIR / "manifest.json"


def default_bottom_to_baseline(ch: str) -> int:
    if ch in "gjpqy":
        return -4
    if ch == "Q":
        return -4
    if ch in "abcdefghijklmnopqrstuvwxyz":
        return 0
    if ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        return 0
    if ch in "0123456789":
        return 0
    measured = {
        "~": 8,
        "`": 18,
        "!": 4,
        "@": 2,
        "#": 4,
        "$": 0,
        "^": 12,
        "&": 4,
        "*": 4,
        "(": 0,
        ")": 0,
        "-": 10,
        # In raid-party rows the detected text baseline sits above the
        # underscore stroke; keep this negative so scaled templates align
        # with names such as The_End.
        "_": -4,
        "+": 4,
        "=": 8,
        "|": 0,
        "{": 0,
        "}": 0,
        "[": 0,
        "]": 0,
        ":": 4,
        ";": 0,
        ",": 0,
        ".": 4,
        "/": 0,
    }
    return measured[ch]


def main() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    updates: list[str] = []

    for slug, entries in manifest.items():
        for entry in entries:
            image_path = CHAT_DIR / entry["file"]
            with Image.open(image_path) as image:
                width, height = image.size
            old = (
                entry.get("width"),
                entry.get("height"),
                entry.get("bottom_to_baseline"),
            )
            entry["width"] = width
            entry["height"] = height
            entry["bottom_to_baseline"] = default_bottom_to_baseline(entry["char"])
            new = (entry["width"], entry["height"], entry["bottom_to_baseline"])
            if old != new:
                updates.append(f"{slug}: {old} -> {new}")

    MANIFEST.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"updated {len(updates)} manifest entries")
    for line in updates:
        print("  " + line)


if __name__ == "__main__":
    main()
