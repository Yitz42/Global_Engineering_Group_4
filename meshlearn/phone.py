"""Student phone server: hosts assignments and the offline library over Wi-Fi.

Runs on an old Android phone (Termux) with its data on the microSD card. The
phone turns on its hotspot; students join it with any device and open the
phone's address in a browser. Nothing needs the internet.

The phone keeps its own copy of every assignment (from a course pack loaded by
memory card or school Wi-Fi, or sent by radio when new). Students only see the
ones the teacher has opened. Submissions are batched so several students'
work shares a few radio packets.

Every word students see comes from the school's language (language.py), which
arrives with the course pack. Until one arrives, the phone speaks English.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional
from urllib.parse import quote

from . import __version__
from . import assignments as asg
from . import comprehension as cmp
from . import courses as crs
from . import protocol
from .db import Database
from .language import Strings
from .messenger import Messenger
from .phonesignin import PhoneSignIn
from .zipapps import PhoneApps
from .web import (ASSIGNMENT_JS, CLEAR_DRAFT_JS, COUNTDOWN_JS, LISTEN_JS, MATH_JS, READING_JS, RESUME_JS, SCRATCH_JS, STUDENT_CSS, App, Request, Response,
                  due_label, esc, file_response, icon, minutes_for, page, picture, pictures_html, redirect)

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY, version TEXT NOT NULL, data TEXT NOT NULL,
    open INTEGER NOT NULL DEFAULT 0, due TEXT NOT NULL DEFAULT '', received REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
    sub_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL, version TEXT, student TEXT NOT NULL,
    answers TEXT NOT NULL, submitted_at REAL NOT NULL, msg_id INTEGER,
    score REAL, max REAL, complete INTEGER, detail TEXT
);
CREATE TABLE IF NOT EXISTS courses (id TEXT PRIMARY KEY, data TEXT NOT NULL, position INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS opens (
    student TEXT NOT NULL, aid TEXT NOT NULL, opened_at REAL NOT NULL, PRIMARY KEY (student, aid)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS newcomers (name TEXT PRIMARY KEY, pic TEXT NOT NULL, look TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reading (
    student TEXT PRIMARY KEY, file TEXT NOT NULL, title TEXT NOT NULL, collection TEXT NOT NULL, at REAL NOT NULL
);
"""
MAX_OPEN_S = 6 * 3600  # an assignment opened longer ago than this: timing unknown

# Library file types, as the key of the word a student knows.
KIND_KEYS = {"html": "kind.page", "htm": "kind.page", "txt": "kind.text", "pdf": "kind.pdf", "epub": "kind.book",
             "png": "kind.picture", "jpg": "kind.picture", "jpeg": "kind.picture", "svg": "kind.picture",
             "mp3": "kind.audio", "mp4": "kind.video", "webm": "kind.video", "zim": "kind.encyclopedia",
             "zip": "kind.app", "ggb": "kind.tool"}
VIEWABLE = {".html", ".htm", ".txt", ".pdf", ".epub", ".png", ".jpg", ".jpeg", ".svg", ".mp3", ".mp4", ".webm", ".zim"}
STARTER_DIR = os.path.join(os.path.dirname(__file__), "starter_library")
MAX_BATCH = 20
LOOKS = {"early": "young", "primary": "mid", "lower": "teen", "upper": "teen", "adult": "teen"}


def looks_for(ages) -> List[str]:
    """The looks a school's ages need, youngest first. Ages 8 to 11 until the hub has said."""
    found = {LOOKS[a] for a in ages or [] if isinstance(a, str) and a in LOOKS} if isinstance(ages, list) else set()
    return [look for look in ("young", "mid", "teen") if look in found] or ["mid"]


def subject_icon(a: dict) -> str:
    """The picture for an assignment's course."""
    text = f"{a.get('course', '')} {a.get('course_title', '')} {a.get('subject', '')}".lower()
    for pattern, name in (("math|matem|mathe|hisab|number", "maths"), ("scien|cienc|wissen|sayansi|plant|natur", "plant"),
                          ("read|writ|liter|lese|lectur|leitura|kusoma|story", "book")):
        if re.search(pattern, text):
            return name
    return "pencil"


def stars_of(s) -> Optional[int]:
    """0 to 3 stars for marked work; None while it waits for a mark."""
    if s["score"] is None or not s["complete"] or not s["max"]:
        return None
    return int(3 * s["score"] / s["max"] + 0.5)


SHELF_FLAT = 40   # a course with more lessons than this lists its topics first


def _size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit in ("B", "KB") else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


