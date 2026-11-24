"""The teacher hub's look and its everyday pages, following the "Desktop: teacher" designs:
a sidebar on every page, Today (what needs the teacher), Marking (written answers one at a time),
Assignments, and Phones and radio. Also the example lessons a new hub can start from.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import socket
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode

from . import assignments as asg
from . import comprehension as cmp
from . import language as lang
from .web import CSS, MATH_JS, Request, Response, esc, info_panel, page, redirect
from .zipapps import QR_JS

HUB_CSS = """
svg.pic{width:56px;height:56px;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round;background:#fff;border-radius:12px}
svg.pic.mini{width:34px;height:34px;vertical-align:middle}
form.inline{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0 0}form.inline input[type=text],form.inline select{width:auto;flex:0 1 180px}form.inline button{margin:0}
dialog#picdlg{width:min(900px,94vw);max-height:86vh;border:1px solid var(--line);border-radius:14px;background:var(--card);color:var(--ink)}
.pichead{display:flex;gap:8px;position:sticky;top:0;background:var(--card)}.pichead button{margin:0}
.picgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px}.picgrid button{display:flex;flex-direction:column;align-items:center;gap:4px;margin:0;padding:8px 4px;background:transparent;color:var(--ink);border:1px solid var(--line);font-weight:400;font-size:13px}.picgrid button[hidden]{display:none}
:root{--sel:#e6eefa;--sel-ink:#173f80;--goodbg:#e9f3ec;--goodink:#154a27;--warnbg:#fdf3e2;--warnline:#e2b866;--warnink:#6b4600;--soft:#f1f1ec;--badbg:#fbe4e1;--badink:#8c1d16;--badbar:#b4532a}
@media (prefers-color-scheme:dark){:root{--sel:#1e2c45;--sel-ink:#cfe0ff;--goodbg:#1b3324;--goodink:#bfe8cc;--warnbg:#3a2e17;--warnline:#8a6a2a;--warnink:#f3d39a;--soft:#2a2a27;--badbg:#46201c;--badink:#ffb4ab;--badbar:#d98a5f}}
body{font-size:16px;line-height:1.45}
.shell{display:flex;min-height:100vh}
.side{flex:0 0 232px;background:var(--card);border-right:1px solid var(--line);padding:20px 14px;display:flex;flex-direction:column;gap:2px;position:sticky;top:0;height:100vh;overflow-y:auto}
.brand{font-size:18px;font-weight:700;padding:0 10px 14px;color:var(--ink);text-decoration:none}
.side nav{display:flex;flex-direction:column;gap:2px}
.side nav a{display:flex;align-items:center;gap:10px;min-height:42px;padding:0 10px;border-radius:10px;color:var(--ink);text-decoration:none}
.side nav a:hover{background:var(--soft)}
.side nav a[aria-current=page]{background:var(--sel);color:var(--sel-ink);font-weight:700}
.count{margin-left:auto;padding:0 8px;border-radius:99px;background:var(--warn);color:#fff;font-size:13px;font-weight:700}
.radiobox{margin-top:20px;padding:12px;border-radius:12px;font-size:14px;background:var(--goodbg);color:var(--goodink);display:flex;flex-direction:column;gap:2px}
.radiobox.off{background:var(--warnbg);color:var(--warnink)}
.radiobox a{color:inherit}
.dot{display:inline-block;width:8px;height:8px;border-radius:4px;background:currentColor;margin-right:6px}
.content{flex:1;min-width:0;padding:28px 32px 48px;max-width:1240px}
.top{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:12px}
.top h1{margin:0}
.top .button,.top button{margin:0}
.sub{color:var(--muted)}
h2{font-size:17px;margin:22px 0 10px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(240px,100%),1fr));gap:12px}
a.need{display:flex;flex-direction:column;gap:6px;padding:16px;border-radius:14px;background:var(--card);border:1px solid var(--line);color:var(--ink);text-decoration:none}
a.need:hover{border-color:var(--accent)}
a.need.urgent{border:2px solid var(--warnline)}
a.need .num{font-size:32px;font-weight:800;line-height:1.1}
a.need.urgent .num{color:var(--warnink)}
a.need .go{color:var(--accent);font-weight:700;margin-top:4px}
.cols{display:flex;flex-wrap:wrap;gap:20px;align-items:flex-start}
.cols>section,.cols>.col{flex:1 1 340px;min-width:0}
.cols>.wide{flex:3 1 460px}
.list{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow:hidden}
.list>a,.list>div{display:flex;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--line);color:var(--ink);text-decoration:none}
.list>a:hover{background:var(--soft)}
.list>a[aria-current=true]{background:var(--sel)}
.list>:last-child{border-bottom:0}
.grow{flex:1;min-width:0;display:flex;flex-direction:column;line-height:1.3}
.small{font-size:14px;color:var(--muted)}
.course{flex-direction:column!important;align-items:stretch!important;gap:6px!important}
.course .row{display:flex;justify-content:space-between;gap:8px}
.meter{display:block;height:8px;border-radius:4px;background:var(--line);overflow:hidden}
.meter span{display:block;height:100%;background:var(--accent)}
.level{padding:0 10px;border-radius:99px;font-size:14px;white-space:nowrap;background:var(--soft);color:var(--muted)}
.level.strong,.level.good{background:var(--goodbg);color:var(--goodink)}
.level.developing{background:var(--warnbg);color:var(--warnink)}.level.needs{background:var(--badbg);color:var(--badink)}
.meter.ok span{background:var(--good)}.meter.bad span{background:var(--badbar)}
.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 14px}.tabs a{display:flex;align-items:center;min-height:40px;padding:0 14px;border-radius:99px;border:1px solid var(--line);background:var(--card);color:var(--ink);text-decoration:none;font-size:15px}
.tabs a[aria-current=page]{background:var(--ink);color:var(--bg);border-color:var(--ink);font-weight:600}
.scroll{overflow-x:auto}table.grid2{margin:0;font-size:15px}table.grid2 td,table.grid2 th{vertical-align:middle;padding:8px}table.grid2 th a{color:inherit}table.grid2 th.sel{box-shadow:inset 0 -3px 0 var(--accent)}table.grid2 .level{display:inline-block;padding:3px 10px;border-radius:8px}table.grid2 tfoot td{border-top:2px solid var(--line);border-bottom:0}
aside.col .list>div{flex-wrap:wrap}aside.col .list .grow{flex:1 1 100%}aside.col .list form{margin:0}aside.col{display:flex;flex-direction:column;gap:12px}aside.col .panel h2{font-size:16px;margin:0 0 8px}.panel.look{background:var(--warnbg);border-color:var(--warnline)}.panel.look p{margin:0 0 6px;font-size:15px}
.mist{display:flex;flex-direction:column;gap:4px;margin:0 0 8px}.mist .row,.wrongans .row{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
details.qcard{background:var(--card);border:1px solid var(--line);border-radius:14px;margin:0 0 10px}details.qcard.hard{border:2px solid var(--warnline)}
details.qcard>summary{display:flex;align-items:center;gap:12px;padding:12px 16px;cursor:pointer;list-style:none}details.qcard>summary::-webkit-details-marker{display:none}details.qcard .grow{gap:6px}
.qn{flex:none;display:flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:14px;background:var(--soft);font-weight:800;font-size:14px}
.qin{padding:0 16px 14px 56px}.nowrap{white-space:nowrap}.sig{margin:8px 0 0;padding:8px 12px;border-radius:10px;background:var(--soft);font-size:15px}
.wrongans{margin:10px 0;display:flex;flex-direction:column;gap:5px}.badge{flex:none;min-width:28px;text-align:center;padding:2px 8px;border-radius:99px;background:var(--badbg);color:var(--badink);font-weight:700;font-size:14px}
strong.note{color:var(--badink);background:var(--badbg);border-radius:6px;padding:1px 8px;font-weight:600;font-size:15px}
details.why summary{cursor:pointer;color:var(--accent);font-size:14px}button.tiny{margin:0;padding:7px 10px;font-size:14px;white-space:nowrap}
@media (max-width:600px){.qin{padding-left:16px}}
.score{font-weight:700;white-space:nowrap}
.tomark{font-size:14px;font-weight:600;color:var(--warnink);white-space:nowrap}
.panel{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:20px 22px}
.panel h2:first-child,.panel h3:first-child{margin-top:0}
.marks{display:flex;gap:8px;flex-wrap:wrap;border:0;padding:0;margin:6px 0 0}
.marks label{margin:0;font-weight:inherit}
.marks input{position:absolute;opacity:0;width:1px;height:1px}
.marks span{display:flex;align-items:center;justify-content:center;min-width:64px;height:56px;padding:0 10px;border-radius:12px;border:2px solid var(--line);font-size:22px;font-weight:800;cursor:pointer;background:var(--card)}
.marks input:checked+span{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.marks input:focus-visible+span{outline:3px solid var(--accent);outline-offset:2px}
.said{padding:14px 16px;border-radius:10px;border:2px solid var(--line);font-size:18px;white-space:pre-line;margin:6px 0 0}
.model{padding:12px 14px;border-radius:10px;background:var(--soft);font-size:15px}
.phonecard{display:flex;flex-wrap:wrap;align-items:center;gap:14px;padding:16px;background:var(--card);border:1px solid var(--line);border-radius:14px;margin-bottom:12px}
.phonecard.quiet{background:var(--warnbg);border:2px solid var(--warnline)}
.phonecard .state{flex:none;width:12px;height:12px;border-radius:6px;background:var(--good)}
.phonecard.quiet .state{background:var(--warn)}
.phonecard.new .state{background:var(--muted)}
.phonecard .right{display:flex;flex-direction:column;font-size:14px;color:var(--muted);text-align:right}
.phonecard .tip{flex-basis:100%;font-size:15px}
.kv{display:flex;flex-wrap:wrap;gap:16px}
.kv>span{flex:1 1 140px;display:flex;flex-direction:column}
.kv .k{font-size:13px;color:var(--muted)}
.code{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:20px;font-weight:700;letter-spacing:.04em}
.hero{padding:24px;background:var(--card);border:1px solid var(--line);border-radius:16px}
.steps{list-style:none;padding:0;margin:16px 0 0;display:flex;flex-direction:column;gap:12px}
.steps li{display:flex;gap:14px;align-items:flex-start;padding:14px;border-radius:12px;background:var(--soft)}
.steps .n{flex:none;display:flex;align-items:center;justify-content:center;width:32px;height:32px;border-radius:16px;background:var(--accent);color:var(--accent-ink);font-weight:800}
.steps form,.steps .button{margin:8px 0 0}
.notice{padding:12px 16px;border-radius:12px;background:var(--goodbg);color:var(--goodink);margin:0 0 16px}
.notice.warn{background:var(--warnbg);color:var(--warnink)}
table.plain{background:var(--card);border:1px solid var(--line);border-radius:14px;border-collapse:separate;border-spacing:0;overflow:hidden}
table.plain th{font-size:14px;color:var(--muted);font-weight:600}
table.plain tr:last-child td{border-bottom:0}
@media (max-width:760px){.shell{flex-direction:column}.side{position:static;height:auto;flex:none;border-right:0;border-bottom:1px solid var(--line);padding:12px}
.side nav{flex-direction:row;flex-wrap:wrap}.brand{padding:0 6px 8px}.radiobox{margin-top:8px}.content{padding:16px;width:100%}table.plain{display:block;overflow-x:auto}}
"""

# The sidebar, in the designs' order. (key, label, link)
NAV = [("today", "Today", "/"), ("marking", "Marking", "/marking"), ("assignments", "Assignments", "/assignments"),
       ("students", "Students", "/students"), ("class", "Class progress", "/comprehension"), ("courses", "Courses", "/courses"),
       ("phones", "Phones and radio", "/phones"), ("library", "Library", "/library"), ("language", "Language", "/language")]
_TITLE_TO_NAV = {"Today": "today", "Welcome": "today", "Marking": "marking", "Assignments": "assignments",
                 "New assignment": "assignments", "Edit assignment": "assignments", "Class progress": "class",
                 "Courses": "courses", "Students": "students", "Phones and radio": "phones", "Card library": "library",
                 "Copy to memory card": "library", "Copying to the card": "library", "School language": "language"}

QUIET_S = 24 * 3600   # a phone not heard for a day needs a look

EXAMPLE_FILE = os.path.join(os.path.dirname(__file__), "sample_assignment.json")


def ago(ts: Optional[float], now: Optional[float] = None) -> str:
    """'just now', '25 min ago', '3 h ago', 'yesterday', 'Sat 14 Nov'."""
    if not ts:
        return "never"
    s = max(0, (now or time.time()) - ts)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 86400:
        return f"{int(s // 3600)} h ago"
    if s < 2 * 86400:
        return "yesterday"
    return time.strftime("%a %d %b", time.localtime(ts))


def _plural(n: int, word: str, many: str = "") -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _level_class(level: str) -> str:
    return {"Strong": "strong", "Good": "good", "Developing": "developing", "Needs work": "needs"}.get(level, "")


class HubUI:
    """Mixed into Hub: the page frame and the everyday teacher pages."""

    # -- frame --------------------------------------------------------------------

    def _ui_routes(self) -> None:
        app = self.app
        app.add_route("GET", "/", self.today)
        app.add_route("GET", "/marking", self.marking)
        app.add_route("POST", "/marking", self.marking_save)
        app.add_route("GET", "/assignments", self.assignments_page)
        app.add_route("POST", "/assignments/open-all", self.open_all)
        app.add_route("POST", "/examples", self.examples_add)
        app.add_route("GET", "/phones", self.phones_page)
        app.add_route("GET", "/phone-setup", self.phone_setup)

    def _to_mark(self) -> List[Tuple[dict, dict, dict]]:
        """Each written answer still waiting for a mark: (submission row, assignment, question), oldest first."""
        out = []
        for s in self.db.all("SELECT * FROM submissions WHERE grade LIKE '%\"complete\": false%' ORDER BY received_at"):
            a = self.get_assignment(s["assignment_id"], s["version"])
            if not a:
                continue
            scores = {q["id"]: q["score"] for q in json.loads(s["grade"])["questions"]}
            for q in a["questions"]:
                if q["type"] == "long" and scores.get(q["id"]) is None:
                    out.append((s, a, q))
        return out

    def _phone_names(self) -> Dict[str, str]:
        return {p["node_id"]: p["name"] for p in self.db.all("SELECT node_id, name FROM phones")}

    def _radio_state(self) -> Tuple[bool, str, str]:
        """(working, headline, detail) for this laptop's radio."""
        status = self.messenger.status()
        kind = type(getattr(self.messenger, "transport", None)).__name__
        last = self.db.one("SELECT MAX(last_seen) AS t FROM phones")["t"]
        pending = status.get("pending", 0)
        waiting = _plural(pending, "message") + " waiting" if pending else "Nothing waiting"
        heard = f"phones last heard {ago(last)}" if last else "no phone heard yet"
        if kind == "UdpSimTransport":
            return True, "Simulated radio", f"{waiting} · {heard}"
        if status.get("failed"):
            return False, "Some messages failed", f"{_plural(status['failed'], 'message')} gave up · {waiting.lower()}"
        return True, "Radio connected", f"{waiting} · {heard}"

    def _page(self, title: str, body: str, current: str = "", sub: str = "", action: str = "") -> str:
        current = current or _TITLE_TO_NAV.get(title, "")
        to_mark, new_names = len(self._to_mark()), len(self.new_names())
        links = ""
        for key, label, href in NAV:
            badge = f"<span class='count' aria-label='{to_mark} to mark'>{to_mark}</span>" if key == "marking" and to_mark else ""
            if key == "students" and new_names:
                badge = f"<span class='count' aria-label='{new_names} new names'>{new_names}</span>"
            here = " aria-current='page'" if key == current else ""
            links += f"<a href='{href}'{here}><span>{esc(label)}</span>{badge}</a>"
        ok, head, detail = self._radio_state()
        radio = (f"<a class='radiobox{'' if ok else ' off'}' href='/phones' style='text-decoration:none'>"
                 f"<strong><span class='dot'></span>{esc(head)}</strong><span>{esc(detail)}</span></a>")
        top = (f"<div class='top'><div><h1>{esc(title)}</h1>"
               + (f"<span class='sub'>{esc(sub)}</span>" if sub else "") + f"</div>{action}</div>")
        return (
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)} · MeshLearn Teacher Hub</title><style>{CSS}{HUB_CSS}</style></head><body>"
            f"<div class='shell'><aside class='side'><a class='brand' href='/'>MeshLearn</a>"
            f"<nav aria-label='Main'>{links}</nav>{radio}</aside>"
            f"<main id='main' class='content'>{top}{body}</main></div><script>{MATH_JS}</script></body></html>"
        )

    # -- Today --------------------------------------------------------------------

    def today(self, req: Request) -> Response:
        notice = f"<div class='notice'>{esc(req.query['msg'])}</div>" if req.query.get("msg") else ""
        if req.query.get("examples"):
            n = req.query["examples"]
            notice = (f"<div class='notice'>Added {esc(n)} example lessons. To put them on a phone: in the phone app choose "
                      "<strong>Add lessons and books</strong>, then <strong>From the teacher's laptop</strong>, then open them "
                      "for students on the <a href='/assignments'>Assignments</a> page.</div>")
        if not self.db.one("SELECT COUNT(*) AS n FROM assignments")["n"]:
            return Response(self._page("Welcome", notice + self._first_day(), sub="A new hub, with nothing set up yet"))
        now = time.time()
        names = self._phone_names()
        to_mark = self._to_mark()
        by_assignment: Dict[str, int] = {}
        for _, a, _ in to_mark:
            by_assignment[a["title"]] = by_assignment.get(a["title"], 0) + 1
        cards = (
            f"<a class='need{' urgent' if to_mark else ''}' href='/marking'><span class='num'>{len(to_mark)}</span>"
            f"<strong>Written answer{'' if len(to_mark) == 1 else 's'} to mark</strong>"
            f"<span class='small'>{esc(' · '.join(f'{t}: {n}' for t, n in list(by_assignment.items())[:3]) or 'All marked')}</span>"
            f"<span class='go'>{'Start marking' if to_mark else 'See marking'}</span></a>"
        )
        phones = self.db.all("SELECT * FROM phones ORDER BY name")
        quiet = [p for p in phones if not p["last_seen"] or now - p["last_seen"] > QUIET_S]
        if quiet:
            first = quiet[0]
            cards += (f"<a class='need urgent' href='/phones'><span class='num'>{len(quiet)}</span>"
                      f"<strong>{'Phone' if len(quiet) == 1 else 'Phones'} not heard for a day</strong>"
                      f"<span class='small'>{esc(first['name'])} · last heard {esc(ago(first['last_seen']))}</span>"
                      "<span class='go'>See phones</span></a>")
        else:
            cards += (f"<a class='need' href='/phones'><span class='num'>{len(phones)}</span>"
                      f"<strong>{'Phone' if len(phones) == 1 else 'Phones'}{', all heard today' if phones else ' yet'}</strong>"
                      f"<span class='small'>{esc(', '.join(p['name'] for p in phones[:3]) or 'Phones appear when they first check in')}</span>"
                      "<span class='go'>See phones</span></a>")
        latest = self.db.one("SELECT id, data FROM assignments ORDER BY created DESC LIMIT 1")
        if latest:
            a = json.loads(latest["data"])
            students = self.db.one("SELECT COUNT(DISTINCT student) AS n FROM submissions")["n"]
            handed = self.db.one("SELECT COUNT(DISTINCT student) AS n FROM submissions WHERE assignment_id = ?", (a["id"],))["n"]
            due = f"Due {a['due']}" if a.get("due") else "No due date"
            cards += (f"<a class='need' href='/assignments/{esc(a['id'])}'><span class='num'>{handed}"
                      + (f" / {students}" if students else "") + "</span>"
                      f"<strong>Handed in: {esc(a['title'])}</strong><span class='small'>{esc(due)}</span>"
                      "<span class='go'>See who</span></a>")

        records = self.comprehension_records()
        course_rows = ""
        for c in self.courses(include_other=True):
            if c.get("comingSoon") or not self.assignments_in(c["id"]):
                continue
            summary = cmp.summarize(records, c)
            score = summary["score"]   # a percentage, or None
            pct = f"{score}%" if score is not None else ""
            level = summary["level"] or "Not started"
            weakest = ""
            scored = [u for u in summary["units"] if u["score"] is not None]
            if len(scored) > 1:
                low = min(scored, key=lambda u: u["score"])
                weakest = f"<span class='small'>Weakest: unit {low['number']}, {esc(low['title'])}</span>"
            pace = summary["pace"]
            if pace:
                kind, n = max(pace.items(), key=lambda kv: kv[1])
                weakest = f"<span class='small warn'>{n}× {esc(cmp.PACE_LABELS[kind])}</span>"
            course_rows += (
                f"<a class='course' href='/comprehension/{esc(c['id']) or '_other'}'>"
                f"<span class='row'><strong>{esc(c['title'])}</strong><span class='level {_level_class(level)}'>{esc(level)}"
                f"{' · ' + pct if pct else ''}</span></span>"
                f"<span class='meter'><span style='width:{score or 0}%'></span></span>"
                + (weakest or f"<span class='small'>{_plural(summary['answered'], 'answer')} scored</span>") + "</a>")
        arrived = ""
        for s in self.db.all("SELECT * FROM submissions ORDER BY received_at DESC LIMIT 6"):
            a = self.get_assignment(s["assignment_id"], s["version"])
            grade = json.loads(s["grade"]) if s["grade"] else None
            if grade and grade["complete"]:
                well = grade["max"] and grade["score"] / grade["max"] >= 0.7
                right = f"<span class='score{' good' if well else ''}'>{grade['score']:g}/{grade['max']:g}</span>"
            else:
                left = sum(1 for q in (grade or {}).get("questions", []) if q["score"] is None)
                right = f"<span class='tomark'>{left or 1} to mark</span>"
            arrived += (f"<a href='/submissions/{esc(s['sub_id'])}'><span class='grow'><strong>{esc(s['student'])} · "
                        f"{esc(a['title'] if a else s['assignment_id'])}</strong><span class='small'>"
                        f"{esc(names.get(s['phone'], s['phone']))} · {esc(ago(s['received_at']))}</span></span>{right}</a>")
        body = (
            notice
            + f"<section aria-labelledby='needs'><h2 id='needs'>Needs you</h2><div class='cards'>{cards}</div></section>"
            + "<div class='cols'>"
            + "<section aria-labelledby='crs'><h2 id='crs'>Courses</h2>"
            + (f"<div class='list'>{course_rows}</div>" if course_rows else "<p class='muted'>No course has assignments yet.</p>")
            + "</section><section aria-labelledby='in'><h2 id='in'>Just arrived over the radio</h2>"
            + (f"<div class='list'>{arrived}</div>" if arrived else
               "<p class='muted'>Nothing yet. Students' work appears here as it arrives.</p>")
            + "</section></div>"
            + self.learning_data_panel()
        )
        date = datetime.date.today().strftime("%A %d %B").replace(" 0", " ")
        return Response(self._page("Today", body, sub=date,
                                   action="<a class='button' href='/assignments/new'>+ New assignment</a>"))

    def _first_day(self) -> str:
        phones = self.db.one("SELECT COUNT(*) AS n FROM phones")["n"]
        return (
            self._first_country() + "<div class='hero'><h2 style='margin-top:0;font-size:22px'>Three steps to the first lesson</h2>"
            "<p class='muted' style='margin:0'>Students' work comes back here over the radio, even with no internet.</p>"
            "<ol class='steps'>"
            "<li><span class='n'>1</span><span class='grow'><strong>Add lessons</strong>"
            "<span class='small'>Start from nine example lessons in mathematics, science and reading, or write your own.</span>"
            "<span><form method='post' action='/examples' style='display:inline'><button>Add example lessons</button></form>"
            "<a class='button secondary' href='/assignments/new'>Write a lesson</a></span></span></li>"
            "<li><span class='n'>2</span><span class='grow'><strong>Set up the student phones</strong>"
            f"<span class='small'>{'Phones appear here when they first check in.' if not phones else _plural(phones, 'phone') + ' already checked in.'} "
            "Each phone needs this hub's radio id, shown on the Phones page.</span>"
            "<a class='button secondary' href='/phones'>Phones and radio</a></span></li>"
            "<li><span class='n'>3</span><span class='grow'><strong>Copy the lessons to the phones</strong>"
            "<span class='small'>At school, on the same Wi-Fi: in the phone app choose Add lessons and books, then From the "
            "teacher's laptop. Then open the lessons for students from here.</span></span></li>"
            "</ol></div>"
        )

    def _first_country(self) -> str:
        """Asked once: the country sets the languages for learning."""
        if self.setting("country") or self.setting("lang") != lang.SOURCE:
            return ""
        buttons = "".join(f"<button name='country' value='{key}' class='secondary'>{esc(info['name'])}</button>"
                          for key, info in lang.COUNTRIES.items())
        return ("<form method='post' action='/language/country' class='card'><strong>Where is your school?</strong>"
                "<p class='muted' style='margin:4px 0'>This sets the languages students learn in. You can change them any time "
                f"on the <a href='/language'>Language</a> page.</p><input type='hidden' name='back' value='/'>{buttons}</form>")

    # -- example lessons ------------------------------------------------------------

    def example_lessons(self) -> List[dict]:
        """The demo's lessons, with due dates a few days ahead so none start out overdue."""
        from . import showcase
        lessons = []
        try:
            with open(EXAMPLE_FILE, encoding="utf-8") as fh:
                lessons.append(json.load(fh))
        except (OSError, ValueError):
            pass
        lessons += [{k: v for k, v in spec.items() if k not in ("released", "share")}
                    for spec in showcase.ASSIGNMENTS if spec["id"] != "a-fractions1"]
        today = datetime.date.today()
        for n, lesson in enumerate(lessons):
            if lesson.get("due"):
                lesson["due"] = (today + datetime.timedelta(days=3 + 2 * n)).isoformat()
        return lessons

    def add_examples(self) -> int:
        added = 0
        for lesson in self.example_lessons():
            if self.get_assignment(lesson.get("id", "")) is None:
                self.create_assignment(lesson)
                added += 1
        return added

    def examples_add(self, req: Request) -> Response:
        return redirect(f"/?examples={self.add_examples()}")

    # -- Marking ------------------------------------------------------------------

    def marking(self, req: Request) -> Response:
        items = self._to_mark()
        names = self._phone_names()
        notice = ""
        if req.query.get("sent"):
            notice = "<div class='notice'>Saved. The result is on its way to the student's phone.</div>"
        elif req.query.get("saved"):
            notice = "<div class='notice'>Saved. The result goes to the phone once every written answer in it is marked.</div>"
        if not items:
            body = notice + ("<div class='hero'><h2 style='margin-top:0'>Nothing to mark</h2><p class='muted'>Written answers "
                             "wait here as they arrive. Choice, number and short answers are marked by themselves.</p>"
                             "<a class='button secondary' href='/'>Back to Today</a></div>")
            return Response(self._page("Marking", body, sub="Written answers wait here."))
        want_s, want_q = req.query.get("s", ""), req.query.get("q", "")
        cur = next((i for i, (s, _, q) in enumerate(items) if s["sub_id"] == want_s and q["id"] == want_q), 0)
        s, a, q = items[cur]
        queue = ""
        for i, (si, ai, qi) in enumerate(items):
            qn = [x["id"] for x in ai["questions"]].index(qi["id"]) + 1
            queue += (f"<a href='/marking?s={esc(si['sub_id'])}&amp;q={esc(qi['id'])}'"
                      f"{' aria-current=true' if i == cur else ''}><span class='grow'><strong>{esc(si['student'])}</strong>"
                      f"<span class='small'>{esc(names.get(si['phone'], si['phone']))} · {esc(ai['title'])} Q{qn}</span></span>"
                      "<span class='tomark'>To mark</span></a>")
        qn = [x["id"] for x in a["questions"]].index(q["id"]) + 1
        said = json.loads(s["answers"]).get(q["id"], "").strip()
        unsure, _ = asg.read_signals(a, json.loads(s["signals"] or "{}"))
        doubt = " <span class='tomark'>not sure</span>" if q["id"] in unsure else ""
        note = json.loads(s["notes"] or "{}").get(q["id"], "")
        points = q["points"]
        steps = [float(n) for n in range(int(points) + 1)]
        if points not in steps:
            steps.append(points)
        marks = "".join(
            f"<label><input type='radio' name='mark' value='{m:g}' required><span>{m:g}</span></label>" for m in steps)
        model = (f"<div class='model'><strong>Your model answer:</strong> {esc(q['model'])}</div>" if q.get("model") else "")
        info = info_panel(q)
        body = (
            notice + "<div class='cols'>"
            f"<section class='col' aria-label='Answers to mark' style='flex:1 1 260px'><div class='list'>{queue}</div></section>"
            "<section class='wide panel' aria-labelledby='qtext'>"
            f"<span class='small'>{esc(a['title'])} · Question {qn} · worth {points:g} mark{'' if points == 1 else 's'}</span>"
            f"<h2 id='qtext' style='margin:6px 0 12px;font-size:19px;line-height:1.4'>{esc(q['prompt'])}</h2>{info}{model}"
            f"<div style='margin-top:14px'><span class='small'>{esc(s['student'])} wrote · "
            f"{esc(names.get(s['phone'], s['phone']))} · {esc(ago(s['submitted_at'] or s['received_at']))}</span>{doubt}"
            f"<p class='said'>{esc(said) or '<em class=muted>(left blank)</em>'}</p></div>"
            "<form method='post' action='/marking' style='margin-top:14px'>"
            f"<input type='hidden' name='sid' value='{esc(s['sub_id'])}'><input type='hidden' name='qid' value='{esc(q['id'])}'>"
            f"<fieldset class='marks'><legend style='font-weight:600;padding:0;margin-bottom:6px'>Marks</legend>{marks}</fieldset>"
            "<label for='note'>Short note back to the student (optional)</label>"
            f"<input type='text' id='note' name='note' maxlength='60' value='{esc(note)}' placeholder='Good working. Check the units.'>"
            "<span class='small'>Up to 60 letters, so it fits in one radio message.</span>"
            "<div><button style='margin-top:16px'>Save and next</button></div>"
            "<span class='small'>The mark goes back to the student's phone over the radio.</span> "
            f"<a class='small' style='margin-left:8px' href='/submissions/{esc(s['sub_id'])}'>See all of {esc(s['student'])}'s answers</a>"
            "</form></section></div>"
        )
        return Response(self._page("Marking", body, sub=f"{_plural(len(items), 'written answer')} to mark. "
                                                       "Choice questions are marked by themselves."))

    def marking_save(self, req: Request) -> Response:
        sid, qid = req.form.get("sid", ""), req.form.get("qid", "")
        try:
            mark = float(req.form.get("mark", ""))
            self.set_note(sid, qid[:20], req.form.get("note", ""))
            grade = self.grade_submission(sid, {qid: mark})
        except (KeyError, ValueError):
            return redirect("/marking")
        if grade and grade["complete"]:
            row = self.db.one("SELECT phone FROM submissions WHERE sub_id = ?", (sid,))
            self._send_results(row["phone"], [sid])
            return redirect("/marking?sent=1")
        return redirect("/marking?saved=1")

    # -- Assignments --------------------------------------------------------------

    def assignments_page(self, req: Request) -> Response:
        phones = self.db.all("SELECT node_id FROM phones")
        notice = ""
        if req.query.get("opened"):
            notice = (f"<div class='notice'>Opening {esc(req.query['opened'])} on every phone. Phones that already have them "
                      "need one radio packet each; the others get the whole lesson by radio, which is slower.</div>")
        rows = ""
        for r in self.db.all(
                "SELECT a.id, a.data, a.version, (SELECT COUNT(DISTINCT student) FROM submissions s WHERE s.assignment_id = a.id) AS handed, "
                "(SELECT COUNT(*) FROM submissions s WHERE s.assignment_id = a.id AND s.grade LIKE '%\"complete\": false%') AS waiting, "
                "(SELECT COUNT(*) FROM holdings h WHERE h.aid = a.id AND h.open = 1) AS open_on, "
                "(SELECT COUNT(*) FROM holdings h WHERE h.aid = a.id AND h.version = a.version) AS held "
                "FROM assignments a ORDER BY a.created DESC"):
            a = json.loads(r["data"])
            where = self._placement(a) or a.get("subject") or ""
            on = (f"open on {r['open_on']} of {len(phones)}" if r["open_on"] else
                  f"copied to {r['held']} of {len(phones)}, not open" if r["held"] else "not on a phone yet") if phones else ""
            rows += (f"<tr><td><a href='/assignments/{esc(a['id'])}'><strong>{esc(a['title'])}</strong></a>"
                     f"<div class='small'>{esc(where)}{' · practice' if a.get('mode') == 'practice' else ''}</div></td>"
                     f"<td>{esc(a.get('due') or '—')}</td><td>{r['handed']}</td>"
                     f"<td>{'<span class=tomark>' + str(r['waiting']) + '</span>' if r['waiting'] else '—'}</td>"
                     f"<td class='small'>{esc(on)}</td></tr>")
        if not rows:
            body = notice + self._first_day()
            return Response(self._page("Assignments", body))
        open_all = ("<form method='post' action='/assignments/open-all' class='panel' style='margin-top:16px'>"
                    "<h2>Open every assignment for students</h2><p class='muted'>Shows every assignment on every phone. "
                    "Copy the lessons to the phones first (in the phone app: Add lessons and books, then From the teacher's "
                    "laptop), so each one costs a single radio packet.</p><button>Open all on every phone</button></form>"
                    if phones else "")
        examples = ("<form method='post' action='/examples' style='margin-top:12px'><button class='secondary'>"
                    "Add the example lessons</button></form>"
                    if any(self.get_assignment(x.get("id", "")) is None for x in self.example_lessons()) else "")
        body = (notice + "<table class='plain'><tr><th>Assignment</th><th>Due</th><th>Handed in</th><th>To mark</th>"
                f"<th>On phones</th></tr>{rows}</table>{open_all}{examples}")
        count = self.db.one("SELECT COUNT(*) AS n FROM assignments")["n"]
        return Response(self._page("Assignments", body, sub=_plural(count, "assignment"),
                                   action="<a class='button' href='/assignments/new'>+ New assignment</a>"))

    def open_all(self, req: Request) -> Response:
        nodes = [r["node_id"] for r in self.db.all("SELECT node_id FROM phones")]
        count = 0
        for r in self.db.all("SELECT id FROM assignments ORDER BY created"):
            for node in nodes:
                h = self._holding(node, r["id"])
                if h and h["open"] and h["version"] == self.current_version(r["id"]):
                    continue   # already open there
                self.set_open(r["id"], node, True)
            count += 1
        return redirect(f"/assignments?opened={count}")

    # -- Phones and radio ---------------------------------------------------------

    @staticmethod
    def _lan_address(req: Request) -> str:
        """This laptop's address for phones on the same Wi-Fi, or ''."""
        port = (req.headers.get("Host", "") or "").rpartition(":")[2]
        port = port if port.isdigit() else "8000"
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("10.255.255.255", 1))   # sends nothing; picks the network card phones would reach
                ip = s.getsockname()[0]
        except OSError:
            return ""
        return "" if ip.startswith("127.") else f"{ip}:{port}"

    def _setup_qr(self, req: Request) -> str:
        address = self._lan_address(req)
        if not address:
            return ""
        return (f"<div style='margin:12px auto 0;max-width:220px' data-qr='http://{esc(address)}/phone-setup'></div>"
                f"<p class='small' style='margin:4px 0 0'>Or, on the same Wi-Fi, scan this with the phone's camera "
                f"(http://{esc(address)}/phone-setup). It needs the hub started from its start file, or with --host 0.0.0.0.</p><script>{QR_JS}</script>")

    def phone_setup(self, req: Request) -> Response:
        """What a phone sees after scanning the hub's code: the settings, and a button that hands them to the app."""
        transport = getattr(self.messenger, "transport", None)
        node = str(getattr(transport, "node_id", "") or "")
        channel = getattr(transport, "channel", None)
        address = req.headers.get("Host", "")
        address = address if re.match(r"^[A-Za-z0-9.\-:\[\]]{1,80}$", address) else ""
        query = urlencode({k: v for k, v in {"hub": node, "channel": channel, "address": address}.items() if v not in (None, "")})
        body = ("<div class='card'><p>This sets a MeshLearn phone up for this school.</p>"
                f"<p>School radio id: <strong>{esc(node or 'not known yet')}</strong></p>"
                + (f"<p>Channel: <strong>{channel}</strong></p>" if channel is not None else "")
                + f"<p>Laptop address: <strong>{esc(address)}</strong></p>"
                f"<a class='button' href='meshlearn://setup?{esc(query)}'>Open in the MeshLearn app</a>"
                "<p class='muted'>If nothing opens, install the MeshLearn app first, or type these in its Settings.</p></div>")
        return Response(page("Set up a phone", body))

    def phones_page(self, req: Request) -> Response:
        now = time.time()
        phones = self.db.all("SELECT * FROM phones ORDER BY name")
        waiting = self.messenger.pending_by_dest() if hasattr(self.messenger, "pending_by_dest") else {}
        total = self.db.one("SELECT COUNT(*) AS n FROM assignments")["n"]
        cards = ""
        for p in phones:
            students = self.db.one("SELECT COUNT(DISTINCT student) AS n FROM submissions WHERE phone = ?", (p["node_id"],))["n"]
            held = self.db.one("SELECT COUNT(*) AS n FROM holdings h JOIN assignments a ON a.id = h.aid "
                               "WHERE h.node_id = ? AND h.version = a.version", (p["node_id"],))["n"]
            quiet = not p["last_seen"] or now - p["last_seen"] > QUIET_S
            state = "new" if not p["last_seen"] else ("quiet" if quiet else "")
            heard = f"heard {ago(p['last_seen'])}" if p["last_seen"] else "added here, not heard yet"
            line = f"{'Not heard for a while' if quiet and p['last_seen'] else 'Working' if not quiet else 'Waiting to hear from it'} · {heard} · {_plural(students, 'student')}"
            n_wait = waiting.get(p["node_id"], 0)
            behind = total - held
            pack = "Lessons up to date" if total and not behind else (f"{_plural(behind, 'lesson')} not copied yet" if total else "")
            tip = ("<span class='tip'>Often a flat battery or the radio was moved. Ask someone there to open the MeshLearn "
                   "app: its status screen shows what to fix.</span>" if quiet and p["last_seen"] else "")
            cards += (f"<div class='phonecard {state}'><span class='state' aria-hidden='true'></span>"
                      f"<span class='grow' style='flex:1 1 200px'><strong style='font-size:18px'>{esc(p['name'])}</strong>"
                      f"<span class='small'>{esc(line)} · {esc(p['node_id'])}</span></span>"
                      f"<span class='right'><span>{_plural(n_wait, 'message') + ' waiting' if n_wait else 'Nothing waiting'}</span>"
                      f"<span>{esc(pack)}</span></span>{tip}</div>")
        if not cards:
            cards = ("<div class='panel'><strong>No phones yet.</strong><p class='muted' style='margin:4px 0 0'>A phone appears "
                     "here by itself the first time it reaches this hub. Set it up with the radio id on the right.</p></div>")
        status = self.messenger.status()
        transport = getattr(self.messenger, "transport", None)
        node = getattr(transport, "node_id", "") or ""
        channel = getattr(transport, "channel", None)
        simulated = type(transport).__name__ == "UdpSimTransport"
        ok, head, _ = self._radio_state()
        radio = (
            "<section class='panel' aria-labelledby='radio' style='margin-top:4px'><h2 id='radio'>This laptop's radio</h2>"
            "<div class='kv'>"
            f"<span><span class='k'>Status</span><strong class='{'good' if ok else 'warn'}'>{esc(head)}</strong></span>"
            f"<span><span class='k'>Radio id</span><strong>{esc(node or 'not known yet')}</strong></span>"
            + (f"<span><span class='k'>Channel</span><strong>{channel}{' (public!)' if channel == 0 and not simulated else ''}</strong></span>"
               if channel is not None else "")
            + f"<span><span class='k'>Waiting to send</span><strong>{_plural(status.get('pending', 0), 'message')}</strong></span>"
            f"<span><span class='k'>Sent since start</span><strong>{_plural(status.get('frames_sent', 0), 'packet')}</strong></span>"
            "</div><form method='post' action='/mesh/retry'><button class='secondary'>Send waiting now</button></form></section>"
        )
        setup = (
            "<section class='panel' aria-labelledby='setup' style='border:2px solid var(--accent);text-align:center'>"
            "<h2 id='setup'>Set up a new phone</h2><p class='small' style='margin:0'>In the MeshLearn app on the phone, "
            "during setup or in Settings, under School, type:</p>"
            f"<p class='code' style='margin:10px 0 2px'>{esc(node or '!........')}</p>"
            + (f"<p class='small' style='margin:0'>channel <strong>{channel}</strong></p>" if channel is not None else "")
            + self._setup_qr(req)
            + "</section>"
        )
        add = ("<details class='panel' style='margin-top:12px'><summary style='cursor:pointer;font-weight:600'>Add a phone by hand"
               "</summary><form method='post' action='/phones'><label for='name'>Name (the village or family)</label>"
               "<input type='text' id='name' name='name' required><label for='node_id'>Its radio id</label>"
               "<input type='text' id='node_id' name='node_id' placeholder='!a1b2c3d4' required>"
               "<button>Add phone</button></form></details>")
        pack = ("<section class='panel' aria-labelledby='pack' style='margin-top:12px'><h2 id='pack'>Load lessons without the radio</h2>"
                "<p class='small' style='margin:0'>Copy every lesson onto a phone at school, so the radio only carries answers "
                "and marks. Over Wi-Fi: in the phone app, Add lessons and books, then From the teacher's laptop, and type this "
                f"laptop's address with :8000. By memory card: download the course pack and unzip it into the phone's MeshLearn "
                f"folder.</p><p class='small'>{self.language_summary()}. Phones get it with the lessons.</p>"  # already HTML
                "<a class='button' href='/pack.zip'>Download course pack</a></section>")
        students = self.db.one("SELECT COUNT(DISTINCT student) AS n FROM submissions")["n"]
        body = (f"<div class='cols'><div class='col wide'>{cards}{radio}</div>"
                f"<aside class='col' style='flex:1 1 280px'>{setup}{add}{pack}</aside></div>")
        return Response(self._page("Phones and radio", body,
                                   sub=f"{_plural(len(phones), 'community phone')} · {_plural(students, 'student')}"))
