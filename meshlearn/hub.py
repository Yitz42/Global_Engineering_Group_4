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
import time
import zipfile
from typing import Dict, Iterable, List, Optional

from . import assignments as asg
from . import protocol
from .db import Database
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
"""

ACTION_LABELS = {"open": "opening", "close": "closing", "update": "sending update", "copy": "sending full copy"}


def _when(ts: Optional[float]) -> str:
    return time.strftime("%b %d, %H:%M", time.localtime(ts)) if ts else "never"


class Hub:
    def __init__(self, db_path: str, messenger_factory):
        self.db = Database(db_path, SCHEMA)
        self.messenger: Messenger = messenger_factory(self.on_message)
        self.app = App("hub")
        self._routes()

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
        packed = asg.pack(asg.public_view(assignment))
        packed["v"] = self.current_version(aid)
        return packed

    def course_pack(self) -> List[dict]:
        return [self.packed_for_phone(r["id"]) for r in self.db.all("SELECT id FROM assignments ORDER BY created")]

    def course_pack_zip(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for packed in self.course_pack():
                zf.writestr(f"assignments/{packed['i']}.json", json.dumps(packed, ensure_ascii=False, indent=1))
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
                self.db.execute(
                    "INSERT OR IGNORE INTO submissions (sub_id, assignment_id, version, student, phone, answers, submitted_at, received_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (sub_id, str(s.get("a", ""))[:20], str(s.get("v", ""))[:16], str(s.get("u", "?"))[:60], src,
                     json.dumps(s.get("x") or {}), s.get("t"), time.time()),
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
        return "<a href='/'>Dashboard</a><a href='/assignments/new'>New assignment</a><a href='/phones'>Phones</a>"

    def _page(self, title: str, body: str) -> str:
        return page(title, body, self._nav(), site="MeshLearn Teacher Hub")

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
                f"<td>{esc(json.loads(r['data']).get('subject') or '')}</td><td>{r['subs']}</td><td>{r['to_grade']}</td></tr>"
                for r in rows
            )
            table = f"<table><tr><th>Assignment</th><th>Subject</th><th>Submissions</th><th>To grade</th></tr>{items}</table>"
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
        body = f"{stats}<h2>Assignments</h2>{table}{pack}<h2>Student phones</h2><ul>{phone_list}</ul>"
        return Response(self._page("Dashboard", body))

    def new_assignment(self, req: Request) -> Response:
        return Response(self._page("New assignment", BUILDER_HTML.replace("__EDIT_ID__", "")))

    def edit_assignment(self, req: Request) -> Response:
        aid = req.params["aid"]
        if self.get_assignment(aid) is None:
            return redirect("/")
        note = ("<p class='warn'>Changing questions creates a new version. Phones that already hold this assignment "
                "will need the full copy again (sent automatically to phones where it is open). Changing only the "
                "due date costs one packet per phone.</p>")
        return Response(self._page("Edit assignment", note + BUILDER_HTML.replace("__EDIT_ID__", esc(aid))))

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
        return json_response({"assignments": pack})

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
        meta = [assignment.get("subject") or "", f"{assignment['mode']} mode",
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


BUILDER_HTML = """
<p class="muted">Write the assignment here. Phones that sync over Wi-Fi or by memory card get every assignment in
advance; opening it for students then costs one radio packet.</p>
<form id="builder" class="card" novalidate>
  <label for="title">Title</label><input type="text" id="title" required maxlength="120">
  <label for="subject">Subject</label><input type="text" id="subject" maxlength="60">
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
                mode: val('mode'), questions};
  if (EDIT_ID) body.id = EDIT_ID;
  const res = await fetch('/api/assignments', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await res.json();
  if (!res.ok) { document.getElementById('error').textContent = data.error; return; }
  location.href = '/assignments/' + data.id;
});
if (EDIT_ID) {
  fetch('/api/assignments/' + EDIT_ID).then(r => r.json()).then(a => {
    ['title', 'subject', 'due', 'instructions', 'mode'].forEach(k => setv(k, a[k]));
    a.questions.forEach(q => addQuestion(q.type, q));
  });
} else {
  addQuestion('choice');
}
</script>
"""
