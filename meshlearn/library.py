"""Fill the phone's microSD card with open educational content.

Run this somewhere with internet (a school, library or town) before the phone
goes out. It looks up current files and sizes in the Kiwix catalog (offline
Wikipedia, Wikibooks, PhET simulations and more) and Project Gutenberg, picks
the best set that fits the card, and downloads it with resume support.

    python -m meshlearn library --sd-gb 32 --lang pt --dest /path/to/card/meshlearn/library --dry-run
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

KIWIX_CATALOG = "https://library.kiwix.org/catalog/v2/entries"
GUTENBERG_TXT = "https://www.gutenberg.org/cache/epub/{id}/pg{id}.txt"
ATOM = {"a": "http://www.w3.org/2005/Atom"}
USER_AGENT = "MeshLearn-library-fetcher/0.1 (offline education prototype)"

# Kiwix flavours, smallest first: mini = intro of each article, nopic = full text
# without images, maxi = everything. An empty string means the book has one flavour.
FLAVOURS = ("mini", "nopic", "maxi", "")

# Age groups, by age rather than school grade so they mean the same in every country.
AGE_GROUPS = [
    ("early", "Ages 5–8", 5, 8),
    ("primary", "Ages 9–11", 9, 11),
    ("lower", "Ages 12–14", 12, 14),
    ("upper", "Ages 15–18", 15, 18),
    ("adult", "Adults and teachers", 19, 99),
]
DEFAULT_AGES = ("lower", "upper")  # the project's learners are secondary-school students


def age_span(groups) -> Tuple[int, int]:
    """Youngest and oldest age covered by a list of age-group keys."""
    chosen = [g for g in AGE_GROUPS if g[0] in groups] or AGE_GROUPS
    return min(g[2] for g in chosen), max(g[3] for g in chosen)


def ages_overlap(ages: Tuple[int, int], groups) -> bool:
    """True if a collection for `ages` suits at least one chosen group: they share at
    least two years, so a children's collection ending at 12 doesn't count for ages 12-14."""
    return any(min(ages[1], hi) - max(ages[0], lo) >= 1 for key, _, lo, hi in AGE_GROUPS if key in groups)


# Packages in priority order per language, with the ages each suits. Earlier
# packages are kept first when the card is small; later ones are added, and
# flavours upgraded, as room allows. Only packages that suit the students' ages
# are considered.
PACKAGES: Dict[str, List[dict]] = {
    "en": [
        {"key": "phet", "kiwix": "phet_en_all", "collection": "Science simulations (PhET)", "license": "CC BY 4.0", "ages": (8, 99)},
        {"key": "kidsenc", "kiwix": "vikidia_en_all", "collection": "Encyclopedia for children (Vikidia)", "license": "CC BY-SA", "ages": (8, 13)},
        {"key": "picture", "kiwix": "prunelle_en_interactive-books", "collection": "Picture books (Prunelle)", "license": "See source", "ages": (3, 9)},
        {"key": "wp100", "kiwix": "wikipedia_en_100", "collection": "Encyclopedia", "license": "CC BY-SA", "ages": (10, 99)},
        {"key": "books", "gutenberg": [1342, 84, 11, 1661, 2701, 98, 74, 120, 1260, 35],
         "collection": "Classic books (Project Gutenberg)", "license": "Public domain in the USA", "ages": (12, 99)},
        {"key": "wptop", "kiwix": "wikipedia_en_top", "collection": "Encyclopedia", "license": "CC BY-SA", "ages": (10, 99)},
        {"key": "wikibooks", "kiwix": "wikibooks_en_all", "collection": "Textbooks (Wikibooks)", "license": "CC BY-SA", "ages": (13, 99)},
        {"key": "wikiversity", "kiwix": "wikiversity_en_all", "collection": "Courses (Wikiversity)", "license": "CC BY-SA", "ages": (15, 99)},
        {"key": "tedkids", "kiwix": "ted_mul_kids", "collection": "TED talks for kids (video)", "license": "CC BY-NC-ND", "ages": (6, 12)},
        {"key": "readaloud", "kiwix": "booksdash_en_all", "collection": "Read-aloud picture books (Book Dash)", "license": "CC BY 4.0", "ages": (3, 8)},
        {"key": "wpall", "kiwix": "wikipedia_en_all", "collection": "Encyclopedia", "license": "CC BY-SA", "ages": (12, 99)},
        {"key": "wiktionary", "kiwix": "wiktionary_en_all", "collection": "Dictionary (Wiktionary)", "license": "CC BY-SA", "ages": (10, 99)},
    ],
    "pt": [
        {"key": "phet", "kiwix": "phet_pt_all", "collection": "Simulações de ciências (PhET)", "license": "CC BY 4.0", "ages": (8, 99)},
        {"key": "kidsenc", "kiwix": "vikidia_pt_all", "collection": "Enciclopédia para crianças (Vikidia)", "license": "CC BY-SA", "ages": (8, 14)},
        {"key": "tedkids", "kiwix": "ted_mul_kids", "collection": "TED talks for kids (video)", "license": "CC BY-NC-ND", "ages": (6, 12)},
        {"key": "wptop", "kiwix": "wikipedia_pt_top", "collection": "Enciclopédia", "license": "CC BY-SA", "ages": (10, 99)},
        {"key": "wikibooks", "kiwix": "wikibooks_pt_all", "collection": "Livros didáticos (Wikilivros)", "license": "CC BY-SA", "ages": (13, 99)},
        {"key": "wpall", "kiwix": "wikipedia_pt_all", "collection": "Enciclopédia", "license": "CC BY-SA", "ages": (12, 99)},
    ],
    "de": [
        {"key": "phet", "kiwix": "phet_de_all", "collection": "Naturwissenschaftliche Simulationen (PhET)", "license": "CC BY 4.0", "ages": (8, 99)},
        {"key": "kidsenc", "kiwix": "klexikon_de_all", "collection": "Kinderlexikon (Klexikon)", "license": "CC BY-SA", "ages": (6, 12)},
        {"key": "tedkids", "kiwix": "ted_mul_kids", "collection": "TED talks for kids (video)", "license": "CC BY-NC-ND", "ages": (6, 12)},
        {"key": "wptop", "kiwix": "wikipedia_de_top", "collection": "Enzyklopädie", "license": "CC BY-SA", "ages": (10, 99)},
        {"key": "wikibooks", "kiwix": "wikibooks_de_all", "collection": "Lehrbücher (Wikibooks)", "license": "CC BY-SA", "ages": (13, 99)},
        {"key": "wpall", "kiwix": "wikipedia_de_all", "collection": "Enzyklopädie", "license": "CC BY-SA", "ages": (12, 99)},
    ],
}


def specs_for(lang: str, ages=DEFAULT_AGES) -> List[dict]:
    """The starter packages for a language that suit the students' ages."""
    return [spec for spec in PACKAGES.get(lang, []) if ages_overlap(spec["ages"], ages)]

