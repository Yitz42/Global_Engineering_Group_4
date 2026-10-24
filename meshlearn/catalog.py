"""Browse every open collection that can go on a phone's card, education first.

The Kiwix catalog lists thousands of offline collections (Wikipedia in every
language, textbooks, courses, science simulations, talks, Q&A sites, game
wikis...). This module downloads that list once, keeps it on the hub so it
works offline afterwards, and rates each collection for school use with
transparent rules, so the teacher sees learning material first.

It also runs the download of a teacher's chosen collections onto a memory card
in the background.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

from . import library

CATALOG_ALL = library.KIWIX_CATALOG + "?count=-1"

# ISO 639-3 codes used by Kiwix -> names shown to teachers (most common first).
LANGUAGES = {
    "eng": "English", "por": "Portuguese", "deu": "German", "spa": "Spanish", "fra": "French",
    "swa": "Swahili", "ara": "Arabic", "hin": "Hindi", "zho": "Chinese", "rus": "Russian",
    "ita": "Italian", "tur": "Turkish", "jpn": "Japanese", "fas": "Persian", "ukr": "Ukrainian",
    "ron": "Romanian", "pol": "Polish", "nld": "Dutch", "ben": "Bengali", "ind": "Indonesian",
    "kor": "Korean", "vie": "Vietnamese", "heb": "Hebrew", "hun": "Hungarian", "ell": "Greek", "srp": "Serbian",
    "tha": "Thai", "bul": "Bulgarian", "hrv": "Croatian", "ces": "Czech", "mya": "Burmese", "swe": "Swedish",
    "fin": "Finnish", "slk": "Slovak", "glg": "Galician", "cat": "Catalan", "slv": "Slovenian", "lit": "Lithuanian",
    "dan": "Danish", "eus": "Basque", "est": "Estonian", "bel": "Belarusian", "cym": "Welsh", "tam": "Tamil",
    "epo": "Esperanto", "lat": "Latin", "tel": "Telugu", "hye": "Armenian", "nob": "Norwegian (Bokmål)",
    "nno": "Norwegian (Nynorsk)", "aze": "Azerbaijani", "mar": "Marathi", "urd": "Urdu", "mkd": "Macedonian",
    "kat": "Georgian", "mal": "Malayalam", "isl": "Icelandic", "uzb": "Uzbek", "san": "Sanskrit", "kaz": "Kazakh",
    "amh": "Amharic", "hau": "Hausa", "yor": "Yoruba", "ibo": "Igbo", "zul": "Zulu", "xho": "Xhosa",
    "som": "Somali", "kin": "Kinyarwanda", "lug": "Luganda", "sna": "Shona", "orm": "Oromo", "tir": "Tigrinya",
    "afr": "Afrikaans", "tgl": "Tagalog", "msa": "Malay", "khm": "Khmer", "lao": "Lao", "nep": "Nepali",
    "sin": "Sinhala", "pan": "Punjabi", "guj": "Gujarati", "kan": "Kannada", "mlg": "Malagasy", "mul": "Many languages",
}
SHORT_TO_ISO = {"en": "eng", "pt": "por", "de": "deu"}


def language_name(code: str) -> str:
    return LANGUAGES.get(code, code)

# Education rating: 3 = core learning, 2 = education and reference, 1 = general, 0 = not for school.
LEVELS = {3: "Core learning", 2: "Learning reference", 1: "General interest", 0: "Not school-related"}

# (match, label, level). Matches are tried in order against "category:<c>", "name:<name>" and tags.
RULES: List[Tuple[str, str, int]] = [
    (r"^name:(pokepedia|pokewiki|bulbagarden|zh\.minecraft|minecraft|zdoom|granbluefantasy|dandwiki|frackinuniverse|bitcoin)", "Games and hobbies", 0),
    (r"^tag:(gaming|fandom)$", "Games and hobbies", 0),
    (r"^category:phet$", "Science simulations", 3),
    (r"^category:wikibooks$|^name:(libretexts|milneopentextbooks|openstax|openmusictheory|learningstatistic|jeffe\.cs|ethanweed)", "Textbooks", 3),
    (r"^category:(wikiversity|mooc)$|^name:(khanacademy|crashcourse|artofproblemsolving|oer4schools|ubongo|kolibri|avanti|diksha|madrasa|moldova-ministry-education|cest-pas-sorcier|biologycourses|voa_learning|genomicseducation|science-in-the-bath|ncase)|^tag:(openedx|kolibri|khan-academy)$", "Courses and lessons", 3),
    (r"^category:vikidia$|^name:(klexikon|wikimini)", "Encyclopedia for children", 3),
    (r"^category:wikipedia$", "Encyclopedia", 2),
    (r"^category:wiktionary$", "Dictionary", 2),
    (r"^category:(wikisource|gutenberg)$", "Books and source texts", 2),
    (r"^category:ted$", "Talks (video)", 2),
    (r"^name:(freecodecamp|scratch-wiki)", "Programming courses", 2),
    (r"^name:(proofwiki|planetmath|stacks\.math)", "Math reference", 2),
    (r"^name:(medlineplus|mdwiki|wikem)|^tag:(medical|medicine)$", "Health", 2),
    (r"^name:(apod\.nasa|energypedia|edutechwiki|womenshistory|folgerpedia|wikispecies)", "Learning reference", 2),
    (r"^category:wikivoyage$", "Travel guides", 1),
    (r"^category:wikiquote$", "Quotations", 1),
    (r"^category:stack_exchange$", "Questions and answers", 1),
    (r"^category:ifixit$", "Repair guides", 1),
    (r"^name:devdocs", "Software documentation", 1),
    (r"^name:maps", "Maps", 1),
]
EDU_WORDS = re.compile(
    r"\b(education|educational|school|students?|teach\w*|course|courses|lesson|lessons|textbooks?|curriculum|learn\w*|"
    r"math\w*|science|biology|chemistry|physics|history|geography|kids|children|"
    r"escola|ensino|aula|curso|estudantes|schule|unterricht|lernen|kurs|kinder)\b", re.I)


def classify(entry: dict) -> Tuple[int, str]:
    keys = [f"category:{entry.get('category', '')}", f"name:{entry.get('name', '')}"]
    keys += [f"tag:{t.lower()}" for t in entry.get("tags", [])]
    for pattern, label, level in RULES:
        rx = re.compile(pattern)
        if any(rx.search(k) for k in keys):
            return level, label
    text = " ".join([entry.get("title", ""), entry.get("summary", "")])
    if EDU_WORDS.search(text):
        return 2, "Learning reference"
    return 1, "General interest"


def parse_catalog(xml_bytes: bytes) -> List[dict]:
    root = ET.fromstring(xml_bytes)
    ns = library.ATOM
    entries = []
    for e in root.findall("a:entry", ns):
        get = lambda k: (e.findtext(f"a:{k}", default="", namespaces=ns) or "").strip()  # noqa: E731
        url, size = "", 0
        for link in e.findall("a:link", ns):
            if link.get("rel", "").endswith("acquisition/open-access") and link.get("href"):
                url = link.get("href")
                if url.endswith(".meta4"):
                    url = url[: -len(".meta4")]
                size = int(link.get("length") or 0)
        if not url or not size:
            continue
        entry = {
            "name": get("name"), "title": get("title"), "summary": get("summary"),
            "lang": get("language"), "category": get("category"), "flavour": get("flavour"),
            "tags": [t for t in get("tags").split(";") if t and not t.startswith("_")],
            "articles": int(get("articleCount") or 0), "updated": get("updated")[:10],
            "size": size, "url": url, "file": os.path.basename(urllib.parse.urlparse(url).path),
        }
        entry["level"], entry["label"] = classify(entry)
        entries.append(entry)
    return entries


def fetch_catalog() -> List[dict]:
    """Download the full Kiwix catalog (about 4 MB). Needs internet."""
    return parse_catalog(library._get(CATALOG_ALL, timeout=120))


class Catalog:
    """The catalog, cached in the hub's data folder so it works offline."""

    def __init__(self, cache_path: str):
        self.cache_path = cache_path
        self.entries: List[dict] = []
        self.fetched_at: Optional[float] = None
        self._lock = threading.Lock()
        if os.path.exists(cache_path):
            try:
                with open(cache_path, encoding="utf-8") as fh:
                    data = json.load(fh)
                self.entries, self.fetched_at = data["entries"], data["fetched_at"]
                for e in self.entries:  # re-rate with the current rules
                    e["level"], e["label"] = classify(e)
            except (OSError, ValueError, KeyError):
                self.entries = []

    def refresh(self) -> int:
        entries = fetch_catalog()
        with self._lock:
            self.entries, self.fetched_at = entries, time.time()
            tmp = self.cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"fetched_at": self.fetched_at, "entries": entries}, fh, ensure_ascii=False)
            os.replace(tmp, self.cache_path)
        return len(entries)

    def languages(self) -> List[Tuple[str, int]]:
        """(code, number of collections) for every language: those in the catalog, most
        collections first, then the built-in names, so the menu works before the list is downloaded."""
        counts: Dict[str, int] = {}
        for e in self.entries:
            for code in e["lang"].split(","):
                if code:
                    counts[code] = counts.get(code, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], language_name(kv[0])))
        rest = sorted((code for code in LANGUAGES if code not in counts and code != "mul"), key=language_name)
        return ranked + [(code, 0) for code in rest]

    def groups(self, lang: str = "", min_level: int = 2, query: str = "", label: str = "") -> List[dict]:
        """Collections (all flavours of one name together), education first, then smallest."""
        query = query.lower().strip()
        by_name: Dict[str, dict] = {}
        for e in self.entries:
            if lang and lang not in e["lang"].split(","):
                continue
            if e["level"] < min_level or (label and e["label"] != label):
                continue
            if query and query not in " ".join([e["title"], e["summary"], e["name"]] + e["tags"]).lower():
                continue
            group = by_name.setdefault(e["name"], {"name": e["name"], "title": e["title"], "summary": e["summary"],
                                                   "lang": e["lang"], "label": e["label"], "level": e["level"],
                                                   "category": e["category"], "options": []})
            group["options"].append(e)
        order = {f: i for i, f in enumerate(library.FLAVOURS)}
        groups = list(by_name.values())
        for g in groups:
            g["options"].sort(key=lambda o: (order.get(o["flavour"], 99), o["size"]))
        groups.sort(key=lambda g: (-g["level"], g["label"], g["options"][0]["size"]))
        return groups

    def labels(self, lang: str = "", min_level: int = 0) -> List[str]:
        """Kinds of collection present, most educational first."""
        level_of: Dict[str, int] = {}
        for e in self.entries:
            if e["level"] >= min_level and (not lang or lang in e["lang"].split(",")):
                level_of[e["label"]] = max(level_of.get(e["label"], 0), e["level"])
        return sorted(level_of, key=lambda lab: (-level_of[lab], lab))

    def find(self, name: str, flavour: str) -> Optional[dict]:
        for e in self.entries:
            if e["name"] == name and e["flavour"] == flavour:
                return e
        return None

    def suggest(self, short_lang: str, budget: int) -> List[dict]:
        """The built-in starter plan for a language, sized to the card, from the cached catalog."""
        packages = []
        for spec in library.PACKAGES.get(short_lang, []):
            pkg = library.Package(spec["key"], spec["collection"], spec["license"])
            if "kiwix" in spec:
                for e in self.entries:
                    if e["name"] == spec["kiwix"]:
                        pkg.options.append(library.Option(e["flavour"], e["title"], e["size"], [(e["url"], e["file"])], "zim"))
                order = {f: i for i, f in enumerate(library.FLAVOURS)}
                pkg.options.sort(key=lambda o: (order.get(o.flavour, 99), o.size))
            else:
                pkg.options = [gutenberg_choice()["option"]]
            if pkg.options:
                packages.append(pkg)
        picks = []
        for choice in library.plan(packages, budget):
            if choice.package.key == "books":
                picks.append(gutenberg_choice()["item"])
                continue
            url, filename = choice.option.files[0]
            entry = next(e for e in self.entries if e["url"] == url)
            picks.append(selection_item(entry))
        return picks


