"""Who is using the phone: picking yourself by picture, name or PIN (the "Student sign-in" designs).

The teacher's class list comes with the lessons (Wi-Fi copy or memory card). Without one, students type
their name as before. Ages 5 to 7 tap their picture (and a secret picture, if the teacher turns it on),
ages 8 to 11 find their name, ages 12 and up type their name (and a PIN, if turned on).

Secrets keep classmates honest; they are not strong security. A 4-number PIN can be guessed by someone
with the phone's files, so phones slow guessing down and the list holds only a hash of each secret.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Dict, List, Optional, Tuple

from .language import Strings
from .web import IDLE_JS, PICTURES, SIGNIN_JS, Request, Response, esc, icon, picture, redirect

# Pictures students choose from, and the ones used as secrets.
STUDENT_PICTURES = [k for k in (
    "cat lion butterfly fish flower bird sun tree boat apple star dog moon frog heart ball leaf hen cow goat rabbit snail banana "
    "carrot maize mango cloud mountain fire car bicycle hat bee elephant turtle monkey").split() if k in PICTURES]
SECRET_PICTURES = [k for k in "apple ball house star moon key fish tree sun flower boat heart".split() if k in PICTURES]
MAX_TRIES = 5
LOCK_S = 60


def secret_hash(name: str, secret: str) -> str:
    """How the class list carries a secret. Kept the same in SignIn.kt."""
    return hashlib.sha256(f"{name}|{secret}".encode("utf-8")).hexdigest()[:32]


class PhoneSignIn:
    """Mixed into Phone: uses its db, look, words and _page."""

    def _signin_routes(self) -> None:
        self.app.add_route("GET", "/who", self.who_page)
        self.app.add_route("POST", "/who", self.who_submit)
        self.app.add_route("GET", "/new", self.new_page)
        self.app.add_route("GET", "/me", self.me_page)

    def _load_roster(self, data: dict) -> None:
        rows = data.get("students") if isinstance(data.get("students"), list) else []
        self.roster: List[dict] = [
            {"n": r["n"], "p": r.get("p", ""), "a": r.get("a") if r.get("a") in ("young", "mid", "teen") else "mid", "s": r.get("s", "")}
            for r in rows if isinstance(r, dict) and isinstance(r.get("n"), str) and r["n"]]
        ways = data.get("signin") if isinstance(data.get("signin"), dict) else {}
        self.signin = {"young": ways.get("young", "pic"), "mid": ways.get("mid", "list"), "teen": ways.get("teen", "list")}
        out = data.get("signout")
        self.signout_min = out if isinstance(out, int) and 0 <= out <= 240 else 0
        self._tries: Dict[str, Tuple[int, float]] = {}
        if self.roster:   # the class list says which ages there are
            have = {r["a"] for r in self.roster}
            self.looks = [k for k in ("young", "mid", "teen") if k in have]

    def people(self) -> List[dict]:
        """The class list, then newcomers who signed in on this phone and are not on it yet."""
        known = {r["n"].casefold() for r in self.roster}
        extra = [{"n": r["name"], "p": r["pic"], "a": r["look"], "s": ""} for r in self.db.all("SELECT * FROM newcomers ORDER BY name")
                 if r["name"].casefold() not in known]
        return self.roster + extra

    def person(self, name: str) -> Optional[dict]:
        return next((r for r in self.people() if r["n"].casefold() == (name or "").casefold()), None)

    def _needs_secret(self, person: Optional[dict]) -> bool:
        """Whether this student must also tap their secret picture or type their PIN."""
        return bool(person and person["s"] and self.signin.get(person["a"]) in ("secret", "pin"))

    def _proof(self, name: str) -> str:
        """Goes in the cookie of a student who gave their secret, so nobody can sign in by typing a cookie."""
        row = self.db.one("SELECT value FROM settings WHERE key = 'cookie_key'")
        if row is None:
            self.db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('cookie_key', ?)", (secrets.token_hex(16),))
            row = self.db.one("SELECT value FROM settings WHERE key = 'cookie_key'")
        return hmac.new(row["value"].encode(), name.encode("utf-8"), hashlib.sha256).hexdigest()[:24]

    def student(self, req: Request) -> str:
        """The signed-in student's name, or ''."""
        name, _, proof = (req.cookies.get("student") or "").partition("~")
        if name and self.roster and self._needs_secret(self.person(name)) and not hmac.compare_digest(proof, self._proof(name)):
            return ""
        return name

    def _sign_in(self, person: dict) -> Response:
        name = person["n"]
        return redirect("/").set_cookie("student", name + ("~" + self._proof(name) if self._needs_secret(person) else ""))

    # -- pages ----------------------------------------------------------------

    @staticmethod
    def _face(person: dict) -> str:
        return picture(person["p"], "face") if person["p"] in PICTURES else f"<span class='avatar' aria-hidden='true'>{esc(person['n'][:1].upper())}</span>"

    def _who_body(self, req: Request, w: Strings) -> str:
        """The sign-in page when there is a class list."""
        look = self.look(req)
        people = [r for r in self.people() if r["a"] == look]
        if look == "young" or (look == "mid" and self.signin["mid"] == "pic"):
            cards = "".join(f"<a class='kid' data-name='{esc(r['n'])}' href='/who?n={quote_name(r['n'])}'>{self._face(r)}<strong>{esc(r['n'])}</strong></a>"
                            for r in people)
            return (f"<p class='lead'>{esc(w('signin.tap'))}</p><div class='kids' id='kids'>{cards}</div>"
                    f"<a class='button big secondary' href='/new'>{esc(w('signin.not_here'))}</a>")
        people = sorted(people, key=lambda r: r["n"].casefold())
        rows = "".join(f"<a data-name='{esc(r['n'])}' href='/who?n={quote_name(r['n'])}'>{self._face(r)}<span class='grow'><strong>{esc(r['n'])}</strong></span>"
                       f"{icon('chev', 'arr')}</a>" for r in people)
        find = ""
        if look == "teen":
            find = (f"<label for='find'>{esc(w('welcome.name'))}</label><input type='text' id='find' autocomplete='off' "
                    f"placeholder='{esc(w('signin.type'))}'>")
        else:
            letters = sorted({r["n"][:1].upper() for r in people})
            if len(people) > 8:
                find = "<div class='letters'>" + "".join(f"<button type='button' data-letter='{esc(c)}' aria-pressed='{'true' if not c else 'false'}'>"
                                                         f"{esc(c) or '★'}</button>" for c in [""] + letters) + "</div>"
        return (f"<div id='lastone' style='display:none' data-label='{esc(w('signin.last'))}' data-im='{esc(w('signin.im'))}'></div>{find}"
                f"<div class='list' id='kids'>{rows}</div><p class='meta' id='nobody' style='display:none'>{esc(w('signin.none'))}</p>"
                f"<a class='button big secondary' href='/new'>{esc(w('signin.not_here'))}</a>")

    def who_page(self, req: Request, wrong: str = "") -> Response:
        """Are you Lena? Then the secret picture or PIN, when the teacher turned it on."""
        w = self.words(req)
        person = self.person(req.query.get("n") or req.form.get("n", ""))
        if person is None:
            return redirect("/")
        name = person["n"]
        head = f"<div class='done'>{self._face(person)}</div>"
        hidden = f"<input type='hidden' name='n' value='{esc(name)}'>"
        note = f"<p class='error' role='alert'>{esc(wrong)}</p>" if wrong else ""
        back = f"<a class='button big secondary' href='/'>{esc(w('form.change') if self._needs_secret(person) else w('signin.no'))}</a>"
        if not self._needs_secret(person):
            return Response(self._page(req, w("signin.are_you", name=name),
                                       head + f"<div class='yesno'>{back}<form method='post' action='/who'>{hidden}<button class='big yes'>{icon('check')} "
                                       f"{esc(w('signin.yes'))}</button></form></div>"))
        if self.signin.get(person["a"]) == "secret":
            buttons = "".join(f"<button name='secret' value='{esc(k)}' aria-label='{esc(PICTURES[k]['n'])}'>{picture(k)}</button>"
                              for k in self._secret_choices(person))
            return Response(self._page(req, name, head + f"<p class='lead'>{esc(w('signin.secret'))}</p>{note}"
                                       f"<form method='post' action='/who' class='secrets'>{hidden}{buttons}</form>" + back))
        return Response(self._page(req, name, head + f"<form method='post' action='/who' class='card'>{hidden}"
                                   f"<label for='pin'>{esc(w('signin.pin'))}</label>{note}<input type='password' id='pin' name='secret' "
                                   "class='pin' inputmode='numeric' pattern='[0-9]*' maxlength='4' autocomplete='off' required autofocus>"
                                   f"<button class='big'>{esc(w('welcome.start'))}</button></form>" + back))

    def _secret_choices(self, person: dict) -> List[str]:
        """Six pictures, the right one among them, always in the same order for one student."""
        right = next((k for k in SECRET_PICTURES if secret_hash(person["n"], k) == person["s"]), SECRET_PICTURES[0])
        by_hash = sorted((k for k in SECRET_PICTURES if k != right), key=lambda k: secret_hash(person["n"] + "#", k))
        return sorted([right] + by_hash[:5], key=lambda k: secret_hash(person["n"] + "@", k))

    def who_submit(self, req: Request) -> Response:
        w = self.words(req)
        person = self.person(req.form.get("n", ""))
        if person is None:
            return redirect("/")
        if not self._needs_secret(person):
            return self._sign_in(person)
        count, since = self._tries.get(person["n"], (0, 0.0))
        if count >= MAX_TRIES and time.time() - since < LOCK_S:
            return self.who_page(req, w("signin.wait"))
        if count >= MAX_TRIES:
            count = 0
        if hmac.compare_digest(secret_hash(person["n"], req.form.get("secret", "")[:20]), person["s"]):
            self._tries.pop(person["n"], None)
            return self._sign_in(person)
        self._tries[person["n"]] = (count + 1, time.time())
        pin = self.signin.get(person["a"]) == "pin"
        return self.who_page(req, w("signin.pin_wrong") if pin else w("signin.wrong") + (" " + w("signin.help") if count >= 2 else ""))

    def new_page(self, req: Request) -> Response:
        """A student who is not on the list: a name and a picture, and they can start."""
        w = self.words(req)
        taken = {r["p"] for r in self.people()}
        pics = "".join(f"<label class='picpick'><input type='radio' name='pic' value='{k}'><span>{picture(k)}</span></label>"
                       for k in [k for k in STUDENT_PICTURES if k not in taken][:12])
        return Response(self._page(req, w("signin.new_title"), f"<p class='lead'>{esc(w('signin.new_text'))}</p>"
                                   f"<form method='post' action='/login' class='card'><label for='student'>{esc(w('welcome.name'))}</label>"
                                   "<input type='text' id='student' name='student' maxlength='40' autocomplete='off' required>"
                                   f"<p><strong>{esc(w('signin.pick'))}</strong></p><div class='picks'>{pics}</div>"
                                   f"<button class='big'>{esc(w('welcome.start'))}</button></form>"))

    def login(self, req: Request) -> Response:
        name = " ".join(req.form.get("student", "").replace("~", " ").split())[:40]
        if not name:
            return redirect("/")
        if self.roster:
            person = self.person(name)
            if person:   # on the list after all: the same way in as everyone
                return redirect("/who?n=" + quote_name(person["n"])) if self._needs_secret(person) else self._sign_in(person)
            pic = req.form.get("pic", "")
            self.db.execute("INSERT OR REPLACE INTO newcomers (name, pic, look) VALUES (?, ?, ?)",
                            (name, pic if pic in STUDENT_PICTURES else "", self.look(req)))
        return redirect("/").set_cookie("student", name)

    def me_page(self, req: Request) -> Response:
        """Finished? Pass the phone on."""
        w = self.words(req)
        name = self.student(req)
        if not name:
            return redirect("/")
        person = self.person(name) or {"n": name, "p": "", "a": "mid", "s": ""}
        idle = f"<p class='meta'>{esc(w('signin.idle', n=self.signout_min))}</p>" if self.signout_min and self.roster else ""
        return Response(self._page(req, w("signin.finished", name=name),
                                   f"<div class='done'>{self._face(person)}<p>{esc(w('signin.saved'))}</p></div>"
                                   f"<form method='post' action='/logout'><button class='big'>{esc(w('signin.pass'))}</button></form>"
                                   f"<a class='button big secondary' href='/'>{esc(w('signin.keep'))}</a>{idle}"))

    def _idle_form(self, signed_in: bool) -> str:
        """Goes back to "Who are you?" by itself when nobody taps for a while (IDLE_JS)."""
        if not (signed_in and self.roster and self.signout_min):
            return ""
        return f"<form method='post' action='/logout' id='idleout' data-min='{self.signout_min}'></form><script>{IDLE_JS}</script>"

    def _signin_script(self) -> str:
        return f"<script>{SIGNIN_JS}</script>"


def quote_name(name: str) -> str:
    from urllib.parse import quote
    return quote(name, safe="")
