"""Teacher hub pages for pictures: browse the library, draw one, add a photo, or change one of ours.

Mixed into Hub, like the other page files. The format is in ownpics.py.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from typing import Dict, List
from urllib.parse import quote, urlencode

from . import ownpics, protocol
from . import catalog as cat
from .hubbuilder import _js
from .web import PICTURES, Request, Response, esc, json_response, picture, pictures_of, redirect

MADE = "Made by you"


class HubPictures:
    def _picture_routes(self) -> None:
        app = self.app
        app.add_route("GET", "/pictures", self.pictures_page)
        app.add_route("GET", "/pictures/add", self.pictures_add)
        app.add_route("GET", "/pictures/draw", self.pictures_draw)
        app.add_route("GET", "/pictures/buttons", lambda req: Response(self._own_buttons()))
        app.add_route("POST", "/pictures/delete", self.pictures_delete)
        app.add_route("POST", "/api/pictures", self.api_picture)

    # -- data -----------------------------------------------------------------

    def own_pictures(self) -> Dict[str, dict]:
        return {r["key"]: json.loads(r["data"]) for r in self.db.all("SELECT key, data FROM pictures ORDER BY added")}

    def save_picture(self, raw: dict) -> str:
        pic = ownpics.clean(raw, PICTURES)
        if not ownpics.whole(pic):
            raise ValueError("draw something, or choose a photo, and give it a name")
        slug = re.sub(r"[^a-z0-9]+", "-", pic["n"].lower()).strip("-")[:20] or "picture"
        key = f"my-{slug}-{secrets.token_hex(2)}"
        self.db.execute("INSERT INTO pictures (key, data, added) VALUES (?, ?, ?)", (key, json.dumps(pic, ensure_ascii=False), time.time()))
        return key

    def pictures_in(self, assignment: dict) -> List[str]:
        """The school's own pictures this assignment shows."""
        own = self.own_pictures()
        found: List[str] = []
        for q in assignment.get("questions", []):
            found += [k for k in pictures_of(q.get("pic", ""), own) + [k for k in q.get("opic") or [] if k] if k in own]
        return list(dict.fromkeys(found))

    def picture_uses(self) -> Dict[str, List[str]]:
        """Which assignments show each picture: {key: [title, …]}."""
        own, uses = self.own_pictures(), {}
        for r in self.db.all("SELECT data FROM assignments"):
            a = json.loads(r["data"])
            keys = set()
            for q in a["questions"]:
                keys.update(pictures_of(q.get("pic", ""), own))
                keys.update(k for k in q.get("opic") or [] if k)
            for k in keys:
                uses.setdefault(k, []).append(a["title"])
        return uses

    def _frames(self, pic: dict) -> int:
        return protocol.frame_count(protocol.ASSIGN, {"Px": {"my-picture-0000": ownpics.on_radio(pic)}})

    def _own_buttons(self) -> str:
        own = self.own_pictures()
        return "".join(f"<button type='button' data-pic='{k}' data-find='{esc((p['n'] + ' made by you ' + k).lower())}'>"
                       f"{picture(k, own=own)}<span>{esc(p['n'])}</span></button>" for k, p in own.items())

    def _name_fields(self) -> str:
        """A name in the school's language and the others students can switch to."""
        school = self.school_language()
        fields = (f"<label for='name'>Name in {esc(cat.language_name(school))}</label>"
                  "<input type='text' id='name' maxlength='30' placeholder='our well' required>")
        for code in self.other_languages():
            fields += (f"<label for='name-{code}'>{esc(cat.language_name(code))} (optional)</label>"
                       f"<input type='text' id='name-{code}' data-lang='{code}' maxlength='30'>")
        return fields

    # -- pages ----------------------------------------------------------------

    def pictures_page(self, req: Request) -> Response:
        own, uses = self.own_pictures(), self.picture_uses()
        items = [{"k": k, "n": p["n"], "g": MADE, "own": True, "svg": picture(k, own=own), "used": uses.get(k, []),
                  "size": "Photo · Wi-Fi or memory card only" if p.get("j") else f"About {self._frames(p)} radio packet{'' if self._frames(p) == 1 else 's'}, once",
                  "names": {cat.language_name(c): v for c, v in (p.get("l") or {}).items()}, "base": not p.get("j")} for k, p in own.items()]
        items += [{"k": k, "n": p["n"], "g": p["g"], "svg": picture(k), "used": uses.get(k, []), "base": True,
                   "size": "On every phone already: only its name travels", "names": {}} for k, p in PICTURES.items()]
        groups = ([MADE] if own else []) + list(dict.fromkeys(p["g"] for p in PICTURES.values()))
        msg = f"<div class='notice' role='status'>{esc(req.query['msg'])}</div>" if req.query.get("msg") else ""
        state = {"items": items, "groups": groups, "pick": req.query.get("pick", ""), "change": bool(req.query.get("change"))}
        body = msg + ("<div class='notice'>Pick a picture, then <strong>Change this one</strong>.</div>" if req.query.get("change") else "") + (
            PICTURES_HTML.replace("__STATE__", _js(state)))
        return Response(self._page("Pictures", body, sub=f"{len(PICTURES)} on every phone" + (f" · {len(own)} made by you" if own else ""),
                                   action="<a class='button' href='/pictures/add'>Add your own</a>"))

    def pictures_add(self, req: Request) -> Response:
        body = ADD_HTML.replace("__NAMES__", self._name_fields())
        return Response(self._page("Add your own picture", body, current="pictures", sub="Draw it, use a photo, or change one of ours"))

    def pictures_draw(self, req: Request) -> Response:
        own = self.own_pictures()
        start = own.get(req.query.get("from", ""))
        base = start.get("b", "") if start else req.query.get("from", "") if req.query.get("from") in PICTURES else ""
        name = (start["n"] if start else PICTURES[base]["n"] if base else "")
        state = {"alphabet": ownpics.ALPHABET, "colours": ownpics.COLOURS, "names": ownpics.COLOUR_NAMES, "widths": ownpics.WIDTHS,
                 "base": base, "fill": (start or {}).get("f") or (PICTURES[base].get("f", "#ffffff") if base else ""), "name": name,
                 "strokes": [[c, w, [list(p) for p in pts]] for c, w, pts in ownpics.strokes((start or {}).get("s", ""))],
                 "art": {k: PICTURES[base].get(k, "") for k in "dxs"} if base else None}
        body = DRAW_HTML.replace("__NAMES__", self._name_fields()).replace("__STATE__", _js(state))
        return Response(self._page("Draw a picture", body, current="pictures",
                                   sub="For picture answers and story tiles. Simple lines travel well over the radio."))

    def api_picture(self, req: Request) -> Response:
        try:
            raw = req.json()
            if isinstance(raw, dict) and raw.get("size"):   # how big a drawing would be, before it is saved
                pic = ownpics.clean(dict(raw, n=raw.get("n") or "picture"), PICTURES)
                return json_response({"frames": self._frames(pic) if ownpics.whole(pic) else 0, "bytes": len(json.dumps(pic)) if pic else 0})
            key = self.save_picture(raw)
        except (ValueError, TypeError, AttributeError) as exc:
            return json_response({"error": str(exc)}, status=400)
        return json_response({"key": key})

    def pictures_delete(self, req: Request) -> Response:
        key = req.form.get("key", "")
        own, used = self.own_pictures(), self.picture_uses().get(key, [])
        if key not in own:
            return redirect("/pictures")
        if used:
            return redirect("/pictures?" + urlencode({"pick": key, "msg": f"“{own[key]['n']}” is used in {', '.join(used[:3])}. Take it out of there first."}))
        self.db.execute("DELETE FROM pictures WHERE key = ?", (key,))
        return redirect("/pictures?" + urlencode({"msg": f"Deleted “{own[key]['n']}”."}))


