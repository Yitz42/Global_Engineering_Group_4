"""How well each student understands each unit and course, worked out from
their graded answers. Follows the Engineering Mechanics Sandbox's rules so the
two projects read the same way. Pure functions: records in, numbers out.

A record is one graded submission:
    {"student", "aid", "course", "unit", "mode", "at" (time submitted), "duration" (s or None),
     "questions": [{"id", "type", "points", ...answer key on the hub}],
     "answers": {qid: text}, "scores": {qid: points or None (not graded yet)}}

Scoring each question (0 to 1):
  graded assignment   points earned / points possible
  practice assignment 1.0 right first time, 0.6 right on the second try,
                      0.3 right later, 0 never right (as the sandbox scores tries)
A unit's comprehension is the average of the student's 20 most recent question
scores in it, so it follows them as they improve. A course's is the average of
its units that have answers.

Mistakes are sorted into kinds, each in an area, so the teacher sees *where*
students go wrong, not just how often. Timing gives signals worth a look, never
proof: rushing (fast and wrong), very fast and right, very slow.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Tuple

from .assignments import parse_number

RECENT = 20
LEVELS = ("Not started", "Just started", "Needs work", "Developing", "Good", "Strong")

AREAS = {
    "calculation": {"label": "Calculation", "about": "Working out numbers: signs, place value, rounding, upside-down fractions."},
    "understanding": {"label": "Understanding", "about": "Choosing the wrong idea in a multiple-choice question."},
    "recall": {"label": "Recall", "about": "Short answers: the right word or fact."},
    "explaining": {"label": "Explaining", "about": "Written answers the teacher marked below half."},
    "unknown": {"label": "Not identified", "about": "Wrong numbers that matched no known slip."},
}
KINDS = {
    "sign": ("calculation", "Sign (+ or −) flipped"),
    "place": ("calculation", "Place value (off by 10, 100, 1000…)"),
    "rounding": ("calculation", "Rounded too early or too far"),
    "inverted": ("calculation", "Fraction upside down"),
    "concept": ("understanding", "Wrong choice"),
    "recall": ("recall", "Wrong word or fact"),
    "explaining": ("explaining", "Written answer below half marks"),
    "unexplained": ("unknown", "Wrong number, cause unknown"),
}

# Seconds a typical student needs per question, to spot answers far off pace.
EXPECTED = {"choice": 30, "number": 60, "text": 30, "long": 180}
FAST, SLOW = 0.25, 4.0
AWAY_S = 60 * 60  # longer than an hour: probably left it open, not slow


def level_of(score: Optional[float], answered: int) -> str:
    """The sandbox's words for a score: Strong, Good, Developing, Needs work."""
    if score is None or answered == 0:
        return "Not started"
    if answered < 3:
        return "Just started"
    if score >= 85:
        return "Strong"
    if score >= 65:
        return "Good"
    if score >= 40:
        return "Developing"
    return "Needs work"


