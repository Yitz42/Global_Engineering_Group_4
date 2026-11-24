"""Lessons from Kolibri's public channels, for the phone's card.

Kolibri (https://learningequality.org/kolibri) publishes whole courses as "channels", such as Khan
Academy in Swahili, split into topics. Each channel comes with a small database that lists its topics
and the files, sizes and licences of every lesson. The hub downloads that database once, lets the
teacher pick topics, and copies just those lessons' files.

Only lessons that any browser can open (videos, sound, PDF and e-books) and that have an open licence
are offered. Exercises, apps and lessons with other licences need Kolibri itself, so they are left out,
and so is material marked for teachers only.
"""

from __future__ import annotations

import bisect
import gzip
import json
import os
import pathlib
import re
import sqlite3
from typing import Dict, List, Optional, Tuple

from . import library

STUDIO = "https://studio.learningequality.org"
CHANNELS_URL = STUDIO + "/api/public/v1/channels"
FOLDER = "kolibri"            # in the card's library folder; files are named by their checksum
MAX_FILES = 3000              # more than this in one go: the teacher picks smaller topics
ID = re.compile(r"^[0-9a-f]{32}$")

# Best file first when a lesson comes in several versions: the smaller video, PDF before e-book.
PRESETS = {"low_res_video": 0, "high_res_video": 1, "audio": 0, "document": 0, "epub": 1}
EXTENSIONS = ("mp4", "webm", "mp3", "pdf", "epub")
KINDS = {"video": ("video", "videos"), "audio": ("recording", "recordings"), "document": ("document", "documents")}
PLAYABLE = tuple(KINDS)

# Kolibri's language codes -> the hub's (ISO 639-3).
ISO3 = {
    "af": "afr", "am": "amh", "ar": "ara", "az": "aze", "bg": "bul", "bn": "ben", "cs": "ces", "da": "dan", "de": "deu",
    "el": "ell", "en": "eng", "es": "spa", "fa": "fas", "fr": "fra", "gu": "guj", "ha": "hau", "he": "heb", "hi": "hin",
    "hr": "hrv", "hu": "hun", "hy": "hye", "id": "ind", "ig": "ibo", "is": "isl", "it": "ita", "ja": "jpn", "ka": "kat",
    "kk": "kaz", "km": "khm", "kn": "kan", "ko": "kor", "la": "lat", "lg": "lug", "lo": "lao", "lt": "lit", "mg": "mlg",
    "mk": "mkd", "ml": "mal", "mr": "mar", "ms": "msa", "my": "mya", "ne": "nep", "nl": "nld", "nb": "nob", "nn": "nno",
    "om": "orm", "pa": "pan", "pl": "pol", "pt": "por", "ro": "ron", "ru": "rus", "rw": "kin", "sa": "san", "si": "sin",
    "sk": "slk", "sl": "slv", "sn": "sna", "so": "som", "sr": "srp", "sv": "swe", "sw": "swa", "ta": "tam", "te": "tel",
    "th": "tha", "ti": "tir", "tr": "tur", "uk": "ukr", "ur": "urd", "uz": "uzb", "vi": "vie", "xh": "xho", "yo": "yor",
    "zh": "zho", "zu": "zul", "fil": "tgl", "tl": "tgl",
}
_NODE = "SELECT id, title, kind, lft, rght, level, parent_id FROM content_contentnode "


class KolibriError(RuntimeError):
    pass


def language(code: str) -> str:
    base = code.split("-")[0].lower()
    return ISO3.get(base, base)


def count_text(counts: Dict[str, int]) -> str:
    """{"video": 1, "document": 12} -> "1 video, 12 documents" """
    return ", ".join(f"{counts[k]:,} {KINDS[k][0 if counts[k] == 1 else 1]}" for k in KINDS if counts.get(k))


