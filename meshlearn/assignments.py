"""Interactive assignments: definition, answer validation, and grading.

An assignment is plain JSON so it can travel over the mesh:

    {
      "id": "a1b2c3", "title": "Fractions check", "subject": "Math",
      "instructions": "...", "due": "2026-11-01",
      "mode": "graded" | "practice",
      "questions": [
        {"id": "q1", "type": "choice", "prompt": "...", "options": ["A", "B"], "answer": 1, "points": 1},
        {"id": "q2", "type": "number", "prompt": "...", "answer": 0.5, "tolerance": 0.01, "points": 1},
        {"id": "q3", "type": "text", "prompt": "...", "accept": ["chlorophyll"], "points": 1},
        {"id": "q4", "type": "long", "prompt": "Explain ...", "points": 4, "max_chars": 600}
      ]
    }

Any question may also have "info": the text, table or data that goes with it, which students
pull down on a phone and see beside the question on a computer. "pic" shows drawings from the
picture library ("apple*3, ball"), and a choice question's "opic" one drawing per option (or "").
Every phone has the library, so only the names travel.

"why" holds the teacher's notes on wrong answers: for a choice, one per option ("" for none); for a
number or short answer, [{"x": "2/6", "w": "Added tops and bottoms"}]. Like a written question's
"model" answer it stays on the hub: it is never packed for phones and does not change the version.

"tr" holds translations, {"por": {"p": prompt, "o": [options], "n": info, "s": stamp}}; the assignment's own
"tr" has {"T": title, "I": instructions}. A phone shows a student their language where it is there and the original
where it isn't. "s" says which original was translated, so the hub can tell when it has changed since; like "lang",
the language the assignment is written in, it stays on the hub.

In "graded" mode the answer key never leaves the hub: the phone only checks
that answers are complete and well-formed, and the hub grades them. In
"practice" mode the key goes to the phone so students get instant feedback.
"long" answers are always graded by the teacher.
"""

from __future__ import annotations

import copy
import json
import re
import secrets
import zlib
from typing import Dict, List, Optional, Tuple

QUESTION_TYPES = ("choice", "number", "text", "long")
KEY_FIELDS = ("answer", "tolerance", "accept")
OWN_FIELDS = ("why", "model")   # the teacher's own notes: never on a phone, in any mode
MAX_TEXT_CHARS = 200
MAX_LONG_CHARS = 1500
MAX_INFO_CHARS = 2000
MAX_WHY_CHARS = 80
MAX_FIRST_CHARS = 40
MAX_UNSURE = 62   # questions the "not sure" bits cover: they stay a plain whole number on every phone
_PIC = re.compile(r"^[a-z0-9-]{1,30}(\*\d{1,2})?$")


class AssignmentError(ValueError):
    pass


def new_id(prefix: str = "") -> str:
    return prefix + secrets.token_hex(4)


