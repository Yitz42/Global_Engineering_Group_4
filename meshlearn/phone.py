"""Student phone server: hosts assignments and the offline library over Wi-Fi.

Runs on an old Android phone (Termux) with its data on the microSD card. The
phone turns on its hotspot; students join it with any device and open the
phone's address in a browser. Nothing needs the internet.

The phone keeps its own copy of every assignment (from a course pack loaded by
memory card or school Wi-Fi, or sent by radio when new). Students only see the
ones the teacher has opened. Submissions are batched so several students'
work shares a few radio packets.
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
from . import protocol
from .db import Database
from .messenger import Messenger
from .web import App, Request, Response, esc, file_response, page, redirect

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
"""

VIEWABLE = {".html", ".htm", ".txt", ".pdf", ".epub", ".png", ".jpg", ".jpeg", ".svg", ".mp3", ".mp4", ".webm", ".zim"}
STARTER_DIR = os.path.join(os.path.dirname(__file__), "starter_library")
MAX_BATCH = 20


def _when(ts: Optional[float]) -> str:
    return time.strftime("%b %d, %H:%M", time.localtime(ts)) if ts else ""


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
        self.messenger.send(self.hub_id, protocol.HELLO, {
            "n": self.name, "f": usage.free // (1024 * 1024), "l": len(self.library_items()),
            "sw": __version__, "inv": self.inventory(),
        })

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

    def load_pack_folder(self) -> int:
        """Load assignments copied onto the memory card (data/assignments/*.json)."""
        count = 0
        if not os.path.isdir(self.pack_dir):
            return 0
        for fname in sorted(os.listdir(self.pack_dir)):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.pack_dir, fname), encoding="utf-8") as fh:
                    if self.import_assignment(json.load(fh)):
                        count += 1
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                log.warning("skipping unreadable course pack file %s", fname)
        return count

    def sync_from_hub(self, hub_url: str) -> int:
        """Copy every assignment from the teacher hub over Wi-Fi (no radio)."""
        query = urllib.parse.urlencode({"node": self.node_id, "name": self.name})
        url = hub_url.rstrip("/") + "/api/pack?" + query
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
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

    def submit(self, assignment: dict, student: str, answers: Dict[str, str]) -> str:
        """Save a submission now; the batcher sends it with others shortly."""
        sub_id = asg.new_id("s")
        self.db.execute(
            "INSERT INTO submissions (sub_id, assignment_id, version, student, answers, submitted_at) VALUES (?, ?, ?, ?, ?, ?)",
            (sub_id, assignment["id"], assignment.get("version"), student, json.dumps(answers), time.time()),
        )
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
        batch = [{"id": r["sub_id"], "a": r["assignment_id"], "v": r["version"], "u": r["student"],
                  "x": json.loads(r["answers"]), "t": round(r["submitted_at"])} for r in rows]
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
                              "collection": folder.capitalize() if folder else "Other", "kind": ext.lstrip(".")})
        return items

    # -- web ------------------------------------------------------------------

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
        app.add_route("POST", "/status/sync", self.status_sync)
        app.add_route("POST", "/status/reload", self.status_reload)

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
        rows = self.db.all("SELECT id FROM assignments WHERE open = 1 ORDER BY due = '', due, received DESC")
        mine = {r["assignment_id"]: r for r in self.db.all(
            "SELECT assignment_id, MAX(submitted_at) AS at, score, max, complete FROM submissions "
            "WHERE student = ? GROUP BY assignment_id", (student,))}
        cards = ""
        for r in rows:
            a = self.get_assignment(r["id"])
            sub = mine.get(a["id"])
            if sub is None:
                state = "<span class='pill'>To do</span>"
            elif sub["score"] is None:
                state = "<span class='pill warn'>Turned in · waiting for grade</span>"
            else:
                state = f"<span class='pill good'>Score {sub['score']:g} / {sub['max']:g}</span>"
            due = f" · due {esc(a['due'])}" if a.get("due") else ""
            cards += (
                f"<div class='card'><h2 style='margin-top:0'><a href='/a/{esc(a['id'])}'>{esc(a['title'])}</a></h2>"
                f"<p class='muted'>{esc(a.get('subject') or '')}{due} · {len(a['questions'])} questions"
                f"{' · practice' if a.get('mode') == 'practice' else ''}</p>{state}</div>"
            )
        if not cards:
            cards = "<p class='muted'>No assignments are open right now. Your teacher will open them by radio.</p>"
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

    def _open_assignment(self, req: Request):
        a = self.get_assignment(req.params["aid"])
        return a if a and a.get("open") else None

    def assignment_form(self, req: Request) -> Response:
        student = req.cookies.get("student")
        if not student:
            return redirect("/")
        a = self._open_assignment(req)
        if a is None:
            return Response(self._page(req, "Not open", "<p>That assignment is not open right now.</p>"
                                       "<p><a href='/'>Back to assignments</a></p>"), status=404)
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
        a = self._open_assignment(req)
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
            delivery = self.messenger.message_status(s["msg_id"])
            sent = {"acked": "reached your teacher", "pending": "on its way", "failed": "could not be sent"}.get(
                delivery, "saved, sending soon")
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

    # -- status and admin -----------------------------------------------------

    def _is_admin(self, req: Request) -> bool:
        """Course-pack changes come from the phone itself, or anyone with the PIN."""
        if req.client in ("127.0.0.1", "::1"):
            return True
        return bool(self.admin_pin) and req.form.get("pin", "") == self.admin_pin

    def status(self, req: Request, notice: str = "") -> Response:
        usage = shutil.disk_usage(self.data_dir)
        q = self.messenger.status()
        held = self.db.one("SELECT COUNT(*) AS n, SUM(open) AS o FROM assignments")
        waiting = self.db.one("SELECT COUNT(*) AS n FROM submissions WHERE msg_id IS NULL")["n"]
        def pin_field(field_id: str) -> str:
            if not self.admin_pin:
                return ""
            return (f"<label for='{field_id}'>Teacher PIN</label>"
                    f"<input type='text' id='{field_id}' name='pin' inputmode='numeric'>")

        body = (
            (f"<p class='good' role='status'>{esc(notice)}</p>" if notice else "")
            + "<div class='grid'>"
            f"<div class='card'><div class='stat'>{held['n']}</div>assignments stored ({held['o'] or 0} open)</div>"
            f"<div class='card'><div class='stat'>{waiting}</div>submissions waiting to be batched</div>"
            f"<div class='card'><div class='stat'>{q['pending']}</div>messages waiting for the radio"
            f"<div class='muted'>{q['frames_sent']} radio packets sent since start</div></div>"
            f"<div class='card'><div class='stat'>{_size(usage.free)}</div>free on the card</div></div>"
            "<h2>Copy assignments from the teacher (no radio)</h2>"
            "<form method='post' action='/status/sync' class='card'>"
            "<p>When this phone is on the same Wi-Fi as the teacher's laptop, copy every assignment at once.</p>"
            "<label for='hub_url'>Teacher hub address</label>"
            "<input type='text' id='hub_url' name='hub_url' placeholder='http://192.168.1.20:8000' required>"
            f"{pin_field('pin-sync')}<button>Copy assignments</button></form>"
            "<form method='post' action='/status/reload' class='card'>"
            "<p>Copied a course pack onto the memory card? Load it now.</p>"
            f"{pin_field('pin-reload')}<button class='secondary'>Reload from card</button></form>"
            f"<p class='muted'>Teacher hub radio: {esc(self.hub_id)} · this phone's radio: {esc(self.node_id)} · "
            f"data folder: {esc(self.data_dir)}</p>"
        )
        return Response(self._page(req, "Phone status", body))

    def status_sync(self, req: Request) -> Response:
        if not self._is_admin(req):
            return self.status(req, notice="Only the teacher can do that (open this page on the phone itself, or enter the PIN).")
        url = req.form.get("hub_url", "").strip()
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        try:
            count = self.sync_from_hub(url)
        except Exception as exc:
            return self.status(req, notice=f"Could not reach the teacher hub at {url}: {exc}")
        # No radio check-in needed: the hub recorded what this phone copied.
        return self.status(req, notice=f"Copied from the teacher hub: {count} new or updated assignments.")

    def status_reload(self, req: Request) -> Response:
        if not self._is_admin(req):
            return self.status(req, notice="Only the teacher can do that (open this page on the phone itself, or enter the PIN).")
        count = self.load_pack_folder()
        self.say_hello()
        return self.status(req, notice=f"Loaded from the card: {count} new or updated assignments.")
