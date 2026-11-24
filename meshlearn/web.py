"""A very small web framework on the standard library.

No third-party packages, so the phone server runs on a bare Python install
(for example Termux on Android) with nothing to download in the field.
"""

from __future__ import annotations

import datetime
import html
import json
import logging
import mimetypes
import os
import re
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse

from .language import ENGLISH, Strings

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024

esc = html.escape


class Request:
    def __init__(self, method: str, path: str, query: Dict[str, str], headers, body: bytes, client: str):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.client = client
        self.params: Dict[str, str] = {}
        self._form: Optional[Dict[str, str]] = None
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("Cookie", ""))
        except Exception:
            pass
        self.cookies = {k: unquote(v.value) for k, v in cookie.items()}

    @property
    def form(self) -> Dict[str, str]:
        if self._form is None:
            parsed = parse_qs(self.body.decode("utf-8", "replace"), keep_blank_values=True)
            self._form = {k: v[-1] for k, v in parsed.items()}
        return self._form

    def form_list(self, name: str) -> List[str]:
        return parse_qs(self.body.decode("utf-8", "replace"), keep_blank_values=True).get(name, [])

    def json(self):
        return json.loads(self.body.decode("utf-8") or "null")


class Response:
    def __init__(self, body=b"", status: int = 200, content_type: str = "text/html; charset=utf-8", headers=None):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.headers.update(headers or {})
        self.file_path: Optional[str] = None

    def set_cookie(self, name: str, value: str, max_age: int = 60 * 60 * 24 * 180) -> "Response":
        self.headers["Set-Cookie"] = f"{name}={quote(value)}; Path=/; Max-Age={max_age}; SameSite=Lax; HttpOnly"
        return self


def redirect(location: str) -> Response:
    return Response(b"", status=303, headers={"Location": location})


def json_response(data, status: int = 200) -> Response:
    return Response(json.dumps(data), status=status, content_type="application/json")


def file_response(path: str) -> Response:
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if content_type.startswith("text/"):
        content_type += "; charset=utf-8"
    resp = Response(b"", content_type=content_type)
    resp.file_path = path
    return resp


def byte_range(header: str, size: int) -> Optional[Tuple[int, int]]:
    """First and last byte a "Range: bytes=..." header asks for (what video players send to start or skip
    ahead), or None to send the whole file."""
    m = re.match(r"^bytes=(\d*)-(\d*)$", (header or "").strip())
    if not m or not (m.group(1) or m.group(2)):
        return None
    if m.group(1):
        start, end = int(m.group(1)), min(int(m.group(2) or size - 1), size - 1)
    else:
        start, end = max(size - int(m.group(2)), 0), size - 1
    return (start, end) if start <= end else None


Handler = Callable[[Request], Response]


class App:
    def __init__(self, name: str, max_body: int = MAX_BODY_BYTES):
        self.name = name
        self.max_body = max_body
        self._routes: List[Tuple[str, re.Pattern, Handler]] = []
        self._server: Optional[ThreadingHTTPServer] = None

    def route(self, method: str, pattern: str):
        # "<name>" matches one path segment; "<name:path>" matches the rest of the path.
        converted = re.sub(
            r"<(\w+)(:path)?>",
            lambda m: f"(?P<{m.group(1)}>{'.+' if m.group(2) else '[^/]+'})",
            pattern,
        )
        regex = re.compile("^" + converted + "$")

        def decorator(fn: Handler) -> Handler:
            self._routes.append((method, regex, fn))
            return fn

        return decorator

    def add_route(self, method: str, pattern: str, fn: Handler) -> None:
        self.route(method, pattern)(fn)

    def dispatch(self, request: Request) -> Response:
        if request.method == "POST" and request.headers.get("Origin", "") == "null":
            # A walled-off activity (zipapps.py) must not be able to press our buttons.
            return Response("Not allowed", status=403, content_type="text/plain")
        path_matched = False
        for method, regex, fn in self._routes:
            match = regex.match(request.path)
            if not match:
                continue
            path_matched = True
            if method != request.method:
                continue
            request.params = {k: unquote(v) for k, v in match.groupdict().items()}
            return fn(request)
        if path_matched:
            return Response("Method not allowed", status=405, content_type="text/plain")
        return Response(page("Not found", "<p>That page does not exist.</p><p><a href='/'>Go home</a></p>"), status=404)

    def make_server(self, host: str, port: int) -> ThreadingHTTPServer:
        app = self

        class _Handler(BaseHTTPRequestHandler):
            server_version = "MeshLearn"

            def _handle(self):
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                if length > app.max_body:
                    self.send_error(413, "Request too large")
                    return
                body = self.rfile.read(length) if length else b""
                query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
                request = Request(self.command, parsed.path, query, self.headers, body, self.client_address[0])
                try:
                    response = app.dispatch(request)
                except Exception:
                    log.exception("error handling %s %s", self.command, self.path)
                    response = Response(page("Something went wrong", "<p>Sorry, that did not work. Please try again.</p>"), status=500)
                if response.file_path:
                    self._send_file(response)
                    return
                self.send_response(response.status)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(response.body)))
                self.end_headers()
                self.wfile.write(response.body)

            def _send_file(self, response):
                size = os.path.getsize(response.file_path)
                span = byte_range(self.headers.get("Range", ""), size)
                start, end = span or (0, size - 1)
                self.send_response(206 if span else response.status)
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.send_header("Accept-Ranges", "bytes")
                if span:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                try:
                    with open(response.file_path, "rb") as fh:
                        fh.seek(start)
                        left = end - start + 1
                        while left > 0:
                            chunk = fh.read(min(64 * 1024, left))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                except ConnectionError:   # a video player stops a download whenever the student skips ahead
                    self.close_connection = True

            do_GET = _handle
            do_POST = _handle

            def log_message(self, fmt, *args):
                log.debug("%s %s", self.address_string(), fmt % args)

        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._server.daemon_threads = True
        return self._server

    def serve_in_thread(self, host: str, port: int) -> ThreadingHTTPServer:
        server = self.make_server(host, port)
        threading.Thread(target=server.serve_forever, name=f"http-{self.name}", daemon=True).start()
        return server


