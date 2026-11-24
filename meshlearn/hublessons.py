"""Teacher hub pages for getting lessons to phones: what each phone has, filling a memory card, and a printed setup card.

Mixed into Hub, like the other page files.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import time
from typing import Dict, List, Optional
from urllib.parse import quote, urlencode

from . import catalog as cat
from . import protocol
from .web import Request, Response, esc, redirect
from .zipapps import QR_JS

RADIO_MOST = 40   # packets: more than this is too much to send one phone by radio
APP_FOLDERS = ("org.meshlearn.phone", "com.termux")


def removable_drives() -> List[str]:
    """Memory cards and USB drives plugged into this laptop."""
    found: List[str] = []
    if sys.platform == "win32":
        bits = ctypes.windll.kernel32.GetLogicalDrives()
        for n in range(26):
            root = f"{chr(65 + n)}:\\"
            if bits >> n & 1 and ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 2 and os.path.isdir(root):
                found.append(root)
        return found
    places = ["/Volumes"] if sys.platform == "darwin" else [p for base in ("/media", "/run/media") if os.path.isdir(base)
                                                             for p in [base] + [e.path for e in os.scandir(base) if e.is_dir()]]
    for place in places:
        try:
            for entry in os.scandir(place):
                if entry.is_dir() and os.path.ismount(entry.path) and os.path.realpath(entry.path) != "/":
                    found.append(entry.path)
        except OSError:
            continue
    return sorted(set(found))


def card_folder(root: str) -> str:
    """The phone's MeshLearn folder on a card: the one already there, else the Android app's."""
    folders = [os.path.join(root, "Android", "data", app, "files", "meshlearn") for app in APP_FOLDERS]
    return next((f for f in folders if os.path.isdir(f)), folders[0])


def place_exists(dest: str) -> bool:
    """Whether the card a folder is on is really in. (A missing card leaves only the folder cards appear under.)"""
    if not dest or not os.path.isabs(dest):
        return False
    root = os.path.normpath(dest)
    while not os.path.isdir(root) and os.path.dirname(root) != root:
        root = os.path.dirname(root)
    empty = ("/", "/Volumes", "/media", "/run", "/run/media", "/mnt")
    return os.path.isdir(root) and root not in empty and os.path.dirname(root) not in ("/media", "/run/media")


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class HubLessons:
    def _lesson_routes(self) -> None:
        app = self.app
        app.add_route("GET", "/phones/lessons", self.lessons_page)
        app.add_route("POST", "/phones/lessons/send", self.lessons_send)
        app.add_route("GET", "/phones/card", self.card_page)
        app.add_route("POST", "/phones/card", self.card_start)
        app.add_route("POST", "/phones/card/stop", self.card_stop)
        app.add_route("GET", "/phones/setup-card", self.setup_card)
        self.card_job: Optional[dict] = None

    def phone_tabs(self, here: str) -> str:
        return ("<nav class='tabs' aria-label='View'>" + "".join(
            f"<a href='{href}'{' aria-current=page' if key == here else ''}>{label}</a>"
            for key, label, href in (("radio", "Radio", "/phones"), ("lessons", "Lessons", "/phones/lessons"))) + "</nav>")

    # -- what each phone has --------------------------------------------------------

    def phone_lessons(self, node: str) -> dict:
        """Which of this laptop's assignments a phone has, is being sent, or lacks."""
        out: dict = {"has": 0, "sending": [], "missing": []}
        held = {h["aid"]: h for h in self.db.all("SELECT * FROM holdings WHERE node_id = ?", (node,))}
        for a in self.db.all("SELECT id, data, version FROM assignments ORDER BY created"):
            h = held.get(a["id"])
            title = json.loads(a["data"])["title"]
            pending = h and h["msg_id"] and h["action"] in ("update", "copy") and self.messenger.message_status(h["msg_id"]) == "pending"
            if pending:   # the hub notes a version when it sends it, so check whether it has arrived
                out["sending"].append((a["id"], title))
            elif h and h["version"] == a["version"]:
                out["has"] += 1
            else:
                out["missing"].append((a["id"], title))
        out["frames"] = sum(protocol.frame_count(protocol.ASSIGN, {"a": self.packed_for_phone(aid), "o": 0}) for aid, _ in out["missing"])
        return out

    def lessons_page(self, req: Request) -> Response:
        total = self.db.one("SELECT COUNT(*) AS n FROM assignments")["n"]
        courses = len({json.loads(r["data"]).get("course") or "" for r in self.db.all("SELECT data FROM assignments")} - {""})
        notice = f"<div class='notice' role='status'>{esc(req.query['msg'])}</div>" if req.query.get("msg") else ""
        rows = ""
        for p in self.db.all("SELECT * FROM phones ORDER BY name"):
            s = self.phone_lessons(p["node_id"])
            has, behind = s["has"], len(s["missing"])
            copied = time.strftime("%d %b", time.localtime(p["copied"])).lstrip("0") if p["copied"] else ""
            card = json.loads(self.setting(f"card:{p['node_id']}") or "{}")
            line = f"{has} of {total}" + (f" · copied by Wi-Fi {copied}" if copied else "")
            fill = f"<a class='button secondary tiny' href='/phones/card?node={quote(p['node_id'])}'>Fill a memory card</a>"
            send = (f"<form method='post' action='/phones/lessons/send'><input type='hidden' name='node' value='{esc(p['node_id'])}'>"
                    f"<button class='secondary tiny'>Send by radio ({_count(s['frames'], 'packet')})</button></form>")
            if not total:
                cls, state, how, do = "", "Nothing to copy yet", "Write or add lessons first.", ""
            elif s["sending"]:
                names = ", ".join(f"“{t}”" for _, t in s["sending"][:2]) + (f" and {len(s['sending']) - 2} more" if len(s["sending"]) > 2 else "")
                cls, state, how, do = "sending", "Sending by radio", f"Sending {names}. Nothing to do.", ""
                if behind:
                    how = f"Sending {names}. {_count(behind, 'more')} to go." if behind == 1 else f"Sending {names}; {behind} more still missing."
            elif not behind:
                cls, state, how, do = "ok", "Up to date", "Nothing to do.", ""
            elif not has:
                cls, state, how, do = "behind", "Empty", "Students see “No lessons yet” until a copy arrives.", fill
            elif s["frames"] <= RADIO_MOST:
                cls, state = "behind", f"{_count(behind, 'lesson')} behind"
                how, do = "Small enough for the radio, or copy them at school.", send + fill
            else:
                cls, state = "behind", "Needs a visit"
                how, do = f"Missing {_count(behind, 'lesson')}: about {s['frames']} packets, too much for the radio.", fill
            if behind and not s["sending"]:
                titles = ", ".join(t for _, t in s["missing"][:3]) + (f" and {behind - 3} more" if behind > 3 else "")
                how += f"<div class='small'>{'Missing: ' if has else ''}{esc(titles)}</div>"
            else:
                how = esc(how)
            if card and behind:
                how += f"<div class='small'>A card was filled for it on {esc(card.get('at', ''))}; it hasn't reported those lessons yet.</div>"
            rows += (f"<div class='lessoncard {cls}'><div class='row'><span class='dot2' aria-hidden='true'></span><span class='grow'>"
                     f"<strong style='font-size:18px'>{esc(p['name'])}</strong><span class='small'>{esc(line)}</span></span><strong class='st'>{esc(state)}</strong></div>"
                     f"<div>{how}</div>" + (f"<div class='acts'>{do}</div>" if do else "") + "</div>")
        if not rows:
            rows = "<div class='panel'><strong>No phones yet.</strong><p class='muted' style='margin:4px 0 0'>They appear here once they reach this hub.</p></div>"
        address = self._lan_address(req)
        side = (
            "<section class='panel'><h2>A phone is here at school</h2><p>Put it on the school Wi-Fi. In its MeshLearn app tap "
            "<strong>Add lessons and books</strong>, then <strong>From the teacher's laptop</strong>.</p>"
            + (f"<p>The laptop's address is <strong>{esc(address)}</strong>.</p>" if address else "") + "</section>"
            "<section class='panel'><h2>A phone is far away</h2><p>Fill a memory card here and send it with someone going that way. "
            "Lessons, pictures and new books go on together.</p><a class='button secondary' href='/phones/card'>Fill a memory card</a></section>"
            "<section class='panel'><h2>By radio</h2><p>When you change an assignment, the radio sends it to phones where it is open. "
            "A whole course is too big for the radio.</p><a class='button secondary' href='/pack.zip'>Download course pack</a></section>")
        body = f"{notice}{self.phone_tabs('lessons')}<div class='cols'><div class='wide'>{rows}</div><aside class='col' style='flex:1 1 280px'>{side}</aside></div>"
        return Response(self._page("Lessons on each phone", body, current="phones",
                                   sub=f"{_count(total, 'assignment')}" + (f" in {_count(courses, 'course')}" if courses else "") + " on this laptop" if total else "No assignments yet"))

    def lessons_send(self, req: Request) -> Response:
        node = req.form.get("node", "")
        if not self.db.one("SELECT 1 FROM phones WHERE node_id = ?", (node,)):
            return redirect("/phones/lessons")
        s = self.phone_lessons(node)
        if s["frames"] > RADIO_MOST:
            return redirect("/phones/lessons?" + urlencode({"msg": "That is too much for the radio. Fill a memory card instead."}))
        for aid, _ in s["missing"]:
            held = self._holding(node, aid)
            self.set_open(aid, node, bool(held and held["open"]))   # a copy stays hidden until it is opened
        return redirect("/phones/lessons?" + urlencode({"msg": f"Sending {_count(len(s['missing']), 'lesson')}."}))

    # -- filling a card -------------------------------------------------------------

    def _cards(self) -> List[dict]:
        out = []
        for root in removable_drives():
            try:
                usage = shutil.disk_usage(root)
            except OSError:
                continue
            folder = card_folder(root)
            out.append({"root": root, "folder": folder, "free": usage.free, "total": usage.total, "used": usage.used, "known": os.path.isdir(folder)})
        return out

    def card_page(self, req: Request) -> Response:
        job, book_job = self.card_job, self.download_job
        node = req.query.get("node", "")
        phone = self.db.one("SELECT * FROM phones WHERE node_id = ?", (node,))
        name = phone["name"] if phone else "a phone"
        running = bool(job and book_job and book_job.is_alive())
        done = bool(job and not running and not req.query.get("again"))
        at = 1 if running else 2 if done and (not book_job or book_job.state == "done") else 0
        steps = "<ol class='cardsteps'>" + "".join(
            f"<li{' aria-current=step' if i == at else ''}><span>{'✓' if i < at else i + 1}</span>{t}</li>"
            for i, t in enumerate(("Choose what goes on", "Copy", "Take the card out"))) + "</ol>"
        back = "<p style='margin:0 0 8px'><a href='/phones/lessons'>Lessons on each phone</a></p>"
        if running:
            p = book_job.progress()
            pct = round(100 * p["done"] / p["total"]) if p["total"] else 0
            body = (f"{back}{steps}<meta http-equiv='refresh' content='3'><section class='panel' aria-live='polite'><strong>Copying to {esc(job['name'])}'s card</strong>"
                    f"<p>Lessons are on ({_count(job['lessons'], 'file')}). Now the books: {esc(p['current'])}</p>"
                    f"<div class='sizebar' role='progressbar' aria-valuenow='{pct}' aria-valuemin='0' aria-valuemax='100' aria-label='Copying'><span style='width:{pct}%;background:var(--accent)'></span></div>"
                    f"<p class='small'>{self._gb(p['done'])} of {self._gb(p['total'])} · {pct}%. Lessons go first, so the card is useful even if you have to stop. "
                    "You can stop and carry on later.</p><form method='post' action='/phones/card/stop'><button class='secondary'>Stop for now</button></form></section>")
            return Response(self._page(f"Fill a memory card for {job['name']}", body, current="phones"))
        if done:
            stopped = book_job is not None and job["books"] and book_job.state != "done"
            trouble = (f"<p class='bad' role='alert'>{esc(book_job.error)}</p>" if stopped and book_job.error else "")
            head = "Stopped. The lessons are on the card" if stopped else "The card is ready"
            body = (f"{back}{steps}<section class='panel'><strong style='font-size:20px'>{head}</strong>{trouble}"
                    f"<p>{_count(job['lessons'], 'lesson file')}" + (f" and {_count(len(book_job.finished), 'collection')}" if job["books"] and book_job else "")
                    + f" in <code>{esc(job['dest'])}</code>.</p><p>Eject the card in your file manager, then take it out and label it “{esc(job['name'])}”. "
                    "Back in that phone, MeshLearn's <strong>Add lessons and books</strong> screen offers what is new on the card.</p>"
                    f"<a class='button secondary' href='/phones/card?again=1&amp;node={quote(job['node'])}'>{'Carry on' if stopped else 'Fill another card'}</a></section>")
            return Response(self._page(f"Fill a memory card for {job['name']}", body, current="phones"))
        notice = f"<div class='notice warn' role='alert'>{esc(req.query['msg'])}</div>" if req.query.get("msg") else ""
        local = self._local(req)
        cards = self._cards() if local else []
        first = next((i for i, c in enumerate(cards) if c["known"]), -1)   # never pick a drive that may not be a phone's card
        drives = "".join(
            f"<label class='cardpick'><input type='radio' name='dest' value='{esc(c['folder'])}'{' checked' if i == first else ''}><span class='grow'><strong>{esc(c['root'])} · {self._gb(c['total'])}</strong>"
            f"<span class='small'>{'A MeshLearn card' if c['known'] else 'No MeshLearn folder on it yet: check that it is the right card'} · {self._gb(c['free'])} free</span></span></label>"
            for i, c in enumerate(cards))
        other = self.setting("dest")
        place = (f"{drives}<label class='cardpick'><input type='radio' name='dest' value='other'{'' if cards else ' checked'}><span class='grow'><strong>Another folder</strong>"
                 f"<input type='text' name='other' value='{esc(other)}' aria-label='Folder' placeholder='E:\\Android\\data\\org.meshlearn.phone\\files\\meshlearn'></span></label>"
                 + ("" if cards else "<p class='small'>No card found in this laptop. Put it in and reload the page, or type its MeshLearn folder.</p>"))
        files = self.course_pack_files()
        size = sum(len(v) for v in files.values())
        total = self.db.one("SELECT COUNT(*) AS n FROM assignments")["n"]
        own = len(self.own_pictures())
        mine = f", {_count(own, 'picture')} of yours" if own else ""
        items = (f"<label class='cardpick'><input type='checkbox' checked disabled><span class='grow'><strong>Lessons</strong><span class='small'>{_count(total, 'assignment')}, the class list and the words students see"
                 f"{mine} · always included</span></span><span class='small nowrap'>{max(1, round(size / 1024))} KB</span></label>")
        for i in self.selection():
            items += (f"<label class='cardpick'><input type='checkbox' name='item' value='{esc(i['key'])}' data-size='{i['size']}' checked><span class='grow'><strong>{esc(i['title'])}</strong>"
                      f"<span class='small'>{esc(i.get('label', ''))} · picked on the <a href='/library'>Library</a> page</span></span><span class='small nowrap'>{self._gb(i['size'])}</span></label>")
        if not self.selection():
            items += "<p class='small'>No books are picked. Choose some on the <a href='/library'>Library</a> page to put them on the card too.</p>"
        phones = "".join(f"<option value='{esc(p['node_id'])}'{' selected' if p['node_id'] == node else ''}>{esc(p['name'])}</option>"
                         for p in self.db.all("SELECT * FROM phones ORDER BY name"))
        meter = _js_meter(cards)
        body = (f"{notice}{back}{steps}<form method='post' action='/phones/card' class='panel'>"
                + ("" if local else "<p class='warn'>Open this page on the hub laptop itself to copy: the card goes in that laptop.</p>")
                + f"<label for='node'>Whose card is it?</label><select id='node' name='node'><option value=''>Any phone</option>{phones}</select>"
                f"<h2>The card</h2>{place}<h2>What goes on</h2>{items}{meter}"
                + ("<button>Start copying</button><p class='small'>Books need internet on this laptop; lessons don't. Files already on the card are skipped.</p>" if local else "")
                + "</form>")
        return Response(self._page(f"Fill a memory card for {name}", body, current="phones"))

    def card_start(self, req: Request) -> Response:
        node = req.form.get("node", "")
        back = "/phones/card?" + urlencode({"node": node, "again": 1}) + "&"
        if not self._local(req):
            return redirect(back + urlencode({"msg": "Start the copy from the hub laptop itself."}))
        if self.download_job and self.download_job.is_alive():
            return redirect(back + urlencode({"msg": "A copy is already running. Stop it first, or wait for it."}))
        dest = req.form.get("dest", "")
        dest = os.path.expanduser((req.form.get("other", "") if dest == "other" else dest).strip())
        if not place_exists(dest):
            return redirect(back + urlencode({"msg": f"Folder not found: {dest or 'none chosen'}. Is the card in?"}))
        wanted = set(req.form_list("item"))
        items = [i for i in self.selection() if i["key"] in wanted]
        try:
            os.makedirs(dest, exist_ok=True)
            needed, free = cat.remaining_bytes(items, dest), cat.free_bytes(dest) or 0
            if needed > free:
                return redirect(back + urlencode({"msg": f"Not enough space: need {self._gb(needed)}, the card has {self._gb(free)} free."}))
            files = self.course_pack_files()
            for rel, data in files.items():
                path = os.path.join(dest, *rel.split("/"))
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".tmp", "wb") as fh:
                    fh.write(data)
                os.replace(path + ".tmp", path)
        except OSError as exc:
            return redirect(back + urlencode({"msg": f"Could not write to the card: {exc.strerror or exc}."}))
        self.set_setting("dest", dest)
        phone = self.db.one("SELECT name FROM phones WHERE node_id = ?", (node,))
        self.card_job = {"node": node, "name": phone["name"] if phone else "a phone", "dest": dest, "lessons": len(files), "books": bool(items)}
        if phone:
            self.set_setting(f"card:{node}", json.dumps({"at": time.strftime("%d %b").lstrip("0")}))
        self.download_job = None
        if items:
            self.download_job = cat.DownloadJob(items, dest)
            self.download_job.start()
        return redirect("/phones/card?" + urlencode({"node": node}))

    def card_stop(self, req: Request) -> Response:
        if self.download_job and self._local(req):
            self.download_job.cancel()
            self.download_job.join(timeout=10)
        return redirect("/phones/card")

    # -- a printed card for setting up a phone ----------------------------------------

    def setup_card(self, req: Request) -> Response:
        transport = getattr(self.messenger, "transport", None)
        node, channel = str(getattr(transport, "node_id", "") or ""), getattr(transport, "channel", None)
        address = self._lan_address(req)
        qr = f"<div style='width:200px' data-qr='http://{esc(address)}/phone-setup'></div>" if address else ""
        body = ("<style>@media print{.side,.noprint{display:none!important}.content{padding:0}}</style>"
                "<p class='noprint'><button type='button' onclick='print()'>Print</button> <a class='button secondary' href='/phones'>Back</a></p>"
                f"<section class='panel' style='max-width:640px'><h2 style='font-size:24px;margin-top:0'>Set up a MeshLearn phone</h2><div style='display:flex;flex-wrap:wrap;gap:24px'>{qr}"
                "<ol style='flex:1 1 280px;margin:0;padding-left:20px;font-size:17px;line-height:1.7'><li>Install the MeshLearn app and open it.</li>"
                "<li>Put the phone on the school Wi-Fi and scan the code with its camera, then tap <strong>Open in the MeshLearn app</strong>.</li>"
                f"<li>Or type in the app: school radio <strong class='code'>{esc(node or '!........')}</strong>" + (f", channel <strong>{channel}</strong>" if channel is not None else "") + ".</li>"
                "<li>Pair the phone's own radio when the app asks.</li>"
                + (f"<li>Copy the lessons: <strong>From the teacher's laptop</strong>, address <strong>{esc(address)}</strong>.</li>" if address else "")
                + f"</ol></div></section><script>{QR_JS}</script>")
        return Response(self._page("Setup card", body, current="phones"))


def _js_meter(cards: List[Dict]) -> str:
    """The card's space after copying, worked out as boxes are ticked."""
    if not cards:
        return ""
    data = json.dumps({c["folder"]: [c["used"], c["total"]] for c in cards}).replace("</", "<\\/")
    return ("<div style='margin-top:12px'><div style='display:flex;justify-content:space-between'><span>Card after copying, at most</span><strong id='after'></strong></div>"
            "<span class='sizebar' id='abar'><span></span></span></div>"
            f"<script>(function(){{var D={data},f=document.querySelector('form.panel');"
            "function go(){var d=f.querySelector('[name=dest]:checked'),c=d&&D[d.value];document.getElementById('abar').parentNode.style.display=c?'':'none';if(!c)return;"
            "var n=c[0];f.querySelectorAll('[name=item]:checked').forEach(function(i){n+=+i.dataset.size});"
            "document.getElementById('after').textContent=(n/1e9).toFixed(1)+' of '+(c[1]/1e9).toFixed(1)+' GB';"
            "var b=document.getElementById('abar');b.className='sizebar'+(n>c[1]?' big':'');b.firstChild.style.width=Math.min(100,n/c[1]*100)+'%';}"
            "f.addEventListener('change',go);go();})();</script>")
