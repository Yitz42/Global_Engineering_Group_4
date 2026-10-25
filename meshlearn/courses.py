"""Courses and their numbered units, the frame that assignments hang on.

Starter courses come from courses.json (edited by the team). Teachers can add
and change courses on the hub; once a course is changed there, the hub's copy
is kept even if the file changes. Assignments name a course and unit by id, so
ids must not change once in use (change the title instead).
"""

from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional

COURSE_FILE = os.path.join(os.path.dirname(__file__), "courses.json")
OTHER = {"id": "", "title": "Other assignments", "description": "Assignments not filed under a course.", "units": []}


class CourseError(ValueError):
    pass


def slug(text: str, fallback: str = "x") -> str:
    out = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:24]
    return out or fallback


def normalize_course(data: dict) -> dict:
    if not isinstance(data, dict):
        raise CourseError("a course must be an object")
    title = str(data.get("title") or "").strip()[:80]
    if not title:
        raise CourseError("the course needs a title")
    course_id = slug(data.get("id") or title, "course")
    units, seen = [], set()
    for n, u in enumerate(data.get("units") or [], start=1):
        if isinstance(u, str):
            u = {"title": u}
        unit_title = str(u.get("title") or "").strip()[:80]
        if not unit_title:
            continue
        unit_id = slug(u.get("id") or unit_title, f"unit{n}")
        while unit_id in seen:
            unit_id += "-2"
        seen.add(unit_id)
        unit = {"id": unit_id, "title": unit_title}
        if str(u.get("read") or "").strip():
            unit["read"] = str(u["read"]).strip()[:60]
        units.append(unit)
    course = {"id": course_id, "title": title, "description": str(data.get("description") or "").strip()[:240],
              "units": units}
    if data.get("comingSoon"):
        course["comingSoon"] = True
    return course


def load_file(path: str = COURSE_FILE) -> List[dict]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return [normalize_course(c) for c in data.get("courses", [])]


def unit_of(course: Optional[dict], unit_id: str) -> Optional[dict]:
    if not course:
        return None
    return next((u for u in course["units"] if u["id"] == unit_id), None)


def unit_number(course: Optional[dict], unit_id: str) -> str:
    """'2' for the second unit of a course, '' if not found."""
    if not course:
        return ""
    for n, u in enumerate(course["units"], start=1):
        if u["id"] == unit_id:
            return str(n)
    return ""


def by_id(courses: List[dict]) -> Dict[str, dict]:
    return {c["id"]: c for c in courses}
