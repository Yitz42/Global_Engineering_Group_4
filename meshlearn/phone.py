"""Student phone server: hosts assignments and the offline library over Wi-Fi.

Runs on an old Android phone (Termux) with its data on the microSD card. The
phone turns on its hotspot; students join it with any device and open the
phone's address in a browser. Nothing needs the internet.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from typing import Dict, List, Optional
from urllib.parse import quote

from . import __version__
from . import assignments as asg
from . import protocol
from .db import Database
from .messenger import Messenger
from .web import App, Request, Response, esc, file_response, page, redirect

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY, data TEXT NOT NULL, received REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
    sub_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL, student TEXT NOT NULL,
    answers TEXT NOT NULL, submitted_at REAL NOT NULL, msg_id INTEGER,
    score REAL, max REAL, complete INTEGER, detail TEXT
);
"""

VIEWABLE = {".html", ".htm", ".txt", ".pdf", ".epub", ".png", ".jpg", ".jpeg", ".svg", ".mp3", ".mp4", ".webm", ".zim"}
STARTER_DIR = os.path.join(os.path.dirname(__file__), "starter_library")


def _when(ts: Optional[float]) -> str:
    return time.strftime("%b %d, %H:%M", time.localtime(ts)) if ts else ""


def _size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit in ("B", "KB") else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"


