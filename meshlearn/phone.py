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
from .web import (ASSIGNMENT_JS, CLEAR_DRAFT_JS, STUDENT_CSS, App, Request, Response, due_label, esc, file_response,
                  minutes_for, page, redirect)

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
"""
MAX_OPEN_S = 6 * 3600  # an assignment opened longer ago than this: timing unknown

# Library file types, as the key of the word a student knows.
KIND_KEYS = {"html": "kind.page", "htm": "kind.page", "txt": "kind.text", "pdf": "kind.pdf", "epub": "kind.book",
             "png": "kind.picture", "jpg": "kind.picture", "jpeg": "kind.picture", "svg": "kind.picture",
             "mp3": "kind.audio", "mp4": "kind.video", "webm": "kind.video", "zim": "kind.encyclopedia"}
VIEWABLE = {".html", ".htm", ".txt", ".pdf", ".epub", ".png", ".jpg", ".jpeg", ".svg", ".mp3", ".mp4", ".webm", ".zim"}
STARTER_DIR = os.path.join(os.path.dirname(__file__), "starter_library")
MAX_BATCH = 20


def _size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit in ("B", "KB") else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


class Phone:
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
        try:  # added after the first release
            self.db.execute("ALTER TABLE submissions ADD COLUMN duration REAL")
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
        second = data.get("second")
        if isinstance(second, dict) and second.get("code") and second.get("code") != school.code:
            other = Strings(second)
            self.languages[other.code] = other

    def import_language(self, data) -> bool:
        """Keep the school's words from a course pack. Returns True if they changed."""
        if not isinstance(data, dict) or not isinstance(data.get("school"), dict):
            return False
        second = data.get("second") if isinstance(data.get("second"), dict) else None
        value = json.dumps({"school": data["school"], "second": second}, ensure_ascii=False, sort_keys=True)
        row = self.db.one("SELECT value FROM settings WHERE key = 'language'")
        if row and row["value"] == value:
            return False
        self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('language', ?)", (value,))
        self._load_language()
        return True

    def words(self, req: Request) -> Strings:
        """The student's language: the one they switched to, else the school's."""
        return self.languages.get(req.cookies.get("lang", ""), self.school)

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
        else:
            log.info("phone ignoring %s message from %s", message.type, message.src)

    def opened(self, student: str, aid: str) -> None:
        """Remember when a student opened an assignment, to time their work."""
        self.db.execute("INSERT OR REPLACE INTO opens (student, aid, opened_at) VALUES (?, ?, ?)", (student, aid, time.time()))

    def submit(self, assignment: dict, student: str, answers: Dict[str, str], duration: Optional[float] = None) -> str:
        """Save a submission now; the batcher sends it with others shortly."""
        sub_id = asg.new_id("s")
        if duration is None:
            row = self.db.one("SELECT opened_at FROM opens WHERE student = ? AND aid = ?", (student, assignment["id"]))
            if row and 0 <= time.time() - row["opened_at"] < MAX_OPEN_S:
                duration = max(1.0, round(time.time() - row["opened_at"], 1))  # under a second still counts
        self.db.execute(
            "INSERT INTO submissions (sub_id, assignment_id, version, student, answers, submitted_at, duration) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sub_id, assignment["id"], assignment.get("version"), student, json.dumps(answers), time.time(), duration),
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
            batch.append(item)
        msg_id = self.messenger.send(self.hub_id, protocol.SUBMIT, {"s": batch})
        for r in rows:
            self.db.execute("UPDATE submissions SET msg_id = ? WHERE sub_id = ?", (msg_id, r["sub_id"]))
        return msg_id

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
        tabs = [("/", w("tab.home")), ("/library", w("tab.library"))]
        if req.cookies.get("student"):
            tabs.append(("/results", w("tab.results")))
        links = ""
        for href, label in tabs:
            current = " aria-current='page'" if href == section else ""
            links += f"<a href='{href}'{current}>{esc(label)}</a>"
        return f"<nav class='tabs'>{links}</nav>"

    def _language_switch(self, req: Request, w: Strings) -> str:
        """With a second language, a button in the header switches to the other one."""
        other = next((s for code, s in self.languages.items() if code != w.code), None)
        if other is None:
            return ""
        back = req.path + ("?" + urllib.parse.urlencode(req.query) if req.query else "")
        return (f"<form method='post' action='/lang' class='lang'><input type='hidden' name='to' value='{esc(other.code)}'>"
                f"<input type='hidden' name='back' value='{esc(back)}'>"
                f"<button title='{esc(w('lang.switch'))}' lang='{esc(other.tag)}' dir='{other.dir}'>{esc(other.name)}</button></form>")

    def _page(self, req: Request, title: str, body: str) -> str:
        w = self.words(req)
        who = req.cookies.get("student")
        site = f"MeshLearn · {self.name}"
        out = page(title, body, self._language_switch(req, w) + self._nav(req, w), site=site, extra_css=STUDENT_CSS,
                   lang=w.tag, direction=w.dir)
        if who:  # the student's name on the right of the header line, so they can see who is signed in
            out = out.replace(f"<header><strong>{esc(site)}</strong>",
                              f"<header><strong><span>{esc(site)}</span><span class='who'>{esc(who)}</span></strong>", 1)
        return out

    def _routes(self) -> None:
        app = self.app
        app.add_route("GET", "/", self.home)
        app.add_route("POST", "/login", self.login)
        app.add_route("POST", "/logout", self.logout)
        app.add_route("POST", "/lang", self.switch_language)
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
        student = req.cookies.get("student")
        if not student:
            link = f"<a href='/library'>{esc(w('welcome.library'))}</a>"
            body = (
                f"<p class='lead'>{esc(w('welcome.lead'))}</p>"
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
        lead = w("home.caught_up") if not todo else w.n("home.todo", len(todo))
        todo_html = ""
        if todo:
            todo_html = (f"<h2>{esc(w('home.todo_heading'))}</h2>"
                         + "".join(self._assignment_card(a, None, w, title) for a, title in todo[:5])
                         + (f"<p class='meta'>{esc(w('home.more', n=len(todo) - 5))}</p>" if len(todo) > 5 else ""))
        body = (f"<p class='lead'>{esc(lead)}</p>" + todo_html + f"<h2>{esc(w('home.courses'))}</h2>"
                + (f"<div class='grid'>{cards}</div>" if cards else
                   f"<p class='muted'>{esc(w('home.no_courses'))}</p>")
                + f"<form method='post' action='/logout'><button class='secondary'>{esc(w('home.change_name'))}</button></form>")
        return Response(self._page(req, w("home.title", name=student), body))

    def course_page(self, req: Request) -> Response:
        w = self.words(req)
        student = req.cookies.get("student")
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

    def login(self, req: Request) -> Response:
        name = " ".join(req.form.get("student", "").split())[:40]
        if not name:
            return redirect("/")
        return redirect("/").set_cookie("student", name)

    def logout(self, req: Request) -> Response:
        return redirect("/").set_cookie("student", "", max_age=0)

    def switch_language(self, req: Request) -> Response:
        back = req.form.get("back", "/")
        if not back.startswith("/") or back.startswith("//"):
            back = "/"
        code = req.form.get("to", "")
        response = redirect(back)
        return response.set_cookie("lang", code) if code in self.languages else response

    def _render_form(self, req: Request, a: dict, student: str, values: Dict[str, str], errors: Dict[str, str]) -> str:
        """Question cards with big choices, a character count on written answers, and a bar at the bottom
        saying how many are answered. ASSIGNMENT_JS keeps the answers in the browser while the student works."""
        w = self.words(req)
        parts = []
        n = len(a["questions"])
        count = w.n("card.questions", n)
        if a.get("instructions"):
            parts.append(f"<div class='card'><p>{esc(a['instructions'])}</p></div>")
        parts.append(f"<p class='meta'>{esc(w('form.meta', count=count, minutes=minutes_for(a['questions'], w)))}</p>")
        if errors:
            parts.append(f"<p class='error' role='alert'>{esc(w.n('form.fix', len(errors)))}</p>")
        parts.append(f"<form id='aform' method='post' action='/a/{esc(a['id'])}' data-key='{esc(student + '|' + a['id'])}' novalidate>")
        for i, q in enumerate(a["questions"], start=1):
            qid = esc(q["id"])
            value = values.get(q["id"], "")
            err = errors.get(q["id"])
            err_html = f"<p class='error' id='{qid}-err'>{esc(w.error(err))}</p>" if err else ""
            described = f" aria-describedby='{qid}-err' aria-invalid='true'" if err else ""
            points = w("form.points.one") if q["points"] == 1 else w("form.points.other", n=f"{q['points']:g}")
            legend = (f"<legend><span class='qnum'>{esc(w('form.qnum', i=i, n=n, points=points))}"
                      f"</span>{esc(q['prompt'])}</legend>")
            cls = "q bad" if err else "q"
            if q["type"] == "choice":
                opts = ""
                for j, opt in enumerate(q["options"]):
                    on = value == str(j)
                    opts += (f"<label class='option{' on' if on else ''}'><input type='radio' name='{qid}' value='{j}'"
                             f"{' checked' if on else ''}{described}> {esc(opt)}</label>")
                parts.append(f"<fieldset class='{cls}'>{legend}{err_html}{opts}</fieldset>")
            elif q["type"] == "long":
                limit = q.get("max_chars", 600)
                parts.append(
                    f"<fieldset class='{cls}'>{legend}{err_html}"
                    f"<label for='{qid}'>{esc(w('form.long', n=limit))}</label>"
                    f"<textarea id='{qid}' name='{qid}' maxlength='{limit}'{described}>{esc(value)}</textarea>"
                    f"<p class='counter' id='{qid}-count' aria-live='polite'>{len(value)} / {limit}</p></fieldset>"
                )
            else:
                mode = " inputmode='decimal'" if q["type"] == "number" else ""
                hint = w("form.number") if q["type"] == "number" else w("form.short")
                parts.append(
                    f"<fieldset class='{cls}'>{legend}{err_html}"
                    f"<label for='{qid}'>{esc(hint)}</label>"
                    f"<input type='text' id='{qid}' name='{qid}' value='{esc(value)}' maxlength='{asg.MAX_TEXT_CHARS}'{mode}{described}>"
                    "</fieldset>"
                )
        parts.append(f"<div class='submitbar'><p class='meta' id='answered' aria-live='polite' "
                     f"data-words='{esc(w('form.answered'))}'>{esc(count)}</p>"
                     f"<button class='big'>{esc(w('form.submit'))}</button></div></form><script>{ASSIGNMENT_JS}</script>")
        return "".join(parts)

    def _open_assignment(self, req: Request):
        a = self.get_assignment(req.params["aid"])
        return a if a and a.get("open") else None

    def assignment_form(self, req: Request) -> Response:
        w = self.words(req)
        student = req.cookies.get("student")
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
        student = req.cookies.get("student")
        if not student:
            return redirect("/")
        a = self._open_assignment(req)
        if a is None:
            return redirect("/")
        raw = {q["id"]: req.form.get(q["id"], "") for q in a["questions"]}
        answers, errors = asg.validate_answers(a, raw)
        if errors:
            return Response(self._page(req, a["title"], self._render_form(req, a, student, raw, errors)), status=400)
        if a.get("mode") != "practice" and self.db.one(
                "SELECT 1 FROM submissions WHERE assignment_id = ? AND student = ?", (a["id"], student)):
            return redirect("/results")
        self.submit(a, student, answers)
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
        body = (f"<div class='card'><p class='good grade'>{esc(w('submitted.done'))}</p>"
                f"<p>{esc(w('submitted.radio'))}</p>"
                f"<p class='meta'>{esc(w('submitted.later'))}</p></div>"
                f"<a class='button big' href='/'>{esc(w('nav.back_home'))}</a>" + clear_draft)
        return Response(self._page(req, w("submitted.title"), body))

    def results(self, req: Request) -> Response:
        w = self.words(req)
        student = req.cookies.get("student")
        if not student:
            return redirect("/")
        rows = self.db.all("SELECT * FROM submissions WHERE student = ? ORDER BY submitted_at DESC", (student,))
        out = ""
        for s in rows:
            a = self.get_assignment(s["assignment_id"]) or {"title": s["assignment_id"]}
            delivery = self.messenger.message_status(s["msg_id"])
            sent = {"acked": f"<span class='pill good'>{esc(w('results.acked'))}</span>",
                    "pending": f"<span class='pill warn'>{esc(w('results.pending'))}</span>",
                    "failed": f"<span class='pill bad'>{esc(w('results.failed'))}</span>"}.get(
                delivery, f"<span class='pill'>{esc(w('results.saved'))}</span>")
            if s["score"] is None:
                grade = f"<p class='meta'>{esc(w('results.not_graded'))}</p>"
            elif s["complete"]:
                grade = f"<p class='grade'>{s['score']:g} / {s['max']:g}</p>"
            else:
                so_far = w("results.so_far", score=format(s["score"], "g"), max=format(s["max"], "g"))
                grade = f"<p class='grade'>{esc(so_far)}</p><p class='meta'>{esc(w('results.still_grading'))}</p>"
            out += (f"<div class='card'><h3 style='margin:0'>{esc(a['title'])}</h3>"
                    f"<p class='meta'>{esc(w('results.turned_in', when=w.when(s['submitted_at'])))}</p>{grade}{sent}</div>")
        records = self.my_records(student)
        progress = ""
        for course in self.course_tree():
            summary = cmp.summarize(records, course)
            if not summary["answered"]:
                continue
            units = "".join(f"<li>{esc(w('course.unit', n=u['number'], title=u['title']))} {self._level(u['level'], u['score'], w)}</li>"
                            for u in summary["units"] if u["answered"])
            progress += (f"<div class='card'><h3 style='margin:0'>{esc(self._title(course, w))} "
                         f"{self._level(summary['level'], summary['score'], w)}</h3><ul class='units'>{units}</ul></div>")
        body = ((f"<h2>{esc(w('results.how'))}</h2>" + progress if progress else "")
                + f"<h2>{esc(w('results.everything'))}</h2>"
                + (out or f"<p class='muted'>{esc(w('results.nothing'))}</p>"))
        return Response(self._page(req, w("results.title"), body))

    def library(self, req: Request) -> Response:
        w = self.words(req)
        query = (req.query.get("q") or "").strip().lower()
        items = self.library_items()
        if query:
            items = [i for i in items if query in (i.get("title", "") + " " + i.get("collection", "")).lower()]
        groups: Dict[str, List[dict]] = {}
        for item in items:
            groups.setdefault(item.get("collection") or w("library.other"), []).append(item)
        sections = ""
        for name in sorted(groups):
            rows = ""
            for item in groups[name]:
                href = "/library/files/" + quote(item["file"].replace(os.sep, "/"))
                note = ""
                if item.get("kind") == "zim":
                    book = os.path.splitext(os.path.basename(item["file"]))[0]
                    if self.kiwix_url:
                        href = f"{esc(self.kiwix_url)}/content/{quote(book)}"
                    else:
                        note = " · " + w("library.kiwix")
                key = KIND_KEYS.get(item.get("kind", ""))
                kind = w(key) if key else item.get("kind", "")
                rows += (f"<a class='card item' href='{href}'><strong>{esc(item['title'])}</strong>"
                         f"<span class='meta' style='display:block'>{esc(kind)} · {_size(item.get('size', 0))}{esc(note)}</span></a>")
            sections += f"<h2>{esc(name)}</h2>{rows}"
        search = (f"<label for='q'>{esc(w('library.search_label'))}</label><form method='get' action='/library' class='search'>"
                  f"<input type='text' id='q' name='q' value='{esc(query)}'><button>{esc(w('library.search'))}</button></form>"
                  + (f"<p class='meta'>{esc(w('library.showing', q=query))} <a href='/library'>{esc(w('library.show_all'))}</a></p>"
                     if query else ""))
        body = search + (sections or f"<p class='muted'>{esc(w('library.nothing'))}</p>")
        return Response(self._page(req, w("library.title"), body))

    def library_file(self, req: Request) -> Response:
        root = os.path.realpath(self.library_dir)
        target = os.path.realpath(os.path.join(root, req.params["path"]))
        if not target.startswith(root + os.sep) or not os.path.isfile(target):
            w = self.words(req)
            return Response(self._page(req, w("notfound.title"), f"<p>{esc(w('library.no_file'))}</p>"), status=404)
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
