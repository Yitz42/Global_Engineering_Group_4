"""Teacher hub: build assignments, open them on student phones, grade what comes back.

Phones keep their own copy of every assignment (loaded over USB or school
Wi-Fi as a "course pack"), so the radio normally carries only small messages:
open/close an assignment (1 packet), student answers, and grades. A full
assignment travels by radio only when it is new or changed since the phone's
copy, and phones say which versions they hold whenever they check in.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import logging
import os
import re
import threading
import time
import zipfile
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlencode

from . import assignments as asg
from . import ownpics
from . import catalog as cat
from . import courses as crs
from . import library
from . import protocol
from .db import Database
from .hubbuilder import HubBuilder
from .hubclass import HubClass
from .hublessons import HubLessons
from .hubpictures import HubPictures
from .hubtranslate import HubTranslate
from .hubvoice import HubVoice
from .hubkolibri import HubKolibri
from .hublang import HubLanguage
from .hubstudents import HubStudents
from .hubpages import HubPages
from .hubui import HubUI
from .messenger import Messenger
from .zipapps import vendor_response
from .web import PICTURES, App, Request, Response, esc, info_panel, json_response, page, picture, redirect

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY, data TEXT NOT NULL, version TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS assignment_versions (
    aid TEXT NOT NULL, version TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY (aid, version)
);
CREATE TABLE IF NOT EXISTS phones (
    node_id TEXT PRIMARY KEY, name TEXT NOT NULL, last_seen REAL, info TEXT
);
CREATE TABLE IF NOT EXISTS holdings (
    node_id TEXT NOT NULL, aid TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',   -- version the phone holds ('' = none)
    open INTEGER NOT NULL DEFAULT 0,    -- visible to students
    msg_id INTEGER, action TEXT, updated REAL,
    PRIMARY KEY (node_id, aid)
);
CREATE TABLE IF NOT EXISTS submissions (
    sub_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL, version TEXT, student TEXT NOT NULL, phone TEXT NOT NULL,
    answers TEXT NOT NULL, submitted_at REAL, received_at REAL NOT NULL,
    grade TEXT, manual TEXT NOT NULL DEFAULT '{}', result_msg_id INTEGER
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS courses (
    id TEXT PRIMARY KEY, data TEXT NOT NULL, source TEXT NOT NULL, position INTEGER NOT NULL, updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS library_selection (key TEXT PRIMARY KEY, item TEXT NOT NULL, added REAL NOT NULL);
CREATE TABLE IF NOT EXISTS students (
    name TEXT PRIMARY KEY, pic TEXT NOT NULL, ages TEXT NOT NULL, secret TEXT NOT NULL, pin TEXT NOT NULL, added REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS name_links (name TEXT PRIMARY KEY, student TEXT NOT NULL);   -- a name from a phone -> the class list ('' = not a student)
CREATE TABLE IF NOT EXISTS released (aid TEXT PRIMARY KEY, at REAL NOT NULL);           -- right answers sent early by the teacher
CREATE TABLE IF NOT EXISTS pictures (key TEXT PRIMARY KEY, data TEXT NOT NULL, added REAL NOT NULL);   -- the school's own (ownpics.py)
CREATE TABLE IF NOT EXISTS voices (key TEXT PRIMARY KEY, secs REAL NOT NULL, added REAL NOT NULL);
CREATE TABLE IF NOT EXISTS phrases (lang TEXT NOT NULL, key TEXT NOT NULL, text TEXT NOT NULL, PRIMARY KEY (lang, key));
"""

MAX_NOTE_CHARS = 60   # so it fits in one radio message with the marks
LIBRARY_DEFAULTS = {"card_gb": "32", "reserve_gb": "2", "lang": "eng", "lang2": "", "lang3": "", "country": "", "dest": "",
                    "ages": ",".join(library.DEFAULT_AGES), "right_answers": "due",
                    "signin_young": "pic", "signin_mid": "list", "signin_teen": "list", "signout": "10"}
AGE_FILTERS = [("fit", "Suited to the students' ages"), ("any", "Any age (suited ones first)")]
LEVEL_FILTERS = [("3", "Core learning only"), ("2", "Education and reference"), ("0", "Everything")]

ACTION_LABELS = {"open": "opening", "close": "closing", "update": "sending update", "copy": "sending full copy"}


def _when(ts: Optional[float]) -> str:
    return time.strftime("%b %d, %H:%M", time.localtime(ts)) if ts else "never"