PICTURES_HTML = r"""
<div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:12px">
<input type="text" id="pq" placeholder="Find a picture: goat, rain, cup" aria-label="Find a picture" style="flex:1 1 260px;width:auto;border-radius:22px;padding:10px 16px">
<select id="pg" aria-label="Group" style="width:auto;flex:0 1 220px"><option value="">All groups</option></select></div>
<div class="cols"><div class="wide"><div class="bigpics" id="grid"></div><p class="muted" id="none" hidden>No picture found. <a href="/pictures/draw">Draw one</a> in a minute.</p></div>
<aside class="col" style="flex:1 1 260px"><section class="panel" id="detail" style="border:2px solid var(--accent);position:sticky;top:16px"></section></aside></div>
<script>
const S = __STATE__, $ = id => document.getElementById(id);
let sel = S.items.find(p => p.k === S.pick) || S.items[0], shown = 60;
S.groups.forEach(g => $('pg').add(new Option(g, g)));
function el(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; }
function art(p) { const s = el('span', undefined, 'art'); s.innerHTML = p.svg; return s; }   // drawn by the hub from checked data
function list() { const q = $('pq').value.trim().toLowerCase(), g = $('pg').value; return S.items.filter(p => (!g || p.g === g) && (!q || (p.n + ' ' + p.g + ' ' + p.k + ' ' + Object.values(p.names).join(' ')).toLowerCase().includes(q))); }
function grid() {
  const found = list(), box = $('grid');
  box.replaceChildren(...found.slice(0, shown).map(p => { const b = el('button'); b.type = 'button'; b.setAttribute('aria-pressed', p === sel); b.append(art(p), el('span', p.n)); b.onclick = () => { sel = p; grid(); detail(); }; return b; }));
  if (found.length > shown) { const m = el('button', `Show ${found.length - shown} more`, 'more'); m.type = 'button'; m.onclick = () => { shown = 999; grid(); }; box.append(m); }
  $('none').hidden = found.length > 0;
}
function row(k, v) { const d = el('div', undefined, 'pr'); d.append(el('span', k, 'small'), el('span', v)); return d; }
function detail() {
  const d = $('detail'); d.replaceChildren(); if (!sel) return;
  const big = art(sel); big.classList.add('big');
  d.append(big, el('h2', sel.n), row('Group', sel.g));
  Object.entries(sel.names).forEach(([l, n]) => d.append(row(l, n)));
  d.append(row('Size', sel.size), row('Used in', sel.used.length ? sel.used.slice(0, 3).join(', ') + (sel.used.length > 3 ? ` and ${sel.used.length - 3} more` : '') : 'Not used yet'));
  const hear = el('button', 'Hear it', 'secondary wide'); hear.type = 'button'; hear.onclick = () => { try { speechSynthesis.cancel(); speechSynthesis.speak(new SpeechSynthesisUtterance(sel.n)); } catch (e) {} };
  const use = el('a', 'Use in a question', 'button wide'); use.href = '/assignments/new?pic=' + encodeURIComponent(sel.k);
  d.append(hear);
  if (sel.base) { const c = el('a', 'Change this one', 'button secondary wide'); c.href = '/pictures/draw?from=' + encodeURIComponent(sel.k); d.append(c); }
  d.append(use);
  if (sel.own) { const f = el('form'); f.method = 'post'; f.action = '/pictures/delete'; const i = el('input'); i.type = 'hidden'; i.name = 'key'; i.value = sel.k; const b = el('button', 'Delete', 'secondary wide'); f.append(i, b); f.onsubmit = () => confirm(`Delete “${sel.n}”?`); d.append(f); }
}
$('pq').addEventListener('input', () => { shown = 60; grid(); }); $('pg').addEventListener('change', () => { shown = 60; grid(); });
if (S.change) document.querySelector('.notice').scrollIntoView();
grid(); detail();
</script>
"""

