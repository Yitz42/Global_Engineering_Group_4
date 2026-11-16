"""The words students see on the phone, in the school's language.

Every word the phone shows has a key, and meshlearn/lang/eng.json holds the English for each.
Other languages are files beside it with the same keys (lang/<ISO 639-3 code>.json). A file
may be partly done: any word it lacks shows in English, so a school can start using its
language before every button is translated. Teachers can also translate or correct words on
the hub; the hub merges those into the pack it sends to phones with their lessons.

Plurals: a key ending ".one" is used when the number is 1, ".other" for every other number.
Languages that change their words for 2, 5 or 11 write ".other" so it reads right for any
number (for example "Assignments to do: {n}").

The Kotlin phone (Language.kt) reads the same files and must give the same words.
"""

from __future__ import annotations

import datetime
import html
import json
import os
import re
import time
from typing import Dict, List, Optional

LANG_DIR = os.path.join(os.path.dirname(__file__), "lang")
SOURCE = "eng"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")

# Languages written right to left (ISO 639-3), for languages that have no file yet.
RTL = {"ara", "heb", "fas", "urd", "pus", "snd", "uig", "yid", "div", "ckb", "syr", "prs"}

# ISO 639-1 tags for the HTML lang attribute, so browsers pick the right fonts and voices.
# A language with no two-letter code keeps its three-letter one, which is also a valid tag.
TAGS = {
    "eng": "en", "por": "pt", "deu": "de", "spa": "es", "fra": "fr", "swa": "sw", "ara": "ar", "hin": "hi",
    "zho": "zh", "rus": "ru", "ita": "it", "tur": "tr", "jpn": "ja", "fas": "fa", "ukr": "uk", "ron": "ro",
    "pol": "pl", "nld": "nl", "ben": "bn", "ind": "id", "kor": "ko", "vie": "vi", "heb": "he", "hun": "hu",
    "ell": "el", "srp": "sr", "tha": "th", "bul": "bg", "hrv": "hr", "ces": "cs", "mya": "my", "swe": "sv",
    "fin": "fi", "slk": "sk", "glg": "gl", "cat": "ca", "slv": "sl", "lit": "lt", "dan": "da", "eus": "eu",
    "est": "et", "bel": "be", "cym": "cy", "tam": "ta", "epo": "eo", "lat": "la", "tel": "te", "hye": "hy",
    "nob": "nb", "nno": "nn", "aze": "az", "mar": "mr", "urd": "ur", "mkd": "mk", "kat": "ka", "mal": "ml",
    "isl": "is", "uzb": "uz", "san": "sa", "kaz": "kk", "amh": "am", "hau": "ha", "yor": "yo", "ibo": "ig",
    "zul": "zu", "xho": "xh", "som": "so", "kin": "rw", "lug": "lg", "sna": "sn", "orm": "om", "tir": "ti",
    "afr": "af", "tgl": "tl", "msa": "ms", "khm": "km", "lao": "lo", "nep": "ne", "sin": "si", "pan": "pa",
    "guj": "gu", "kan": "kn", "mlg": "mg",
}

LEVEL_KEYS = {"Not started": "level.not_started", "Just started": "level.just_started", "Needs work": "level.needs_work",
              "Developing": "level.developing", "Good": "level.good", "Strong": "level.strong"}

# Answer-checking messages from assignments.validate_answers, as keys.
_ERRORS = [
    (re.compile(r"^Please answer this question\.$"), "error.blank", ()),
    (re.compile(r"^Choose one of the options\.$"), "error.choose", ()),
    (re.compile(r"^Enter a number, like 12, 3\.5, 3,5 or 1/2\.$"), "error.number", ()),
    (re.compile(r"^Keep this under (\d+) characters\.$"), "error.too_long", ("n",)),
    (re.compile(r"^Keep this under (\d+) characters \((\d+) now\)\.$"), "error.too_long_now", ("n", "now")),
]

_cache: Dict[str, dict] = {}


def bundled() -> Dict[str, dict]:
    """Every language file that comes with MeshLearn, by code."""
    if not _cache:
        for fname in sorted(os.listdir(LANG_DIR)):
            if fname.endswith(".json"):
                with open(os.path.join(LANG_DIR, fname), encoding="utf-8") as fh:
                    data = json.load(fh)
                _cache[data["code"]] = data
    return _cache


def source() -> dict:
    return bundled()[SOURCE]


