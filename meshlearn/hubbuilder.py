"""The teacher hub's assignment builder: question cards, notes on wrong answers, a phone preview and the radio size.

Mixed into Hub, like the other page files.
"""

from __future__ import annotations

import json
from urllib.parse import quote

from . import assignments as asg
from . import protocol
from .web import PICTURES, Request, Response, esc, json_response, picture, redirect

AGE_LOOKS = (("early", "young", "Ages 5 to 7"), ("primary", "mid", "Ages 8 to 11"), ("lower", "teen", "Ages 12 and up"),
             ("upper", "teen", "Ages 12 and up"), ("adult", "teen", "Ages 12 and up"))


def _js(value) -> str:
    """JSON that is safe inside a <script>."""
    return json.dumps(value).replace("</", "<\\/").replace("<!--", "<\\!--")


class HubBuilder:
    def _builder_routes(self) -> None:
        app = self.app
        app.add_route("GET", "/assignments/new", self.new_assignment)
        app.add_route("GET", "/assignments/<aid>/edit", self.edit_assignment)
        app.add_route("POST", "/api/assignments", self.api_create)
        app.add_route("POST", "/api/assignments/size", self.api_size)
        app.add_route("GET", "/api/assignments/<aid>", self.api_get)

    def _builder(self, title: str, edit_id: str = "", draft: dict = None, note: str = "") -> Response:
        courses = [{"id": c["id"], "title": c["title"], "units": c["units"]} for c in self.courses() if not c.get("comingSoon")]
        pics = "".join(f"<button type='button' data-pic='{k}' data-find='{esc((p['n'] + ' ' + p['g'] + ' ' + k).lower())}'>"
                       f"{picture(k)}<span>{esc(p['n'])}</span></button>" for k, p in PICTURES.items())
        ages = self.setting("ages").split(",")
        looks = {look: label for age, look, label in AGE_LOOKS if age in ages} or {"mid": "Ages 8 to 11"}
        look_options = "".join(f"<option value='{k}'>{esc(v)}</option>" for k, v in looks.items())
        phones = self.db.one("SELECT COUNT(*) AS n FROM phones")["n"]
        body = (f"<div class='notice warn' role='status'>{esc(note)}</div>" if note else "") + (
            BUILDER_HTML.replace("__LOOKS__", look_options).replace("__PICTURES__", pics)
            .replace("__STATE__", _js({"edit": edit_id, "courses": courses, "draft": draft or {}, "phones": phones})))
        action = ("<span><button type='button' class='secondary' id='savedraft'>" + ("Save changes" if edit_id else "Save draft") + "</button> "
                  "<button type='button' id='savesend'>" + ("Save and send to phones" if edit_id else "Send to phones") + "</button></span>")
        return Response(self._page(title, body, current="assignments", action=action))

    def new_assignment(self, req: Request) -> Response:
        course, unit = req.query.get("course", ""), req.query.get("unit", "")
        if req.query.get("practice"):
            draft, note = self.practice_draft(course, unit, req.query["practice"][:200])
            return self._builder("New assignment", draft=draft, note=note)
        return self._builder("New assignment", draft={"course": course, "unit": unit})

    def edit_assignment(self, req: Request) -> Response:
        aid = req.params["aid"]
        if self.get_assignment(aid) is None:
            return redirect("/assignments")
        return self._builder("Edit assignment", edit_id=aid, draft=self.get_assignment(aid),
                             note="Changing questions makes a new version. Phones where this is open get the full copy again. Changing only "
                                  "the due date costs one packet per phone, and notes on wrong answers cost nothing.")

    def api_create(self, req: Request) -> Response:
        try:
            data = req.json()
            old_version = self.current_version(str(data.get("id") or "")) if isinstance(data, dict) else None
            assignment = self.create_assignment(data)
        except (asg.AssignmentError, ValueError, AttributeError) as exc:
            return json_response({"error": str(exc)}, status=400)
        aid = assignment["id"]
        sent = 0
        if data.get("send"):
            for row in self.db.all("SELECT node_id FROM phones"):
                self.set_open(aid, row["node_id"], True)
                sent += 1
        elif old_version is not None:
            # Edited: push changes to phones where it is open (full copy if questions changed, 1 packet if only the date).
            for row in self.db.all("SELECT h.node_id, h.version, a.version AS current FROM holdings h JOIN assignments a ON a.id = h.aid "
                                   "WHERE h.aid = ? AND h.open = 1", (aid,)):
                self.set_open(aid, row["node_id"], True)
        return json_response({"id": aid, "sent": sent, "frames": protocol.frame_count(protocol.ASSIGN, {"a": self.packed_for_phone(aid)})})

    def api_size(self, req: Request) -> Response:
        """How many radio packets an unsaved assignment would need."""
        try:
            assignment = asg.normalize_assignment(req.json())
            packed = asg.pack(dict(asg.public_view(assignment), version="00000000"))
            return json_response({"frames": protocol.frame_count(protocol.ASSIGN, {"a": packed, "o": 1, "d": assignment["due"]})})
        except (ValueError, AttributeError, TypeError) as exc:
            return json_response({"frames": None, "why": str(exc)})

    def api_get(self, req: Request) -> Response:
        assignment = self.get_assignment(req.params["aid"])
        return json_response(assignment) if assignment else json_response({"error": "not found"}, status=404)


