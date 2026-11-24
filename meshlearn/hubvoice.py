"""Teacher hub page for recording the teacher's voice (the "Record your voice" design).

A recording belongs to the words that were said in one language (voices.py), not to one question: the same word in
another question, or another assignment, uses it again, and words that change need a new one. Recordings go to phones over Wi-Fi or on the
memory card, never by radio. Mixed into Hub, like the other page files.
"""

from __future__ import annotations

import os
import time
from typing import Dict, List
from urllib.parse import quote

from . import assignments as asg
from . import catalog as cat
from . import voices
from .hubbuilder import _js
from .web import Request, Response, esc, json_response, redirect


class HubVoice:
    def _voice_routes(self) -> None:
        app = self.app
        app.add_route("GET", "/assignments/<aid>/voice", self.voice_page)
        app.add_route("POST", "/api/voice/<key>", self.voice_save)
        app.add_route("POST", "/api/voice/<key>/delete", self.voice_delete)
        app.add_route("GET", "/voice/<key>", lambda req: voices.response(self.voice_files(), req.params["key"]))

    def voice_files(self) -> Dict[str, str]:
        return voices.folder_files(self.voice_dir)

    def voice_lengths(self) -> Dict[str, float]:
        return {r["key"]: r["secs"] for r in self.db.all("SELECT key, secs FROM voices")}

    def spoken_words(self) -> Dict[str, int]:
        """Every text a phone could say, by key, and how many assignments say it."""
        found: Dict[str, int] = {}
        for r in self.db.all("SELECT id FROM assignments"):
            a = self.get_assignment(r["id"])
            codes = dict.fromkeys([self.written_in(a)] + self.class_codes())
            keys = {voices.key(t, code) for code in codes for t, _ in voices.texts(a if code == self.written_in(a) else asg.localized(a, code))}
            for k in keys:
                found[k] = found.get(k, 0) + 1
        return found

    def used_voices(self) -> Dict[str, str]:
        """The recordings phones need: those whose words are still in an assignment."""
        words = self.spoken_words()
        return {k: path for k, path in self.voice_files().items() if k in words}

    def voice_list(self) -> Dict[str, list]:
        return {k: [os.path.splitext(path)[1][1:], os.path.getsize(path)] for k, path in self.used_voices().items()}

    # -- the page -----------------------------------------------------------------

    def voice_page(self, req: Request) -> Response:
        a = self.get_assignment(req.params["aid"])
        if a is None:
            return Response(self._page("Not found", "<p>No such assignment.</p>"), status=404)
        base = self.written_in(a)
        codes = [base] + self.translate_to(a)
        code = req.query.get("lang") if req.query.get("lang") in codes else base
        view = a if code == base else asg.localized(a, code)
        original = {t for t, _ in voices.texts(a)}
        files, lengths, words = self.voice_files(), self.voice_lengths(), self.spoken_words()
        rows: List[dict] = []
        for text, what in voices.texts(view):
            k = voices.key(text, code)
            if any(r["k"] == k for r in rows):
                continue
            rows.append({"k": k, "t": text, "what": what, "have": k in files, "secs": lengths.get(k, 0),
                         "other": words.get(k, 1) - 1, "bytes": os.path.getsize(files[k]) if k in files else 0,
                         "untranslated": code != base and text in original and not text.strip().isdigit()})
        tabs = "".join(f"<a href='/assignments/{esc(a['id'])}/voice?lang={c}'{' aria-current=page' if c == code else ''}>{esc(cat.language_name(c))}</a>" for c in codes)
        missing = sum(1 for r in rows if r["untranslated"])
        note = (f"<div class='notice warn'>{missing} of these {'is' if missing == 1 else 'are'} not translated into {esc(cat.language_name(code))} yet, so students see "
                f"{esc(cat.language_name(base))}. <a href='/assignments/{esc(a['id'])}/languages'>Translate first</a>, then record.</div>" if missing else "")
        total = sum(os.path.getsize(p) for p in self.used_voices().values())
        state = {"rows": rows, "most": voices.MAX_BYTES, "secs": voices.MAX_SECONDS, "total": total}
        pack = self.language_pack(code)
        body = (("<nav class='tabs' aria-label='Language'>" + tabs + "</nav>" if len(codes) > 1 else "") + note
                + VOICE_HTML.replace("__LANG__", f"lang='{esc(pack['tag'])}' dir='{esc(pack['dir'])}'").replace("__STATE__", _js(state))
                .replace("__NAME__", esc(cat.language_name(code))))
        return Response(self._page("Record your voice", body, current="assignments", sub=a["title"],
                                   action=f"<a class='button secondary' href='/assignments/{esc(a['id'])}'>Back to the assignment</a>"))

    def voice_save(self, req: Request) -> Response:
        key, ext = req.params["key"], voices.ext_of(req.headers.get("Content-Type", ""))
        data = req.body
        if not voices.KEY.match(key) or not ext:
            return json_response({"error": "that is not a sound this hub can keep"}, status=400)
        if not data or len(data) > voices.MAX_BYTES or not voices.looks_like(data, ext):
            return json_response({"error": "the recording is empty, too long or not a sound"}, status=400)
        if key not in self.spoken_words():
            return json_response({"error": "those words are no longer in an assignment"}, status=404)
        try:
            secs = max(0.0, min(float(req.query.get("secs") or 0), voices.MAX_SECONDS))
        except ValueError:
            secs = 0.0
        voices.save(self.voice_dir, key, ext, data)
        self.db.execute("INSERT OR REPLACE INTO voices (key, secs, added) VALUES (?, ?, ?)", (key, secs, time.time()))
        return json_response({"bytes": len(data), "secs": secs})

    def voice_delete(self, req: Request) -> Response:
        key = req.params["key"]
        if voices.KEY.match(key):
            voices.remove(self.voice_dir, key)
            self.db.execute("DELETE FROM voices WHERE key = ?", (key,))
        return json_response({"ok": True})


