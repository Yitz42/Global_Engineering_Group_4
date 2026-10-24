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

In "graded" mode the answer key never leaves the hub: the phone only checks
that answers are complete and well-formed, and the hub grades them. In
"practice" mode the key goes to the phone so students get instant feedback.
"long" answers are always graded by the teacher.
"""

from __future__ import annotations

import copy
import re
import secrets
from typing import Dict, List, Optional, Tuple

QUESTION_TYPES = ("choice", "number", "text", "long")
KEY_FIELDS = ("answer", "tolerance", "accept")
MAX_TEXT_CHARS = 200
MAX_LONG_CHARS = 1500


class AssignmentError(ValueError):
    pass


def new_id(prefix: str = "") -> str:
    return prefix + secrets.token_hex(4)


def _clean_str(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


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
        questions.append(clean)

    return {
        "id": _clean_str(data.get("id"), 20) or new_id("a"),
        "title": title,
        "subject": _clean_str(data.get("subject"), 60),
        "instructions": _clean_str(data.get("instructions"), 2000),
        "due": _clean_str(data.get("due"), 10),
        "mode": mode,
        "questions": questions,
    }


def max_points(assignment: dict) -> float:
    return sum(q["points"] for q in assignment["questions"])


def public_view(assignment: dict) -> dict:
    """The copy sent to the phone: answer key removed unless in practice mode."""
    view = copy.deepcopy(assignment)
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
    """Grade a submission. `manual` holds teacher scores for long answers."""
    manual = manual or {}
    per_question: List[dict] = []
    total = 0.0
    needs_manual = False
    for q in assignment["questions"]:
        value = answers.get(q["id"], "")
        score = grade_question(q, value)
        if score is None:
            if q["id"] in manual:
                score = max(0.0, min(float(manual[q["id"]]), q["points"]))
            else:
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
