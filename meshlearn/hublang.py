"""Hub pages for the school's language: choose it, translate MeshLearn's words, share them.

The school language is the hub's "lang" setting (an ISO 639-3 code, the same one the card
library uses), so books and buttons always match. Up to two more are optional; students can
then switch on the phone. Choosing the school's country sets them all. Phones get the words
with their lessons (Wi-Fi copy or memory card), never by radio.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Tuple
from urllib.parse import urlencode

from . import catalog as cat
from . import language as lang
from .web import Request, Response, esc, json_response, redirect

_CODE = re.compile(r"^[a-z]{3}$")


class HubLanguage:
    # -- data -----------------------------------------------------------------

    def phrases(self, code: str) -> Dict[str, str]:
        """The teacher's own words for a language (not the language's name and direction)."""
        return {r["key"]: r["text"] for r in self.db.all("SELECT key, text FROM phrases WHERE lang = ?", (code,))
                if not r["key"].startswith("_")}

    def _meta(self, code: str) -> Tuple[str, str]:
        rows = {r["key"]: r["text"] for r in self.db.all(
            "SELECT key, text FROM phrases WHERE lang = ? AND key IN ('_name', '_dir')", (code,))}
        return rows.get("_name", ""), rows.get("_dir", "")

    def language_pack(self, code: str) -> dict:
        name, direction = self._meta(code)
        base = lang.bundled().get(code, {})
        country = self.setting("country")
        return lang.build_pack(code, self.phrases(code), name=name or lang.regional(code, country).get("name")
                               or base.get("name") or cat.language_name(code), direction=direction, country=country)

    def school_language(self) -> str:
        code = self.setting("lang")
        return code if _CODE.match(code or "") else lang.SOURCE

    def other_languages(self) -> List[str]:
        """The other languages students can switch to, in order."""
        out: List[str] = []
        for key in ("lang2", "lang3"):
            code = self.setting(key)
            if _CODE.match(code or "") and code != self.school_language() and code not in out:
                out.append(code)
        return out

    def language_bundle(self) -> dict:
        """What phones get with their lessons: the school's words, and the other languages' if there are any.
        ("second" apart from "more": phones from before there could be three know only the second.)"""
        packs = [self.language_pack(c) for c in self.other_languages()]
        return {"school": self.language_pack(self.school_language()), "second": packs[0] if packs else None,
                "more": packs[1:], "ages": self.student_ages(), **self.student_bundle()}

    def all_under_12(self) -> bool:
        return all(a in ("early", "primary") for a in self.student_ages())

    def known_languages(self) -> List[str]:
        codes = set(cat.LANGUAGES) | set(lang.bundled()) | {r["lang"] for r in self.db.all("SELECT DISTINCT lang FROM phrases")}
        codes.discard("mul")
        return sorted(c for c in codes if _CODE.match(c))

    def _valid_code(self, code: str) -> bool:
        return bool(_CODE.match(code or "")) and code in self.known_languages()

    # -- pages ----------------------------------------------------------------

    def _language_routes(self) -> None:
        app = self.app
        app.add_route("GET", "/language", self.language_page)
        app.add_route("POST", "/language", self.language_choose)
        app.add_route("POST", "/language/country", self.language_country)
        app.add_route("GET", "/language/words", self.language_words)
        app.add_route("POST", "/language/words", self.language_words_save)
        app.add_route("GET", "/language/export/<code>", self.language_export)
        app.add_route("POST", "/api/language/import", self.language_import)

    def _language_label(self, pack: dict) -> str:
        english = cat.language_name(pack["code"])
        own = pack["name"]
        return (f"<strong lang='{esc(pack['tag'])}' dir='{esc(pack['dir'])}'>{esc(own)}</strong>"
                + (f" <span class='muted'>{esc(english)}</span>" if english != own else ""))

    def language_summary(self) -> str:
        """One line for the dashboard."""
        pack = self.language_pack(self.school_language())
        pct = lang.coverage(pack)
        return (f"School language: <strong>{esc(pack['name'])}</strong>"
                + ("" if pct == 100 else f" · {pct}% of the words students see are translated")
                + " · <a href='/language'>change</a>")

    def language_page(self, req: Request) -> Response:
        current, others = self.school_language(), self.other_languages()
        query = (req.query.get("q") or "").strip().lower()
        notice = req.query.get("msg", "")
        books = dict(self.catalog.languages()) if self.catalog.entries else {}
        packs = {code: self.language_pack(code) for code in self.known_languages()}

        pack = packs[current]
        pct = lang.coverage(pack)
        if current == lang.SOURCE:
            words = "Every word students see is in English."
        elif pct == 100:
            words = "Every word students see is translated."
        elif pct == 0:
            words = "MeshLearn's words are not translated into this language yet, so students see English until you translate them."
        else:
            words = f"{pct}% of the words students see are translated. The rest show in English until they are."
        unchecked = ("<p class='warn'>These words came with MeshLearn and have not been checked by a native speaker yet. "
                     "Please read them and fix anything that sounds wrong.</p>"
                     if current in lang.bundled() and current != lang.SOURCE and not pack["reviewed"] else "")
        top = (
            "<div class='card'>"
            f"<p style='font-size:20px;margin:0'>Your school teaches in {self._language_label(pack)}</p>"
            f"<p>{esc(words)}</p>{unchecked}"
            "<p class='muted'>This sets every phone and the books suggested for the memory card. Phones get the words "
            "with their next lesson copy, over Wi-Fi or on the memory card. The radio is not used.</p>"
            f"<a class='button' href='/language/words?lang={esc(current)}'>Translate or fix words</a>"
            "</div>"
        )
        def pick(n: int) -> str:
            chosen = others[n] if len(others) > n else ""
            return "<option value=''>None</option>" + "".join(
                f"<option value='{esc(c)}'{' selected' if c == chosen else ''}>{esc(cat.language_name(c))}</option>"
                for c in sorted(packs, key=lambda c: cat.language_name(c)) if c != current)

        second_form = (
            "<form method='post' action='/language' class='card'>"
            f"<input type='hidden' name='lang' value='{esc(current)}'>"
            "<strong>Students can also learn in</strong><div class='grid'>"
            f"<div><label for='lang2'>Second language</label><select id='lang2' name='lang2'>{pick(0)}</select></div>"
            f"<div><label for='lang3'>Third language</label><select id='lang3' name='lang3'>{pick(1)}</select></div></div>"
            "<p class='muted'>Students choose their language when they sign in, and can switch on any page.</p>"
            "<button class='secondary'>Save</button></form>"
        )
        countries = self._country_cards()

        def rank(code: str):
            return (code != current, -lang.coverage(packs[code]), -books.get(code, 0), cat.language_name(code))

        rows = ""
        for code in sorted(packs, key=rank):
            p = packs[code]
            names = f"{code} {cat.language_name(code)} {p['name']}".lower()
            if query and query not in names:
                continue
            cov = lang.coverage(p)
            cov_text = "Ready" if cov == 100 else "Not yet" if cov == 0 else f"{cov}% done"
            cov_cls = "good" if cov == 100 else "warn" if cov else "muted"
            if code in lang.bundled() and code != lang.SOURCE and not p["reviewed"]:
                cov_text += " · not checked yet"
            action = ("<span class='pill good'>In use</span>" if code == current else
                      "<form method='post' action='/language' style='display:inline'>"
                      f"<input type='hidden' name='lang' value='{esc(code)}'><button class='secondary'>Use</button></form>")
            rows += (f"<tr><td>{self._language_label(p)}</td><td class='{cov_cls}'>{esc(cov_text)}</td>"
                     f"<td>{'Right to left' if p['dir'] == 'rtl' else 'Left to right'}</td>"
                     f"<td>{books[code] if code in books else '–'}</td><td>{action}</td></tr>")
        search = ("<form method='get' action='/language' class='card'><label for='q'>Find a language</label>"
                  f"<input type='text' id='q' name='q' value='{esc(query)}' placeholder='swahili, kiswahili, swa'>"
                  "<button class='secondary'>Find</button>"
                  + (" <a href='/language'>Show all</a>" if query else "") + "</form>")
        table = (f"<table><tr><th>Language</th><th>MeshLearn's words</th><th>Writing</th><th>Book collections</th><th></th></tr>{rows}</table>"
                 if rows else "<p class='muted'>No language matches. Try its name in English.</p>")
        note = ("" if books else "<p class='muted'>Book counts appear once the list of library collections is downloaded "
                "on the <a href='/library'>Card library</a> page.</p>")
        body = ((f"<p class='good' role='status'>{esc(notice)}</p>" if notice else "") + top
                + "<h2>Languages for learning, by country</h2>" + countries + second_form
                + "<h2>All languages</h2>" + search + note + table)
        return Response(self._page("School language", body))

    def _country_cards(self) -> str:
        country, young = self.setting("country"), self.all_under_12()
        cards = ""
        for key, info in lang.COUNTRIES.items():
            codes = lang.languages_for(key, young)
            names = "".join(f"<li>{self._language_label(lang.build_pack(c, country=key))}</li>" for c in codes)
            button = ("<span class='pill good'>Your country</span>" if key == country else "")
            text = "Set these languages again" if key == country else f"Use for {info['name']}"
            cards += (f"<form method='post' action='/language/country' class='card' style='margin:0'>"
                      f"<input type='hidden' name='country' value='{key}'><strong>{esc(info['name'])}</strong> {button}"
                      f"<ol style='margin:8px 0;padding-left:22px'>{names}</ol><p class='muted'>{esc(info['why'])}</p>"
                      f"<button class='secondary'>{esc(text)}</button></form>")
        hint = ""
        dropped = [c for c in lang.COUNTRIES.get(country, {}).get("from_12", [])
                   if young and c in [self.school_language()] + self.other_languages()]
        if dropped:
            hint = (f"<p class='warn'>Your students are all under 12, and {esc(lang.COUNTRIES[country]['name'])} leaves out "
                    f"{esc(cat.language_name(dropped[0]))} for them. Press “Set these languages again” to leave it out.</p>")
        ages = ("Your students are all under 12." if young else "Some of your students are 12 or older.")
        return (f"<p class='muted'>Sets the school's main language and the others students can choose. {ages} "
                f"<a href='/library'>Change their ages</a></p>{hint}<div class='grid'>{cards}</div>")

    def language_country(self, req: Request) -> Response:
        country = req.form.get("country", "").strip().upper()
        codes = lang.languages_for(country, self.all_under_12())
        if not codes:
            return redirect("/language?" + urlencode({"msg": "That country is not in the list. Choose the languages below."}))
        self.set_setting("country", country)
        for key, code in zip(("lang", "lang2", "lang3"), codes + ["", ""]):
            self.set_setting(key, code)
        names = [self.language_pack(c)["name"] for c in codes]
        msg = f"Set for {lang.COUNTRIES[country]['name']}: {', '.join(names)}. Phones switch with their next lesson copy."
        return redirect(("/?" if req.form.get("back") == "/" else "/language?") + urlencode({"msg": msg}))

    def language_choose(self, req: Request) -> Response:
        code = req.form.get("lang", "").strip()
        if not self._valid_code(code):
            return redirect("/language?" + urlencode({"msg": "That language is not in the list."}))
        changed = code != self.school_language()
        self.set_setting("lang", code)
        for key in ("lang2", "lang3"):
            other = req.form.get(key, "").strip() if key in req.form else self.setting(key)
            self.set_setting(key, other if self._valid_code(other) and other != code else "")
        name = self.language_pack(code)["name"]
        msg = (f"Your school now uses {name}. Phones switch with their next lesson copy." if changed
               else "Saved. Phones get the change with their next lesson copy.")
        return redirect("/language?" + urlencode({"msg": msg}))

    def language_words(self, req: Request) -> Response:
        code = (req.query.get("lang") or self.school_language()).strip()
        if not self._valid_code(code):
            return redirect("/language")
        show = req.query.get("show", "all")
        notice = req.query.get("msg", "")
        src = lang.source()
        notes = src.get("notes", {})
        base = lang.bundled().get(code, {}).get("strings", {})
        mine = self.phrases(code)
        pack = self.language_pack(code)
        name, direction = self._meta(code)
        pct = lang.coverage(pack)
        rows = ""
        shown = 0
        for key in lang.keys():
            value = mine.get(key, base.get(key, ""))
            if show == "missing" and key in pack["strings"]:
                continue
            shown += 1
            english = src["strings"][key]
            note = notes.get(key, "")
            field = "k." + key
            rows += (
                f"<tr><td style='width:45%'><span lang='en'>{esc(english)}</span>"
                + (f"<div class='muted' style='font-size:14px'>{esc(note)}</div>" if note else "")
                + f"<div class='muted' style='font-size:13px'>{esc(key)}</div></td>"
                f"<td><label for='{esc(field)}' style='position:absolute;left:-9999px'>{esc(pack['name'])}: {esc(english)}</label>"
                f"<input type='text' id='{esc(field)}' name='{esc(field)}' value='{esc(value)}' lang='{esc(pack['tag'])}' "
                f"dir='{esc(pack['dir'])}' placeholder='Not translated yet: students see the English'></td></tr>"
            )
        own = ""
        if code not in lang.bundled():
            own = (
                "<div class='grid'>"
                f"<div><label for='_name'>The language's name, written in it</label>"
                f"<input type='text' id='_name' name='_name' value='{esc(name)}' placeholder='{esc(cat.language_name(code))}'></div>"
                "<div><label for='_dir'>Writing</label><select id='_dir' name='_dir'>"
                f"<option value='ltr'{' selected' if pack['dir'] == 'ltr' else ''}>Left to right</option>"
                f"<option value='rtl'{' selected' if pack['dir'] == 'rtl' else ''}>Right to left</option></select></div></div>"
            )
        tabs = (f"<p><a class='button{'' if show != 'missing' else ' secondary'}' href='/language/words?lang={esc(code)}'>All words</a>"
                f"<a class='button{'' if show == 'missing' else ' secondary'}' href='/language/words?lang={esc(code)}&amp;show=missing'>"
                "Missing only</a></p>")
        form = (
            "<form method='post' action='/language/words'>"
            f"<input type='hidden' name='lang' value='{esc(code)}'><input type='hidden' name='show' value='{esc(show)}'>"
            + own
            + (f"<table><tr><th>English</th><th>{esc(pack['name'])}</th></tr>{rows}</table>" if shown else
               "<p class='good'>Every word is translated.</p>")
            + "<button>Save</button></form>"
        )
        share = (
            "<h2>Share with other schools</h2><div class='card'>"
            "<p>Download these words to give to another school on a memory card, or to send to the MeshLearn team "
            "the next time this laptop is online, so every school using this language gets them.</p>"
            f"<a class='button secondary' href='/language/export/{esc(code)}'>Download the words</a>"
            "<label class='button secondary' for='lang-file'>Add words from a file…</label>"
            "<input type='file' id='lang-file' accept='application/json,.json' style='display:none'>"
            "<p id='lang-status' role='status' class='muted'></p></div>"
            "<script>document.getElementById('lang-file').addEventListener('change', async e => {"
            "const f = e.target.files[0]; if (!f) return; const out = document.getElementById('lang-status');"
            "out.textContent = 'Reading…';"
            "const res = await fetch('/api/language/import', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: await f.text()});"
            "const d = await res.json(); out.textContent = res.ok ? `Added ${d.saved} words for ${d.name} (${d.skipped} skipped). "
            "Reload to see them.` : 'Could not read that file: ' + d.error; });</script>"
        )
        intro = (f"<p>{pct}% translated. The words students see on the phone: buttons, headings and messages. "
                 "Your assignments are separate: write those straight in this language. Words in {curly brackets} "
                 "are filled in by the phone, so keep them as they are.</p>")
        body = ((f"<p class='good' role='status'>{esc(notice)}</p>" if notice else "")
                + "<p><a href='/language'>School language</a></p>" + intro + tabs + form + share)
        return Response(self._page(f"Translate MeshLearn into {pack['name']}", body))

    def _save_phrase(self, code: str, key: str, text: str) -> None:
        base = lang.bundled().get(code, {}).get("strings", {}).get(key, "")
        if not text or text == base:
            self.db.execute("DELETE FROM phrases WHERE lang = ? AND key = ?", (code, key))
        else:
            self.db.execute("INSERT OR REPLACE INTO phrases (lang, key, text) VALUES (?, ?, ?)", (code, key, text))

    def language_words_save(self, req: Request) -> Response:
        code = req.form.get("lang", "").strip()
        if not self._valid_code(code):
            return redirect("/language")
        problems: List[str] = []
        saved = 0
        for key in lang.keys():
            field = "k." + key
            if field not in req.form:
                continue
            text = " ".join(req.form[field].split())[:400]
            problem = lang.check(key, text) if text else ""
            if problem:
                problems.append(f"“{text}” ({problem})")
                continue
            self._save_phrase(code, key, text)
            saved += 1
        if code not in lang.bundled():
            name = " ".join(req.form.get("_name", "").split())[:60]
            for key, value in (("_name", name), ("_dir", req.form.get("_dir", "") if req.form.get("_dir") in ("ltr", "rtl") else "")):
                if value:
                    self.db.execute("INSERT OR REPLACE INTO phrases (lang, key, text) VALUES (?, ?, ?)", (code, key, value))
                else:
                    self.db.execute("DELETE FROM phrases WHERE lang = ? AND key = ?", (code, key))
        msg = "Saved. Phones get the words with their next lesson copy."
        if problems:
            msg = f"Saved, except {len(problems)}: " + "; ".join(problems[:5]) + ("…" if len(problems) > 5 else "")
        params = {"lang": code, "msg": msg}
        if req.form.get("show") == "missing":
            params["show"] = "missing"
        return redirect("/language/words?" + urlencode(params))

    def language_export(self, req: Request) -> Response:
        code = req.params["code"]
        if not self._valid_code(code):
            return json_response({"error": "unknown language"}, status=404)
        pack = self.language_pack(code)
        pack["reviewed"] = False
        return Response(json.dumps(pack, ensure_ascii=False, indent=1), content_type="application/json",
                        headers={"Content-Disposition": f"attachment; filename=meshlearn-words-{code}.json"})

    def language_import(self, req: Request) -> Response:
        """Words from another school's file: added as this hub's own, if usable."""
        try:
            data = req.json()
            code = str(data.get("code", ""))
            strings = data.get("strings") or {}
            if not _CODE.match(code) or not isinstance(strings, dict):
                raise ValueError("this is not a MeshLearn words file")
        except (ValueError, AttributeError) as exc:
            return json_response({"error": str(exc)}, status=400)
        saved = skipped = 0
        for key, text in strings.items():
            text = " ".join(str(text).split())[:400] if isinstance(text, str) else ""
            if key not in lang.source()["strings"] or not text or lang.check(key, text):
                skipped += 1
                continue
            self._save_phrase(code, key, text)
            saved += 1
        if code not in lang.bundled():
            name = " ".join(str(data.get("name", "")).split())[:60]
            if name and name != code:
                self.db.execute("INSERT OR REPLACE INTO phrases (lang, key, text) VALUES (?, '_name', ?)", (code, name))
            if data.get("dir") in ("ltr", "rtl"):
                self.db.execute("INSERT OR REPLACE INTO phrases (lang, key, text) VALUES (?, '_dir', ?)", (code, data["dir"]))
        return json_response({"saved": saved, "skipped": skipped, "name": self.language_pack(code)["name"]})