class Hub(HubUI, HubClass, HubBuilder, HubPictures, HubLessons, HubTranslate, HubVoice, HubPages, HubLanguage, HubKolibri, HubStudents):
    def __init__(self, db_path: str, messenger_factory):
        self.db = Database(db_path, SCHEMA)
        self.messenger: Messenger = messenger_factory(self.on_message)
        self.catalog = cat.Catalog(os.path.join(os.path.dirname(os.path.abspath(db_path)), "catalog.json"))
        self.kolibri_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "kolibri")
        self.voice_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), "voice")
        self.download_job: Optional[cat.DownloadJob] = None
        self._migrate()
        self.load_course_file()
        self.app = App("hub", max_body=32 * 1024 * 1024)  # learning-data files can be large
        self._routes()
        self._ui_routes()
        self._builder_routes()
        self._picture_routes()
        self._lesson_routes()
        self._translate_routes()
        self._voice_routes()
        self._class_routes()
        self._page_routes()
        self._language_routes()
        self._student_routes()
        self.app.add_route("GET", "/site/<path:path>", lambda req: vendor_response(req.params["path"]))
        threading.Thread(target=self._answers_loop, name="right-answers", daemon=True).start()

    def _migrate(self) -> None:
        """Columns added after the first release; older hub databases get them here."""
        for sql in ("ALTER TABLE submissions ADD COLUMN duration REAL",
                    "ALTER TABLE submissions ADD COLUMN source TEXT NOT NULL DEFAULT 'radio'",
                    "ALTER TABLE submissions ADD COLUMN answers_sent INTEGER NOT NULL DEFAULT 0",
                    "ALTER TABLE submissions ADD COLUMN signals TEXT",
                    "ALTER TABLE submissions ADD COLUMN notes TEXT",
                    "ALTER TABLE submissions ADD COLUMN lang TEXT",
                    "ALTER TABLE students ADD COLUMN lang TEXT NOT NULL DEFAULT ''",
                    "ALTER TABLE phones ADD COLUMN copied REAL"):
            try:
                self.db.execute(sql)
            except Exception:
                pass  # already there

    # -- courses --------------------------------------------------------------

    def load_course_file(self, path: str = crs.COURSE_FILE) -> int:
        """Add the starter courses from the course file. Courses changed on the hub keep the
        hub's version; ones still as the file had them follow the file."""
        try:
            file_courses = crs.load_file(path)
        except (OSError, ValueError) as exc:
            log.warning("could not read course file %s: %s", path, exc)
            return 0
        count = 0
        for position, course in enumerate(file_courses):
            row = self.db.one("SELECT source FROM courses WHERE id = ?", (course["id"],))
            if row and row["source"] == "hub":
                continue
            self.db.execute(
                "INSERT INTO courses (id, data, source, position, updated) VALUES (?, ?, 'file', ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data, position = excluded.position, updated = excluded.updated",
                (course["id"], json.dumps(course), position, time.time()),
            )
            count += 1
        return count

    def courses(self, include_other: bool = False) -> List[dict]:
        rows = self.db.all("SELECT data, source FROM courses ORDER BY position, id")
        out = [dict(json.loads(r["data"]), source=r["source"]) for r in rows]
        if include_other:
            known = {c["id"] for c in out}
            if any(json.loads(r["data"]).get("course", "") not in known for r in self.db.all("SELECT data FROM assignments")):
                out.append(dict(crs.OTHER, source="hub"))
        return out

    def get_course(self, course_id: str) -> Optional[dict]:
        row = self.db.one("SELECT data, source FROM courses WHERE id = ?", (course_id,))
        return dict(json.loads(row["data"]), source=row["source"]) if row else None

    def save_course(self, data: dict) -> dict:
        course = crs.normalize_course(data)
        existing = self.db.one("SELECT position FROM courses WHERE id = ?", (course["id"],))
        position = existing["position"] if existing else (self.db.one("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM courses")["p"])
        self.db.execute(
            "INSERT INTO courses (id, data, source, position, updated) VALUES (?, ?, 'hub', ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET data = excluded.data, source = 'hub', updated = excluded.updated",
            (course["id"], json.dumps(course), position, time.time()),
        )
        return course

    def assignments_in(self, course_id: str, unit_id: Optional[str] = None) -> List[dict]:
        out = []
        known = {c["id"] for c in self.courses()}
        for r in self.db.all("SELECT data FROM assignments ORDER BY created"):
            a = json.loads(r["data"])
            course = a.get("course", "") if a.get("course", "") in known else ""
            if course == course_id and (unit_id is None or a.get("unit", "") == unit_id):
                out.append(a)
        return out

    def _placement(self, assignment: dict) -> str:
        course = self.get_course(assignment.get("course", ""))
        if not course:
            return ""
        unit = crs.unit_of(course, assignment.get("unit", ""))
        if not unit:
            return course["title"]
        return f"{course['title']} · Unit {crs.unit_number(course, unit['id'])}: {unit['title']}"

    # -- assignments ----------------------------------------------------------

    def create_assignment(self, data: dict) -> dict:
        assignment = asg.normalize_assignment(data)
        version = asg.version_of(assignment)
        now = time.time()
        self.db.execute(
            "INSERT INTO assignments (id, data, version, created, updated) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET data = excluded.data, version = excluded.version, updated = excluded.updated",
            (assignment["id"], json.dumps(assignment), version, now, now),
        )
        # Keep every version so answers are graded against the questions the student saw.
        self.db.execute(
            "INSERT OR REPLACE INTO assignment_versions (aid, version, data) VALUES (?, ?, ?)",
            (assignment["id"], version, json.dumps(assignment)),
        )
        return assignment

    def get_assignment(self, aid: str, version: Optional[str] = None) -> Optional[dict]:
        if version:
            row = self.db.one("SELECT data FROM assignment_versions WHERE aid = ? AND version = ?", (aid, version))
            if row:
                return json.loads(row["data"])
        row = self.db.one("SELECT data FROM assignments WHERE id = ?", (aid,))
        return json.loads(row["data"]) if row else None

    def current_version(self, aid: str) -> Optional[str]:
        row = self.db.one("SELECT version FROM assignments WHERE id = ?", (aid,))
        return row["version"] if row else None

    def packed_for_phone(self, aid: str, pictures: bool = True) -> dict:
        """The phone's copy: answer key removed (unless practice), compact, version-stamped. The school's own drawings go
        along ("Px"), so a question never arrives without its picture; a course pack carries them once instead."""
        assignment = self.get_assignment(aid)
        view = asg.only_languages(asg.public_view(assignment), self.class_codes())
        course = self.get_course(assignment.get("course", ""))
        if course:  # titles travel with the assignment, so radio-only phones can show the course
            view["course_title"] = course["title"]
            unit = crs.unit_of(course, assignment.get("unit", ""))
            if unit:
                view["unit_title"] = unit["title"]
                view["unit_no"] = crs.unit_number(course, unit["id"])
        packed = asg.pack(view)
        packed["v"] = self.current_version(aid)
        packed.update(self.packed_pictures(assignment) if pictures else {})
        return packed

    def packed_pictures(self, assignment: dict) -> dict:
        own = self.own_pictures()
        found = {k: ownpics.on_radio(own[k]) for k in self.pictures_in(assignment)}
        return {"Px": found} if found else {}

    def course_pack(self) -> List[dict]:
        return [self.packed_for_phone(r["id"], pictures=False) for r in self.db.all("SELECT id FROM assignments ORDER BY created")]

    def course_list_for_phones(self) -> List[dict]:
        return [{k: v for k, v in c.items() if k != "source"} for c in self.courses()]

    def course_pack_files(self) -> Dict[str, bytes]:
        """Everything for a phone's "assignments" folder, by path."""
        def text(data) -> bytes:
            return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
        files = {f"assignments/{packed['i']}.json": text(packed) for packed in self.course_pack()}
        files["assignments/courses.json"] = text({"courses": self.course_list_for_phones()})
        files["assignments/language.json"] = text(self.language_bundle())
        if self.own_pictures():
            files["assignments/pictures.json"] = text({"pictures": self.own_pictures()})
        for key, path in self.used_voices().items():
            with open(path, "rb") as fh:
                files[f"assignments/voice/{os.path.basename(path)}"] = fh.read()
        files["assignments/README.txt"] = (
            "Copy this 'assignments' folder into the phone's MeshLearn data folder on the microSD card,\n"
            "next to the 'library' folder. The phone loads it when it starts or when you press\n"
            "'Reload from card' on its status page.\n").encode("utf-8")
        return files

    def course_pack_zip(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, data in self.course_pack_files().items():
                zf.writestr(path, data)
        return buf.getvalue()

    # -- phones ---------------------------------------------------------------

    def add_phone(self, node_id: str, name: str) -> None:
        self.db.execute(
            "INSERT INTO phones (node_id, name) VALUES (?, ?) ON CONFLICT(node_id) DO UPDATE SET name = excluded.name",
            (node_id, name or node_id),
        )

    def _holding(self, node_id: str, aid: str):
        return self.db.one("SELECT * FROM holdings WHERE node_id = ? AND aid = ?", (node_id, aid))

    def _set_holding(self, node_id: str, aid: str, **fields) -> None:
        self.db.execute("INSERT OR IGNORE INTO holdings (node_id, aid) VALUES (?, ?)", (node_id, aid))
        sets = ", ".join(f"{k} = ?" for k in fields) + ", updated = ?"
        self.db.execute(f"UPDATE holdings SET {sets} WHERE node_id = ? AND aid = ?",
                        (*fields.values(), time.time(), node_id, aid))

    def record_inventory(self, node_id: str, inventory: Dict[str, str], complete: bool) -> None:
        """Note which versions a phone holds. A complete inventory also clears what it lacks."""
        if complete:
            self.db.execute("UPDATE holdings SET version = '' WHERE node_id = ?", (node_id,))
        for aid, version in inventory.items():
            self._set_holding(node_id, str(aid)[:20], version=str(version)[:16])

    def set_open(self, aid: str, node_id: str, open_: bool = True) -> int:
        """Show or hide an assignment on a phone: one packet if the phone has the
        current version, otherwise the full assignment."""
        version = self.current_version(aid)
        if version is None:
            raise KeyError(aid)
        assignment = self.get_assignment(aid)
        held = self._holding(node_id, aid)
        flag = 1 if open_ else 0
        if held and held["version"] == version:
            msg_id = self.messenger.send(node_id, protocol.OPEN, {"a": aid, "v": version, "o": flag, "d": assignment.get("due", "")})
            action = "open" if open_ else "close"
        else:
            msg_id = self.messenger.send(node_id, protocol.ASSIGN, {"a": self.packed_for_phone(aid), "o": flag,
                                                                    "d": assignment.get("due", "")})
            action = "update" if held and held["version"] else "copy"
        self._set_holding(node_id, aid, version=version, open=flag, msg_id=msg_id, action=action)
        return msg_id

    def _heal(self, node_id: str) -> None:
        """Resend assignments that are open on a phone but missing or out of date there."""
        for row in self.db.all("SELECT h.aid, h.version, a.version AS current FROM holdings h "
                               "JOIN assignments a ON a.id = h.aid WHERE h.node_id = ? AND h.open = 1", (node_id,)):
            if row["version"] != row["current"]:
                self.set_open(row["aid"], node_id, True)

    # -- grading --------------------------------------------------------------

    def grade_submission(self, sub_id: str, manual: Optional[dict] = None) -> Optional[dict]:
        row = self.db.one("SELECT * FROM submissions WHERE sub_id = ?", (sub_id,))
        if row is None:
            raise KeyError(sub_id)
        assignment = self.get_assignment(row["assignment_id"], row["version"])
        merged = json.loads(row["manual"])
        merged.update(manual or {})
        grade = asg.grade(assignment, json.loads(row["answers"]), merged) if assignment else None
        self.db.execute(
            "UPDATE submissions SET grade = ?, manual = ? WHERE sub_id = ?",
            (json.dumps(grade) if grade else None, json.dumps(merged), sub_id),
        )
        return grade

    def set_note(self, sub_id: str, qid: str, note: str) -> None:
        """A short note back to the student about one answer. It travels with the marks."""
        row = self.db.one("SELECT notes FROM submissions WHERE sub_id = ?", (sub_id,))
        if row is None:
            return
        notes = json.loads(row["notes"] or "{}")
        notes[qid] = " ".join(note.split())[:MAX_NOTE_CHARS]
        notes = {k: v for k, v in notes.items() if v}
        self.db.execute("UPDATE submissions SET notes = ? WHERE sub_id = ?", (json.dumps(notes, ensure_ascii=False) if notes else None, sub_id))

    def _send_results(self, node_id: str, sub_ids: Iterable[str]) -> None:
        results = []
        ids = list(sub_ids)
        for sub_id in ids:
            row = self.db.one("SELECT * FROM submissions WHERE sub_id = ?", (sub_id,))
            if row and row["grade"]:
                g = json.loads(row["grade"])
                result = {"id": sub_id, "sc": g["score"], "mx": g["max"], "ok": 1 if g["complete"] else 0,
                          "q": {q["id"]: q["score"] for q in g["questions"]}}
                missed = self._missed_answers(row, g)
                if missed is not None:
                    self.db.execute("UPDATE submissions SET answers_sent = 1 WHERE sub_id = ?", (sub_id,))
                    if missed:
                        result["an"] = missed
                notes = json.loads(row["notes"] or "{}")
                if notes:
                    result["nt"] = notes
                results.append(result)
        if not results:
            return
        msg_id = self.messenger.send(node_id, protocol.RESULT, {"r": results})
        for sub_id in ids:
            self.db.execute("UPDATE submissions SET result_msg_id = ? WHERE sub_id = ?", (msg_id, sub_id))

    # -- right answers --------------------------------------------------------
    #
    # The key stays on the hub until the due date has passed (or the teacher sends it), so it can't reach
    # classmates who have not done the work. Then each student gets the answers to what they missed.

    def _answers_allowed(self, assignment: dict) -> bool:
        if assignment.get("mode") == "practice":
            return False   # the phone already has a practice key
        if self.db.one("SELECT 1 FROM released WHERE aid = ?", (assignment["id"],)):
            return True
        if self.setting("right_answers") != "due":
            return False
        try:
            return datetime.date.fromisoformat((assignment.get("due") or "")[:10]) < datetime.date.today()
        except ValueError:
            return False   # no due date: only when the teacher sends them

    def _missed_answers(self, row, grade: dict) -> Optional[dict]:
        """Right answers to the questions this student got wrong; None while they must stay on the hub."""
        current = self.get_assignment(row["assignment_id"])
        done = self.get_assignment(row["assignment_id"], row["version"])
        if not current or not done or not self._answers_allowed(current):
            return None
        scores = {q["id"]: q["score"] for q in grade["questions"]}
        return {q["id"]: asg.key_text(q)[:120] for q in done["questions"]
                if scores.get(q["id"]) is not None and scores[q["id"]] < q["points"] and asg.key_text(q)}

    def send_right_answers(self) -> int:
        """Send answers that have become allowed since the work was marked. Returns how many pieces of work."""
        by_phone: Dict[str, List[str]] = {}
        for row in self.db.all("SELECT * FROM submissions WHERE answers_sent = 0 AND grade IS NOT NULL AND source = 'radio'"):
            missed = self._missed_answers(row, json.loads(row["grade"]))
            if missed:
                by_phone.setdefault(row["phone"], []).append(row["sub_id"])
            elif missed is not None:
                self.db.execute("UPDATE submissions SET answers_sent = 1 WHERE sub_id = ?", (row["sub_id"],))
        for phone, ids in by_phone.items():
            for start in range(0, len(ids), 5):
                self._send_results(phone, ids[start:start + 5])
        return sum(len(ids) for ids in by_phone.values())

    def _answers_loop(self) -> None:
        wait = 60
        while True:
            time.sleep(wait)
            wait = 1800
            try:
                if hasattr(self.messenger, "send"):
                    self.send_right_answers()
            except Exception:
                log.exception("could not send right answers")

    def release_answers(self, req: Request) -> Response:
        aid = req.params["aid"]
        if self.current_version(aid) is None:
            return redirect("/assignments")
        self.db.execute("INSERT OR IGNORE INTO released (aid, at) VALUES (?, ?)", (aid, time.time()))
        n = self.send_right_answers()
        return redirect(f"/assignments/{aid}?" + urlencode({"msg": f"Sending the right answers for {n} piece{'' if n == 1 else 's'} of work."}))

    def answers_setting(self, req: Request) -> Response:
        self.set_setting("right_answers", "due" if req.form.get("right_answers") == "due" else "never")
        back = req.form.get("back", "")
        return redirect(back if back.startswith("/assignments/") else "/assignments")

    def _answers_card(self, assignment: dict) -> str:
        if assignment.get("mode") == "practice":
            return ""
        aid = esc(assignment["id"])
        auto = self.setting("right_answers") == "due"
        if self._answers_allowed(assignment):
            text, button = "Students can see the right answers to questions they missed.", ""
        else:
            text = ("Students see the right answers to what they missed the day after the due date." if auto and assignment.get("due") else
                    "This has no due date, so the right answers stay on this laptop until you send them." if auto else
                    "Right answers stay on this laptop.")
            button = (f"<form method='post' action='/assignments/{aid}/answers' style='display:inline'>"
                      "<button class='secondary'>Send right answers now</button></form>")
        return (f"<h2>Right answers</h2><div class='card'><p>{text} They travel with the marks, only for questions that student got wrong. "
                f"Anyone who does the work later could ask a classmate.</p>{button}"
                f"<form method='post' action='/settings/answers' style='display:inline'><input type='hidden' name='back' value='/assignments/{aid}'>"
                f"<button class='secondary' name='right_answers' value='{'never' if auto else 'due'}'>"
                f"{'Never send them by themselves (all lessons)' if auto else 'Send them after the due date (all lessons)'}</button></form></div>")

    # -- mesh -----------------------------------------------------------------

    def on_message(self, message: protocol.Message) -> None:
        p, src = message.payload, message.src
        if message.type == protocol.HELLO:
            self.db.execute(
                "INSERT INTO phones (node_id, name, last_seen, info) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(node_id) DO UPDATE SET last_seen = excluded.last_seen, info = excluded.info",
                (src, str(p.get("n") or src)[:60], time.time(), json.dumps({k: v for k, v in p.items() if k != "inv"})),
            )
            if isinstance(p.get("inv"), dict):
                self.record_inventory(src, p["inv"], complete=True)
                self._heal(src)
        elif message.type == protocol.NEED:
            aid = str(p.get("a", ""))[:20]
            if self.current_version(aid):
                held = self._holding(src, aid)
                self._set_holding(src, aid, version="")
                self.set_open(aid, src, bool(held["open"]) if held else True)
        elif message.type == protocol.SUBMIT:
            received = []
            for s in p.get("s") or []:
                sub_id = str(s.get("id", ""))[:40]
                if not sub_id:
                    continue
                duration = s.get("d")
                signals: dict = {}
                if isinstance(s.get("n"), int) and 0 < s["n"] < 1 << 200:
                    signals["n"] = s["n"]
                if isinstance(s.get("f"), dict):
                    signals["f"] = {str(k)[:20]: str(v)[:asg.MAX_FIRST_CHARS] for k, v in list(s["f"].items())[:100]}
                self.db.execute(
                    "INSERT OR IGNORE INTO submissions (sub_id, assignment_id, version, student, phone, answers, submitted_at, "
                    "received_at, duration, signals, lang) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (sub_id, str(s.get("a", ""))[:20], str(s.get("v", ""))[:16], self.student_name(str(s.get("u", "?"))[:60]), src,
                     json.dumps(s.get("x") or {}), s.get("t"), time.time(),
                     float(duration) if isinstance(duration, (int, float)) and duration > 0 else None,
                     json.dumps(signals) if signals else None,
                     s["l"] if isinstance(s.get("l"), str) and re.match(r"^[a-z]{3}$", s["l"]) else None),
                )
                grade = self.grade_submission(sub_id)
                if grade:
                    received.append(sub_id)
            self.db.execute("UPDATE phones SET last_seen = ? WHERE node_id = ?", (time.time(), src))
            self._send_results(src, received)  # one batched message back
        else:
            log.info("hub ignoring %s message from %s", message.type, src)

    # -- web ------------------------------------------------------------------

    # The page frame (_page) and the Today, Marking, Assignments and Phones pages are in hubui.py.

    def _routes(self) -> None:
        app = self.app
        app.add_route("POST", "/assignments/<aid>/answers", self.release_answers)
        app.add_route("POST", "/settings/answers", self.answers_setting)
        app.add_route("GET", "/api/pack", self.api_pack)
        app.add_route("GET", "/pack.zip", self.pack_zip)
        app.add_route("POST", "/assignments/<aid>/open", lambda r: self.assignment_action(r, True))
        app.add_route("POST", "/assignments/<aid>/close", lambda r: self.assignment_action(r, False))
        app.add_route("GET", "/assignments/<aid>/grades.csv", self.grades_csv)
        app.add_route("GET", "/submissions/<sid>", self.submission_detail)
        app.add_route("POST", "/submissions/<sid>/grade", self.submission_grade)
        app.add_route("POST", "/phones", self.phones_add)
        app.add_route("POST", "/mesh/retry", self.mesh_retry)
        app.add_route("GET", "/api/status", lambda r: json_response(self.messenger.status()))
        app.add_route("GET", "/library", self.library_page)
        app.add_route("POST", "/library/settings", self.library_settings)
        app.add_route("POST", "/library/add", self.library_add)
        app.add_route("POST", "/library/remove", self.library_remove)
        app.add_route("POST", "/library/suggest", self.library_suggest)
        app.add_route("POST", "/library/refresh", self.library_refresh)
        app.add_route("GET", "/library/download", self.library_download_page)
        app.add_route("POST", "/library/download", self.library_download_start)
        app.add_route("POST", "/library/download/cancel", self.library_download_cancel)
        app.add_route("GET", "/api/library/progress", self.library_progress)
        self.add_kolibri_routes(app)

    def api_pack(self, req: Request) -> Response:
        """Course pack for a phone syncing over Wi-Fi. ?node=<radio id> records what it now holds."""
        pack = self.course_pack()
        node = (req.query.get("node") or "").strip()[:40]
        if node:
            name = (req.query.get("name") or node).strip()[:60]
            self.db.execute("INSERT INTO phones (node_id, name, last_seen) VALUES (?, ?, ?) "
                            "ON CONFLICT(node_id) DO UPDATE SET last_seen = excluded.last_seen", (node, name, time.time()))
            self.record_inventory(node, {p["i"]: p["v"] for p in pack}, complete=False)
            self.db.execute("UPDATE phones SET copied = ? WHERE node_id = ?", (time.time(), node))
        return json_response({"assignments": pack, "courses": self.course_list_for_phones(), "language": self.language_bundle(),
                              "pictures": self.own_pictures(), "voices": self.voice_list()})

    def pack_zip(self, req: Request) -> Response:
        return Response(self.course_pack_zip(), content_type="application/zip",
                        headers={"Content-Disposition": "attachment; filename=meshlearn-course-pack.zip"})

    def assignment_action(self, req: Request, open_: bool) -> Response:
        aid = req.params["aid"]
        node_id = req.form.get("node_id", "")
        if self.current_version(aid) is None or not node_id:
            return redirect("/assignments")
        targets = [r["node_id"] for r in self.db.all("SELECT node_id FROM phones")] if node_id == "*" else [node_id]
        for target in targets:
            self.set_open(aid, target, open_)
        return redirect(f"/assignments/{aid}")

    def grades_csv(self, req: Request) -> Response:
        aid = req.params["aid"]
        assignment = self.get_assignment(aid)
        if assignment is None:
            return Response("not found", status=404, content_type="text/plain")
        out = io.StringIO()
        writer = csv.writer(out)
        qids = [q["id"] for q in assignment["questions"]]
        writer.writerow(["student", "phone", "submitted", "score", "max", "complete"] + qids + ["not_sure", "changed"])
        for s in self.db.all("SELECT * FROM submissions WHERE assignment_id = ? ORDER BY student", (aid,)):
            grade = json.loads(s["grade"]) if s["grade"] else {"score": "", "max": "", "complete": False, "questions": []}
            per_q = {q["id"]: q["score"] for q in grade["questions"]}
            writer.writerow([s["student"], s["phone"], _when(s["submitted_at"]), grade["score"], grade["max"],
                             grade["complete"]] + [per_q.get(q, "") for q in qids] + list(self._signal_cells(s)))
        return Response(out.getvalue(), content_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=grades-{aid}.csv"})

    def _signal_cells(self, s) -> Tuple[str, str]:
        a = self.get_assignment(s["assignment_id"], s["version"])
        unsure, first = asg.read_signals(a, json.loads(s["signals"] or "{}")) if a else (set(), {})
        return " ".join(sorted(unsure)), " ".join(sorted(first))

    def submission_detail(self, req: Request) -> Response:
        notice = "Saved. The result is on its way to the student's phone." if req.query.get("saved") else ""
        s = self.db.one("SELECT * FROM submissions WHERE sub_id = ?", (req.params["sid"],))
        if s is None:
            return Response(self._page("Not found", "<p>No such submission.</p>"), status=404)
        assignment = self.get_assignment(s["assignment_id"], s["version"])
        answers = json.loads(s["answers"])
        grade = json.loads(s["grade"]) if s["grade"] else None
        scores = {q["id"]: q["score"] for q in (grade or {}).get("questions", [])}
        manual = json.loads(s["manual"] or "{}")
        notes, own = json.loads(s["notes"] or "{}"), self.own_pictures()
        unsure, first = asg.read_signals(assignment, json.loads(s["signals"] or "{}")) if assignment else (set(), {})
        rows = ""
        for q in (assignment or {"questions": []})["questions"]:
            given = answers.get(q["id"], "")
            shown = asg.answer_text(q, given)
            score = scores.get(q["id"])
            current = "" if score is None else f"{score:g}"
            # Every mark can be changed: written answers always, and auto-marked ones when the
            # key did not foresee a good answer (or accepted a wrong one).
            box = (f"<input type='number' name='m_{esc(q['id'])}' min='0' max='{q['points']:g}' step='0.5' "
                   f"value='{current}' style='width:90px' aria-label='Points for {esc(q['prompt'][:40])}'> / {q['points']:g}")
            if q["type"] == "long":
                note = ""
            elif q["id"] in manual:
                note = "<div class='small warn'>Changed by you</div>"
            else:
                mark = "good" if score else "bad"
                note = f"<div class='small {mark}'>{'Right' if score else 'Wrong'}, marked by MeshLearn</div>"
            key = asg.key_text(q)
            answer_cell = esc(shown) or "<em class='muted'>(left blank)</em>"
            if key:
                answer_cell += f"<div class='small'>Right answer: {esc(key)}</div>"
            if score is not None and score < q["points"] and asg.wrong_note(q, given):
                answer_cell += f"<div class='small'><strong class='note'>{esc(asg.wrong_note(q, given))}</strong></div>"
            if q["id"] in unsure:
                answer_cell += "<div class='small tomark'>Not sure</div>"
            if q["id"] in first:
                was = " (right)" if (asg.grade_question(q, first[q["id"]]) or 0) >= q["points"] else ""
                answer_cell += f"<div class='small'>First answer: {esc(asg.answer_text(q, first[q['id']]))}{was}</div>"
            answer_cell += (f"<input type='text' name='n_{esc(q['id'])}' maxlength='{MAX_NOTE_CHARS}' value='{esc(notes.get(q['id'], ''))}' "
                            f"placeholder='Short note to the student (optional)' aria-label='Note about {esc(q['prompt'][:40])}' style='margin-top:6px'>")
            rows += f"<tr><td>{esc(q['prompt'])}{info_panel(q, own)}</td><td>{answer_cell}</td><td>{box}{note}</td></tr>"
        total = f"{grade['score']:g} / {grade['max']:g}" if grade else "—"
        phone = self._phone_names().get(s["phone"], s["phone"])
        body = (
            (f"<div class='notice'>{esc(notice)}</div>" if notice else "")
            + f"<p class='muted'>{esc(assignment['title'] if assignment else s['assignment_id'])} · from {esc(phone)} · "
            f"submitted {_when(s['submitted_at'])}</p>"
            f"<form method='post' action='/submissions/{esc(s['sub_id'])}/grade'>"
            f"<table class='plain'><tr><th>Question</th><th>Answer</th><th>Points</th></tr>{rows}</table>"
            f"<p><strong>Total: {total}</strong></p>"
            "<p class='small'>Change any mark, then save. The new result goes to the student's phone by radio.</p>"
            "<button>Save marks and send result to phone</button></form>"
            + (f"<p><a href='/assignments/{esc(assignment['id'])}'>Back to assignment</a></p>" if assignment else "")
        )
        return Response(self._page(f"Submission from {s['student']}", body, current="marking"))

    def submission_grade(self, req: Request) -> Response:
        sid = req.params["sid"]
        manual = {}
        for key, value in req.form.items():
            if key.startswith("n_"):
                self.set_note(sid, key[2:22], value)
            if key.startswith("m_") and value.strip():
                try:
                    manual[key[2:]] = float(value)
                except ValueError:
                    pass
        row = self.db.one("SELECT * FROM submissions WHERE sub_id = ?", (sid,))
        if row is None:
            return redirect("/")
        # An auto-marked question only becomes the teacher's mark when it was actually changed.
        assignment = self.get_assignment(row["assignment_id"], row["version"]) or {"questions": []}
        answers, before = json.loads(row["answers"]), json.loads(row["manual"] or "{}")
        for q in assignment["questions"]:
            if q["type"] != "long" and q["id"] in manual and q["id"] not in before:
                if manual[q["id"]] == asg.grade_question(q, answers.get(q["id"], "")):
                    del manual[q["id"]]
        try:
            self.grade_submission(sid, manual)
        except KeyError:
            return redirect("/")
        row = self.db.one("SELECT phone FROM submissions WHERE sub_id = ?", (sid,))
        self._send_results(row["phone"], [sid])
        return redirect(f"/submissions/{sid}?saved=1")

    def phones_add(self, req: Request) -> Response:
        node_id = req.form.get("node_id", "").strip()[:40]
        if node_id:
            self.add_phone(node_id, req.form.get("name", "").strip()[:60])
        return redirect("/phones")

    def mesh_retry(self, req: Request) -> Response:
        self.messenger.retry_now()
        return redirect("/phones")

    # -- card library ---------------------------------------------------------

    def setting(self, key: str) -> str:
        row = self.db.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else LIBRARY_DEFAULTS.get(key, "")

    def set_setting(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))

    def selection(self) -> List[dict]:
        return [json.loads(r["item"]) for r in self.db.all("SELECT item FROM library_selection ORDER BY added")]

    def select_item(self, item: dict) -> None:
        name = item["key"].split("|")[0]
        # One version (flavour) per collection: picking another replaces it.
        for row in self.db.all("SELECT key FROM library_selection"):
            if row["key"].split("|")[0] == name:
                self.db.execute("DELETE FROM library_selection WHERE key = ?", (row["key"],))
        self.db.execute("INSERT INTO library_selection (key, item, added) VALUES (?, ?, ?)",
                        (item["key"], json.dumps(item), time.time()))

    def student_ages(self) -> List[str]:
        valid = {g[0] for g in library.AGE_GROUPS}
        return [a for a in self.setting("ages").split(",") if a in valid] or [g[0] for g in library.AGE_GROUPS]

    def _ages_text(self, ages: List[str]) -> str:
        if len(ages) == len(library.AGE_GROUPS):
            return "all ages"
        lo, hi = library.age_span(ages)
        return "teachers and adults" if lo >= 19 else (f"ages {lo}+" if hi >= 99 else f"ages {lo}–{hi}")

    def library_budget(self) -> int:
        try:
            return library.usable_bytes(float(self.setting("card_gb")), float(self.setting("reserve_gb")))
        except ValueError:
            return 0

    @staticmethod
    def _local(req: Request) -> bool:
        """Downloads write to this laptop's disk, so only its own browser may start them."""
        return req.client in ("127.0.0.1", "::1")

    @staticmethod
    def _gb(num: int) -> str:
        return f"{num / 1e9:.2f} GB" if num >= 1e8 else f"{num / 1e6:.0f} MB" if num >= 1e6 else f"{max(num, 1) / 1e3:.0f} KB"

    def _back(self, req: Request) -> Response:
        back = req.form.get("back", "")
        return redirect(back if back.startswith("/library") else "/library")

    def library_page(self, req: Request) -> Response:
        lang = req.query.get("lang", self.setting("lang"))
        level = req.query.get("level", "2")
        kind = req.query.get("kind", "")
        query = req.query.get("q", "")
        agefit = req.query.get("agefit", "fit")
        ages = self.student_ages()
        try:
            page_no = max(1, int(req.query.get("page", "1")))
        except ValueError:
            page_no = 1
        here = "/library?" + urlencode({k: v for k, v in {"lang": lang, "level": level, "kind": kind, "q": query,
                                                           "agefit": agefit, "page": page_no}.items() if v not in ("", None)})
        notice = req.query.get("msg", "")
        selected = {i["key"]: i for i in self.selection()}
        budget = self.library_budget()
        used = cat.total_bytes(list(selected.values()))

        # -- plan panel
        pct = min(100, round(100 * used / budget)) if budget else 100
        over = used > budget
        bar = (f"<div style='height:14px;border-radius:7px;background:var(--line);overflow:hidden' role='img' "
               f"aria-label='{pct}% of the card used'><div style='height:100%;width:{pct}%;background:"
               f"{'var(--bad)' if over else 'var(--accent)'}'></div></div>")
        def age_note(i):
            if not i.get("ages"):
                return ""
            note = cat.age_label(*i["ages"])
            if not library.ages_overlap(tuple(i["ages"]), ages):
                return f" · <span class='warn'>{esc(note)}: outside the students' ages</span>"
            return f" · {esc(note)}"

        rows = "".join(
            f"<tr><td>{esc(i['title'])}<div class='muted'>{esc(i['label'])}{age_note(i)}</div></td><td>{self._gb(i['size'])}</td>"
            f"<td><form method='post' action='/library/remove'><input type='hidden' name='key' value='{esc(i['key'])}'>"
            f"<input type='hidden' name='back' value='{esc(here)}'><button class='secondary'>Remove</button></form></td></tr>"
            for i in selected.values()
        )
        languages = self.catalog.languages()
        current = self.setting("lang")
        if current not in {code for code, _ in languages}:
            languages.insert(0, (current, 0))
        lang_options = "".join(
            f"<option value='{esc(code)}'{' selected' if code == current else ''}>"
            f"{esc(cat.language_name(code))}{f' ({n} collections)' if n else ''}</option>" for code, n in languages
        )
        short = {v: k for k, v in cat.SHORT_TO_ISO.items()}.get(self.setting("lang"))
        age_boxes = "".join(
            f"<label class='option' style='display:inline-flex;margin-right:16px'><input type='checkbox' name='ages' value='{key}'"
            f"{' checked' if key in ages else ''}> {esc(text)}</label>" for key, text, _, _ in library.AGE_GROUPS)
        plan = (
            "<div class='card'>"
            "<form method='post' action='/library/settings' class='grid'>"
            f"<div><label for='card_gb'>Card size (GB on the label)</label><input type='number' id='card_gb' name='card_gb' min='1' step='1' value='{esc(self.setting('card_gb'))}'></div>"
            f"<div><label for='reserve_gb'>Keep free for assignments (GB)</label><input type='number' id='reserve_gb' name='reserve_gb' min='0' step='0.5' value='{esc(self.setting('reserve_gb'))}'></div>"
            f"<div><label for='lang_set'>School language (<a href='/language'>more</a>)</label>"
            f"<select id='lang_set' name='lang'>{lang_options}</select></div>"
            f"<fieldset style='grid-column:1/-1;margin:0'><legend>Students' ages</legend>{age_boxes}</fieldset>"
            "<div><input type='hidden' name='ages_form' value='1'><button class='secondary'>Save</button></div></form>"
            f"<p><strong>{self._gb(used)}</strong> chosen of <strong>{self._gb(budget)}</strong> available on the card"
            + (f" · <span class='bad'>{self._gb(used - budget)} too much: remove something or use a bigger card</span>" if over else "")
            + f"</p>{bar}"
            + (f"<table>{rows}</table>" if rows else "<p class='muted'>Nothing chosen yet. Add collections below, or let MeshLearn suggest a starter set.</p>")
            + "<p>"
            + (f"<form method='post' action='/library/suggest' style='display:inline'><button class='secondary'>"
               f"{'Replace with' if rows else 'Suggest'} a starter set for {esc(self._ages_text(ages))}</button></form>" if short else
               "<span class='muted'>Starter sets exist for English, Portuguese and German; pick collections by hand for other languages.</span>")
            + (" <a class='button' href='/library/download'>Copy to memory card…</a>" if rows else "")
            + "</p></div>"
        )

        # -- catalog browser
        if not self.catalog.entries:
            browse = ("<div class='card'><p>The list of available collections has not been downloaded yet. "
                      "Connect this laptop to the internet once, then:</p>"
                      "<form method='post' action='/library/refresh'><button>Download the list (about 5 MB)</button></form></div>")
        else:
            try:
                min_level = int(level)
            except ValueError:
                min_level = 2
            groups = self.catalog.groups(lang, min_level, query, kind, ages=ages, only_fit=agefit != "any")
            per_page = 40
            pages = max(1, (len(groups) + per_page - 1) // per_page)
            page_no = min(page_no, pages)
            shown = groups[(page_no - 1) * per_page: page_no * per_page]
            lang_filter = "<option value=''>All languages</option>" + "".join(
                f"<option value='{esc(code)}'{' selected' if code == lang else ''}>{esc(cat.language_name(code))}</option>"
                for code, n in self.catalog.languages() if n)
            level_filter = "".join(f"<option value='{v}'{' selected' if v == level else ''}>{esc(t)}</option>" for v, t in LEVEL_FILTERS)
            age_filter = "".join(f"<option value='{v}'{' selected' if v == agefit else ''}>{esc(t)}</option>" for v, t in AGE_FILTERS)
            kind_filter = "<option value=''>All kinds</option>" + "".join(
                f"<option{' selected' if k == kind else ''}>{esc(k)}</option>" for k in self.catalog.labels(lang, min_level) + ["Tools"])
            school = [self.school_language()] + self.other_languages()
            by_language = ("<p>Books and courses in: " + " ".join(
                f"<a class='button{'' if c == lang else ' secondary'}' href='/library?{esc(urlencode({'lang': c}))}'>"
                f"{esc(cat.language_name(c))}</a>" for c in school) + "</p>") if len(school) > 1 else ""
            filters = (
                by_language + "<form method='get' action='/library' class='card grid'>"
                f"<div><label for='q'>Search</label><input type='text' id='q' name='q' value='{esc(query)}' placeholder='e.g. math, biology, chemistry'></div>"
                f"<div><label for='lang'>Language</label><select id='lang' name='lang'>{lang_filter}</select></div>"
                f"<div><label for='level'>Show</label><select id='level' name='level'>{level_filter}</select></div>"
                f"<div><label for='kind'>Kind</label><select id='kind' name='kind'>{kind_filter}</select></div>"
                f"<div><label for='agefit'>Ages ({esc(self._ages_text(ages))})</label><select id='agefit' name='agefit'>{age_filter}</select></div>"
                "<div><label>&nbsp;</label><button>Filter</button></div></form>"
            )
            items = ""
            g = cat.gutenberg_choice()["item"]
            g_fits = library.ages_overlap(tuple(g["ages"]), ages)
            if (lang in ("", "eng") and not query and kind in ("", "Books and source texts") and min_level <= 2
                    and page_no == 1 and (g_fits or agefit == "any")):
                items += self._library_row(g["title"], "Ten well-known novels as plain text that opens in any browser.",
                                           g["label"], 2, "eng", [(g, "Add", g["key"] in selected)], here, show_lang=not lang,
                                           ages=g["ages"], age_est=False, fits=g_fits)
            gg = cat.geogebra_item()
            gg_fits = library.ages_overlap(tuple(gg["ages"]), ages)
            if (kind in ("", "Tools") and page_no == 1 and (gg_fits or agefit == "any")
                    and (not query or query.lower() in "geogebra geometry graph algebra calculator maths math tools")):
                items += self._library_row(gg["title"], "Graphing, geometry, 3D, scientific calculator and more, for maths. Free for non-commercial "
                                           "use only: not on cards that are sold or part of a paid service.", gg["label"], 3, "mul",
                                           [(gg, "Add", gg["key"] in selected)], here, show_lang=False, ages=gg["ages"], fits=gg_fits)
            for g in shown:
                if g["category"] == "kolibri":
                    items += self._library_row(g["title"], g["summary"], g["label"], g["level"], g["lang"], [], here,
                                               show_lang=not lang, ages=g["ages"], age_est=g["age_est"], fits=g["fits"],
                                               channel=g["options"][0], selected=selected)
                    continue
                options = []
                for o in g["options"]:
                    item = cat.selection_item(o)
                    label = (o["flavour"] or "Add").replace("mini", "mini: article intros").replace(
                        "nopic", "nopic: text only").replace("maxi", "maxi: with images")
                    options.append((item, label, item["key"] in selected))
                items += self._library_row(g["title"], g["summary"], g["label"], g["level"], g["lang"], options, here,
                                           show_lang=not lang, ages=g["ages"], age_est=g["age_est"], fits=g["fits"])
            nav = ""
            if pages > 1:
                def link(p, text):
                    params = {k: v for k, v in {"lang": lang, "level": level, "kind": kind, "q": query, "agefit": agefit}.items() if v}
                    params["page"] = p
                    return f"<a class='button secondary' href='/library?{esc(urlencode(params))}'>{text}</a>"
                nav = (f"<p>{link(page_no - 1, '← Previous') if page_no > 1 else ''} "
                       f"<span class='muted'>Page {page_no} of {pages}</span> {link(page_no + 1, 'Next →') if page_no < pages else ''}</p>")
            when = time.strftime("%b %d, %Y", time.localtime(self.catalog.fetched_at)) if self.catalog.fetched_at else "?"
            no_kolibri = ("" if any(e["category"] == "kolibri" for e in self.catalog.entries) else
                          "<p class='muted'>Kolibri courses, such as Khan Academy in many languages, can be listed too: "
                          "press <strong>Update the list</strong>.</p>")
            browse = (
                filters + no_kolibri
                + f"<p class='muted'>{len(groups)} collection{'' if len(groups) == 1 else 's'} match · list from {when} · "
                "<form method='post' action='/library/refresh' style='display:inline'><button class='secondary'>Update the list</button></form></p>"
                + (f"<table><tr><th>Collection</th><th>Add to card</th></tr>{items}</table>" if items else "<p class='muted'>Nothing matches.</p>")
                + nav
            )
        body = (
            (f"<p class='good' role='status'>{esc(notice)}</p>" if notice else "")
            + "<p class='muted'>Choose what goes on each phone's memory card, from Kiwix and Kolibri. Material that suits the students' ages and "
            "learning material are listed first. Age ranges come from each collection's name and description. "
            "Everything is free and works without internet once it is on the card.</p>"
            + "<h2>This card</h2>" + plan + "<h2>Available collections</h2>" + browse
        )
        return Response(self._page("Card library", body))

    def _library_row(self, title, summary, label, level, lang, options, here, show_lang,
                     ages=None, age_est=False, fits=True, channel=None, selected=None) -> str:
        badge_class = {3: "good", 2: "", 1: "muted", 0: "bad"}.get(level, "")
        age_badge = ""
        if ages:
            age_badge = (f" <span class='pill{'' if fits else ' warn'}'>{esc(cat.age_label(ages[0], ages[1], age_est))}"
                         f"{'' if fits else ' · not the students’ ages'}</span>")
        codes = [c for c in lang.split(",") if c]
        if len(codes) > 3:
            langs = f"multilingual ({len(codes)} languages)"
        elif show_lang or len(codes) > 1:
            langs = ", ".join(cat.LANGUAGES.get(c, c) for c in codes)
        else:
            langs = ""
        buttons = ""
        if channel:
            n = sum(1 for key in selected if key.startswith(channel["name"] + ":"))
            buttons = (f"<a class='button secondary' href='/library/kolibri?id={esc(channel['channel'])}'>Choose topics…</a>"
                       + (f" <span class='good'>✓ {n} chosen</span>" if n else "")
                       + f"<div class='muted'>from Kolibri · {self._gb(channel['size'])} for everything</div>")
        for item, text, chosen in options:
            if chosen:
                buttons += (f"<form method='post' action='/library/remove' style='display:inline'>"
                            f"<input type='hidden' name='key' value='{esc(item['key'])}'><input type='hidden' name='back' value='{esc(here)}'>"
                            f"<button aria-pressed='true'>✓ {esc(text)} · {self._gb(item['size'])}</button></form>")
            else:
                buttons += (f"<form method='post' action='/library/add' style='display:inline'>"
                            f"<input type='hidden' name='key' value='{esc(item['key'])}'><input type='hidden' name='back' value='{esc(here)}'>"
                            f"<button class='secondary'>{esc(text)} · {self._gb(item['size'])}</button></form>")
        return (f"<tr><td><strong>{esc(title)}</strong> <span class='pill {badge_class}'>{esc(label)}</span>{age_badge}"
                f"{' <span class=muted>· ' + esc(langs) + '</span>' if langs else ''}"
                f"<div class='muted'>{esc(summary[:160])}</div></td><td>{buttons}</td></tr>")

    def library_settings(self, req: Request) -> Response:
        for key in ("card_gb", "reserve_gb"):
            try:
                value = float(req.form.get(key, ""))
                if value >= 0:
                    self.set_setting(key, f"{value:g}")
            except ValueError:
                pass
        if req.form.get("ages_form"):
            valid = [g[0] for g in library.AGE_GROUPS]
            chosen = [a for a in req.form_list("ages") if a in valid]
            self.set_setting("ages", ",".join(chosen or valid))
        lang = req.form.get("lang", "").strip()[:12]
        if lang:
            self.set_setting("lang", lang)
            return redirect("/library?" + urlencode({"lang": lang}))
        return redirect("/library")

    def library_add(self, req: Request) -> Response:
        key = req.form.get("key", "")
        if key == "gutenberg|classics":
            self.select_item(cat.gutenberg_choice()["item"])
        elif key == "geogebra|apps":
            self.select_item(cat.geogebra_item())
        else:
            name, _, flavour = key.partition("|")
            entry = self.catalog.find(name, flavour)
            if entry:
                self.select_item(cat.selection_item(entry))
        return self._back(req)

    def library_remove(self, req: Request) -> Response:
        self.db.execute("DELETE FROM library_selection WHERE key = ?", (req.form.get("key", ""),))
        return self._back(req)

    def library_suggest(self, req: Request) -> Response:
        short = {v: k for k, v in cat.SHORT_TO_ISO.items()}.get(self.setting("lang"))
        if not self.catalog.entries:
            return redirect("/library?" + urlencode(
                {"msg": "First download the list of collections (button below), then ask for a starter set."}))
        if not short:
            return redirect("/library?" + urlencode(
                {"msg": "Starter sets exist for English, Portuguese and German. Pick collections by hand for this language."}))
        ages = self.student_ages()
        picks = self.catalog.suggest(short, self.library_budget(), ages)
        self.db.execute("DELETE FROM library_selection")
        for item in picks:
            self.select_item(item)
        return redirect("/library?" + urlencode({"msg": f"Suggested {len(picks)} collections for {self._ages_text(ages)} "
                                                        "that fit this card. Change anything you like."}))

    def library_refresh(self, req: Request) -> Response:
        if not self._local(req):
            return redirect("/library?" + urlencode({"msg": "Update the list from the hub laptop itself."}))
        try:
            count = self.catalog.refresh()
            courses = sum(1 for e in self.catalog.entries if e["category"] == "kolibri")
            msg = f"Downloaded the list: {count - courses} Kiwix collections and {courses} Kolibri channels available."
        except Exception as exc:
            msg = f"Could not download the list ({exc}). Is this laptop online?"
        return redirect("/library?" + urlencode({"msg": msg}))

    def library_download_page(self, req: Request) -> Response:
        job = self.download_job
        selected = self.selection()
        total = cat.total_bytes(selected)
        if job and job.is_alive():
            p = job.progress()
            pct = round(100 * p["done"] / p["total"]) if p["total"] else 0
            body = (
                "<meta http-equiv='refresh' content='3'>"
                f"<div class='card'><p><strong>Copying to {esc(p['library_dir'])}</strong></p>"
                f"<p>{self._gb(p['done'])} of {self._gb(p['total'])} ({pct}%)</p>"
                f"<div style='height:14px;border-radius:7px;background:var(--line);overflow:hidden' role='progressbar' "
                f"aria-valuenow='{pct}' aria-valuemin='0' aria-valuemax='100'><div style='height:100%;width:{pct}%;background:var(--accent)'></div></div>"
                f"<p class='muted'>Now: {esc(p['current'])}</p>"
                f"<p>Finished: {esc(', '.join(p['finished'])) or 'none yet'}</p>"
                "<form method='post' action='/library/download/cancel'><button class='secondary'>Stop (you can resume later)</button></form></div>"
            )
            return Response(self._page("Copying to the card", body))
        status = ""
        if job:
            p = job.progress()
            if p["state"] == "done":
                status = (f"<p class='good' role='status'>Done: copied {len(p['finished'])} collections to {esc(p['library_dir'])}. "
                          "Put the card back in the phone; its Library page lists everything.</p>")
            elif p["state"] == "cancelled":
                status = "<p class='warn' role='status'>Stopped. Start again to resume where it left off.</p>"
            elif p["state"] == "failed":
                status = f"<p class='bad' role='alert'>Copy failed: {esc(p['error'])}. Start again to resume.</p>"
        notice = req.query.get("msg", "")
        dest = self.setting("dest")
        free = cat.free_bytes(dest) if dest else None
        space = f" · {self._gb(free)} free there" if free is not None else ""
        body = (
            status + (f"<p class='bad' role='alert'>{esc(notice)}</p>" if notice else "")
            + f"<p>{len(selected)} collections, <strong>{self._gb(total)}</strong> in total. "
            "<a href='/library'>Change the selection</a></p>"
            "<div class='card'><ol>"
            "<li>Put the phone's microSD card in this laptop's card reader.</li>"
            "<li>Enter the phone's MeshLearn folder on the card. With the MeshLearn app this is "
            "<code>&lt;card&gt;/Android/data/org.meshlearn.phone/files/meshlearn</code> (with Termux, "
            "<code>com.termux</code> in place of <code>org.meshlearn.phone</code>).</li>"
            "<li>Start. Large collections take a while; you can stop and resume later.</li></ol>"
            "<form method='post' action='/library/download'>"
            f"<label for='dest'>MeshLearn folder on the card{space}</label>"
            f"<input type='text' id='dest' name='dest' value='{esc(dest)}' placeholder='/Volumes/SDCARD/Android/data/org.meshlearn.phone/files/meshlearn' required>"
            "<button>Start copying</button></form></div>"
            "<p class='muted'>Needs internet on this laptop. Files that are already on the card are skipped.</p>"
        )
        return Response(self._page("Copy to memory card", body))

    def library_download_start(self, req: Request) -> Response:
        if not self._local(req):
            return redirect("/library/download?" + urlencode({"msg": "Start the copy from the hub laptop itself."}))
        if self.download_job and self.download_job.is_alive():
            return redirect("/library/download")
        dest = os.path.expanduser(req.form.get("dest", "").strip())
        if not dest or not os.path.isdir(os.path.dirname(dest.rstrip(os.sep)) or dest):
            return redirect("/library/download?" + urlencode({"msg": f"Folder not found: {dest}. Is the card inserted?"}))
        items = self.selection()
        os.makedirs(dest, exist_ok=True)
        needed = cat.remaining_bytes(items, dest)
        free = cat.free_bytes(dest) or 0
        if needed > free:
            return redirect("/library/download?" + urlencode(
                {"msg": f"Not enough space: need {self._gb(needed)}, the card has {self._gb(free)} free."}))
        self.set_setting("dest", dest)
        self.download_job = cat.DownloadJob(items, dest)
        self.download_job.start()
        return redirect("/library/download")

    def library_download_cancel(self, req: Request) -> Response:
        if self.download_job and self._local(req):
            self.download_job.cancel()
            self.download_job.join(timeout=10)
        return redirect("/library/download")

    def library_progress(self, req: Request) -> Response:
        return json_response(self.download_job.progress() if self.download_job else {"state": "idle"})