ADD_HTML = r"""
<div class="cards">
<a class="need" href="/pictures/draw"><strong style="font-size:20px">Draw it</strong><span>Simple lines and colours. Stays tiny, so it can go by radio.</span><span class="small">Usually 1 to 4 radio packets · best choice</span><span class="go">Draw a picture</span></a>
<div class="panel"><strong style="font-size:20px">Use a photo</strong><p>From this laptop. MeshLearn shrinks it, but photos are still too big for the radio.</p><p class="small">About 15 KB after shrinking · Wi-Fi or memory card only. Until it arrives, students see its name. Ask before using a photo of a person.</p>
<label class="button secondary" for="file" style="display:inline-block">Choose a photo</label><input type="file" id="file" accept="image/*" style="position:absolute;opacity:0;width:1px"></div>
<a class="need" href="/pictures?change=1"><strong style="font-size:20px">Change one of ours</strong><span>Start from a picture in the library, then change its colour or draw on it.</span><span class="go">Browse pictures</span></a>
</div>
<section class="panel" id="photo" hidden style="margin-top:16px"><div class="cols"><div class="col" style="flex:0 1 260px"><img id="shot" alt="Your photo, shrunk" style="width:240px;height:240px;border-radius:18px;border:1px solid var(--line)"><p class="small" id="kb"></p></div>
<div class="wide"><h2>Every picture needs a name</h2><p class="small">Young students hear the name, and it shows while the picture is still on its way to a phone.</p>__NAMES__
<button type="button" id="save">Save to my picture library</button><p class="error" id="err" role="alert"></p></div></div></section>
<script>
const $ = id => document.getElementById(id); let jpeg = '';
$('file').addEventListener('change', async e => {
  const f = e.target.files[0]; $('err').textContent = ''; if (!f) return;
  try {
    const img = await createImageBitmap(f), side = Math.min(img.width, img.height), c = document.createElement('canvas');
    c.width = c.height = 240; c.getContext('2d').drawImage(img, (img.width - side) / 2, (img.height - side) / 2, side, side, 0, 0, 240, 240);
    let q = 0.75, url = c.toDataURL('image/jpeg', q);   // redrawn, so where and when it was taken is not kept
    while (url.length > 20000 && q > 0.3) { q -= 0.1; url = c.toDataURL('image/jpeg', q); }
    jpeg = url.split(',')[1]; $('shot').src = url; $('kb').textContent = Math.round(jpeg.length * 0.75 / 1024) + ' KB'; $('photo').hidden = false; $('name').focus();
  } catch (err) { alert('That file could not be read as a picture.'); }
});
$('save').addEventListener('click', async () => {
  const l = {}; document.querySelectorAll('[data-lang]').forEach(i => { if (i.value.trim()) l[i.dataset.lang] = i.value; });
  const r = await fetch('/api/pictures', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({n: $('name').value, l, j: jpeg})}), d = await r.json();
  if (!r.ok) { $('err').textContent = $('name').value.trim() ? d.error : 'Give it a name.'; return; }
  location.href = '/pictures?pick=' + d.key + '&msg=' + encodeURIComponent('Saved to your pictures.');
});
</script>
"""