SITE_DIR = os.path.join(os.path.dirname(__file__), "site")


def _site(name: str) -> str:
    """A file of the student site's look or scripts, shared with the Kotlin phone."""
    with open(os.path.join(SITE_DIR, name), encoding="utf-8") as fh:
        return fh.read()


ICONS: Dict[str, str] = json.loads(_site("icons.json"))


def icon(name: str, cls: str = "") -> str:
    """A line icon from the designs (site/icons.json). Decorative: the words beside it carry the meaning."""
    return (f"<svg class='i{' ' + cls if cls else ''}' viewBox='0 0 24 24' aria-hidden='true'>"
            f"<path d='{ICONS[name]}'/></svg>")


PICTURES: Dict[str, dict] = json.loads(_site("pictures.json"))   # drawings for young learners, one set on every phone
MAX_PICS = 12


def picture(key: str, cls: str = "") -> str:
    """One drawing from the picture library, or '' when this version doesn't have it."""
    p = PICTURES.get(key)
    if not p:
        return ""
    paths = (f"<path d='{p['d']}' fill='{p['f']}'/>" if p.get("d") else "") + (f"<path d='{p['x']}' fill='none'/>" if p.get("x") else "")
    return f"<svg class='pic{' ' + cls if cls else ''}' viewBox='0 0 24 24' stroke='{p.get('s', '#1a1a1a')}' aria-hidden='true'>{paths}</svg>"


def pictures_of(spec: str) -> List[str]:
    """The pictures a question shows: "apple*3, ball" -> apple, apple, apple, ball (at most MAX_PICS)."""
    out: List[str] = []
    for part in (spec or "").split(","):
        key, _, count = part.strip().partition("*")
        if key in PICTURES:
            out += [key] * max(1, min(int(count) if count.strip().isdigit() else 1, MAX_PICS))
    return out[:MAX_PICS]


def pictures_html(spec: str) -> str:
    """A question's pictures in a frame. Read aloud as the names of what is shown, never how many."""
    keys = pictures_of(spec)
    if not keys:
        return ""
    names = ", ".join(dict.fromkeys(PICTURES[k]["n"] for k in keys))
    return f"<div class='pics n{len(keys)}' role='img' aria-label='{esc(names)}'>{''.join(picture(k) for k in keys)}</div>"


CSS = """
:root{--bg:#f7f7f4;--card:#fff;--ink:#1d1d1b;--muted:#5b5b57;--line:#d9d9d3;--accent:#1f5fbf;--accent-ink:#fff;--good:#1d7a3a;--bad:#b3261e;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#22221f;--ink:#efefea;--muted:#b4b4ad;--line:#3a3a36;--accent:#7fb0ff;--accent-ink:#0b1a33;--good:#6fcf8a;--bad:#ff8a80;--warn:#f0c060}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}
header strong{font-size:18px;margin-right:auto}
header a{color:var(--accent);text-decoration:none;font-weight:600}
main{max-width:860px;margin:0 auto;padding:16px}
main.wide{max-width:1160px}
h1{font-size:26px;line-height:1.25;margin:8px 0 16px}
h2{font-size:20px;margin:24px 0 8px}
a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0}
.muted{color:var(--muted)}
.good{color:var(--good)} .bad{color:var(--bad)} .warn{color:var(--warn)}
label{display:block;font-weight:600;margin:12px 0 4px}
input[type=text],input[type=number],input[type=date],textarea,select{width:100%;font:inherit;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
textarea{min-height:120px}
fieldset{border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:16px 0}
legend{font-weight:700;padding:0 6px}
.option{display:flex;gap:10px;align-items:center;padding:8px 0;font-weight:400}
.option input{width:22px;height:22px}
button,.button{display:inline-block;font:inherit;font-weight:700;padding:12px 18px;border-radius:8px;border:0;background:var(--accent);color:var(--accent-ink);text-decoration:none;cursor:pointer;margin:8px 8px 0 0}
button.secondary,.button.secondary{background:transparent;color:var(--accent);border:1px solid var(--accent)}
table{width:100%;border-collapse:collapse;margin:8px 0}
th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
.error{color:var(--bad);font-weight:600;margin:4px 0}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;border:1px solid var(--line);font-size:14px;white-space:nowrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr));gap:12px}
.stat{font-size:28px;font-weight:700}
"""


