"""Teacher hub pages about how the class did: one assignment, question by question, and class progress.

They show where students went wrong: which wrong answers they gave, what the teacher says those show, who was
not sure, and who changed a right answer. Mixed into Hub, like the other page files.
"""

from __future__ import annotations

import copy
import json
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from . import assignments as asg
from . import comprehension as cmp
from . import courses as crs
from . import protocol
from .web import Request, Response, esc, info_panel, redirect

IDLE_DAYS = 7
PRACTICE_MAX = 8


def _names(names: List[str], most: int = 12) -> str:
    more = len(names) - most
    return ", ".join(names[:most]) + (f" and {more} more" if more > 0 else "")


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _meter(n: int, total: int, cls: str = "") -> str:
    return f"<span class='meter {cls}' aria-hidden='true'><span style='width:{round(100 * n / total) if total else 0}%'></span></span>"


def level_chip(level: str, score: Optional[int]) -> str:
    cls = {"Strong": "strong", "Good": "good", "Developing": "developing", "Needs work": "needs"}.get(level, "")
    return f"<span class='level {cls}'>{esc(level if score is None else f'{level} {score}%')}</span>"


class HubClass:
    def _class_routes(self) -> None:
        app = self.app
        app.add_route("GET", "/assignments/<aid>", self.assignment_detail)
        app.add_route("POST", "/assignments/<aid>/why", self.why_save)
        app.add_route("GET", "/comprehension", self.comprehension_home)
        app.add_route("GET", "/comprehension/<cid>", self.comprehension_page)

    # -- one assignment -----------------------------------------------------------

    def _assignment_records(self, aid: str, version: str) -> List[dict]:
        return [r for r in self.comprehension_records() if r["aid"] == aid and r["version"] == version]

    def assignment_detail(self, req: Request) -> Response:
        aid = req.params["aid"]
        assignment = self.get_assignment(aid)
        if assignment is None:
            return Response(self._page("Not found", "<p>No such assignment.</p>"), status=404)
        version = self.current_version(aid)
        records = self._assignment_records(aid, version)
        report = cmp.question_report(assignment, records)
        meta = [self._placement(assignment) or assignment.get("subject") or "",
                "practice" if assignment["mode"] == "practice" else "graded",
                f"due {assignment['due']}" if assignment.get("due") else "no due date"]
        older = self.db.one("SELECT COUNT(*) AS n FROM submissions WHERE assignment_id = ? AND version IS NOT NULL AND version != '' "
                            "AND version != ?", (aid, version))["n"]
        notice = f"<div class='notice' role='status'>{esc(req.query['msg'])}</div>" if req.query.get("msg") else ""
        instructions = (f"<p class='said' style='font-size:16px'>{esc(assignment['instructions'])}</p>" if assignment.get("instructions") else "")
        body = (
            f"{notice}{instructions}<div class='cols'><div class='wide'>"
            + self._hardest(report) +
            "<h2>Question by question</h2>"
            + ("<p class='small'>A practice counts each student's first try.</p>" if assignment["mode"] == "practice" else "")
            + "".join(self._question_card(aid, n, item) for n, item in enumerate(report, start=1))
            + (f"<p class='small'>{_count(older, 'piece')} of work on an earlier version of this assignment "
               "is marked below but not counted here.</p>" if older else "")
            + f"</div><aside class='col' style='flex:1 1 280px'>{self._on_phones(aid, assignment, version)}{self._answers_card(assignment)}</aside></div>"
            + self._submissions_table(aid, version)
        )
        action = (f"<span><a class='button secondary' href='/assignments/{esc(aid)}/edit'>Edit</a> "
                  f"<a class='button secondary' href='/assignments/{esc(aid)}/grades.csv'>Download grades</a></span>")
        return Response(self._page(assignment["title"], body, current="assignments", sub=" · ".join(m for m in meta if m), action=action))

    @staticmethod
    def _hardest(report: List[dict]) -> str:
        """The top of the page: how many answered, and the wrong ideas most students share."""
        marked = [i for i in report if i["right"] or i["wrong"]]
        if not marked:
            return "<div class='panel'><strong>No marked work yet.</strong><p class='small' style='margin:4px 0 0'>When answers " \
                   "arrive, this shows which questions were hard and what went wrong.</p></div>"
        students = len({s for i in report for s in i["right"] + i["wrong"] + i["waiting"]})
        right = sum(sum(i["earned"]) for i in marked)
        total = sum(len(i["earned"]) * i["q"]["points"] for i in marked)
        ideas = sorted(((len(g["names"]), n, g) for n, i in enumerate(report, start=1) for g in i["groups"] if len(g["names"]) > 1),
                       key=lambda x: (-x[0], x[1]))[:3]
        rows = "".join(f"<li><strong>{n} students</strong> on question {qn}: "
                       + (esc(g["note"]) if g["note"] else f"answered “{esc(g['text'] or 'nothing')}”") + "</li>" for n, qn, g in ideas)
        guesses = sorted({s for i in report for s in i["unsure_right"]}, key=str.lower)
        away = sorted({s for i in report for s in i["changed_away"]}, key=str.lower)
        extra = ""
        if guesses:
            extra += f"<li>Right but not sure: {esc(_names(guesses))}</li>"
        if away:
            extra += f"<li>Had a right answer and changed it: {esc(_names(away))}</li>"
        return (f"<section class='panel' aria-labelledby='wrong'><h2 id='wrong'>Where the class went wrong</h2>"
                f"<p style='margin:0 0 8px'>{_count(students, 'student')} · {round(100 * right / total) if total else 0}% of the points</p>"
                + (f"<ul style='margin:0;padding-left:20px'>{rows}{extra}</ul>" if rows or extra else
                   "<p class='small' style='margin:0'>No wrong answer was shared by two or more students.</p>") + "</section>")

    def _question_card(self, aid: str, n: int, item: dict) -> str:
        q = item["q"]
        marked = len(item["right"]) + len(item["wrong"])
        kind = {"choice": "Choice", "number": "Number", "text": "Short answer", "long": "Written"}[q["type"]]
        head = f"{len(item['right'])} of {marked} right" if marked else "no answers yet"
        bar, total = len(item["right"]), marked
        if q["type"] == "long" and marked:   # written answers earn part marks, so "how many were perfect" says little
            bar, total = sum(item["earned"]), marked * q["points"]
            head = f"average {bar / marked:.1f} of {q['points']:g}"
        hard = marked >= 2 and bar * 2 < total
        inner = f"<div class='small'>{kind} · {q['points']:g} point{'' if q['points'] == 1 else 's'}</div>{info_panel(q)}"
        key = asg.key_text(q)
        if key:
            inner += f"<p style='margin:6px 0'><span class='good'><strong>Right answer:</strong></span> {esc(key)}"
            inner += f" <span class='small'>{esc(_names(item['right']))}</span></p>" if item["right"] else "</p>"
        for g in item["groups"]:
            inner += self._wrong_group(aid, q, g, marked)
        if q["type"] == "long":
            if item["right"]:
                inner += f"<p class='small' style='margin:6px 0'>Full marks: {esc(_names(item['right']))}</p>"
            if item["wrong"]:
                inner += f"<p class='small' style='margin:6px 0'>Lost marks: {esc(_names(item['wrong']))}</p>"
        for label, names in (("Right, but not sure", item["unsure_right"]), ("Wrong, and knew they were not sure", item["unsure_wrong"]),
                             ("Had it right, then changed it", item["changed_away"]), ("Changed a wrong answer to the right one", item["fixed"])):
            if names:
                inner += f"<p class='sig'><strong>{label}:</strong> {esc(_names(names))}</p>"
        if item["waiting"]:
            inner += f"<p class='sig'><a href='/marking'>{_count(len(item['waiting']), 'answer')} to mark</a>: {esc(_names(item['waiting']))}</p>"
        return (f"<details class='qcard{' hard' if hard else ''}' id='q-{esc(q['id'])}'{' open' if hard else ''}><summary>"
                f"<span class='qn'>{n}</span><span class='grow'><strong>{esc(q['prompt'])}</strong>"
                f"{_meter(bar, total, 'ok') if marked else ''}</span><span class='small nowrap'>{head}</span></summary>"
                f"<div class='qin'>{inner}</div></details>")

    @staticmethod
    def _wrong_group(aid: str, q: dict, g: dict, marked: int) -> str:
        """One wrong answer: how many gave it, who, and what the teacher says it shows (editable here)."""
        kind = cmp.KINDS.get(g["kind"] or "", ("", ""))[1]
        guess = f"<span class='small'>{esc(kind)}</span>" if kind and g["kind"] in ("sign", "place", "rounding", "inverted") else ""
        label = "Change note" if g["note"] else "Say what it shows"
        form = (f"<details class='why'><summary>{label}</summary><form method='post' action='/assignments/{esc(aid)}/why' class='inline'>"
                f"<input type='hidden' name='q' value='{esc(q['id'])}'><input type='hidden' name='answer' value='{esc(g['answer'])}'>"
                f"<input type='text' name='note' maxlength='{asg.MAX_WHY_CHARS}' value='{esc(g['note'])}' "
                "placeholder='e.g. added tops and bottoms' aria-label='What this answer shows'>"
                "<button class='secondary'>Save</button></form></details>") if g["answer"] else ""
        return (f"<div class='wrongans'><div class='row'><span class='badge'>{len(g['names'])}</span><span class='grow'>"
                f"<span>{esc(g['text']) or '<em class=muted>left blank</em>'}"
                + (f" <strong class='note'>{esc(g['note'])}</strong>" if g["note"] else "") + f" {guess}</span>"
                f"<span class='small'>{esc(_names(g['names']))}</span></span></div>{_meter(len(g['names']), marked, 'bad')}{form}</div>")

    def why_save(self, req: Request) -> Response:
        """Say what a wrong answer shows, from the class view. Notes stay here, so nothing is sent to phones."""
        aid, qid = req.params["aid"], req.form.get("q", "")
        a = self.get_assignment(aid)
        q = next((q for q in a["questions"] if q["id"] == qid), None) if a else None
        if not q:
            return redirect("/assignments")
        answer, note = req.form.get("answer", ""), req.form.get("note", "").strip()
        if q["type"] == "choice" and answer.isdigit() and int(answer) < len(q["options"]):
            why = q.get("why") or [""] * len(q["options"])
            why[int(answer)] = note
            q["why"] = why
        elif q["type"] in ("number", "text"):
            q["why"] = [w for w in q.get("why") or [] if not asg.same_answer(q, w["x"], answer)] + ([{"x": answer, "w": note}] if note else [])
        self.create_assignment(a)
        return redirect(f"/assignments/{quote(aid)}#q-{quote(qid)}")

    def _on_phones(self, aid: str, assignment: dict, version: str) -> str:
        full = protocol.frame_count(protocol.ASSIGN, {"a": self.packed_for_phone(aid), "o": 1, "d": assignment.get("due", "")})
        phones = self.db.all("SELECT * FROM phones ORDER BY name")
        holdings = {h["node_id"]: h for h in self.db.all("SELECT * FROM holdings WHERE aid = ?", (aid,))}
        if not phones:
            return "<section class='panel'><h2>On student phones</h2><p class='muted'>Add a phone first on the <a href='/phones'>Phones</a> page.</p></section>"
        rows = ""
        for p in phones:
            h = holdings.get(p["node_id"])
            has = "has it" if h and h["version"] == version else "has an older version" if h and h["version"] else "doesn't have it"
            state = "open for students" if h and h["open"] else "not shown"
            if h and h["msg_id"]:
                status = self.messenger.message_status(h["msg_id"])
                if status in ("pending", "failed"):
                    state += " · sending" if status == "pending" else " · not delivered"
            cost = "1 packet" if h and h["version"] == version else f"{full} packets"
            node = f"<input type='hidden' name='node_id' value='{esc(p['node_id'])}'>"
            rows += (f"<div><span class='grow'><strong>{esc(p['name'])}</strong><span class='small'>{has} · {state}</span></span>"
                     f"<form method='post' action='/assignments/{esc(aid)}/open'>{node}<button class='secondary tiny'>"
                     f"{'Resend' if h and h['open'] else 'Open'} ({cost})</button></form>"
                     + (f"<form method='post' action='/assignments/{esc(aid)}/close'>{node}<button class='secondary tiny'>Hide</button></form>"
                        if h and h["open"] else "") + "</div>")
        everyone = (f"<form method='post' action='/assignments/{esc(aid)}/open'><input type='hidden' name='node_id' value='*'>"
                    "<button>Open on every phone</button></form>") if len(phones) > 1 else ""
        return (f"<h2 style='margin-top:0'>On student phones</h2><div class='list'>{rows}</div>{everyone}"
                f"<p class='small'>Opening costs 1 radio packet on phones that already have this version; otherwise the whole "
                f"assignment goes by radio ({_count(full, 'packet')}).</p>")

    def _submissions_table(self, aid: str, version: str) -> str:
        names = self._phone_names()
        rows = ""
        for s in self.db.all("SELECT * FROM submissions WHERE assignment_id = ? ORDER BY received_at DESC", (aid,)):
            grade = json.loads(s["grade"]) if s["grade"] else None
            score = ("<span class='muted'>—</span>" if grade is None else f"{grade['score']:g} / {grade['max']:g}" if grade["complete"] else
                     f"<span class='tomark'>{grade['score']:g} / {grade['max']:g} so far · to mark</span>")
            old = " <span class='muted'>(earlier version)</span>" if s["version"] and s["version"] != version else ""
            a = self.get_assignment(aid, s["version"])
            unsure, first = asg.read_signals(a, json.loads(s["signals"] or "{}")) if a else (set(), {})
            signals = " · ".join(x for x in (f"not sure on {len(unsure)}" if unsure else "", f"changed {len(first)}" if first else "") if x)
            rows += (f"<tr><td><a href='/submissions/{esc(s['sub_id'])}'>{esc(s['student'])}</a></td><td>{esc(names.get(s['phone'], s['phone']))}</td>"
                     f"<td>{time.strftime('%b %d, %H:%M', time.localtime(s['submitted_at'])) if s['submitted_at'] else ''}</td>"
                     f"<td>{score}{old}</td><td class='small'>{signals}</td></tr>")
        return ("<h2>Submissions</h2>" + (f"<table class='plain'><tr><th>Student</th><th>Phone</th><th>Submitted</th><th>Score</th><th>Signals</th></tr>{rows}</table>"
                                          if rows else "<p class='muted'>Nothing yet.</p>"))

    # -- class progress -------------------------------------------------------------

    def comprehension_home(self, req: Request) -> Response:
        """Opens on the first course with answers."""
        records = self.comprehension_records()
        courses = [c for c in self.courses(include_other=True) if not c.get("comingSoon")]
        used = {r["course"] for r in records}
        cid = next((c["id"] for c in courses if c["id"] in used), courses[0]["id"] if courses else "")
        return self._progress(cid, records, req)

    def comprehension_page(self, req: Request) -> Response:
        cid = req.params["cid"]
        return self._progress("" if cid == "_other" else cid, self.comprehension_records(), req)

    def _progress(self, cid: str, records: List[dict], req: Request) -> Response:
        course = crs.OTHER if cid == "" else self.get_course(cid)
        if course is None:
            return Response(self._page("Not found", "<p>No such course.</p>"), status=404)
        s = cmp.summarize(records, course)
        tabs = "".join(f"<a href='/comprehension/{esc(c['id']) or '_other'}'{' aria-current=page' if c['id'] == cid else ''}>{esc(c['title'])}</a>"
                       for c in self.courses(include_other=True) if not c.get("comingSoon"))
        mine = [r for r in records if r["course"] == cid]
        sub = f"{course['title']} · from {_count(s['answered'], 'scored answer')}"
        if not s["students"]:
            body = (f"<nav class='tabs' aria-label='Course'>{tabs}</nav><div class='panel'><strong>No graded answers in this course yet.</strong>"
                    "<p class='small' style='margin:4px 0 0'>Levels, mistakes and timing show here once work arrives.</p></div>")
            return Response(self._page("Class progress", body, current="class", sub=sub))
        phones: Dict[str, str] = {}
        names = self._phone_names()
        for r in sorted(mine, key=lambda r: r["at"] or 0):
            phones[r["student"]] = names.get(r["phone"], r["phone"])
        units = s["units"]
        scored = [u for u in units if u["score"] is not None]
        chosen = req.query.get("unit", "")
        unit = next((u for u in units if u["id"] == chosen), None) or (min(scored, key=lambda u: u["score"]) if scored else units[0] if units else None)
        base = f"/comprehension/{quote(cid) or '_other'}"
        head = "".join(f"<th{' class=sel' if unit and u['id'] == unit['id'] else ''}><a href='{base}?{esc(urlencode({'unit': u['id']}))}'>"
                       f"{u['number']}. {esc(u['title'])}</a></th>" for u in units)
        rows = ""
        for student, st in s["students"].items():
            rows += (f"<tr><td><strong>{esc(student)}</strong></td>" + "".join(f"<td>{level_chip(u['level'], u['score'])}</td>" for u in st["units"])
                     + f"<td class='small'>{esc(phones.get(student, ''))}</td></tr>")
        foot = "".join(f"<td>{level_chip(u['level'], u['score'])}</td>" for u in units)
        body = (
            f"<nav class='tabs' aria-label='Course'>{tabs}</nav><div class='cols'>"
            "<section class='wide' aria-label='Each student by unit'><div class='scroll'><table class='plain grid2'><thead><tr><th>Student</th>"
            f"{head}<th>Phone</th></tr></thead><tbody>{rows}</tbody><tfoot><tr><td><strong>Class</strong></td>{foot}<td>{level_chip(s['level'], s['score'])}</td></tr></tfoot>"
            "</table></div><p class='small'>A unit's level is the average of a student's 20 most recent scored questions in it: Strong 85%+, Good 65%+, "
            "Developing 40%+, Needs work below. Fewer than 3 answers is “Just started”.</p></section>"
            f"<aside class='col' style='flex:1 1 280px'>{self._mistakes_panel(cid, unit, mine)}{self._worth_a_look(mine, records)}{self._areas_panel(s)}</aside></div>"
        )
        return Response(self._page("Class progress", body, current="class", sub=sub))

    def _mistakes_panel(self, cid: str, unit: Optional[dict], records: List[dict]) -> str:
        if not unit:
            return ""
        top = cmp.common_mistakes([r for r in records if r.get("unit", "") == unit["id"]])[:5]
        if not top:
            return f"<section class='panel'><h2>Most common mistakes · {esc(unit['title'])}</h2><p class='small'>No mistakes recorded yet.</p></section>"
        most = top[0][2]
        rows = "".join(f"<div class='mist'><span class='row'><span>{esc(cmp.mistake_label(kind, value))}</span><strong>{n}</strong></span>"
                       f"{_meter(n, most, 'bad')}</div>" for kind, value, n in top)
        # The most common mistake that says what to practise; "wrong choice" alone doesn't, so then the whole unit.
        named = next(((k, v) for k, v, _ in top if k == "note" or v in ("sign", "place", "rounding", "inverted")), None)
        kind, value = named or ("unit", unit["title"])
        link = "/assignments/new?" + urlencode({"course": cid, "unit": unit["id"], "practice": f"{kind}:{value}"})
        label = cmp.mistake_label(kind, value) if named else unit["title"]
        return (f"<section class='panel' aria-labelledby='mist'><h2 id='mist'>Most common mistakes · {esc(unit['title'])}</h2>{rows}"
                f"<a class='button secondary' style='display:block;text-align:center;margin:12px 0 0' href='{esc(link)}'>Make a practice on “{esc(label[:40])}{'…' if len(label) > 40 else ''}”</a>"
                "</section>")

    def _worth_a_look(self, mine: List[dict], everything: List[dict]) -> str:
        """Signals about single students: never proof, so worded as something to ask about."""
        now = time.time()
        found: List[Tuple[str, List[str]]] = []
        by_student: Dict[str, List[dict]] = {}
        for r in mine:
            by_student.setdefault(r["student"], []).append(r)
        last = {}
        for r in everything:
            if not r["source"].startswith("import") and r["at"]:
                last[r["student"]] = max(r["at"], last.get(r["student"], 0))
        seen = {p["node_id"]: p for p in self.db.all("SELECT * FROM phones")}
        for student in sorted(by_student, key=str.lower):
            recs = by_student[student]
            pace: Dict[str, int] = {}
            for r in recs:
                sig = cmp.pace(r)
                if sig:
                    pace[sig] = pace.get(sig, 0) + 1
            lines: List[str] = []
            if pace.get("rushing"):
                lines.append(f"rushed {_count(pace['rushing'], 'piece')} of work (fast and wrong)")
            if pace.get("fast_right"):
                lines.append(f"was very fast and right {_count(pace['fast_right'], 'time')}")
            if pace.get("slow"):
                lines.append(f"took much longer than expected {_count(pace['slow'], 'time')} (stuck?)")
            guessed = away = 0
            for r in recs:
                for q in r["questions"]:
                    right = (r["scores"].get(q["id"]) or 0) >= q["points"]
                    guessed += right and q["id"] in r["unsure"]
                    first = r["first"].get(q["id"])
                    away += bool(first is not None and not right and (asg.grade_question(q, first) or 0) >= q["points"])
            if away:
                lines.append(f"changed {_count(away, 'right answer')} to a wrong one")
            if guessed >= 3:
                lines.append(f"got {guessed} right without being sure")
            if student in last and now - last[student] > IDLE_DAYS * 86400:
                phone = seen.get(recs[-1]["phone"])
                quiet = (f" (their phone, {phone['name']}, has not been heard for {int((now - phone['last_seen']) // 86400)} days)"
                         if phone and phone["last_seen"] and now - phone["last_seen"] > 2 * 86400 else "")
                lines.append(f"has not sent work for {int((now - last[student]) // 86400)} days{quiet}")
            if lines:
                found.append((student, lines))
        if not found:
            return ""
        found.sort(key=lambda f: -len(f[1]))   # the students with most to ask about first
        shown = "".join(f"<p><strong>{esc(s)}</strong> {esc('; '.join(t))}.</p>" for s, t in found[:8])
        more = f"<p class='small'>and {_count(len(found) - 8, 'more student')}</p>" if len(found) > 8 else ""
        return f"<section class='panel look' aria-labelledby='look'><h2 id='look'>Worth a look</h2>{shown}{more}<p class='small'>Signals, never proof.</p></section>"

    @staticmethod
    def _areas_panel(s: dict) -> str:
        areas = s["mistakes"]["areas"]
        total = sum(areas.values())
        if not total:
            return ""
        rows = "".join(f"<li><strong>{esc(cmp.AREAS[a]['label'])}</strong> {n} ({round(100 * n / total)}%)<div class='small'>{esc(cmp.AREAS[a]['about'])}</div></li>"
                       for a, n in sorted(areas.items(), key=lambda kv: -kv[1]))
        kinds = "".join(f"<li>{esc(cmp.KINDS[k][1])}: {n}</li>" for k, n in sorted(s["mistakes"]["kinds"].items(), key=lambda kv: -kv[1]))
        return (f"<details class='panel'><summary><strong>Where the mistakes are, whole course</strong></summary><ul class='small'>{rows}</ul>"
                f"<ul class='small'>{kinds}</ul></details>")

    # -- a practice on a mistake ------------------------------------------------------

    def practice_draft(self, cid: str, unit_id: str, wanted: str) -> Tuple[dict, str]:
        """Questions students lost marks on through one mistake, ready to edit into a practice. A practice carries its
        answer key to phones, so questions whose answers must still stay on the hub are left out."""
        kind, _, value = wanted.partition(":")
        counts: Dict[str, int] = {}
        questions: Dict[str, dict] = {}
        held = set()
        for r in self.comprehension_records():
            if r["course"] != cid or r["unit"] != unit_id:
                continue
            for q in r["questions"]:
                answer = r["answers"].get(q["id"], "")
                found = cmp.diagnose(q, answer, r["scores"].get(q["id"]))
                note = asg.wrong_note(q, answer) if found else ""
                if not found or q["type"] == "long" or (kind == "note" and note != value) or (kind == "kind" and (note or found != value)):
                    continue
                current = self.get_assignment(r["aid"])
                if current and current["mode"] != "practice" and not self._answers_allowed(current):
                    held.add((r["aid"], q["id"]))
                    continue
                key = q["prompt"]
                counts[key] = counts.get(key, 0) + 1
                questions.setdefault(key, q)
        picked = sorted(questions, key=lambda k: -counts[k])[:PRACTICE_MAX]
        label = value if kind == "unit" else cmp.mistake_label(kind, value)
        draft = {"title": f"Practice: {label}"[:120], "mode": "practice", "course": cid, "unit": unit_id,
                 "questions": [copy.deepcopy(questions[k]) for k in picked]}
        msg = (f"These are questions students missed this way. Change the numbers or words so it is new practice." if picked else
               "Add your own questions here.")
        if held:
            msg += (f" {_count(len(held), 'question')} {'was' if len(held) == 1 else 'were'} left out: a practice sends its right answers to phones, "
                    "and those assignments are not past their due date.")
        return draft, msg