class Phone:
    def __init__(self, data_dir: str, hub_id: str, name: str, messenger_factory, kiwix_url: str = ""):
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.library_dir = os.path.join(data_dir, "library")
        self.hub_id = hub_id
        self.name = name
        self.kiwix_url = kiwix_url.rstrip("/")
        self._install_starter_library()
        self.db = Database(os.path.join(data_dir, "phone.db"), SCHEMA)
        self.messenger: Messenger = messenger_factory(self.on_message)
        self.app = App("phone")
        self._routes()

    def _install_starter_library(self) -> None:
        target = os.path.join(self.library_dir, "starter")
        if not os.path.isdir(target) and os.path.isdir(STARTER_DIR):
            shutil.copytree(STARTER_DIR, target)

    def say_hello(self) -> None:
        usage = shutil.disk_usage(self.data_dir)
        self.messenger.send(self.hub_id, protocol.HELLO, {
            "name": self.name, "free_mb": usage.free // (1024 * 1024),
            "library_items": len(self.library_items()), "version": __version__,
        })

    # -- mesh ---------------------------------------------------------------

    def on_message(self, message: protocol.Message) -> None:
        p = message.payload
        if message.type == protocol.ASSIGN:
            assignment = p.get("assignment") or {}
            if assignment.get("id") and assignment.get("questions"):
                self.db.execute(
                    "INSERT OR REPLACE INTO assignments (id, data, received) VALUES (?, ?, ?)",
                    (assignment["id"], json.dumps(assignment), time.time()),
                )
        elif message.type == protocol.RESULT:
            self.db.execute(
                "UPDATE submissions SET score = ?, max = ?, complete = ?, detail = ? WHERE sub_id = ?",
                (p.get("score"), p.get("max"), 1 if p.get("complete") else 0, json.dumps(p.get("q") or {}), p.get("sub_id")),
            )
        else:
            log.info("phone ignoring %s message from %s", message.type, message.src)

    # -- data ---------------------------------------------------------------

    def get_assignment(self, aid: str) -> Optional[dict]:
        row = self.db.one("SELECT data FROM assignments WHERE id = ?", (aid,))
        return json.loads(row["data"]) if row else None

    def submit(self, assignment: dict, student: str, answers: Dict[str, str]) -> str:
        sub_id = asg.new_id("s")
        now = time.time()
        payload = {"sub_id": sub_id, "assignment_id": assignment["id"], "student": student,
                   "answers": answers, "submitted_at": now}
        msg_id = self.messenger.send(self.hub_id, protocol.SUBMIT, payload)
        self.db.execute(
            "INSERT INTO submissions (sub_id, assignment_id, student, answers, submitted_at, msg_id) VALUES (?, ?, ?, ?, ?, ?)",
            (sub_id, assignment["id"], student, json.dumps(answers), now, msg_id),
        )
        return sub_id

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
                              "collection": folder.capitalize() if folder else "Other", "kind": ext.lstrip(".")})
        return items

    # -- web ----------------------------------------------------------------

    def _nav(self, req: Request) -> str:
        who = req.cookies.get("student")
        out = "<a href='/'>Assignments</a><a href='/library'>Library</a>"
        if who:
            out += f"<a href='/results'>My results</a><span class='muted'>Hi, {esc(who)}</span>"
        return out

    def _page(self, req: Request, title: str, body: str) -> str:
        return page(title, body, self._nav(req), site=f"MeshLearn · {self.name}")

    def _routes(self) -> None:
        app = self.app
        app.add_route("GET", "/", self.home)
        app.add_route("POST", "/login", self.login)
        app.add_route("POST", "/logout", self.logout)
        app.add_route("GET", "/a/<aid>", self.assignment_form)
        app.add_route("POST", "/a/<aid>", self.assignment_submit)
        app.add_route("GET", "/results", self.results)
        app.add_route("GET", "/library", self.library)
        app.add_route("GET", "/library/files/<path:path>", self.library_file)
        app.add_route("GET", "/status", self.status)

    def home(self, req: Request) -> Response:
        student = req.cookies.get("student")
        if not student:
            body = (
                "<div class='card'><p>Welcome! Type your name so your teacher knows whose work this is.</p>"
                "<form method='post' action='/login'><label for='student'>Your name</label>"
                "<input type='text' id='student' name='student' maxlength='40' autocomplete='name' required>"
                "<button>Start</button></form></div>"
                "<p>Or go straight to the <a href='/library'>library</a>.</p>"
            )
            return Response(self._page(req, "Welcome", body))
        rows = self.db.all("SELECT data, received FROM assignments ORDER BY received DESC")
        mine = {r["assignment_id"]: r for r in self.db.all(
            "SELECT assignment_id, MAX(submitted_at) AS at, score, max, complete FROM submissions "
            "WHERE student = ? GROUP BY assignment_id", (student,))}
        cards = ""
        for r in rows:
            a = json.loads(r["data"])
            sub = mine.get(a["id"])
            if sub is None:
                state = "<span class='pill'>To do</span>"
            elif sub["score"] is None:
                state = "<span class='pill warn'>Sent · waiting for grade</span>"
            else:
                state = f"<span class='pill good'>Score {sub['score']:g} / {sub['max']:g}</span>"
            due = f" · due {esc(a['due'])}" if a.get("due") else ""
            cards += (
                f"<div class='card'><h2 style='margin-top:0'><a href='/a/{esc(a['id'])}'>{esc(a['title'])}</a></h2>"
                f"<p class='muted'>{esc(a.get('subject') or '')}{due} · {len(a['questions'])} questions"
                f"{' · practice' if a.get('mode') == 'practice' else ''}</p>{state}</div>"
            )
        if not cards:
            cards = "<p class='muted'>No assignments on this phone yet. Your teacher will send them over the radio.</p>"
        body = cards + "<form method='post' action='/logout'><button class='secondary'>Not you? Change name</button></form>"
        return Response(self._page(req, "Your assignments", body))

    def login(self, req: Request) -> Response:
        name = " ".join(req.form.get("student", "").split())[:40]
        if not name:
            return redirect("/")
        return redirect("/").set_cookie("student", name)

    def logout(self, req: Request) -> Response:
        return redirect("/").set_cookie("student", "", max_age=0)

    def _render_form(self, req: Request, a: dict, values: Dict[str, str], errors: Dict[str, str]) -> str:
        parts = []
        if a.get("instructions"):
            parts.append(f"<div class='card'><p>{esc(a['instructions'])}</p></div>")
        if errors:
            parts.append(f"<p class='error' role='alert'>Please fix {len(errors)} answer{'s' if len(errors) > 1 else ''} below.</p>")
        parts.append(f"<form method='post' action='/a/{esc(a['id'])}' novalidate>")
        for i, q in enumerate(a["questions"], start=1):
            qid = esc(q["id"])
            value = values.get(q["id"], "")
            err = errors.get(q["id"])
            err_html = f"<p class='error' id='{qid}-err'>{esc(err)}</p>" if err else ""
            described = f" aria-describedby='{qid}-err' aria-invalid='true'" if err else ""
            pts = f" <span class='muted'>({q['points']:g} pt)</span>"
            if q["type"] == "choice":
                opts = "".join(
                    f"<label class='option'><input type='radio' name='{qid}' value='{j}'"
                    f"{' checked' if value == str(j) else ''}{described}> {esc(opt)}</label>"
                    for j, opt in enumerate(q["options"])
                )
                parts.append(f"<fieldset><legend>{i}. {esc(q['prompt'])}{pts}</legend>{err_html}{opts}</fieldset>")
            elif q["type"] == "long":
                limit = q.get("max_chars", 600)
                parts.append(
                    f"<fieldset><legend>{i}. {esc(q['prompt'])}{pts}</legend>{err_html}"
                    f"<label for='{qid}'>Your answer (up to {limit} characters)</label>"
                    f"<textarea id='{qid}' name='{qid}' maxlength='{limit}'{described}>{esc(value)}</textarea></fieldset>"
                )
            else:
                mode = " inputmode='decimal'" if q["type"] == "number" else ""
                hint = "A number, like 12, 3.5, 3,5 or 1/2" if q["type"] == "number" else "Short answer"
                parts.append(
                    f"<fieldset><legend>{i}. {esc(q['prompt'])}{pts}</legend>{err_html}"
                    f"<label for='{qid}'>{hint}</label>"
                    f"<input type='text' id='{qid}' name='{qid}' value='{esc(value)}' maxlength='{asg.MAX_TEXT_CHARS}'{mode}{described}>"
                    "</fieldset>"
                )
        parts.append("<button>Check and submit</button></form>")
        return "".join(parts)

    def assignment_form(self, req: Request) -> Response:
        student = req.cookies.get("student")
        if not student:
            return redirect("/")
        a = self.get_assignment(req.params["aid"])
        if a is None:
            return Response(self._page(req, "Not found", "<p>That assignment is not on this phone.</p>"), status=404)
        if a.get("mode") != "practice":
            done = self.db.one("SELECT submitted_at FROM submissions WHERE assignment_id = ? AND student = ?", (a["id"], student))
            if done:
                body = (f"<p>You already turned this in on {_when(done['submitted_at'])}.</p>"
                        "<p><a href='/results'>See your results</a></p>")
                return Response(self._page(req, a["title"], body))
        return Response(self._page(req, a["title"], self._render_form(req, a, {}, {})))

    def assignment_submit(self, req: Request) -> Response:
        student = req.cookies.get("student")
        if not student:
            return redirect("/")
        a = self.get_assignment(req.params["aid"])
        if a is None:
            return redirect("/")
        raw = {q["id"]: req.form.get(q["id"], "") for q in a["questions"]}
        answers, errors = asg.validate_answers(a, raw)
        if errors:
            return Response(self._page(req, a["title"], self._render_form(req, a, raw, errors)), status=400)
        if a.get("mode") != "practice" and self.db.one(
                "SELECT 1 FROM submissions WHERE assignment_id = ? AND student = ?", (a["id"], student)):
            return redirect("/results")
        self.submit(a, student, answers)
        if a.get("mode") == "practice":
            feedback = asg.practice_feedback(a, answers)
            items = ""
            for q in a["questions"]:
                ok = feedback.get(q["id"])
                mark = ("<span class='good'>Correct</span>" if ok else "<span class='bad'>Not quite</span>") if ok is not None \
                    else "<span class='muted'>Your teacher will read this one</span>"
                items += f"<li>{esc(q['prompt'])}: {mark}</li>"
            right = sum(1 for v in feedback.values() if v)
            body = (f"<p>You got <strong>{right}</strong> of {sum(1 for v in feedback.values() if v is not None)} "
                    f"auto-checked questions right.</p><ol>{items}</ol>"
                    f"<a class='button' href='/a/{esc(a['id'])}'>Try again</a><a class='button secondary' href='/'>Back</a>")
            return Response(self._page(req, "Practice results", body))
        body = ("<div class='card'><p class='good'><strong>Done! Your work is saved on this phone.</strong></p>"
                "<p>It will reach your teacher over the radio. That can take a while, and you do not need to wait here.</p></div>"
                "<a class='button' href='/'>Back to assignments</a>")
        return Response(self._page(req, "Submitted", body))

    def results(self, req: Request) -> Response:
        student = req.cookies.get("student")
        if not student:
            return redirect("/")
        rows = self.db.all("SELECT * FROM submissions WHERE student = ? ORDER BY submitted_at DESC", (student,))
        out = ""
        for s in rows:
            a = self.get_assignment(s["assignment_id"]) or {"title": s["assignment_id"]}
            delivery = self.messenger.message_status(s["msg_id"]) if s["msg_id"] else None
            sent = {"acked": "reached your teacher", "pending": "waiting for the radio", "failed": "could not be sent"}.get(delivery, "saved")
            if s["score"] is None:
                grade = "Not graded yet"
            elif s["complete"]:
                grade = f"<strong>{s['score']:g} / {s['max']:g}</strong>"
            else:
                grade = f"{s['score']:g} / {s['max']:g} so far (teacher still grading)"
            out += f"<tr><td>{esc(a['title'])}</td><td>{_when(s['submitted_at'])}</td><td>{sent}</td><td>{grade}</td></tr>"
        body = (f"<table><tr><th>Assignment</th><th>Turned in</th><th>Delivery</th><th>Grade</th></tr>{out}</table>"
                if out else "<p class='muted'>Nothing turned in yet.</p>")
        return Response(self._page(req, "My results", body))

    def library(self, req: Request) -> Response:
        query = (req.query.get("q") or "").strip().lower()
        items = self.library_items()
        if query:
            items = [i for i in items if query in (i.get("title", "") + " " + i.get("collection", "")).lower()]
        groups: Dict[str, List[dict]] = {}
        for item in items:
            groups.setdefault(item.get("collection") or "Other", []).append(item)
        sections = ""
        for name in sorted(groups):
            rows = ""
            for item in groups[name]:
                href = "/library/files/" + quote(item["file"].replace(os.sep, "/"))
                link = f"<a href='{href}'>{esc(item['title'])}</a>"
                note = ""
                if item.get("kind") == "zim":
                    book = os.path.splitext(os.path.basename(item["file"]))[0]
                    if self.kiwix_url:
                        link = f"<a href='{esc(self.kiwix_url)}/content/{quote(book)}'>{esc(item['title'])}</a>"
                    else:
                        note = " <span class='muted'>· open with the Kiwix app</span>"
                rows += (f"<tr><td>{link}{note}</td><td class='muted'>{esc(item.get('kind', ''))}</td>"
                         f"<td class='muted'>{_size(item.get('size', 0))}</td></tr>")
            sections += f"<h2>{esc(name)}</h2><table>{rows}</table>"
        search = (f"<form method='get' action='/library'><label for='q'>Search the library</label>"
                  f"<input type='text' id='q' name='q' value='{esc(query)}'><button>Search</button></form>")
        body = search + (sections or "<p class='muted'>Nothing found.</p>")
        return Response(self._page(req, "Library", body))

    def library_file(self, req: Request) -> Response:
        root = os.path.realpath(self.library_dir)
        target = os.path.realpath(os.path.join(root, req.params["path"]))
        if not target.startswith(root + os.sep) or not os.path.isfile(target):
            return Response(self._page(req, "Not found", "<p>That file is not in the library.</p>"), status=404)
        return file_response(target)

    def status(self, req: Request) -> Response:
        usage = shutil.disk_usage(self.data_dir)
        q = self.messenger.status()
        body = (
            "<div class='grid'>"
            f"<div class='card'><div class='stat'>{q['pending']}</div>messages waiting for the radio</div>"
            f"<div class='card'><div class='stat'>{q['acked']}</div>messages delivered</div>"
            f"<div class='card'><div class='stat'>{_size(usage.free)}</div>free on the card</div>"
            f"<div class='card'><div class='stat'>{len(self.library_items())}</div>library items</div></div>"
            f"<p class='muted'>Hub radio: {esc(self.hub_id)} · data folder: {esc(self.data_dir)}</p>"
        )
        return Response(self._page(req, "Phone status", body))