def _mean(xs: List[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def question_scores(records: Iterable[dict]) -> List[dict]:
    """Every scored question: {student, course, unit, qid, type, score (0-1), at}."""
    out: List[dict] = []
    practice: Dict[Tuple[str, str], List[dict]] = {}
    for r in records:
        if r.get("mode") == "practice":
            practice.setdefault((r["student"], r["aid"]), []).append(r)
            continue
        for q in r["questions"]:
            got = r["scores"].get(q["id"])
            if got is None or not q.get("points"):
                continue
            out.append({"student": r["student"], "course": r.get("course", ""), "unit": r.get("unit", ""),
                        "qid": q["id"], "type": q["type"], "score": max(0.0, min(1.0, got / q["points"])), "at": r["at"]})
    for tries in practice.values():
        tries.sort(key=lambda r: r["at"])
        first = tries[0]
        for q in first["questions"]:
            right_at = next((i for i, t in enumerate(tries)
                             if t["scores"].get(q["id"]) is not None and t["scores"][q["id"]] >= q["points"]), None)
            if right_at is None and all(t["scores"].get(q["id"]) is None for t in tries):
                continue  # never graded (a written answer the teacher hasn't read)
            score = 0.0 if right_at is None else (1.0 if right_at == 0 else 0.6 if right_at == 1 else 0.3)
            last = tries[right_at] if right_at is not None else tries[-1]
            out.append({"student": first["student"], "course": first.get("course", ""), "unit": first.get("unit", ""),
                        "qid": q["id"], "type": q["type"], "score": score, "at": last["at"]})
    out.sort(key=lambda s: s["at"])
    return out


def diagnose(question: dict, answer: str, earned: Optional[float]) -> Optional[str]:
    """The kind of mistake in one answer, or None if it was right (or not graded)."""
    if earned is None or earned >= question.get("points", 1):
        return None
    qtype = question["type"]
    if qtype == "choice":
        return "concept"
    if qtype == "text":
        return "recall"
    if qtype == "long":
        return "explaining" if earned < question["points"] / 2 else None
    got, want = parse_number(answer or ""), question.get("answer")
    if got is None or want is None:
        return "unexplained"
    if want != 0 and math.isclose(got, -want, rel_tol=1e-6):
        return "sign"
    if want != 0 and got != 0:
        ratio = got / want
        power = math.log10(abs(ratio)) if ratio > 0 else None
        if power is not None and abs(power) >= 0.99 and abs(power - round(power)) < 1e-6:
            return "place"
        if abs(ratio - 1) <= 0.02:
            return "rounding"
        if math.isclose(got, 1 / want, rel_tol=1e-6):
            return "inverted"
    return "unexplained"


def mistakes(records: Iterable[dict]) -> Dict[str, Dict[str, int]]:
    """Counts by area and by kind, from answers that lost marks (needs the answer key)."""
    by_kind: Dict[str, int] = {}
    for r in records:
        for q in r["questions"]:
            kind = diagnose(q, r["answers"].get(q["id"], ""), r["scores"].get(q["id"]))
            if kind:
                by_kind[kind] = by_kind.get(kind, 0) + 1
    by_area: Dict[str, int] = {}
    for kind, n in by_kind.items():
        area = KINDS[kind][0]
        by_area[area] = by_area.get(area, 0) + n
    return {"areas": by_area, "kinds": by_kind}


def pace(record: dict) -> Optional[str]:
    """'rushing', 'fast_right' or 'slow' for one submission, or None."""
    duration = record.get("duration")
    if not duration or duration <= 0 or duration > AWAY_S:
        return None
    expected = sum(EXPECTED.get(q["type"], 45) for q in record["questions"])
    possible = sum(q.get("points", 0) for q in record["questions"] if record["scores"].get(q["id"]) is not None)
    earned = sum(record["scores"][q["id"]] for q in record["questions"] if record["scores"].get(q["id"]) is not None)
    fraction = earned / possible if possible else None
    if duration < FAST * expected and fraction is not None:
        if fraction < 0.5:
            return "rushing"
        if fraction >= 0.9:
            return "fast_right"
    if duration > SLOW * expected:
        return "slow"
    return None


PACE_LABELS = {
    "rushing": "rushing (fast and wrong)",
    "fast_right": "very fast and right (worth a look)",
    "slow": "much slower than expected (stuck?)",
}


def summarize(records: List[dict], course: dict) -> dict:
    """One course: overall and per unit, for all students together and for each student.

    Returns {"score", "level", "answered", "units": [...], "students": {name: {...}},
             "mistakes": {...}, "pace": {signal: count}}.
    """
    scores = question_scores(records)
    mine = [r for r in records if r.get("course", "") == course["id"]]
    unit_ids = [u["id"] for u in course.get("units", [])] or sorted({r.get("unit", "") for r in mine})

    def block(qs: List[dict], recs: List[dict]) -> dict:
        units = []
        for n, uid in enumerate(unit_ids, start=1):
            uq = [q for q in qs if q["unit"] == uid][-RECENT:]
            m = _mean([q["score"] for q in uq])
            unit = next((u for u in course.get("units", []) if u["id"] == uid), {"id": uid, "title": uid or "No unit"})
            units.append({"id": uid, "number": n, "title": unit["title"], "answered": len(uq),
                          "score": None if m is None else round(m * 100),
                          "level": level_of(None if m is None else m * 100, len(uq))})
        scored = [u for u in units if u["score"] is not None]
        score = round(_mean([u["score"] for u in scored])) if scored else None
        answered = sum(u["answered"] for u in units)
        signals: Dict[str, int] = {}
        for r in recs:
            sig = pace(r)
            if sig:
                signals[sig] = signals.get(sig, 0) + 1
        return {"score": score, "level": level_of(score, answered), "answered": answered, "units": units,
                "mistakes": mistakes(recs), "pace": signals}

    course_qs = [q for q in scores if q["course"] == course["id"]]
    out = block(course_qs, mine)
    out["students"] = {}
    for student in sorted({r["student"] for r in mine}, key=str.lower):
        out["students"][student] = block([q for q in course_qs if q["student"] == student],
                                         [r for r in mine if r["student"] == student])
    return out