GUTENBERG_IDS = library.PACKAGES["en"][2]["gutenberg"]
GUTENBERG_BYTES_PER_BOOK = 620_000  # average of the ten default books, measured Oct 2026


def gutenberg_choice() -> dict:
    files = [(library.GUTENBERG_TXT.format(id=i), f"pg{i}.txt") for i in GUTENBERG_IDS]
    size = GUTENBERG_BYTES_PER_BOOK * len(files)
    item = {"key": "gutenberg|classics", "title": f"{len(files)} classic novels (Project Gutenberg, plain text)",
            "label": "Books and source texts", "level": 2, "lang": "eng", "size": size, "folder": "books",
            "kind": "txt", "files": files, "license": "Public domain in the USA"}
    return {"item": item, "option": library.Option("", item["title"], size, files, "txt")}


def selection_item(entry: dict) -> dict:
    flavour = f" ({entry['flavour']})" if entry["flavour"] else ""
    return {"key": f"{entry['name']}|{entry['flavour']}", "title": entry["title"] + flavour, "label": entry["label"],
            "level": entry["level"], "lang": entry["lang"], "size": entry["size"],
            "folder": entry["category"] or "other", "kind": "zim", "files": [(entry["url"], entry["file"])],
            "license": "See the collection's source"}


class DownloadJob(threading.Thread):
    """Copies chosen collections onto a card folder, resuming partial files."""

    def __init__(self, items: List[dict], dest_root: str):
        super().__init__(name="library-download", daemon=True)
        self.items = items
        self.library_dir = os.path.join(dest_root, "library")
        self.total = sum(i["size"] for i in items)
        self.done_bytes = 0      # finished collections
        self.item_bytes = 0      # finished files of the current collection
        self.file_bytes = 0      # the file being downloaded now
        self.current = ""
        self.finished: List[str] = []
        self.error = ""
        self.state = "running"  # running, done, failed, cancelled
        self.started = time.time()
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def _on_bytes(self, have: int) -> None:
        self.file_bytes = have

    def run(self) -> None:
        try:
            os.makedirs(self.library_dir, exist_ok=True)
            for item in self.items:
                folder = os.path.join(self.library_dir, item["folder"])
                os.makedirs(folder, exist_ok=True)
                per_file = item["size"] if len(item["files"]) == 1 else 0
                self.item_bytes = 0
                for url, filename in item["files"]:
                    if self._cancel.is_set():
                        self.state = "cancelled"
                        return
                    self.current = f"{item['title']}: {filename}"
                    self.file_bytes = 0
                    path = os.path.join(folder, filename)
                    library.download(url, path, per_file, log=lambda line: None, cancel=self._cancel,
                                     progress=self._on_bytes)
                    if self._cancel.is_set():
                        self.state = "cancelled"
                        return
                    self.item_bytes += os.path.getsize(path) if os.path.exists(path) else 0
                    self.file_bytes = 0
                self.done_bytes += item["size"]
                self.item_bytes = 0
                self.finished.append(item["title"])
                self._write_index(item)
            self.state = "done"
            self.current = ""
        except Exception as exc:  # report to the teacher instead of dying silently
            self.state = "failed"
            self.error = str(exc)

    def _write_index(self, item: dict) -> None:
        index_path = os.path.join(self.library_dir, "index.json")
        try:
            with open(index_path, encoding="utf-8") as fh:
                index = json.load(fh)
        except (OSError, ValueError):
            index = []
        by_file = {e["file"]: e for e in index}
        folder = os.path.join(self.library_dir, item["folder"])
        titles = library.gutenberg_titles(folder) if item["kind"] == "txt" else {}
        for url, filename in item["files"]:
            rel = os.path.join(item["folder"], filename)
            by_file[rel] = {"title": titles.get(filename, item["title"]), "file": rel, "collection": item["label"],
                            "kind": item["kind"], "lang": item["lang"], "source": url, "license": item["license"]}
        with open(index_path, "w", encoding="utf-8") as fh:
            json.dump(list(by_file.values()), fh, indent=1, ensure_ascii=False)

    def progress(self) -> dict:
        done = min(self.done_bytes + self.item_bytes + self.file_bytes, self.total)
        return {"state": self.state, "total": self.total, "done": done, "current": self.current,
                "finished": list(self.finished), "error": self.error, "library_dir": self.library_dir}


def remaining_bytes(items: List[dict], dest_root: str) -> int:
    """Bytes still to download, counting files (or partial files) already on the card."""
    library_dir = os.path.join(dest_root, "library")
    remaining = 0
    for item in items:
        have = 0
        for _url, filename in item["files"]:
            path = os.path.join(library_dir, item["folder"], filename)
            for candidate in (path, path + ".part"):
                if os.path.exists(candidate):
                    have += os.path.getsize(candidate)
                    break
        remaining += max(item["size"] - have, 0)
    return remaining


def free_bytes(path: str) -> Optional[int]:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None