def keys() -> List[str]:
    return list(source()["strings"])


def placeholders(text: str) -> set:
    return set(_PLACEHOLDER.findall(text or ""))


def check(key: str, text: str) -> str:
    """'' if a translation is usable, else what is wrong with it in plain words."""
    english = source()["strings"].get(key)
    if english is None:
        return "not a word MeshLearn uses"
    missing = placeholders(english) - placeholders(text)
    if missing:
        return "keep " + " and ".join("{" + m + "}" for m in sorted(missing))
    extra = placeholders(text) - placeholders(english)
    if extra:
        return "remove " + " and ".join("{" + m + "}" for m in sorted(extra)) + ": the phone has nothing to put there"
    return ""


def fill(text: str, values: dict) -> str:
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


def build_pack(code: str, overrides: Optional[Dict[str, str]] = None, name: str = "", direction: str = "") -> dict:
    """The words for one language as phones receive them: the file that comes with MeshLearn
    (if any), with the teacher's own words on top. Only usable words are kept."""
    base = bundled().get(code, {})
    strings: Dict[str, str] = {}
    for key, text in list((base.get("strings") or {}).items()) + list((overrides or {}).items()):
        if isinstance(text, str) and text.strip() and not check(key, text):
            strings[key] = text.strip()
    lists = {}
    for key, want in (("days", 7), ("months", 12)):
        value = (base.get("lists") or {}).get(key)
        if isinstance(value, list) and len(value) == want and all(isinstance(v, str) for v in value):
            lists[key] = value
    return {
        "code": code,
        "tag": base.get("tag") or TAGS.get(code, code),
        "name": name or base.get("name") or code,
        "dir": direction if direction in ("ltr", "rtl") else base.get("dir") or ("rtl" if code in RTL else "ltr"),
        "reviewed": bool(base.get("reviewed")) and not overrides,
        "strings": strings,
        "lists": lists,
    }


def coverage(pack: dict) -> int:
    """How much of MeshLearn is translated, as a whole percent (English counts as 100)."""
    if pack.get("code") == SOURCE:
        return 100
    total = len(keys())
    done = sum(1 for k in keys() if k in (pack.get("strings") or {}))
    return int(100 * done / total) if total else 0


class Strings:
    """The words for one language, falling back to English word by word."""

    def __init__(self, pack: Optional[dict] = None):
        base = source()
        pack = pack or base
        self.code = str(pack.get("code") or SOURCE)
        self.tag = str(pack.get("tag") or TAGS.get(self.code, self.code))
        self.name = str(pack.get("name") or self.code)
        self.dir = "rtl" if pack.get("dir") == "rtl" else "ltr"
        self._strings = dict(base["strings"])
        for key, text in (pack.get("strings") or {}).items():
            if key in self._strings and isinstance(text, str) and text.strip() and not check(key, text):
                self._strings[key] = text
        self._lists = dict(base["lists"])
        for key, want in (("days", 7), ("months", 12)):
            value = (pack.get("lists") or {}).get(key)
            if isinstance(value, list) and len(value) == want and all(isinstance(v, str) and v for v in value):
                self._lists[key] = value

    def __call__(self, key: str, **values) -> str:
        return fill(self._strings.get(key, key), values)

    def n(self, key: str, count: int, **values) -> str:
        """The .one or .other form, with {n} filled in."""
        return self(f"{key}.one" if count == 1 else f"{key}.other", n=count, **values)

    def html(self, key: str, **parts) -> str:
        """Escaped words with ready-made HTML (like a link) put in at the placeholders."""
        return fill(html.escape(self._strings.get(key, key)), parts)

    def date(self, day: datetime.date) -> str:
        return self("date.short", weekday=self._lists["days"][day.weekday()], month=self._lists["months"][day.month - 1],
                    day=day.day)

    def when(self, ts: Optional[float]) -> str:
        if not ts:
            return ""
        t = time.localtime(ts)
        return self("date.when", month=self._lists["months"][t.tm_mon - 1], day=f"{t.tm_mday:02d}",
                    time=f"{t.tm_hour:02d}:{t.tm_min:02d}")

    def level(self, name: str) -> str:
        key = LEVEL_KEYS.get(name)
        return self(key) if key else name

    def error(self, message: str) -> str:
        for pattern, key, names in _ERRORS:
            m = pattern.match(message)
            if m:
                return self(key, **dict(zip(names, m.groups())))
        return message


ENGLISH = Strings()