class Phone(PhoneSignIn, PhoneApps):
    def __init__(self, data_dir: str, hub_id: str, name: str, messenger_factory, kiwix_url: str = "",
                 batch_s: float = 120.0, admin_pin: str = ""):
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.library_dir = os.path.join(data_dir, "library")
        self.pack_dir = os.path.join(data_dir, "assignments")
        self.hub_id = hub_id
        self.name = name
        self.kiwix_url = kiwix_url.rstrip("/")
        self.batch_s = batch_s
        self.admin_pin = admin_pin
        self._install_starter_library()
        self.db = Database(os.path.join(data_dir, "phone.db"), SCHEMA)
        for column in ("duration REAL", "right_answers TEXT", "signals TEXT", "notes TEXT"):  # added after the first release
            try:
                self.db.execute("ALTER TABLE submissions ADD COLUMN " + column)
            except Exception:
                pass
        self._load_language()
        self.messenger: Messenger = messenger_factory(self.on_message)
        self.load_pack_folder()
        self.app = App("phone")
        self._routes()
        self._flush = threading.Event()
        self._running = False

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        self.messenger.start()
        self._running = True
        threading.Thread(target=self._batch_loop, name="batcher", daemon=True).start()

    def stop(self) -> None:
        self._running = False
        self._flush.set()
        self.messenger.stop()

    def _install_starter_library(self) -> None:
        target = os.path.join(self.library_dir, "starter")
        if not os.path.isdir(target) and os.path.isdir(STARTER_DIR):
            shutil.copytree(STARTER_DIR, target)

    @property
    def node_id(self) -> str:
        return getattr(self.messenger.transport, "node_id", "") or ""

    def inventory(self) -> Dict[str, str]:
        return {r["id"]: r["version"] for r in self.db.all("SELECT id, version FROM assignments")}

    def say_hello(self) -> None:
        usage = shutil.disk_usage(self.data_dir)
        # Only the newest hello matters: a phone restarted often must not queue a pile of them.
        self.messenger.drop_pending(self.hub_id, protocol.HELLO)
        self.messenger.send(self.hub_id, protocol.HELLO, {
            "n": self.name, "f": usage.free // (1024 * 1024), "l": len(self.library_items()),
            "sw": __version__, "inv": self.inventory(),
        })

    # -- language -------------------------------------------------------------

    def _load_language(self) -> None:
        row = self.db.one("SELECT value FROM settings WHERE key = 'language'")
        try:
            data = json.loads(row["value"]) if row else {}
        except ValueError:
            data = {}
        school = Strings(data.get("school") if isinstance(data.get("school"), dict) else None)
        self.school = school
        self.languages: Dict[str, Strings] = {school.code: school}
        self.looks = looks_for(data.get("ages"))
        self._load_roster(data)
        more = data.get("more") if isinstance(data.get("more"), list) else []
        for pack in [data.get("second")] + more:
            if isinstance(pack, dict) and pack.get("code") and pack["code"] not in self.languages:
                self.languages[pack["code"]] = Strings(pack)

    def import_language(self, data) -> bool:
        """Keep the school's words from a course pack. Returns True if they changed."""
        if not isinstance(data, dict) or not isinstance(data.get("school"), dict):
            return False
        second = data.get("second") if isinstance(data.get("second"), dict) else None
        more = [m for m in data["more"] if isinstance(m, dict)] if isinstance(data.get("more"), list) else []
        ages = [a for a in data["ages"] if isinstance(a, str)] if isinstance(data.get("ages"), list) else []
        value = json.dumps({"school": data["school"], "second": second, "more": more, "ages": ages,
                            "students": data.get("students") if isinstance(data.get("students"), list) else [],
                            "signin": data.get("signin") if isinstance(data.get("signin"), dict) else {},
                            "signout": data.get("signout")}, ensure_ascii=False, sort_keys=True)
        row = self.db.one("SELECT value FROM settings WHERE key = 'language'")
        if row and row["value"] == value:
            return False
        self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('language', ?)", (value,))
        self._load_language()
        return True

    def words(self, req: Request) -> Strings:
        """The student's language: the one they switched to, else the school's."""
        return self.languages.get(req.cookies.get("lang", ""), self.school)

    def look(self, req: Request) -> str:
        """Which of the three looks this student sees: their age on the class list, else the one they chose."""
        me = self.person((req.cookies.get("student") or "").partition("~")[0]) if self.roster else None
        chosen = me["a"] if me else req.cookies.get("look", "")
        return chosen if chosen in self.looks else "mid" if "mid" in self.looks else self.looks[-1]

    # -- assignments ----------------------------------------------------------

    def import_assignment(self, packed: dict, open_: Optional[bool] = None, due: Optional[str] = None) -> bool:
        """Store a packed assignment from the radio or a course pack. Returns True if new or changed."""
        assignment = asg.unpack(packed)
        aid, version = assignment.get("id"), packed.get("v") or asg.version_of(assignment)
        if not aid or not assignment.get("questions"):
            return False
        row = self.db.one("SELECT version, open, due FROM assignments WHERE id = ?", (aid,))
        new_open = (1 if open_ else 0) if open_ is not None else (row["open"] if row else 0)
        new_due = due if due is not None else (row["due"] if row and row["due"] else assignment.get("due", ""))
        changed = row is None or row["version"] != version
        self.db.execute(
            "INSERT INTO assignments (id, version, data, open, due, received) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET version = excluded.version, data = excluded.data, open = excluded.open, "
            "due = excluded.due, received = CASE WHEN assignments.version = excluded.version THEN assignments.received ELSE excluded.received END",
            (aid, version, json.dumps(assignment), new_open, new_due, time.time()),
        )
        return changed

    def import_courses(self, courses: List[dict]) -> int:
        count = 0
        for position, c in enumerate(courses or []):
            try:
                course = crs.normalize_course(c)
            except crs.CourseError:
                continue
            self.db.execute("INSERT OR REPLACE INTO courses (id, data, position) VALUES (?, ?, ?)",
                            (course["id"], json.dumps(course), position))
            count += 1
        return count

    def load_pack_folder(self) -> int:
        """Load assignments copied onto the memory card (data/assignments/*.json)."""
        count = 0
        if not os.path.isdir(self.pack_dir):
            return 0
        courses_file = os.path.join(self.pack_dir, "courses.json")
        if os.path.exists(courses_file):
            try:
                with open(courses_file, encoding="utf-8") as fh:
                    self.import_courses(json.load(fh).get("courses", []))
            except (OSError, ValueError):
                log.warning("skipping unreadable courses.json in the course pack")
        language_file = os.path.join(self.pack_dir, "language.json")
        if os.path.exists(language_file):
            try:
                with open(language_file, encoding="utf-8") as fh:
                    self.import_language(json.load(fh))
            except (OSError, ValueError):
                log.warning("skipping unreadable language.json in the course pack")
        for fname in sorted(os.listdir(self.pack_dir)):
            if not fname.endswith(".json") or fname in ("courses.json", "language.json"):
                continue
            try:
                with open(os.path.join(self.pack_dir, fname), encoding="utf-8") as fh:
                    if self.import_assignment(json.load(fh)):
                        count += 1
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                log.warning("skipping unreadable course pack file %s", fname)
        return count

    def sync_from_hub(self, hub_url: str) -> int:
        """Copy every assignment, and the school's words, from the teacher hub over Wi-Fi (no radio)."""
        query = urllib.parse.urlencode({"node": self.node_id, "name": self.name})
        url = hub_url.rstrip("/") + "/api/pack?" + query
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.import_courses(data.get("courses", []))
        self.import_language(data.get("language"))
        return sum(1 for packed in data.get("assignments", []) if self.import_assignment(packed))

    def get_assignment(self, aid: str) -> Optional[dict]:
        row = self.db.one("SELECT data, version, open, due FROM assignments WHERE id = ?", (aid,))
        if not row:
            return None
        assignment = json.loads(row["data"])
        assignment.update(version=row["version"], open=bool(row["open"]), due=row["due"])
        return assignment

    # -- mesh -----------------------------------------------------------------

    def on_message(self, message: protocol.Message) -> None:
        p = message.payload
        if message.type == protocol.ASSIGN:
            if isinstance(p.get("a"), dict):
                self.import_assignment(p["a"], open_=bool(p.get("o")), due=p.get("d"))
        elif message.type == protocol.OPEN:
            aid, version = str(p.get("a", "")), str(p.get("v", ""))
            row = self.db.one("SELECT version FROM assignments WHERE id = ?", (aid,))
            if row and row["version"] == version:
                self.db.execute("UPDATE assignments SET open = ?, due = ? WHERE id = ?",
                                (1 if p.get("o") else 0, p.get("d") or "", aid))
            elif p.get("o"):
                self.messenger.send(self.hub_id, protocol.NEED, {"a": aid, "v": version})
        elif message.type == protocol.RESULT:
            for r in p.get("r") or []:
                self.db.execute(
                    "UPDATE submissions SET score = ?, max = ?, complete = ?, detail = ? WHERE sub_id = ?",
                    (r.get("sc"), r.get("mx"), 1 if r.get("ok") else 0, json.dumps(r.get("q") or {}), r.get("id")),
                )
                if isinstance(r.get("an"), dict):   # right answers to what this student missed, after the due date
                    self.db.execute("UPDATE submissions SET right_answers = ? WHERE sub_id = ?",
                                    (json.dumps(r["an"], ensure_ascii=False), r.get("id")))
                if isinstance(r.get("nt"), dict):   # the teacher's short notes
                    self.db.execute("UPDATE submissions SET notes = ? WHERE sub_id = ?",
                                    (json.dumps(r["nt"], ensure_ascii=False), r.get("id")))
        else:
            log.info("phone ignoring %s message from %s", message.type, message.src)

    def opened(self, student: str, aid: str) -> None:
        """Remember when a student opened an assignment, to time their work."""
        self.db.execute("INSERT OR REPLACE INTO opens (student, aid, opened_at) VALUES (?, ?, ?)", (student, aid, time.time()))

    def submit(self, assignment: dict, student: str, answers: Dict[str, str], duration: Optional[float] = None,
               signals: Optional[dict] = None) -> str:
        """Save a submission now; the batcher sends it with others shortly."""
        sub_id = asg.new_id("s")
        if duration is None:
            row = self.db.one("SELECT opened_at FROM opens WHERE student = ? AND aid = ?", (student, assignment["id"]))
            if row and 0 <= time.time() - row["opened_at"] < MAX_OPEN_S:
                duration = max(1.0, round(time.time() - row["opened_at"], 1))  # under a second still counts
        self.db.execute(
            "INSERT INTO submissions (sub_id, assignment_id, version, student, answers, submitted_at, duration, signals) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (sub_id, assignment["id"], assignment.get("version"), student, json.dumps(answers), time.time(), duration,
             json.dumps(signals) if signals else None),
        )
        self.db.execute("DELETE FROM opens WHERE student = ? AND aid = ?", (student, assignment["id"]))
        if self.db.one("SELECT COUNT(*) AS n FROM submissions WHERE msg_id IS NULL")["n"] >= MAX_BATCH:
            self._flush.set()
        return sub_id

    def flush_submissions(self, force: bool = False) -> Optional[int]:
        """Send waiting submissions as one message once the oldest has waited batch_s."""
        rows = self.db.all("SELECT * FROM submissions WHERE msg_id IS NULL ORDER BY submitted_at LIMIT ?", (MAX_BATCH,))
        if not rows:
            return None
        if not force and len(rows) < MAX_BATCH and time.time() - rows[0]["submitted_at"] < self.batch_s:
            return None
        batch = []
        for r in rows:
            item = {"id": r["sub_id"], "a": r["assignment_id"], "v": r["version"], "u": r["student"],
                    "x": json.loads(r["answers"]), "t": round(r["submitted_at"])}
            if r["duration"]:
                item["d"] = round(r["duration"])
            if r["signals"]:
                item.update(json.loads(r["signals"]))
            batch.append(item)
        msg_id = self.messenger.send(self.hub_id, protocol.SUBMIT, {"s": batch})
        for r in rows:
            self.db.execute("UPDATE submissions SET msg_id = ? WHERE sub_id = ?", (msg_id, r["sub_id"]))
        return msg_id

    def next_send_in(self) -> Optional[float]:
        """Seconds until saved work goes out by radio; None when nothing is waiting to be batched."""
        rows = self.db.all("SELECT submitted_at FROM submissions WHERE msg_id IS NULL ORDER BY submitted_at LIMIT ?",
                           (MAX_BATCH,))
        if not rows:
            return None
        if len(rows) >= MAX_BATCH:
            return 0.0
        return max(0.0, rows[0]["submitted_at"] + self.batch_s - time.time())

    def _batch_loop(self) -> None:
        while self._running:
            try:
                self.flush_submissions()
            except Exception:
                log.exception("sending submissions failed")
            self._flush.wait(min(1.0, max(self.batch_s / 4, 0.1)))
            self._flush.clear()

    # -- library --------------------------------------------------------------

    def library_items(self) -> List[dict]:
        """Everything on the card: the fetcher's index first, then any other files."""
        items: List[dict] = []
        known = set()
        index_path = os.path.join(self.library_dir, "index.json")
        if os.path.exists(index_path):
            try:
                with open(index_path, encoding="utf-8") as fh:
                    for entry in json.load(fh):
                        path = os.path.join(self.library_dir, entry.get("file", ""))
                        if os.path.isfile(path):
                            entry["size"] = os.path.getsize(path)
                            items.append(entry)
                            known.add(os.path.normpath(entry["file"]))
            except (OSError, ValueError):
                log.warning("could not read library index")
        for root, _dirs, files in os.walk(self.library_dir):
            for fname in sorted(files):
                ext = os.path.splitext(fname)[1].lower()
                rel = os.path.normpath(os.path.relpath(os.path.join(root, fname), self.library_dir))
                if ext not in VIEWABLE or rel in known or fname.startswith("."):
                    continue
                title = os.path.splitext(fname)[0].replace("_", " ").replace("-", " ").strip().capitalize()
                folder = os.path.dirname(rel)
                items.append({"title": title, "file": rel, "size": os.path.getsize(os.path.join(root, fname)),
                              "collection": folder.capitalize() if folder else "", "kind": ext.lstrip(".")})
        return items

    # -- web ------------------------------------------------------------------

    def _nav(self, req: Request, w: Strings) -> str:
        """Big tabs; the one for this page is highlighted."""
        path = req.path
        if path.startswith("/library"):
            section = "/library"
        elif path.startswith("/results"):
            section = "/results"
        elif path == "/" or path.startswith(("/c/", "/a/")):
            section = "/"
        else:
            section = ""
        student, young = self.student(req), self.look(req) == "young"
        tabs = [("/", w("young.work") if young and student else w("tab.home"), "home")]
        if not (young and student):   # young students reach the library from a card on their first page
            tabs.append(("/library", w("tab.library"), "books"))
        if student:
            tabs.append(("/results", w("young.stars") if young else w("tab.results"), "chart" if self.look(req) == "teen" else "star"))
        links = ""
        for href, label, name in tabs:
            current = " aria-current='page'" if href == section else ""
            links += f"<a href='{href}'{current}><span>{icon(name)}</span>{esc(label)}</a>"
        return f"<nav class='tabs'>{links}</nav>"

    def _language_switch(self, req: Request, w: Strings) -> str:
        """When the school uses more than one language, a button in the header for each of the others.
        The welcome page has a bigger picker of its own."""
        others = [s for code, s in self.languages.items() if code != w.code]
        if not others or (req.path == "/" and not self.student(req)):
            return ""
        back = req.path + ("?" + urllib.parse.urlencode(req.query) if req.query else "")
        buttons = "".join(f"<button name='to' value='{esc(s.code)}' title='{esc(w('lang.switch'))}' lang='{esc(s.tag)}' "
                          f"dir='{s.dir}'>{esc(s.name)}</button>" for s in others)
        return (f"<form method='post' action='/lang' class='lang'><input type='hidden' name='back' value='{esc(back)}'>"
                f"{buttons}</form>")

    def _language_picker(self, w: Strings) -> str:
        """On the welcome page: every language this school learns in, each in its own words."""
        if len(self.languages) < 2:
            return ""
        buttons = "".join(f"<button name='to' value='{esc(s.code)}' aria-pressed='{'true' if s.code == w.code else 'false'}' "
                          f"lang='{esc(s.tag)}' dir='{s.dir}'>{esc(s.name)}</button>" for s in self.languages.values())
        return (f"<form method='post' action='/lang' class='card'><strong>{esc(w('welcome.language'))}</strong>"
                f"<input type='hidden' name='back' value='/'><div class='langpick'>{buttons}</div></form>")

    def _page(self, req: Request, title: str, body: str) -> str:
        w = self.words(req)
        who = self.student(req)
        site = f"MeshLearn · {self.name}"
        home = who and req.path == "/"
        listen = (f"<button type='button' class='aa' data-reading aria-label='{esc(w('tools.reading'))}'>Aa</button>"
                  f"<button type='button' class='listen' data-listen style='display:none' aria-label='{esc(w('listen.page'))}'>"
                  f"{icon('speaker')}</button>")
        me = self.person(who) if who and self.roster else None
        face = self._face(me) if me else f"<span class='avatar' aria-hidden='true'>{esc(who[:1].upper())}</span>" if who else ""
        out = page(title, body + self._idle_form(bool(who)) + self._reading_panel(w) + f"<script>{LISTEN_JS}</script><script>{MATH_JS}</script>", self._language_switch(req, w) + self._nav(req, w), site=site,
                   extra_css=STUDENT_CSS, lang=w.tag, direction=w.dir, body_cls="st look-" + self.look(req),
                   before=face if home else "", after=listen)
        if who:  # the student's name on the right of the header line, so they can see who is signed in
            out = out.replace(f"<header><strong>{esc(site)}</strong>",
                              f"<header><strong><span>{esc(site)}</span><a class='who' href='/me'>{esc(who)}</a></strong>", 1)
        return out

    @staticmethod
    def _reading_panel(w: Strings) -> str:
        """Bigger text, easy-to-read letters (OpenDyslexic) and more space; the browser remembers the choice."""
        return (f"<div id='readpanel' role='group' aria-label='{esc(w('tools.reading'))}' style='display:none'>"
                f"<button type='button' data-down>A− {esc(w('tools.smaller'))}</button><button type='button' data-up>A+ {esc(w('tools.bigger'))}</button>"
                f"<button type='button' data-font aria-pressed='false'>{esc(w('tools.letters'))}</button>"
                f"<button type='button' data-space aria-pressed='false'>{esc(w('tools.space'))}</button>"
                f"<button type='button' data-shut>{esc(w('tools.close'))}</button></div><script>{READING_JS}</script>")

    @staticmethod
    def _scratchpad(w: Strings) -> str:
        return (f"<div id='scratch' style='display:none'><div class='bar2'><strong>{esc(w('tools.scratch'))}</strong>"
                f"<button type='button' data-undo>{esc(w('tools.undo'))}</button><button type='button' data-clear>{esc(w('tools.clear'))}</button>"
                f"<button type='button' data-shut>{esc(w('tools.close'))}</button></div>"
                f"<canvas aria-label='{esc(w('tools.scratch'))}'></canvas></div><script>{SCRATCH_JS}</script>")

    def _routes(self) -> None:
        app = self.app
        app.add_route("GET", "/", self.home)
        app.add_route("POST", "/login", self.login)
        app.add_route("POST", "/logout", self.logout)
        app.add_route("POST", "/lang", self.switch_language)
        app.add_route("POST", "/look", self.switch_look)
        self._signin_routes()
        self._app_routes()
        app.add_route("GET", "/c/<cid>", self.course_page)
        app.add_route("GET", "/a/<aid>", self.assignment_form)
        app.add_route("POST", "/a/<aid>", self.assignment_submit)
        app.add_route("GET", "/results", self.results)
        app.add_route("GET", "/library", self.library)
        app.add_route("GET", "/library/files/<path:path>", self.library_file)
        app.add_route("GET", "/status", self.status)
        app.add_route("POST", "/status/sync", self.status_sync)
        app.add_route("POST", "/status/reload", self.status_reload)

    # -- courses and progress ---------------------------------------------------

    def course_tree(self) -> List[dict]:
        """Courses with their units: from the course pack, plus any the phone only knows
        from assignments that came by radio, plus 'Other assignments'."""
        tree = [json.loads(r["data"]) for r in self.db.all("SELECT data FROM courses ORDER BY position")]
        known = {c["id"]: c for c in tree}
        loose = False
        for r in self.db.all("SELECT data FROM assignments"):
            a = json.loads(r["data"])
            cid = a.get("course", "")
            if not cid:
                loose = True
                continue
            course = known.get(cid)
            if course is None:
                course = known[cid] = {"id": cid, "title": a.get("course_title") or cid, "description": "", "units": []}
                tree.append(course)
            uid = a.get("unit", "")
            if uid and not crs.unit_of(course, uid):
                course["units"].append({"id": uid, "title": a.get("unit_title") or uid, "_no": a.get("unit_no", "")})
        for course in tree:
            if any("_no" in u for u in course["units"]):
                course["units"].sort(key=lambda u: int(u.get("_no") or 999))
        if loose:
            tree.append(dict(crs.OTHER))
        return tree

    @staticmethod
    def _title(course: dict, w: Strings) -> str:
        """A course's title; 'Other assignments' in the student's language."""
        return w("course.other.title") if course["id"] == "" else course["title"]

    @staticmethod
    def _description(course: dict, w: Strings) -> str:
        return w("course.other.description") if course["id"] == "" else course.get("description", "")

    def _assignments_in(self, course_id: str, open_only: bool = True) -> List[dict]:
        rows = self.db.all("SELECT id FROM assignments" + (" WHERE open = 1" if open_only else "")
                           + " ORDER BY due = '', due, received DESC")
        known = {c["id"] for c in self.course_tree()}
        out = []
        for r in rows:
            a = self.get_assignment(r["id"])
            cid = a.get("course", "") if a.get("course", "") in known else ""
            if cid == course_id:
                out.append(a)
        return out

    def my_records(self, student: str) -> List[dict]:
        """This student's graded work, for comprehension.py."""
        records = []
        for s in self.db.all("SELECT * FROM submissions WHERE student = ? AND score IS NOT NULL ORDER BY submitted_at",
                             (student,)):
            a = self.get_assignment(s["assignment_id"])
            if not a:
                continue
            records.append({"student": student, "aid": a["id"], "course": a.get("course", ""), "unit": a.get("unit", ""),
                            "mode": a.get("mode", "graded"), "at": s["submitted_at"], "duration": s["duration"],
                            "questions": a["questions"], "answers": json.loads(s["answers"]),
                            "scores": json.loads(s["detail"] or "{}")})
        return records

    @staticmethod
    def _level(level: str, score, w: Strings) -> str:
        cls = {"Strong": "good", "Good": "good", "Developing": "warn", "Needs work": "bad"}.get(level, "")
        return f"<span class='pill {cls}'>{esc(w.level(level))}{'' if score is None else f' · {score}%'}</span>"

    def _assignment_card(self, a: dict, sub, w: Strings, course_title: str = "") -> str:
        """One assignment as a card that opens it: what it is, when it is due, and what to do next."""
        practice = a.get("mode") == "practice"
        if sub is None:
            state, action = "", w("card.practice") if practice else w("card.start")
        elif practice:
            state, action = f"<span class='pill good'>{esc(w('card.practiced'))}</span>", w("card.practice_again")
        elif sub["score"] is None:
            state, action = f"<span class='pill warn'>{esc(w('card.waiting'))}</span>", ""
        else:
            score, top = format(sub["score"], "g"), format(sub["max"], "g")
            if not sub["complete"]:
                state, action = f"<span class='pill warn'>{esc(w('card.so_far', score=score, max=top))}</span>", ""
            else:
                state, action = f"<span class='pill good'>{esc(w('card.score', score=score, max=top))}</span>", ""
        n = len(a["questions"])
        meta = " · ".join(x for x in (course_title, w.n("card.questions", n), minutes_for(a["questions"], w),
                                      w("card.practice_tag") if practice else "") if x)
        due_text, due_cls = due_label(a.get("due", ""), turned_in=sub is not None, words=w)
        due = f"<p class='due {due_cls}'>{esc(due_text)}</p>" if due_text else ""
        go = f"<span class='button go'>{esc(action)}</span>" if action else ""
        return (f"<a class='card task' href='/a/{esc(a['id'])}'><h3>{esc(a['title'])}</h3>"
                f"<p class='meta'>{esc(meta)}</p>{due}{state}{go}</a>")

    def _my_latest(self, student: str) -> dict:
        return {r["assignment_id"]: r for r in self.db.all(
            "SELECT assignment_id, MAX(submitted_at) AS at, score, max, complete FROM submissions "
            "WHERE student = ? GROUP BY assignment_id", (student,))}

    def home(self, req: Request) -> Response:
        w = self.words(req)
        student = self.student(req)
        if not student:
            link = f"<a href='/library'>{esc(w('welcome.library'))}</a>"
            if self.roster:
                body = (f"<div class='compact'>{self._language_picker(w)}{self._age_picker(req, w)}</div>" + self._who_body(req, w)
                        + f"<p>{w.html('welcome.just_read', link=link)}</p>" + self._signin_script())
                return Response(self._page(req, w("signin.who"), body))
            body = (
                self._language_picker(w) + self._age_picker(req, w)
                + f"<p class='lead'>{esc(w('welcome.lead'))}</p>"
                f"<form method='post' action='/login' class='card'><label for='student'>{esc(w('welcome.name'))}</label>"
                "<input type='text' id='student' name='student' maxlength='40' autocomplete='name' required>"
                f"<button class='big'>{esc(w('welcome.start'))}</button></form>"
                f"<p>{w.html('welcome.just_read', link=link)}</p>"
            )
            return Response(self._page(req, w("welcome.title"), body))
        mine = self._my_latest(student)
        records = self.my_records(student)
        cards, todo = "", []
        for course in self.course_tree():
            open_items = self._assignments_in(course["id"])
            if course["id"] == "" and not open_items:
                continue
            title = self._title(course, w)
            todo += [(a, title) for a in open_items if a["id"] not in mine]
            if course.get("comingSoon"):
                cards += (f"<div class='card' style='opacity:.6'><h2 style='margin-top:0'>{esc(title)}</h2>"
                          f"<p class='muted'>{esc(self._description(course, w))}</p>"
                          f"<span class='pill'>{esc(w('course.coming_later'))}</span></div>")
                continue
            summary = cmp.summarize(records, course)
            done = sum(1 for a in open_items if a["id"] in mine)
            if open_items:
                pct = done * 100 // len(open_items)
                done_text = esc(w("course.done", done=done, total=len(open_items)))
                progress = (f"<div class='bar' role='img' aria-label='{done_text}'><span style='width:{pct}%'></span></div>"
                            f"<p class='meta'>{done_text}</p>")
            else:
                progress = f"<p class='meta'>{esc(w('course.nothing_now'))}</p>"
            cards += (
                f"<a class='card' href='/c/{esc(course['id']) or '_other'}'><h2>{esc(title)}</h2>"
                f"<p class='muted'>{esc(self._description(course, w))}</p>{progress}"
                f"{self._level(summary['level'], summary['score'], w)}</a>"
            )
        change = f"<form method='post' action='/logout'><button class='secondary'>{esc(w('home.change_name'))}</button></form>"
        look = self.look(req)
        if look == "young":
            body = self._home_young(todo, student, w)
        elif look == "teen":
            body = self._home_teen(todo, mine, records, student, w)
        else:
            body = (self._home_top(todo, mine, student, w) + f"<h2>{esc(w('home.courses'))}</h2>"
                    + (f"<div class='grid'>{cards}</div>" if cards else f"<p class='muted'>{esc(w('home.no_courses'))}</p>"))
        return Response(self._page(req, w("home.title", name=student), body + change + f"<script>{RESUME_JS}</script>"))

    @staticmethod
    def _start(a: dict, student: str, w: Strings) -> str:
        """The button that opens an assignment; says "Keep going" when the browser holds answers (RESUME_JS)."""
        label = w("card.practice") if a.get("mode") == "practice" else w("card.start")
        return (f"<a class='button' href='/a/{esc(a['id'])}' data-draft='{esc(student + '|' + a['id'])}' "
                f"data-resume='{esc(w('home.resume'))}'><span class='lb'>{esc(label)}</span> {icon('arrow', 'arr')}</a>")

    @staticmethod
    def _sub(a: dict) -> str:
        name = subject_icon(a)
        return f"<span class='sub {name}'>{icon(name)}</span>"

    def _all_done(self, student: str, w: Strings) -> str:
        return (f"<div class='card'><p class='grade good' style='margin-top:0'>{esc(w('home.all_done'))}</p>"
                f"<p>{esc(w('home.all_done_text'))}</p>"
                f"<a class='button secondary' href='/library'>{esc(w('tab.library'))}</a></div>" + self._keep_reading(student, w))

    def _home_young(self, todo: List[tuple], student: str, w: Strings) -> str:
        """Ages 5 to 7: one big thing to play, the rest as picture cards, and this week's stars."""
        out = f"<h2>{esc(w('young.today'))}</h2>"
        if todo:
            a = todo[0][0]
            out += (f"<a class='bigplay' href='/a/{esc(a['id'])}'>{self._sub(a)}<strong>{esc(a['title'])}</strong>"
                    f"<span class='go2'>{icon('play', 'fillplay')}{esc(w('young.play'))}</span></a>")
            for a, _ in todo[1:4]:
                out += (f"<a class='ycard' href='/a/{esc(a['id'])}'>{self._sub(a)}<span class='grow'>{esc(a['title'])}</span>"
                        f"<span class='ply' aria-hidden='true'>{icon('play', 'fillplay')}</span></a>")
        else:
            out += f"<div class='done'><span class='tick'>{icon('check')}</span><p class='grade'>{esc(w('home.all_done'))}</p></div>"
        out += (f"<a class='ycard' href='/library'><span class='sub book'>{icon('book')}</span>"
                f"<span class='grow'>{esc(w('tab.library'))}</span><span class='ply' aria-hidden='true'>{icon('play', 'fillplay')}</span></a>")
        week = time.time() - 7 * 86400
        stars = sum(stars_of(s) or 0 for s in self.db.all("SELECT * FROM submissions WHERE student = ?", (student,))
                    if s["submitted_at"] >= week)
        return out + f"<div class='weekstars'>{icon('star')}<span>{esc(w('young.stars_n', n=stars))}</span></div>"

    def _home_teen(self, todo: List[tuple], mine: dict, records: List[dict], student: str, w: Strings) -> str:
        """Ages 12 and up: the next thing, everything due by date, and every course at a glance."""
        lead = w("home.caught_up") if not todo else w.n("home.todo", len(todo))
        out = f"<p class='lead'>{esc(lead)}</p>"
        out += self._next_card(*todo[0], student, w) if todo else self._all_done(student, w)
        rows = ""
        for a, course_title in todo[1:9]:
            _, cls = due_label(a.get("due", ""), False, words=w)
            parts = w.day_parts(a.get("due", ""))
            day = f"<span class='day {cls}'>{esc(parts[0])}<b>{parts[1]}</b></span>" if parts else "<span class='day'>–</span>"
            kind = w("card.practice_tag") if a.get("mode") == "practice" else w.n("card.questions", len(a["questions"]))
            rows += (f"<a href='/a/{esc(a['id'])}'>{day}<span class='grow'><strong>{esc(a['title'])}</strong>"
                     f"<span class='meta'>{esc(' · '.join(x for x in (course_title, kind) if x))}</span></span></a>")
        if rows:
            out += f"<h2>{esc(w('home.coming_up'))}</h2><div class='list'>{rows}</div>"
        if len(todo) > 9:
            out += f"<p class='meta'>{esc(w('home.more', n=len(todo) - 9))}</p>"
        tiles = self._course_tiles(records, w)
        return out + f"<h2>{esc(w('home.courses'))}</h2>" + (tiles or f"<p class='muted'>{esc(w('home.no_courses'))}</p>")

    def _course_tiles(self, records: List[dict], w: Strings, marked_only: bool = False) -> str:
        tiles = ""
        for course in self.course_tree():
            if course["id"] == "" and not self._assignments_in(""):
                continue
            title = esc(self._title(course, w))
            if course.get("comingSoon"):
                if not marked_only:
                    tiles += f"<div class='tile' style='opacity:.6'><b>{title}</b><small>{esc(w('course.coming_later'))}</small></div>"
                continue
            s = cmp.summarize(records, course)
            if marked_only and not s["answered"]:
                continue
            score = "–" if s["score"] is None else f"{s['score']}%"
            tiles += (f"<a class='tile' href='/c/{esc(course['id']) or '_other'}'><b>{title}</b><strong>{score}</strong>"
                      f"<div class='bar'><span style='width:{s['score'] or 0}%'></span></div><small>{esc(w.level(s['level']))}</small></a>")
        return f"<div class='tiles'>{tiles}</div>" if tiles else ""

    def _next_card(self, a: dict, course_title: str, student: str, w: Strings) -> str:
        meta = " · ".join(x for x in (course_title, w.n("card.questions", len(a["questions"])),
                                      minutes_for(a["questions"], w)) if x)
        due_text, _ = due_label(a.get("due", ""), False, words=w)
        return (f"<section class='next' aria-labelledby='next-label'><span class='label' id='next-label'>{esc(w('home.next'))}</span>"
                f"<div class='nhead'>{self._sub(a)}<h2>{esc(a['title'])}</h2></div><p class='meta'>{esc(meta)}</p>"
                + (f"<p class='due'>{esc(due_text)}</p>" if due_text else "") + self._start(a, student, w) + "</section>")

    def _home_top(self, todo: List[tuple], mine: dict, student: str, w: Strings) -> str:
        """How much is done, the one thing to do next as a big card, then the rest (Ages 8 to 11 design)."""
        total = len(todo) + sum(1 for course in self.course_tree() for a in self._assignments_in(course["id"]) if a["id"] in mine)
        lead = w("home.caught_up") if not todo else w.n("home.todo", len(todo))
        out = f"<p class='lead'>{esc(lead)}</p>"
        if total:
            done = total - len(todo)
            text = esc(w("home.week_done", done=done, total=total))
            out += (f"<div class='week'><div class='wk'><strong>{esc(w('home.week'))}</strong><span class='meta'>{text}</span></div>"
                    f"<div class='bar' role='img' aria-label='{text}'><span style='width:{done * 100 // total}%'></span></div></div>")
        if not todo:
            return out + self._all_done(student, w)
        out += self._next_card(*todo[0], student, w)
        if len(todo) > 1:
            out += f"<h2>{esc(w('home.coming_up'))}</h2>"
            for a, course_title in todo[1:5]:
                due_text, _ = due_label(a.get("due", ""), False, words=w)
                meta = " · ".join(x for x in (course_title, due_text or (w("card.practice_tag") if a.get("mode") == "practice" else "")) if x)
                out += (f"<a class='row' href='/a/{esc(a['id'])}'>{self._sub(a)}<span class='grow'><strong>{esc(a['title'])}</strong>"
                        f"<span class='meta'>{esc(meta)}</span></span><span class='chev' aria-hidden='true'>{icon('chev', 'arr')}</span></a>")
            if len(todo) > 5:
                out += f"<p class='meta'>{esc(w('home.more', n=len(todo) - 5))}</p>"
        return out

    def course_page(self, req: Request) -> Response:
        w = self.words(req)
        student = self.student(req)
        if not student:
            return redirect("/")
        cid = "" if req.params["cid"] == "_other" else req.params["cid"]
        course = next((c for c in self.course_tree() if c["id"] == cid), None)
        if course is None:
            return Response(self._page(req, w("notfound.title"), f"<p>{esc(w('course.not_here'))}</p>"), status=404)
        mine = self._my_latest(student)
        summary = cmp.summarize(self.my_records(student), course)
        by_unit = {u["id"]: u for u in summary["units"]}
        items = self._assignments_in(cid)
        sections = ""
        for n, unit in enumerate(course["units"], start=1):
            in_unit = [a for a in items if a.get("unit", "") == unit["id"]]
            u = by_unit.get(unit["id"], {"level": "Not started", "score": None})
            reading = (f"<a class='button secondary' href='/library?q={quote(unit.get('read') or unit['title'])}'>"
                       f"{esc(w('course.find_readings'))}</a>")
            sections += (
                f"<h2>{esc(w('course.unit', n=n, title=unit['title']))} {self._level(u['level'], u['score'], w)}</h2>"
                + ("".join(self._assignment_card(a, mine.get(a["id"]), w) for a in in_unit)
                   or f"<p class='muted'>{esc(w('course.nothing_in_unit'))}</p>")
                + f"<p>{reading}</p>"
            )
        loose = [a for a in items if a.get("unit", "") not in {u["id"] for u in course["units"]}]
        if loose:
            heading = w("course.assignments") if not course["units"] else w("course.other_assignments")
            sections += f"<h2>{esc(heading)}</h2>" + "".join(self._assignment_card(a, mine.get(a["id"]), w) for a in loose)
        body = (f"<p class='muted'>{esc(self._description(course, w))}</p>"
                f"<p>{w.html('course.your_level', level=self._level(summary['level'], summary['score'], w))}</p>" + sections)
        return Response(self._page(req, self._title(course, w), body))

    def logout(self, req: Request) -> Response:
        return redirect("/").set_cookie("student", "", max_age=0)

    def switch_language(self, req: Request) -> Response:
        back = req.form.get("back", "/")
        if not back.startswith("/") or back.startswith("//"):
            back = "/"
        code = req.form.get("to", "")
        response = redirect(back)
        return response.set_cookie("lang", code) if code in self.languages else response

    def switch_look(self, req: Request) -> Response:
        look = req.form.get("to", "")
        response = redirect("/")
        return response.set_cookie("look", look) if look in self.looks else response

    def _age_picker(self, req: Request, w: Strings) -> str:
        """On the welcome page, when the school has students of different ages."""
        if len(self.looks) < 2:
            return ""
        buttons = "".join(f"<button name='to' value='{k}' aria-pressed='{'true' if k == self.look(req) else 'false'}'>"
                          f"{esc(w('look.' + k))}</button>" for k in self.looks)
        return (f"<form method='post' action='/look' class='card agepick'><strong>{esc(w('welcome.age'))}</strong>"
                f"<div class='langpick'>{buttons}</div></form>")

    def _render_form(self, req: Request, a: dict, student: str, values: Dict[str, str], errors: Dict[str, str]) -> str:
        """Question cards with big choices, a character count on written answers, and a bar at the bottom
        saying how many are answered. ASSIGNMENT_JS keeps the answers in the browser while the student works."""
        w = self.words(req)
        parts = []
        n = len(a["questions"])
        count = w.n("card.questions", n)
        due_text, _ = due_label(a.get("due", ""), False, words=w)
        sub = " · ".join(x for x in (due_text, w("form.saved_here")) if x)
        parts.append(f"<p class='meta formmeta'>{esc(w('form.meta', count=count, minutes=minutes_for(a['questions'], w)))}</p>")
        if errors:
            parts.append(f"<p class='error' role='alert'>{esc(w.n('form.fix', len(errors)))}</p>")
        parts.append(f"<form id='aform' method='post' action='/a/{esc(a['id'])}' data-key='{esc(student + '|' + a['id'])}' "
                     f"data-none='{esc(w('form.no_answer'))}' data-flagged='{esc(w('form.flagged'))}' "
                     f"data-unsure='{esc(w('form.unsure'))}' data-change='{esc(w('form.change'))}' data-next='{esc(w('form.next'))}' data-review='{esc(w('form.review'))}' "
                     f"data-star=\"{icon('star')}\" novalidate>")
        parts.append(f"<div class='qhead'><a class='qx' href='/' aria-label='{esc(w('form.close'))}'>{icon('close')}</a>"
                     f"<span class='qt'><b>{esc(a['title'])}</b><small>{esc(sub)}</small></span>"
                     f"<button type='button' class='scratchopen' id='scratchopen' aria-label='{esc(w('tools.scratch'))}'>{icon('pencil')}</button>"
                     f"<button type='button' class='aa' data-reading aria-label='{esc(w('tools.reading'))}'>Aa</button>"
                     f"<button type='button' class='listen' data-listen style='display:none' aria-label='{esc(w('listen.button'))}'>"
                     f"{icon('speaker')}<span>{esc(w('listen.button'))}</span></button></div><div class='qprog' id='qprog'></div>")
        for i, q in enumerate(a["questions"], start=1):
            qid = esc(q["id"])
            value = values.get(q["id"], "")
            err = errors.get(q["id"])
            err_html = f"<p class='error' id='{qid}-err'>{esc(w.error(err))}</p>" if err else ""
            described = f" aria-describedby='{qid}-err' aria-invalid='true'" if err else ""
            points = w("form.points.one") if q["points"] == 1 else w("form.points.other", n=f"{q['points']:g}")
            legend = (f"<legend><span class='qnum'>{esc(w('form.qnum', i=i, n=n, points=points))}"
                      f"</span>{esc(q['prompt'])}</legend>") + pictures_html(q.get("pic", ""))
            cls = "q bad" if err else "q"
            info = self._question_info(a, q, i, w)
            flag = (f"<label class='flag unsure'><input type='checkbox' name='unsure-{qid}' value='1'"
                    f"{' checked' if ('unsure-' + q['id']) in values else ''}> {esc(w('form.unsure'))}</label>"
                    f"<label class='flag'><input type='checkbox' data-flag='{qid}'> {esc(w('form.flag'))}</label></fieldset></div>")
            parts.append(f"<div class='qwrap{' has' if info else ''}' data-prompt='{esc(q['prompt'])}'>{info}")
            if q["type"] == "choice":
                opts = ""
                opic = q.get("opic") or []
                short = " short" if any(opic) or max(len(str(o)) for o in q["options"]) <= 4 else ""
                for j, opt in enumerate(q["options"]):
                    on = value == str(j)
                    opts += (f"<label class='option{' on' if on else ''}'><input type='radio' name='{qid}' value='{j}'"
                             f"{' checked' if on else ''}{described}>"
                             f"<span class='ltr' aria-hidden='true'>{chr(65 + j)}</span>{picture(opic[j]) if j < len(opic) else ''}"
                             f"<span class='otx'>{esc(opt)}</span></label>")
                parts.append(f"<fieldset class='{cls}'>{legend}{err_html}<div class='opts{short}'>{opts}</div>{flag}")
            elif q["type"] == "long":
                limit = q.get("max_chars", 600)
                parts.append(
                    f"<fieldset class='{cls}'>{legend}{err_html}"
                    f"<label for='{qid}'>{esc(w('form.long', n=limit))}</label>"
                    f"<textarea id='{qid}' name='{qid}' maxlength='{limit}'{described}>{esc(value)}</textarea>"
                    f"<p class='counter' id='{qid}-count' aria-live='polite'>{len(value)} / {limit}</p>{flag}"
                )
            else:
                mode = " inputmode='decimal'" if q["type"] == "number" else ""
                hint = w("form.number") if q["type"] == "number" else w("form.short")
                parts.append(
                    f"<fieldset class='{cls}'>{legend}{err_html}"
                    f"<label for='{qid}'>{esc(hint)}</label>"
                    f"<input type='text' id='{qid}' name='{qid}' value='{esc(value)}' maxlength='{asg.MAX_TEXT_CHARS}'{mode}{described}>"
                    f"{flag}"
                )
        parts.append("<input type='hidden' name='first' id='first' value=''>")
        parts.append(f"<section id='review' aria-label='{esc(w('form.review_title'))}'><h2>{esc(w('form.review_title'))}</h2>"
                     f"<p class='meta'>{esc(w('form.review_text'))}</p><div id='reviewlist'></div></section>")
        parts.append(f"<div class='submitbar'><p class='meta' id='answered' aria-live='polite' "
                     f"data-words='{esc(w('form.answered'))}'>{esc(count)}</p>"
                     f"<button class='big' id='submit'>{esc(w('form.submit'))}</button><div class='stepnav'>"
                     f"<button type='button' class='secondary' id='back'><span class='arr' aria-hidden='true'>←</span> {esc(w('form.back'))}</button>"
                     f"<button type='button' class='secondary' id='toreview'>{esc(w('form.review'))}</button>"
                     f"<button type='button' id='next'><span id='nextlabel'>{esc(w('form.next'))}</span> <span class='arr' aria-hidden='true'>→</span></button>"
                     f"</div></div></form>{self._scratchpad(w)}<script>{ASSIGNMENT_JS}</script>")
        return "".join(parts)

    def _question_info(self, a: dict, q: dict, i: int, w: Strings) -> str:
        """The panel that goes with a question: its info, and the assignment's instructions. Students pull it
        down on a phone; it sits beside the question on a wide screen. Open at the first question."""
        text = f"<p>{esc(q['info'])}</p>" if q.get("info") else ""
        if a.get("instructions"):
            text += (f"<h3>{esc(w('form.instructions'))}</h3>" if text else "") + f"<p>{esc(a['instructions'])}</p>"
        if not text:
            return ""
        label = w("form.info") if q.get("info") else w("form.instructions")
        return (f"<details class='qinfo'{' open' if i == 1 else ''}><summary>{esc(label)}</summary>"
                f"<div class='txt' tabindex='0'>{text}</div></details>")

    def _open_assignment(self, req: Request):
        a = self.get_assignment(req.params["aid"])
        return a if a and a.get("open") else None

    def assignment_form(self, req: Request) -> Response:
        w = self.words(req)
        student = self.student(req)
        if not student:
            return redirect("/")
        a = self._open_assignment(req)
        if a is None:
            return Response(self._page(req, w("open.not_title"), f"<p>{esc(w('open.not_text'))}</p>"
                                       f"<p><a href='/'>{esc(w('open.back'))}</a></p>"), status=404)
        if a.get("mode") != "practice":
            done = self.db.one("SELECT submitted_at FROM submissions WHERE assignment_id = ? AND student = ?", (a["id"], student))
            if done:
                body = (f"<div class='card'><p>{esc(w('open.already', when=w.when(done['submitted_at'])))}</p></div>"
                        f"<a class='button big' href='/results'>{esc(w('open.see_results'))}</a>"
                        f"<a class='button big secondary' href='/'>{esc(w('nav.back_home'))}</a>")
                return Response(self._page(req, a["title"], body))
        self.opened(student, a["id"])
        return Response(self._page(req, a["title"], self._render_form(req, a, student, {}, {})))

    def assignment_submit(self, req: Request) -> Response:
        w = self.words(req)
        student = self.student(req)
        if not student:
            return redirect("/")
        a = self._open_assignment(req)
        if a is None:
            return redirect("/")
        raw = {q["id"]: req.form.get(q["id"], "") for q in a["questions"]}
        answers, errors = asg.validate_answers(a, raw)
        unsure = {q["id"] for q in a["questions"] if req.form.get("unsure-" + q["id"])}
        if errors:
            raw.update({"unsure-" + qid: "1" for qid in unsure})
            return Response(self._page(req, a["title"], self._render_form(req, a, student, raw, errors)), status=400)
        if a.get("mode") != "practice" and self.db.one(
                "SELECT 1 FROM submissions WHERE assignment_id = ? AND student = ?", (a["id"], student)):
            return redirect("/results")
        self.submit(a, student, answers, signals=asg.signals_of(a, unsure, req.form.get("first", ""), answers))
        # Turned in: forget the draft the browser kept while the student worked.
        clear_draft = (f"<div id='clear-draft' data-key='{esc(student + '|' + a['id'])}'></div>"
                       f"<script>{CLEAR_DRAFT_JS}</script>")
        if a.get("mode") == "practice":
            feedback = asg.practice_feedback(a, answers)
            items = ""
            for q in a["questions"]:
                ok = feedback.get(q["id"])
                mark = (f"<span class='good'>{esc(w('practice.correct'))}</span>" if ok else
                        f"<span class='bad'>{esc(w('practice.not_quite'))}</span>") if ok is not None \
                    else f"<span class='muted'>{esc(w('practice.teacher_reads'))}</span>"
                items += f"<li>{esc(q['prompt'])}: {mark}</li>"
            right = sum(1 for v in feedback.values() if v)
            checked = sum(1 for v in feedback.values() if v is not None)
            body = (f"<div class='card'><p class='grade'>{esc(w('practice.right', right=right, checked=checked))}</p>"
                    f"<p class='meta'>{esc(w('practice.note'))}</p>"
                    f"<ol>{items}</ol></div>"
                    f"<a class='button big' href='/a/{esc(a['id'])}'>{esc(w('practice.try_again'))}</a>"
                    f"<a class='button big secondary' href='/'>{esc(w('nav.back_home'))}</a>" + clear_draft)
            return Response(self._page(req, w("practice.title"), body))
        if self.look(req) == "young":
            body = (f"<div class='done'><div class='bigstars' aria-hidden='true'>{icon('star') * 3}</div></div>"
                    f"<p class='lead'>{esc(w('submitted.done'))}</p>"
                    f"<a class='button big' href='/results'>{esc(w('young.see_stars'))}</a>"
                    f"<a class='button big secondary' href='/'>{esc(w('nav.back_home'))}</a>" + clear_draft)
            return Response(self._page(req, w("young.well_done"), body))
        tick = icon("check")
        body = (f"<div class='done'><span class='tick'>{tick}</span><p class='grade'>{esc(w('submitted.done'))}</p></div>"
                f"<div class='card'><h2 style='margin-top:0'>{esc(w('submitted.where'))}</h2><ol class='steps'>"
                f"<li class='ok'><b>{tick}</b><span>{esc(w('submitted.saved'))}</span></li>"
                f"<li><b>2</b><span>{esc(w('submitted.sent'))}{self._send_countdown(w)}</span></li>"
                f"<li><b>3</b><span>{esc(w('results.acked'))}</span></li><li><b>4</b><span>{esc(w('submitted.marked'))}</span></li></ol>"
                f"<p class='meta'>{esc(w('submitted.later'))}</p></div>"
                f"<p class='meta'>{esc(w('submitted.no_wait'))} {esc(w('submitted.safe'))}</p>"
                f"<a class='button big' href='/'>{esc(w('nav.back_home'))}</a>" + clear_draft)
        return Response(self._page(req, w("submitted.title"), body))

    def _send_countdown(self, w: Strings) -> str:
        """'It goes to your teacher in about 1:45.', counting down in the browser (COUNTDOWN_JS)."""
        left = self.next_send_in()
        if left is None or left < 1:
            return f"<p class='sendin'>{esc(w('submitted.sending'))}</p>"
        s = int(left + 0.5)
        words = w("submitted.sends_in", time="{time}")
        return (f"<p class='sendin' id='sendin' aria-live='polite' data-in='{s}' data-words='{esc(words)}' "
                f"data-now='{esc(w('submitted.sending'))}'>{esc(words.replace('{time}', f'{s // 60}:{s % 60:02d}'))}</p>"
                f"<script>{COUNTDOWN_JS}</script>")

    def results(self, req: Request) -> Response:
        w = self.words(req)
        student = self.student(req)
        if not student:
            return redirect("/")
        rows = self.db.all("SELECT * FROM submissions WHERE student = ? ORDER BY submitted_at DESC", (student,))
        if self.look(req) == "young":
            return Response(self._page(req, w("young.stars"), self._results_young(rows, w)))
        teen = self.look(req) == "teen"
        out = ""
        for i, s in enumerate(rows):
            a = self.get_assignment(s["assignment_id"]) or {"title": s["assignment_id"], "questions": []}
            delivery = self.messenger.message_status(s["msg_id"])
            sent = {"acked": f"<span class='pill good'>{esc(w('results.acked'))}</span>",
                    "pending": f"<span class='pill warn'>{esc(w('results.pending'))}</span>",
                    "failed": f"<span class='pill bad'>{esc(w('results.failed'))}</span>"}.get(
                delivery, f"<span class='pill'>{esc(w('results.saved'))}</span>")
            if i == 0 and not teen:
                out += self._newest_result(s, a, sent, w) + (f"<h2>{esc(w('results.earlier'))}</h2>" if len(rows) > 1 else "")
                continue
            course = self._course_title_of(a, w)
            if s["score"] is None:
                out += (f"<div class='row wait'><span class='grow'><strong>{esc(a['title'])}</strong>"
                        f"<span class='meta'>{esc(course)}</span></span><span class='end small'>{sent}</span></div>")
                continue
            if s["complete"]:
                end = f"<span class='end'>{s['score']:g} / {s['max']:g}</span>"
            else:
                end = f"<span class='end small'>{esc(w('results.reading'))}</span>"
                if teen:   # no big card for the newest here, so say how far the marking is and where the work is
                    so_far = w("results.so_far", score=format(s["score"], "g"), max=format(s["max"], "g"))
                    end = f"<span class='end small'>{esc(so_far)}<br>{esc(w('results.reading'))}<br>{sent}</span>"
            out += (f"<div class='row{'' if s['complete'] else ' wait'}'><span class='grow'><strong>{esc(a['title'])}</strong>"
                    f"<span class='meta'>{esc(' · '.join(x for x in (course, w.when(s['submitted_at'])) if x))}</span></span>{end}</div>"
                    + self._teacher_notes(a, s, w))
        records = self.my_records(student)
        if teen:
            review = self._to_review(rows, w)
            body = (self._course_tiles(records, w, marked_only=True) + review
                    + (f"<h2>{esc(w('results.all'))}</h2>{out}" if out else f"<p class='muted'>{esc(w('results.nothing'))}</p>"))
            return Response(self._page(req, w("results.title"), body))
        progress = ""
        for course in self.course_tree():
            summary = cmp.summarize(records, course)
            if not summary["answered"]:
                continue
            units = "".join(f"<li>{esc(w('course.unit', n=u['number'], title=u['title']))} {self._level(u['level'], u['score'], w)}</li>"
                            for u in summary["units"] if u["answered"])
            progress += (f"<div class='card'><h3 style='margin:0'>{esc(self._title(course, w))} "
                         f"{self._level(summary['level'], summary['score'], w)}</h3><ul class='units'>{units}</ul></div>")
        body = ((out or f"<p class='muted'>{esc(w('results.nothing'))}</p>")
                + (f"<h2>{esc(w('results.how'))}</h2>" + progress if progress else ""))
        return Response(self._page(req, w("results.title"), body))

    @staticmethod
    def _given(q: dict, answers: dict) -> str:
        """What the student answered, as words."""
        given = answers.get(q["id"], "")
        if q["type"] == "choice" and given.isdigit() and int(given) < len(q.get("options", [])):
            given = q["options"][int(given)]
        return given or "—"

    def _right_answer(self, a: dict, q: dict, s, w: Strings) -> str:
        """The right answer to a question this student missed: from the hub after the due date, or the practice key."""
        sent = json.loads(s["right_answers"] or "{}") if s["right_answers"] else {}
        text = sent.get(q["id"], "")
        if not text and a.get("mode") == "practice":
            text = asg.key_text(q)
        return f"<span class='rightans'>{esc(w('results.answer', answer=text))}</span>" if text else ""

    @staticmethod
    def _teacher_notes(a: dict, s, w: Strings) -> str:
        """Short notes the teacher wrote back, with the question each is about."""
        notes = json.loads(s["notes"] or "{}") if s["notes"] else {}
        out = ""
        for n, q in enumerate(a.get("questions", []), start=1):
            text = notes.get(q["id"])
            if isinstance(text, str) and text.strip():
                out += (f"<div class='tnote'><span class='meta'>{n}. {esc(q['prompt'])}</span>"
                        f"<span>{esc(w('results.note', note=text))}</span></div>")
        return out

    def _results_young(self, rows, w: Strings) -> str:
        """Ages 5 to 7: stars out of three for each piece of work."""
        out, seen = "", set()
        for s in rows:
            if s["assignment_id"] in seen:
                continue
            seen.add(s["assignment_id"])
            a = self.get_assignment(s["assignment_id"]) or {"id": "", "title": s["assignment_id"], "questions": []}
            got = stars_of(s)
            if got is None:
                stars = f"<p class='meta'>{esc(w('results.reading') if s['score'] is not None else w('results.not_graded'))}</p>"
            else:
                stars = (f"<div class='stars' role='img' aria-label='{got} / 3'>"
                         + "".join(icon("star", "on" if n < got else "") for n in range(3)) + "</div>")
            again = (f"<a class='button' href='/a/{esc(a['id'])}'>{icon('redo')}{esc(w('young.more'))}</a>"
                     if a.get("mode") == "practice" and a.get("open") and (got or 0) < 3 else "")
            out += (f"<div class='ycard col'><div class='top'>{self._sub(a)}<span class='grow'>{esc(a['title'])}</span></div>"
                    f"{stars}{self._teacher_notes(a, s, w)}{again}</div>")
        return out or f"<p class='muted'>{esc(w('results.nothing'))}</p>"

    def _to_review(self, rows, w: Strings) -> str:
        """Ages 12 and up: the latest questions answered wrong, up to five."""
        out, count, seen = "", 0, set()
        for s in rows:
            if s["assignment_id"] in seen or s["score"] is None or count >= 5:
                continue
            seen.add(s["assignment_id"])
            a = self.get_assignment(s["assignment_id"]) or {"title": "", "questions": []}
            detail, answers = json.loads(s["detail"] or "{}"), json.loads(s["answers"] or "{}")
            for n, q in enumerate(a["questions"], start=1):
                if detail.get(q["id"]) == 0 and count < 5:
                    count += 1
                    out += (f"<div class='missed'><span class='meta'>{esc(a['title'])} · {n}</span><strong>{esc(q['prompt'])}</strong>"
                            f"<span class='meta'>{esc(w('results.you_said', answer=self._given(q, answers)))}</span>"
                            f"{self._right_answer(a, q, s, w)}</div>")
        return f"<h2>{esc(w('results.to_review'))}</h2>{out}" if out else ""

    def _course_title_of(self, a: dict, w: Strings) -> str:
        course = next((c for c in self.course_tree() if c["id"] and c["id"] == a.get("course")), None)
        return self._title(course, w) if course else a.get("course_title", "")

    def _newest_result(self, s, a: dict, sent: str, w: Strings) -> str:
        """The latest work as a big card: how many right, a square per question, and the first one missed."""
        detail = json.loads(s["detail"] or "{}")
        answers = json.loads(s["answers"] or "{}")
        squares, right, missed = "", 0, ""
        for n, q in enumerate(a.get("questions", []), start=1):
            score = detail.get(q["id"]) if s["score"] is not None else None
            points = q.get("points", 1) or 1
            if score is None:
                cls, mark = "wait", "…"
            elif score >= points:
                cls, mark = "right", icon("check")
                right += 1
            elif score > 0:
                cls, mark = "part", "½"
            else:
                cls, mark = "wrong", icon("cross")
                if not missed:
                    missed = (f"<div class='missed'><strong>{n}. {esc(q['prompt'])}</strong>"
                              f"<span class='meta'>{esc(w('results.you_said', answer=self._given(q, answers)))}</span>"
                              f"{self._right_answer(a, q, s, w)}</div>")
            squares += f"<span class='{cls}' aria-hidden='true'>{mark}</span>"
        total = len(a.get("questions", []))
        if s["score"] is None:
            headline = f"<p class='meta'>{esc(w('results.not_graded'))}</p>"
        elif s["complete"]:
            well = total and right * 10 >= total * 7
            headline = (f"<p class='bigscore{' good' if well else ''}'>{esc(w('results.right', right=right, total=total))}</p>"
                        f"<p class='meta'>{s['score']:g} / {s['max']:g}</p>")
        else:
            so_far = w("results.so_far", score=format(s["score"], "g"), max=format(s["max"], "g"))
            headline = f"<p class='bigscore'>{esc(so_far)}</p><p class='meta'>{esc(w('results.still_grading'))}</p>"
        grid = (f"<div class='squares' role='img' aria-label='{esc(w('results.right', right=right, total=total))}'>{squares}</div>"
                if squares and s["score"] is not None else "")
        again = (f"<a class='button big' href='/a/{esc(a['id'])}'>{esc(w('results.try_again'))}</a>"
                 if a.get("mode") == "practice" and a.get("id") and a.get("open") else "")
        course = self._course_title_of(a, w)
        return (f"<section class='card' aria-labelledby='newest'><span class='new' id='newest'>{esc(w('results.newest'))}</span>"
                f"<h2 style='margin:10px 0 2px;color:var(--ink)'>{esc(a['title'])}</h2>"
                f"<p class='meta'>{esc(' · '.join(x for x in (course, w('results.turned_in', when=w.when(s['submitted_at']))) if x))}</p>"
                f"{headline}{grid}{missed}{self._teacher_notes(a, s, w)}{sent}{again}</section>")

    def library(self, req: Request) -> Response:
        """Books and lessons on the card. Whole courses (entries with a "shelf", such as a Kolibri channel) get
        one card each here and a page of their own, so a thousand videos don't fill this one."""
        w = self.words(req)
        query = (req.query.get("q") or "").strip().lower()
        shelf, part = req.query.get("shelf") or "", req.query.get("c") or ""
        items = self.library_items()
        body = (f"<form method='get' action='/library' class='search'><label class='vh' for='q'>{esc(w('library.search_label'))}</label>"
                f"{icon('search')}<input type='text' id='q' name='q' value='{esc(query)}' placeholder='{esc(w('library.search_label'))}'><button>{esc(w('library.search'))}</button></form>")
        if query:
            items = [i for i in items if query in f"{i.get('title', '')} {i.get('collection', '')} {i.get('shelf', '')}".lower()]
            body += f"<p class='meta'>{esc(w('library.showing', q=query))} <a href='/library'>{esc(w('library.show_all'))}</a></p>"
            body += self._library_groups(w, items)
        elif shelf:
            items = [i for i in items if i.get("shelf") == shelf]
            parts: Dict[str, List[dict]] = {}
            for item in items:
                parts.setdefault(item.get("collection") or w("library.other"), []).append(item)
            link = "/library?shelf=" + quote(shelf)
            if part in parts:
                body += (f"<p class='meta'><a href='/library'>{esc(w('library.title'))}</a> › "
                         f"<a href='{link}'>{esc(shelf)}</a></p>" + self._library_groups(w, parts[part]))
            else:
                body += f"<p class='meta'><a href='/library'>{esc(w('library.title'))}</a></p><h2>{esc(shelf)}</h2>"
                if len(items) <= SHELF_FLAT or len(parts) == 1:
                    body += self._library_groups(w, items)
                else:
                    body += "".join(self._library_card(f"{link}&amp;c={quote(name)}", name, w("library.lessons", n=len(entries)))
                                    for name, entries in parts.items())
        else:
            body += self._keep_reading(self.student(req), w)
            shelves: Dict[str, int] = {}
            for item in items:
                if item.get("shelf"):
                    shelves[item["shelf"]] = shelves.get(item["shelf"], 0) + 1
            if shelves:
                body += f"<h2>{esc(w('library.courses'))}</h2>" + "".join(
                    self._library_card("/library?shelf=" + quote(name), name, w("library.lessons", n=shelves[name]))
                    for name in sorted(shelves))
            body += self._library_groups(w, [i for i in items if not i.get("shelf")], by_name=True)
        if not items:
            body += f"<p class='muted'>{esc(w('library.nothing'))}</p>"
        body += f"<p class='meta'><a href='/about'>{esc(w('about.title'))}</a></p>"
        return Response(self._page(req, w("library.title"), body))

    def _keep_reading(self, student: str, w: Strings) -> str:
        """The last thing this student opened in the library."""
        row = self.db.one("SELECT * FROM reading WHERE student = ?", (student,)) if student else None
        if row is None or not os.path.isfile(os.path.join(self.library_dir, row["file"])):
            return ""
        href = "/library/open?f=" + quote(row["file"], safe="") if row["file"].endswith(".zip") else "/library/files/" + quote(row["file"])
        return (f"<a class='keepread' href='{href}'><span class='sub book'>{icon('book')}</span>"
                f"<span class='grow'><small>{esc(w('library.keep'))}</small><strong>{esc(row['title'])}</strong>"
                f"<span>{esc(row['collection'])}</span></span>{icon('chev', 'arr')}</a>")

    def _remember_reading(self, student: str, rel: str) -> None:
        item = next((i for i in self.library_items() if i["file"].replace(os.sep, "/") == rel), None)
        if item:
            self.db.execute("INSERT OR REPLACE INTO reading (student, file, title, collection, at) VALUES (?, ?, ?, ?, ?)",
                            (student, rel, item["title"], item.get("shelf") or item.get("collection") or "", time.time()))

    @staticmethod
    def _library_card(href: str, title: str, meta: str, kind: str = "shelf") -> str:
        name = {"mp4": "video", "webm": "video", "mp3": "speaker", "html": "page", "htm": "page", "png": "picture", "jpg": "picture",
                "jpeg": "picture", "svg": "picture", "shelf": "books", "zim": "books", "zip": "puzzle", "ggb": "maths"}.get(kind, "book")
        return (f"<a class='card item' href='{href}'><span class='sub'>{icon(name)}</span><span class='grow'>"
                f"<strong>{esc(title)}</strong><span class='meta' style='display:block'>{esc(meta)}</span></span></a>")

    def _library_groups(self, w: Strings, items: List[dict], by_name: bool = False) -> str:
        """Items under a heading per collection, in card order (the course's order) or by name."""
        groups: Dict[str, List[dict]] = {}
        for item in items:
            groups.setdefault(item.get("collection") or w("library.other"), []).append(item)
        out = ""
        for name in sorted(groups) if by_name else groups:
            out += f"<h2>{esc(name)}</h2>"
            for item in groups[name]:
                href = "/library/files/" + quote(item["file"].replace(os.sep, "/"))
                note = ""
                if item.get("kind") == "zim":
                    book = os.path.splitext(os.path.basename(item["file"]))[0]
                    if self.kiwix_url:
                        href = f"{esc(self.kiwix_url)}/content/{quote(book)}"
                    else:
                        note = " · " + w("library.kiwix")
                if item.get("kind") in ("zip", "ggb"):
                    href = "/library/open?f=" + quote(item["file"].replace(os.sep, "/"), safe="")
                if item.get("credit") and item.get("kind") not in ("zip", "ggb"):   # those show it beside the activity
                    note += " · " + item["credit"]
                key = KIND_KEYS.get(item.get("kind", ""))
                kind = w(key) if key else item.get("kind", "")
                out += self._library_card(href, item["title"], f"{kind} · {_size(item.get('size', 0))}{note}",
                                          item.get("kind", ""))
        return out

    def library_file(self, req: Request) -> Response:
        root = os.path.realpath(self.library_dir)
        target = os.path.realpath(os.path.join(root, req.params["path"]))
        if not target.startswith(root + os.sep) or not os.path.isfile(target):
            w = self.words(req)
            return Response(self._page(req, w("notfound.title"), f"<p>{esc(w('library.no_file'))}</p>"), status=404)
        student = self.student(req)
        if student and req.headers.get("Range", "") in ("", "bytes=0-"):   # opened, not a video player's later pieces
            self._remember_reading(student, os.path.relpath(target, root).replace(os.sep, "/"))
        return file_response(target)

    # -- status and admin -----------------------------------------------------

    def _is_admin(self, req: Request) -> bool:
        """Course-pack changes come from the phone itself, or anyone with the PIN."""
        if req.client in ("127.0.0.1", "::1"):
            return True
        return bool(self.admin_pin) and req.form.get("pin", "") == self.admin_pin

    def status(self, req: Request, notice: str = "") -> Response:
        w = self.words(req)
        usage = shutil.disk_usage(self.data_dir)
        q = self.messenger.status()
        held = self.db.one("SELECT COUNT(*) AS n, SUM(open) AS o FROM assignments")
        waiting = self.db.one("SELECT COUNT(*) AS n FROM submissions WHERE msg_id IS NULL")["n"]
        def pin_field(field_id: str) -> str:
            if not self.admin_pin:
                return ""
            return (f"<label for='{field_id}'>{esc(w('status.pin'))}</label>"
                    f"<input type='text' id='{field_id}' name='pin' inputmode='numeric'>")

        names = " · ".join(s.name for s in self.languages.values())
        body = (
            (f"<p class='good' role='status'>{esc(notice)}</p>" if notice else "")
            + "<div class='grid'>"
            f"<div class='card'><div class='stat'>{held['n']}</div>{esc(w('status.stored', n=held['o'] or 0))}</div>"
            f"<div class='card'><div class='stat'>{waiting}</div>{esc(w('status.waiting'))}</div>"
            f"<div class='card'><div class='stat'>{q['pending']}</div>{esc(w('status.radio'))}"
            f"<div class='muted'>{esc(w('status.packets', n=q['frames_sent']))}</div></div>"
            f"<div class='card'><div class='stat'>{_size(usage.free)}</div>{esc(w('status.free'))}</div></div>"
            f"<p>{esc(w('status.language', name=names))}</p>"
            f"<h2>{esc(w('status.copy_heading'))}</h2>"
            "<form method='post' action='/status/sync' class='card'>"
            f"<p>{esc(w('status.copy_text'))}</p>"
            f"<label for='hub_url'>{esc(w('status.hub_address'))}</label>"
            "<input type='text' id='hub_url' name='hub_url' placeholder='http://192.168.1.20:8000' dir='ltr' required>"
            f"{pin_field('pin-sync')}<button>{esc(w('status.copy_button'))}</button></form>"
            "<form method='post' action='/status/reload' class='card'>"
            f"<p>{esc(w('status.reload_text'))}</p>"
            f"{pin_field('pin-reload')}<button class='secondary'>{esc(w('status.reload_button'))}</button></form>"
            f"<p class='muted'>{esc(w('status.footer', hub=self.hub_id, node=self.node_id, dir=self.data_dir))}</p>"
        )
        return Response(self._page(req, w("status.title"), body))

    def status_sync(self, req: Request) -> Response:
        if not self._is_admin(req):
            return self.status(req, notice=self.words(req)("status.only_teacher"))
        url = req.form.get("hub_url", "").strip()
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        try:
            count = self.sync_from_hub(url)
        except Exception as exc:
            return self.status(req, notice=self.words(req)("status.cant_reach", url=url, error=exc))
        # No radio check-in needed: the hub recorded what this phone copied. The words may have changed too.
        return self.status(req, notice=self.words(req)("status.copied", n=count))

    def status_reload(self, req: Request) -> Response:
        if not self._is_admin(req):
            return self.status(req, notice=self.words(req)("status.only_teacher"))
        count = self.load_pack_folder()
        self.say_hello()
        return self.status(req, notice=self.words(req)("status.loaded", n=count))
