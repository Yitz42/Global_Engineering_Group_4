"""Tests for the protocol, delivery, grading, library planning and the full hub-to-phone loop.

Run with: python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import socket
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

from meshlearn import assignments as asg
from meshlearn import library, protocol
from meshlearn.hub import Hub
from meshlearn.messenger import Messenger
from meshlearn.phone import Phone
from meshlearn.transport import UdpSimTransport

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "meshlearn", "sample_assignment.json")


def free_port(kind=socket.SOCK_STREAM):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for(cond, timeout=30.0, step=0.1):
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
        asm = protocol.Assembler()
        out = None
        for f in reversed(frames):  # order does not matter
            out = asm.add(f, "a") or out
        self.assertEqual(out.payload, payload)
        self.assertEqual(out.msg_id, msg_id)

    def test_duplicate_frames_are_harmless(self):
        _, frames = protocol.encode(protocol.HELLO, {"name": "p"})
        asm = protocol.Assembler()
        self.assertIsNotNone(asm.add(frames[0], "a"))
        self.assertIsNotNone(asm.add(frames[0], "a"))  # a resend reassembles again; messenger dedupes

    def test_corruption_is_rejected(self):
        _, frames = protocol.encode(protocol.HELLO, {"name": "phone"})
        bad = frames[0][:-3] + b"xyz"
        with self.assertRaises(protocol.ProtocolError):
            protocol.Assembler().add(bad, "a")

    def test_too_big_is_refused(self):
        big = {"blob": os.urandom(80_000).hex()}
        with self.assertRaises(protocol.ProtocolError):
            protocol.encode(protocol.ASSIGN, big)


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        with open(SAMPLE, encoding="utf-8") as fh:
            self.a = asg.normalize_assignment(json.load(fh))

    def test_public_view_hides_key_in_graded_mode(self):
        view = asg.public_view(self.a)
        for q in view["questions"]:
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
        self.assertEqual(g["score"], 5)
        self.assertTrue(g["complete"])
        self.assertEqual(g["max"], 7)

    def test_bad_assignment_rejected(self):
        with self.assertRaises(asg.AssignmentError):
            asg.normalize_assignment({"title": "x", "questions": [{"type": "choice", "prompt": "?", "options": ["a"], "answer": 0}]})


class MessengerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delivers_through_heavy_loss(self):
        pa, pb = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
        got = []
        ta = UdpSimTransport("a", pa, {"b": ("127.0.0.1", pb)}, drop_rate=0.4, seed=1)
        tb = UdpSimTransport("b", pb, {"a": ("127.0.0.1", pa)}, drop_rate=0.4, seed=2)
        ma = Messenger(ta, os.path.join(self.tmp, "a.db"), lambda m: None, retry_s=0.3, max_retry_s=0.6)
        mb = Messenger(tb, os.path.join(self.tmp, "b.db"), got.append, retry_s=0.3, max_retry_s=0.6)
        ma.start(), mb.start()
        try:
            ids = [ma.send("b", protocol.SUBMIT, {"i": i, "pad": "y" * 600}) for i in range(5)]
            self.assertTrue(wait_for(lambda: all(ma.message_status(i) == "acked" for i in ids), 40))
            self.assertEqual(sorted(m.payload["i"] for m in got), list(range(5)))  # each exactly once
        finally:
            ma.stop(), mb.stop()


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


class EndToEndTests(unittest.TestCase):
    """Teacher sends an assignment, a student answers on the phone, the grade comes back."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        hp, pp = free_port(socket.SOCK_DGRAM), free_port(socket.SOCK_DGRAM)
        ht = UdpSimTransport("hub", hp, {"phone-1": ("127.0.0.1", pp)}, drop_rate=0.2, seed=3)
        pt = UdpSimTransport("phone-1", pp, {"hub": ("127.0.0.1", hp)}, drop_rate=0.2, seed=4)
        self.hub = Hub(os.path.join(self.tmp, "hub.db"),
                       lambda h: Messenger(ht, os.path.join(self.tmp, "hub-out.db"), h, retry_s=0.3, max_retry_s=0.6))
        self.phone = Phone(os.path.join(self.tmp, "card"), "hub", "Test phone",
                           lambda h: Messenger(pt, os.path.join(self.tmp, "phone-out.db"), h, retry_s=0.3, max_retry_s=0.6))
        self.hub.messenger.start()
        self.phone.messenger.start()
        self.hport, self.pport = free_port(), free_port()
        self.servers = [self.hub.app.serve_in_thread("127.0.0.1", self.hport),
                        self.phone.app.serve_in_thread("127.0.0.1", self.pport)]
        self.browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
        self.hub.messenger.stop()
        self.phone.messenger.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, port, path):
        return self.browser.open(f"http://127.0.0.1:{port}{path}").read().decode()

    def post(self, port, path, data):
        body = urllib.parse.urlencode(data).encode()
        try:
            return self.browser.open(f"http://127.0.0.1:{port}{path}", body).read().decode()
        except urllib.error.HTTPError as e:
            return e.read().decode()

    def test_full_loop(self):
        self.phone.say_hello()
        self.assertTrue(wait_for(lambda: self.hub.db.one("SELECT 1 FROM phones WHERE node_id = 'phone-1'")))

        with open(SAMPLE, encoding="utf-8") as fh:
            created = json.loads(self.browser.open(urllib.request.Request(
                f"http://127.0.0.1:{self.hport}/api/assignments", data=fh.read().encode(),
                headers={"Content-Type": "application/json"})).read())
        aid = created["id"]
        self.post(self.hport, f"/assignments/{aid}/send", {"node_id": "phone-1"})
        self.assertTrue(wait_for(lambda: self.phone.get_assignment(aid) is not None))
        self.assertNotIn("answer", self.phone.get_assignment(aid)["questions"][0])  # key stayed on the hub

        self.post(self.pport, "/login", {"student": "Ana"})
        self.assertIn("Fractions and plants", self.get(self.pport, "/"))
        bad = self.post(self.pport, f"/a/{aid}", {"q1": "1"})
        self.assertIn("Please fix", bad)
        ok = self.post(self.pport, f"/a/{aid}", {"q1": "1", "q2": "0,75", "q3": "chlorophyll", "q4": "1", "q5": "We share bread."})
        self.assertIn("Done!", ok)

        self.assertTrue(wait_for(lambda: self.hub.db.one("SELECT 1 FROM submissions WHERE student = 'Ana'")))
        sub = self.hub.db.one("SELECT sub_id FROM submissions WHERE student = 'Ana'")
        self.assertIn("needs grading", self.get(self.hport, f"/assignments/{aid}"))
        self.post(self.hport, f"/submissions/{sub['sub_id']}/grade", {"m_q5": "3"})
        self.assertTrue(wait_for(lambda: self.phone.db.one("SELECT complete FROM submissions WHERE student = 'Ana'")["complete"] == 1))
        self.assertIn("7 / 7", self.get(self.pport, "/results"))

        csv_text = self.get(self.hport, f"/assignments/{aid}/grades.csv")
        self.assertIn("Ana", csv_text)

    def test_library_serves_starter_and_blocks_traversal(self):
        page = self.get(self.pport, "/library?q=fractions")
        self.assertIn("Fractions", page)
        self.assertIn("<h1>Fractions", self.get(self.pport, "/library/files/starter/fractions.html"))
        with self.assertRaises(urllib.error.HTTPError):
            self.get(self.pport, "/library/files/..%2Fphone.db")


if __name__ == "__main__":
    unittest.main()