VOICE_HTML = r"""
<p class="sub" style="margin-top:0">Many phones can't read __NAME__ aloud. Students there hear your recording when they tap Listen. Young children often listen better to a voice they know anyway.</p>
<div class="notice warn" id="nomic" hidden>This page can't reach a microphone here. Open it on the hub laptop itself, at <code>http://localhost</code> or <code>http://127.0.0.1</code>, or add a sound file to each line.</div>
<div class="list voice" id="rows" __LANG__></div><p class="error" id="err" role="alert"></p>
<div class="cards" style="margin-top:16px">
<section class="panel"><h2 style="margin-top:0">How it gets to phones</h2><p class="small">Recordings total <strong><span id="total"></span></strong>. That's too big for the radio, so they go with the next Wi-Fi copy or memory card. Until then, phones show the words and the pictures.</p></section>
<section class="panel"><h2 style="margin-top:0">Say each word once</h2><p class="small">A word is recorded once and used in every question and assignment that shows it. If you change the words, record them again.</p></section></div>
<script>
const S = __STATE__, $ = id => document.getElementById(id);
let live = null, player = new Audio();
const canRecord = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
if (!canRecord) $('nomic').hidden = false;
function el(tag, text, cls) { const e = document.createElement(tag); if (text !== undefined) e.textContent = text; if (cls) e.className = cls; return e; }
function button(text, fn, cls, label) { const b = el('button', text, 'secondary tiny ' + (cls || '')); b.type = 'button'; b.onclick = fn; if (label) b.setAttribute('aria-label', label); return b; }
function secs(n) { return n < 1.5 ? '1 second' : Math.round(n) + ' seconds'; }
function draw() {
  $('total').textContent = (S.total ? Math.max(1, Math.round(S.total / 1024)) : 0) + ' KB';
  $('rows').replaceChildren(...S.rows.map(r => {
    const row = el('div'), words = el('span', undefined, 'grow'), on = live && live.row === r;
    words.append(el('strong', r.t)); words.lastChild.dataset.math = 1;
    const what = el('span', r.what + (r.other > 0 ? ` · also in ${r.other} other assignment${r.other === 1 ? '' : 's'}` : '') + (r.untranslated ? ' · not translated' : ''), 'small'); what.lang = 'en'; what.dir = 'ltr'; words.append(what);
    const len = el('span', on ? 'Recording…' : r.have ? (r.secs ? secs(r.secs) : 'Recorded') + ` · ${Math.max(1, Math.round(r.bytes / 1024))} KB` : 'Not recorded', 'small'); len.lang = 'en'; len.style.flex = '0 0 130px';
    const bar = el('span', undefined, 'level' + (on ? ' on' : '')); if (on) { bar.append(el('i')); live.bar = bar.firstChild; }
    const acts = el('span', undefined, 'acts'); acts.lang = 'en'; acts.dir = 'ltr';
    if (r.have && !on) acts.append(button('Play', () => { player.pause(); player = new Audio('/voice/' + r.k + '?' + r.bytes); player.play(); }, '', 'Play ' + r.t));
    if (canRecord) acts.append(button(on ? 'Stop' : r.have ? 'Again' : 'Record', () => on ? stop() : record(r), on ? 'danger' : r.have ? '' : 'rec', (on ? 'Stop recording ' : 'Record ') + r.t));
    if (!on) { const f = el('label', 'File', 'button secondary tiny'), i = el('input'); i.type = 'file'; i.accept = 'audio/*'; i.style.cssText = 'position:absolute;opacity:0;width:1px'; i.setAttribute('aria-label', 'Sound file for ' + r.t); i.onchange = () => i.files[0] && send(r, i.files[0], i.files[0].type, 0); f.append(i); acts.append(f); }
    if (r.have && !on) acts.append(button('Delete', async () => { await fetch('/api/voice/' + r.k + '/delete', {method: 'POST'}); S.total -= r.bytes; r.have = false; r.bytes = 0; draw(); }, '', 'Delete the recording of ' + r.t));
    row.append(words, bar, len, acts); return row;
  }));
  if (window.meshMath) meshMath($('rows'));
}
async function record(r) {
  if (live) await stop();
  $('err').textContent = '';
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true}}); }
  catch (e) { $('err').textContent = 'The microphone was not allowed. Allow it in the browser, or add a sound file.'; return; }
  const type = ['audio/webm;codecs=opus', 'audio/mp4', 'audio/ogg;codecs=opus'].find(t => MediaRecorder.isTypeSupported(t)) || '';
  const rec = new MediaRecorder(stream, Object.assign({audioBitsPerSecond: 20000}, type ? {mimeType: type} : {})), parts = [], start = Date.now();
  live = {row: r, rec, stream, bar: null};
  rec.ondataavailable = e => parts.push(e.data);
  live.done = new Promise(ok => { rec.onstop = async () => { const t = (Date.now() - start) / 1000; stream.getTracks().forEach(x => x.stop()); live = null; await send(r, new Blob(parts, {type: rec.mimeType}), rec.mimeType, t); ok(); }; });
  rec.start(); draw();
  const ctx = new (window.AudioContext || window.webkitAudioContext)(), an = ctx.createAnalyser(), d = new Uint8Array(an.fftSize); ctx.createMediaStreamSource(stream).connect(an);
  const me = live; (function tick() { if (live !== me) { ctx.close(); return; } an.getByteTimeDomainData(d); let m = 0; d.forEach(v => { m = Math.max(m, Math.abs(v - 128)); }); if (me.bar) me.bar.style.width = Math.min(100, m) + '%'; if (Date.now() - start > S.secs * 1000) stop(); else setTimeout(tick, 80); })();
}
function stop() { if (!live) return; const d = live.done; if (live.rec.state !== 'inactive') live.rec.stop(); return d; }
async function send(r, blob, type, t) {
  $('err').textContent = '';
  if (blob.size > S.most) { $('err').textContent = `That sound is too big (${Math.round(blob.size / 1024)} KB). Keep it under ${Math.round(S.most / 1024)} KB.`; draw(); return; }
  try {
    const res = await fetch(`/api/voice/${r.k}?secs=${t.toFixed(1)}`, {method: 'POST', headers: {'Content-Type': (type || 'audio/webm').split(';')[0]}, body: blob}), d = await res.json();
    if (!res.ok) throw new Error(d.error);
    S.total += d.bytes - r.bytes; r.have = true; r.bytes = d.bytes; r.secs = d.secs;
  } catch (e) { $('err').textContent = e.message[0].toUpperCase() + e.message.slice(1) + '.'; }
  draw();
}
window.addEventListener('beforeunload', e => { if (live) e.preventDefault(); });
draw();
</script>
"""
