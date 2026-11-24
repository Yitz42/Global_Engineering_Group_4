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
        self.headers["Set-Cookie"] = f"{name}={quote(value)}; Path=/; Max-Age={max_age}; SameSite=Lax"
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
# cards that open, tabs that show where you are. Kept identical to STUDENT_CSS in the Kotlin phone (Web.kt).
STUDENT_CSS = """
body{font-size:18px}
header{position:sticky;top:0;z-index:5;flex-direction:column;align-items:stretch;gap:8px;padding:10px 12px}
header strong{font-size:16px;margin:0;display:flex;justify-content:space-between;gap:8px}
.who{font-weight:400;color:var(--muted);font-size:15px}
nav.tabs{display:flex;gap:6px}
nav.tabs a{flex:1;display:flex;align-items:center;justify-content:center;white-space:nowrap;padding:9px 4px;border-radius:10px;border:1px solid var(--line);font-size:16px;color:var(--accent);text-decoration:none}
nav.tabs a[aria-current=page]{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
main{padding:12px 14px 32px}
h1{font-size:24px;margin:6px 0 10px}
.lead{font-size:19px;margin:0 0 14px}
a.card{display:block;color:var(--ink);text-decoration:none}
a.card:hover,a.card:focus{border-color:var(--accent)}
a.card h2,a.card h3{color:var(--accent);margin:0 0 6px}
a.card h3{font-size:19px}
.meta{color:var(--muted);font-size:15px;margin:4px 0}
.due{font-size:15px;font-weight:700;margin:6px 0}
.due.late{color:var(--bad)} .due.soon{color:var(--warn)} .due.done{color:var(--muted);font-weight:400}
.go{display:block;text-align:center;margin:12px 0 0}
.bar{height:10px;border-radius:99px;background:var(--line);overflow:hidden;margin:10px 0 4px}
.bar span{display:block;height:100%;background:var(--good)}
fieldset.q{background:var(--card);margin:14px 0;padding:14px}
fieldset.q legend{float:left;width:100%;padding:0;margin:0 0 10px;font-size:19px;line-height:1.35}
fieldset.q legend+*{clear:both}
.qnum{display:block;color:var(--muted);font-size:14px;font-weight:600;margin-bottom:2px}
.q label:not(.option){font-weight:400;font-size:15px;color:var(--muted);margin:4px 0}
.option{border:1px solid var(--line);border-radius:10px;padding:12px;margin:8px 0;min-height:52px;font-size:18px;color:var(--ink);cursor:pointer}
.option.on{border-color:var(--accent);outline:2px solid var(--accent)}
.q.bad{border-color:var(--bad)}
.counter{font-size:14px;color:var(--muted);text-align:right;margin:4px 0 0}
.submitbar{position:sticky;bottom:0;background:var(--bg);border-top:1px solid var(--line);padding:10px 0;margin-top:12px}
.submitbar .meta{margin:0 0 6px}
button.big,.button.big{display:block;width:100%;text-align:center;padding:15px;font-size:19px;margin:8px 0 0}
.grade{font-size:22px;font-weight:700;margin:6px 0}
.units{list-style:none;padding:0;margin:8px 0 0}
.units li{padding:8px 0;border-top:1px solid var(--line)}
a.item{padding:12px 14px;margin:8px 0}
a.item strong{color:var(--accent)}
form.search{display:flex;gap:8px;align-items:center;margin:0 0 8px}
form.search input{flex:1}
form.search button{margin:0}
form.lang{align-self:flex-end;margin:0}
form.lang button{margin:0;padding:6px 14px;font-size:15px;background:transparent;color:var(--accent);border:1px solid var(--line)}
[dir=rtl] fieldset.q legend{float:right}
[dir=rtl] .counter{text-align:left}
[dir=rtl] button,[dir=rtl] .button{margin:8px 0 0 8px}
[dir=rtl] button.big,[dir=rtl] .button.big,[dir=rtl] form.search button,[dir=rtl] form.lang button{margin:8px 0 0}
[dir=rtl] th,[dir=rtl] td{text-align:right}
.week{margin:0 0 14px}
.week .wk{display:flex;justify-content:space-between;gap:8px;font-size:16px}
.week .bar{height:14px;margin:6px 0 0}
.next{background:var(--accent);color:var(--accent-ink);border-radius:22px;padding:20px;margin:0 0 16px}
.next .label{font-size:15px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;opacity:.85}
.next h2{margin:8px 0 6px;font-size:23px;line-height:1.25;color:inherit}
.next .meta{color:inherit;opacity:.9}
.next .due{color:inherit}
.next .button{display:block;text-align:center;background:var(--card);color:var(--accent);font-size:20px;padding:16px;border-radius:16px;margin:14px 0 0}
.row{display:flex;align-items:center;gap:12px;background:var(--card);border:2px solid var(--line);border-radius:18px;padding:12px 14px;margin:8px 0;color:var(--ink);text-decoration:none}
.row.wait{border-style:dashed;background:transparent}
.row .grow{flex:1;min-width:0;display:flex;flex-direction:column}
.row .meta{margin:0}
.row .end{font-weight:700;text-align:right}
.row .end.small{font-weight:400;font-size:15px;color:var(--muted)}
.chev{color:var(--muted);font-size:22px}
.squares{display:grid;grid-template-columns:repeat(auto-fill,minmax(40px,1fr));gap:6px;margin:10px 0}
.squares span{display:flex;align-items:center;justify-content:center;height:40px;border-radius:10px;font-weight:800}
.squares .right{background:var(--good);color:var(--card)}
.squares .part{background:var(--warn);color:var(--card)}
.squares .wrong{border:3px solid var(--bad);color:var(--bad)}
.squares .wait{border:2px dashed var(--line);color:var(--muted)}
.new{display:inline-block;padding:2px 10px;border-radius:99px;background:var(--accent);color:var(--accent-ink);font-size:14px;font-weight:800}
.bigscore{font-size:24px;font-weight:800;margin:6px 0}
.missed{background:var(--bg);border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin:10px 0 0}
.missed strong{display:block}
.sendin{font-weight:700}
"""