def parse_channels(data) -> List[dict]:
    """Catalog entries for the channels that have anything a plain browser can open."""
    entries = []
    for ch in data:
        counts = {k["kind_id"]: int(k["count"]) for k in ch.get("kind_count") or [] if k.get("kind_id") != "topic"}
        if not ID.match(str(ch.get("id", ""))) or not any(counts.get(k) for k in PLAYABLE):
            continue
        if ch.get("language") and ch["language"] != "mul":   # a Swahili course with one English video is a Swahili course
            langs = [language(ch["language"])]
        else:
            codes = [c for c in (ch.get("included_languages") or []) if c not in ("mul", "und")]
            langs = list(dict.fromkeys(language(c) for c in codes)) or ["mul"]
        entries.append({
            "name": f"kolibri:{ch['id']}", "title": ch["name"].strip(), "flavour": "",
            "summary": f"{count_text(counts)}. {' '.join((ch.get('description') or '').split())}", "lang": ",".join(langs),
            "category": "kolibri", "tags": ["kolibri", re.sub(r"[^a-z0-9]+", "-", ch["name"].lower()).strip("-")],
            "articles": sum(counts.values()), "updated": (ch.get("last_published") or "")[:10],
            "size": int(ch.get("published_size") or 0), "url": "", "file": "", "channel": ch["id"],
        })
    return entries


def fetch_channels() -> List[dict]:
    return parse_channels(json.loads(library._get(CHANNELS_URL, timeout=120).decode("utf-8")))


def file_url(file_id: str, ext: str) -> str:
    return f"{STUDIO}/content/storage/{file_id[0]}/{file_id[1]}/{file_id}.{ext}"


def open_licence(name: Optional[str]) -> bool:
    return bool(name) and (name.startswith("CC ") or name == "Public Domain")


