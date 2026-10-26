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
from . import comprehension as cmp
from . import courses as crs
from .web import Request, Response, esc, json_response, redirect

LEVEL_CLASS = {"Strong": "good", "Good": "good", "Developing": "warn", "Needs work": "bad",
               "Just started": "muted", "Not started": "muted"}

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
    "submissions.pace": "'rushing', 'fast_right', 'slow' or null: a signal worth a look, never proof.",
    "mistake_kinds": "Every mistake kind, its area and what it means.",
}


def _level_pill(level: str, score: Optional[int]) -> str:
    text = level if score is None else f"{level} · {score}%"
    return f"<span class='pill {LEVEL_CLASS.get(level, '')}'>{esc(text)}</span>"


def _bar(score: Optional[int]) -> str:
    width = score or 0
    return (f"<div style='height:10px;border-radius:5px;background:var(--line);overflow:hidden;margin:8px 0' "
            f"role='img' aria-label='{width}%'><div style='height:100%;width:{width}%;background:var(--accent)'></div></div>")


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
        app.add_route("GET", "/comprehension", self.comprehension_home)
        app.add_route("GET", "/comprehension/<cid>", self.comprehension_page)
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
            records.append({
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

    def _course_cards(self, records: List[dict], link: str = "/courses/") -> str:
        cards = ""
        for c in self.courses(include_other=True):
            summary = cmp.summarize(records, c)
            count = len(self.assignments_in(c["id"]))
            soon = c.get("comingSoon")
            pace = summary["pace"]
            flags = "".join(f"<div class='warn' style='font-size:15px'>{n}× {esc(cmp.PACE_LABELS[k])}</div>"
                            for k, n in pace.items())  # lines, not pills: long labels must wrap in narrow cards
            cards += (
                f"<div class='card'{' style=opacity:.6' if soon else ''}>"
                f"<h2 style='margin-top:0'><a href='{link}{esc(c['id']) or '_other'}'>{esc(c['title'])}</a></h2>"
                f"<p class='muted'>{esc(c.get('description', ''))}</p>"
                + ("<span class='pill'>Coming later</span>" if soon else
                   f"<p>{len(c['units'])} units · {count} assignment{'' if count == 1 else 's'}</p>"
                   f"{_level_pill(summary['level'], summary['score'])}{_bar(summary['score'])}"
                   f"<p class='muted' style='margin-bottom:4px'>{summary['answered'] or 'No'} answers scored</p>{flags}")
                + "</div>"
            )
        return f"<div class='grid'>{cards}</div>"

    def courses_page(self, req: Request) -> Response:
        records = self.comprehension_records()
        body = (
            "<p class='muted'>Courses and their numbered units are the frame for assignments. "
            "Starter courses come from <code>meshlearn/courses.json</code>; anything changed here is kept.</p>"
            + self._course_cards(records)
            + "<h2>New course</h2><form method='post' action='/courses' class='card'>"
            "<label for='title'>Course title</label><input type='text' id='title' name='title' required maxlength='80'>"
            "<label for='description'>What it covers</label><input type='text' id='description' name='description' maxlength='240'>"
            "<label for='units'>Units, one per line, in order</label>"
            "<textarea id='units' name='units' rows='5' placeholder='Numbers and fractions&#10;Measurement'></textarea>"
            "<button>Add course</button></form>"
            "<form method='post' action='/courses/reload'><button class='secondary'>Reload starter courses from the file</button></form>"
        )
        return Response(self._page("Courses", body))

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
        units_html = ""
        for n, unit in enumerate(course["units"], start=1):
            items = self.assignments_in(course["id"], unit["id"])
            links = "".join(f"<li><a href='/assignments/{esc(a['id'])}'>{esc(a['title'])}</a>"
                            f"{' <span class=muted>(practice)</span>' if a.get('mode') == 'practice' else ''}</li>" for a in items)
            u = by_unit.get(unit["id"], {"level": "Not started", "score": None, "answered": 0})
            units_html += (
                f"<div class='card'><h2 style='margin-top:0'>Unit {n}: {esc(unit['title'])}</h2>"
                f"{_level_pill(u['level'], u['score'])} <span class='muted'>{u['answered']} answers scored</span>"
                + (f"<ul>{links}</ul>" if links else "<p class='muted'>No assignments yet.</p>")
                + f"<a class='button secondary' href='/assignments/new?course={esc(course['id'])}&unit={esc(unit['id'])}'>"
                "+ New assignment in this unit</a></div>"
            )
        loose = [a for a in self.assignments_in(course["id"]) if a.get("unit", "") not in {u["id"] for u in course["units"]}]
        if loose:
            units_html += ("<div class='card'><h2 style='margin-top:0'>Not in a unit</h2><ul>"
                           + "".join(f"<li><a href='/assignments/{esc(a['id'])}'>{esc(a['title'])}</a></li>" for a in loose)
                           + "</ul></div>")
        editor = ""
        if cid != "_other":
            rows = "".join(
                f"<tr><td>{n}</td><td><input type='hidden' name='unit_id' value='{esc(u['id'])}'>"
                f"<input type='text' name='unit_title' value='{esc(u['title'])}' aria-label='Unit {n} title'></td>"
                f"<td><input type='text' name='unit_read' value='{esc(u.get('read', ''))}' aria-label='Unit {n} library search'></td></tr>"
                for n, u in enumerate(course["units"], start=1))
            rows += "".join(
                f"<tr><td>new</td><td><input type='hidden' name='unit_id' value=''>"
                f"<input type='text' name='unit_title' value='' aria-label='New unit title'></td>"
                f"<td><input type='text' name='unit_read' value='' aria-label='New unit library search'></td></tr>" for _ in range(2))
            editor = (
                "<h2>Edit course</h2><form method='post' class='card'>"
                f"<label for='title'>Title</label><input type='text' id='title' name='title' value='{esc(course['title'])}' required>"
                f"<label for='description'>What it covers</label><input type='text' id='description' name='description' value='{esc(course.get('description', ''))}'>"
                f"<label class='option'><input type='checkbox' name='comingSoon' value='1'{' checked' if course.get('comingSoon') else ''}> Coming later (shown greyed out)</label>"
                "<table><tr><th>#</th><th>Unit title (clear it to remove an empty unit)</th><th>Library search for readings</th></tr>"
                f"{rows}</table><button>Save course</button></form>"
                + ("<p class='muted'>This course comes from the course file. Saving here keeps the hub's version from now on.</p>"
                   if course.get("source") == "file" else "")
            )
        body = (
            f"<p class='muted'>{esc(course.get('description', ''))}</p>"
            f"<p>{_level_pill(summary['level'], summary['score'])} <a href='/comprehension/{esc(course['id']) or '_other'}'>"
            "Comprehension by student</a></p>" + units_html + editor
        )
        return Response(self._page(course["title"], body))

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

    # -- comprehension ----------------------------------------------------------

    def comprehension_home(self, req: Request) -> Response:
        records = self.comprehension_records()
        body = ("<p class='muted'>Worked out from every graded answer: how well each course and unit is understood, "
                "and where the mistakes are. Practice tries count the way the sandbox counts them: right first time "
                "1.0, on the second try 0.6, later 0.3.</p>" + self._course_cards(records, link="/comprehension/"))
        return Response(self._page("Comprehension", body))

    def comprehension_page(self, req: Request) -> Response:
        cid = req.params["cid"]
        course = crs.OTHER if cid == "_other" else self.get_course(cid)
        if course is None:
            return Response(self._page("Not found", "<p>No such course.</p>"), status=404)
        s = self.course_summary(course)
        units = s["units"]
        head = "".join(f"<th>Unit {u['number']}<div class='muted'>{esc(u['title'])}</div></th>" for u in units)
        overall = "".join(f"<td>{_level_pill(u['level'], u['score'])}</td>" for u in units)
        rows = f"<tr><td><strong>Whole class</strong></td><td>{_level_pill(s['level'], s['score'])}</td>{overall}<td></td></tr>"
        for student, st in s["students"].items():
            cells = "".join(f"<td>{_level_pill(u['level'], u['score'])}</td>" for u in st["units"])
            flags = ", ".join(f"{n}× {cmp.PACE_LABELS[k]}" for k, n in st["pace"].items())
            rows += (f"<tr><td>{esc(student)}</td><td>{_level_pill(st['level'], st['score'])}</td>{cells}"
                     f"<td class='muted'>{esc(flags)}</td></tr>")
        areas = s["mistakes"]["areas"]
        total = sum(areas.values())
        mistake_rows = "".join(
            f"<tr><td>{esc(cmp.AREAS[a]['label'])}<div class='muted'>{esc(cmp.AREAS[a]['about'])}</div></td>"
            f"<td>{n}</td><td>{round(100 * n / total)}%</td></tr>"
            for a, n in sorted(areas.items(), key=lambda kv: -kv[1]))
        kind_rows = "".join(f"<li>{esc(cmp.KINDS[k][1])}: {n}</li>"
                            for k, n in sorted(s["mistakes"]["kinds"].items(), key=lambda kv: -kv[1]))
        body = (
            "<p class='muted'>A unit's level is the average of each student's 20 most recent scored questions in it. "
            "Levels: Strong 85%+, Good 65%+, Developing 40%+, Needs work below; fewer than 3 answers is "
            "“Just started”. Timing signals are worth a look, never proof.</p>"
            + (f"<div style='overflow-x:auto'><table><tr><th>Student</th><th>Course</th>{head}<th>Timing</th></tr>{rows}</table></div>"
               if s["students"] else "<p class='muted'>No graded answers in this course yet.</p>")
            + "<h2>Where the mistakes are</h2>"
            + (f"<table><tr><th>Area</th><th>Mistakes</th><th>Share</th></tr>{mistake_rows}</table><ul>{kind_rows}</ul>"
               if total else "<p class='muted'>No mistakes recorded yet.</p>")
        )
        return Response(self._page(f"{course['title']}: comprehension", body))

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
            self.db.execute(
                "INSERT INTO submissions (sub_id, assignment_id, version, student, phone, answers, submitted_at, received_at, "
                "grade, duration, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, a["id"], str(s.get("version", "")), str(s.get("student", "?"))[:60], str(s.get("phone", "?"))[:60],
                 json.dumps(s.get("answers") or {}), when, time.time(), json.dumps(grade), s.get("duration_s"), source),
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
