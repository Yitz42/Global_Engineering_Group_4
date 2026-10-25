"""Teacher hub: build assignments, open them on student phones, grade what comes back.

Phones keep their own copy of every assignment (loaded over USB or school
Wi-Fi as a "course pack"), so the radio normally carries only small messages:
open/close an assignment (1 packet), student answers, and grades. A full
assignment travels by radio only when it is new or changed since the phone's
copy, and phones say which versions they hold whenever they check in.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
import zipfile
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlencode

from . import assignments as asg
from . import catalog as cat
from . import courses as crs
from . import library
from . import protocol
from .db import Database
from .hubpages import HubPages
from .messenger import Messenger
from .web import App, Request, Response, esc, json_response, page, redirect

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
"""

LIBRARY_DEFAULTS = {"card_gb": "32", "reserve_gb": "2", "lang": "eng", "dest": "", "ages": ",".join(library.DEFAULT_AGES)}
AGE_FILTERS = [("fit", "Suited to the students' ages"), ("any", "Any age (suited ones first)")]
LEVEL_FILTERS = [("3", "Core learning only"), ("2", "Education and reference"), ("0", "Everything")]

ACTION_LABELS = {"open": "opening", "close": "closing", "update": "sending update", "copy": "sending full copy"}


def _when(ts: Optional[float]) -> str:
    return time.strftime("%b %d, %H:%M", time.localtime(ts)) if ts else "never"


