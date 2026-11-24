"""Pictures a school makes itself: drawings small enough for the radio, library pictures changed a little, and photos.

A picture is a dict with short keys, as it travels:
    n  its name                     l  the name in other languages, {"por": "o nosso poço"}
    s  drawn lines (see below)      b, f  a library picture to start from, and its new colour
    j  a photo: a small JPEG in base64. Too big for the radio, so it goes by Wi-Fi or memory card only.

Drawn lines are words separated by spaces. Each starts with a colour (0-4) and a width (0-2), then two characters per
point: x and y on a 240-wide square, one of 87 steps each. A picture never changes once made (a changed one is a new
picture), so a phone that has it never needs it again. The Android app reads the same format (OwnPics.kt).
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Dict, List, Optional, Tuple

ALPHABET = "".join(c for c in map(chr, range(35, 127)) if c not in "&'<>\\")   # 87 characters, safe in HTML and JSON
STEPS = len(ALPHABET) - 1
SIZE = 240
COLOURS = ["#1d1d1b", "#8a4b1c", "#d0382c", "#1f7a3a", "#1f5fbf"]
COLOUR_NAMES = ["Black", "Brown", "Red", "Green", "Blue"]
WIDTHS = [4, 8, 14]
MAX_LINES = 6000          # characters of drawn lines
MAX_PHOTO = 24 * 1024     # bytes
KEY = re.compile(r"^my-[a-z0-9-]{1,27}$")
_STROKE = re.compile(r"^[0-4][0-2](?:[" + re.escape(ALPHABET) + r"]{2})+$")
_HEX = re.compile(r"^#[0-9a-f]{6}$")


def clean(raw, library: Dict[str, dict]) -> Optional[dict]:
    """A picture checked and trimmed, or None. Comes from the teacher's browser, a card or the radio."""
    if not isinstance(raw, dict):
        return None
    name = " ".join(str(raw.get("n") or "").split())[:30]
    if not name:
        return None
    out: dict = {"n": name}
    names = raw.get("l") if isinstance(raw.get("l"), dict) else {}
    names = {k: " ".join(v.split())[:30] for k, v in names.items() if isinstance(k, str) and re.match(r"^[a-z]{3}$", k)
             and isinstance(v, str) and v.strip()}
    if names:
        out["l"] = dict(list(names.items())[:6])
    if raw.get("j"):
        try:
            data = base64.b64decode(str(raw["j"]), validate=True)
        except (binascii.Error, ValueError):
            return None
        if not data.startswith(b"\xff\xd8\xff") or len(data) > MAX_PHOTO:
            return None
        out["j"] = str(raw["j"])
        return out
    if raw.get("b"):
        if raw["b"] not in library:
            return None
        out["b"] = raw["b"]
        if isinstance(raw.get("f"), str) and _HEX.match(raw["f"]):
            out["f"] = raw["f"]
    lines = " ".join(w for w in str(raw.get("s") or "").split(" ") if _STROKE.match(w))[:MAX_LINES]
    lines = " ".join(w for w in lines.split(" ") if _STROKE.match(w))   # the cut may have split one
    if lines:
        out["s"] = lines
    return out


def on_radio(pic: dict) -> dict:
    """The copy that goes by radio: a photo leaves only its name, which shows until the photo is copied over."""
    return {k: v for k, v in pic.items() if k != "j"}


def whole(pic: Optional[dict]) -> bool:
    return bool(pic) and any(k in pic for k in ("s", "b", "j"))


def strokes(lines: str) -> List[Tuple[int, int, List[Tuple[int, int]]]]:
    out = []
    for word in (lines or "").split(" "):
        if _STROKE.match(word):
            at = [round(ALPHABET.index(c) * SIZE / STEPS) for c in word[2:]]
            out.append((int(word[0]), int(word[1]), list(zip(at[::2], at[1::2]))))
    return out


def encode(lines: List[Tuple[int, int, List[Tuple[float, float]]]]) -> str:
    def step(v: float) -> str:
        return ALPHABET[max(0, min(STEPS, round(v * STEPS / SIZE)))]
    return " ".join(f"{c}{w}" + "".join(step(x) + step(y) for x, y in pts) for c, w, pts in lines if pts)


def name_of(pic: dict, lang: str = "") -> str:
    return (pic.get("l") or {}).get(lang) or pic["n"]


def svg(pic: dict, cls: str, library: Dict[str, dict], esc) -> str:
    inner = ""
    if pic.get("j"):
        inner = f"<image href='data:image/jpeg;base64,{pic['j']}' width='{SIZE}' height='{SIZE}' preserveAspectRatio='xMidYMid slice'/>"
    base = library.get(pic.get("b") or "")
    if base:
        inner += (f"<g transform='scale(10)' stroke='{base.get('s', '#1a1a1a')}' style='stroke-width:1.5'>"
                  + (f"<path d='{base['d']}' fill='{pic.get('f') or base['f']}'/>" if base.get("d") else "")
                  + (f"<path d='{base['x']}' fill='none'/>" if base.get("x") else "") + "</g>")
    for colour, width, pts in strokes(pic.get("s", "")):
        d = "M" + "L".join(f"{x} {y}" for x, y in pts) + ("l.1 0" if len(pts) == 1 else "")
        inner += f"<path d='{d}' fill='none' stroke='{COLOURS[colour]}' style='stroke-width:{WIDTHS[width]}'/>"
    if not inner:   # still on its way: its name in a frame
        label = pic["n"] if len(pic["n"]) <= 12 else pic["n"][:11] + "…"
        inner = (f"<rect x='6' y='6' width='228' height='228' rx='26' fill='none' stroke='#9a9a94' style='stroke-width:5;stroke-dasharray:14 12'/>"
                 f"<text x='120' y='132' text-anchor='middle' font-size='34' fill='#4a4a46' stroke='none'>{esc(label)}</text>")
    return f"<svg class='pic own{' ' + cls if cls else ''}' viewBox='0 0 {SIZE} {SIZE}' aria-hidden='true'>{inner}</svg>"