DRAW_HTML = r"""
<p style="margin:0 0 12px"><a href="/pictures">All pictures</a></p>
<div class="cols"><section class="wide" aria-label="Drawing">
<div class="panel toolbar" role="toolbar" aria-label="Drawing tools" id="tools"></div>
<svg id="pad" viewBox="0 0 240 240" role="img" aria-label="Drawing area. Draw with the mouse, a finger or a pen." style="width:100%;max-width:520px;aspect-ratio:1/1;background:#fff;border:2px dashed #9a9a94;border-radius:18px;touch-action:none;cursor:crosshair"></svg>
<p class="small">Draw in the box with the mouse, a finger or a pen.</p></section>
<aside class="col" style="flex:1 1 280px">
<section class="panel"><div style="display:flex;justify-content:space-between"><strong>Radio size</strong><strong id="word"></strong></div><span class="sizebar" id="bar"><span></span></span><span class="small" id="text"></span></section>
<section class="panel"><strong>On a student's answer button</strong><div class="answerbtn"><svg id="thumb" viewBox="0 0 240 240" width="60" height="60" aria-hidden="true"></svg><strong id="label"></strong></div></section>
<section class="panel"><strong>Picture name</strong><p class="small" style="margin:2px 0 0">Read aloud to young students.</p>__NAMES__
<button type="button" id="save" class="wide">Save to my picture library</button><button type="button" id="use" class="secondary wide">Save and use in a question</button><p class="error" id="err" role="alert"></p></section>
</aside></div>
<script>
const S = __STATE__, $ = id => document.getElementById(id), NS = 'http://www.w3.org/2000/svg';
let colour = 0, width = 1, lines = S.strokes, now = null, fill = S.fill, dirty = false;
const tools = $('tools');
function btn(label, fn, cls) { const b = document.createElement('button'); b.type = 'button'; b.className = 'tool ' + (cls || ''); b.setAttribute('aria-label', label); b.onclick = fn; tools.append(b); return b; }
const cs = S.colours.map((c, i) => { const b = btn(S.names[i], () => { colour = i; mark(); }, 'round'); b.style.background = c; return b; });
const ws = S.widths.map((w, i) => { const b = btn(['Thin line', 'Medium line', 'Thick line'][i], () => { width = i; mark(); }); const d = document.createElement('i'); d.style.cssText = `width:${w + 3}px;height:${w + 3}px`; b.append(d); return b; });
if (S.base) { const l = document.createElement('label'); l.className = 'small'; l.style.cssText = 'display:flex;gap:6px;align-items:center;margin:0'; const i = document.createElement('input'); i.type = 'color'; i.value = fill; i.oninput = () => { fill = i.value; changed(); }; l.append('Colour of the picture', i); tools.append(l); }
const gap = document.createElement('span'); gap.style.flex = '1'; tools.append(gap);
btn('Undo', () => { lines.pop(); changed(); }, 'text').textContent = 'Undo'; btn('Clear', () => { lines = []; changed(); }, 'text').textContent = 'Clear';
function mark() { cs.forEach((b, i) => b.setAttribute('aria-pressed', i === colour)); ws.forEach((b, i) => b.setAttribute('aria-pressed', i === width)); }
function path(l, min) { const p = document.createElementNS(NS, 'path'); p.setAttribute('d', 'M' + l[2].map(q => q.join(' ')).join('L') + (l[2].length === 1 ? 'l.1 0' : '')); p.setAttribute('fill', 'none'); p.setAttribute('stroke', S.colours[l[0]]); p.setAttribute('stroke-width', Math.max(min || 0, S.widths[l[1]])); p.setAttribute('stroke-linecap', 'round'); p.setAttribute('stroke-linejoin', 'round'); return p; }
function paint(svg, min) {
  svg.replaceChildren();
  if (S.art) { const g = document.createElementNS(NS, 'g'); g.setAttribute('transform', 'scale(10)'); g.setAttribute('stroke', S.art.s || '#1a1a1a'); g.setAttribute('stroke-width', 1.5); g.setAttribute('stroke-linecap', 'round'); g.setAttribute('stroke-linejoin', 'round');
    [['d', fill], ['x', 'none']].forEach(([k, f]) => { if (S.art[k]) { const p = document.createElementNS(NS, 'path'); p.setAttribute('d', S.art[k]); p.setAttribute('fill', f); g.append(p); } }); svg.append(g); }
  lines.concat(now ? [now] : []).forEach(l => svg.append(path(l, min)));
}
// Fewer points along straight parts (Douglas-Peucker), so a drawing stays small.
function thin(pts, tol) {
  if (pts.length < 3) return pts;
  const [a, b] = [pts[0], pts[pts.length - 1]], dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy) || 1;
  let far = 0, at = 0;
  pts.forEach((p, i) => { const d = len === 1 && !dx && !dy ? Math.hypot(p[0] - a[0], p[1] - a[1]) : Math.abs(dy * (p[0] - a[0]) - dx * (p[1] - a[1])) / len; if (d > far) { far = d; at = i; } });
  return far <= tol ? [a, b] : thin(pts.slice(0, at + 1), tol).slice(0, -1).concat(thin(pts.slice(at), tol));
}
function point(e) { const r = $('pad').getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top].map((v, i) => Math.max(0, Math.min(240, Math.round(v / (i ? r.height : r.width) * 240)))); }
$('pad').addEventListener('pointerdown', e => { e.preventDefault(); $('pad').setPointerCapture(e.pointerId); now = [colour, width, [point(e)]]; paint($('pad')); });
$('pad').addEventListener('pointermove', e => { if (!now) return; const p = point(e), q = now[2][now[2].length - 1]; if (Math.abs(p[0] - q[0]) + Math.abs(p[1] - q[1]) < 3) return; now[2].push(p); paint($('pad')); });
['pointerup', 'pointercancel'].forEach(t => $('pad').addEventListener(t, () => { if (!now) return; now[2] = thin(now[2], 0.9); lines.push(now); now = null; changed(); }));
const steps = S.alphabet.length - 1, ch = v => S.alphabet[Math.max(0, Math.min(steps, Math.round(v * steps / 240)))];
function data() {
  const l = {}; document.querySelectorAll('[data-lang]').forEach(i => { if (i.value.trim()) l[i.dataset.lang] = i.value; });
  const out = {n: $('name').value, l, s: lines.map(t => '' + t[0] + t[1] + t[2].map(p => ch(p[0]) + ch(p[1])).join('')).join(' ')};
  if (S.base) { out.b = S.base; out.f = fill; }
  return out;
}
async function post(body) { const r = await fetch('/api/pictures', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}); return [r.ok, await r.json()]; }
let timer = null;
function changed() { dirty = true; show(); }
function show() {
  paint($('pad')); paint($('thumb'), 10); $('label').textContent = $('name').value || 'Its name';
  clearTimeout(timer); timer = setTimeout(async () => {
    const [, d] = await post(Object.assign(data(), {size: true})), n = d.frames || 0, bar = $('bar');
    $('word').textContent = !n ? '' : n <= 3 ? 'Small' : n <= 6 ? 'Medium' : 'Large'; bar.className = 'sizebar ' + (n <= 3 ? '' : n <= 6 ? 'mid' : 'big'); bar.firstChild.style.width = Math.min(100, n / 8 * 100) + '%';
    $('text').textContent = !n ? 'Nothing drawn yet.' : `About ${n} radio packet${n === 1 ? '' : 's'}.` + (n > 6 ? ' Try fewer, longer lines.' : ' Sent once with the lesson that uses it, then kept on the phone.');
  }, 400);
}
async function save(next) {
  $('err').textContent = '';
  const [ok, d] = await post(data());
  if (!ok) { $('err').textContent = !$('name').value.trim() ? 'Give it a name.' : d.error[0].toUpperCase() + d.error.slice(1) + '.'; return; }
  dirty = false; location.href = next(d.key);
}
$('save').onclick = () => save(k => '/pictures?pick=' + k + '&msg=' + encodeURIComponent('Saved to your pictures.'));
$('use').onclick = () => save(k => '/assignments/new?pic=' + k);
$('name').value = S.name; $('name').addEventListener('input', show);
window.addEventListener('beforeunload', e => { if (dirty && lines.length) e.preventDefault(); });
mark(); show();
</script>
"""