def _clean_str(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


_CODE = re.compile(r"^[a-z]{3}$")


def _clean_tr(q: Optional[dict], raw) -> dict:
    """Translations of one question, or of the title and instructions (q is None); {} when there are none."""
    out: dict = {}
    for code, t in list(raw.items())[:8] if isinstance(raw, dict) else []:
        if not (isinstance(code, str) and _CODE.match(code) and isinstance(t, dict)):
            continue
        if q is None:
            one = {"T": _clean_str(t.get("T"), 120), "I": _clean_str(t.get("I"), 2000)}
        else:
            one = {"p": _clean_str(t.get("p"), 500), "n": _clean_str(t.get("n"), MAX_INFO_CHARS) if q.get("info") else ""}
            if q["type"] == "choice" and isinstance(t.get("o"), list):
                done = [_clean_str(o, 200) if isinstance(o, str) else "" for o in t["o"]][:len(q["options"])]
                if any(done):
                    one["o"] = done + [""] * (len(q["options"]) - len(done))
        one = {k: v for k, v in one.items() if v}
        if one:
            if q is not None and isinstance(t.get("s"), str):
                one["s"] = t["s"][:8]
            out[code] = one
    return out


def source_stamp(q: dict) -> str:
    """Says which wording of a question a translation was made from."""
    text = json.dumps([q["prompt"], q.get("options", []), q.get("info", "")], ensure_ascii=False)
    return format(zlib.crc32(text.encode("utf-8")), "08x")


def stale(q: dict, code: str) -> bool:
    """Whether the original has changed since this translation was made."""
    stamp = ((q.get("tr") or {}).get(code) or {}).get("s")
    return bool(stamp) and stamp != source_stamp(q)


def translated(q: dict, code: str) -> bool:
    """Whether nothing of a question is left in the original. An option that is only a number needs no translation."""
    t = (q.get("tr") or {}).get(code) or {}
    done = t.get("o") or []
    return bool(t.get("p")) and (not q.get("info") or bool(t.get("n"))) and all(
        (j < len(done) and done[j]) or parse_number(o) is not None for j, o in enumerate(q.get("options", [])))


def localized(assignment: dict, code: str) -> dict:
    """The assignment as a student of this language sees it: translated where it can be, the original elsewhere."""
    top = (assignment.get("tr") or {}).get(code) or {}
    if not top and not any(code in (q.get("tr") or {}) for q in assignment.get("questions", [])):
        return assignment
    out = dict(assignment)
    out["title"] = top.get("T") or assignment.get("title", "")
    if top.get("I") and assignment.get("instructions"):
        out["instructions"] = top["I"]
    out["questions"] = []
    for q in assignment["questions"]:
        t = (q.get("tr") or {}).get(code) or {}
        q = dict(q, prompt=t.get("p") or q["prompt"])
        if t.get("n") and q.get("info"):
            q["info"] = t["n"]
        if t.get("o") and q.get("options"):
            q["options"] = [(t["o"][j] if j < len(t["o"]) else "") or o for j, o in enumerate(q["options"])]
        out["questions"].append(q)
    return out


def only_languages(assignment: dict, codes) -> dict:
    """A copy with just these languages' translations: what phones get."""
    out = copy.deepcopy(assignment)
    for part in [out] + out["questions"]:
        if part.get("tr"):
            part["tr"] = {c: t for c, t in part["tr"].items() if c in codes}
    return out


def _pack_tr(tr) -> dict:
    """Translations without their stamps, and nothing empty."""
    out = {code: {k: v for k, v in t.items() if k != "s" and v} for code, t in (tr or {}).items()}
    return {code: t for code, t in out.items() if t}


def _clean_why(q: dict, raw) -> list:
    """The teacher's notes on wrong answers, cleaned; [] when there are none."""
    raw = raw if isinstance(raw, list) else []
    if q["type"] == "choice":
        notes = [_clean_str(n, MAX_WHY_CHARS) if isinstance(n, str) else "" for n in raw][:len(q["options"])]
        notes += [""] * (len(q["options"]) - len(notes))
        notes[q["answer"]] = ""
        return notes if any(notes) else []
    out = []
    for item in raw[:12]:
        if not isinstance(item, dict):
            continue
        x, w = _clean_str(item.get("x"), MAX_FIRST_CHARS), _clean_str(item.get("w"), MAX_WHY_CHARS)
        if x and w and grade_question(q, x) == 0 and (q["type"] == "text" or parse_number(x) is not None):
            out.append({"x": x, "w": w})
    return out


def normalize_assignment(data: dict) -> dict:
    """Check a teacher-built assignment and return a cleaned copy."""
    if not isinstance(data, dict):
        raise AssignmentError("assignment must be an object")
    title = _clean_str(data.get("title"), 120)
    if not title:
        raise AssignmentError("title is required")
    mode = data.get("mode", "graded")
    if mode not in ("graded", "practice"):
        raise AssignmentError("mode must be 'graded' or 'practice'")
    raw_questions = data.get("questions") or []
    if not raw_questions:
        raise AssignmentError("add at least one question")

    questions = []
    seen_ids = set()
    for index, q in enumerate(raw_questions, start=1):
        qtype = q.get("type")
        if qtype not in QUESTION_TYPES:
            raise AssignmentError(f"question {index}: unknown type {qtype!r}")
        prompt = _clean_str(q.get("prompt"), 500)
        if not prompt:
            raise AssignmentError(f"question {index}: prompt is required")
        qid = _clean_str(q.get("id"), 20) or f"q{index}"
        if qid in seen_ids:
            qid = f"q{index}"
        seen_ids.add(qid)
        try:
            points = float(q.get("points", 1))
        except (TypeError, ValueError):
            raise AssignmentError(f"question {index}: points must be a number")
        if points <= 0:
            raise AssignmentError(f"question {index}: points must be positive")
        clean = {"id": qid, "type": qtype, "prompt": prompt, "points": points}
        info = _clean_str(q.get("info"), MAX_INFO_CHARS)
        if info:
            clean["info"] = info
        pics = [p.strip() for p in str(q.get("pic") or "").replace("\n", ",").split(",") if _PIC.match(p.strip())][:12]
        if pics:
            clean["pic"] = ",".join(pics)

        if qtype == "choice":
            options = [_clean_str(o, 200) for o in q.get("options") or [] if _clean_str(o, 200)]
            if len(options) < 2:
                raise AssignmentError(f"question {index}: give at least two options")
            try:
                answer = int(q.get("answer"))
            except (TypeError, ValueError):
                raise AssignmentError(f"question {index}: pick the correct option")
            if not 0 <= answer < len(options):
                raise AssignmentError(f"question {index}: correct option is out of range")
            clean.update(options=options, answer=answer)
            opic = [str(p or "").strip() for p in q.get("opic") or []][:len(options)]
            opic = [p if _PIC.match(p) and "*" not in p else "" for p in opic]
            if any(opic):
                clean["opic"] = opic + [""] * (len(options) - len(opic))
        elif qtype == "number":
            try:
                clean["answer"] = float(q.get("answer"))
                clean["tolerance"] = abs(float(q.get("tolerance") or 0))
            except (TypeError, ValueError):
                raise AssignmentError(f"question {index}: answer and tolerance must be numbers")
        elif qtype == "text":
            accept = [_clean_str(a, MAX_TEXT_CHARS) for a in q.get("accept") or [] if _clean_str(a, MAX_TEXT_CHARS)]
            if not accept:
                raise AssignmentError(f"question {index}: list at least one accepted answer")
            clean["accept"] = accept
        else:  # long
            try:
                max_chars = int(q.get("max_chars") or 600)
            except (TypeError, ValueError):
                max_chars = 600
            clean["max_chars"] = max(50, min(max_chars, MAX_LONG_CHARS))
            # For the teacher's marking screen only: never packed for phones (see _Q_KEYS).
            model = _clean_str(q.get("model"), MAX_LONG_CHARS)
            if model:
                clean["model"] = model
        tr = _clean_tr(clean, q.get("tr"))
        if tr:
            clean["tr"] = tr
        why = _clean_why(clean, q.get("why")) if qtype != "long" else []
        if why:
            clean["why"] = why
        questions.append(clean)

    tr = _clean_tr(None, data.get("tr"))
    written = data.get("lang") if isinstance(data.get("lang"), str) and _CODE.match(data["lang"]) else ""
    return {
        **({"tr": tr} if tr else {}), **({"lang": written} if written else {}),
        "id": _clean_str(data.get("id"), 20) or new_id("a"),
        "title": title,
        "subject": _clean_str(data.get("subject"), 60),
        "course": _clean_str(data.get("course"), 24),
        "unit": _clean_str(data.get("unit"), 24),
        "instructions": _clean_str(data.get("instructions"), 2000),
        "due": _clean_str(data.get("due"), 10),
        "mode": mode,
        "questions": questions,
    }


# -- compact form for the radio and the course pack ---------------------------
#
# Short keys, numeric question types, and defaults left out. Packing is lossless, except that the
# teacher's own notes ("model", "why") are left out: otherwise unpack(pack(x)) == x.

_TOP_KEYS = {"id": "i", "title": "T", "subject": "S", "instructions": "I", "due": "D", "mode": "M", "version": "v",
             "course": "C", "unit": "U", "course_title": "Ct", "unit_title": "Ut", "unit_no": "Un"}
# Filled in by the hub for the phone's copy only; left out of unpacked assignments when empty.
_OPTIONAL_TOP = ("version", "course_title", "unit_title", "unit_no")
_Q_KEYS = {"id": "i", "prompt": "p", "options": "o", "answer": "a", "tolerance": "l", "accept": "c", "points": "s", "max_chars": "m",
           "pic": "g", "opic": "og"}
_TYPE_CODES = {t: n for n, t in enumerate(QUESTION_TYPES)}
_Q_DEFAULTS = {"points": 1.0, "tolerance": 0.0, "max_chars": 600}


def _num(value):
    return int(value) if isinstance(value, float) and value.is_integer() else value


def pack(assignment: dict) -> dict:
    out = {}
    for key, short in _TOP_KEYS.items():
        value = assignment.get(key)
        if key == "mode":
            if value == "practice":
                out[short] = "p"
        elif value not in (None, ""):
            out[short] = value
    questions = []
    info_before = ""
    for n, q in enumerate(assignment["questions"], start=1):
        cq = {"t": _TYPE_CODES[q["type"]]}
        for key, short in _Q_KEYS.items():
            if key not in q:
                continue
            value = q[key]
            if key == "id" and value == f"q{n}":
                continue
            if key in _Q_DEFAULTS and value == _Q_DEFAULTS[key]:
                continue
            cq[short] = _num(value)
        info = q.get("info") or ""
        if info:  # several questions about one text: it travels once, then 1 means "the same again"
            cq["n"] = 1 if info == info_before else info
        info_before = info
        if _pack_tr(q.get("tr")):
            cq["x"] = _pack_tr(q["tr"])
        questions.append(cq)
    out["Q"] = questions
    if _pack_tr(assignment.get("tr")):
        out["X"] = _pack_tr(assignment["tr"])
    return out


def unpack(packed: dict) -> dict:
    out = {key: packed.get(short, "") for key, short in _TOP_KEYS.items()}
    out["mode"] = "practice" if packed.get("M") == "p" else "graded"
    questions = []
    info_before = ""
    for n, cq in enumerate(packed.get("Q") or [], start=1):
        qtype = QUESTION_TYPES[int(cq.get("t", 0))]
        q = {"id": cq.get("i") or f"q{n}", "type": qtype, "prompt": cq.get("p", ""),
             "points": float(cq.get("s", _Q_DEFAULTS["points"]))}
        if "o" in cq:
            q["options"] = list(cq["o"])
        if "a" in cq:
            q["answer"] = int(cq["a"]) if qtype == "choice" else float(cq["a"])
        if qtype == "number" and "a" in cq:
            q["tolerance"] = float(cq.get("l", _Q_DEFAULTS["tolerance"]))
        if "c" in cq:
            q["accept"] = list(cq["c"])
        if qtype == "long":
            q["max_chars"] = int(cq.get("m", _Q_DEFAULTS["max_chars"]))
        if cq.get("g"):
            q["pic"] = str(cq["g"])
        if isinstance(cq.get("og"), list):
            q["opic"] = [str(p) for p in cq["og"]]
        info = cq.get("n") or ""
        info = info if isinstance(info, str) else info_before
        if info:
            q["info"] = info
        info_before = info
        if isinstance(cq.get("x"), dict):
            q["tr"] = cq["x"]
        questions.append(q)
    out["questions"] = questions
    if isinstance(packed.get("X"), dict):
        out["tr"] = packed["X"]
    for key in _OPTIONAL_TOP:
        if not out[key]:
            out.pop(key)
    return out


def version_of(assignment: dict) -> str:
    """Content stamp shared by hub and phone. The due date is left out so it can
    change with a one-packet OPEN message instead of a full resend."""
    view = dict(public_view(assignment))
    for key in ("due", "version", "course_title", "unit_title", "unit_no"):
        view.pop(key, None)
    canonical = json.dumps(pack(view), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return format(zlib.crc32(canonical.encode("utf-8")), "08x")


def max_points(assignment: dict) -> float:
    return sum(q["points"] for q in assignment["questions"])


def public_view(assignment: dict) -> dict:
    """The copy sent to the phone: answer key removed unless in practice mode."""
    view = copy.deepcopy(assignment)
    for q in view["questions"]:
        for field in OWN_FIELDS:
            q.pop(field, None)
    if view.get("mode") != "practice":
        for q in view["questions"]:
            for field in KEY_FIELDS:
                q.pop(field, None)
    return view


_NUMBER_RE = re.compile(r"^[-+]?(\d+([.,]\d*)?|[.,]\d+)([eE][-+]?\d+)?$")


def parse_number(text: str) -> Optional[float]:
    """Accept "3.5", "3,5" (comma decimals are common in Brazil and Germany) or "1/2"."""
    text = (text or "").strip().replace(" ", "")
    if "/" in text:
        num, _, den = text.partition("/")
        a, b = parse_number(num), parse_number(den)
        if a is None or b in (None, 0):
            return None
        return a / b
    if not _NUMBER_RE.match(text):
        return None
    return float(text.replace(",", "."))


def validate_answers(assignment: dict, raw: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Check answers are complete and well-formed. Returns (answers, errors)."""
    answers: Dict[str, str] = {}
    errors: Dict[str, str] = {}
    for q in assignment["questions"]:
        value = (raw.get(q["id"]) or "").strip()
        qtype = q["type"]
        if not value:
            errors[q["id"]] = "Please answer this question."
            continue
        if qtype == "choice":
            if not value.isdigit() or not 0 <= int(value) < len(q["options"]):
                errors[q["id"]] = "Choose one of the options."
                continue
        elif qtype == "number":
            if parse_number(value) is None:
                errors[q["id"]] = "Enter a number, like 12, 3.5, 3,5 or 1/2."
                continue
        elif qtype == "text":
            if len(value) > MAX_TEXT_CHARS:
                errors[q["id"]] = f"Keep this under {MAX_TEXT_CHARS} characters."
                continue
        elif qtype == "long":
            limit = q.get("max_chars", 600)
            if len(value) > limit:
                errors[q["id"]] = f"Keep this under {limit} characters ({len(value)} now)."
                continue
        answers[q["id"]] = value
    return answers, errors


def _normalize_text(value: str) -> str:
    return " ".join(value.lower().split()).strip(" .!?")


def grade_question(q: dict, value: str) -> Optional[float]:
    """Points earned, or None when the teacher must grade it."""
    qtype = q["type"]
    if qtype == "long":
        return None
    if qtype == "choice":
        return q["points"] if value.isdigit() and int(value) == q["answer"] else 0.0
    if qtype == "number":
        got = parse_number(value)
        if got is None:
            return 0.0
        return q["points"] if abs(got - q["answer"]) <= q.get("tolerance", 0) + 1e-9 else 0.0
    if qtype == "text":
        accepted = {_normalize_text(a) for a in q["accept"]}
        return q["points"] if _normalize_text(value) in accepted else 0.0
    return None


def grade(assignment: dict, answers: Dict[str, str], manual: Optional[Dict[str, float]] = None) -> dict:
    """Grade a submission. `manual` holds the teacher's scores: for long answers, and for any
    question the teacher re-marked (an answer the key did not foresee, say). Those win."""
    manual = manual or {}
    per_question: List[dict] = []
    total = 0.0
    needs_manual = False
    for q in assignment["questions"]:
        value = answers.get(q["id"], "")
        if q["id"] in manual:
            score = max(0.0, min(float(manual[q["id"]]), q["points"]))
        else:
            score = grade_question(q, value)
            if score is None:
                needs_manual = True
        if score is not None:
            total += score
        per_question.append({"id": q["id"], "score": score, "points": q["points"]})
    return {
        "score": round(total, 2),
        "max": max_points(assignment),
        "complete": not needs_manual,
        "questions": per_question,
    }


def same_answer(q: dict, a: str, b: str) -> bool:
    if q["type"] == "number":
        x, y = parse_number(a), parse_number(b)
        return x is not None and y is not None and abs(x - y) <= 1e-9 * max(1.0, abs(x), abs(y))
    if q["type"] == "text":
        return _normalize_text(a) == _normalize_text(b)
    return (a or "").strip() == (b or "").strip()


def wrong_note(q: dict, answer: str) -> str:
    """The teacher's note on this wrong answer, or ''."""
    why = q.get("why") or []
    if q["type"] == "choice":
        return why[int(answer)] if (answer or "").isdigit() and int(answer) < len(why) else ""
    exact = next((item["w"] for item in why if same_answer(q, item["x"], answer)), "")
    if exact or q["type"] != "number":
        return exact
    got = parse_number(answer)   # 2/6 is also written 0.33
    return next((item["w"] for item in why if "/" in item["x"] and got is not None
                 and abs(got - (parse_number(item["x"]) or 0)) <= 0.015 * abs(parse_number(item["x"]) or 0)), "")


def signals_of(assignment: dict, unsure, first_json: str, answers: Dict[str, str]) -> dict:
    """What goes with a student's answers, small enough for the radio: "n", the questions they ticked as not
    sure (one bit each, in order), and "f", their first answer where they changed it after moving on."""
    out: dict = {}
    mask = sum(1 << i for i, q in enumerate(assignment["questions"][:MAX_UNSURE]) if q["id"] in unsure)
    if mask:
        out["n"] = mask
    try:
        firsts = json.loads(first_json or "{}")
    except ValueError:
        firsts = {}
    changed = {}
    for q in assignment["questions"] if isinstance(firsts, dict) else []:
        value = firsts.get(q["id"])
        if q["type"] == "long" or not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()[:MAX_FIRST_CHARS]
        if q["type"] == "choice" and not (value.isdigit() and int(value) < len(q["options"])):
            continue
        if q["type"] == "number" and parse_number(value) is None:
            continue
        if not same_answer(q, value, answers.get(q["id"], "")):
            changed[q["id"]] = value
    if changed:
        out["f"] = changed
    return out


def read_signals(assignment: dict, signals) -> Tuple[set, Dict[str, str]]:
    """The ids of the questions a student was not sure of, and their first answers where they changed."""
    signals = signals if isinstance(signals, dict) else {}
    mask = signals.get("n")
    mask = mask if isinstance(mask, int) and mask > 0 else 0
    ids = [q["id"] for q in assignment["questions"]]
    first = signals.get("f")
    first = {k: v for k, v in first.items() if k in ids and isinstance(v, str)} if isinstance(first, dict) else {}
    return {qid for i, qid in enumerate(ids) if mask >> i & 1}, first


def answer_text(q: dict, answer: str) -> str:
    """An answer in words."""
    if q["type"] == "choice" and (answer or "").isdigit() and int(answer) < len(q.get("options", [])):
        return q["options"][int(answer)]
    return answer or ""


def practice_feedback(assignment: dict, answers: Dict[str, str]) -> Dict[str, Optional[bool]]:
    """For practice mode on the phone: True/False per auto-gradable question."""
    feedback: Dict[str, Optional[bool]] = {}
    for q in assignment["questions"]:
        if any(field in q for field in ("answer", "accept")):
            score = grade_question(q, answers.get(q["id"], ""))
            feedback[q["id"]] = None if score is None else score > 0
        else:
            feedback[q["id"]] = None
    return feedback


def key_text(q: dict) -> str:
    """The right answer of an auto-marked question, in words."""
    if q["type"] == "choice" and isinstance(q.get("answer"), int) and q["answer"] < len(q.get("options", [])):
        return q["options"][q["answer"]]
    if q["type"] == "number" and "answer" in q:
        tol = q.get("tolerance") or 0
        return f"{q['answer']:g}" + (f" (± {tol:g})" if tol else "")
    if q["type"] == "text" and q.get("accept"):
        return " or ".join(q["accept"])
    return ""