# Counts down to when the phone sends the student's work by radio (data-in seconds from now), then
# says it is on its way. Kept identical to COUNTDOWN_JS in the Kotlin phone (Web.kt). Plain ES5.
COUNTDOWN_JS = r"""
(function(){var el=document.getElementById('sendin');if(!el)return;
var left=parseInt(el.getAttribute('data-in'),10)||0,t0=new Date().getTime();
function tick(){var s=Math.max(0,Math.round(left-(new Date().getTime()-t0)/1000));
if(s<=0){el.textContent=el.getAttribute('data-now');return;}
var m=Math.floor(s/60),r=s%60;el.textContent=el.getAttribute('data-words').replace('{time}',m+':'+(r<10?'0':'')+r);
setTimeout(tick,1000);}tick();})();
"""

# Keeps a student's answers in the browser while they work (so a closed tab or a flat battery loses
# nothing), counts what is answered, and shows which choice is picked. Plain ES5 for old phones.
ASSIGNMENT_JS = r"""
(function(){var f=document.getElementById('aform');if(!f)return;var key='meshlearn-draft:'+f.getAttribute('data-key');
var store=null;try{store=window.localStorage;}catch(e){}
function all(){return f.querySelectorAll('input[type=text],input[type=radio],textarea');}
function save(){if(!store)return;var d={},els=all();for(var i=0;i<els.length;i++){var el=els[i];
if(el.type==='radio'){if(el.checked)d[el.name]=el.value;}else if(el.value)d[el.name]=el.value;}
try{store.setItem(key,JSON.stringify(d));}catch(e){}}
function restore(){if(!store)return;var d;try{d=JSON.parse(store.getItem(key)||'{}')||{};}catch(e){d={};}
var els=all();for(var i=0;i<els.length;i++){var el=els[i];if(!d.hasOwnProperty(el.name))continue;
if(el.type==='radio'){if(!f.querySelector('input[name="'+el.name+'"]:checked')&&el.value===d[el.name])el.checked=true;}
else if(!el.value)el.value=d[el.name];}}
function update(){var qs=f.querySelectorAll('fieldset.q'),done=0;for(var i=0;i<qs.length;i++){
var q=qs[i],ok=false,els=q.querySelectorAll('input,textarea');for(var j=0;j<els.length;j++){var el=els[j];
if(el.type==='radio'){var lab=el.parentNode;if(el.checked){ok=true;lab.className='option on';}else lab.className='option';}
else if(el.value.replace(/\s/g,''))ok=true;
if(el.tagName==='TEXTAREA'){var c=document.getElementById(el.id+'-count');if(c)c.innerHTML=el.value.length+' / '+el.getAttribute('maxlength');}}
if(ok)done++;}var a=document.getElementById('answered');
if(a)a.textContent=(a.getAttribute('data-words')||'{done} of {total} answered').replace('{done}',done).replace('{total}',qs.length);}
restore();update();f.addEventListener('input',function(){save();update();});f.addEventListener('change',function(){save();update();});
var bad=f.querySelector('[aria-invalid=true]');if(bad)bad.focus();})();
"""

# Forgets a saved draft once the work is safely turned in.
CLEAR_DRAFT_JS = ("(function(){var el=document.getElementById('clear-draft');if(!el)return;"
                  "try{window.localStorage.removeItem('meshlearn-draft:'+el.getAttribute('data-key'));}catch(e){}})();")

_EXPECTED_S = {"choice": 30, "number": 60, "text": 30, "long": 180}  # as in comprehension.py


def minutes_for(questions, words: Strings = ENGLISH) -> str:
    """'about 5 min' for a list of questions."""
    seconds = sum(_EXPECTED_S.get(q.get("type"), 45) for q in questions)
    return words("time.about_min", n=max(1, int(seconds / 60 + 0.5)))


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
         lang: str = "en", direction: str = "ltr") -> str:
    attrs = f"lang='{esc(lang)}'" + (" dir='rtl'" if direction == "rtl" else "")
    return (
        f"<!doctype html><html {attrs}><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)} · {esc(site)}</title><style>{CSS}{extra_css}</style></head>"
        f"<body><header><strong>{esc(site)}</strong>{nav}</header>"
        f"<main id='main'{' class=wide' if wide else ''}><h1>{esc(title)}</h1>{body}</main></body></html>"
    )