# All of Wikipedia already contains its top-articles sets, so it replaces them.
SUPERSEDES = {"wpall": ("wptop", "wp100")}


@dataclass
class Option:
    """One downloadable variant of a package."""
    flavour: str
    title: str
    size: int
    files: List[Tuple[str, str]]  # (url, filename)
    kind: str


@dataclass
class Package:
    key: str
    collection: str
    license: str
    options: List[Option] = field(default_factory=list)  # smallest first


@dataclass
class Choice:
    package: Package
    option: Option


def _get(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _head_size(url: str) -> Optional[int]:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            length = resp.headers.get("Content-Length")
            return int(length) if length else None
    except Exception:
        return None


def kiwix_options(name: str) -> List[Option]:
    """Current files for a Kiwix book name, one per flavour, smallest first."""
    url = KIWIX_CATALOG + "?" + urllib.parse.urlencode({"name": name, "count": 20})
    root = ET.fromstring(_get(url))
    options = []
    for entry in root.findall("a:entry", ATOM):
        flavour = entry.findtext("a:flavour", default="", namespaces=ATOM) or ""
        title = entry.findtext("a:title", default=name, namespaces=ATOM)
        for link in entry.findall("a:link", ATOM):
            if link.get("rel", "").endswith("acquisition/open-access") and link.get("href"):
                href = link.get("href")
                if href.endswith(".meta4"):  # metalink wrapper; the .zim itself sits beside it
                    href = href[: -len(".meta4")]
                filename = os.path.basename(urllib.parse.urlparse(href).path)
                label = f"{title} ({flavour})" if flavour else title
                options.append(Option(flavour, label, int(link.get("length") or 0), [(href, filename)], "zim"))
    order = {f: i for i, f in enumerate(FLAVOURS)}
    options.sort(key=lambda o: (order.get(o.flavour, 99), o.size))
    return options


def gutenberg_option(ids: List[int]) -> Option:
    files = []
    total = 0
    for book_id in ids:
        url = GUTENBERG_TXT.format(id=book_id)
        size = _head_size(url)
        if size is None:
            continue
        files.append((url, f"pg{book_id}.txt"))
        total += size
        time.sleep(0.5)  # be gentle with Project Gutenberg's servers
    return Option("", f"{len(files)} classic books", total, files, "txt")


def resolve(lang: str, log=print, ages=DEFAULT_AGES) -> List[Package]:
    """Look up live sizes for every package in a language."""
    if lang not in PACKAGES:
        raise ValueError(f"unknown language {lang!r}; choose from {', '.join(PACKAGES)}")
    packages = []
    for spec in specs_for(lang, ages):
        pkg = Package(spec["key"], spec["collection"], spec["license"])
        try:
            if "kiwix" in spec:
                pkg.options = kiwix_options(spec["kiwix"])
            else:
                pkg.options = [gutenberg_option(spec["gutenberg"])]
        except Exception as exc:
            log(f"  ! could not look up {spec['key']}: {exc}")
        pkg.options = [o for o in pkg.options if o.size > 0 and o.files]
        if pkg.options:
            packages.append(pkg)
    return packages


def usable_bytes(sd_gb: float, reserve_gb: float) -> int:
    """Space we may fill: cards hold about 93% of their label after formatting."""
    return max(0, int(sd_gb * 1e9 * 0.93 - reserve_gb * 1e9))


def plan(packages: List[Package], budget: int) -> List[Choice]:
    """Pick packages and flavours that fit the budget.

    Pass 1 takes the smallest version of each package in priority order, so a
    small card still gets a bit of everything important. Pass 2 spends what is
    left upgrading packages, again in priority order. A package that covers
    another (all of Wikipedia vs. its top articles) replaces it.
    """
    chosen: Dict[str, Choice] = {}

    def used() -> int:
        return sum(c.option.size for c in chosen.values())

    for pkg in packages:
        smallest = pkg.options[0]
        freed = sum(chosen[k].option.size for k in SUPERSEDES.get(pkg.key, ()) if k in chosen)
        if used() - freed + smallest.size <= budget:
            for k in SUPERSEDES.get(pkg.key, ()):
                chosen.pop(k, None)
            chosen[pkg.key] = Choice(pkg, smallest)

    for pkg in packages:
        if pkg.key not in chosen:
            continue
        for option in reversed(pkg.options):  # biggest first
            current = chosen[pkg.key].option
            if option.size > current.size and used() - current.size + option.size <= budget:
                chosen[pkg.key] = Choice(pkg, option)
                break

    order = [p.key for p in packages]
    return sorted(chosen.values(), key=lambda c: order.index(c.package.key))


def _fmt(num: int) -> str:
    return f"{num / 1e9:.2f} GB" if num >= 1e8 else f"{num / 1e6:.1f} MB"


def download(url: str, dest: str, expected: int, log=print, cancel=None, progress=None) -> None:
    """Download with resume: a partial file is continued, not restarted.
    `cancel` (a threading.Event) stops early and keeps the partial file for next time;
    `progress(bytes_so_far)` is called as data arrives."""
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    if os.path.exists(dest) and (not expected or os.path.getsize(dest) == expected):
        log(f"  already have {os.path.basename(dest)}")
        return
    headers = {"User-Agent": USER_AGENT}
    if have:
        headers["Range"] = f"bytes={have}-"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        mode = "ab" if have and resp.status == 206 else "wb"
        if mode == "wb":
            have = 0
        last = time.monotonic()
        with open(part, mode) as fh:
            while True:
                if cancel is not None and cancel.is_set():
                    return
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                have += len(chunk)
                if progress is not None:
                    progress(have)
                if time.monotonic() - last > 5:
                    pct = f" {100 * have / expected:.0f}%" if expected else ""
                    log(f"  {os.path.basename(dest)}: {_fmt(have)}{pct}")
                    last = time.monotonic()
    if expected and abs(have - expected) > max(1024, expected // 1000):
        raise IOError(f"{os.path.basename(dest)}: got {have} bytes, expected about {expected}; run again to resume")
    os.replace(part, dest)


def write_index(dest_root: str, choices: List[Choice], lang: str) -> None:
    index_path = os.path.join(dest_root, "index.json")
    existing = []
    if os.path.exists(index_path):
        try:
            with open(index_path, encoding="utf-8") as fh:
                existing = json.load(fh)
        except (OSError, ValueError):
            existing = []
    by_file = {e["file"]: e for e in existing}
    for choice in choices:
        for url, filename in choice.option.files:
            rel = os.path.join(choice.package.key, filename)
            title = choice.option.title
            if choice.option.kind == "txt":
                title = gutenberg_titles(os.path.join(dest_root, choice.package.key)).get(filename, filename)
            by_file[rel] = {"title": title, "file": rel, "collection": choice.package.collection,
                            "kind": choice.option.kind, "lang": lang, "source": url, "license": choice.package.license}
    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(list(by_file.values()), fh, indent=1, ensure_ascii=False)


def gutenberg_titles(dest_dir: str) -> Dict[str, str]:
    """Read 'Title:' lines from downloaded Gutenberg files for nicer names."""
    titles = {}
    for fname in os.listdir(dest_dir):
        if fname.endswith(".txt"):
            try:
                with open(os.path.join(dest_dir, fname), encoding="utf-8", errors="replace") as fh:
                    for _ in range(60):
                        line = fh.readline()
                        if line.startswith("Title:"):
                            titles[fname] = line.split(":", 1)[1].strip()
                            break
            except OSError:
                pass
    return titles


def run(sd_gb: float, lang: str, dest: str, reserve_gb: float = 2.0, dry_run: bool = False, log=print,
        ages=DEFAULT_AGES) -> int:
    log(f"Looking up current open content for language '{lang}'...")
    packages = resolve(lang, log, ages)
    budget = usable_bytes(sd_gb, reserve_gb)
    if not dry_run:
        os.makedirs(dest, exist_ok=True)
        free = shutil.disk_usage(dest).free - int(0.5e9)
        if free < budget:
            log(f"Note: only {_fmt(max(free, 0))} free at {dest}; planning for that instead.")
            budget = max(free, 0)
    choices = plan(packages, budget)
    total = sum(c.option.size for c in choices)

    log(f"\nCard: {sd_gb:g} GB · keeping {reserve_gb:g} GB free for assignments and the system · budget {_fmt(budget)}\n")
    for c in choices:
        log(f"  {_fmt(c.option.size):>10}  {c.option.title}  [{c.package.collection}]")
    chosen_keys = {c.package.key for c in choices}
    covered = {k for key in chosen_keys for k in SUPERSEDES.get(key, ())}
    for p in packages:
        if p.key in chosen_keys:
            continue
        if p.key in covered:
            log(f"  {'covered':>10}  {p.options[0].title} (already inside the full encyclopedia)")
        else:
            log(f"  {'skipped':>10}  {p.options[0].title} (smallest is {_fmt(p.options[0].size)}; needs a bigger card)")
    log(f"\n  {_fmt(total):>10}  total\n")
    if dry_run:
        log("Dry run: nothing downloaded.")
        return 0

    for c in choices:
        folder = os.path.join(dest, c.package.key)
        os.makedirs(folder, exist_ok=True)
        log(f"Downloading {c.option.title}...")
        per_file = c.option.size if len(c.option.files) == 1 else 0
        for url, filename in c.option.files:
            download(url, os.path.join(folder, filename), per_file, log)
    write_index(dest, choices, lang)
    log("Done. The phone will list everything on its Library page.")
    return 0


if __name__ == "__main__":
    sys.exit(run(float(sys.argv[1]), sys.argv[2], sys.argv[3], dry_run="--dry-run" in sys.argv))