class Channel:
    """One channel's downloaded list of topics and lessons."""

    def __init__(self, folder: str, channel_id: str):
        if not ID.match(channel_id):
            raise KolibriError("unknown Kolibri channel")
        self.id = channel_id
        self.path = os.path.join(folder, channel_id + ".sqlite3")

    @property
    def ready(self) -> bool:
        return os.path.exists(self.path)

    def fetch(self) -> None:
        """Download the list (most are under 10 MB, the largest about 120). Needs internet."""
        data = library._get(f"{STUDIO}/content/databases/{self.id}.sqlite3", timeout=300)
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        try:
            self._query("SELECT license_name, coach_content, lft FROM content_contentnode LIMIT 1", path=tmp)
        except sqlite3.Error:
            os.remove(tmp)
            raise KolibriError("this channel's list is in a format that MeshLearn can't read") from None
        os.replace(tmp, self.path)

    def _query(self, sql: str, args: tuple = (), path: Optional[str] = None) -> List[sqlite3.Row]:
        con = sqlite3.connect(pathlib.Path(os.path.abspath(path or self.path)).as_uri() + "?mode=ro", uri=True)
        try:
            con.row_factory = sqlite3.Row
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def node(self, node_id: str = "") -> Optional[dict]:
        """A topic or lesson; the whole channel when no id is given."""
        rows = self._query(_NODE + "WHERE id = ?", (node_id,)) if node_id else self._query(_NODE + "WHERE level = 0 LIMIT 1")
        return dict(rows[0]) if rows else None

    def trail(self, node: dict) -> List[dict]:
        """The topics above a node, the channel itself first."""
        return [dict(r) for r in self._query(_NODE + "WHERE lft < ? AND rght > ? ORDER BY lft", (node["lft"], node["rght"]))]

    def lessons(self, node: dict) -> List[dict]:
        """Every lesson under a node that can go on the card, in course order, one file each.
        A file that is listed under several topics appears once for each of them."""
        best: Dict[str, Tuple[int, dict]] = {}
        kinds, exts = ",".join("?" * len(PLAYABLE)), ",".join("?" * len(EXTENSIONS))
        for r in self._query(
                "SELECT n.id, n.title, n.lft, n.parent_id, n.kind, n.license_name, n.license_owner, n.author, f.preset, "
                "l.id AS file, l.extension, l.file_size FROM content_contentnode n "
                "JOIN content_file f ON f.contentnode_id = n.id JOIN content_localfile l ON l.id = f.local_file_id "
                f"WHERE n.lft BETWEEN ? AND ? AND n.kind IN ({kinds}) AND n.coach_content = 0 AND f.supplementary = 0 "
                f"AND f.thumbnail = 0 AND l.extension IN ({exts}) ORDER BY n.lft",
                (node["lft"], node["rght"]) + PLAYABLE + EXTENSIONS):
            if (not open_licence(r["license_name"]) or r["preset"] not in PRESETS or not r["file_size"]
                    or not ID.match(r["file"])):
                continue
            rank = PRESETS[r["preset"]]
            if r["id"] not in best or rank < best[r["id"]][0]:
                best[r["id"]] = (rank, dict(r))
        return [lesson for _, lesson in best.values()]

    def left_out(self, node: dict, lessons: List[dict]) -> int:
        """How many things under a node can't go on the card."""
        rows = self._query("SELECT COUNT(*) AS n FROM content_contentnode WHERE kind <> 'topic' AND lft BETWEEN ? AND ?",
                           (node["lft"], node["rght"]))
        return rows[0]["n"] - len(lessons)

    def children(self, node: dict, lessons: List[dict]) -> List[dict]:
        """The topics and lessons directly under a topic that have something for the card, each with
        "lessons" (how many different files) and "size"."""
        starts = [x["lft"] for x in lessons]
        out = []
        for r in self._query(_NODE + "WHERE parent_id = ? ORDER BY lft", (node["id"],)):
            within = lessons[bisect.bisect_left(starts, r["lft"]): bisect.bisect_right(starts, r["rght"])]
            if within:
                files = {x["file"]: x["file_size"] for x in within}
                out.append(dict(r, lessons=len(files), size=sum(files.values()), extension=within[0]["extension"]))
        return out

    def selection_item(self, node: dict, entry: dict) -> dict:
        """What goes in the hub's card selection for a topic (or one lesson). entry: the channel's catalog entry."""
        topics = {r["id"]: r for r in self._query(_NODE + "WHERE kind = 'topic'")}
        files, details = [], []
        seen = set()
        for x in self.lessons(node):
            if x["file"] in seen:
                continue
            seen.add(x["file"])
            parent = topics.get(x["parent_id"])
            above = topics.get(parent["parent_id"]) if parent else None
            names = [t["title"].strip() for t in (above, parent) if t is not None and t["level"] > 0]
            owner = (x["license_owner"] or x["author"] or "").strip()
            files.append((file_url(x["file"], x["extension"]), f"{x['file']}.{x['extension']}"))
            details.append({"title": x["title"].strip(), "size": x["file_size"], "kind": x["extension"],
                            "shelf": entry["title"], "collection": ": ".join(names) or entry["title"],
                            "license": x["license_name"], "credit": " · ".join(t for t in (owner, x["license_name"]) if t)})
        if not files:
            raise KolibriError("nothing in this topic can go on the card")
        if len(files) > MAX_FILES:
            raise KolibriError(f"that is {len(files):,} files: open it and pick smaller topics")
        title = entry["title"] if node["level"] == 0 else f"{entry['title']}: {node['title'].strip()}"
        return {"key": f"kolibri:{self.id}:{node['id']}|", "title": title, "label": entry["label"], "level": entry["level"],
                "lang": entry["lang"], "ages": entry["ages"], "size": sum(d["size"] for d in details), "folder": FOLDER,
                "kind": "kolibri", "license": "Open licences, shown with each lesson", "files": files, "details": details,
                "kolibri": {"channel": self.id, "lft": node["lft"], "rght": node["rght"]}}


def part_of(a: dict, b: dict) -> bool:
    """True if selection item a lies inside selection item b (and is not the same one)."""
    ka, kb = a.get("kolibri"), b.get("kolibri")
    return bool(ka and kb and a["key"] != b["key"] and ka["channel"] == kb["channel"]
                and kb["lft"] <= ka["lft"] and ka["rght"] <= kb["rght"])
