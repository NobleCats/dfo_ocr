"""Template library: extract, save, load character templates.

Templates are stored as binary PNGs (uint8, 0/255). Filename encodes the
character via a safe scheme since some chars are not valid filenames on
Windows (`<>:"/\\|?*`).

Layout:
    templates/{ui}/{scale_tag}/{idx}_{slug}.png
    templates/{ui}/{scale_tag}/manifest.json

`manifest.json` maps slug -> {char, source: row index, count}.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict
from pathlib import Path
import json
import numpy as np
from PIL import Image


# Map every printable char to a filesystem-safe slug.
# Letters and digits map to themselves, other chars map to a name.
_CHAR_TO_SLUG = {
    "~": "tilde", "`": "backtick", "!": "excl", "@": "at", "#": "hash",
    "$": "dollar", "^": "caret", "&": "amp", "*": "star",
    "(": "lparen", ")": "rparen", "-": "dash", "_": "uscore",
    "+": "plus", "=": "eq", "|": "pipe",
    "{": "lbrace", "}": "rbrace", "[": "lbrack", "]": "rbrack",
    ":": "colon", ";": "semi", ",": "comma", ".": "dot", "/": "slash",
}
_SLUG_TO_CHAR = {v: k for k, v in _CHAR_TO_SLUG.items()}


def char_to_slug(c: str) -> str:
    if c in _CHAR_TO_SLUG:
        return _CHAR_TO_SLUG[c]
    if c.isupper():
        return f"upper_{c.lower()}"
    if c.islower():
        return f"lower_{c}"
    if c.isdigit():
        return f"digit_{c}"
    raise ValueError(f"unsupported character: {c!r}")


def slug_to_char(slug: str) -> str:
    if slug in _SLUG_TO_CHAR:
        return _SLUG_TO_CHAR[slug]
    if slug.startswith("upper_"):
        return slug[len("upper_"):].upper()
    if slug.startswith("lower_"):
        return slug[len("lower_"):]
    if slug.startswith("digit_"):
        return slug[len("digit_"):]
    raise ValueError(f"unknown slug: {slug!r}")


@dataclass
class Template:
    char: str
    image: np.ndarray  # (H, W) uint8 binary mask 0/255, tight-cropped
    bottom_to_baseline: int  # rows from glyph bottom to baseline (>= 0 normal,
                             # < 0 means glyph descends below baseline)

    @property
    def width(self) -> int:
        return self.image.shape[1]

    @property
    def height(self) -> int:
        return self.image.shape[0]


def save_templates(library_dir: Path,
                   templates: dict[str, list[Template]]) -> None:
    """Save templates to disk. Each character can have multiple variants."""
    library_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, list[dict]] = {}
    for char, variants in templates.items():
        slug = char_to_slug(char)
        entries: list[dict] = []
        for i, tpl in enumerate(variants):
            fname = f"{slug}__{i}.png" if len(variants) > 1 else f"{slug}.png"
            Image.fromarray(tpl.image).save(library_dir / fname)
            entries.append({
                "char": char,
                "file": fname,
                "width": tpl.width,
                "height": tpl.height,
                "bottom_to_baseline": tpl.bottom_to_baseline,
            })
        manifest[slug] = entries
    (library_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_templates(library_dir: Path) -> dict[str, list[Template]]:
    """Load all templates (and their variants) from a per-(ui, scale) directory."""
    manifest_path = library_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    out: dict[str, list[Template]] = {}
    for slug, entries in manifest.items():
        # Backward compat: single-entry manifest (dict instead of list)
        if isinstance(entries, dict):
            entries = [entries | {"file": f"{slug}.png"}]
        for info in entries:
            path = library_dir / info["file"]
            img = np.array(Image.open(path).convert("L"))
            out.setdefault(info["char"], []).append(
                Template(char=info["char"], image=img,
                         bottom_to_baseline=info.get("bottom_to_baseline", 0))
            )
    return out
