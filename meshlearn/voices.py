"""Recordings of the teacher's voice: which words they belong to, and the files that hold them.

A recording's key comes from the language and the words said, so a phone can find it for any text it shows, and a word
recorded once works everywhere. ("2" is read differently in each language, so the words alone are not enough.) Files are
named <key>.<ext> in a "voice" folder. The Android app uses the same keys (Voices.kt).
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Dict, List, Tuple

from .web import Response, file_response

KEY = re.compile(r"^[0-9a-f]{16}$")
MAX_BYTES = 400 * 1024
MAX_SECONDS = 60
# What browsers record, and what a teacher might pick. (ext: MIME type)
TYPES = {"webm": "audio/webm", "ogg": "audio/ogg", "m4a": "audio/mp4", "mp3": "audio/mpeg"}
_BY_TYPE = {"audio/webm": "webm", "video/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/aac": "m4a",
            "audio/mpeg": "mp3", "audio/mp3": "mp3"}


def key(text: str, lang: str) -> str:
    return hashlib.sha256((lang + "\n" + " ".join((text or "").split())).encode("utf-8")).hexdigest()[:16]


def find(files: Dict[str, str], text: str, langs: List[str]) -> str:
    """The key of the recording to play for this text: the student's language first, then the others they may know.
    A bare number is only read in the student's own language."""
    for n, lang in enumerate(langs):
        k = key(text, lang)
        if k in files and (n == 0 or any(c.isalpha() for c in text)):
            return k
    return ""


def texts(assignment: dict) -> List[Tuple[str, str]]:
    """What can be said for an assignment, in order: (words, what they are)."""
    out: List[Tuple[str, str]] = []
    for n, q in enumerate(assignment["questions"], start=1):
        out.append((q["prompt"], f"Question {n} · read to the student"))
        opic = q.get("opic") or []
        for j, o in enumerate(q.get("options", [])):
            out.append((o, "Picture word" if j < len(opic) and opic[j] else f"Answer to question {n}"))
    return out


def ext_of(content_type: str) -> str:
    return _BY_TYPE.get((content_type or "").split(";")[0].strip().lower(), "")


def looks_like(data: bytes, ext: str) -> bool:
    """Whether the bytes start like the sound they claim to be."""
    if ext == "webm":
        return data[:4] == b"\x1a\x45\xdf\xa3"
    if ext == "ogg":
        return data[:4] == b"OggS"
    if ext == "m4a":
        return data[4:8] == b"ftyp"
    return data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)


def folder_files(folder: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in sorted(names):
        stem, _, ext = name.partition(".")
        if KEY.match(stem) and ext in TYPES:
            out[stem] = os.path.join(folder, name)
    return out


def remove(folder: str, key_: str) -> None:
    path = folder_files(folder).get(key_)
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def save(folder: str, key_: str, ext: str, data: bytes) -> None:
    os.makedirs(folder, exist_ok=True)
    remove(folder, key_)
    path = os.path.join(folder, f"{key_}.{ext}")
    with open(path + ".tmp", "wb") as fh:
        fh.write(data)
    os.replace(path + ".tmp", path)


def response(files: Dict[str, str], key_: str) -> Response:
    path = files.get(key_) if KEY.match(key_) else None
    if not path or not os.path.isfile(path):
        return Response("not found", status=404, content_type="text/plain")
    resp = file_response(path)
    resp.headers["Content-Type"] = TYPES[os.path.splitext(path)[1][1:]]
    return resp
