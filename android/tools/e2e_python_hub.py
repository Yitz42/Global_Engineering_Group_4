"""End-to-end check: the real Python teacher hub and the Kotlin phone, linked by the simulated radio.

    cd android && gradlew :desktop:installDist
    python tools/e2e_python_hub.py [--loss 0.2]

Runs the Python hub in this process and the Kotlin phone (desktop build) as a child process,
then walks the school day: copy assignments over Wi-Fi, open one with a single radio packet,
a student turns it in on the phone's web pages, the hub grades it, and the grade comes back.
Exits non-zero if any step fails.
"""

import argparse
import http.cookiejar
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ANDROID = os.path.dirname(HERE)
REPO = os.path.dirname(ANDROID)
sys.path.insert(0, REPO)

from meshlearn.hub import Hub  # noqa: E402
from meshlearn import ownpics, voices
from meshlearn.messenger import Messenger  # noqa: E402
from meshlearn.transport import UdpSimTransport  # noqa: E402

HUB_HTTP, PHONE_HTTP, HUB_UDP, PHONE_UDP = 8291, 8292, 47291, 47292


def wait_for(what, cond, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                print(f"  ok: {what}")
                return
        except Exception:
            pass
        time.sleep(0.3)
    raise SystemExit(f"FAILED: {what} (waited {timeout} s)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loss", type=float, default=0.2, help="fraction of radio packets each side drops")
    args = ap.parse_args()

    root = tempfile.mkdtemp(prefix="meshlearn-e2e-")
    hub_t = UdpSimTransport("hub", HUB_UDP, {"phone-1": ("127.0.0.1", PHONE_UDP)}, drop_rate=args.loss)
    hub = Hub(os.path.join(root, "hub.db"), lambda h: Messenger(hub_t, os.path.join(root, "hub-outbox.db"), h, retry_s=2.0))
    hub.messenger.start()
    hub.add_phone("phone-1", "Kotlin phone")
    with open(os.path.join(REPO, "meshlearn", "sample_assignment.json"), encoding="utf-8") as fh:
        sample = hub.create_assignment(json.load(fh))
    aid = sample["id"]
    server = hub.app.serve_in_thread("127.0.0.1", HUB_HTTP)

    # Start java directly (not the launcher script) so terminating it really stops the phone.
    java = os.path.join(os.environ["JAVA_HOME"], "bin", "java") if os.environ.get("JAVA_HOME") else "java"
    classpath = os.path.join(ANDROID, "desktop", "build", "install", "desktop", "lib", "*")
    phone_dir = os.path.join(root, "phone")
    phone = subprocess.Popen([java, "-cp", classpath, "org.meshlearn.desktop.MainKt", "--data", phone_dir, "--hub", "hub", "--name", "Kotlin phone", "--port", str(PHONE_HTTP),
                              "--host", "127.0.0.1", "--sim-port", str(PHONE_UDP), "--sim-peer", f"hub=127.0.0.1:{HUB_UDP}",
                              "--sim-loss", str(args.loss), "--batch-seconds", "1"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    jar = http.cookiejar.CookieJar()
    web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    base = f"http://127.0.0.1:{PHONE_HTTP}"

    def get(path):
        return web.open(base + path, timeout=10).read().decode("utf-8")

    def post(path, fields):
        return web.open(base + path, data=urllib.parse.urlencode(fields).encode(), timeout=30).read().decode("utf-8")

    try:
        print(f"radio loss {args.loss:.0%} each way; data in {root}")
        wait_for("Kotlin phone serving its site", lambda: "Welcome" in get("/"), timeout=60)
        wait_for("hub got the phone's HELLO", lambda: (hub.db.one("SELECT info FROM phones WHERE node_id = 'phone-1'") or {})["info"])

        page = post("/status/sync", {"hub_url": f"127.0.0.1:{HUB_HTTP}"})
        assert "Copied from the teacher hub: 1 new" in page, page[-600:]
        print("  ok: copied the assignment from the hub over Wi-Fi")

        hub.set_open(aid, "phone-1", True)
        post("/login", {"student": "Lena Müller"})
        wait_for("OPEN packet reached the phone", lambda: "Fractions and plants check-in" in get("/"))

        form = get(f"/a/{aid}")
        assert "Hand it in" in form and "chlorophyll" not in form.lower(), "answer key must not reach the phone"
        done = post(f"/a/{aid}", {"q1": "1", "q2": "3/4", "q3": "Chlorophyll", "q4": "0", "q5": "We share a pizza in quarters."})
        assert "Done! Your work is saved" in done, done[-600:]
        print("  ok: student turned it in on the phone")

        wait_for("hub received and graded the submission",
                 lambda: hub.db.one("SELECT grade FROM submissions WHERE student = ?", ("Lena Müller",))["grade"])
        row = hub.db.one("SELECT grade, answers, duration FROM submissions WHERE student = ?", ("Lena Müller",))
        grade = json.loads(row["grade"])
        assert grade["score"] == 3.0 and not grade["complete"], grade   # q1-q3 right, q4 wrong, q5 waits for the teacher
        assert row["duration"] and row["duration"] >= 1, dict(row)
        assert json.loads(row["answers"])["q2"] == "3/4"
        wait_for("grade came back to the phone", lambda: "3 / 7 so far" in get("/results"))
        wait_for("phone shows delivery confirmed", lambda: "Your teacher has it" in get("/results"))

        # The teacher picks Kiswahili for the school, with English as a second language; the words
        # reach the phone with the next Wi-Fi copy, and the student can switch back to English.
        web.open(f"http://127.0.0.1:{HUB_HTTP}/language", data=urllib.parse.urlencode({"lang": "swa", "lang2": "eng"}).encode(),
                 timeout=10).read()
        page = post("/status/sync", {"hub_url": f"127.0.0.1:{HUB_HTTP}"})
        assert "Lugha kwenye simu hii: Kiswahili · English" in page, page[-600:]
        home = get("/")
        assert "<html lang='sw'>" in home and "Habari, Lena Müller" in home, home[:800]
        english = post("/lang", {"to": "eng", "back": "/results"})
        assert "<html lang='en'>" in english and "3 / 7 so far" in english, english[:800]
        print("  ok: the school's language came with the lessons, and the student switched to English")

        # A lesson written in English with one Kiswahili question, a drawing and the teacher's voice. By radio come the
        # words and the drawing; the recording comes with the next Wi-Fi copy.
        drawn = hub.save_picture({"n": "our well", "s": ownpics.encode([(4, 1, [(60, 60), (180, 60), (120, 200)])])})
        hub.create_assignment({"id": "a-well", "title": "Our well", "lang": "eng", "tr": {"swa": {"T": "Kisima chetu"}}, "questions": [
            {"type": "choice", "prompt": "Where is the water?", "pic": drawn, "options": ["well", "goat"], "answer": 0,
             "why": ["", "Mixed up the animals"], "tr": {"swa": {"p": "Maji yako wapi?", "o": ["kisima", "mbuzi"]}}}]})
        said = voices.key("Maji yako wapi?", "swa")
        sound = b"\x1a\x45\xdf\xa3" + bytes(range(256)) * 12
        req = urllib.request.Request(f"http://127.0.0.1:{HUB_HTTP}/api/voice/{said}?secs=2", data=sound, headers={"Content-Type": "audio/webm"})
        web.open(req, timeout=10).read()
        hub.set_open("a-well", "phone-1", True)
        wait_for("the translated lesson reached the phone by radio", lambda: "Our well" in get("/"))
        form = get("/a/a-well")
        assert "Where is the water?" in form and "pic own" in form and "stroke='#1f5fbf'" in form and "Mixed up" not in form, form[-3000:]
        post("/lang", {"to": "swa", "back": "/"})
        form = get("/a/a-well")
        assert "Maji yako wapi?" in form and ">kisima<" in form and "Where is the water?" not in form and "data-voice='/voice/" not in form, form[-3000:]
        post("/status/sync", {"hub_url": f"127.0.0.1:{HUB_HTTP}"})
        assert f"data-voice='/voice/{said}'" in get("/a/a-well")
        with web.open(f"{base}/voice/{said}", timeout=10) as resp:
            assert resp.headers["Content-Type"].startswith("audio/webm") and resp.read() == sound
        with web.open(urllib.request.Request(f"{base}/voice/{said}", headers={"Range": "bytes=10-19"}), timeout=10) as resp:
            assert resp.status == 206 and resp.read() == sound[10:20]
        post("/a/a-well", {"q1": "1", "unsure-q1": "1"})
        wait_for("the hub heard the work, its language and the not-sure tick",
                 lambda: (hub.db.one("SELECT lang, signals FROM submissions WHERE assignment_id = 'a-well'") or {"lang": None})["lang"] is None
                 and hub.db.one("SELECT signals FROM submissions WHERE assignment_id = 'a-well'"))
        row = hub.db.one("SELECT lang, signals FROM submissions WHERE assignment_id = 'a-well'")
        assert row["lang"] is None and json.loads(row["signals"]) == {"n": 1}, dict(row)   # Kiswahili is the school's language
        assert "Mixed up the animals" in web.open(f"http://127.0.0.1:{HUB_HTTP}/assignments/a-well", timeout=10).read().decode()
        print("  ok: translated question, drawing and the teacher's voice on the Kotlin phone")
        print("PASSED")
        return 0
    except BaseException:
        print("hub submissions:", [dict(r) for r in hub.db.all("SELECT * FROM submissions")])
        print("hub outbox:", hub.messenger.status())
        try:
            status = get("/status")
            print("phone status:", status[status.find("<div class='grid'>"):][:700])
        except Exception as exc:
            print("phone status unavailable:", exc)
        raise
    finally:
        phone.terminate()
        try:
            out, _ = phone.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            phone.kill()
            out = ""
        if os.environ.get("E2E_VERBOSE"):
            print(out)
        server.shutdown()
        hub.messenger.stop()


if __name__ == "__main__":
    sys.exit(main())
