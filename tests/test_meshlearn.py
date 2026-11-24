"""Tests for the protocol, delivery, grading, library planning and the full hub-to-phone loop.

Run with: python -m unittest discover -s tests -v
"""

import contextlib
import functools
import http.server
import io
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.cookiejar import CookieJar
from unittest import mock

from meshlearn import assignments as asg
from meshlearn import catalog, comprehension, courses, kolibri, library, protocol, radioports
from meshlearn.hub import Hub
from meshlearn.messenger import Messenger
from meshlearn.phone import Phone
from meshlearn.transport import UdpSimTransport
from meshlearn.web import Request

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "meshlearn", "sample_assignment.json")
GOOD_ANSWERS = {"q1": "1", "q2": "0,75", "q3": "chlorophyll", "q4": "1", "q5": "We share bread."}


def load_sample():
    with open(SAMPLE, encoding="utf-8") as fh:
        return json.load(fh)


def free_port(kind=socket.SOCK_STREAM):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for(cond, timeout=30.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


class ProtocolTests(unittest.TestCase):
    def test_round_trip_multi_frame(self):
        payload = {"text": "x" * 5000, "n": list(range(300))}
        msg_id, frames = protocol.encode(protocol.SUBMIT, payload)
        self.assertGreater(len(frames), 1)
        self.assertTrue(all(len(f) <= protocol.MAX_FRAME_BYTES for f in frames))
        self.assertTrue(protocol.parse_header(frames[-1]).end)
        self.assertFalse(protocol.parse_header(frames[0]).end)
        asm = protocol.Assembler()
        out = None
        for f in [protocol.set_end(f, False) for f in reversed(frames)]:  # order does not matter
            out = asm.add(f, "a").message or out
        self.assertEqual(out.payload, payload)
        self.assertEqual(out.msg_id, msg_id)

    def test_end_of_burst_reports_missing_pieces(self):
        _, frames = protocol.encode(protocol.SUBMIT, {"blob": os.urandom(900).hex()})
        asm = protocol.Assembler()
        asm.add(frames[0], "a")
        result = asm.add(frames[-1], "a")
        self.assertIsNone(result.message)
        self.assertEqual(result.missing, list(range(1, len(frames) - 1)))

    def test_corruption_is_rejected(self):
        _, frames = protocol.encode(protocol.HELLO, {"n": "phone", "pad": "a" * 50})
        bad = frames[0][:-3] + b"xyz"
        with self.assertRaises(protocol.ProtocolError):
            protocol.Assembler().add(bad, "a")

    def test_too_big_is_refused(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.encode(protocol.ASSIGN, {"blob": os.urandom(80_000).hex()})

    def test_sample_assignment_is_small(self):
        packed = asg.pack(asg.public_view(asg.normalize_assignment(load_sample())))
        self.assertLessEqual(protocol.frame_count(protocol.ASSIGN, {"a": packed, "o": 1}), 2)
        self.assertEqual(protocol.frame_count(protocol.OPEN, {"a": "a-fractions1", "v": "0123abcd", "o": 1, "d": "2026-11-06"}), 1)


class StudentSiteTests(unittest.TestCase):
    def test_due_dates_in_words(self):
        from datetime import date

        from meshlearn.web import due_label, minutes_for
        today = date(2026, 11, 15)
        self.assertEqual(due_label("2026-11-15", False, today), ("Due today", "soon"))
        self.assertEqual(due_label("2026-11-16", False, today), ("Due tomorrow", "soon"))
        self.assertEqual(due_label("2026-11-20", False, today), ("Due Fri, Nov 20", ""))
        self.assertEqual(due_label("2026-10-16", False, today), ("Overdue · was due Fri, Oct 16", "late"))
        self.assertEqual(due_label("2026-10-16", True, today), ("Was due Fri, Oct 16", "done"))
        self.assertEqual(due_label("", False, today), ("", ""))
        self.assertEqual(minutes_for([{"type": t} for t in ("choice", "number", "number", "long")]), "about 6 min")


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.a = asg.normalize_assignment(load_sample())

    def test_pack_round_trip_is_lossless(self):
        plain = dict(self.a, questions=[{k: v for k, v in q.items() if k not in asg.OWN_FIELDS} for q in self.a["questions"]])
        self.assertEqual(asg.unpack(asg.pack(self.a)), plain)   # all but the teacher's own notes
        view = asg.public_view(self.a)
        self.assertEqual(asg.unpack(asg.pack(view)), view)

    def test_version_ignores_due_date_but_not_questions(self):
        v1 = asg.version_of(self.a)
        moved = dict(self.a, due="2026-12-01")
        self.assertEqual(asg.version_of(moved), v1)
        changed = json.loads(json.dumps(self.a))
        changed["questions"][0]["prompt"] = "Which fraction equals one half?"
        self.assertNotEqual(asg.version_of(changed), v1)

    def test_public_view_hides_key_in_graded_mode(self):
        for q in asg.public_view(self.a)["questions"]:
            self.assertNotIn("answer", q)
            self.assertNotIn("accept", q)

    def test_validation_catches_blank_and_bad_numbers(self):
        answers, errors = asg.validate_answers(self.a, {"q1": "1", "q2": "lots"})
        self.assertIn("q2", errors)
        self.assertIn("q3", errors)
        self.assertEqual(answers, {"q1": "1"})

    def test_number_formats(self):
        self.assertEqual(asg.parse_number("3,5"), 3.5)
        self.assertEqual(asg.parse_number("3/4"), 0.75)
        self.assertIsNone(asg.parse_number("1/0"))
        self.assertIsNone(asg.parse_number("abc"))

    def test_grading_with_manual_long_answer(self):
        answers = {"q1": "1", "q2": "3/4", "q3": " Chlorophyll. ", "q4": "0", "q5": "Cooking"}
        g = asg.grade(self.a, answers)
        self.assertEqual(g["score"], 3)
        self.assertFalse(g["complete"])
        g = asg.grade(self.a, answers, {"q5": 2})
        self.assertEqual((g["score"], g["complete"], g["max"]), (5, True, 7))

    def test_bad_assignment_rejected(self):
        with self.assertRaises(asg.AssignmentError):
            asg.normalize_assignment({"title": "x", "questions": [{"type": "choice", "prompt": "?", "options": ["a"], "answer": 0}]})


class QuestionInfoTests(unittest.TestCase):
    RAIN = "Rainfall:\nJanuary 120 mm\nFebruary 95 mm"

    def lesson(self):
        return asg.normalize_assignment({"title": "Rain", "instructions": "Use the table.", "questions": [
            {"type": "number", "prompt": "Total?", "answer": 215, "info": self.RAIN},
            {"type": "number", "prompt": "Difference?", "answer": 25, "info": self.RAIN},
            {"type": "text", "prompt": "Why <b>?</b>", "accept": ["x"]},
            {"type": "long", "prompt": "Explain", "info": self.RAIN + "!"}]})

    def test_shared_info_travels_once(self):
        a = self.lesson()
        packed = asg.pack(a)
        self.assertEqual([q.get("n") for q in packed["Q"]], [self.RAIN, 1, None, self.RAIN + "!"])
        self.assertEqual(asg.unpack(json.loads(json.dumps(packed))), a)
        self.assertFalse(any("n" in q for q in asg.pack(asg.normalize_assignment(load_sample()))["Q"]))

    def test_student_page_has_info_arrows_and_review(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        phone = Phone(os.path.join(tmp, "card"), "hub", "P", lambda h: _QuietRadio())
        a = self.lesson()
        phone.import_assignment(asg.pack(a), open_=True)
        req = Request("GET", f"/a/{a['id']}", {}, {"Cookie": "student=Ana"}, b"", "192.168.43.9")
        page_html = phone.app.dispatch(req).body.decode()
        self.assertEqual(page_html.count("<details class='qinfo'"), 4)      # the instructions go with every question
        self.assertEqual(page_html.count("<details class='qinfo' open>"), 1)
        self.assertEqual(page_html.count("<summary>Question info</summary>"), 3)
        self.assertEqual(page_html.count("<summary>Instructions</summary>"), 1)
        self.assertIn("January 120 mm", page_html)
        self.assertIn("data-prompt='Why &lt;b&gt;?&lt;/b&gt;'", page_html)
        for part in ("id='back'", "id='next'", "id='toreview'", "id='review'", "Review your answers", "Hand it in", "id='qprog'", "data-listen"):
            self.assertIn(part, page_html)
        self.assertEqual(page_html.count("data-flag='"), 4)
        self.assertNotIn("name='flag", page_html)                           # flags stay in the browser


class LookTests(unittest.TestCase):
    """The three looks by age: the hub's ages decide which a phone offers, the student's age which they see."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.phone = Phone(os.path.join(self.tmp, "card"), "hub", "P", lambda h: _QuietRadio())
        self.a = asg.normalize_assignment({"title": "Counting", "course": "math", "mode": "practice", "due": "2026-12-01", "questions": [
            {"type": "choice", "prompt": "How many?", "options": ["2", "3"], "answer": 1},
            {"type": "choice", "prompt": "Who?", "options": ["A white rabbit", "A cat"], "answer": 0}]})
        b = dict(self.a, id="a-two", title="Second", mode="graded")
        self.phone.import_assignment(asg.pack(self.a), open_=True)
        self.phone.import_assignment(asg.pack(asg.normalize_assignment(b)), open_=True)

    def ages(self, *ages):
        from meshlearn import language as lang
        self.phone.import_language({"school": lang.build_pack("eng"), "second": None, "more": [], "ages": list(ages)})

    def get(self, path, cookie="student=Ana", method="GET", body=b""):
        return self.phone.app.dispatch(Request(method, path, {}, {"Cookie": cookie}, body, "192.168.43.9"))

    def page(self, *args):
        return self.get(*args).body.decode()

    def test_hub_ages_choose_the_look(self):
        self.assertIn("<body class='st look-mid'>", self.page("/"))                  # until the hub has said
        for ages, look in ((["early"], "young"), (["primary"], "mid"), (["lower", "upper"], "teen"), (["adult"], "teen")):
            self.ages(*ages)
            self.assertIn(f"<body class='st look-{look}'>", self.page("/"))
            self.assertNotIn("How old are you?", self.page("/", ""))
        self.ages("early", "primary", "upper")
        self.assertIn("look-mid", self.page("/"))
        welcome = self.page("/", "")
        for text in ("How old are you?", "5 to 7", "8 to 11", "12 or older"):
            self.assertIn(text, welcome)
        chosen = self.get("/look", "", "POST", b"to=young")
        self.assertIn("look=young", chosen.headers["Set-Cookie"])
        self.assertIn("<body class='st look-young'>", self.page("/", "student=Ana; look=young"))
        self.assertNotIn("Set-Cookie", self.get("/look", "", "POST", b"to=<script>").headers)
        self.assertIn("look-mid'", self.page("/", "student=Ana; look=<script>"))
        hub = Hub(os.path.join(self.tmp, "hub.db"), lambda h: _QuietRadio())
        self.assertEqual(hub.language_bundle()["ages"], ["lower", "upper"])

    def test_young(self):
        self.ages("early")
        home = self.page("/")
        self.assertIn("class='bigplay'", home)
        self.assertIn("Stars this week: 0", home)
        self.assertIn("</span>My stars</a>", home)
        form = self.page(f"/a/{self.a['id']}")
        self.assertEqual(form.count("class='opts short'"), 1)      # "2" and "3" side by side, the long answers one per line
        done = self.page(f"/a/{self.a['id']}", "student=Ana", "POST", b"q1=1&q2=1")
        self.assertIn("1 of 2 right", done)
        self.phone.db.execute("UPDATE submissions SET score = 1, max = 2, complete = 1, detail = ?", ('{"q1": 1, "q2": 0}',))
        stars = self.page("/results")
        self.assertIn("<h1>My stars</h1>", stars)
        self.assertIn("aria-label='2 / 3'", stars)                  # half right: 1.5 stars, rounded up
        self.assertIn("Try for more stars", stars)
        self.assertIn("Stars this week: 2", self.page("/"))

    def test_teen(self):
        self.ages("upper")
        home = self.page("/")
        self.assertIn("class='next'", home)
        self.assertIn("<b>1</b></span>", home)                     # the due day in the list
        self.assertIn("class='tiles'", home)
        self.assertIn("data-draft='Ana|", home)
        self.page(f"/a/{self.a['id']}", "student=Ana", "POST", b"q1=1&q2=1")
        self.phone.db.execute("UPDATE submissions SET score = 1, max = 2, complete = 1, detail = ?", ('{"q1": 1, "q2": 0}',))
        results = self.page("/results")
        self.assertIn("To review", results)
        self.assertIn("You said: A cat", results)
        self.assertIn("All marks", results)
        self.phone.db.execute("UPDATE submissions SET complete = 0")
        results = self.page("/results")
        self.assertIn("1 / 2 so far<br>Your teacher is reading it", results)
        self.assertIn("<br><span class='pill", results)

    def test_handed_in_says_where_the_work_is(self):
        page_html = self.page("/a/a-two", "student=Ana", "POST", b"q1=1&q2=0")
        for text in ("Where your work is", "Sent by radio", "Marked by your teacher", "give the phone to the next person"):
            self.assertIn(text, page_html)
        self.ages("early")
        self.phone.db.execute("DELETE FROM submissions")
        self.assertIn("<h1>Well done!</h1>", self.page("/a/a-two", "student=Ana", "POST", b"q1=1&q2=0"))


class SignInTests(unittest.TestCase):
    """The class list from the hub's Students page to signing in on the phone."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hub = Hub(os.path.join(self.tmp, "hub.db"), lambda h: _QuietRadio())
        self.phone = Phone(os.path.join(self.tmp, "card"), "hub", "P", lambda h: _QuietRadio())

    def hub_post(self, path, **form):
        return self.hub.app.dispatch(Request("POST", path, {}, {}, urllib.parse.urlencode(form).encode(), "127.0.0.1"))

    def hub_page(self, path):
        path, _, query = path.partition("?")
        return self.hub.app.dispatch(Request("GET", path, dict(urllib.parse.parse_qsl(query)), {}, b"", "127.0.0.1")).body.decode()

    def go(self, path, cookie="", method="GET", **form):
        path, _, query = path.partition("?")
        return self.phone.app.dispatch(Request(method, path, dict(urllib.parse.parse_qsl(query)), {"Cookie": cookie},
                                               urllib.parse.urlencode(form).encode(), "192.168.43.9"))

    def sync(self):
        self.phone.import_language(json.loads(json.dumps(self.hub.language_bundle())))

    def add(self, ages, *names):
        self.hub_post("/students/add", names="\n".join(names), ages=ages)

    def test_without_a_list_students_type_their_name(self):
        self.sync()
        self.assertIn("Type your name", self.go("/").body.decode())
        self.assertEqual(self.go("/login", "", "POST", student=" Ana~x ").headers["Set-Cookie"].split(";")[0], "student=Ana%20x")

    def test_young_students_tap_their_picture(self):
        self.add("young", "Lena", "Kofi", "Lena")
        self.assertEqual([s["name"] for s in self.hub.students()], ["Kofi", "Lena"])     # no doubles
        self.assertEqual(len({s["pic"] for s in self.hub.students()}), 2)               # a picture each
        self.sync()
        page_html = self.go("/").body.decode()
        self.assertIn("<h1>Who are you?</h1>", page_html)
        self.assertIn("data-name='Lena'", page_html)
        self.assertIn("class='st look-young'", page_html)
        self.assertIn("Are you Lena?", self.go("/who?n=Lena").body.decode())
        cookie = self.go("/who", "", "POST", n="Lena").headers["Set-Cookie"].split(";")[0]
        self.assertEqual(cookie, "student=Lena")
        home = self.go("/", cookie).body.decode()
        self.assertIn("Hi, Lena", home)
        self.assertIn("class='pic face'", home)
        self.assertIn("id='idleout' data-min='10'", home)
        self.assertIn("Finished, Lena?", self.go("/me", cookie).body.decode())
        self.assertNotIn("idleout", self.go("/").body.decode())                          # nobody signed in

    def test_secret_picture(self):
        self.add("young", "Lena")
        self.hub_post("/students/ways", young="secret", signout="5")
        self.sync()
        secret = self.hub.students()[0]["secret"]
        self.assertNotIn(secret, json.dumps(self.hub.language_bundle()["students"]))      # only a hash travels
        page_html = self.go("/who?n=Lena").body.decode()
        self.assertEqual(page_html.count("name='secret'"), 6)
        self.assertIn(f"value='{secret}'", page_html)
        wrong = next(k for k in ("apple", "ball") if k != secret)
        self.assertIn("Not that one", self.go("/who", "", "POST", n="Lena", secret=wrong).body.decode())
        self.assertNotIn("Set-Cookie", self.go("/login", "", "POST", student="lena").headers)  # typing the name is no way round
        self.assertIn("Who are you?", self.go("/", "student=Lena").body.decode())              # nor typing the cookie
        self.assertIn("Who are you?", self.go("/", "student=Lena~abc").body.decode())
        cookie = self.go("/who", "", "POST", n="Lena", secret=secret).headers["Set-Cookie"].split(";")[0]
        self.assertIn("Hi, Lena", self.go("/", urllib.parse.unquote(cookie)).body.decode())
        self.assertIn("data-min='5'", self.go("/", urllib.parse.unquote(cookie)).body.decode())

    def test_pin_and_slowing_down_guesses(self):
        self.add("teen", "Zara Khan")
        self.hub_post("/students/ways", teen="pin")
        self.sync()
        pin = self.hub.students()[0]["pin"]
        self.assertIn("id='find'", self.go("/").body.decode())
        self.assertIn("4-number PIN", self.go("/who?n=Zara%20Khan").body.decode())
        bad = "0000" if pin != "0000" else "1111"
        for _ in range(5):
            self.assertIn("not right", self.go("/who", "", "POST", n="Zara Khan", secret=bad).body.decode())
        self.assertIn("Too many tries", self.go("/who", "", "POST", n="Zara Khan", secret=pin).body.decode())
        self.phone._tries["Zara Khan"] = (5, 0.0)                                         # a minute later
        self.assertIn("Set-Cookie", self.go("/who", "", "POST", n="Zara Khan", secret=pin).headers)
        # A new PIN from the teacher replaces it with the next copy.
        self.hub_post("/students/edit", old="Zara Khan", do="secret")
        self.hub.db.execute("UPDATE students SET pin = '4321'")
        self.sync()
        self.assertNotIn("Set-Cookie", self.go("/who", "", "POST", n="Zara Khan", secret="9999").headers)
        self.assertIn("Set-Cookie", self.go("/who", "", "POST", n="Zara Khan", secret="4321").headers)
        self.assertIn("4321", self.hub_page("/students/cards"))

    def test_newcomers_and_names_from_phones(self):
        self.add("mid", "Amara", "Tomás")
        self.sync()
        page_html = self.go("/new").body.decode()
        self.assertIn("Welcome! You&#x27;re new", page_html)
        for s in self.hub.students():
            self.assertNotIn(f"value='{s['pic']}'", page_html)                            # others' pictures are hidden
        cookie = self.go("/login", "", "POST", student="Ama", pic="frog").headers["Set-Cookie"]
        self.assertIn("student=Ama", cookie)
        self.assertIn("data-name='Ama'", self.go("/").body.decode())                      # next time they tap it
        for sub, name in (("s1", "Ama"), ("s2", "Daniel")):
            self.hub.db.execute("INSERT INTO submissions (sub_id, assignment_id, student, phone, answers, received_at) VALUES (?, 'a', ?, 'p1', '{}', 1)", (sub, name))
        self.assertIn("New names from phones", self.hub_page("/students"))
        self.hub_post("/students/link", name="Ama", do="same", same="Amara")
        self.assertEqual(self.hub.db.one("SELECT student FROM submissions WHERE sub_id = 's1'")["student"], "Amara")
        self.assertEqual(self.hub.student_name("Ama"), "Amara")                           # and later work too
        self.hub_post("/students/link", name="Daniel", do="ignore")
        self.assertEqual(self.hub.new_names(), [])
        self.assertEqual(self.hub.student_name("Daniel"), "Daniel")
        self.hub_post("/students/edit", old="Amara", do="save", name="Amara Osei", pic="dog", ages="teen")
        self.assertEqual(self.hub.db.one("SELECT student FROM submissions WHERE sub_id = 's1'")["student"], "Amara Osei")
        self.assertEqual(self.hub.student_name("Ama"), "Amara Osei")

    def test_hash_matches_the_android_app(self):
        from meshlearn.phonesignin import secret_hash
        self.assertEqual(secret_hash("Tomás", "4321"), "8b86cf26c69f90f40d1f77b5a6864445")   # the same value is in CoreTest.kt


class PictureTests(unittest.TestCase):
    def test_pictures_travel_by_name_and_show_on_the_phone(self):
        a = asg.normalize_assignment({"title": "Count", "questions": [
            {"type": "choice", "prompt": "How many?", "options": ["2", "3"], "answer": 1, "pic": "apple*3, <svg>, ball"},
            {"type": "choice", "prompt": "Which swims?", "options": ["Goat", "Fish"], "answer": 1, "opic": ["goat", "fish", "x"]},
            {"type": "text", "prompt": "Name it", "accept": ["x"], "pic": "nosuchpicture"}]})
        self.assertEqual(a["questions"][0]["pic"], "apple*3,ball")
        self.assertEqual(a["questions"][1]["opic"], ["goat", "fish"])
        self.assertEqual(asg.unpack(json.loads(json.dumps(asg.pack(a)))), a)
        self.assertLess(len(json.dumps(asg.pack(a)["Q"][0])), 90)                        # names only
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        phone = Phone(os.path.join(tmp, "card"), "hub", "P", lambda h: _QuietRadio())
        phone.import_assignment(asg.pack(a), open_=True)
        html = phone.app.dispatch(Request("GET", f"/a/{a['id']}", {}, {"Cookie": "student=Ana"}, b"", "192.168.43.9")).body.decode()
        self.assertIn("<div class='pics n4' role='img' aria-label='Apple, Ball'>", html)   # never says how many
        self.assertEqual(html.count("class='pics"), 1)                                   # an unknown picture shows nothing
        self.assertEqual(html.count("class='opts short'"), 2)
        self.assertIn("<span class='otx'>Fish</span>", html)

    def test_every_drawing_is_plain_paths(self):
        import re as regex

        from meshlearn.web import PICTURES, picture
        self.assertGreater(len(PICTURES), 250)
        for key, p in PICTURES.items():
            self.assertRegex(key, r"^[a-z0-9-]+$")
            for part in (p.get("d", ""), p.get("x", "")):
                self.assertTrue(regex.match(r"^[MmLlHhVvCcSsQqTtAaZz0-9 .,\-]*$", part), key)
            self.assertRegex(p["f"], r"^(#[0-9a-fA-F]{3,8}|none)$")
        self.assertEqual(picture("nosuch"), "")

    def test_hub_offers_the_library(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        hub = Hub(os.path.join(tmp, "hub.db"), lambda h: _QuietRadio())
        html = hub.app.dispatch(Request("GET", "/assignments/new", {}, {}, b"", "127.0.0.1")).body.decode()
        self.assertIn("data-pic='elephant'", html)
        self.assertIn("Choose pictures", html)


class ZipAppTests(unittest.TestCase):
    """Activities and tools on the card run walled off; ready-made libraries are served by name only."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.phone = Phone(os.path.join(self.tmp, "card"), "hub", "P", lambda h: _QuietRadio())
        lib = self.phone.library_dir
        os.makedirs(os.path.join(lib, "kolibri"))
        os.makedirs(os.path.join(lib, "tools"))
        with zipfile.ZipFile(os.path.join(lib, "kolibri", "sim.zip"), "w") as z:
            z.writestr("index.html", "<!doctype html><html><head><title>é</title></head><body>hi</body></html>")
            z.writestr("a/pic.png", b"\x89PNG")
            z.writestr("a/", "")
        with zipfile.ZipFile(os.path.join(lib, "tools", "geogebra.zip"), "w") as z:
            z.writestr("GeoGebra/deployggb.js", "x")
            z.writestr("GeoGebra/x.html", "<head></head>")
        with open(os.path.join(lib, "index.json"), "w", encoding="utf-8") as fh:
            json.dump([{"title": "Sim", "file": "kolibri/sim.zip", "kind": "zip", "collection": "C", "shelf": "PhET", "credit": "By PhET"},
                       {"title": "GeoGebra", "file": "tools/geogebra.zip", "kind": "ggb", "collection": "Tools", "credit": "GG"}], fh)

    def get(self, path, method="GET", **headers):
        path, _, query = path.partition("?")
        return self.phone.app.dispatch(Request(method, path, dict(urllib.parse.parse_qsl(query)),
                                               dict({"Cookie": "student=Ana", "Host": "192.168.43.1:8080"}, **headers), b"", "192.168.43.9"))

    def test_activities_are_walled_off(self):
        self.assertIn("href='/library/open?f=tools%2Fgeogebra.zip'", self.get("/library").body.decode())
        page_html = self.get("/library/open?f=kolibri/sim.zip").body.decode()
        self.assertIn("sandbox='allow-scripts'", page_html)
        self.assertIn("By PhET", page_html)
        resp = self.get("/library/app/kolibri/sim.zip/index.html")
        csp = resp.headers["Content-Security-Policy"]
        self.assertIn("sandbox allow-scripts; default-src http://192.168.43.1:8080/library/app/kolibri/sim.zip/ ", csp)
        self.assertNotIn("allow-same-origin", csp)
        self.assertIn("form-action 'none'", csp)
        html = resp.body.decode("utf-8")
        self.assertTrue(html.startswith("<!doctype html><html><head><script>(function(){function store()"))
        self.assertIn("<title>é</title>", html)
        self.assertEqual(self.get("/library/app/kolibri/sim.zip/a/pic.png").headers["Content-Type"], "image/png")
        for bad in ("kolibri/sim.zip/none.js", "kolibri/sim.zip/a/", "kolibri/other.zip/index.html", "../phone.db.zip/x", "kolibri/sim.zip/../../x"):
            self.assertEqual(self.get("/library/app/" + bad).status, 404, bad)
        evil = self.get("/library/app/kolibri/sim.zip/index.html", Host="evil'; script-src *")
        self.assertIn("default-src http://localhost/", evil.headers["Content-Security-Policy"])
        self.assertEqual(self.get("/library/open?f=../phone.db").status, 404)
        self.assertIn("class='keepread' href='/library/open?f=kolibri%2Fsim.zip'", self.get("/library").body.decode())

    def test_an_activity_cannot_press_our_buttons(self):
        self.assertEqual(self.get("/status/reload", "POST", Origin="null").status, 403)
        self.assertNotEqual(self.get("/logout", "POST", Origin="http://192.168.43.1:8080").status, 403)
        self.assertIn("HttpOnly", self.phone.app.dispatch(Request("POST", "/login", {}, {}, b"student=Ana", "1.1.1.1")).headers["Set-Cookie"])

    def test_geogebra_is_the_only_one_with_its_own_origin(self):
        page_html = self.get("/library/open?f=tools/geogebra.zip").body.decode()
        self.assertNotIn("<iframe", page_html)
        self.assertIn("_start.html?app=scientific", page_html)
        resp = self.get("/library/app/tools/geogebra.zip/_start.html?app=<x>&lang='x")
        self.assertIn("sandbox allow-scripts allow-same-origin; default-src http://192.168.43.1:8080/library/app/tools/geogebra.zip/ ",
                      resp.headers["Content-Security-Policy"])
        self.assertIn('"appName": "graphing"', resp.body.decode())
        self.assertIn('"language": "en"', resp.body.decode())
        self.assertIn("GeoGebra GmbH", resp.body.decode())

    def test_libraries_by_name_only(self):
        for path, kind in (("katex/katex.min.js", "text/javascript"), ("katex/fonts/KaTeX_Main-Regular.woff2", "font/woff2"),
                           ("signature_pad/signature_pad.umd.min.js", "text/javascript"), ("opendyslexic/regular.woff2", "font/woff2"),
                           ("qrcode_generator/qrcode.js", "text/javascript"), ("papaparse/papaparse.min.js", "text/javascript")):
            resp = self.get("/site/vendor/" + path)
            self.assertEqual(resp.status, 200, path)
            self.assertTrue(resp.headers["Content-Type"].startswith(kind), path)
        for bad in ("looks.css", "vendor/../looks.css", "vendor/katex/../../looks.css", "vendor/NOTICES.md", "vendor/nope/x.js"):
            self.assertEqual(self.get("/site/" + bad).status, 404, bad)
        css = self.get("/site/vendor/katex/katex.min.css").body.decode()
        import re as regex
        for name in set(regex.findall(r"url\((fonts/[^)]+)\)", css)):    # every font the stylesheet asks for is there
            self.assertEqual(self.get("/site/vendor/katex/" + name).status, 200, name)

    def test_tools_on_the_pages(self):
        a = asg.normalize_assignment({"title": "E", "questions": [{"type": "text", "prompt": r"What is \(x\)?", "accept": ["1"]}]})
        self.phone.import_assignment(asg.pack(a), open_=True)
        html = self.get(f"/a/{a['id']}").body.decode()
        for part in ("id='scratchopen'", "id='readpanel'", "data-reading", r"What is \(x\)?", "/site/vendor/katex/katex.min.js"):
            self.assertIn(part, html)
        self.assertIn("data-qr='http://192.168.43.1:8080'", self.get("/join?u=http://192.168.43.1:8080").body.decode())
        self.assertIn("data-qr='http://192.168.43.1:8080'", self.get("/join?u=javascript:alert(1)").body.decode())
        self.assertIn("PhET Interactive Simulations, University of Colorado Boulder", self.get("/about").body.decode())

    def test_hub_geogebra_qr_and_import(self):
        hub = Hub(os.path.join(self.tmp, "hub.db"), lambda h: _QuietRadio())
        hub.catalog.entries = catalog.parse_catalog(fake_catalog_xml())
        def page_of(path, method="GET", body=b"", host="192.168.1.5:8000"):
            path, _, query = path.partition("?")
            return hub.app.dispatch(Request(method, path, dict(urllib.parse.parse_qsl(query)), {"Host": host}, body, "127.0.0.1"))
        self.assertIn("non-commercial", page_of("/library?q=geo").body.decode())
        page_of("/library/add", "POST", b"key=geogebra%7Capps")
        (item,) = hub.selection()
        self.assertEqual((item["kind"], item["files"][0][1]), ("ggb", "geogebra.zip"))
        with mock.patch.object(library, "download", side_effect=lambda url, path, size, **k: open(path, "wb").write(b"z" * 100) and self.assertEqual(size, 0)):
            job = catalog.DownloadJob([item], os.path.join(self.tmp, "c2"))
            job.run()
        self.assertEqual(job.state, "done", job.error)                    # an updated bundle of another size is fine
        with open(os.path.join(self.tmp, "c2", "library", "index.json"), encoding="utf-8") as fh:
            (entry,) = json.load(fh)
        self.assertEqual((entry["file"], entry["kind"]), ("tools/geogebra.zip", "ggb"))
        self.assertIn("GeoGebra GmbH", entry["credit"])
        setup = page_of("/phone-setup").body.decode()
        self.assertIn("meshlearn://setup?", setup)
        self.assertIn("address=192.168.1.5%3A8000", setup)
        self.assertNotIn("<b>", page_of("/phone-setup", host="<b>").body.decode())
        builder = page_of("/assignments/new").body.decode()
        self.assertIn("Import questions from a spreadsheet", builder)
        self.assertEqual(page_of("/site/vendor/papaparse/papaparse.min.js").status, 200)

    def test_kolibri_activities_carry_phets_credit(self):
        case = KolibriTests("test_channels_become_catalog_entries")
        case.setUp()
        self.addCleanup(case.doCleanups)
        path = os.path.join(case.tmp, "mirror", "content", "databases", case.CHANNEL + ".sqlite3")
        db = sqlite3.connect(path)
        db.execute("INSERT INTO content_contentnode VALUES ('sim','Sim','html5',3,3,3,'add','CC BY','PhET Interactive Simulations, University of Colorado Boulder','',0)")
        db.execute("INSERT INTO content_file VALUES ('sim', ?, 'html5_zip', 0, 0)", ("9" * 32,))
        db.execute("INSERT INTO content_localfile VALUES (?, 'zip', 500)", ("9" * 32,))
        db.commit()
        db.close()
        channel = kolibri.Channel(case.hub.kolibri_dir, case.CHANNEL)
        channel.fetch()
        item = channel.selection_item(channel.node("add"), case.hub.catalog.entries[0])
        sim = next(d for d in item["details"] if d["title"] == "Sim")
        self.assertEqual((sim["kind"], sim["credit"]), ("zip", kolibri.PHET_CREDIT))


class WrongAnswerTests(unittest.TestCase):
    """Notes on wrong answers, not-sure ticks and changed answers, from the builder to the class view and back."""

    LESSON = {"id": "a-why", "title": "Adding", "course": "math", "unit": "fractions", "due": "2020-01-01", "questions": [
        {"type": "choice", "prompt": "Bigger?", "options": ["1/2", "1/8"], "answer": 0, "why": ["ignored", "Bigger bottom means bigger"]},
        {"type": "number", "prompt": "1/4 + 1/2?", "answer": 0.75, "why": [{"x": "2/6", "w": "Added tops and bottoms"}, {"x": "3/4", "w": "right"}, {"x": "abc", "w": "no"}]},
        {"type": "text", "prompt": "Gas out?", "accept": ["oxygen"], "why": [{"x": "Carbon dioxide", "w": "Mixed up in and out"}]},
        {"type": "long", "prompt": "Why?", "points": 2}]}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hub = Hub(os.path.join(self.tmp, "hub.db"), lambda h: _QuietRadio())
        self.hub.add_phone("p1", "North village")

    def page(self, path, method="GET", body=b""):
        path, _, query = path.partition("?")
        return self.hub.app.dispatch(Request(method, path, dict(urllib.parse.parse_qsl(query)), {}, body, "127.0.0.1"))

    def hand_in(self, name, answers, **signals):
        sid = "s" + name
        self.hub.on_message(protocol.Message(1, protocol.SUBMIT, {"s": [dict(
            {"id": sid, "a": "a-why", "v": self.hub.current_version("a-why"), "u": name,
             "x": dict(zip(("q1", "q2", "q3", "q4"), answers)), "t": time.time()}, **signals)]}, "p1"))
        return sid

    def test_notes_are_cleaned_and_never_leave_the_hub(self):
        a = asg.normalize_assignment(self.LESSON)
        self.assertEqual(a["questions"][0]["why"], ["", "Bigger bottom means bigger"])
        self.assertEqual(a["questions"][1]["why"], [{"x": "2/6", "w": "Added tops and bottoms"}])   # not the right answer, not a word
        self.assertEqual(asg.wrong_note(a["questions"][1], "0,4"), "")
        self.assertEqual(asg.wrong_note(a["questions"][1], "0,33"), "Added tops and bottoms")
        self.assertEqual(asg.wrong_note(a["questions"][1], "4/12"), "Added tops and bottoms")
        self.assertEqual(asg.wrong_note(a["questions"][2], " carbon DIOXIDE. "), "Mixed up in and out")
        for mode in ("graded", "practice"):
            b = asg.normalize_assignment(dict(self.LESSON, mode=mode))
            self.assertNotIn("tops", json.dumps(asg.pack(asg.public_view(b))))
            plain = asg.normalize_assignment(dict(self.LESSON, mode=mode, questions=[{k: v for k, v in q.items() if k != "why"} for q in self.LESSON["questions"]]))
            self.assertEqual(asg.version_of(b), asg.version_of(plain))

    def test_signals_are_small_and_checked(self):
        a = asg.normalize_assignment(self.LESSON)
        answers = {"q1": "1", "q2": "0.75", "q3": "oxygen", "q4": "x"}
        firsts = {"q1": "0", "q2": "3/4", "q3": "Oxygen ", "q4": "y", "q9": "1"}   # only q1 really changed
        s = asg.signals_of(a, {"q1", "q3"}, json.dumps(firsts), answers)
        self.assertEqual(s, {"n": 5, "f": {"q1": "0"}})
        self.assertEqual(asg.signals_of(a, set(), json.dumps({"q1": "7", "q2": "abc"}), answers), {})
        self.assertEqual(asg.signals_of(a, set(), "[not json", answers), {})
        self.assertEqual(asg.read_signals(a, s), ({"q1", "q3"}, {"q1": "0"}))
        self.assertEqual(asg.read_signals(a, {"n": "x", "f": 3}), (set(), {}))

    def test_phone_sends_them_and_the_class_view_shows_them(self):
        self.hub.create_assignment(self.LESSON)
        radio = _QuietRadio()
        sent = []
        radio.send = lambda dest, kind, payload: sent.append((kind, payload)) or 1
        phone = Phone(os.path.join(self.tmp, "phone"), "hub", "P", lambda h: radio)
        phone.import_assignment(self.hub.packed_for_phone("a-why"), open_=True)
        def ask(method, path, form=""):
            return phone.app.dispatch(Request(method, path, {}, {"Cookie": "student=Ana", "Content-Type": "application/x-www-form-urlencoded"}, form.encode(), "1.1.1.1"))
        page = ask("GET", "/a/a-why").body.decode()
        self.assertIn("name='unsure-q1'", page)
        self.assertNotIn("tops", page)
        again = ask("POST", "/a/a-why", "q1=1&unsure-q1=1")
        self.assertIn("name='unsure-q1' value='1' checked", again.body.decode())
        ask("POST", "/a/a-why", urllib.parse.urlencode({"q1": "1", "q2": "2/6", "q3": "oxygen", "q4": "x", "unsure-q3": "1", "first": '{"q1":"0"}'}))
        phone.flush_submissions(force=True)
        item = next(p for k, p in sent if k == protocol.SUBMIT)["s"][0]
        self.assertEqual((item["n"], item["f"]), (4, {"q1": "0"}))
        self.hub.on_message(protocol.Message(1, protocol.SUBMIT, {"s": [item]}, "p1"))
        self.hand_in("Kofi", ("1", "2/6", "carbon dioxide", "y"), n=2)
        self.hand_in("Zara", ("0", "7.5", "oxygen", "z"), n="junk", f=["junk"])
        report = comprehension.question_report(self.hub.get_assignment("a-why"), self.hub.comprehension_records())
        self.assertEqual([(g["note"], g["names"]) for g in report[1]["groups"]], [("Added tops and bottoms", ["Ana", "Kofi"]), ("", ["Zara"])])
        self.assertEqual((report[0]["changed_away"], report[2]["unsure_right"], report[1]["unsure_wrong"]), (["Ana"], ["Ana"], ["Kofi"]))
        self.assertEqual(report[3]["waiting"], ["Ana", "Kofi", "Zara"])
        html = self.page("/assignments/a-why").body.decode()
        for part in ("<strong>2 students</strong> on question 1: Bigger bottom means bigger", "Added tops and bottoms", "Right, but not sure:</strong> Ana",
                     "Had it right, then changed it:</strong> Ana", "Place value", "3 answers to mark"):
            self.assertIn(part, html)
        self.assertIn("Not sure", self.page("/submissions/" + item["id"]).body.decode())
        csv_text = self.page("/assignments/a-why/grades.csv").body.decode()
        self.assertIn("not_sure,changed", csv_text)

    def test_a_note_can_be_added_from_the_class_view(self):
        self.hub.create_assignment(self.LESSON)
        version = self.hub.current_version("a-why")
        self.hand_in("Zara", ("0", "7.5", "oxygen", "z"))
        self.page("/assignments/a-why/why", "POST", urllib.parse.urlencode({"q": "q2", "answer": "7.5", "note": "<b>Point</b>"}).encode())
        self.assertIn("&lt;b&gt;Point&lt;/b&gt;", self.page("/assignments/a-why").body.decode())
        self.assertEqual(self.hub.current_version("a-why"), version)   # nothing for phones to fetch
        self.page("/assignments/a-why/why", "POST", b"q=q2&answer=7%2C50&note=")
        self.assertNotIn("Point", self.page("/assignments/a-why").body.decode())
        self.page("/assignments/a-why/why", "POST", b"q=q1&answer=9&note=x")   # no such option
        self.assertEqual(self.page("/assignments/nope/why", "POST", b"q=q1").status, 303)

    def test_class_progress_and_practice_on_a_mistake(self):
        self.hub.create_assignment(self.LESSON)
        for name in ("Ana", "Kofi"):
            self.hand_in(name, ("1", "2/6", "oxygen", "y"))
        self.assertEqual(comprehension.common_mistakes(self.hub.comprehension_records())[0], ("note", "Added tops and bottoms", 2))
        html = self.page("/comprehension/math?unit=fractions").body.decode()
        self.assertIn("Most common mistakes", html)
        self.assertIn("practice=note%3AAdded+tops+and+bottoms", html)
        draft, _ = self.hub.practice_draft("math", "fractions", "note:Added tops and bottoms")
        self.assertEqual([q["prompt"] for q in draft["questions"]], ["1/4 + 1/2?"])
        self.assertEqual(draft["mode"], "practice")
        late = asg.normalize_assignment(dict(self.LESSON, due="2099-01-01"))   # still open: its answers must not go out in a practice
        self.hub.create_assignment(late)
        draft, note = self.hub.practice_draft("math", "fractions", "note:Added tops and bottoms")
        self.assertEqual(draft["questions"], [])
        self.assertIn("left out", note)
        self.assertIn("left out", self.page("/assignments/new?course=math&unit=fractions&practice=note:Added+tops+and+bottoms").body.decode())

    def test_a_note_goes_back_to_the_student(self):
        self.hub.create_assignment(self.LESSON)
        sid = self.hand_in("Ana", ("0", "0.75", "oxygen", "because"), n=8)
        self.assertIn("not sure", self.page("/marking").body.decode())
        sent = []
        self.hub.messenger.send = lambda dest, kind, payload: sent.append(payload) or 1
        self.page("/marking", "POST", urllib.parse.urlencode({"sid": sid, "qid": "q4", "mark": "1", "note": "Say  why, " + "x" * 90}).encode())
        (result,) = sent[-1]["r"]
        self.assertEqual(len(result["nt"]["q4"]), 60)
        self.assertTrue(result["nt"]["q4"].startswith("Say why, "))
        phone = Phone(os.path.join(self.tmp, "phone"), "hub", "P", lambda h: _QuietRadio())
        phone.import_assignment(self.hub.packed_for_phone("a-why"), open_=True)
        a = phone.get_assignment("a-why")
        own = phone.submit(a, "Ana", {"q1": "0", "q2": "0.75", "q3": "oxygen", "q4": "because"})
        phone.on_message(protocol.Message(1, protocol.RESULT, {"r": [dict(result, id=own, nt={"q4": "<i>Say why</i>"})]}, "hub"))
        self.assertIn("From your teacher: &lt;i&gt;Say why&lt;/i&gt;", phone.app.dispatch(
            Request("GET", "/results", {}, {"Cookie": "student=Ana"}, b"", "1.1.1.1")).body.decode())

    def test_builder_sizes_saves_and_sends(self):
        html = self.page("/assignments/new").body.decode()
        for part in ("What students see", "Radio size", "What picking this shows", "Wrong answers to watch for"):
            self.assertIn(part, html)
        def post(path, data):
            resp = self.hub.app.dispatch(Request("POST", path, {}, {"Content-Type": "application/json"}, json.dumps(data).encode(), "127.0.0.1"))
            return resp.status, json.loads(resp.body)
        self.assertGreaterEqual(post("/api/assignments/size", self.LESSON)[1]["frames"], 1)
        self.assertIsNone(post("/api/assignments/size", {"title": ""})[1]["frames"])
        self.assertIsNone(post("/api/assignments/size", [1])[1]["frames"])
        self.assertEqual(self.hub.db.one("SELECT COUNT(*) AS n FROM assignments")["n"], 0)   # sizing saves nothing
        status, data = post("/api/assignments", dict(self.LESSON, send=True))
        self.assertEqual((status, data["sent"]), (200, 1))
        self.assertEqual(self.hub.db.one("SELECT open FROM holdings")["open"], 1)
        self.assertNotIn("send", self.hub.get_assignment("a-why"))
        evil = dict(self.LESSON, title="</script><script>alert(1)</script>")
        post("/api/assignments", evil)
        self.assertNotIn("</script><script>alert", self.page("/assignments/a-why/edit").body.decode().split("const S =")[1])

    def test_data_export_round_trip(self):
        self.hub.create_assignment(self.LESSON)
        self.hand_in("Ana", ("1", "2/6", "oxygen", "y"), n=1, f={"q1": "0"})
        record = self.hub.learning_record()
        self.assertEqual(record["submissions"][0]["not_sure"], ["q1"])
        self.assertEqual(record["submissions"][0]["wrong_notes"]["q2"], "Added tops and bottoms")
        other = Hub(os.path.join(self.tmp, "other.db"), lambda h: _QuietRadio())
        other.import_learning_record(json.loads(json.dumps(record)))
        (r,) = other.comprehension_records()
        self.assertEqual((r["unsure"], r["first"]), ({"q1"}, {"q1": "0"}))


class KeepReadingTests(unittest.TestCase):
    def test_the_last_thing_opened_is_offered_again(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        phone = Phone(os.path.join(tmp, "card"), "hub", "P", lambda h: _QuietRadio())
        def get(path, cookie="student=Ana", headers=None):
            req = Request("GET", path, {}, dict({"Cookie": cookie}, **(headers or {})), b"", "192.168.43.9")
            return phone.app.dispatch(req)
        self.assertNotIn("Keep reading", get("/library").body.decode())
        get("/library/files/starter/fractions.html")
        get("/library/files/starter/photosynthesis.html", headers={"Range": "bytes=100-"})   # a player fetching a later piece
        get("/library/files/starter/photosynthesis.html", cookie="student=Kofi")
        page_html = get("/library").body.decode()
        self.assertIn("Keep reading", page_html)
        self.assertIn("class='keepread' href='/library/files/starter/fractions.html'", page_html)
        os.remove(os.path.join(phone.library_dir, "starter", "fractions.html"))
        self.assertNotIn("Keep reading", get("/library").body.decode())


class SendCountdownTests(unittest.TestCase):
    def test_next_send_in_follows_the_batch_wait(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        t = UdpSimTransport("p", free_port(socket.SOCK_DGRAM), {"hub": ("127.0.0.1", free_port(socket.SOCK_DGRAM))})
        phone = Phone(os.path.join(tmp, "card"), "hub", "P", lambda h: Messenger(t, os.path.join(tmp, "o.db"), h), batch_s=120)
        self.addCleanup(t.close)
        self.assertIsNone(phone.next_send_in())
        a = asg.normalize_assignment(load_sample())
        phone.submit(a, "Ana", {q["id"]: "1" for q in a["questions"]})
        self.assertTrue(110 < phone.next_send_in() <= 120)
        phone.flush_submissions(force=True)
        self.assertIsNone(phone.next_send_in())   # batched: now it is the radio's turn


class RadioPortTests(unittest.TestCase):
    """Starting the hub without knowing the radio's port or its MeshLearn channel slot."""

    @staticmethod
    def port(device, vid):
        return type("Port", (), {"device": device, "vid": vid})()

    def test_finds_the_radio_among_other_serial_ports(self):
        ports = [self.port("/dev/cu.Bluetooth-Incoming-Port", None), self.port("/dev/cu.debug-console", None),
                 self.port("/dev/cu.usbmodem1101", 0x303A), self.port("/dev/tty.usbmodem1101", 0x303A),
                 self.port("/dev/cu.usbserial-0001", 0x9999)]
        self.assertEqual(radioports.find_radio_ports(ports), ["/dev/cu.usbmodem1101"])
        # A board with a chip not on the list is still offered when nothing better is plugged in.
        self.assertEqual(radioports.find_radio_ports([self.port("COM7", 0x9999)]), ["COM7"])
        self.assertEqual(radioports.find_radio_ports([self.port("COM1", None)]), [])

    def test_choose_port(self):
        self.assertEqual(radioports.choose_radio_port(["COM5"]), "COM5")
        with self.assertRaisesRegex(radioports.NoRadioFound, "No radio is plugged in"):
            radioports.choose_radio_port([])
        answers = iter(["9", "2"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(radioports.choose_radio_port(["COM3", "COM5"], ask=lambda _: next(answers)), "COM5")

    def test_meshlearn_channel_by_name(self):
        def ch(index, name, role=2):
            return type("Ch", (), {"index": index, "role": role, "settings": type("S", (), {"name": name})()})()
        self.assertEqual(radioports.meshlearn_channel([ch(0, "", 1), ch(1, "Family"), ch(3, "MeshLearn")]), 3)
        self.assertIsNone(radioports.meshlearn_channel([ch(0, "", 1), ch(3, "meshlearn", role=0)]))
        self.assertIsNone(radioports.meshlearn_channel(None))

    def test_hub_explains_a_missing_radio(self):
        from meshlearn import __main__ as cli
        err = io.StringIO()
        with mock.patch.object(cli, "choose_radio_port", side_effect=radioports.NoRadioFound("No radio is plugged in.")), \
                contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["hub", "--radio-serial", "--data", tempfile.mkdtemp()]), 1)
        self.assertIn("No radio is plugged in", err.getvalue())


class MessengerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pair(self, loss):
        pa, pb = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
        self.got = []
        ta = UdpSimTransport("a", pa, {"b": ("127.0.0.1", pb)}, drop_rate=loss, seed=1)
        tb = UdpSimTransport("b", pb, {"a": ("127.0.0.1", pa)}, drop_rate=loss, seed=2)
        ma = Messenger(ta, os.path.join(self.tmp, "a.db"), lambda m: None, retry_s=0.3, max_retry_s=0.6)
        mb = Messenger(tb, os.path.join(self.tmp, "b.db"), self.got.append, retry_s=0.3, max_retry_s=0.6)
        ma.start(), mb.start()
        self.addCleanup(ma.stop)
        self.addCleanup(mb.stop)
        return ma, mb

    def test_a_new_hello_replaces_one_still_waiting(self):
        # Nothing listens on this port, so messages stay queued.
        t = UdpSimTransport("a", free_port(socket.SOCK_DGRAM), {"b": ("127.0.0.1", free_port(socket.SOCK_DGRAM))})
        m = Messenger(t, os.path.join(self.tmp, "a.db"), lambda msg: None, retry_s=60, max_retry_s=60)
        self.addCleanup(t.close)
        for i in range(3):
            m.drop_pending("b", protocol.HELLO)
            m.send("b", protocol.HELLO, {"n": "phone %d" % i})
        m.send("b", protocol.SUBMIT, {"a": 1, "s": "Ana", "r": []})
        self.assertEqual(m.status()["pending"], 2)   # the last hello and the work

    def test_delivers_through_heavy_loss_exactly_once(self):
        ma, _ = self.pair(0.4)
        ids = [ma.send("b", protocol.SUBMIT, {"i": i, "pad": os.urandom(150).hex()}) for i in range(5)]
        self.assertTrue(wait_for(lambda: all(ma.message_status(i) == "acked" for i in ids), 60))
        self.assertEqual(sorted(m.payload["i"] for m in self.got), list(range(5)))

    def test_only_missing_pieces_are_resent(self):
        ma, _ = self.pair(0.3)
        payload = {"blob": os.urandom(1000).hex()}  # incompressible: about 10 frames
        n = protocol.frame_count(protocol.SUBMIT, payload)
        msg = ma.send("b", protocol.SUBMIT, payload)
        self.assertTrue(wait_for(lambda: ma.message_status(msg) == "acked", 60))
        # Resending the whole message each time would need about n / 0.7**n ≈ 35x n frames on average.
        self.assertLess(ma.frames_sent, 4 * n, f"{ma.frames_sent} frames for a {n}-frame message")


class _InstantLink:
    """A radio where frames arrive instantly, inside the sender's own send() call, so
    replies come back while a burst is still going out. Drops the first copy of chosen frames."""

    def __init__(self, node_id, drop_first=()):
        self.node_id = node_id
        self.peer = None
        self.handler = None
        self.drop_first = set(drop_first)

    def start(self, handler):
        self.handler = handler

    def send(self, frame, dest):
        seq = protocol.parse_header(frame).seq
        if seq in self.drop_first:
            self.drop_first.discard(seq)
            return
        self.peer.handler(frame, self.node_id)

    def close(self):
        pass


class SelectiveResendRaceTests(unittest.TestCase):
    def test_nack_arriving_mid_burst_is_not_lost(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        a, b = _InstantLink("a", drop_first={1, 3}), _InstantLink("b")
        a.peer, b.peer = b, a
        got = []
        # A long retry time: if the NACK were lost, delivery would take far longer than this test waits.
        ma = Messenger(a, os.path.join(tmp, "a.db"), lambda m: None, retry_s=60, max_retry_s=60)
        mb = Messenger(b, os.path.join(tmp, "b.db"), got.append, retry_s=60, max_retry_s=60)
        ma.start(), mb.start()
        self.addCleanup(ma.stop)
        self.addCleanup(mb.stop)
        msg = ma.send("b", protocol.SUBMIT, {"blob": os.urandom(900).hex()})
        self.assertTrue(wait_for(lambda: ma.message_status(msg) == "acked", 5))
        self.assertEqual(len(got), 1)


class LibraryPlanTests(unittest.TestCase):
    def pkg(self, key, *sizes):
        p = library.Package(key, key, "x")
        p.options = [library.Option(f"f{i}", f"{key}{i}", s, [("u", f"{key}{i}")], "zim") for i, s in enumerate(sizes)]
        return p

    def test_small_card_gets_smallest_of_each_in_priority_order(self):
        pkgs = [self.pkg("phet", 100), self.pkg("wp100", 300), self.pkg("wptop", 300, 2700, 7000), self.pkg("wpall", 14000)]
        chosen = {c.package.key: c.option.size for c in library.plan(pkgs, 1000)}
        self.assertEqual(chosen, {"phet": 100, "wp100": 300, "wptop": 300})

    def test_big_card_upgrades_and_replaces_superseded(self):
        pkgs = [self.pkg("phet", 100), self.pkg("wp100", 300), self.pkg("wptop", 300, 2700, 7000), self.pkg("wpall", 14000, 50000)]
        chosen = {c.package.key: c.option.size for c in library.plan(pkgs, 60000)}
        self.assertEqual(chosen, {"phet": 100, "wpall": 50000})

    def test_usable_bytes(self):
        self.assertEqual(library.usable_bytes(32, 2), int(32e9 * 0.93 - 2e9))
        self.assertEqual(library.usable_bytes(1, 2), 0)


class EndToEndBase(unittest.TestCase):
    LOSS = 0.2

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        hp, pp = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
        ht = UdpSimTransport("hub", hp, {"phone-1": ("127.0.0.1", pp)}, drop_rate=self.LOSS, seed=3)
        pt = UdpSimTransport("phone-1", pp, {"hub": ("127.0.0.1", hp)}, drop_rate=self.LOSS, seed=4)
        self.hub = Hub(os.path.join(self.tmp, "hub.db"),
                       lambda h: Messenger(ht, os.path.join(self.tmp, "hub-out.db"), h, retry_s=0.3, max_retry_s=0.6))
        self.phone = Phone(os.path.join(self.tmp, "card"), "hub", "Test phone",
                           lambda h: Messenger(pt, os.path.join(self.tmp, "phone-out.db"), h, retry_s=0.3, max_retry_s=0.6),
                           batch_s=0.3)
        self.hub.messenger.start()
        self.phone.start()
        self.hub.add_phone("phone-1", "Test phone")
        self.hport, self.pport = free_port(), free_port()
        self.servers = [self.hub.app.serve_in_thread("127.0.0.1", self.hport),
                        self.phone.app.serve_in_thread("127.0.0.1", self.pport)]
        self.browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
        self.hub.messenger.stop()
        self.phone.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, port, path):
        return self.browser.open(f"http://127.0.0.1:{port}{path}").read().decode()

    def post(self, port, path, data):
        body = urllib.parse.urlencode(data).encode()
        try:
            return self.browser.open(f"http://127.0.0.1:{port}{path}", body).read().decode()
        except urllib.error.HTTPError as e:
            return e.read().decode()

    def post_json(self, port, path, data):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(data).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.loads(self.browser.open(req).read())

    def phone_open(self, aid):
        return lambda: (self.phone.get_assignment(aid) or {}).get("open")


class EndToEndTests(EndToEndBase):
    def test_full_loop_over_radio_only(self):
        """No pre-loaded copy: the hub sends the whole assignment by radio, then grades come back."""
        aid = self.post_json(self.hport, "/api/assignments", load_sample())["id"]
        self.post(self.hport, f"/assignments/{aid}/open", {"node_id": "phone-1"})
        self.assertTrue(wait_for(self.phone_open(aid)))
        self.assertNotIn("answer", self.phone.get_assignment(aid)["questions"][0])  # key stayed on the hub

        self.post(self.pport, "/login", {"student": "Ana"})
        self.assertIn("Fractions and plants", self.get(self.pport, "/"))
        form = self.get(self.pport, f"/a/{aid}")
        self.assertIn("Question 1 of 5", form)
        self.assertIn("meshlearn-draft:", form)  # answers are kept in the browser while the student works
        self.assertIn("Please fix", self.post(self.pport, f"/a/{aid}", {"q1": "1"}))
        self.assertIn("Done!", self.post(self.pport, f"/a/{aid}", GOOD_ANSWERS))

        self.assertTrue(wait_for(lambda: self.hub.db.one("SELECT 1 FROM submissions WHERE student = 'Ana'")))
        sub = self.hub.db.one("SELECT sub_id FROM submissions WHERE student = 'Ana'")
        self.assertIn("to mark", self.get(self.hport, f"/assignments/{aid}"))
        self.post(self.hport, f"/submissions/{sub['sub_id']}/grade", {"m_q5": "3"})
        self.assertTrue(wait_for(lambda: self.phone.db.one("SELECT complete FROM submissions WHERE student = 'Ana'")["complete"] == 1))
        self.assertIn("7 / 7", self.get(self.pport, "/results"))
        self.assertIn("Ana", self.get(self.hport, f"/assignments/{aid}/grades.csv"))

    def test_teacher_pages_examples_and_marking(self):
        """A new hub starts from the example lessons; work comes in, is marked on the Marking page, and goes back."""
        self.phone.looks = ["mid"]
        self.hub.set_setting("ages", "primary")
        self.assertIn("Add example lessons", self.get(self.hport, "/"))
        today = self.post(self.hport, "/examples", {})
        self.assertIn("Added 9 example lessons", today)
        self.assertIn("Needs you", today)
        self.assertEqual(self.hub.add_examples(), 0)   # a second press adds nothing
        self.assertTrue(all(not a.get("due") or a["due"] >= time.strftime("%Y-%m-%d")
                            for a in (self.hub.get_assignment(x["id"]) for x in self.hub.example_lessons())))
        garden = self.hub.get_assignment("a-garden")
        self.assertTrue(any(q.get("model") for q in garden["questions"]))
        self.assertNotIn("model", json.dumps(self.hub.packed_for_phone("a-garden")))   # marking notes stay on the hub
        # Copied over Wi-Fi, then opened by radio for students: one packet each.
        self.phone.sync_from_hub(f"http://127.0.0.1:{self.hport}")
        self.assertIn("copied to 1 of 1, not open", self.get(self.hport, "/assignments"))
        self.post(self.hport, "/assignments/open-all", {})
        self.assertTrue(wait_for(self.phone_open("a-garden"), timeout=30))
        self.post(self.pport, "/login", {"student": "Ana"})
        home = self.get(self.pport, "/")
        self.assertIn("Do this next", home)
        self.assertIn("Coming up", home)
        done = self.post(self.pport, "/a/a-garden", {
            "q1": "24", "q2": "20", "q3": "3000", "q4": "Count my steps along each side."})
        self.assertIn("Done!", done)
        self.assertIn("class='sendin'", done)   # when it goes to the teacher
        self.assertTrue(wait_for(lambda: self.hub.db.one("SELECT 1 FROM submissions WHERE student = 'Ana'")))
        marking = self.get(self.hport, "/marking")
        self.assertIn("Count my steps", marking)
        self.assertIn("Your model answer", marking)
        sub = self.hub.db.one("SELECT sub_id FROM submissions WHERE student = 'Ana'")["sub_id"]
        self.assertIn("Nothing to mark", self.post(self.hport, "/marking", {"sid": sub, "qid": "q4", "mark": "3"}))
        self.assertTrue(wait_for(lambda: self.phone.db.one("SELECT complete FROM submissions WHERE student = 'Ana'")["complete"] == 1))
        results = self.get(self.pport, "/results")
        self.assertIn("4 out of 4 right", results)
        self.assertIn("class='squares'", results)
        # The teacher can change an auto-marked question too; untouched ones stay MeshLearn's.
        page = self.post(self.hport, f"/submissions/{sub}/grade",
                         {"m_q1": "0", "m_q2": "1", "m_q3": "1", "m_q4": "3"})
        self.assertIn("Changed by you", page)
        self.assertIn("Right answer: 24", page)
        manual = json.loads(self.hub.db.one("SELECT manual FROM submissions WHERE sub_id = ?", (sub,))["manual"])
        self.assertEqual(set(manual), {"q1", "q4"})
        self.assertEqual(json.loads(self.hub.db.one("SELECT grade FROM submissions WHERE sub_id = ?", (sub,))["grade"])["score"], 5)
        phones = self.get(self.hport, "/phones")
        self.assertIn("Test phone", phones)
        self.assertIn("Set up a new phone", phones)
        for path in ("/", "/assignments", "/comprehension", "/courses", "/library", "/language"):
            self.assertIn("aria-label='Main'", self.get(self.hport, path))

    def test_hidden_assignments_are_not_shown(self):
        aid = self.hub.create_assignment(load_sample())["id"]
        self.phone.sync_from_hub(f"http://127.0.0.1:{self.hport}")
        self.post(self.pport, "/login", {"student": "Ana"})
        self.assertNotIn("Fractions and plants", self.get(self.pport, "/"))
        self.assertIn("not open", self.post(self.pport, f"/a/{aid}", GOOD_ANSWERS) + self.get_status(f"/a/{aid}"))

    def get_status(self, path):
        try:
            return self.get(self.pport, path)
        except urllib.error.HTTPError as e:
            return e.read().decode()

    def test_need_fetches_a_copy_the_phone_is_missing(self):
        aid = self.hub.create_assignment(load_sample())["id"]
        # The hub wrongly believes the phone already holds it (e.g. its memory card was replaced).
        self.hub.record_inventory("phone-1", {aid: self.hub.current_version(aid)}, complete=False)
        self.hub.set_open(aid, "phone-1", True)  # sends a 1-packet OPEN
        self.assertTrue(wait_for(self.phone_open(aid)))  # phone replied NEED, hub sent the full copy

    def test_edit_sends_new_version_and_old_answers_grade_against_old_questions(self):
        aid = self.hub.create_assignment(load_sample())["id"]
        v1 = self.hub.current_version(aid)
        self.hub.set_open(aid, "phone-1", True)
        self.assertTrue(wait_for(self.phone_open(aid)))
        self.post(self.pport, "/login", {"student": "Ana"})
        self.post(self.pport, f"/a/{aid}", GOOD_ANSWERS)
        self.assertTrue(wait_for(lambda: self.hub.db.one("SELECT 1 FROM submissions WHERE student = 'Ana'")))

        edited = load_sample()
        edited["questions"] = edited["questions"][:2]  # teacher trims the assignment
        self.post_json(self.hport, "/api/assignments", edited)
        v2 = self.hub.current_version(aid)
        self.assertNotEqual(v1, v2)
        self.assertTrue(wait_for(lambda: (self.phone.get_assignment(aid) or {}).get("version") == v2))
        self.assertEqual(len(self.phone.get_assignment(aid)["questions"]), 2)
        grade = json.loads(self.hub.db.one("SELECT grade FROM submissions WHERE student = 'Ana'")["grade"])
        self.assertEqual(grade["max"], 7)  # still graded on the 5 questions Ana saw
        self.assertIn("earlier version", self.get(self.hport, f"/assignments/{aid}"))

    def test_submissions_from_several_students_share_one_message(self):
        aid = self.hub.create_assignment(load_sample())["id"]
        self.hub.set_open(aid, "phone-1", True)
        self.assertTrue(wait_for(self.phone_open(aid)))
        self.phone.batch_s = 2.0
        for name in ("Ana", "Bo", "Caio"):
            self.phone.submit(self.phone.get_assignment(aid), name, GOOD_ANSWERS)
        self.assertTrue(wait_for(lambda: self.hub.db.one("SELECT COUNT(*) AS n FROM submissions")["n"] == 3))
        msg_ids = {r["msg_id"] for r in self.phone.db.all("SELECT msg_id FROM submissions")}
        self.assertEqual(len(msg_ids), 1)

    def test_library_serves_starter_and_blocks_traversal(self):
        self.assertIn("Fractions", self.get(self.pport, "/library?q=fractions"))
        self.assertIn("<h1>Fractions", self.get(self.pport, "/library/files/starter/fractions.html"))
        with self.assertRaises(urllib.error.HTTPError):
            self.get(self.pport, "/library/files/..%2Fphone.db")


class CoursePackTests(EndToEndBase):
    LOSS = 0.0  # exact packet counts

    def test_wifi_sync_then_opening_costs_one_packet(self):
        aid = self.hub.create_assignment(load_sample())["id"]
        self.assertEqual(self.post(self.pport, "/status/sync", {"hub_url": f"127.0.0.1:{self.hport}"}).count("1 new or updated"), 1)
        self.assertEqual(self.phone.get_assignment(aid)["version"], self.hub.current_version(aid))
        before = self.hub.messenger.frames_sent
        self.hub.set_open(aid, "phone-1", True)
        self.assertTrue(wait_for(self.phone_open(aid)))
        self.assertEqual(self.hub.messenger.frames_sent - before, 1)
        self.assertIn("1 packet", self.get(self.hport, f"/assignments/{aid}"))

    def test_memory_card_pack(self):
        self.hub.create_assignment(load_sample())
        with zipfile.ZipFile(io.BytesIO(self.hub.course_pack_zip())) as zf:
            zf.extractall(self.phone.data_dir)
        self.assertEqual(self.phone.load_pack_folder(), 1)
        self.assertEqual(self.phone.load_pack_folder(), 0)  # already up to date
        self.phone.say_hello()  # tells the hub which versions it holds
        aid = "a-fractions1"
        self.assertTrue(wait_for(lambda: (self.hub._holding("phone-1", aid) or {"version": ""})["version"]
                                 == self.hub.current_version(aid)))


FAKE_CATALOG = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
{entries}
</feed>"""
FAKE_ENTRY = """<entry><title>{title}</title><summary>{summary}</summary><language>{lang}</language><name>{name}</name>
<flavour>{flavour}</flavour><category>{category}</category><tags>{tags}</tags><articleCount>10</articleCount><updated>2026-09-01T00:00:00Z</updated>
<link rel="http://opds-spec.org/acquisition/open-access" type="application/x-zim" href="{base}/{name}_{flavour}.zim.meta4" length="{size}"/></entry>"""


def fake_catalog_xml(base="https://download.example.org/zim"):
    rows = [
        ("PhET Interactive Simulations", "Simulations", "eng", "phet_en_all", "", "phet", "phet", 110_000_000),
        ("Wikipedia", "The free encyclopedia", "eng", "wikipedia_en_all", "mini", "wikipedia", "wikipedia", 14_000_000_000),
        ("Wikipedia", "The free encyclopedia", "eng", "wikipedia_en_all", "nopic", "wikipedia", "wikipedia", 52_000_000_000),
        ("Wikipedia 100", "Top 100", "eng", "wikipedia_en_100", "", "wikipedia", "wikipedia", 330_000_000),
        ("Vikidia", "The encyclopedia for children", "eng", "vikidia_en_all", "nopic", "vikidia", "vikidia", 11_000_000),
        ("Stardew Valley Wiki", "Farming game", "eng", "stardewvalleywiki.com_en_all", "", "", "gaming;fandom", 90_000_000),
        ("Ocean course", "A short course for school students about oceans", "eng", "oceans_en_all", "", "", "", 5_000_000),
        ("Simulações", "PhET em português", "por", "phet_pt_all", "", "phet", "phet", 80_000_000),
    ]
    entries = "".join(FAKE_ENTRY.format(title=t, summary=s, lang=l, name=n, flavour=f, category=c, tags=g, size=z, base=base)
                      for t, s, l, n, f, c, g, z in rows)
    return FAKE_CATALOG.format(entries=entries).encode()


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cat = catalog.Catalog(os.path.join(self.tmp, "catalog.json"))
        self.cat.entries = catalog.parse_catalog(fake_catalog_xml())

    def test_parse_strips_metalink_and_reads_sizes(self):
        phet = self.cat.find("phet_en_all", "")
        self.assertTrue(phet["url"].endswith("phet_en_all_.zim"))
        self.assertEqual(phet["size"], 110_000_000)

    def test_education_comes_first_and_games_are_marked(self):
        levels = {e["name"]: (e["level"], e["label"]) for e in self.cat.entries}
        self.assertEqual(levels["phet_en_all"], (3, "Science simulations"))
        self.assertEqual(levels["vikidia_en_all"], (3, "Encyclopedia for children"))
        self.assertEqual(levels["wikipedia_en_all"], (2, "Encyclopedia"))
        self.assertEqual(levels["oceans_en_all"], (2, "Learning reference"))  # found by its description
        self.assertEqual(levels["stardewvalleywiki.com_en_all"][0], 0)
        names = [g["name"] for g in self.cat.groups("eng", 2)]
        self.assertEqual(names[:2], ["vikidia_en_all", "phet_en_all"])  # core learning, smallest first
        self.assertNotIn("stardewvalleywiki.com_en_all", names)
        self.assertIn("stardewvalleywiki.com_en_all", [g["name"] for g in self.cat.groups("eng", 0)])
        self.assertEqual([g["name"] for g in self.cat.groups("por", 0)], ["phet_pt_all"])

    def test_wikipedia_flavours_are_one_collection(self):
        wp = next(g for g in self.cat.groups("eng", 2) if g["name"] == "wikipedia_en_all")
        self.assertEqual([o["flavour"] for o in wp["options"]], ["mini", "nopic"])

    def test_suggest_fits_budget(self):
        picks = self.cat.suggest("en", 1_000_000_000)
        self.assertLessEqual(sum(p["size"] for p in picks), 1_000_000_000)
        self.assertIn("phet_en_all|", [p["key"] for p in picks])

    def test_cache_survives_restart(self):
        with open(self.cat.cache_path, "w") as fh:
            json.dump({"fetched_at": 1.0, "entries": self.cat.entries}, fh)
        again = catalog.Catalog(self.cat.cache_path)
        self.assertEqual(len(again.entries), len(self.cat.entries))


class _Files(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class CardDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = os.path.join(self.tmp, "mirror")
        os.makedirs(self.src)
        with open(os.path.join(self.src, "vikidia_en_all_nopic.zim"), "wb") as fh:
            fh.write(os.urandom(300_000))
        with open(os.path.join(self.src, "pg11.txt"), "w") as fh:
            fh.write("Title: Alice's Adventures in Wonderland\n\nAlice was beginning to get very tired...\n")
        handler = functools.partial(_Files, directory=self.src)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def items(self):
        zim = {"key": "vikidia_en_all|nopic", "title": "Vikidia (nopic)", "label": "Encyclopedia for children", "level": 3,
               "lang": "eng", "size": 300_000, "folder": "vikidia", "kind": "zim",
               "files": [(f"{self.base}/vikidia_en_all_nopic.zim", "vikidia_en_all_nopic.zim")], "license": "x"}
        books = {"key": "gutenberg|classics", "title": "1 classic novel", "label": "Books and source texts", "level": 2,
                 "lang": "eng", "size": 60, "folder": "books", "kind": "txt",
                 "files": [(f"{self.base}/pg11.txt", "pg11.txt")], "license": "Public domain in the USA"}
        return [zim, books]

    def test_copies_files_and_writes_index_the_phone_reads(self):
        card = os.path.join(self.tmp, "card", "meshlearn")
        self.assertEqual(catalog.remaining_bytes(self.items(), card), 300_060)
        job = catalog.DownloadJob(self.items(), card)
        job.start()
        job.join(30)
        self.assertEqual(job.progress()["state"], "done", job.progress())
        self.assertEqual(catalog.remaining_bytes(self.items(), card), 0)
        phone = Phone(card, "hub", "t", lambda h: None)
        titles = {i["title"] for i in phone.library_items()}
        self.assertIn("Vikidia (nopic)", titles)
        self.assertIn("Alice's Adventures in Wonderland", titles)

    def test_hub_library_page_and_selection(self):
        hub = Hub(os.path.join(self.tmp, "hub.db"), lambda h: _QuietRadio())
        hub.catalog.entries = catalog.parse_catalog(fake_catalog_xml(self.base))
        port = free_port()
        server = hub.app.serve_in_thread("127.0.0.1", port)
        self.addCleanup(server.shutdown)
        post = lambda path, data: urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", urllib.parse.urlencode(data).encode()).read().decode()  # noqa: E731
        page_html = post("/library/add", {"key": "wikipedia_en_all|mini"})
        self.assertIn("14.00 GB</strong> chosen", page_html)
        post("/library/add", {"key": "wikipedia_en_all|nopic"})  # replaces the other version
        self.assertEqual([i["key"] for i in hub.selection()], ["wikipedia_en_all|nopic"])
        self.assertIn("too much", urllib.request.urlopen(f"http://127.0.0.1:{port}/library").read().decode())
        post("/library/suggest", {})
        self.assertLessEqual(sum(i["size"] for i in hub.selection()), hub.library_budget())
        self.assertIn("Vikidia", urllib.request.urlopen(f"http://127.0.0.1:{port}/library?level=3").read().decode())
        outsider = Request("POST", "/library/download", {}, {}, b"dest=/tmp", "10.0.0.7")
        self.assertIn("hub laptop itself", hub.library_download_start(outsider).headers["Location"].replace("+", " "))


class KolibriTests(unittest.TestCase):
    """Kolibri topics: from the channel's own list, through the hub's card selection, to the student library."""
    CHANNEL = "c" * 32

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.storage = os.path.join(self.tmp, "mirror", "content", "storage")
        self.hub = Hub(os.path.join(self.tmp, "hub.db"), lambda h: _QuietRadio())
        self.hub.catalog.entries = kolibri.parse_channels([
            {"id": self.CHANNEL, "name": "Khan Academy (Kiswahili)", "language": "sw", "included_languages": ["en", "sw"],
             "description": "Math", "published_size": 9000, "last_published": "2023-07-06T00:00:04Z",
             "kind_count": [{"kind_id": "topic", "count": 4}, {"kind_id": "video", "count": 5}, {"kind_id": "exercise", "count": 1}]},
            {"id": "d" * 32, "name": "Only exercises", "language": "en", "kind_count": [{"kind_id": "exercise", "count": 9}]}])
        for e in self.hub.catalog.entries:
            catalog._rate(e)
        self.make_channel()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Files, directory=os.path.join(self.tmp, "mirror")))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        patch = mock.patch.object(kolibri, "STUDIO", f"http://127.0.0.1:{server.server_address[1]}")
        patch.start()
        self.addCleanup(patch.stop)
        self.port = free_port()
        self.addCleanup(self.hub.app.serve_in_thread("127.0.0.1", self.port).shutdown)

    def make_channel(self):
        """Channel > Math > (Adding: v1, v2, exercise; Taking away: v1 again, a PDF and e-book pair, a closed video, a teacher's key)"""
        databases = os.path.join(self.tmp, "mirror", "content", "databases")
        os.makedirs(databases)
        db = sqlite3.connect(os.path.join(databases, self.CHANNEL + ".sqlite3"))
        db.executescript("""
            CREATE TABLE content_contentnode (id TEXT, title TEXT, kind TEXT, lft INT, rght INT, level INT, parent_id TEXT,
                license_name TEXT, license_owner TEXT, author TEXT, coach_content INT);
            CREATE TABLE content_file (contentnode_id TEXT, local_file_id TEXT, preset TEXT, supplementary INT, thumbnail INT);
            CREATE TABLE content_localfile (id TEXT, extension TEXT, file_size INT);""")
        def node(id, title, kind, lft, rght, level, parent, lic="CC BY-NC-SA", coach=0):
            db.execute("INSERT INTO content_contentnode VALUES (?,?,?,?,?,?,?,?,'Khan Academy','',?)",
                       (id, title, kind, lft, rght, level, parent, None if kind == "topic" else lic, coach))
        def file(node_id, letter, ext, size, preset, extra=0):
            fid = letter * 32
            db.execute("INSERT INTO content_file VALUES (?,?,?,?,0)", (node_id, fid, preset, extra))
            db.execute("INSERT INTO content_localfile SELECT ?,?,? WHERE NOT EXISTS (SELECT 1 FROM content_localfile WHERE id=?)",
                       (fid, ext, size, fid))
            os.makedirs(os.path.join(self.storage, letter, letter), exist_ok=True)
            with open(os.path.join(self.storage, letter, letter, f"{fid}.{ext}"), "wb") as fh:
                fh.write(bytes(range(256)) * (size // 256 + 1))
                fh.truncate(size)
        node("root", "Khan Academy (Kiswahili)", "topic", 1, 22, 0, None)
        node("math", "Hisabati", "topic", 2, 21, 1, "root")
        node("add", "Kuongeza", "topic", 3, 10, 2, "math")
        node("v1", "Kuongeza namba", "video", 4, 5, 3, "add")
        node("v2", "Kuongeza zaidi", "video", 6, 7, 3, "add")
        node("ex", "Mazoezi", "exercise", 8, 9, 3, "add", "Special Permissions")
        node("sub", "Kutoa", "topic", 11, 20, 2, "math")
        node("v1b", "Kuongeza namba", "video", 12, 13, 3, "sub")
        node("doc", "Kitabu", "document", 14, 15, 3, "sub", "CC BY")
        node("closed", "Imefungwa", "video", 16, 17, 3, "sub", "All Rights Reserved")
        node("key", "Majibu", "document", 18, 19, 3, "sub", "CC BY", coach=1)
        file("v1", "a", "mp4", 4000, "high_res_video")
        file("v1", "b", "mp4", 1000, "low_res_video")
        file("v1b", "b", "mp4", 1000, "low_res_video")
        file("v1", "e", "vtt", 50, "video_subtitle", extra=1)
        file("v2", "c", "mp4", 2000, "low_res_video")
        file("doc", "d", "epub", 700, "epub")
        file("doc", "f", "pdf", 600, "document")
        file("closed", "1", "mp4", 800, "low_res_video")
        file("key", "2", "pdf", 300, "document")
        db.commit()
        db.close()

    def page(self, path, data=None):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", body).read().decode()

    def test_channels_become_catalog_entries(self):
        (entry,) = self.hub.catalog.entries   # the channel with nothing a browser opens is left out
        self.assertEqual((entry["lang"], entry["label"], entry["ages"]), ("swa", "Courses and lessons", [6, 18]))
        self.assertIn("5 videos", entry["summary"])
        html = self.page("/library?lang=swa")
        self.assertIn("Choose topics", html)
        self.assertIn(f"/library/kolibri?id={self.CHANNEL}", html)
        many = kolibri.parse_channels([{"id": "e" * 32, "name": "Stories", "language": "mul", "included_languages": ["sw", "swa", "pt-BR", "und"],
                                        "kind_count": [{"kind_id": "document", "count": 3}]}])
        self.assertEqual(many[0]["lang"], "swa,por")

    def test_refresh_keeps_kiwix_when_kolibri_is_down(self):
        kiwix = catalog.parse_catalog(fake_catalog_xml())
        with mock.patch.object(catalog, "fetch_catalog", return_value=list(kiwix)), \
                mock.patch.object(kolibri, "fetch_channels", side_effect=OSError("down")):
            self.assertEqual(self.hub.catalog.refresh(), len(kiwix) + 1)   # and the Kolibri entry from before
        self.assertTrue(self.hub.catalog.find(f"kolibri:{self.CHANNEL}", ""))

    def test_choose_topics_copy_and_read_them_on_the_phone(self):
        self.assertIn("Get the list of topics", self.page(f"/library/kolibri?id={self.CHANNEL}"))
        html = self.page("/library/kolibri/open", {"id": self.CHANNEL})
        self.assertIn("Hisabati", html)
        html = self.page(f"/library/kolibri?id={self.CHANNEL}&topic=math")
        self.assertIn("2 lessons · 3 KB", html)                     # Kuongeza: the small videos, no subtitles or exercise
        self.assertIn("Left out here: 3 exercises", html)            # exercise, closed licence, teacher's key
        self.assertNotIn("Imefungwa", self.page(f"/library/kolibri?id={self.CHANNEL}&topic=sub"))

        back = f"/library/kolibri?id={self.CHANNEL}&topic=math"
        self.page("/library/kolibri/add", {"id": self.CHANNEL, "topic": "add", "back": back})
        html = self.page("/library/kolibri/add", {"id": self.CHANNEL, "topic": "sub", "back": back})
        self.assertIn("✓ Added", html)
        # v1 sits under both topics but is copied, and counted, once: 1000 + 2000 + the PDF's 600
        self.assertEqual(catalog.total_bytes(self.hub.selection()), 3600)
        self.assertIn("✓ 2 chosen", self.page("/library?lang=swa"))

        card = os.path.join(self.tmp, "card", "meshlearn")
        self.assertEqual(catalog.remaining_bytes(self.hub.selection(), card), 3600)
        job = catalog.DownloadJob(self.hub.selection(), card)
        job.run()
        self.assertEqual((job.state, job.progress()["done"], job.total), ("done", 3600, 3600), job.error)
        with open(os.path.join(card, "library", "index.json"), encoding="utf-8") as fh:
            index = json.load(fh)
        self.assertEqual(sorted(e["file"] for e in index), [f"kolibri/{c * 32}.{x}" for c, x in (("b", "mp4"), ("c", "mp4"), ("f", "pdf"))])

        phone = Phone(card, "hub", "t", lambda h: _QuietRadio())
        port = free_port()
        self.addCleanup(phone.app.serve_in_thread("127.0.0.1", port).shutdown)
        get = lambda path: urllib.request.urlopen(f"http://127.0.0.1:{port}{path}").read().decode()  # noqa: E731
        home = get("/library")
        self.assertIn("Lessons: 3", home)
        self.assertNotIn("Kuongeza zaidi", home)                     # behind the course's own card
        shelf = get("/library?shelf=" + urllib.parse.quote("Khan Academy (Kiswahili)"))
        self.assertIn("Hisabati: Kuongeza</h2>", shelf)
        self.assertIn("Khan Academy · CC BY-NC-SA", shelf)
        with mock.patch("meshlearn.phone.SHELF_FLAT", 1):            # a big course lists its topics first
            shelf = get("/library?shelf=" + urllib.parse.quote("Khan Academy (Kiswahili)"))
            self.assertNotIn("Kuongeza zaidi", shelf)
            self.assertIn("&amp;c=Hisabati%3A%20Kuongeza", shelf)
            self.assertIn("Kuongeza zaidi", get("/library?shelf=" + urllib.parse.quote("Khan Academy (Kiswahili)")
                                                + "&c=" + urllib.parse.quote("Hisabati: Kuongeza")))
        self.assertIn("Kuongeza zaidi", get("/library?q=zaidi"))

        # Skipping ahead in a video
        url = f"http://127.0.0.1:{port}/library/files/kolibri/{'c' * 32}.mp4"
        resp = urllib.request.urlopen(urllib.request.Request(url, headers={"Range": "bytes=256-259"}))
        self.assertEqual((resp.status, resp.headers["Content-Range"], resp.read()), (206, "bytes 256-259/2000", bytes([0, 1, 2, 3])))
        resp = urllib.request.urlopen(url)
        self.assertEqual((resp.status, len(resp.read()), resp.headers["Accept-Ranges"]), (200, 2000, "bytes"))

    def test_a_bigger_topic_replaces_the_ones_inside_it(self):
        self.page("/library/kolibri/open", {"id": self.CHANNEL})
        self.page("/library/kolibri/add", {"id": self.CHANNEL, "topic": "add"})
        self.page("/library/kolibri/add", {"id": self.CHANNEL, "topic": "root"})
        self.page("/library/kolibri/add", {"id": self.CHANNEL, "topic": "sub"})      # already inside
        self.assertEqual([i["title"] for i in self.hub.selection()], ["Khan Academy (Kiswahili)"])
        self.assertIn("Included in", self.page(f"/library/kolibri?id={self.CHANNEL}&topic=sub"))
        self.page("/library/remove", {"key": self.hub.selection()[0]["key"]})
        with mock.patch.object(kolibri, "MAX_FILES", 1):
            self.assertIn("pick smaller topics", self.page("/library/kolibri/add", {"id": self.CHANNEL, "topic": "root"}))
        self.assertEqual(self.hub.selection(), [])

    def test_stopped_copy_lists_the_lessons_that_made_it(self):
        channel = kolibri.Channel(self.hub.kolibri_dir, self.CHANNEL)
        channel.fetch()
        item = channel.selection_item(channel.node("add"), self.hub.catalog.entries[0])
        os.remove(os.path.join(self.storage, "c", "c", "c" * 32 + ".mp4"))
        card = os.path.join(self.tmp, "card")
        job = catalog.DownloadJob([item], card)
        job.run()
        self.assertEqual(job.state, "failed")
        with open(os.path.join(card, "library", "index.json"), encoding="utf-8") as fh:
            self.assertEqual([e["title"] for e in json.load(fh)], ["Kuongeza namba"])

    def test_outsiders_and_odd_ids(self):
        outsider = Request("POST", "/library/kolibri/open", {}, {}, f"id={self.CHANNEL}".encode(), "10.0.0.7")
        self.assertIn("hub laptop itself", self.hub.kolibri_open(outsider).headers["Location"].replace("+", " "))
        with self.assertRaises(kolibri.KolibriError):
            kolibri.Channel(self.hub.kolibri_dir, "../../etc/passwd")
        self.assertIn("Card library", self.page("/library/kolibri?id=../x"))


class AgeTests(unittest.TestCase):
    def entry(self, name, category="", summary="", tags=()):
        return {"name": name, "category": category, "title": name, "summary": summary, "tags": list(tags)}

    def test_known_collections_and_description_clues(self):
        self.assertEqual(catalog.ages_for(self.entry("vikidia_en_all", "vikidia")), (8, 13, False))
        self.assertEqual(catalog.ages_for(self.entry("booksdash_en_all")), (3, 9, False))
        self.assertEqual(catalog.ages_for(self.entry("wikiversity_en_all", "wikiversity")), (15, 99, False))
        self.assertEqual(catalog.ages_for(self.entry("x_en_all", summary="Math for middle school students")), (11, 14, False))
        self.assertEqual(catalog.ages_for(self.entry("y_en_all", summary="Stories for kids")), (5, 12, False))
        self.assertEqual(catalog.ages_for(self.entry("z_en_all", summary="Lecture notes for university courses")), (17, 99, False))
        # Stack Exchange "for teachers and students" is still a Q&A site for older students
        self.assertEqual(catalog.ages_for(self.entry("chemistry.stackexchange.com_en_all", "stack_exchange",
                                                     "Q&A for teachers and students")), (16, 99, False))
        self.assertEqual(catalog.ages_for(self.entry("mystery_en_all", summary="Assorted pages")), (12, 99, True))

    def test_age_groups_overlap(self):
        self.assertTrue(library.ages_overlap((8, 13), ["lower"]))      # shares 12 and 13 with 12-14
        self.assertFalse(library.ages_overlap((6, 12), ["lower"]))     # only age 12 in common
        self.assertTrue(library.ages_overlap((19, 99), ["adult"]))
        self.assertFalse(library.ages_overlap((15, 99), ["early", "primary"]))
        self.assertEqual(library.age_span(["primary", "lower"]), (9, 14))

    def test_browse_and_starter_sets_follow_ages(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        cat = catalog.Catalog(os.path.join(tmp, "c.json"))
        cat.entries = catalog.parse_catalog(fake_catalog_xml())
        young = [g["name"] for g in cat.groups("eng", 2, ages=["primary"], only_fit=True)]
        self.assertIn("vikidia_en_all", young)
        self.assertNotIn("wikipedia_en_all", young)
        teens = [g["name"] for g in cat.groups("eng", 2, ages=["upper"], only_fit=False)]
        self.assertLess(teens.index("wikipedia_en_all"), teens.index("vikidia_en_all"))  # suited ones first
        kid_keys = [p["key"] for p in cat.suggest("en", 10**10, ["primary"])]
        self.assertIn("vikidia_en_all|nopic", kid_keys)
        self.assertNotIn("wikipedia_en_all|mini", kid_keys)
        teen_keys = [p["key"] for p in cat.suggest("en", 10**12, ["upper"])]
        self.assertNotIn("vikidia_en_all|nopic", teen_keys)

    def test_hub_saves_students_ages(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        hub = Hub(os.path.join(tmp, "hub.db"), lambda h: _QuietRadio())
        self.assertEqual(hub.student_ages(), ["lower", "upper"])
        port = free_port()
        server = hub.app.serve_in_thread("127.0.0.1", port)
        self.addCleanup(server.shutdown)
        body = urllib.parse.urlencode([("ages_form", "1"), ("ages", "early"), ("ages", "primary")]).encode()
        html = urllib.request.urlopen(f"http://127.0.0.1:{port}/library/settings", body).read().decode()
        self.assertEqual(hub.student_ages(), ["early", "primary"])
        self.assertIn("starter set for ages 5–11", html)


class LibraryLanguageTests(unittest.TestCase):
    """The students' language must be choosable before the collection list is downloaded."""

    def test_language_menu_works_without_catalog(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        hub = Hub(os.path.join(tmp, "hub.db"), lambda h: _QuietRadio())
        port = free_port()
        server = hub.app.serve_in_thread("127.0.0.1", port)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{port}"
        html = urllib.request.urlopen(f"{url}/library").read().decode()
        self.assertIn("<option value='por'>Portuguese</option>", html)
        self.assertIn("<option value='swa'>Swahili</option>", html)
        html = urllib.request.urlopen(f"{url}/library/settings", urllib.parse.urlencode({"lang": "swa"}).encode()).read().decode()
        self.assertEqual(hub.setting("lang"), "swa")
        self.assertIn("<option value='swa' selected>Swahili</option>", html)
        html = urllib.request.urlopen(f"{url}/library/suggest", b"").read().decode()
        self.assertIn("First download the list", html)


class _QuietRadio:
    """Stands in for the messenger where a test needs no radio."""

    def __init__(self):
        self.sent = []

    def status(self):
        return {"pending": 0, "acked": 0, "failed": 0, "frames_sent": 0}

    def send(self, dest, msg_type, payload):
        self.sent.append((dest, msg_type, payload))
        return len(self.sent)

    def message_status(self, msg_id):
        return "acked"


def submit_via_radio(hub, student, answers, duration=None, phone="phone-1", aid="a-fractions1", version=None, t=None):
    item = {"id": asg.new_id("s"), "a": aid, "v": version or hub.current_version(aid), "u": student, "x": answers,
            "t": t or time.time()}
    if duration:
        item["d"] = duration
    hub.on_message(protocol.Message(1, protocol.SUBMIT, {"s": [item]}, src=phone))
    return item["id"]


class CourseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hub = Hub(os.path.join(self.tmp, "hub.db"), lambda h: _QuietRadio())

    def test_starter_courses_load_from_the_file(self):
        ids = [c["id"] for c in self.hub.courses()]
        self.assertEqual(ids[:3], ["math", "science", "literacy"])
        self.assertTrue(self.hub.get_course("mechanics")["comingSoon"])
        self.assertEqual(courses.unit_number(self.hub.get_course("math"), "measurement"), "2")

    def test_file_changes_flow_unless_the_hub_changed_the_course(self):
        path = os.path.join(self.tmp, "courses.json")
        with open(path, "w") as fh:
            json.dump({"courses": [{"id": "math", "title": "Maths (new name)", "units": ["Numbers"]},
                                   {"id": "science", "title": "Science 2", "units": []}]}, fh)
        self.hub.save_course(dict(self.hub.get_course("science"), title="Our science"))
        self.hub.load_course_file(path)
        self.assertEqual(self.hub.get_course("math")["title"], "Maths (new name)")
        self.assertEqual(self.hub.get_course("science")["title"], "Our science")  # hub's edit kept

    def test_course_pages_and_builder(self):
        port = free_port()
        server = self.hub.app.serve_in_thread("127.0.0.1", port)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{port}"
        body = urllib.parse.urlencode({"title": "Digital skills", "units": "Using a phone\nStaying safe online"}).encode()
        page = urllib.request.urlopen(f"{url}/courses", body).read().decode()
        self.assertIn("Unit 2: Staying safe online", page)
        course = self.hub.get_course("digital-skills")
        self.assertEqual([u["id"] for u in course["units"]], ["using-a-phone", "staying-safe-online"])
        self.hub.create_assignment(load_sample())
        self.assertIn("Mathematics · Unit 1: Numbers and fractions", urllib.request.urlopen(f"{url}/assignments/a-fractions1").read().decode())
        builder = urllib.request.urlopen(f"{url}/assignments/new?course=math&unit=algebra").read().decode()
        self.assertIn('"course": "math", "unit": "algebra"', builder)
        self.assertIn("Fractions and plants check-in", urllib.request.urlopen(f"{url}/courses/math").read().decode())
        packed = self.hub.packed_for_phone("a-fractions1")
        self.assertEqual((packed["Ct"], packed["Ut"], packed["Un"]), ("Mathematics", "Numbers and fractions", "1"))


class ComprehensionTests(unittest.TestCase):
    def setUp(self):
        self.a = asg.normalize_assignment(load_sample())

    def record(self, student, scores, answers=None, mode="graded", at=1.0, duration=None):
        return {"student": student, "aid": self.a["id"], "course": "math", "unit": "fractions", "mode": mode, "at": at,
                "duration": duration, "questions": self.a["questions"], "answers": answers or {}, "scores": scores}

    def test_levels_follow_the_sandbox(self):
        self.assertEqual(comprehension.level_of(None, 0), "Not started")
        self.assertEqual(comprehension.level_of(90, 2), "Just started")
        self.assertEqual([comprehension.level_of(s, 5) for s in (90, 70, 50, 10)], ["Strong", "Good", "Developing", "Needs work"])

    def test_graded_and_practice_scoring(self):
        graded = comprehension.question_scores([self.record("Ana", {"q1": 1, "q2": 0, "q3": 1, "q4": 1, "q5": 1.5})])
        self.assertEqual([round(q["score"], 2) for q in graded], [1, 0, 1, 1, 0.5])
        tries = [self.record("Bo", {"q1": 0, "q2": 0, "q3": 1, "q4": 0, "q5": None}, mode="practice", at=1),
                 self.record("Bo", {"q1": 1, "q2": 0, "q3": 1, "q4": 0, "q5": None}, mode="practice", at=2),
                 self.record("Bo", {"q1": 1, "q2": 1, "q3": 1, "q4": 0, "q5": None}, mode="practice", at=3)]
        by_q = {q["qid"]: q["score"] for q in comprehension.question_scores(tries)}
        self.assertEqual(by_q, {"q1": 0.6, "q2": 0.3, "q3": 1.0, "q4": 0.0})  # q5 never graded: left out

    def test_mistake_kinds(self):
        q = {"id": "n", "type": "number", "answer": 0.75, "tolerance": 0.001, "points": 1}
        self.assertEqual(comprehension.diagnose(q, "-0.75", 0), "sign")
        self.assertEqual(comprehension.diagnose(q, "7.5", 0), "place")
        self.assertEqual(comprehension.diagnose(q, "0.76", 0), "rounding")
        self.assertEqual(comprehension.diagnose(q, "4/3", 0), "inverted")
        self.assertEqual(comprehension.diagnose(q, "2", 0), "unexplained")
        self.assertIsNone(comprehension.diagnose(q, "3/4", 1))
        self.assertEqual(comprehension.diagnose({"id": "c", "type": "choice", "points": 1}, "0", 0), "concept")
        self.assertEqual(comprehension.diagnose({"id": "l", "type": "long", "points": 4}, "x", 1), "explaining")
        self.assertIsNone(comprehension.diagnose({"id": "l", "type": "long", "points": 4}, "x", 3))

    def test_pace_signals(self):
        wrong = {"q1": 0, "q2": 0, "q3": 0, "q4": 0, "q5": 0}
        right = {"q1": 1, "q2": 1, "q3": 1, "q4": 1, "q5": 3}
        self.assertEqual(comprehension.pace(self.record("A", wrong, duration=20)), "rushing")
        self.assertEqual(comprehension.pace(self.record("A", right, duration=20)), "fast_right")
        self.assertEqual(comprehension.pace(self.record("A", right, duration=3000)), "slow")
        self.assertIsNone(comprehension.pace(self.record("A", right, duration=300)))
        self.assertIsNone(comprehension.pace(self.record("A", right, duration=None)))

    def test_summary_by_unit_and_student(self):
        course = {"id": "math", "units": [{"id": "fractions", "title": "Fractions"}, {"id": "data", "title": "Data"}]}
        recs = [self.record("Ana", {"q1": 1, "q2": 1, "q3": 1, "q4": 1, "q5": 3}),
                self.record("Bo", {"q1": 0, "q2": 0, "q3": 1, "q4": 0, "q5": 0}, answers={"q2": "-0.75"}, duration=15)]
        s = comprehension.summarize(recs, course)
        self.assertEqual(s["students"]["Ana"]["units"][0]["level"], "Strong")
        self.assertEqual(s["students"]["Bo"]["units"][0]["level"], "Needs work")
        self.assertEqual(s["units"][1]["level"], "Not started")
        self.assertEqual(s["mistakes"]["kinds"]["sign"], 1)
        self.assertEqual(s["pace"], {"rushing": 1})


class LearningDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.hub = Hub(os.path.join(self.tmp, "hub.db"), lambda h: _QuietRadio())
        self.hub.create_assignment(load_sample())
        submit_via_radio(self.hub, "Ana Souza", GOOD_ANSWERS, duration=240)
        submit_via_radio(self.hub, "Bo", {"q1": "0", "q2": "7.5", "q3": "sun", "q4": "0", "q5": "?"}, duration=12)

    def test_export_is_anonymous_and_complete(self):
        data = self.hub.learning_record()
        text = json.dumps(data)
        self.assertEqual((data["format"], data["version"], data["encoding"]), ("meshlearn-learning-record", 1, "none"))
        self.assertNotIn("Ana Souza", text)
        self.assertNotIn("phone-1", text)
        self.assertEqual(len(data["submissions"]), 2)
        bo = next(s for s in data["submissions"] if s["duration_s"] == 12)
        self.assertEqual(bo["mistakes"]["q2"], "place")
        self.assertEqual(bo["pace"], "rushing")
        self.assertEqual(self.hub.pseudonym("Ana Souza"), self.hub.pseudonym("Ana Souza"))
        self.assertIn("submissions.student", data["dictionary"])

    def test_import_adds_and_skips_doubles(self):
        data = self.hub.learning_record()
        other = Hub(os.path.join(self.tmp, "other.db"), lambda h: _QuietRadio())
        self.assertEqual(other.import_learning_record(data)["submissions"], 2)
        again = other.import_learning_record(data)
        self.assertEqual((again["submissions"], again["skipped"]), (0, 2))
        summary = other.course_summary(other.get_course("math"))
        self.assertEqual(len(summary["students"]), 2)
        self.assertTrue(all(name.startswith("s-") for name in summary["students"]))
        with self.assertRaises(ValueError):
            other.import_learning_record({"format": "something-else"})

    def test_hub_pages_show_comprehension_and_data_panel(self):
        port = free_port()
        server = self.hub.app.serve_in_thread("127.0.0.1", port)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{port}"
        page = urllib.request.urlopen(f"{url}/comprehension/math").read().decode()
        self.assertIn("Ana Souza", page)
        self.assertIn("rushed 1 piece of work (fast and wrong)", page)
        self.assertIn("Calculation", page)
        home = urllib.request.urlopen(url).read().decode()
        self.assertIn("Export all data", home)
        self.assertIn("Mathematics", home)
        exported = json.loads(urllib.request.urlopen(f"{url}/data/export.json").read())
        self.assertEqual(len(exported["submissions"]), 2)


class PhoneCourseTests(EndToEndBase):
    LOSS = 0.0

    def test_course_cards_unit_pages_timing_and_levels(self):
        self.phone.looks = ["mid"]
        self.hub.set_setting("ages", "primary")
        aid = self.hub.create_assignment(load_sample())["id"]
        self.phone.sync_from_hub(f"http://127.0.0.1:{self.hport}")
        self.hub.set_open(aid, "phone-1", True)
        self.assertTrue(wait_for(self.phone_open(aid)))
        self.post(self.pport, "/login", {"student": "Ana"})
        home = self.get(self.pport, "/")
        self.assertIn("Your courses", home)
        self.assertIn("/c/math", home)
        self.assertIn("Coming later", home)  # Engineering mechanics, from the course file
        course = self.get(self.pport, "/c/math")
        self.assertIn("Unit 1: Numbers and fractions", course)
        self.assertIn("Find readings in the library", course)
        self.get(self.pport, f"/a/{aid}")
        time.sleep(1.1)
        self.post(self.pport, f"/a/{aid}", GOOD_ANSWERS)
        self.assertGreaterEqual(self.phone.db.one("SELECT duration FROM submissions")["duration"], 1)
        self.assertTrue(wait_for(lambda: self.hub.db.one("SELECT duration FROM submissions WHERE student = 'Ana'")))
        self.assertGreaterEqual(self.hub.db.one("SELECT duration FROM submissions WHERE student = 'Ana'")["duration"], 1)
        sid = self.hub.db.one("SELECT sub_id FROM submissions")["sub_id"]
        self.post(self.hport, f"/submissions/{sid}/grade", {"m_q5": "3"})
        self.assertTrue(wait_for(lambda: self.phone.db.one("SELECT complete FROM submissions")["complete"] == 1))
        self.assertIn("How you are doing", self.get(self.pport, "/results"))
        self.assertIn("Strong · 100%", self.get(self.pport, "/c/math"))


class LanguageTests(unittest.TestCase):
    def test_every_language_file_matches_the_english_words(self):
        from meshlearn import language as lang
        english = set(lang.keys())
        for code, pack in lang.bundled().items():
            with self.subTest(code=code):
                self.assertEqual(pack["code"], code)
                self.assertIn(pack["dir"], ("ltr", "rtl"))
                self.assertEqual(set(pack["strings"]) - english, set())
                for key, text in pack["strings"].items():
                    self.assertEqual(lang.check(key, text), "", f"{code} {key}")
                self.assertEqual(len(pack["lists"]["days"]), 7)
                self.assertEqual(len(pack["lists"]["months"]), 12)

    def test_words_fall_back_to_english_and_fill_in(self):
        from datetime import date

        from meshlearn import language as lang
        from meshlearn.web import due_label, minutes_for
        partial = lang.Strings({"code": "hau", "name": "Hausa",
                                "strings": {"tab.home": "Gida", "home.title": "Sannu, {name}", "card.score": "broken {nope}"}})
        self.assertEqual(partial("tab.home"), "Gida")
        self.assertEqual(partial("tab.library"), "Library")  # not translated yet: English
        self.assertEqual(partial("home.title", name="Ada"), "Sannu, Ada")
        self.assertEqual(partial("card.score", score=3, max=4), "Score 3 / 4")  # a broken translation is ignored
        self.assertEqual(partial.tag, "ha")

        es = lang.Strings(lang.build_pack("spa"))
        self.assertEqual(es.n("card.questions", 1), "1 pregunta")
        self.assertEqual(es.n("card.questions", 5), "5 preguntas")
        self.assertEqual(due_label("2026-11-20", False, date(2026, 11, 15), words=es), ("Para el vie 20 de nov", ""))
        self.assertEqual(minutes_for([{"type": "long"}], es), "unos 3 min")
        self.assertEqual(es.error("Keep this under 600 characters (640 now)."), "Usa menos de 600 caracteres (ahora hay 640).")
        self.assertEqual(es.level("Needs work"), "Necesita práctica")
        self.assertEqual(lang.ENGLISH.html("welcome.just_read", link="<a href='/library'>library</a>"),
                         "Just want to read? Open the <a href='/library'>library</a>.")

        ar = lang.Strings(lang.build_pack("ara"))
        self.assertEqual((ar.tag, ar.dir), ("ar", "rtl"))
        self.assertEqual(lang.build_pack("urd")["dir"], "rtl")  # no file yet, still written right to left
        self.assertEqual(lang.check("home.title", "Habari"), "keep {name}")
        self.assertIn("remove {x}", lang.check("tab.home", "Home {x}"))
        self.assertLess(lang.coverage(lang.build_pack("hau", {"tab.home": "Gida"})), 5)


class SchoolLanguageTests(EndToEndBase):
    LOSS = 0.0

    def sync(self):
        return self.post(self.pport, "/status/sync", {"hub_url": f"127.0.0.1:{self.hport}"})

    def test_school_language_reaches_the_phone_with_the_lessons(self):
        page = self.get(self.hport, "/language")
        self.assertIn("Kiswahili", page)
        self.assertIn("Right to left", page)  # Arabic, Urdu, …
        self.post(self.hport, "/language", {"lang": "swa"})
        self.assertEqual(self.hub.setting("lang"), "swa")  # the same setting the card library uses
        aid = self.hub.create_assignment(load_sample())["id"]
        self.assertIn("Lugha kwenye simu hii: Kiswahili", self.sync())  # the words came with the lessons
        welcome = self.get(self.pport, "/")
        self.assertIn("<html lang='sw'>", welcome)
        self.assertIn("Andika jina lako", welcome)

        self.hub.set_open(aid, "phone-1", True)
        self.assertTrue(wait_for(self.phone_open(aid)))
        self.post(self.pport, "/login", {"student": "Ana"})
        home = self.get(self.pport, "/")
        self.assertIn("Habari, Ana", home)
        self.assertIn("Una kazi 1 ya kufanya.", home)
        form = self.get(self.pport, f"/a/{aid}")
        self.assertIn("Swali 1 kati ya 5", form)
        self.assertIn("data-words='Umejibu {done} kati ya {total}'", form)
        self.assertIn("Tafadhali jibu swali hili.", self.post(self.pport, f"/a/{aid}", {"q1": "1"}))

        # A teacher fixes a word on the hub. A word that drops its {name} is refused, not sent.
        saved = self.post(self.hport, "/language/words", {"lang": "swa", "k.tab.home": "Nyumbani", "k.home.title": "Habari"})
        self.assertIn("keep {name}", saved)
        self.sync()
        home = self.get(self.pport, "/")
        self.assertIn(">Nyumbani<", home)
        self.assertIn("Habari, Ana", home)

    def test_second_language_switch_and_right_to_left(self):
        self.post(self.hport, "/language", {"lang": "ara", "lang2": "eng"})
        self.sync()
        page = self.get(self.pport, "/")
        self.assertIn("<html lang='ar' dir='rtl'>", page)
        self.assertIn("اكتب اسمك", page)
        self.assertIn("action='/lang'", page)
        english = self.post(self.pport, "/lang", {"to": "eng", "back": "/library"})
        self.assertIn("<html lang='en'>", english)
        self.assertIn("Search the library", english)
        self.assertIn(">العربية</button>", english)  # the way back
        # Unknown languages and other sites are ignored.
        self.assertIn("<html lang='en'>", self.post(self.pport, "/lang", {"to": "xyz", "back": "https://example.org/"}))

    def test_right_answers_only_after_the_due_date(self):
        from datetime import date, timedelta
        self.phone.looks = ["mid"]
        self.hub.set_setting("ages", "primary")
        sample = dict(load_sample(), due=(date.today() + timedelta(days=2)).isoformat())
        aid = self.hub.create_assignment(sample)["id"]
        self.hub.set_open(aid, "phone-1", True)
        self.assertTrue(wait_for(self.phone_open(aid)))
        self.post(self.pport, "/login", {"student": "Ana"})
        self.post(self.pport, f"/a/{aid}", {"q1": "0", "q2": "5", "q3": "Chlorophyll", "q4": "1", "q5": "Cutting bread."})
        marked = lambda: (self.phone.db.one("SELECT score FROM submissions") or {"score": None})["score"] is not None  # noqa: E731
        self.assertTrue(wait_for(marked))
        results = self.get(self.pport, "/results")
        self.assertIn("You said: 2/3", results)
        self.assertNotIn("Right answer", results)                               # classmates may still be working
        self.assertIsNone(self.phone.db.one("SELECT right_answers FROM submissions")["right_answers"])
        self.assertEqual(self.hub.send_right_answers(), 0)
        self.assertIn("day after the due date", self.get(self.hport, f"/assignments/{aid}"))

        # The day after: only what this student missed.
        self.hub.create_assignment(dict(self.hub.get_assignment(aid), due=(date.today() - timedelta(days=1)).isoformat()))
        self.assertEqual(self.hub.send_right_answers(), 1)
        self.assertEqual(self.hub.send_right_answers(), 0)                      # once
        got = lambda: self.phone.db.one("SELECT right_answers FROM submissions")["right_answers"]  # noqa: E731
        self.assertTrue(wait_for(lambda: bool(got())))
        self.assertEqual(json.loads(got()), {"q1": "3/6", "q2": "0.75 (± 0.001)"})
        self.assertIn("Right answer: 3/6", self.get(self.pport, "/results"))
        self.post(self.pport, "/login", {"student": "Kofi"})
        self.assertNotIn("Right answer", self.get(self.pport, "/results"))

    def test_right_answers_sent_by_hand_or_never(self):
        aid = self.hub.create_assignment(dict(load_sample(), due=""))["id"]
        self.assertIn("no due date", self.get(self.hport, f"/assignments/{aid}"))
        self.assertFalse(self.hub._answers_allowed(self.hub.get_assignment(aid)))
        self.post(self.hport, f"/assignments/{aid}/answers", {})
        self.assertTrue(self.hub._answers_allowed(self.hub.get_assignment(aid)))
        other = self.hub.create_assignment(dict(load_sample(), id="a-old", due="2020-01-01"))["id"]
        self.assertTrue(self.hub._answers_allowed(self.hub.get_assignment(other)))
        self.post(self.hport, "/settings/answers", {"right_answers": "never", "back": f"/assignments/{other}"})
        self.assertFalse(self.hub._answers_allowed(self.hub.get_assignment(other)))

    def test_country_sets_the_languages_for_learning(self):
        from meshlearn import language as lang
        self.assertIn("Where is your school?", self.get(self.hport, "/"))
        html = self.post(self.hport, "/language/country", {"country": "KE"})
        self.assertIn("Set for Kenya: English, Kiswahili, Français", html)
        self.assertEqual((self.hub.school_language(), self.hub.other_languages()), ("eng", ["swa", "fra"]))
        self.assertNotIn("Where is your school?", self.get(self.hport, "/"))
        self.sync()
        welcome = self.get(self.pport, "/")                        # students pick from all three
        self.assertIn("Which language do you learn in?", welcome)
        for name in ("English", "Kiswahili", "Français"):
            self.assertIn(f">{name}</button>", welcome)
        french = self.post(self.pport, "/lang", {"to": "fra", "back": "/library"})
        self.assertIn("<html lang='fr'>", french)
        self.assertIn(">Kiswahili</button>", french)               # and can switch later
        self.assertIn(">English</button>", french)
        self.assertIn("/library?lang=swa", self.get(self.hport, "/language") + self._library_with_list())

        # Brazil: Portuguese first, in Brazil's own words
        self.post(self.hport, "/language/country", {"country": "BR"})
        self.assertEqual((self.hub.school_language(), self.hub.other_languages()), ("por", ["eng", "deu"]))
        self.sync()
        self.post(self.pport, "/lang", {"to": "por", "back": "/"})
        welcome = self.get(self.pport, "/")
        self.assertIn("<html lang='pt-BR'>", welcome)
        self.assertIn("Escreva seu nome", welcome)
        self.assertIn(">Deutsch</button>", welcome)

        # Germany: English only where students are 12 or older
        self.post(self.hport, "/language/country", {"country": "DE"})
        self.assertEqual((self.hub.school_language(), self.hub.other_languages()), ("deu", ["eng", "fra"]))
        self.assertEqual(self.hub.language_pack("fra")["tag"], "fr-FR")
        self.hub.set_setting("ages", "early,primary")
        self.assertIn("leaves out English", self.get(self.hport, "/language"))
        self.post(self.hport, "/language/country", {"country": "DE"})
        self.assertEqual((self.hub.school_language(), self.hub.other_languages()), ("deu", ["fra"]))
        self.assertNotIn("leaves out English", self.get(self.hport, "/language"))
        self.assertIn("not in the list", self.post(self.hport, "/language/country", {"country": "XX"}))
        self.assertEqual(lang.languages_for("US"), ["eng", "spa", "fra"])
        # A phone from before there could be three still gets two.
        self.post(self.hport, "/language/country", {"country": "US"})
        bundle = self.hub.language_bundle()
        self.assertEqual((bundle["second"]["code"], [m["code"] for m in bundle["more"]]), ("spa", ["fra"]))

    def _library_with_list(self):
        self.hub.catalog.entries = catalog.parse_catalog(fake_catalog_xml())
        return self.get(self.hport, "/library")

    def test_every_language_that_comes_with_meshlearn_is_complete(self):
        from meshlearn import language as lang
        for code in lang.bundled():
            self.assertEqual(set(lang.keys()) - set(lang.bundled()[code]["strings"]), set(), code)
        brazil = lang.regional("por", "BR")
        for key, text in brazil["strings"].items():
            self.assertEqual(lang.check(key, text), "", key)

    def test_a_language_with_no_file_by_memory_card_and_shared(self):
        self.post(self.hport, "/language", {"lang": "hau"})  # MeshLearn has no Hausa words yet
        self.assertIn("not translated into this language yet", self.get(self.hport, "/language"))
        saved = self.post(self.hport, "/language/words",
                          {"lang": "hau", "_name": "Hausa", "_dir": "ltr", "k.tab.home": "Gida", "k.welcome.start": "Fara"})
        self.assertIn("Saved.", saved)
        with zipfile.ZipFile(io.BytesIO(self.hub.course_pack_zip())) as zf:
            self.assertIn("assignments/language.json", zf.namelist())
            zf.extractall(self.phone.data_dir)
        self.phone.load_pack_folder()
        welcome = self.get(self.pport, "/")
        self.assertIn("<html lang='ha'>", welcome)
        self.assertIn(">Gida<", welcome)
        self.assertIn(">Fara<", welcome)
        self.assertIn(">Library<", welcome)  # not translated yet: English

        exported = json.loads(self.get(self.hport, "/language/export/hau"))
        self.assertEqual(exported["strings"]["tab.home"], "Gida")
        self.hub.db.execute("DELETE FROM phrases")  # as if on another school's hub
        result = self.post_json(self.hport, "/api/language/import", exported)
        self.assertEqual((result["saved"], result["name"]), (2, "Hausa"))
        self.assertEqual(self.hub.language_pack("hau")["strings"]["welcome.start"], "Fara")


if __name__ == "__main__":
    unittest.main()