# The student site's look, on top of CSS: made for a phone held in one hand. Big tap targets, whole
# cards that open, tabs that show where you are. student.css is the base and looks.css the three looks by
# age. The Kotlin phone (Web.kt) reads the same files from meshlearn/site.
STUDENT_CSS = _site("student.css") + _site("looks.css")

# Counts down to when the phone sends the student's work by radio (data-in seconds from now), then
# says it is on its way. Plain ES5.
COUNTDOWN_JS = _site("countdown.js")

# One question at a time, with arrows and a review page; without it, every question shows in one list.
# Keeps a student's answers and place in the browser while they work (so a closed tab or a flat battery
# loses nothing), counts what is answered, and shows which choice is picked. Question info pulls down on
# a phone and stays open at the side on a wide screen. Plain ES5 for old phones.
ASSIGNMENT_JS = _site("assignment.js")

LISTEN_JS = _site("listen.js")   # reads the page or the question aloud, where the device has a voice for the language
SIGNIN_JS = _site("signin.js")   # finds a name in the class list, and offers whoever was last on this phone
IDLE_JS = _site("idle.js")       # goes back to "Who are you?" when nobody taps for a while
MATH_JS = _site("math.js")         # equations (KaTeX), only on pages that have some
READING_JS = _site("reading.js")   # bigger text, easy-to-read letters, more space
SCRATCH_JS = _site("scratch.js")   # a scratchpad per question; stays in the browser
RESUME_JS = _site("resume.js")   # "Keep going" instead of "Start" on work this student has begun

# Forgets a saved draft once the work is safely turned in.
CLEAR_DRAFT_JS = ("(function(){var el=document.getElementById('clear-draft');if(!el)return;"
                  "try{window.localStorage.removeItem('meshlearn-draft:'+el.getAttribute('data-key'));}catch(e){}})();")

_EXPECTED_S = {"choice": 30, "number": 60, "text": 30, "long": 180}  # as in comprehension.py


def minutes_for(questions, words: Strings = ENGLISH) -> str:
    """'about 5 min' for a list of questions."""
    seconds = sum(_EXPECTED_S.get(q.get("type"), 45) for q in questions)
    return words("time.about_min", n=max(1, int(seconds / 60 + 0.5)))


def teacher_pics(q: dict) -> str:
    keys = pictures_of(q.get("pic", "")) + [k for k in q.get("opic", []) if k in PICTURES]
    return f"<div>{''.join(picture(k, 'mini') for k in keys)}</div>" if keys else ""


def info_panel(q: dict) -> str:
    """A question's info, folded away, for the teacher's pages."""
    if not q.get("info"):
        return teacher_pics(q)
    return teacher_pics(q) + (f"<details class='muted' style='margin:4px 0'><summary>Question info</summary>"
            f"<p style='white-space:pre-line;margin:4px 0'>{esc(q['info'])}</p></details>")


def due_label(due: str, turned_in: bool, today: Optional[datetime.date] = None,
              words: Strings = ENGLISH) -> Tuple[str, str]:
    """A due date in words and its style, e.g. ('Due tomorrow', 'soon') or ('Overdue · was due Thu, Oct 16', 'late').
    Empty when there is no date. Work already turned in is never overdue."""
    if not due or not due.strip():
        return "", ""
    try:
        when = datetime.date.fromisoformat(due.strip()[:10])
    except ValueError:
        return words("due.on", date=due), ""
    nice = words.date(when)
    days = (when - (today or datetime.date.today())).days
    if turned_in:
        return words("due.was", date=nice), "done"
    if days < 0:
        return words("due.overdue", date=nice), "late"
    if days == 0:
        return words("due.today"), "soon"
    if days == 1:
        return words("due.tomorrow"), "soon"
    return words("due.on", date=nice), ""


def page(title: str, body: str, nav: str = "", site: str = "MeshLearn", wide: bool = False, extra_css: str = "",
         lang: str = "en", direction: str = "ltr", body_cls: str = "", before: str = "", after: str = "") -> str:
    attrs = f"lang='{esc(lang)}'" + (" dir='rtl'" if direction == "rtl" else "")
    return (
        f"<!doctype html><html {attrs}><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)} · {esc(site)}</title><style>{CSS}{extra_css}</style></head>"
        f"<body{f' class={chr(39)}{body_cls}{chr(39)}' if body_cls else ''}><header><strong>{esc(site)}</strong>{nav}</header>"
        f"<main id='main'{' class=wide' if wide else ''}>"
        + (f"<div class='titlebar'>{before}<h1>{esc(title)}</h1>{after}</div>" if body_cls else f"<h1>{esc(title)}</h1>")
        + f"{body}</main></body></html>"
    )
