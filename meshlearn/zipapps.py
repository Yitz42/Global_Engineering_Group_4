"""Interactive lessons on the card (PhET simulations, Blockly Games, GeoGebra), served straight from their zip files.

They are other people's programs, so each runs walled off: the browser gives it an origin of its own (a CSP sandbox), with no
cookies, no forms, and no requests to anything but its own files. It can't see who is signed in or act for them.

GeoGebra can't run with an origin of its own: it loads itself through an inner frame that it must be able to reach. So it runs
as a whole page rather than inside one of ours, with the same limits otherwise, and the sign-in cookie is hidden from scripts.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import zipfile
from typing import Optional
from urllib.parse import quote

from .web import SITE_DIR, Request, Response, esc, icon

MAX_ENTRY = 60 * 1024 * 1024
_HOST = re.compile(r"^[A-Za-z0-9.\-:\[\]]{1,100}$")
_VENDOR = re.compile(r"^[a-z_]+(/fonts)?/[A-Za-z0-9_.\-]+$")
GEOGEBRA_ZIP = "tools/geogebra.zip"
GEOGEBRA_CREDIT = "GeoGebra © GeoGebra GmbH, https://www.geogebra.org. Free for non-commercial use."
GEOGEBRA_APPS = [("graphing", "Graphing"), ("geometry", "Geometry"), ("3d", "3D"), ("scientific", "Calculator"), ("classic", "Classic")]
_TYPES = {".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8", ".html": "text/html; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml", ".wasm": "application/wasm",
          ".woff": "font/woff", ".woff2": "font/woff2", ".cur": "image/x-icon", ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".wav": "audio/wav"}


def content_type(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    return _TYPES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"


class PhoneApps:
    """Mixed into Phone."""

    def _app_routes(self) -> None:
        self.app.add_route("GET", "/library/open", self.open_app)
        self.app.add_route("GET", "/library/app/<path:path>", self.app_file)
        self.app.add_route("GET", "/site/<path:path>", self.vendor_file)
        self.app.add_route("GET", "/about", self.about_page)
        self.app.add_route("GET", "/join", self.join_page)

    def _zip_on_card(self, rel: str) -> Optional[str]:
        root = os.path.realpath(self.library_dir)
        target = os.path.realpath(os.path.join(root, rel))
        return target if target.startswith(root + os.sep) and target.lower().endswith(".zip") and os.path.isfile(target) else None

    def open_app(self, req: Request) -> Response:
        """One activity or tool, full width, with its credit beside it."""
        w = self.words(req)
        rel = req.query.get("f", "")
        item = next((i for i in self.library_items() if i["file"].replace(os.sep, "/") == rel and i.get("kind") in ("zip", "ggb")), None)
        if item is None or not self._zip_on_card(rel):
            return Response(self._page(req, w("notfound.title"), f"<p>{esc(w('library.no_file'))}</p>"), status=404)
        student = self.student(req)
        if student:
            self._remember_reading(student, rel)
        base = "/library/app/" + quote(rel)
        credit = item.get("credit", "")
        if item["kind"] == "ggb":
            show = "".join(f"<a class='card item' href='{base}/_start.html?app={k}&amp;lang={esc(w.tag)}'><span class='sub maths'>{icon('maths')}</span>"
                           f"<span class='grow'><strong>{esc(t)}</strong></span>{icon('chev', 'arr')}</a>" for k, t in GEOGEBRA_APPS)
        else:
            show = (f"<iframe class='appframe' src='{base}/index.html' sandbox='allow-scripts' allow='fullscreen' "
                    f"title='{esc(item['title'])}'></iframe>")
        body = (f"<p class='meta'><a href='/library'>{esc(w('library.back'))}</a></p>{show}"
                + (f"<p class='meta credit'>{esc(credit)}</p>" if credit else ""))
        return Response(self._page(req, item["title"], body))

    def app_file(self, req: Request) -> Response:
        path = req.params["path"]
        cut = path.lower().find(".zip/")
        card = self._zip_on_card(path[:cut + 4]) if cut > 0 else None
        if card is None:
            return Response("not found", status=404, content_type="text/plain")
        name = path[cut + 5:]
        host = req.headers.get("Host", "")
        base = f"http://{host if _HOST.match(host) else 'localhost'}/library/app/{quote(path[:cut + 4])}/"
        own = " allow-same-origin" if path[:cut + 4] == GEOGEBRA_ZIP else ""
        headers = {
            # Scripts run, in an origin of their own; nothing but this zip's files can be reached.
            "Content-Security-Policy": f"sandbox allow-scripts{own}; default-src {base} 'unsafe-inline' 'unsafe-eval' data: blob:; "
                                       "form-action 'none'; base-uri 'none'",
            "Access-Control-Allow-Origin": "*", "X-Content-Type-Options": "nosniff", "Cache-Control": "max-age=86400",
            "Referrer-Policy": "no-referrer"}
        if name == "_start.html":
            return Response(_geogebra_page(req.query.get("app", ""), req.query.get("lang", ""), self.words(req)("library.back")), headers=headers)
        try:
            with zipfile.ZipFile(card) as z:
                info = z.getinfo(name)
                if info.is_dir() or info.file_size > MAX_ENTRY:
                    raise KeyError(name)
                data = z.read(info)
        except (KeyError, OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError):
            return Response("not found", status=404, content_type="text/plain")
        kind = content_type(name)
        if kind.startswith("text/html") and not own:
            data = add_shim(data)
        return Response(data, content_type=kind, headers=headers)

    def vendor_file(self, req: Request) -> Response:
        """Ready-made libraries the student site uses (site/vendor)."""
        return vendor_response(req.params["path"])

    def about_page(self, req: Request) -> Response:
        w = self.words(req)
        rows = "".join(f"<li><strong>{esc(n)}</strong> <span class='meta'>{esc(t)}</span></li>" for n, t in ABOUT)
        return Response(self._page(req, w("about.title"), f"<ul class='units card'>{rows}</ul>"))

    def join_page(self, req: Request) -> Response:
        """A QR code for this phone's student site, for the app's "How students join" screen."""
        w = self.words(req)
        url = req.query.get("u", "")
        if not re.match(r"^http://[A-Za-z0-9.\-:]{3,60}/?$", url):
            url = "http://" + (req.headers.get("Host", "") if _HOST.match(req.headers.get("Host", "")) else "192.168.43.1:8080")
        return Response(self._page(req, w("join.title"), f"<div class='qrbox'><div data-qr='{esc(url)}'></div><p class='grade'>{esc(url[7:])}</p></div>"
                                   f"<p>{esc(w('join.text'))}</p><script>{QR_JS}</script>"))


ABOUT = [("KaTeX", "MIT · katex.org"), ("Signature Pad", "MIT · Szymon Nowak"), ("OpenDyslexic", "SIL Open Font License · Abbie Gonzalez"),
         ("qrcode-generator", "MIT · Kazuhiko Arase"), ("Papa Parse", "MIT · Matthew Holt"),
         ("PhET", "Simulation by PhET Interactive Simulations, University of Colorado Boulder, licensed under CC BY 4.0 (https://phet.colorado.edu)."),
         ("Blockly Games", "Google · public domain"), ("GeoGebra", GEOGEBRA_CREDIT), ("Kolibri", "Learning Equality · learningequality.org")]


def _site_text(name: str) -> str:
    with open(os.path.join(SITE_DIR, name), encoding="utf-8") as fh:
        return fh.read()


APPSHIM_JS = _site_text("appshim.js")
QR_JS = _site_text("qr.js")


def add_shim(page: bytes) -> bytes:
    """Give the page storage that lasts while it is open: a walled-off page has none of its own."""
    tag = b"<script>" + APPSHIM_JS.encode("utf-8") + b"</script>"
    m = re.search(rb"<head[^>]*>", page[:20000], re.I)
    return page[:m.end()] + tag + page[m.end():] if m else tag + page


def _geogebra_page(app: str, lang: str, back: str) -> str:
    app = app if app in dict(GEOGEBRA_APPS) else "graphing"
    lang = lang if re.match(r"^[A-Za-z-]{2,8}$", lang or "") else "en"
    params = {"appName": app, "showToolBar": app != "scientific", "showAlgebraInput": True, "showMenuBar": False,
              "language": lang.split("-")[0], "enableFileFeatures": False, "preventFocus": True}
    return ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>GeoGebra</title><style>html,body{margin:0;height:100%;overflow:hidden;font:15px system-ui,sans-serif}"
            ".top{display:flex;gap:12px;align-items:center;height:44px;padding:0 12px;background:#1f5fbf}.top a{color:#fff;font-weight:700}"
            ".foot{height:26px;padding:0 12px;font-size:12px;line-height:26px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}</style></head>"
            f"<body><div class='top'><a href='/library'>{esc(back)}</a></div><div id='ggb'></div><div class='foot'>{esc(GEOGEBRA_CREDIT)}</div>"
            "<script src='GeoGebra/deployggb.js'></script><script>"
            f"var p={json.dumps(params)};function h(){{return Math.max(200,window.innerHeight-70)}}p.width=window.innerWidth;p.height=h();"
            "var a=new GGBApplet(p,true);a.setHTML5Codebase('GeoGebra/HTML5/5.0/web3d/');a.inject('ggb');"
            "window.addEventListener('resize',function(){try{ggbApplet.setSize(window.innerWidth,h())}catch(e){}});"
            "</script></body></html>")


def vendor_response(path: str) -> Response:
    full = os.path.realpath(os.path.join(SITE_DIR, "vendor", path.replace("vendor/", "", 1)))
    root = os.path.realpath(os.path.join(SITE_DIR, "vendor"))
    if not path.startswith("vendor/") or not _VENDOR.match(path[7:]) or not full.startswith(root + os.sep) or not os.path.isfile(full):
        return Response("not found", status=404, content_type="text/plain")
    with open(full, "rb") as fh:
        return Response(fh.read(), content_type=content_type(full), headers={"Cache-Control": "max-age=86400"})
