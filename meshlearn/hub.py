"""Teacher hub: build assignments, send them over the mesh, grade what comes back."""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from typing import Optional

from . import assignments as asg
from . import protocol
from .db import Database
from .messenger import Messenger
from .web import App, Request, Response, esc, json_response, page, redirect

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY, data TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS phones (
    node_id TEXT PRIMARY KEY, name TEXT NOT NULL, last_seen REAL, info TEXT
);
CREATE TABLE IF NOT EXISTS deliveries (
    assignment_id TEXT NOT NULL, node_id TEXT NOT NULL, msg_id INTEGER NOT NULL, sent_at REAL NOT NULL,
    PRIMARY KEY (assignment_id, node_id)
);
CREATE TABLE IF NOT EXISTS submissions (
    sub_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL, student TEXT NOT NULL, phone TEXT NOT NULL,
    answers TEXT NOT NULL, submitted_at REAL, received_at REAL NOT NULL,
    grade TEXT, manual TEXT NOT NULL DEFAULT '{}', result_msg_id INTEGER
);
"""


def _when(ts: Optional[float]) -> str:
    return time.strftime("%b %d, %H:%M", time.localtime(ts)) if ts else "never"


class Hub:
    def __init__(self, db_path: str, messenger_factory):
        self.db = Database(db_path, SCHEMA)
        self.messenger: Messenger = messenger_factory(self.on_message)
        self.app = App("hub")
        self._routes()

    # -- data ---------------------------------------------------------------

    def create_assignment(self, data: dict) -> dict:
        assignment = asg.normalize_assignment(data)
        self.db.execute(
            "INSERT OR REPLACE INTO assignments (id, data, created) VALUES (?, ?, ?)",
            (assignment["id"], json.dumps(assignment), time.time()),
        )
        return assignment

    def get_assignment(self, aid: str) -> Optional[dict]:
        row = self.db.one("SELECT data FROM assignments WHERE id = ?", (aid,))
        return json.loads(row["data"]) if row else None

    def add_phone(self, node_id: str, name: str) -> None:
        self.db.execute(
            "INSERT INTO phones (node_id, name) VALUES (?, ?) ON CONFLICT(node_id) DO UPDATE SET name = excluded.name",
            (node_id, name or node_id),
        )

    def send_assignment(self, aid: str, node_id: str) -> int:
        assignment = self.get_assignment(aid)
        if assignment is None:
            raise KeyError(aid)
        msg_id = self.messenger.send(node_id, protocol.ASSIGN, {"assignment": asg.public_view(assignment)})
        self.db.execute(
            "INSERT OR REPLACE INTO deliveries (assignment_id, node_id, msg_id, sent_at) VALUES (?, ?, ?, ?)",
            (aid, node_id, msg_id, time.time()),
        )
        return msg_id

    def _send_result(self, sub_id: str) -> None:
        row = self.db.one("SELECT phone, grade FROM submissions WHERE sub_id = ?", (sub_id,))
        if not row or not row["grade"]:
            return
        grade = json.loads(row["grade"])
        payload = {"sub_id": sub_id, "score": grade["score"], "max": grade["max"], "complete": grade["complete"],
                   "q": {q["id"]: q["score"] for q in grade["questions"]}}
        msg_id = self.messenger.send(row["phone"], protocol.RESULT, payload)
        self.db.execute("UPDATE submissions SET result_msg_id = ? WHERE sub_id = ?", (msg_id, sub_id))

    def grade_submission(self, sub_id: str, manual: Optional[dict] = None) -> dict:
        row = self.db.one("SELECT * FROM submissions WHERE sub_id = ?", (sub_id,))
        if row is None:
            raise KeyError(sub_id)
        assignment = self.get_assignment(row["assignment_id"])
        merged = json.loads(row["manual"])
        merged.update(manual or {})
        grade = asg.grade(assignment, json.loads(row["answers"]), merged) if assignment else None
        self.db.execute(
            "UPDATE submissions SET grade = ?, manual = ? WHERE sub_id = ?",
            (json.dumps(grade) if grade else None, json.dumps(merged), sub_id),
        )
        return grade

    # -- mesh ---------------------------------------------------------------

    def on_message(self, message: protocol.Message) -> None:
        p = message.payload
        if message.type == protocol.HELLO:
            self.db.execute(
                "INSERT INTO phones (node_id, name, last_seen, info) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(node_id) DO UPDATE SET last_seen = excluded.last_seen, info = excluded.info",
                (message.src, str(p.get("name") or message.src)[:60], time.time(), json.dumps(p)),
            )
        elif message.type == protocol.SUBMIT:
            sub_id = str(p.get("sub_id", ""))[:40]
            if not sub_id:
                return
            self.db.execute(
                "INSERT OR IGNORE INTO submissions (sub_id, assignment_id, student, phone, answers, submitted_at, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sub_id, str(p.get("assignment_id", ""))[:20], str(p.get("student", "?"))[:60], message.src,
                 json.dumps(p.get("answers") or {}), p.get("submitted_at"), time.time()),
            )
            self.db.execute("UPDATE phones SET last_seen = ? WHERE node_id = ?", (time.time(), message.src))
            grade = self.grade_submission(sub_id)
            if grade:
                self._send_result(sub_id)
        else:
            log.info("hub ignoring %s message from %s", message.type, message.src)

    # -- web ----------------------------------------------------------------

    def _nav(self) -> str:
        return "<a href='/'>Dashboard</a><a href='/assignments/new'>New assignment</a><a href='/phones'>Phones</a>"

    def _page(self, title: str, body: str) -> str:
        return page(title, body, self._nav(), site="MeshLearn Teacher Hub")

    def _routes(self) -> None:
        app = self.app
        app.add_route("GET", "/", self.dashboard)
        app.add_route("GET", "/assignments/new", self.new_assignment)
        app.add_route("POST", "/api/assignments", self.api_create)
        app.add_route("GET", "/assignments/<aid>", self.assignment_detail)
        app.add_route("POST", "/assignments/<aid>/send", self.assignment_send)
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
        body = f"{stats}<h2>Assignments</h2>{table}<h2>Student phones</h2><ul>{phone_list}</ul>"
        return Response(self._page("Dashboard", body))

    def new_assignment(self, req: Request) -> Response:
        return Response(self._page("New assignment", BUILDER_HTML))

    def api_create(self, req: Request) -> Response:
        try:
            assignment = self.create_assignment(req.json())
        except (asg.AssignmentError, ValueError) as exc:
            return json_response({"error": str(exc)}, status=400)
        size = len(protocol.encode(protocol.ASSIGN, {"assignment": asg.public_view(assignment)})[1])
        return json_response({"id": assignment["id"], "frames": size})

    def assignment_detail(self, req: Request) -> Response:
        aid = req.params["aid"]
        assignment = self.get_assignment(aid)
        if assignment is None:
            return Response(self._page("Not found", "<p>No such assignment.</p>"), status=404)
        frames = len(protocol.encode(protocol.ASSIGN, {"assignment": asg.public_view(assignment)})[1])
        phones = self.db.all("SELECT * FROM phones ORDER BY name")
        deliveries = {d["node_id"]: d for d in self.db.all("SELECT * FROM deliveries WHERE assignment_id = ?", (aid,))}
        phone_rows = ""
        for p in phones:
            d = deliveries.get(p["node_id"])
            if d:
                state = self.messenger.message_status(d["msg_id"]) or "unknown"
                label = {"acked": "<span class='good'>delivered</span>", "pending": "<span class='warn'>sending…</span>",
                         "failed": "<span class='bad'>failed</span>"}.get(state, state)
                label += f" <span class='muted'>({_when(d['sent_at'])})</span>"
            else:
                label = "<span class='muted'>not sent</span>"
            phone_rows += (
                f"<tr><td>{esc(p['name'])}</td><td>{label}</td><td><form method='post' action='/assignments/{esc(aid)}/send'>"
                f"<input type='hidden' name='node_id' value='{esc(p['node_id'])}'><button class='secondary'>"
                f"{'Resend' if d else 'Send'}</button></form></td></tr>"
            )
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
            sub_rows += (
                f"<tr><td><a href='/submissions/{esc(s['sub_id'])}'>{esc(s['student'])}</a></td>"
                f"<td>{esc(s['phone'])}</td><td>{_when(s['submitted_at'])}</td><td>{score}</td></tr>"
            )
        questions = "".join(f"<li>{esc(q['prompt'])} <span class='muted'>({q['type']}, {q['points']:g} pt)</span></li>"
                            for q in assignment["questions"])
        meta = [assignment.get("subject") or "", f"{assignment['mode']} mode",
                f"due {assignment['due']}" if assignment.get("due") else "", f"{frames} radio packets"]
        body = (
            f"<p class='muted'>{esc(' · '.join(m for m in meta if m))}</p>"
            f"<div class='card'><p>{esc(assignment.get('instructions') or '')}</p><ol>{questions}</ol></div>"
            "<h2>Send to phones</h2>"
            + (f"<table><tr><th>Phone</th><th>Status</th><th></th></tr>{phone_rows}</table>" if phones
               else "<p class='muted'>Add a phone first on the <a href='/phones'>Phones</a> page.</p>")
            + "<h2>Submissions</h2>"
            + (f"<table><tr><th>Student</th><th>Phone</th><th>Submitted</th><th>Score</th></tr>{sub_rows}</table>"
               f"<a class='button secondary' href='/assignments/{esc(aid)}/grades.csv'>Download grades (CSV)</a>"
               if subs else "<p class='muted'>Nothing yet.</p>")
        )
        return Response(self._page(assignment["title"], body))

    def assignment_send(self, req: Request) -> Response:
        aid = req.params["aid"]
        node_id = req.form.get("node_id", "")
        if self.get_assignment(aid) is None or not node_id:
            return redirect("/")
        self.send_assignment(aid, node_id)
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
        assignment = self.get_assignment(s["assignment_id"])
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
        self._send_result(sid)
        return redirect(f"/submissions/{sid}?saved=1")

    def phones(self, req: Request) -> Response:
        rows = "".join(
            f"<tr><td>{esc(p['name'])}</td><td>{esc(p['node_id'])}</td><td>{_when(p['last_seen'])}</td></tr>"
            for p in self.db.all("SELECT * FROM phones ORDER BY name")
        )
        body = (
            (f"<table><tr><th>Name</th><th>Radio node id</th><th>Last heard</th></tr>{rows}</table>" if rows
             else "<p class='muted'>No phones yet. Phones also appear here on their own when they first say hello.</p>")
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
<p class="muted">Build the assignment here, then send it to student phones from its page.
Keep it short: every question travels by radio.</p>
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
const box = document.getElementById('questions');
let n = 0;
const labels = {choice: 'Multiple choice', number: 'Number', text: 'Short answer', long: 'Written response (you grade it)'};
function field(id, label, html) { return `<label for="${id}">${label}</label>${html}`; }
function addQuestion(type) {
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
  fs.querySelector('textarea').focus();
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
  const res = await fetch('/api/assignments', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await res.json();
  if (!res.ok) { document.getElementById('error').textContent = data.error; return; }
  location.href = '/assignments/' + data.id;
});
addQuestion('choice');
</script>
"""
