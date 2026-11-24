"""Teacher hub pages for courses, comprehension and learning data.

Kept apart from hub.py, which holds the assignments, phones and radio. The Hub
class mixes these in, so they share its database and page layout.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import secrets
import time
from typing import Dict, List, Optional

from . import __version__
from . import assignments as asg
from . import comprehension as cmp
from . import courses as crs
from .web import Request, Response, esc, json_response, redirect

RECORD_FORMAT = "meshlearn-learning-record"
RECORD_VERSION = 1
RECORD_DICTIONARY = {
    "format": "Always 'meshlearn-learning-record'. 'version' rises only when fields change meaning.",
    "encoding": "'none': plain JSON. Reserved for a later encrypted or shrunk form.",
    "hub": "A random id for the teacher hub that exported the file (not a name or address).",
    "courses": "Every course and its numbered units at export time.",
    "assignments": "Every version of every assignment answered, with its questions and answer key.",
    "submissions.student": "A pseudonym: the same student always gets the same one from this hub; names never leave it.",
    "submissions.phone": "A pseudonym for the community phone the work came from.",
    "submissions.duration_s": "Seconds from opening the assignment on the phone to submitting it (null if unknown).",
    "submissions.answers": "What the student typed or picked, by question id (choice answers are option numbers, 0 = first).",
    "submissions.scores": "Points earned per question; null = not graded yet (a written answer).",
    "submissions.mistakes": "The kind of mistake per question that lost marks (see 'mistake_kinds').",
    "submissions.wrong_notes": "What the teacher says a wrong answer shows, per question (their own words).",
    "submissions.not_sure": "Questions the student ticked 'I'm not sure about this one'.",
    "submissions.first_answers": "Their first answer, where they changed it after moving on.",
    "submissions.pace": "'rushing', 'fast_right', 'slow' or null: a signal worth a look, never proof.",
    "mistake_kinds": "Every mistake kind, its area and what it means.",
}


class HubPages:
    """Mixed into Hub; expects self.db, self.get_assignment, self.courses, self._page …"""

    # -- routes -----------------------------------------------------------------

    def _page_routes(self) -> None:
        app = self.app
        app.add_route("GET", "/courses", self.courses_page)
        app.add_route("POST", "/courses", self.course_create)
        app.add_route("POST", "/courses/reload", self.courses_reload)
        app.add_route("GET", "/courses/<cid>", self.course_page)
        app.add_route("POST", "/courses/<cid>", self.course_update)
        app.add_route("GET", "/data/export.json", self.data_export)
        app.add_route("POST", "/api/data/import", self.data_import)

    # -- comprehension data -----------------------------------------------------

    def comprehension_records(self) -> List[dict]:
        """Every graded submission as a record for comprehension.py."""
        records = []
        known = {c["id"] for c in self.courses()}
        for s in self.db.all("SELECT * FROM submissions WHERE grade IS NOT NULL ORDER BY submitted_at"):
            a = self.get_assignment(s["assignment_id"], s["version"])
            if not a:
                continue
            grade = json.loads(s["grade"])
            unsure, first = asg.read_signals(a, json.loads(s["signals"] or "{}"))
            records.append({
                "unsure": unsure, "first": first,
                "sub_id": s["sub_id"], "student": s["student"], "phone": s["phone"], "aid": a["id"],
                "version": s["version"] or "", "course": a.get("course", "") if a.get("course", "") in known else "",
                "unit": a.get("unit", ""),
                "mode": a.get("mode", "graded"), "at": s["submitted_at"] or s["received_at"],
                "duration": s["duration"], "questions": a["questions"], "answers": json.loads(s["answers"]),
                "scores": {q["id"]: q["score"] for q in grade["questions"]}, "complete": grade["complete"],
                "source": s["source"], "received": s["received_at"],
            })
        return records

    def course_summary(self, course: dict, records: Optional[List[dict]] = None) -> dict:
        return cmp.summarize(self.comprehension_records() if records is None else records, course)

    # -- courses ----------------------------------------------------------------

    def courses_page(self, req: Request) -> Response:
        records = self.comprehension_records()
        cards = ""
        for c in self.courses(include_other=True):
            s = cmp.summarize(records, c)
            count = len(self.assignments_in(c["id"]))
            cls = {"Strong": "strong", "Good": "good", "Developing": "developing", "Needs work": "needs"}.get(s["level"], "")
            level = "Coming later" if c.get("comingSoon") else s["level"] + (f" · {s['score']}%" if s["score"] is not None else "")
            cards += (f"<a class='need' href='/courses/{esc(c['id']) or '_other'}'{' style=opacity:.6' if c.get('comingSoon') else ''}>"
                      f"<span style='display:flex;justify-content:space-between;gap:8px;align-items:flex-start'><strong style='font-size:19px'>{esc(c['title'])}</strong>"
                      f"<span class='level {cls}'>{esc(level)}</span></span><span class='small'>{esc(c.get('description', ''))}</span>"
                      f"<span class='meter'><span style='width:{s['score'] or 0}%'></span></span>"
                      f"<span class='small'>{len(c['units'])} units · {count} assignment{'' if count == 1 else 's'} · {s['answered'] or 'no'} answers scored</span>"
                      "<span class='go'>Open</span></a>")
        body = (
            f"<div class='cards'>{cards}</div><div class='cols' style='margin-top:20px'><section class='panel wide'><h2>New course</h2>"
            "<form method='post' action='/courses'><label for='title'>Course title</label><input type='text' id='title' name='title' required maxlength='80'>"
            "<label for='description'>What it covers</label><input type='text' id='description' name='description' maxlength='240'>"
            "<label for='units'>Units, one per line, in order</label>"
            "<textarea id='units' name='units' rows='5' placeholder='Numbers and fractions&#10;Measurement'></textarea>"
            "<button>Add course</button></form></section>"
            "<aside class='col' style='flex:1 1 260px'><section class='panel'><h2>Starter courses</h2><p class='small'>They come from "
            "<code>meshlearn/courses.json</code>. Anything you change here is kept.</p>"
            "<form method='post' action='/courses/reload'><button class='secondary'>Reload from the file</button></form></section></aside></div>"
        )
        n = len(self.courses())
        return Response(self._page("Courses", body, sub=f"{n} course{'' if n == 1 else 's'}. Assignments go in their units."))

    def course_create(self, req: Request) -> Response:
        units = [{"title": line} for line in req.form.get("units", "").splitlines() if line.strip()]
        try:
            course = self.save_course({"title": req.form.get("title", ""), "description": req.form.get("description", ""),
                                       "units": units})
        except crs.CourseError:
            return redirect("/courses")
        return redirect(f"/courses/{course['id']}")

    def courses_reload(self, req: Request) -> Response:
        self.load_course_file()
        return redirect("/courses")

    def course_page(self, req: Request) -> Response:
        cid = req.params["cid"]
        course = crs.OTHER if cid == "_other" else self.get_course(cid)
        if course is None:
            return Response(self._page("Not found", "<p>No such course.</p>"), status=404)
        summary = self.course_summary(course)
        by_unit = {u["id"]: u for u in summary["units"]}

        def rows(items: List[dict]) -> str:
            return "".join(f"<a href='/assignments/{esc(a['id'])}'><span class='grow'><strong>{esc(a['title'])}</strong><span class='small'>"
                           f"{'practice' if a.get('mode') == 'practice' else 'due ' + esc(a['due']) if a.get('due') else 'no due date'}</span></span></a>" for a in items)

        def chip(u: dict) -> str:
            cls = {"Strong": "strong", "Good": "good", "Developing": "developing", "Needs work": "needs"}.get(u["level"], "")
            return f"<span class='level {cls}'>{esc(u['level'] if u['score'] is None else u['level'] + ' · ' + str(u['score']) + '%')}</span>"

        units_html = ""
        for n, unit in enumerate(course["units"], start=1):
            items = self.assignments_in(course["id"], unit["id"])
            u = by_unit.get(unit["id"], {"level": "Not started", "score": None, "answered": 0})
            units_html += (
                f"<section aria-labelledby='u{n}'><div style='display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin:20px 0 8px'>"
                f"<h2 id='u{n}' style='margin:0'>Unit {n}: {esc(unit['title'])}</h2>{chip(u)}<span class='small'>{u['answered']} answers scored</span></div>"
                f"<div class='list'>{rows(items)}<a href='/assignments/new?course={esc(course['id'])}&amp;unit={esc(unit['id'])}' style='color:var(--accent)'>"
                "+ New assignment in this unit</a></div></section>")
        loose = [a for a in self.assignments_in(course["id"]) if a.get("unit", "") not in {u["id"] for u in course["units"]}]
        if loose:
            units_html += f"<section><h2>Not in a unit</h2><div class='list'>{rows(loose)}</div></section>"
        editor = ""
        if cid != "_other":
            fields = [(n, u["id"], u["title"], u.get("read", "")) for n, u in enumerate(course["units"], start=1)] + [("new", "", "", "")] * 2
            lines = "".join(
                f"<tr><td>{n}</td><td><input type='hidden' name='unit_id' value='{esc(uid)}'>"
                f"<input type='text' name='unit_title' value='{esc(title)}' aria-label='{'New unit' if n == 'new' else f'Unit {n}'} title'></td>"
                f"<td><input type='text' name='unit_read' value='{esc(read)}' aria-label='{'New unit' if n == 'new' else f'Unit {n}'} library search'></td></tr>"
                for n, uid, title, read in fields)
            editor = (
                "<details class='panel' style='margin-top:24px'><summary><strong>Edit this course</strong></summary><form method='post'>"
                f"<label for='title'>Title</label><input type='text' id='title' name='title' value='{esc(course['title'])}' required>"
                f"<label for='description'>What it covers</label><input type='text' id='description' name='description' value='{esc(course.get('description', ''))}'>"
                f"<label class='option'><input type='checkbox' name='comingSoon' value='1'{' checked' if course.get('comingSoon') else ''}> Coming later (shown greyed out)</label>"
                "<div class='scroll'><table><tr><th>#</th><th>Unit title (clear it to remove an empty unit)</th><th>Library search for readings</th></tr>"
                f"{lines}</table></div><button>Save course</button></form>"
                + ("<p class='muted'>This course comes from the course file. Saving here keeps the hub's version from now on.</p>"
                   if course.get("source") == "file" else "") + "</details>")
        action = (f"<span><a class='button secondary' href='/comprehension/{esc(course['id']) or '_other'}'>Class progress</a> "
                  + (f"<a class='button' href='/assignments/new?course={esc(course['id'])}'>+ New assignment</a>" if cid != "_other" else "") + "</span>")
        body = f"<p>{chip(summary)} <span class='small'>{summary['answered']} answers scored</span></p>" + units_html + editor
        return Response(self._page(course["title"], body, current="courses", sub=course.get("description", ""), action=action))

    def course_update(self, req: Request) -> Response:
        cid = req.params["cid"]
        course = self.get_course(cid)
        if course is None:
            return redirect("/courses")
        in_use = {a.get("unit") for a in self.assignments_in(cid)}
        units = []
        for uid, title, read in zip(req.form_list("unit_id"), req.form_list("unit_title"), req.form_list("unit_read")):
            if not title.strip():
                if uid in in_use:  # a unit with assignments can't vanish
                    old = crs.unit_of(course, uid)
                    units.append(dict(old) if old else {"id": uid, "title": uid})
                continue
            units.append({"id": uid or title, "title": title, "read": read})
        try:
            self.save_course({"id": cid, "title": req.form.get("title", ""), "description": req.form.get("description", ""),
                              "comingSoon": bool(req.form.get("comingSoon")), "units": units})
        except crs.CourseError:
            pass
        return redirect(f"/courses/{cid}")

    # -- learning data ----------------------------------------------------------

    def _secret(self, key: str) -> str:
        value = self.setting(key)
        if not value:
            value = secrets.token_hex(16)
            self.set_setting(key, value)
        return value

    def pseudonym(self, name: str, prefix: str = "s") -> str:
        digest = hashlib.sha256((self._secret("pseudonym_salt") + "|" + name).encode("utf-8")).hexdigest()
        return f"{prefix}-{digest[:10]}"

    def learning_record(self) -> dict:
        records = self.comprehension_records()
        versions = {}
        for r in records:
            a = self.get_assignment(r["aid"], r["version"])
            versions[(r["aid"], r["version"])] = a
        submissions = []
        for r in records:
            imported = r["source"].startswith("import")
            submissions.append({
                "id": r["sub_id"], "assignment": r["aid"], "version": r["version"],
                "student": r["student"] if imported else self.pseudonym(r["student"]),
                "phone": r["phone"] if imported else self.pseudonym(r["phone"], "p"),
                "submitted": datetime.datetime.utcfromtimestamp(r["at"]).isoformat() + "Z" if r["at"] else None,
                "duration_s": r["duration"], "answers": r["answers"], "scores": r["scores"],
                "complete": r["complete"],
                "mistakes": {q["id"]: k for q in r["questions"]
                             for k in [cmp.diagnose(q, r["answers"].get(q["id"], ""), r["scores"].get(q["id"]))] if k},
                "wrong_notes": {q["id"]: n for q in r["questions"] for n in [asg.wrong_note(q, r["answers"].get(q["id"], ""))] if n},
                "not_sure": sorted(r["unsure"]), "first_answers": r["first"],
                "pace": cmp.pace(r), "source": r["source"],
            })
        return {
            "format": RECORD_FORMAT, "version": RECORD_VERSION, "encoding": "none",
            "exported": datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "app": __version__, "hub": self._secret("hub_id")[:12],
            "dictionary": RECORD_DICTIONARY,
            "mistake_kinds": {k: {"area": a, "label": label} for k, (a, label) in cmp.KINDS.items()},
            "courses": self.course_list_for_phones(),
            "assignments": [dict(a, version=v) for (aid, v), a in versions.items() if a],
            "submissions": submissions,
        }

    def data_export(self, req: Request) -> Response:
        stamp = time.strftime("%Y-%m-%d")
        return Response(json.dumps(self.learning_record(), ensure_ascii=False, indent=1),
                        content_type="application/json; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=meshlearn-learning-data-{stamp}.json"})

    def import_learning_record(self, data: dict) -> Dict[str, int]:
        """Add a file's data (never deletes; submissions already here are skipped)."""
        if not isinstance(data, dict) or data.get("format") != RECORD_FORMAT:
            raise ValueError("not a MeshLearn learning-data file")
        if int(data.get("version", 0)) > RECORD_VERSION:
            raise ValueError("this file is from a newer MeshLearn; update this hub first")
        source = f"import:{str(data.get('hub', '?'))[:12]}"
        added = {"courses": 0, "assignments": 0, "submissions": 0, "skipped": 0}
        for c in data.get("courses", []):
            if self.get_course(str(c.get("id", ""))) is None:
                try:
                    course = crs.normalize_course(c)
                except crs.CourseError:
                    continue
                self.db.execute("INSERT INTO courses (id, data, source, position, updated) VALUES (?, ?, 'import', "
                                "(SELECT COALESCE(MAX(position), -1) + 1 FROM courses), ?)",
                                (course["id"], json.dumps(course), time.time()))
                added["courses"] += 1
        for a in data.get("assignments", []):
            aid, version = str(a.get("id", ""))[:20], str(a.get("version", ""))[:16]
            if aid and version and not self.db.one("SELECT 1 FROM assignment_versions WHERE aid = ? AND version = ?", (aid, version)):
                stored = {k: v for k, v in a.items() if k != "version"}
                self.db.execute("INSERT INTO assignment_versions (aid, version, data) VALUES (?, ?, ?)",
                                (aid, version, json.dumps(stored)))
                added["assignments"] += 1
        for s in data.get("submissions", []):
            sid = str(s.get("id", ""))[:40]
            if not sid or self.db.one("SELECT 1 FROM submissions WHERE sub_id = ?", (sid,)):
                added["skipped"] += 1
                continue
            a = self.get_assignment(str(s.get("assignment", "")), str(s.get("version", "")))
            if not a:
                added["skipped"] += 1
                continue
            scores = s.get("scores") or {}
            questions = [{"id": q["id"], "score": scores.get(q["id"]), "points": q["points"]} for q in a["questions"]]
            grade = {"score": round(sum(q["score"] or 0 for q in questions), 2), "max": sum(q["points"] for q in a["questions"]),
                     "complete": all(q["score"] is not None for q in questions), "questions": questions}
            try:
                when = datetime.datetime.fromisoformat(str(s.get("submitted")).rstrip("Z")).replace(
                    tzinfo=datetime.timezone.utc).timestamp()
            except ValueError:
                when = None
            signals = asg.signals_of(a, set(s.get("not_sure") or []), json.dumps(s.get("first_answers") or {}), s.get("answers") or {})
            self.db.execute(
                "INSERT INTO submissions (sub_id, assignment_id, version, student, phone, answers, submitted_at, received_at, "
                "grade, duration, source, signals) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, a["id"], str(s.get("version", "")), str(s.get("student", "?"))[:60], str(s.get("phone", "?"))[:60],
                 json.dumps(s.get("answers") or {}), when, time.time(), json.dumps(grade), s.get("duration_s"), source,
                 json.dumps(signals) if signals else None),
            )
            added["submissions"] += 1
        return added

    def data_import(self, req: Request) -> Response:
        try:
            result = self.import_learning_record(req.json())
        except (ValueError, TypeError, KeyError) as exc:
            return json_response({"error": str(exc)}, status=400)
        return json_response(result)

    def learning_data_panel(self) -> str:
        n = self.db.one("SELECT COUNT(*) AS n FROM submissions")["n"]
        graded = self.db.one("SELECT COUNT(*) AS n FROM submissions WHERE grade IS NOT NULL")["n"]
        imported = self.db.one("SELECT COUNT(*) AS n FROM submissions WHERE source LIKE 'import%'")["n"]
        return (
            "<h2>Learning data</h2><div class='card'>"
            "<p>Everything this hub has gathered: every submission with what was typed, the scores, the kind of each "
            "mistake and how long it took, in one anonymous file. Student and phone names are replaced by pseudonyms. "
            "Import adds another hub's file to this one (nothing is deleted; submissions already here are skipped).</p>"
            f"<p><strong>This hub holds {n} submission{'' if n == 1 else 's'} ({graded} graded"
            f"{f', {imported} imported' if imported else ''}).</strong></p>"
            "<a class='button' href='/data/export.json'>Export all data</a>"
            "<label class='button secondary' for='import-file' style='margin-left:8px'>Import data…</label>"
            "<input type='file' id='import-file' accept='application/json,.json' style='display:none'>"
            "<p id='import-status' role='status' class='muted'></p></div>"
            "<script>document.getElementById('import-file').addEventListener('change', async e => {"
            "const f = e.target.files[0]; if (!f) return; const out = document.getElementById('import-status');"
            "out.textContent = 'Importing…';"
            "const res = await fetch('/api/data/import', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: await f.text()});"
            "const d = await res.json();"
            "out.textContent = res.ok ? `Added ${d.submissions} submissions, ${d.assignments} assignment versions and ${d.courses} courses `"
            " + `(${d.skipped} already here or unreadable). Reload to see them.` : 'Could not import: ' + d.error; });</script>"
        )