BUILDER_HTML = r"""
<style>
#builder .panel{padding:16px}#builder label{margin:8px 0 4px;font-size:15px}#builder .fields{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(180px,100%),1fr));gap:4px 12px}
#builder .full{grid-column:1/-1}#builder textarea{min-height:64px}#builder input[type=text],#builder input[type=number],#builder input[type=date],#builder select,#builder textarea{background:var(--bg)}
.bq{margin:10px 0}.bq .sum{display:flex;align-items:center;gap:10px;width:100%;margin:0;padding:12px 14px;border-radius:14px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-weight:400;text-align:left}
.bq .sum .grow{overflow-wrap:anywhere}.bq .sum:hover{border-color:var(--accent)}.bq.open{border:2px solid var(--accent);border-radius:14px;background:var(--card);padding:14px 16px}
.bq .head{display:flex;flex-wrap:wrap;align-items:center;gap:8px}.bq.open .qn{background:var(--accent);color:var(--accent-ink)}
.opt{display:grid;grid-template-columns:24px minmax(0,1fr) auto auto;gap:6px 8px;align-items:center;margin:8px 0}.opt input[type=radio]{width:20px;height:20px;margin:0}.opt.right input[type=text].otext{border:2px solid var(--good)}
.opt .whyin{grid-column:2/-1;font-size:15px;padding:7px 10px}.opt .tag{grid-column:2/-1;color:var(--good);font-size:13px;font-weight:700}
.watch{display:grid;grid-template-columns:minmax(80px,1fr) minmax(0,2fr) auto;gap:8px;margin:6px 0}
#builder button.mini{margin:0;padding:6px 10px;font-size:14px;background:transparent;color:var(--accent);border:1px solid var(--line);font-weight:600}#builder button.mini:disabled{opacity:.4}
#builder button.mini svg.pic{width:26px;height:26px;display:block}#builder button.add{margin:0;background:var(--sel);color:var(--sel-ink);border:1px dashed var(--accent);padding:10px 14px}
.addrow{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.seg{display:flex;flex-wrap:wrap;gap:8px}.seg label{display:flex;gap:8px;align-items:flex-start;flex:1 1 220px;margin:0!important;padding:10px 12px;border:1px solid var(--line);border-radius:10px;font-weight:400}.seg input{margin:4px 0 0}
.phoneframe{box-sizing:border-box;width:270px;max-width:100%;height:480px;margin:8px auto;border:10px solid #1d1d1b;border-radius:34px;background:#f7f7f4;color:#1d1d1b;overflow:hidden;display:flex;flex-direction:column}
.phoneframe .ph{padding:12px 12px 8px;background:#fff;border-bottom:1px solid #d9d9d3;font-size:12px;font-weight:700}.phoneframe .bars{display:flex;gap:4px;margin-top:6px}.phoneframe .bars i{flex:1;height:5px;border-radius:3px;background:#e2e2dc}.phoneframe .bars i.on{background:#1f5fbf}
.phoneframe .pb{padding:14px 12px;display:flex;flex-direction:column;gap:8px;overflow-y:auto}.phoneframe .pq{font-size:17px;font-weight:700;line-height:1.25;overflow-wrap:anywhere}.phoneframe.young .pq{font-size:22px}
.phoneframe .po{display:flex;align-items:center;gap:8px;min-height:40px;padding:4px 10px;border-radius:10px;border:2px solid #d9d9d3;background:#fff;font-size:14px;overflow-wrap:anywhere}.phoneframe.young .po{min-height:54px;font-size:18px}.phoneframe .po b{flex:none;width:22px;height:22px;border-radius:11px;background:#eeeeea;display:flex;align-items:center;justify-content:center;font-size:11px}
.phoneframe .pi{border:2px solid #1f5fbf;border-radius:10px;padding:6px 10px;font-size:13px;color:#174a96;font-weight:700}.phoneframe .pf{min-height:42px;border:2px solid #d9d9d3;border-radius:10px;background:#fff}.phoneframe .pf.big{min-height:110px}.phoneframe .pics{display:flex;flex-wrap:wrap;gap:4px}.phoneframe svg.pic{width:44px;height:44px}
.sizebar{display:block;height:10px;border-radius:5px;background:var(--line);overflow:hidden;margin:6px 0}.sizebar span{display:block;height:100%;width:0;background:var(--good)}.sizebar.mid span{background:var(--warn)}.sizebar.big span{background:var(--bad)}
@media(min-width:900px){#pvcol{position:sticky;top:16px}}
</style>
<form id="builder" novalidate><div class="cols"><div class="wide">
  <div class="panel fields">
    <div class="full"><label for="title">Title</label><input type="text" id="title" required maxlength="120"></div>
    <div><label for="course">Course</label><select id="course"></select></div>
    <div><label for="unit">Unit</label><select id="unit"></select></div>
    <div><label for="due">Due</label><input type="date" id="due"></div>
    <div><label for="look">Preview for</label><select id="look">__LOOKS__</select></div>
    <div class="full seg" role="radiogroup" aria-label="Mode">
      <label><input type="radio" name="mode" value="graded" checked><span><strong>Graded</strong><br><span class="small">The answer key stays on this laptop.</span></span></label>
      <label><input type="radio" name="mode" value="practice"><span><strong>Practice</strong><br><span class="small">Students see right and wrong at once, and can try again. The key goes to the phones.</span></span></label>
    </div>
    <details class="full"><summary class="small">Instructions and topic</summary>
      <label for="instructions">Instructions for students</label><textarea id="instructions" maxlength="2000"></textarea>
      <label for="subject">Topic (optional)</label><input type="text" id="subject" maxlength="60"></details>
  </div>
  <div id="questions"></div>
  <div class="addrow">
    <button type="button" class="add" data-add="choice">+ Choice question</button>
    <button type="button" class="add" data-add="number">+ Number answer</button>
    <button type="button" class="add" data-add="text">+ Short answer</button>
    <button type="button" class="add" data-add="long">+ Written answer</button>
  </div>
  <p id="error" class="error" role="alert"></p>
  <details class="panel"><summary><strong>Import questions from a spreadsheet</strong></summary>
    <p class="muted">Save the spreadsheet as CSV, with a first row of these headings: <code>type, question, options, answer, points, info,
    pictures, notes</code>. Only question and answer are needed. Put options between | signs; the answer is the option itself or its number.
    Types: choice, number, text, long. Notes say what wrong answers show: for a choice, one per option between | signs; otherwise
    <code>2/6=added tops and bottoms</code>. <a href="#" id="csvsample">Download an example</a></p>
    <input type="file" id="csvfile" accept=".csv,text/csv"><p id="csvmsg" role="status"></p></details>
  <p class="small">Equations: write them between <code>\(</code> and <code>\)</code>, like <code>\(\frac{1}{2} + x^2\)</code>.</p>
</div>
<aside class="col" id="pvcol" style="flex:1 1 290px" aria-label="Preview">
  <strong>What students see</strong>
  <div class="phoneframe" id="pv" aria-live="off"></div>
  <div class="panel"><div style="display:flex;justify-content:space-between"><strong>Radio size</strong><strong id="sizeword"></strong></div>
    <span class="sizebar" id="sizebar"><span></span></span><span class="small" id="sizetext"></span></div>
</aside></div></form>
<dialog id="picdlg"><div class="pichead"><input type="text" id="picfind" placeholder="Find a picture: goat, water, happy…" aria-label="Find a picture">
<button type="button" id="picdone">Done</button></div><p class="muted" id="picnow"></p>
<div class="picgrid">__PICTURES__</div></dialog>
<script>
const S = __STATE__;
const $ = id => document.getElementById(id);
const box = $('questions'), NAMES = {choice: 'Choice question', number: 'Number answer', text: 'Short answer', long: 'Written answer'};
let qs = [], cur = 0, dirty = false, saving = false;

// Text goes in as text, never as HTML, so nothing a teacher types can break the page.
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'cls') el.className = v;
    else if (k === 'value' || k === 'checked' || k === 'disabled') el[k] = v;
    else if (v !== undefined && v !== null && v !== false) el.setAttribute(k, v);
  }
  kids.flat().forEach(kid => kid !== null && kid !== undefined && el.append(kid));
  return el;
}
const num = t => parseFloat(String(t).replace(',', '.'));
function blank(type) {
  const q = {type, prompt: '', points: type === 'long' ? 4 : 1};
  if (type === 'choice') Object.assign(q, {options: ['', '', ''], answer: 0, why: ['', '', ''], opic: ['', '', '']});
  if (type === 'number') Object.assign(q, {answer: '', tolerance: 0, why: []});
  if (type === 'text') Object.assign(q, {accept: [], why: []});
  if (type === 'long') Object.assign(q, {model: '', max_chars: 600});
  return q;
}
function load(q) {            // a saved question, with every list the editor expects
  q = Object.assign(blank(q.type), JSON.parse(JSON.stringify(q)));
  if (q.type === 'choice') ['why', 'opic'].forEach(k => { q[k] = q.options.map((_, i) => (q[k] || [])[i] || ''); });
  q.accepttext = (q.accept || []).join('\n');
  return q;
}
function changed() { dirty = true; preview(); sizeSoon(); }
function input(q, key, attrs) {
  return h('input', Object.assign({type: 'text', value: q[key] ?? '', oninput: e => { q[key] = e.target.value; changed(); }}, attrs));
}
function area(q, key, attrs) {
  return h('textarea', Object.assign({value: q[key] ?? '', oninput: e => { q[key] = e.target.value; changed(); }}, attrs));
}
let uid = 0;
function labelled(text, el) { el.id = el.id || 'f' + (++uid); return [h('label', {for: el.id}, text), el]; }

function render(focus) {
  box.replaceChildren(...qs.map((q, i) => i === cur ? editor(q, i) : summary(q, i)));
  preview();
  if (focus) { const t = box.querySelector('.bq.open textarea'); if (t) t.focus(); }
}
function summary(q, i) {
  const notes = (q.why || []).filter(w => typeof w === 'string' ? w : w.w).length;
  const detail = `${NAMES[q.type].split(' ')[0]} · ${q.points} point${num(q.points) === 1 ? '' : 's'}` + (q.type === 'long' ? ' · you mark it' : '') + (notes ? ` · ${notes} note${notes === 1 ? '' : 's'}` : '');
  return h('div', {cls: 'bq'}, h('button', {type: 'button', cls: 'sum', onclick: () => { cur = i; render(true); }},
    h('span', {cls: 'qn'}, String(i + 1)), h('span', {cls: 'grow'}, q.prompt || '(no question yet)'), h('span', {cls: 'small'}, detail)));
}
function move(i, d) { const [q] = qs.splice(i, 1); qs.splice(i + d, 0, q); cur = i + d; changed(); render(); }
function editor(q, i) {
  const mini = (text, fn, off, label) => h('button', {type: 'button', cls: 'mini', onclick: fn, disabled: !!off, 'aria-label': label || text}, text);
  const head = h('div', {cls: 'head'}, h('span', {cls: 'qn'}, String(i + 1)), h('strong', {cls: 'grow'}, NAMES[q.type]),
    mini('↑', () => move(i, -1), i === 0, 'Move up'), mini('↓', () => move(i, 1), i === qs.length - 1, 'Move down'),
    mini('Copy', () => { qs.splice(i + 1, 0, load(q)); cur = i + 1; changed(); render(true); }, false, 'Copy this question'),
    mini('Remove', () => { qs.splice(i, 1); cur = Math.max(0, Math.min(cur, qs.length - 1)); changed(); render(); }, false, 'Remove this question'));
  const pts = h('div', {cls: 'fields'}, h('div', {}, labelled('Points', input(q, 'points', {type: 'number', min: '0.5', step: '0.5'}))));
  const parts = [head, ...labelled('Question', area(q, 'prompt', {rows: 2, maxlength: 500}))];
  if (q.type === 'choice') parts.push(options(q));
  if (q.type === 'number') {
    pts.append(h('div', {}, labelled('Right answer', input(q, 'answer', {inputmode: 'decimal'}))),
               h('div', {}, labelled('Allowed difference (0 = exact)', input(q, 'tolerance', {inputmode: 'decimal'}))));
    parts.push(watch(q, 'a number'));
  }
  if (q.type === 'text') parts.push(...labelled('Accepted answers, one per line (capitals don’t matter)', area(q, 'accepttext', {rows: 2})), watch(q, 'an answer'));
  if (q.type === 'long') {
    parts.push(...labelled('Your model answer (only you see it, when marking)', area(q, 'model', {rows: 2, maxlength: 1500})));
    pts.append(h('div', {}, labelled('Longest answer, in letters', input(q, 'max_chars', {type: 'number', min: '50', max: '1500'}))));
  }
  const info = area(q, 'info', {rows: 2, maxlength: 2000});
  const more = h('details', {}, h('summary', {cls: 'small'}, 'Question info and pictures'),
    ...labelled('Question info: a text, table or data students can pull down', info),
    h('button', {type: 'button', cls: 'mini', disabled: i === 0, onclick: () => { q.info = qs[i - 1].info || ''; info.value = q.info; changed(); }}, 'Same as the question above'),
    ...labelled('Pictures, like apple*3, ball', input(q, 'pic', {maxlength: 200})),
    h('button', {type: 'button', cls: 'mini', onclick: () => pick(q.pic, v => { q.pic = v; changed(); render(); }, true)}, 'Choose pictures…'));
  if (q.info || q.pic) more.open = true;
  parts.push(pts, more);
  return h('div', {cls: 'bq open'}, ...parts);
}
function options(q) {
  const wrap = h('div', {role: 'radiogroup', 'aria-label': 'Right answer'});
  q.options.forEach((text, j) => {
    const right = q.answer === j, letter = String.fromCharCode(65 + j);
    const pic = q.opic[j] ? svgOf(q.opic[j]) : 'Picture';
    wrap.append(h('div', {cls: 'opt' + (right ? ' right' : '')},
      h('input', {type: 'radio', name: 'right', checked: right, 'aria-label': `Option ${letter} is the right answer`, onchange: () => { q.answer = j; changed(); render(); }}),
      h('input', {type: 'text', cls: 'otext', value: text, maxlength: 200, placeholder: `Option ${letter}`, 'aria-label': `Option ${letter}`, oninput: e => { q.options[j] = e.target.value; changed(); }}),
      h('button', {type: 'button', cls: 'mini', 'aria-label': `Picture for option ${letter}`, onclick: () => pick(q.opic[j], v => { q.opic[j] = v; changed(); render(); }, false)}, pic || 'Picture'),
      h('button', {type: 'button', cls: 'mini', 'aria-label': `Remove option ${letter}`, disabled: q.options.length <= 2, onclick: () => {
        ['options', 'why', 'opic'].forEach(k => q[k].splice(j, 1)); if (q.answer > j) q.answer -= 1; else if (q.answer === j) q.answer = 0; changed(); render(); }}, '×'),
      right ? h('span', {cls: 'tag'}, 'Right answer') :
        h('input', {type: 'text', cls: 'whyin', value: q.why[j], maxlength: 80, placeholder: 'What picking this shows (only you see it)', 'aria-label': `What option ${letter} shows`, oninput: e => { q.why[j] = e.target.value; changed(); }})));
  });
  wrap.append(h('button', {type: 'button', cls: 'mini', onclick: () => { q.options.push(''); q.why.push(''); q.opic.push(''); changed(); render(); }}, '+ Add option'));
  return wrap;
}
function watch(q, what) {
  const wrap = h('details', {}, h('summary', {cls: 'small'}, 'Wrong answers to watch for (only you see these)'));
  wrap.open = q.why.length > 0;
  q.why.forEach((w, j) => wrap.append(h('div', {cls: 'watch'},
    h('input', {type: 'text', value: w.x, maxlength: 40, placeholder: 'If they answer', 'aria-label': `Wrong answer ${j + 1}`, oninput: e => { w.x = e.target.value; changed(); }}),
    h('input', {type: 'text', value: w.w, maxlength: 80, placeholder: 'it shows, e.g. added tops and bottoms', 'aria-label': `What wrong answer ${j + 1} shows`, oninput: e => { w.w = e.target.value; changed(); }}),
    h('button', {type: 'button', cls: 'mini', 'aria-label': `Remove wrong answer ${j + 1}`, onclick: () => { q.why.splice(j, 1); changed(); render(); }}, '×'))));
  wrap.append(h('button', {type: 'button', cls: 'mini', onclick: () => { q.why.push({x: '', w: ''}); changed(); render(); }}, `+ Add ${what}`));
  return wrap;
}

// Pictures
const dlg = $('picdlg'), find = $('picfind');
let picked = '', done = null, many = false;
function svgOf(name) { const b = dlg.querySelector(`[data-pic="${CSS.escape(name)}"] svg`); return b ? b.cloneNode(true) : null; }
function pick(now, then, several) { picked = now || ''; done = then; many = several; find.value = ''; filterPics(); $('picnow').textContent = picked; dlg.showModal(); }
function filterPics() { const t = find.value.trim().toLowerCase(); dlg.querySelectorAll('[data-pic]').forEach(b => { b.hidden = !!t && !b.dataset.find.includes(t); }); }
find.addEventListener('input', filterPics);
dlg.addEventListener('click', e => {
  const b = e.target.closest('[data-pic]'); if (!b) return;
  if (!many) { done(b.dataset.pic === picked ? '' : b.dataset.pic); dlg.close(); return; }
  picked += (picked.trim() ? ', ' : '') + b.dataset.pic; $('picnow').textContent = picked; done(picked);
});
$('picdone').addEventListener('click', () => dlg.close());

// Preview and radio size
function preview() {
  const pv = $('pv'), q = qs[cur];
  pv.className = 'phoneframe ' + $('look').value;
  if (!q) { pv.replaceChildren(); return; }
  const body = h('div', {cls: 'pb'});
  if (q.info) body.append(h('div', {cls: 'pi'}, 'Question info ▾'));
  body.append(h('div', {cls: 'pq'}, q.prompt || 'Your question shows here'));
  const pics = [];
  (q.pic || '').split(',').forEach(p => { const [name, n] = p.trim().split('*'); for (let k = 0; k < Math.min(parseInt(n || 1, 10) || 1, 10); k++) { const s = name && svgOf(name); if (s) pics.push(s); } });
  if (pics.length) body.append(h('div', {cls: 'pics'}, ...pics));
  if (q.type === 'choice') q.options.forEach((o, j) => body.append(h('div', {cls: 'po'}, h('b', {}, String.fromCharCode(65 + j)), (q.opic[j] && svgOf(q.opic[j])) || null, h('span', {}, o || '…'))));
  else body.append(h('div', {cls: 'pf' + (q.type === 'long' ? ' big' : '')}));
  pv.replaceChildren(h('div', {cls: 'ph'}, `Question ${cur + 1} of ${qs.length}`, h('div', {cls: 'bars'}, ...qs.map((_, i) => h('i', {cls: i <= cur ? 'on' : ''})))), body);
  if (window.meshMath) meshMath(pv);
}
let timer = null, asked = 0;
function sizeSoon() { clearTimeout(timer); timer = setTimeout(size, 600); }
async function size() {
  const n = ++asked;
  let frames = null;
  try { frames = (await (await fetch('/api/assignments/size', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(collect())})).json()).frames; } catch (e) {}
  if (n !== asked) return;
  const bar = $('sizebar'), word = $('sizeword');
  if (!frames) { word.textContent = ''; bar.firstChild.style.width = '0'; $('sizetext').textContent = 'Shows once the title and every question are filled in.'; return; }
  const level = frames <= 5 ? ['Small', ''] : frames <= 12 ? ['Medium', 'mid'] : ['Large', 'big'];
  word.textContent = level[0]; bar.className = 'sizebar ' + level[1]; bar.firstChild.style.width = Math.min(100, frames * 5) + '%';
  $('sizetext').textContent = `About ${frames} radio packet${frames === 1 ? '' : 's'}. Phones that copied the lessons over Wi-Fi or a card only need one packet to open it.`;
}

// Saving
const course = $('course'), unit = $('unit');
course.add(new Option('No course', ''));
S.courses.forEach(c => course.add(new Option(c.title, c.id)));
function fillUnits(selected) {
  const c = S.courses.find(c => c.id === course.value);
  unit.length = 0; unit.add(new Option('No unit', ''));
  if (c) c.units.forEach((u, i) => unit.add(new Option(`Unit ${i + 1}: ${u.title}`, u.id)));
  if (selected) unit.value = selected;
}
course.addEventListener('change', () => { fillUnits(); changed(); });
function collect() {
  const questions = qs.map((q, i) => {
    const out = Object.assign({}, q, {id: 'q' + (i + 1)});
    if (q.type === 'choice') {
      const keep = q.options.map((o, j) => j).filter(j => q.options[j].trim());
      out.options = keep.map(j => q.options[j].trim()); out.why = keep.map(j => j === q.answer ? '' : q.why[j]); out.opic = keep.map(j => q.opic[j]); out.answer = keep.indexOf(q.answer);
    }
    if (q.type === 'number') { out.answer = String(q.answer).replace(',', '.'); out.tolerance = String(q.tolerance || 0).replace(',', '.'); }
    if (q.type === 'text') out.accept = q.accepttext.split('\n').map(s => s.trim()).filter(Boolean);
    delete out.accepttext;
    return out;
  });
  const body = {title: $('title').value, subject: $('subject').value, due: $('due').value, instructions: $('instructions').value,
                mode: document.querySelector('[name=mode]:checked').value, course: course.value, unit: unit.value, questions};
  if (S.edit) body.id = S.edit;
  return body;
}
async function save(send) {
  if (saving) return;
  if (send && !S.phones) { $('error').textContent = 'No phone is set up yet, so this can only be saved. Add one on the Phones and radio page.'; return; }
  saving = true; $('error').textContent = '';
  try {
    const res = await fetch('/api/assignments', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(Object.assign(collect(), {send}))});
    const data = await res.json();
    if (!res.ok) {
      $('error').textContent = data.error[0].toUpperCase() + data.error.slice(1) + '.';
      const m = /question (\d+)/.exec(data.error); if (m && qs[m[1] - 1]) { cur = m[1] - 1; render(); }
      $('error').scrollIntoView({block: 'center'}); return;
    }
    dirty = false;
    location.href = '/assignments/' + data.id + '?msg=' + encodeURIComponent(send ? `Saved, and on its way to ${data.sent} phone${data.sent === 1 ? '' : 's'}.` : 'Saved.');
  } catch (e) { $('error').textContent = 'Could not save. Is the hub still running?'; }
  finally { saving = false; }
}
$('savedraft').addEventListener('click', () => save(false));
$('savesend').addEventListener('click', () => save(true));
$('builder').addEventListener('submit', e => e.preventDefault());
window.addEventListener('beforeunload', e => { if (dirty) e.preventDefault(); });
['title', 'subject', 'due', 'instructions'].forEach(id => $(id).addEventListener('input', changed));
unit.addEventListener('change', changed);
document.querySelectorAll('[name=mode]').forEach(r => r.addEventListener('change', changed));
$('look').addEventListener('change', preview);
document.querySelectorAll('[data-add]').forEach(b => b.addEventListener('click', () => { qs.push(blankLoaded(b.dataset.add)); cur = qs.length - 1; changed(); render(true); }));
function blankLoaded(type) { return load(blank(type)); }

// Spreadsheet import
const SAMPLE = 'type,question,options,answer,points,info,pictures,notes\nchoice,How many apples?,2|3|4,3,1,,apple*3,missed one||counted one twice\nnumber,What is 1/4 + 1/2?,,0.75,1,,,2/6=added tops and bottoms\ntext,What gas do plants give off?,,oxygen|O2,1,,,carbon dioxide=mixed up in and out\nlong,Why does rain matter to farmers?,,,3,Look at the table.,,\n';
$('csvsample').addEventListener('click', e => {
  e.preventDefault();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([SAMPLE], {type: 'text/csv'})); a.download = 'meshlearn-questions.csv'; a.click();
});
function questionFromRow(r) {
  const get = k => String(r[k] === undefined || r[k] === null ? '' : r[k]).trim();
  const raw = get('options').split('|').map(s => s.trim()), options = raw.filter(Boolean), answer = get('answer');
  let type = get('type').toLowerCase();
  if (!['choice', 'number', 'text', 'long'].includes(type)) type = options.length > 1 ? 'choice' : !answer ? 'long' : isNaN(answer.replace(',', '.')) ? 'text' : 'number';
  const q = {type, prompt: get('question'), points: num(get('points')) || (type === 'long' ? 4 : 1), info: get('info'), pic: get('pictures')};
  if (!q.prompt) return null;
  const pairs = get('notes').split('|').map(s => s.split('=')).filter(p => p.length > 1).map(p => ({x: p[0].trim(), w: p.slice(1).join('=').trim()}));
  if (type === 'choice') {
    const at = options.findIndex(o => o.toLowerCase() === answer.toLowerCase());
    q.options = options; q.answer = at >= 0 ? at : parseInt(answer, 10) - 1;
    const notes = get('notes').split('|').map(s => s.trim());
    q.why = raw.map((o, i) => [o, notes[i] || '']).filter(p => p[0]).map(p => p[1]);
    if (options.length < 2 || !(q.answer >= 0 && q.answer < options.length)) return {bad: q.prompt};
  } else if (type === 'number') {
    q.answer = answer.replace(',', '.'); q.why = pairs;
    if (answer === '' || isNaN(q.answer)) return {bad: q.prompt};
  } else if (type === 'text') {
    q.accept = answer.split('|').map(s => s.trim()).filter(Boolean); q.why = pairs;
    if (!q.accept.length) return {bad: q.prompt};
  }
  return {q};
}
$('csvfile').addEventListener('change', e => {
  const file = e.target.files[0], out = $('csvmsg');
  if (!file) return;
  // Excel in many countries still saves CSV as Windows-1252, so use that when the bytes aren't UTF-8.
  const read = bytes => { try { return new TextDecoder('utf-8', {fatal: true}).decode(bytes); } catch (err) { return new TextDecoder('windows-1252').decode(bytes); } };
  const go = () => file.arrayBuffer().then(bytes => Papa.parse(read(bytes), {header: true, skipEmptyLines: true, transformHeader: t => t.trim().toLowerCase(), complete: res => {
    const rows = res.data.slice(0, 100).map(questionFromRow).filter(Boolean), bad = rows.filter(r => r.bad), good = rows.filter(r => !r.bad);
    if (good.length) { qs = qs.filter(q => q.prompt.trim()).concat(good.map(r => load(r.q))); cur = qs.length - good.length; changed(); render(); }
    out.textContent = `Added ${good.length} question${good.length === 1 ? '' : 's'}. Check them, then save.` +
      (bad.length ? ` Not added, because the answer doesn't fit: ${bad.slice(0, 5).map(b => b.bad).join('; ')}${bad.length > 5 ? '…' : ''}` : '') +
      (rows.length ? '' : ' Is there a "question" heading in the first row?');
    e.target.value = '';
  }})).catch(() => { out.textContent = 'Could not read that file.'; });
  if (window.Papa) return go();
  const s = document.createElement('script'); s.src = '/site/vendor/papaparse/papaparse.min.js'; s.onload = go; document.body.appendChild(s);
});

// Start
['title', 'subject', 'due', 'instructions'].forEach(k => { $(k).value = S.draft[k] || ''; });
if (S.draft.instructions || S.draft.subject) $('instructions').closest('details').open = true;
(document.querySelector(`[name=mode][value="${S.draft.mode === 'practice' ? 'practice' : 'graded'}"]`)).checked = true;
course.value = S.draft.course || ''; if (course.selectedIndex < 0) course.value = ''; fillUnits(S.draft.unit);
qs = (S.draft.questions || []).map(load);
if (!qs.length) qs.push(blankLoaded('choice'));
render(); size();
</script>
"""
