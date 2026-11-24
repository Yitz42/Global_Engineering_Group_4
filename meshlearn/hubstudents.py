"""Hub pages for the class list: students, their pictures and how they sign in (the "Students" design)."""

from __future__ import annotations

import secrets
import time
from typing import Dict, List
from urllib.parse import urlencode

from .phonesignin import SECRET_PICTURES, STUDENT_PICTURES, secret_hash
from .web import PICTURES, Request, Response, esc, picture, redirect

AGES = [("young", "Ages 5 to 7"), ("mid", "Ages 8 to 11"), ("teen", "Ages 12 and up")]
WAYS = {"young": [("pic", "Tap their picture"), ("secret", "Picture + secret picture")],
        "mid": [("list", "Find their name"), ("pic", "Tap their picture")],
        "teen": [("list", "Type their name"), ("pin", "Name + PIN")]}
SIGNOUTS = [("10", "10 minutes with no taps"), ("5", "5 minutes"), ("30", "30 minutes"), ("0", "Never")]


class HubStudents:
    """Mixed into Hub."""

    def _student_routes(self) -> None:
        app = self.app
        app.add_route("GET", "/students", self.students_page)
        app.add_route("POST", "/students/ways", self.students_ways)
        app.add_route("POST", "/students/add", self.students_add)
        app.add_route("POST", "/students/edit", self.students_edit)
        app.add_route("POST", "/students/link", self.students_link)
        app.add_route("GET", "/students/cards", self.students_cards)

    # -- data -----------------------------------------------------------------

    def students(self) -> List[dict]:
        return [dict(r) for r in self.db.all("SELECT * FROM students ORDER BY name COLLATE NOCASE")]

    def signin_ways(self) -> Dict[str, str]:
        return {a: (self.setting("signin_" + a) if self.setting("signin_" + a) in [v for v, _ in WAYS[a]] else WAYS[a][0][0]) for a, _ in AGES}

    def student_bundle(self) -> dict:
        """The class list as phones get it, with their lessons: names, pictures and hashed secrets."""
        ways = self.signin_ways()
        rows = []
        for s in self.students():
            row = {"n": s["name"], "p": s["pic"], "a": s["ages"]}
            if ways[s["ages"]] in ("secret", "pin"):
                row["s"] = secret_hash(s["name"], s["pin"] if ways[s["ages"]] == "pin" else s["secret"])
            if s["lang"]:
                row["l"] = s["lang"]
            rows.append(row)
        out = self.setting("signout")
        return {"students": rows, "signin": ways, "signout": int(out) if out.isdigit() else 10}

    def student_name(self, name: str) -> str:
        """The class-list name for a name that came from a phone ("Same as Amara")."""
        row = self.db.one("SELECT student FROM name_links WHERE name = ?", (name,))
        return row["student"] if row and row["student"] else name

    def new_names(self) -> List[dict]:
        """Names on work from phones that are not on the class list, when there is one."""
        if not self.db.one("SELECT 1 FROM students LIMIT 1"):
            return []
        return [dict(r) for r in self.db.all(
            "SELECT student AS name, COUNT(*) AS n, MAX(phone) AS phone FROM submissions WHERE source = 'radio' "
            "AND lower(student) NOT IN (SELECT lower(name) FROM students) AND student NOT IN (SELECT name FROM name_links) "
            "GROUP BY student ORDER BY student")]

    def add_student(self, name: str, ages: str) -> bool:
        name = " ".join(name.replace("~", " ").replace("|", " ").split())[:40]
        if not name or ages not in dict(AGES) or self.db.one("SELECT 1 FROM students WHERE lower(name) = lower(?)", (name,)):
            return False
        self.db.execute("DELETE FROM name_links WHERE name = ?", (name,))
        taken = {s["pic"] for s in self.students()}
        free = [k for k in STUDENT_PICTURES if k not in taken] or STUDENT_PICTURES
        self.db.execute("INSERT INTO students (name, pic, ages, secret, pin, added) VALUES (?, ?, ?, ?, ?, ?)",
                        (name, free[0], ages, secrets.choice(SECRET_PICTURES), f"{secrets.randbelow(10000):04d}", time.time()))
        return True

    # -- pages ----------------------------------------------------------------

    def students_page(self, req: Request) -> Response:
        msg = req.query.get("msg", "")
        rows_data, ways, names = self.students(), self.signin_ways(), self._phone_names()
        last = {r["student"]: r for r in self.db.all(
            "SELECT student, phone, MAX(COALESCE(submitted_at, received_at)) AS at FROM submissions GROUP BY student")}

        def options(pairs, chosen):
            return "".join(f"<option value='{v}'{' selected' if v == chosen else ''}>{esc(t)}</option>" for v, t in pairs)

        ways_form = "<form method='post' action='/students/ways' class='panel'><h2 style='margin-top:0'>How they sign in</h2>" + "".join(
            f"<div><label for='way_{a}'>{esc(t)}</label><select id='way_{a}' name='{a}'>{options(WAYS[a], ways[a])}</select></div>" for a, t in AGES) + (
            f"<div><label for='signout'>Sign out after</label><select id='signout' name='signout'>{options(SIGNOUTS, self.setting('signout') or '10')}</select></div>"
            "<button class='secondary'>Save</button></form>")
        add = ("<form method='post' action='/students/add' class='panel' id='add'><h2 style='margin-top:0'>Add students</h2><label for='names'>Names, one per line</label>"
               "<textarea id='names' name='names' rows='4' placeholder='Amara Osei&#10;Kofi Asante'></textarea>"
               f"<label for='ages'>Their age group</label><select id='ages' name='ages'>{options(AGES, 'mid')}</select>"
               "<button>Add students</button></form>")
        rows = ""
        for s in rows_data:
            way = ways[s["ages"]]
            secret = (f"Secret: {picture(s['secret'], 'mini')} {esc(PICTURES[s['secret']]['n'])}" if way == "secret" and s["secret"] in PICTURES
                      else f"PIN {esc(s['pin'])}" if way == "pin" else "")
            new = ("<button name='do' value='secret' class='secondary'>New " + ("PIN" if way == "pin" else "secret") + "</button>") if secret else ""
            seen = last.get(s["name"])
            work = f"{esc(names.get(seen['phone'], seen['phone']))} · {time.strftime('%b %d', time.localtime(seen['at']))}" if seen else "No work yet"
            pics = "".join(f"<option value='{k}'{' selected' if k == s['pic'] else ''}>{esc(PICTURES[k]['n'])}</option>" for k in STUDENT_PICTURES)
            rows += (f"<tr><td>{picture(s['pic'], 'mini') if s['pic'] in PICTURES else ''}</td>"
                     "<td><form method='post' action='/students/edit' class='inline'>"
                     f"<input type='hidden' name='old' value='{esc(s['name'])}'>"
                     f"<input type='text' name='name' value='{esc(s['name'])}' maxlength='40' aria-label='Name'>"
                     f"<select name='pic' aria-label='Picture'>{pics}</select><select name='ages' aria-label='Age group'>{options(AGES, s['ages'])}</select>"
                     f"<button name='do' value='save' class='secondary'>Save</button>{new}"
                     "<button name='do' value='remove' class='secondary'>Remove</button></form>"
                     f"<div class='muted'>{secret}{' · ' if secret else ''}{work}</div></td></tr>")
        fresh = ""
        for r in self.new_names():
            same = "".join(f"<option>{esc(s['name'])}</option>" for s in rows_data)
            fresh += (f"<div class='phonecard quiet' style='display:block'><strong>{esc(r['name'])}</strong> <span class='muted'>· {esc(names.get(r['phone'], r['phone']))} · "
                      f"{r['n']} piece{'' if r['n'] == 1 else 's'} of work</span>"
                      "<form method='post' action='/students/link' class='inline'>"
                      f"<input type='hidden' name='name' value='{esc(r['name'])}'><select name='ages' aria-label='Age group'>{options(AGES, 'mid')}</select>"
                      "<button name='do' value='add'>Add as new student</button>"
                      f"<select name='same' aria-label='Same as'>{same}</select><button name='do' value='same' class='secondary'>Same as this student</button>"
                      "<button name='do' value='ignore' class='secondary'>Not a student</button></form></div>")
        main = (
            ("" if rows_data else "<p>Add your class and students pick themselves on the phone, by picture or name, instead of typing. "
               "Without a list, they type their name as before.</p>")
            + (f"<h2>New names from phones</h2>{fresh}" if fresh else "")
            + (f"<table class='plain'>{rows}</table>" if rows else ""))
        if not rows_data:
            main += add
        side = ((add if rows_data else "") + ways_form
            + "<p class='small'>The list reaches phones with their next lesson copy, over Wi-Fi or on the memory card. It holds names, pictures "
            "and scrambled secrets. Secrets and PINs keep classmates honest; they are not strong protection. Changing a name here also "
            "renames that student's work.</p>"
        )
        body = ((f"<div class='notice' role='status'>{esc(msg)}</div>" if msg else "")
                + f"<div class='cols'><div class='wide'>{main}</div><aside class='col' style='flex:1 1 300px'>{side}</aside></div>")
        action = ("<span><a class='button secondary' href='/students/cards'>Print picture cards</a> <a class='button' href='#add'>Add students</a></span>"
                  if rows_data else "")
        return Response(self._page("Students", body, sub=f"{len(rows_data)} on the class list" if rows_data else "", action=action))

    def students_ways(self, req: Request) -> Response:
        for a, _ in AGES:
            if req.form.get(a) in [v for v, _ in WAYS[a]]:
                self.set_setting("signin_" + a, req.form[a])
        if req.form.get("signout") in [v for v, _ in SIGNOUTS]:
            self.set_setting("signout", req.form["signout"])
        return redirect("/students?" + urlencode({"msg": "Saved. Phones get it with their next lesson copy."}))

    def students_add(self, req: Request) -> Response:
        lines = [n for n in req.form.get("names", "").splitlines() if n.strip()]
        added = sum(self.add_student(n, req.form.get("ages", "")) for n in lines[:200])
        skipped = len(lines) - added
        msg = f"Added {added} student{'' if added == 1 else 's'}." + (f" {skipped} already on the list." if skipped else "")
        return redirect("/students?" + urlencode({"msg": msg}))

    def students_edit(self, req: Request) -> Response:
        old, do = req.form.get("old", ""), req.form.get("do", "")
        row = self.db.one("SELECT * FROM students WHERE name = ?", (old,))
        if row is None:
            return redirect("/students")
        if do == "remove":
            self.db.execute("DELETE FROM students WHERE name = ?", (old,))
            return redirect("/students?" + urlencode({"msg": f"{old} is off the list. Their work is kept."}))
        if do == "secret":
            self.db.execute("UPDATE students SET secret = ?, pin = ? WHERE name = ?",
                            (secrets.choice([k for k in SECRET_PICTURES if k != row["secret"]]), f"{secrets.randbelow(10000):04d}", old))
            return redirect("/students?" + urlencode({"msg": f"New secret for {old}. It works after the phones' next lesson copy."}))
        name = " ".join(req.form.get("name", "").replace("~", " ").replace("|", " ").split())[:40] or old
        clash = self.db.one("SELECT 1 FROM students WHERE lower(name) = lower(?) AND name <> ?", (name, old))
        if clash:
            return redirect("/students?" + urlencode({"msg": f"{name} is already on the list."}))
        pic = req.form.get("pic", "")
        ages = req.form.get("ages", "")
        self.db.execute("UPDATE students SET name = ?, pic = ?, ages = ? WHERE name = ?",
                        (name, pic if pic in STUDENT_PICTURES else row["pic"], ages if ages in dict(AGES) else row["ages"], old))
        if name != old:
            self._rename(old, name)
        return redirect("/students?" + urlencode({"msg": f"Saved {name}."}))

    def _rename(self, old: str, new: str) -> None:
        """Work under one name moves to another, now and when more arrives."""
        self.db.execute("UPDATE submissions SET student = ? WHERE student = ?", (new, old))
        self.db.execute("UPDATE name_links SET student = ? WHERE student = ?", (new, old))
        self.db.execute("DELETE FROM name_links WHERE name = ?", (new,))
        self.db.execute("INSERT OR REPLACE INTO name_links (name, student) VALUES (?, ?)", (old, new))

    def students_link(self, req: Request) -> Response:
        name, do = req.form.get("name", ""), req.form.get("do", "")
        if do == "add":
            ok = self.add_student(name, req.form.get("ages", ""))
            msg = f"Added {name}." if ok else f"{name} is already on the list."
        elif do == "same" and self.db.one("SELECT 1 FROM students WHERE name = ?", (req.form.get("same", ""),)):
            self._rename(name, req.form["same"])
            msg = f"{name}'s work now counts as {req.form['same']}'s."
        elif do == "ignore":
            self.db.execute("INSERT OR REPLACE INTO name_links (name, student) VALUES (?, '')", (name,))
            msg = f"{name} won't be asked about again. Their work is kept."
        else:
            msg = "Choose a student first."
        return redirect("/students?" + urlencode({"msg": msg}))

    def students_cards(self, req: Request) -> Response:
        ways = self.signin_ways()
        cards = ""
        for s in self.students():
            way = ways[s["ages"]]
            secret = (f"<div>Secret {picture(s['secret'], 'mini')}</div>" if way == "secret" and s["secret"] in PICTURES
                      else f"<div>PIN <strong>{esc(s['pin'])}</strong></div>" if way == "pin" else "")
            cards += f"<div class='pcard'>{picture(s['pic'], 'face') if s['pic'] in PICTURES else ''}<strong>{esc(s['name'])}</strong>{secret}</div>"
        body = ("<style>.pcards{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}.pcard{border:2px solid var(--line);"
                "border-radius:14px;padding:12px;text-align:center;break-inside:avoid}.pcard svg.face{width:90px;height:90px;display:block;margin:0 auto}"
                "@media print{.side,.top .sub,.noprint{display:none!important}}</style>"
                "<p class='noprint'><a href='/students'>← Students</a> · <a href='#' onclick='print();return false'>Print</a>. Cards with secrets are for the "
                f"student only.</p><div class='pcards'>{cards}</div>")
        return Response(self._page("Picture cards", body, current="students"))
