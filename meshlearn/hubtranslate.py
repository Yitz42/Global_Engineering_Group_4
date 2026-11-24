"""Teacher hub page for an assignment in the school's languages (the "Languages" design).

Translations live in the assignment ("tr", see assignments.py) and travel with it. Where one is missing, students see the
original. Mixed into Hub, like the other page files.
"""

from __future__ import annotations

import copy
from typing import Dict, List
from urllib.parse import quote, urlencode

from . import assignments as asg
from . import catalog as cat
from . import protocol
from .web import Request, Response, esc, redirect


class HubTranslate:
    def _translate_routes(self) -> None:
        self.app.add_route("GET", "/assignments/<aid>/languages", self.translate_page)
        self.app.add_route("POST", "/assignments/<aid>/languages", self.translate_save)

    def class_codes(self) -> List[str]:
        """The languages students can use: the school's, then the others."""
        return [self.school_language()] + self.other_languages()

    def written_in(self, assignment: dict) -> str:
        return assignment.get("lang") or self.school_language()

    def translate_to(self, assignment: dict) -> List[str]:
        return [c for c in self.class_codes() if c != self.written_in(assignment)]

    def _tr_frames(self, assignment: dict) -> int:
        view = asg.only_languages(asg.public_view(assignment), self.class_codes())
        packed = asg.pack(dict(view, version="00000000"))
        packed.update(self.packed_pictures(assignment))
        return protocol.frame_count(protocol.ASSIGN, {"a": packed, "o": 1, "d": assignment.get("due", "")})

    def translate_page(self, req: Request) -> Response:
        a = self.get_assignment(req.params["aid"])
        if a is None:
            return Response(self._page("Not found", "<p>No such assignment.</p>"), status=404)
        aid, base, codes = a["id"], self.written_in(a), self.translate_to(a)
        counts: Dict[str, int] = {}
        for s in self.class_languages():
            used = (s["seen"]["lang"] or self.school_language()) if s["seen"] else s["set"] or self.school_language()
            counts[used] = counts.get(used, 0) + 1

        def students(code: str) -> str:
            n = counts.get(code, 0)
            return f"{n} student{'' if n == 1 else 's'}"

        total = len(a["questions"])
        chips = (f"<div class='panel trsum'><div class='row'><strong>{esc(cat.language_name(base))}</strong><span class='small'>{students(base)}</span></div>"
                 "<span class='meter'><span style='width:100%'></span></span><span class='small'>Written first</span></div>")
        for code in codes:
            done = sum(1 for q in a["questions"] if asg.translated(q, code))
            stale = sum(1 for q in a["questions"] if asg.stale(q, code))
            left = total - done
            note = (f"All {total} questions translated" if not left else
                    f"{left} missing: {students(code)} see {'that' if left == 1 else 'those'} in {cat.language_name(base)}")
            if stale:
                note += f" · {stale} to check: the original changed"
            chips += (f"<div class='panel trsum{' some' if left or stale else ''}'><div class='row'><strong>{esc(cat.language_name(code))}</strong>"
                      f"<span class='small'>{students(code)}</span></div><span class='meter'><span style='width:{round(100 * done / total)}%'></span></span>"
                      f"<span class='small'>{esc(note)}</span></div>")
        options = "".join(f"<option value='{c}'{' selected' if c == base else ''}>{esc(cat.language_name(c))}</option>"
                          for c in dict.fromkeys(self.class_codes() + [base, "eng"]))
        written = (f"<form method='post' action='/assignments/{esc(aid)}/languages' class='inline' style='margin:0 0 14px'><label for='base' style='margin:0'>This assignment is written in</label>"
                   f"<select id='base' name='base'>{options}</select><button class='secondary'>Change</button></form>")
        saved = f"<div class='notice' role='status'>{esc(req.query['msg'])}</div>" if req.query.get("msg") else ""
        if not codes:
            body = (f"{saved}{written}<div class='panel'><strong>Students have one language here.</strong><p class='small' style='margin:4px 0 0'>Add the languages your "
                    "students learn in on the <a href='/language'>Language</a> page, and they show here to translate into.</p></div>")
            return Response(self._page("Languages", body, current="assignments", sub=a["title"]))
        head = "".join(f"<th scope='col' lang='{esc(self.language_pack(c)['tag'])}'>{esc(cat.language_name(c))}</th>" for c in codes)
        rows = self._tr_row("", "", a, codes, base, {"prompt": a["title"], "info": a.get("instructions", "")}, ("Title", "Instructions"))
        for n, q in enumerate(a["questions"], start=1):
            rows += self._tr_row(str(n), q["id"], a, codes, base, q, ("Question", "Question info"))
        plain = copy.deepcopy(a)
        plain.pop("tr", None)
        for q in plain["questions"]:
            q.pop("tr", None)
        now, without = self._tr_frames(a), self._tr_frames(plain)
        more = now - without
        size = (f"Radio size: about {now} packet{'' if now == 1 else 's'}" + (f", {more} of them for the translations" if more else "")
                + ". Phones get only the languages your students use. Changing a translation sends the whole assignment again to phones where it is open.")
        body = (f"{saved}{written}<div class='cards'>{chips}</div><form method='post' action='/assignments/{esc(aid)}/languages' style='margin-top:16px'>"
                f"<div class='scroll'><table class='plain trgrid'><thead><tr><th scope='col'>#</th><th scope='col'>{esc(cat.language_name(base))} (written first)</th>{head}</tr></thead>"
                f"<tbody>{rows}</tbody></table></div><button>Save translations</button> <a class='button secondary' href='/assignments/{esc(aid)}/voice'>Record your voice</a></form>"
                f"<p class='small'>{esc(size)}</p><p class='small'>Short answers: list the accepted answers in every language when you <a href='/assignments/{esc(aid)}/edit'>edit</a> the question. "
                "Numbers and pictures work in any language.</p>")
        return Response(self._page("Languages", body, current="assignments", sub=a["title"],
                                   action=f"<a class='button secondary' href='/assignments/{esc(aid)}'>Back to the assignment</a>"))

    def _tr_row(self, n: str, qid: str, a: dict, codes: List[str], base: str, q: dict, labels) -> str:
        """One row: the original, then a cell to write in for each language."""
        info = q.get("info") or ""
        original = f"<strong>{esc(q['prompt'])}</strong>" + "".join(f"<div class='tro'>{esc(o)}</div>" for o in q.get("options", []))
        if info:
            original += f"<div class='small' style='white-space:pre-line'>{esc(info)}</div>"
        cells = ""
        for code in codes:
            pack = self.language_pack(code)
            source = (a.get("tr") or {}).get(code, {}) if not qid else (q.get("tr") or {}).get(code, {})
            prompt, text = (source.get("T", ""), source.get("I", "")) if not qid else (source.get("p", ""), source.get("n", ""))
            name, attrs = f"t|{code}|{qid}", f"lang='{esc(pack['tag'])}' dir='{esc(pack['dir'])}'"
            says = f"{labels[0]} {n} in {cat.language_name(code)}".replace("  ", " ")
            cell = (f"<textarea name='{esc(name)}|p' {attrs} rows='2' maxlength='{500 if qid else 120}' aria-label='{esc(says)}' "
                    f"placeholder='Students see {esc(cat.language_name(base))}'>{esc(prompt)}</textarea>")
            done = source.get("o") or []
            for j, o in enumerate(q.get("options", [])):
                cell += (f"<input type='text' name='{esc(name)}|o{j}' {attrs} maxlength='200' value='{esc(done[j] if j < len(done) else '')}' "
                         f"placeholder='{esc(o)}' aria-label='{esc(f'Option {j + 1} of {says}')}'>")
            if info:
                cell += (f"<textarea name='{esc(name)}|n' {attrs} rows='3' maxlength='{asg.MAX_INFO_CHARS}' placeholder='{esc(labels[1])}' "
                         f"aria-label='{esc(f'{labels[1]} for {says}')}'>{esc(text)}</textarea>")
            if qid and asg.stale(q, code):
                cell += (f"<label class='option warn' style='font-size:14px;margin:4px 0 0'><input type='checkbox' name='ok|{code}|{esc(qid)}' value='1' style='width:18px;height:18px'> "
                         "The original changed. Tick if this still fits.</label>")
            cells += f"<td>{cell}</td>"
        return f"<tr><td>{n}</td><td lang='{esc(self.language_pack(base)['tag'])}'>{original}</td>{cells}</tr>"

    def translate_save(self, req: Request) -> Response:
        a = self.get_assignment(req.params["aid"])
        if a is None:
            return redirect("/assignments")
        back = f"/assignments/{quote(a['id'])}/languages?"
        old = self.current_version(a["id"])
        if req.form.get("base"):
            if not self._valid_code(req.form["base"]):
                return redirect(back + urlencode({"msg": "That language is not in the list."}))
            a["lang"] = req.form["base"]
            self.create_assignment(a)
            return redirect(back + urlencode({"msg": "Saved."}))

        def field(code: str, qid: str, part: str) -> str:
            return req.form.get(f"t|{code}|{qid}|{part}", "").strip()

        for code in self.translate_to(a):
            top = {k: v for k, v in (("T", field(code, "", "p")), ("I", field(code, "", "n"))) if v}
            a["tr"] = {**(a.get("tr") or {}), code: top}
            for q in a["questions"]:
                was = (q.get("tr") or {}).get(code, {})
                now: dict = {k: v for k, v in (("p", field(code, q["id"], "p")), ("n", field(code, q["id"], "n"))) if v}
                done = [field(code, q["id"], f"o{j}") for j in range(len(q.get("options", [])))]
                if any(done):
                    now["o"] = done
                if now:
                    same = {k: v for k, v in was.items() if k != "s"} == now
                    now["s"] = was.get("s", "") if same and not req.form.get(f"ok|{code}|{q['id']}") else asg.source_stamp(q)
                q["tr"] = {**(q.get("tr") or {}), code: now}
        self.create_assignment(a)
        sent = 0
        if self.current_version(a["id"]) != old:
            for row in self.db.all("SELECT node_id FROM holdings WHERE aid = ? AND open = 1", (a["id"],)):
                self.set_open(a["id"], row["node_id"], True)
                sent += 1
        return redirect(back + urlencode({"msg": "Saved." + (f" Sending it again to {sent} phone{'' if sent == 1 else 's'} where it is open." if sent else "")}))
