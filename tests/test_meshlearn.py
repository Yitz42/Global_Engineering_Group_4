"""Tests for the protocol, delivery, grading, library planning and the full hub-to-phone loop.

Run with: python -m unittest discover -s tests -v
"""

import functools
import http.server
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.cookiejar import CookieJar

from meshlearn import assignments as asg
from meshlearn import catalog, comprehension, courses, library, protocol
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


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.a = asg.normalize_assignment(load_sample())

    def test_pack_round_trip_is_lossless(self):
        self.assertEqual(asg.unpack(asg.pack(self.a)), self.a)
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
        self.assertIn("Please fix", self.post(self.pport, f"/a/{aid}", {"q1": "1"}))
        self.assertIn("Done!", self.post(self.pport, f"/a/{aid}", GOOD_ANSWERS))

        self.assertTrue(wait_for(lambda: self.hub.db.one("SELECT 1 FROM submissions WHERE student = 'Ana'")))
        sub = self.hub.db.one("SELECT sub_id FROM submissions WHERE student = 'Ana'")
        self.assertIn("needs grading", self.get(self.hport, f"/assignments/{aid}"))
        self.post(self.hport, f"/submissions/{sub['sub_id']}/grade", {"m_q5": "3"})
        self.assertTrue(wait_for(lambda: self.phone.db.one("SELECT complete FROM submissions WHERE student = 'Ana'")["complete"] == 1))
        self.assertIn("7 / 7", self.get(self.pport, "/results"))
        self.assertIn("Ana", self.get(self.hport, f"/assignments/{aid}/grades.csv"))

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
        self.assertIn("rushing (fast and wrong)", page)
        self.assertIn("Calculation", page)
        home = urllib.request.urlopen(url).read().decode()
        self.assertIn("Export all data", home)
        self.assertIn("Mathematics", home)
        exported = json.loads(urllib.request.urlopen(f"{url}/data/export.json").read())
        self.assertEqual(len(exported["submissions"]), 2)


class PhoneCourseTests(EndToEndBase):
    LOSS = 0.0

    def test_course_cards_unit_pages_timing_and_levels(self):
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


if __name__ == "__main__":
    unittest.main()