class Hub(HubPages):
    def __init__(self, db_path: str, messenger_factory):
        self.db = Database(db_path, SCHEMA)
        self.messenger: Messenger = messenger_factory(self.on_message)
        self.catalog = cat.Catalog(os.path.join(os.path.dirname(os.path.abspath(db_path)), "catalog.json"))
        self.download_job: Optional[cat.DownloadJob] = None
        self._migrate()
        self.load_course_file()
        self.app = App("hub", max_body=32 * 1024 * 1024)  # learning-data files can be large
        self._routes()
        self._page_routes()

    def _migrate(self) -> None:
        """Columns added after the first release; older hub databases get them here."""
        for sql in ("ALTER TABLE submissions ADD COLUMN duration REAL",
                    "ALTER TABLE submissions ADD COLUMN source TEXT NOT NULL DEFAULT 'radio'"):
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

    def packed_for_phone(self, aid: str) -> dict:
        """The phone's copy: answer key removed (unless practice), compact, version-stamped."""
        assignment = self.get_assignment(aid)
        view = asg.public_view(assignment)
        course = self.get_course(assignment.get("course", ""))
        if course:  # titles travel with the assignment, so radio-only phones can show the course
            view["course_title"] = course["title"]
            unit = crs.unit_of(course, assignment.get("unit", ""))
            if unit:
                view["unit_title"] = unit["title"]
                view["unit_no"] = crs.unit_number(course, unit["id"])
        packed = asg.pack(view)
        packed["v"] = self.current_version(aid)
        return packed

    def course_pack(self) -> List[dict]:
        return [self.packed_for_phone(r["id"]) for r in self.db.all("SELECT id FROM assignments ORDER BY created")]

    def course_list_for_phones(self) -> List[dict]:
        return [{k: v for k, v in c.items() if k != "source"} for c in self.courses()]

    def course_pack_zip(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for packed in self.course_pack():
                zf.writestr(f"assignments/{packed['i']}.json", json.dumps(packed, ensure_ascii=False, indent=1))
            zf.writestr("assignments/courses.json", json.dumps({"courses": self.course_list_for_phones()},
                                                               ensure_ascii=False, indent=1))
            zf.writestr("assignments/README.txt",
                        "Copy this 'assignments' folder into the phone's MeshLearn data folder on the microSD card,\n"
                        "next to the 'library' folder. The phone loads it when it starts or when you press\n"
                        "'Reload from card' on its status page.\n")
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

    def _send_results(self, node_id: str, sub_ids: Iterable[str]) -> None:
        results = []
        ids = list(sub_ids)
        for sub_id in ids:
            row = self.db.one("SELECT grade FROM submissions WHERE sub_id = ?", (sub_id,))
            if row and row["grade"]:
                g = json.loads(row["grade"])
                results.append({"id": sub_id, "sc": g["score"], "mx": g["max"], "ok": 1 if g["complete"] else 0,
                                "q": {q["id"]: q["score"] for q in g["questions"]}})
        if not results:
            return
        msg_id = self.messenger.send(node_id, protocol.RESULT, {"r": results})
        for sub_id in ids:
            self.db.execute("UPDATE submissions SET result_msg_id = ? WHERE sub_id = ?", (msg_id, sub_id))

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
                self.db.execute(
                    "INSERT OR IGNORE INTO submissions (sub_id, assignment_id, version, student, phone, answers, submitted_at, "
                    "received_at, duration) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (sub_id, str(s.get("a", ""))[:20], str(s.get("v", ""))[:16], str(s.get("u", "?"))[:60], src,
                     json.dumps(s.get("x") or {}), s.get("t"), time.time(),
                     float(duration) if isinstance(duration, (int, float)) and duration > 0 else None),
                )
                grade = self.grade_submission(sub_id)
                if grade:
                    received.append(sub_id)
            self.db.execute("UPDATE phones SET last_seen = ? WHERE node_id = ?", (time.time(), src))
            self._send_results(src, received)  # one batched message back
        else:
            log.info("hub ignoring %s message from %s", message.type, src)

    # -- web ------------------------------------------------------------------

    def _nav(self) -> str:
        return ("<a href='/'>Home</a><a href='/courses'>Courses</a><a href='/comprehension'>Comprehension</a>"
                "<a href='/assignments/new'>New assignment</a><a href='/phones'>Phones</a><a href='/library'>Card library</a>")

    def _page(self, title: str, body: str) -> str:
        return page(title, body, self._nav(), site="MeshLearn Teacher Hub", wide=True)  # used on a laptop

    def _routes(self) -> None:
        app = self.app
        app.add_route("GET", "/", self.dashboard)
        app.add_route("GET", "/assignments/new", self.new_assignment)
        app.add_route("POST", "/api/assignments", self.api_create)
        app.add_route("GET", "/api/assignments/<aid>", self.api_get)
        app.add_route("GET", "/api/pack", self.api_pack)
        app.add_route("GET", "/pack.zip", self.pack_zip)
        app.add_route("GET", "/assignments/<aid>", self.assignment_detail)
        app.add_route("GET", "/assignments/<aid>/edit", self.edit_assignment)
        app.add_route("POST", "/assignments/<aid>/open", lambda r: self.assignment_action(r, True))
        app.add_route("POST", "/assignments/<aid>/close", lambda r: self.assignment_action(r, False))
        app.add_route("GET", "/assignments/<aid>/grades.csv", self.grades_csv)
        app.add_route("GET", "/submissions/<sid>", self.submission_detail)
        app.add_route("POST", "/submissions/<sid>/grade", self.submission_grade)
        app.add_route("GET", "/phones", self.phones)
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

    def dashboard(self, req: Request) -> Response:
        rows = self.db.all(
            "SELECT a.id, a.data, (SELECT COUNT(*) FROM submissions s WHERE s.assignment_id = a.id) AS subs, "
            "(SELECT COUNT(*) FROM submissions s WHERE s.assignment_id = a.id AND (s.grade IS NULL OR s.grade LIKE '%\"complete\": false%')) AS to_grade "
            "FROM assignments a ORDER BY a.created DESC"
        )
        phones = self.db.all("SELECT * FROM phones ORDER BY name")
        status = self.messenger.status()
        stats = (
            "<div class='grid'>"
            f"<div class='card'><div class='stat'>{len(rows)}</div>assignments</div>"
            f"<div class='card'><div class='stat'>{sum(r['subs'] for r in rows)}</div>submissions received</div>"
            f"<div class='card'><div class='stat'>{sum(r['to_grade'] for r in rows)}</div>need your grading</div>"
            f"<div class='card'><div class='stat'>{status['pending']}</div>messages waiting to send"
            f"<div class='muted'>{status['frames_sent']} radio packets sent since start</div>"
            "<form method='post' action='/mesh/retry'><button class='secondary'>Retry now</button></form></div>"
            "</div>"
        )
        if rows:
            items = "".join(
                f"<tr><td><a href='/assignments/{esc(r['id'])}'>{esc(json.loads(r['data'])['title'])}</a></td>"
                f"<td>{esc(self._placement(json.loads(r['data'])) or json.loads(r['data']).get('subject') or '')}</td>"
                f"<td>{r['subs']}</td><td>{r['to_grade']}</td></tr>"
                for r in rows
            )
            table = f"<table><tr><th>Assignment</th><th>Course and unit</th><th>Submissions</th><th>To grade</th></tr>{items}</table>"
        else:
            table = "<p class='muted'>No assignments yet. <a href='/assignments/new'>Create the first one</a>.</p>"
        phone_list = "".join(
            f"<li>{esc(p['name'])} <span class='muted'>({esc(p['node_id'])}) · last heard {_when(p['last_seen'])}</span></li>"
            for p in phones
        ) or "<li class='muted'>No phones yet. <a href='/phones'>Add one</a>.</li>"
        pack = (
            "<h2>Load assignments onto phones (no radio needed)</h2><div class='card'>"
            "<p>Give phones a copy of every assignment in advance, so the radio only has to carry answers, grades, "
            "and one-packet open/close messages.</p>"
            "<p><strong>Over school Wi-Fi:</strong> put the phone on the same network as this laptop, open the phone's "
            "status page, and enter this hub's address.</p>"
            "<p><strong>By memory card:</strong> download the course pack and unzip it into the phone's MeshLearn folder.</p>"
            "<a class='button secondary' href='/pack.zip'>Download course pack (zip)</a></div>"
        )
        courses = "<h2>Courses</h2>" + self._course_cards(self.comprehension_records())
        body = (f"{stats}{courses}<h2>Latest assignments</h2>{table}{pack}<h2>Student phones</h2><ul>{phone_list}</ul>"
                + self.learning_data_panel())
        return Response(self._page("Home", body))

    def _builder(self, edit_id: str, preset_course: str = "", preset_unit: str = "") -> str:
        courses = [{"id": c["id"], "title": c["title"], "units": c["units"]} for c in self.courses() if not c.get("comingSoon")]
        return (BUILDER_HTML.replace("__EDIT_ID__", esc(edit_id))
                .replace("__COURSES__", json.dumps(courses).replace("</", "<\\/"))
                .replace("__PRESET__", json.dumps({"course": preset_course, "unit": preset_unit}).replace("</", "<\\/")))

    def new_assignment(self, req: Request) -> Response:
        return Response(self._page("New assignment", self._builder("", req.query.get("course", ""), req.query.get("unit", ""))))

    def edit_assignment(self, req: Request) -> Response:
        aid = req.params["aid"]
        if self.get_assignment(aid) is None:
            return redirect("/")
        note = ("<p class='warn'>Changing questions creates a new version. Phones that already hold this assignment "
                "will need the full copy again (sent automatically to phones where it is open). Changing only the "
                "due date costs one packet per phone.</p>")
        return Response(self._page("Edit assignment", note + self._builder(aid)))

    def api_create(self, req: Request) -> Response:
        try:
            data = req.json()
            old_version = self.current_version(str(data.get("id") or "")) if isinstance(data, dict) else None
            assignment = self.create_assignment(data)
        except (asg.AssignmentError, ValueError, AttributeError) as exc:
            return json_response({"error": str(exc)}, status=400)
        aid = assignment["id"]
        if old_version is not None:
            # Edited: push changes to phones where it is open (full copy if questions changed, 1 packet if only the date).
            for row in self.db.all("SELECT node_id FROM holdings WHERE aid = ? AND open = 1", (aid,)):
                self.set_open(aid, row["node_id"], True)
        return json_response({"id": aid, "frames": protocol.frame_count(protocol.ASSIGN, {"a": self.packed_for_phone(aid)})})

    def api_get(self, req: Request) -> Response:
        assignment = self.get_assignment(req.params["aid"])
        return json_response(assignment) if assignment else json_response({"error": "not found"}, status=404)

    def api_pack(self, req: Request) -> Response:
        """Course pack for a phone syncing over Wi-Fi. ?node=<radio id> records what it now holds."""
        pack = self.course_pack()
        node = (req.query.get("node") or "").strip()[:40]
        if node:
            name = (req.query.get("name") or node).strip()[:60]
            self.db.execute("INSERT INTO phones (node_id, name, last_seen) VALUES (?, ?, ?) "
                            "ON CONFLICT(node_id) DO UPDATE SET last_seen = excluded.last_seen", (node, name, time.time()))
            self.record_inventory(node, {p["i"]: p["v"] for p in pack}, complete=False)
        return json_response({"assignments": pack, "courses": self.course_list_for_phones()})

    def pack_zip(self, req: Request) -> Response:
        return Response(self.course_pack_zip(), content_type="application/zip",
                        headers={"Content-Disposition": "attachment; filename=meshlearn-course-pack.zip"})

    def assignment_detail(self, req: Request) -> Response:
        aid = req.params["aid"]
        assignment = self.get_assignment(aid)
        if assignment is None:
            return Response(self._page("Not found", "<p>No such assignment.</p>"), status=404)
        version = self.current_version(aid)
        full = protocol.frame_count(protocol.ASSIGN, {"a": self.packed_for_phone(aid), "o": 1, "d": assignment.get("due", "")})
        phones = self.db.all("SELECT * FROM phones ORDER BY name")
        holdings = {h["node_id"]: h for h in self.db.all("SELECT * FROM holdings WHERE aid = ?", (aid,))}
        phone_rows = ""
        for p in phones:
            h = holdings.get(p["node_id"])
            if h and h["version"] == version:
                held = "<span class='good'>yes</span>"
            elif h and h["version"]:
                held = "<span class='warn'>older version</span>"
            else:
                held = "<span class='muted'>no</span>"
            seen = "<span class='good'>open</span>" if h and h["open"] else "<span class='muted'>hidden</span>"
            if h and h["msg_id"]:
                state = self.messenger.message_status(h["msg_id"])
                if state == "pending":
                    seen += f" <span class='warn'>({ACTION_LABELS.get(h['action'], 'sending')}…)</span>"
                elif state == "failed":
                    seen += " <span class='bad'>(not delivered)</span>"
            cost = "1 packet" if h and h["version"] == version else f"{full} packets"
            node = esc(p["node_id"])
            buttons = (
                f"<form method='post' action='/assignments/{esc(aid)}/open' style='display:inline'>"
                f"<input type='hidden' name='node_id' value='{node}'><button class='secondary'>"
                f"{'Resend' if h and h['open'] else 'Open'} ({cost})</button></form>"
            )
            if h and h["open"]:
                buttons += (f"<form method='post' action='/assignments/{esc(aid)}/close' style='display:inline'>"
                            f"<input type='hidden' name='node_id' value='{node}'><button class='secondary'>Hide</button></form>")
            phone_rows += f"<tr><td>{esc(p['name'])}</td><td>{held}</td><td>{seen}</td><td>{buttons}</td></tr>"
        subs = self.db.all("SELECT * FROM submissions WHERE assignment_id = ? ORDER BY received_at DESC", (aid,))
        sub_rows = ""
        for s in subs:
            grade = json.loads(s["grade"]) if s["grade"] else None
            if grade is None:
                score = "<span class='muted'>—</span>"
            elif grade["complete"]:
                score = f"{grade['score']:g} / {grade['max']:g}"
            else:
                score = f"<span class='warn'>{grade['score']:g} / {grade['max']:g} so far · needs grading</span>"
            old = " <span class='muted'>(earlier version)</span>" if s["version"] and s["version"] != version else ""
            sub_rows += (
                f"<tr><td><a href='/submissions/{esc(s['sub_id'])}'>{esc(s['student'])}</a></td>"
                f"<td>{esc(s['phone'])}</td><td>{_when(s['submitted_at'])}</td><td>{score}{old}</td></tr>"
            )
        questions = "".join(f"<li>{esc(q['prompt'])} <span class='muted'>({q['type']}, {q['points']:g} pt)</span></li>"
                            for q in assignment["questions"])
        meta = [self._placement(assignment) or assignment.get("subject") or "", f"{assignment['mode']} mode",
                f"due {assignment['due']}" if assignment.get("due") else "", f"version {version}"]
        body = (
            f"<p class='muted'>{esc(' · '.join(m for m in meta if m))}</p>"
            f"<div class='card'><p>{esc(assignment.get('instructions') or '')}</p><ol>{questions}</ol>"
            f"<a class='button secondary' href='/assignments/{esc(aid)}/edit'>Edit</a></div>"
            "<h2>On student phones</h2>"
            f"<p class='muted'>Opening costs 1 radio packet on phones that already hold this version; "
            f"otherwise the full assignment goes by radio ({full} packets).</p>"
            + (f"<table><tr><th>Phone</th><th>Has it</th><th>Students</th><th></th></tr>{phone_rows}</table>"
               + (f"<form method='post' action='/assignments/{esc(aid)}/open'><input type='hidden' name='node_id' value='*'>"
                  "<button>Open on every phone</button></form>" if len(phones) > 1 else "")
               if phones else "<p class='muted'>Add a phone first on the <a href='/phones'>Phones</a> page.</p>")
            + "<h2>Submissions</h2>"
            + (f"<table><tr><th>Student</th><th>Phone</th><th>Submitted</th><th>Score</th></tr>{sub_rows}</table>"
               f"<a class='button secondary' href='/assignments/{esc(aid)}/grades.csv'>Download grades (CSV)</a>"
               if subs else "<p class='muted'>Nothing yet.</p>")
        )
        return Response(self._page(assignment["title"], body))

    def assignment_action(self, req: Request, open_: bool) -> Response:
        aid = req.params["aid"]
        node_id = req.form.get("node_id", "")
        if self.current_version(aid) is None or not node_id:
            return redirect("/")
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
        writer.writerow(["student", "phone", "submitted", "score", "max", "complete"] + qids)
        for s in self.db.all("SELECT * FROM submissions WHERE assignment_id = ? ORDER BY student", (aid,)):
            grade = json.loads(s["grade"]) if s["grade"] else {"score": "", "max": "", "complete": False, "questions": []}
            per_q = {q["id"]: q["score"] for q in grade["questions"]}
            writer.writerow([s["student"], s["phone"], _when(s["submitted_at"]), grade["score"], grade["max"],
                             grade["complete"]] + [per_q.get(q, "") for q in qids])
        return Response(out.getvalue(), content_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=grades-{aid}.csv"})

    def submission_detail(self, req: Request) -> Response:
        notice = "Saved. The result is on its way to the student's phone." if req.query.get("saved") else ""
        s = self.db.one("SELECT * FROM submissions WHERE sub_id = ?", (req.params["sid"],))
        if s is None:
            return Response(self._page("Not found", "<p>No such submission.</p>"), status=404)
        assignment = self.get_assignment(s["assignment_id"], s["version"])
        answers = json.loads(s["answers"])
        grade = json.loads(s["grade"]) if s["grade"] else None
        scores = {q["id"]: q["score"] for q in (grade or {}).get("questions", [])}
        rows = ""
        for q in (assignment or {"questions": []})["questions"]:
            given = answers.get(q["id"], "")
            if q["type"] == "choice" and given.isdigit() and int(given) < len(q["options"]):
                shown = q["options"][int(given)]
            else:
                shown = given
            score = scores.get(q["id"])
            if q["type"] == "long":
                current = "" if score is None else f"{score:g}"
                cell = (f"<input type='number' name='m_{esc(q['id'])}' min='0' max='{q['points']:g}' step='0.5' "
                        f"value='{current}' aria-label='Points for {esc(q['prompt'][:40])}'> / {q['points']:g}")
            else:
                mark = "good" if score else "bad"
                cell = f"<span class='{mark}'>{(score or 0):g} / {q['points']:g}</span>"
            rows += f"<tr><td>{esc(q['prompt'])}</td><td>{esc(shown)}</td><td>{cell}</td></tr>"
        total = f"{grade['score']:g} / {grade['max']:g}" if grade else "—"
        body = (
            (f"<p class='good'>{esc(notice)}</p>" if notice else "")
            + f"<p class='muted'>{esc(assignment['title'] if assignment else s['assignment_id'])} · from {esc(s['phone'])} · "
            f"submitted {_when(s['submitted_at'])}</p>"
            f"<form method='post' action='/submissions/{esc(s['sub_id'])}/grade'>"
            f"<table><tr><th>Question</th><th>Answer</th><th>Points</th></tr>{rows}</table>"
            f"<p><strong>Total: {total}</strong></p>"
            "<button>Save grades and send result to phone</button></form>"
            + (f"<p><a href='/assignments/{esc(assignment['id'])}'>Back to assignment</a></p>" if assignment else "")
        )
        return Response(self._page(f"Submission from {s['student']}", body))

    def submission_grade(self, req: Request) -> Response:
        sid = req.params["sid"]
        manual = {}
        for key, value in req.form.items():
            if key.startswith("m_") and value.strip():
                try:
                    manual[key[2:]] = float(value)
                except ValueError:
                    pass
        try:
            self.grade_submission(sid, manual)
        except KeyError:
            return redirect("/")
        row = self.db.one("SELECT phone FROM submissions WHERE sub_id = ?", (sid,))
        self._send_results(row["phone"], [sid])
        return redirect(f"/submissions/{sid}?saved=1")

    def phones(self, req: Request) -> Response:
        rows = "".join(
            f"<tr><td>{esc(p['name'])}</td><td>{esc(p['node_id'])}</td><td>{_when(p['last_seen'])}</td></tr>"
            for p in self.db.all("SELECT * FROM phones ORDER BY name")
        )
        body = (
            (f"<table><tr><th>Name</th><th>Radio node id</th><th>Last heard</th></tr>{rows}</table>" if rows
             else "<p class='muted'>No phones yet. Phones also appear here on their own when they first check in.</p>")
            + "<h2>Add a phone</h2><form method='post' action='/phones' class='card'>"
            "<label for='name'>Name (e.g. village or family)</label><input type='text' id='name' name='name' required>"
            "<label for='node_id'>Radio node id</label><input type='text' id='node_id' name='node_id' "
            "placeholder='!a1b2c3d4 (or phone-1 in the simulator)' required>"
            "<button>Add phone</button></form>"
        )
        return Response(self._page("Student phones", body))

    def phones_add(self, req: Request) -> Response:
        node_id = req.form.get("node_id", "").strip()[:40]
        if node_id:
            self.add_phone(node_id, req.form.get("name", "").strip()[:60])
        return redirect("/phones")

    def mesh_retry(self, req: Request) -> Response:
        self.messenger.retry_now()
        return redirect("/")

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
        used = sum(i["size"] for i in selected.values())

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
            f"<div><label for='lang_set'>Students' language</label><select id='lang_set' name='lang'>{lang_options}</select></div>"
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
                      "<form method='post' action='/library/refresh'><button>Download the list (about 4 MB)</button></form></div>")
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
                f"<option{' selected' if k == kind else ''}>{esc(k)}</option>" for k in self.catalog.labels(lang, min_level))
            filters = (
                "<form method='get' action='/library' class='card grid'>"
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
            for g in shown:
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
            browse = (
                filters
                + f"<p class='muted'>{len(groups)} collection{'' if len(groups) == 1 else 's'} match · list from {when} · "
                "<form method='post' action='/library/refresh' style='display:inline'><button class='secondary'>Update the list</button></form></p>"
                + (f"<table><tr><th>Collection</th><th>Add to card</th></tr>{items}</table>" if items else "<p class='muted'>Nothing matches.</p>")
                + nav
            )
        body = (
            (f"<p class='good' role='status'>{esc(notice)}</p>" if notice else "")
            + "<p class='muted'>Choose what goes on each phone's memory card. Material that suits the students' ages and "
            "learning material are listed first. Age ranges come from each collection's name and description. "
            "Everything is free and works without internet once it is on the card.</p>"
            + "<h2>This card</h2>" + plan + "<h2>Available collections</h2>" + browse
        )
        return Response(self._page("Card library", body))

    def _library_row(self, title, summary, label, level, lang, options, here, show_lang,
                     ages=None, age_est=False, fits=True) -> str:
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
            msg = f"Downloaded the list: {count} collections available."
        except Exception as exc:
            msg = f"Could not download the list ({exc}). Is this laptop online?"
        return redirect("/library?" + urlencode({"msg": msg}))

    def library_download_page(self, req: Request) -> Response:
        job = self.download_job
        selected = self.selection()
        total = sum(i["size"] for i in selected)
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
            "<li>Enter the phone's MeshLearn folder on the card. With Termux this is "
            "<code>&lt;card&gt;/Android/data/com.termux/files/meshlearn</code>.</li>"
            "<li>Start. Large collections take a while; you can stop and resume later.</li></ol>"
            "<form method='post' action='/library/download'>"
            f"<label for='dest'>MeshLearn folder on the card{space}</label>"
            f"<input type='text' id='dest' name='dest' value='{esc(dest)}' placeholder='/Volumes/SDCARD/Android/data/com.termux/files/meshlearn' required>"
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


BUILDER_HTML = """
<p class="muted">Write the assignment here. Phones that sync over Wi-Fi or by memory card get every assignment in
advance; opening it for students then costs one radio packet.</p>
<form id="builder" class="card" novalidate>
  <label for="title">Title</label><input type="text" id="title" required maxlength="120">
  <div class="grid">
    <div><label for="course">Course</label><select id="course"></select></div>
    <div><label for="unit">Unit</label><select id="unit"></select></div>
  </div>
  <label for="subject">Topic (optional)</label><input type="text" id="subject" maxlength="60">
  <label for="due">Due date</label><input type="date" id="due">
  <label for="instructions">Instructions for students</label><textarea id="instructions" maxlength="2000"></textarea>
  <label for="mode">Mode</label>
  <select id="mode">
    <option value="graded">Graded: answers are checked on this hub (answer key stays here)</option>
    <option value="practice">Practice: students see right/wrong instantly on the phone</option>
  </select>
  <div id="questions"></div>
  <p>
    <button type="button" class="secondary" data-add="choice">+ Multiple choice</button>
    <button type="button" class="secondary" data-add="number">+ Number</button>
    <button type="button" class="secondary" data-add="text">+ Short answer</button>
    <button type="button" class="secondary" data-add="long">+ Written response</button>
  </p>
  <p id="error" class="error" role="alert"></p>
  <button type="submit">Save assignment</button>
</form>
<script>
const EDIT_ID = "__EDIT_ID__";
const COURSES = __COURSES__;
const PRESET = __PRESET__;
const courseSel = document.getElementById('course'), unitSel = document.getElementById('unit');
courseSel.add(new Option('No course', ''));
COURSES.forEach(c => courseSel.add(new Option(c.title, c.id)));  // Option() sets text, so titles can't inject HTML
function fillUnits(selected) {
  const c = COURSES.find(c => c.id === courseSel.value);
  unitSel.length = 0;
  unitSel.add(new Option('No unit', ''));
  if (c) c.units.forEach((u, i) => unitSel.add(new Option(`Unit ${i + 1}: ${u.title}`, u.id)));
  if (selected) unitSel.value = selected;
}
courseSel.addEventListener('change', () => fillUnits());
courseSel.value = PRESET.course || ''; fillUnits(PRESET.unit);
const box = document.getElementById('questions');
let n = 0;
const labels = {choice: 'Multiple choice', number: 'Number', text: 'Short answer', long: 'Written response (you grade it)'};
function field(id, label, html) { return `<label for="${id}">${label}</label>${html}`; }
function setv(id, v) { const el = document.getElementById(id); if (el && v !== undefined && v !== null) el.value = v; }
function addQuestion(type, q) {
  n += 1;
  const id = 'q' + n;
  const fs = document.createElement('fieldset');
  fs.dataset.type = type;
  let extra = '';
  if (type === 'choice') {
    extra = field(id + 'o', 'Options, one per line', `<textarea id="${id}o" rows="4"></textarea>`) +
            field(id + 'a', 'Number of the correct option (1 = first)', `<input type="number" id="${id}a" min="1" value="1">`);
  } else if (type === 'number') {
    extra = field(id + 'a', 'Correct answer', `<input type="text" id="${id}a" inputmode="decimal">`) +
            field(id + 't', 'Allowed difference (0 = exact)', `<input type="text" id="${id}t" value="0" inputmode="decimal">`);
  } else if (type === 'text') {
    extra = field(id + 'a', 'Accepted answers, one per line (not case-sensitive)', `<textarea id="${id}a" rows="3"></textarea>`);
  } else {
    extra = field(id + 'm', 'Maximum characters', `<input type="number" id="${id}m" value="600" min="50" max="1500">`);
  }
  fs.innerHTML = `<legend>Question ${n}: ${labels[type]}</legend>` +
    field(id + 'p', 'Question', `<textarea id="${id}p" rows="2"></textarea>`) + extra +
    field(id + 'pts', 'Points', `<input type="number" id="${id}pts" value="${type === 'long' ? 4 : 1}" min="0.5" step="0.5">`) +
    `<button type="button" class="secondary" onclick="this.parentElement.remove()">Remove question</button>`;
  fs.dataset.key = id;
  box.appendChild(fs);
  if (q) {
    setv(id + 'p', q.prompt); setv(id + 'pts', q.points);
    if (type === 'choice') { setv(id + 'o', (q.options || []).join('\\n')); setv(id + 'a', (q.answer || 0) + 1); }
    if (type === 'number') { setv(id + 'a', q.answer); setv(id + 't', q.tolerance); }
    if (type === 'text') { setv(id + 'a', (q.accept || []).join('\\n')); }
    if (type === 'long') { setv(id + 'm', q.max_chars); }
  } else {
    fs.querySelector('textarea').focus();
  }
}
document.querySelectorAll('[data-add]').forEach(b => b.addEventListener('click', () => addQuestion(b.dataset.add)));
const val = id => (document.getElementById(id) || {}).value || '';
const lines = id => val(id).split('\\n').map(s => s.trim()).filter(Boolean);
document.getElementById('builder').addEventListener('submit', async e => {
  e.preventDefault();
  const questions = [...box.querySelectorAll('fieldset')].map((fs, i) => {
    const k = fs.dataset.key, type = fs.dataset.type;
    const q = {id: 'q' + (i + 1), type, prompt: val(k + 'p'), points: val(k + 'pts')};
    if (type === 'choice') { q.options = lines(k + 'o'); q.answer = parseInt(val(k + 'a'), 10) - 1; }
    if (type === 'number') { q.answer = val(k + 'a').replace(',', '.'); q.tolerance = val(k + 't').replace(',', '.'); }
    if (type === 'text') { q.accept = lines(k + 'a'); }
    if (type === 'long') { q.max_chars = val(k + 'm'); }
    return q;
  });
  const body = {title: val('title'), subject: val('subject'), due: val('due'), instructions: val('instructions'),
                mode: val('mode'), course: val('course'), unit: val('unit'), questions};
  if (EDIT_ID) body.id = EDIT_ID;
  const res = await fetch('/api/assignments', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await res.json();
  if (!res.ok) { document.getElementById('error').textContent = data.error; return; }
  location.href = '/assignments/' + data.id;
});
if (EDIT_ID) {
  fetch('/api/assignments/' + EDIT_ID).then(r => r.json()).then(a => {
    ['title', 'subject', 'due', 'instructions', 'mode'].forEach(k => setv(k, a[k]));
    courseSel.value = a.course || ''; fillUnits(a.unit || '');
    a.questions.forEach(q => addQuestion(q.type, q));
  });
} else {
  addQuestion('choice');
}
</script>
"""
