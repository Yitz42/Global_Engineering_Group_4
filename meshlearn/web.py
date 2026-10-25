"""A very small web framework on the standard library.

No third-party packages, so the phone server runs on a bare Python install
(for example Termux on Android) with nothing to download in the field.
"""

from __future__ import annotations

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
                self.send_response(response.status)
                if response.file_path:
                    size = os.path.getsize(response.file_path)
                    for key, value in response.headers.items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(size))
                    self.end_headers()
                    with open(response.file_path, "rb") as fh:
                        while True:
                            chunk = fh.read(64 * 1024)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                    return
                for key, value in response.headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(response.body)))
                self.end_headers()
                self.wfile.write(response.body)

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
.pill{display:inline-block;padding:2px 10px;border-radius:99px;border:1px solid var(--line);font-size:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.stat{font-size:28px;font-weight:700}
"""


def page(title: str, body: str, nav: str = "", site: str = "MeshLearn") -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{esc(title)} · {esc(site)}</title><style>{CSS}</style></head>"
        f"<body><header><strong>{esc(site)}</strong>{nav}</header>"
        f"<main id='main'><h1>{esc(title)}</h1>{body}</main></body></html>"
    )
