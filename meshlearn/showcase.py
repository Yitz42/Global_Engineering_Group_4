"""Fill a demo hub and phone with a realistic school, to show what a fully set-up
MeshLearn looks like (python -m meshlearn demo --showcase).

Three community phones, thirteen students of different abilities, nine
assignments across Mathematics, Science and Reading and writing, graded work
and practice tries over the past three weeks, a few written answers still
waiting for the teacher, and the odd student rushing. Students on the demo
phone send their work through the real (simulated) radio, so their own pages
show grades that came back over the mesh. Everything is made up and seeded, so
every run looks the same.
"""

from __future__ import annotations

import json
import random
import time
from typing import Callable, Dict, List

from . import assignments as asg
from . import comprehension as cmp

DAY = 86400

ASSIGNMENTS: List[dict] = [
    {"id": "a-fractions1", "released": 20, "share": 0.95},  # the sample assignment (loaded by the demo)
    {"id": "a-eqfrac", "released": 18, "share": 0.9, "title": "Equivalent fractions practice", "course": "math",
     "unit": "fractions", "mode": "practice", "instructions": "Try as many times as you like. You will see right and wrong straight away.",
     "questions": [
         {"type": "choice", "prompt": "Which fraction is equal to 2/4?", "options": ["1/2", "2/3", "3/4"], "answer": 0},
         {"type": "number", "prompt": "Write 3/4 as a decimal.", "answer": 0.75, "tolerance": 0.001},
         {"type": "number", "prompt": "What is 1/3 + 1/3? (a fraction or a decimal)", "answer": 0.6667, "tolerance": 0.01},
         {"type": "choice", "prompt": "Which fraction is the biggest?", "options": ["1/3", "1/2", "1/4"], "answer": 1}]},
    {"id": "a-garden", "released": 15, "share": 0.9, "title": "Measuring the school garden", "course": "math",
     "unit": "measurement", "due": "2026-10-16",
     "instructions": "The school garden plot is a rectangle 6 m long and 4 m wide.",
     "questions": [
         {"type": "number", "prompt": "What is the area of the plot in square metres?", "answer": 24},
         {"type": "number", "prompt": "What is its perimeter in metres?", "answer": 20},
         {"type": "number", "prompt": "Fencing costs 150 per metre. How much does it cost to fence the whole plot?", "answer": 3000},
         {"type": "long", "prompt": "How could you measure the garden if you had no tape measure?", "points": 3}]},
    {"id": "a-rain", "released": 12, "share": 0.85, "title": "Rainfall in our village", "course": "math", "unit": "data",
     "due": "2026-10-20",
     "instructions": "Rainfall measured at the school: January 120 mm, February 95 mm, March 60 mm, April 30 mm.",
     "questions": [
         {"type": "number", "prompt": "What was the total rainfall from January to April, in mm?", "answer": 305},
         {"type": "number", "prompt": "What was the average rainfall per month, in mm?", "answer": 76.25, "tolerance": 0.1},
         {"type": "choice", "prompt": "Which month was the driest?", "options": ["January", "February", "March", "April"], "answer": 3},
         {"type": "long", "prompt": "Why does rainfall matter to farmers here?", "points": 3}]},
    {"id": "a-plants", "released": 10, "share": 0.85, "title": "How plants make food", "course": "science", "unit": "living",
     "due": "2026-10-22", "instructions": "Read 'How plants make food' in the Library first.",
     "questions": [
         {"type": "choice", "prompt": "Which gas do plants take in from the air?", "options": ["Oxygen", "Carbon dioxide", "Nitrogen"], "answer": 1},
         {"type": "text", "prompt": "What is the green substance in leaves that captures light?", "accept": ["chlorophyll", "clorofila"]},
         {"type": "choice", "prompt": "Where does photosynthesis mainly happen?", "options": ["Roots", "Leaves", "Flowers"], "answer": 1},
         {"type": "long", "prompt": "Why does a plant kept in a dark cupboard grow weak?", "points": 3}]},
    {"id": "a-water", "released": 6, "share": 0.6, "title": "The water cycle", "course": "science", "unit": "earth",
     "due": "2026-10-30",
     "questions": [
         {"type": "choice", "prompt": "What is it called when water turns into vapour?", "options": ["Condensation", "Evaporation", "Precipitation"], "answer": 1},
         {"type": "choice", "prompt": "Clouds form when water vapour…", "options": ["evaporates", "condenses", "melts"], "answer": 1},
         {"type": "text", "prompt": "Rain, snow and hail are all forms of what?", "accept": ["precipitation"]},
         {"type": "long", "prompt": "Where does the water in your village well come from?", "points": 3}]},
    {"id": "a-solar", "released": 5, "share": 0.55, "title": "Solar power at home", "course": "science", "unit": "energy",
     "mode": "practice",
     "questions": [
         {"type": "number", "prompt": "A 10 W solar panel is in full sun for 5 hours. How many watt-hours does it make?", "answer": 50},
         {"type": "choice", "prompt": "Which uses the least power?", "options": ["An LED bulb", "A kettle", "A fridge"], "answer": 0},
         {"type": "number", "prompt": "A phone battery needs 15 Wh. How many hours does a 5 W panel take to fill it?", "answer": 3}]},
    {"id": "a-alice", "released": 8, "share": 0.7, "title": "Reading: Alice's Adventures, chapter 1", "course": "literacy",
     "unit": "reading", "due": "2026-10-28", "instructions": "Read chapter 1 of Alice's Adventures in Wonderland in the Library.",
     "questions": [
         {"type": "choice", "prompt": "What does Alice follow down the hole?", "options": ["A cat", "A White Rabbit", "A bird"], "answer": 1},
         {"type": "text", "prompt": "What is written on the little bottle?", "accept": ["drink me"]},
         {"type": "long", "prompt": "How would you feel if you were Alice? Explain why.", "points": 4}]},
    {"id": "a-letter", "released": 2, "share": 0.3, "title": "A letter to a pen friend", "course": "literacy", "unit": "writing",
     "due": "2026-11-03",
     "questions": [
         {"type": "long", "prompt": "Write a letter to a pen friend in another country about a day in your village.",
          "points": 6, "max_chars": 1200}]},
]

PHONES = [("phone-1", "North village"), ("phone-2", "River school"), ("phone-3", "Hill farm")]
# name, phone, ability (chance of a right answer), habit
STUDENTS = [
    ("Ana", "phone-1", 0.93, ""), ("Bruno", "phone-1", 0.55, "rushes"), ("Kofi", "phone-1", 0.82, ""),
    ("Lena", "phone-1", 0.72, ""), ("Mateus", "phone-1", 0.48, "slow"),
    ("Faith", "phone-2", 0.88, ""), ("Grace", "phone-2", 0.76, ""), ("Hamza", "phone-2", 0.62, ""), ("João", "phone-2", 0.84, ""),
    ("Esther", "phone-3", 0.66, ""), ("Ivy", "phone-3", 0.95, "fast"), ("Nia", "phone-3", 0.52, ""), ("Daniel", "phone-3", 0.79, ""),
]
WRONG_WORDS = {"chlorophyll": ["sunlight", "water", "leaf juice"], "precipitation": ["evaporation", "clouds", "weather"],
               "drink me": ["eat me", "open me", "poison"]}
LONG_ANSWERS = [
    "Count my steps along each side, then measure one step with a stick I know the length of.",
    "Farmers need rain for the maize and beans to grow. Too little and the crops dry out, too much and the river floods the fields.",
    "Without light the plant cannot make sugar, so it has no energy to grow strong and green.",
    "The rain soaks into the ground and collects deep under the soil, and the well reaches down to it.",
    "I would feel scared but also curious, because everything is strange and I would want to know where the rabbit is going.",
    "Dear friend, today I woke early, fetched water from the well, walked to school and helped my mother in the garden.",
    "I am not sure.",
]


def _answer(q: dict, right: bool, rng: random.Random) -> str:
    if q["type"] == "choice":
        if right:
            return str(q["answer"])
        return str(rng.choice([i for i in range(len(q["options"])) if i != q["answer"]]))
    if q["type"] == "number":
        want = q["answer"]
        if right:
            text = f"{want:g}" if want != 0.6667 else "2/3"
            return text.replace(".", ",") if rng.random() < 0.3 else text
        slip = rng.choice(["sign", "place", "place", "rounding", "other"])
        if slip == "sign":
            return f"{-want:g}"
        if slip == "place":
            return f"{want * rng.choice([10, 0.1]):g}"
        if slip == "rounding":
            return f"{round(want * 1.015, 2):g}"
        return f"{round(want + rng.choice([-7, -3, 4, 9]), 2):g}"
    if q["type"] == "text":
        return q["accept"][0] if right else rng.choice(WRONG_WORDS.get(q["accept"][0], ["I don't know"]))
    return rng.choice(LONG_ANSWERS[:-1]) if right else rng.choice(LONG_ANSWERS)


def _expected_s(a: dict) -> float:
    return sum(cmp.EXPECTED.get(q["type"], 45) for q in a["questions"])


def seed(hub, phone, log: Callable[[str], None] = print, wait_s: float = 60.0) -> Dict[str, int]:
    """Fill `hub` and the demo `phone` (radio node phone-1). Returns counts."""
    rng = random.Random(4)
    now = time.time()
    made = {}
    for spec in ASSIGNMENTS:
        if spec["id"] == "a-fractions1":
            made[spec["id"]] = hub.get_assignment("a-fractions1")
            continue
        data = {k: v for k, v in spec.items() if k not in ("released", "share")}
        made[spec["id"]] = hub.create_assignment(data)
    for node, name in PHONES:
        hub.add_phone(node, name)
        hub.db.execute("UPDATE phones SET last_seen = ? WHERE node_id = ?", (now - rng.randint(300, 7200), node))

    # Every phone got the course pack at school; all but the newest assignment are open.
    phone.import_courses(hub.course_list_for_phones())
    for spec in ASSIGNMENTS:
        aid = spec["id"]
        version = hub.current_version(aid)
        for node, _ in PHONES:
            hub._set_holding(node, aid, version=version, open=1, action="open")
        phone.import_assignment(hub.packed_for_phone(aid), open_=True)

    def answers_for(a, ability, rushing):
        p = ability * (0.45 if rushing else 1)
        return {q["id"]: _answer(q, rng.random() < p, rng) for q in a["questions"]}

    def duration_for(a, habit, rushing):
        base = _expected_s(a) * rng.uniform(0.5, 1.4)
        if rushing:
            return round(_expected_s(a) * rng.uniform(0.08, 0.2))
        if habit == "slow":
            return round(base * rng.uniform(2.5, 5))
        if habit == "fast" and rng.random() < 0.3:
            return round(_expected_s(a) * rng.uniform(0.1, 0.2))
        return round(base)

    to_grade = []  # (student, aid, sub_id or None, answers) for written answers the teacher marks
    radio_subs = 0
    for name, node, ability, habit in STUDENTS:
        for spec in ASSIGNMENTS:
            a = made[spec["id"]]
            if rng.random() > spec["share"]:
                continue
            tries = rng.randint(1, 3) if a.get("mode") == "practice" else 1
            start = now - spec["released"] * DAY + rng.uniform(0.2, 3.5) * DAY
            for t in range(tries):
                at = min(start + t * rng.uniform(600, 7200), now - 600)
                rushing = habit == "rushes" and rng.random() < 0.35
                answers = answers_for(a, min(0.98, ability + 0.12 * t), rushing)
                duration = duration_for(a, habit, rushing)
                if node == "phone-1":  # through the real radio path
                    sub_id = phone.submit(phone.get_assignment(a["id"]), name, answers, duration=duration)
                    phone.db.execute("UPDATE submissions SET submitted_at = ? WHERE sub_id = ?", (at, sub_id))
                    radio_subs += 1
                else:  # phones out of range in the demo: as if their work had already arrived
                    sub_id = asg.new_id("s")
                    hub.db.execute(
                        "INSERT INTO submissions (sub_id, assignment_id, version, student, phone, answers, submitted_at, "
                        "received_at, duration) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (sub_id, a["id"], hub.current_version(a["id"]), name, node, json.dumps(answers), at,
                         at + rng.uniform(600, 4 * 3600), duration))
                    hub.grade_submission(sub_id)
                if any(q["type"] == "long" for q in a["questions"]):
                    to_grade.append((name, a, sub_id, ability, spec["released"]))

    log(f"  sending {radio_subs} submissions from the demo phone by radio…")
    while phone.flush_submissions(force=True):
        pass
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if hub.db.one("SELECT COUNT(*) AS n FROM submissions WHERE phone = 'phone-1'")["n"] >= radio_subs:
            break
        time.sleep(0.5)

    # The teacher has read most written answers; the newest still wait.
    graded = 0
    for name, a, sub_id, ability, released in to_grade:
        if released <= 2 or rng.random() < 0.12:
            continue
        row = hub.db.one("SELECT answers FROM submissions WHERE sub_id = ?", (sub_id,))
        if not row:
            continue
        given = json.loads(row["answers"])
        manual = {}
        for q in a["questions"]:
            if q["type"] == "long":
                quality = 0.25 if given.get(q["id"]) == LONG_ANSWERS[-1] else min(1.0, ability + rng.uniform(-0.15, 0.15))
                manual[q["id"]] = round(q["points"] * quality * 2) / 2
        hub.grade_submission(sub_id, manual)
        node = hub.db.one("SELECT phone FROM submissions WHERE sub_id = ?", (sub_id,))["phone"]
        if node == "phone-1":
            hub._send_results(node, [sub_id])
        graded += 1
    deadline = time.time() + wait_s
    while time.time() < deadline and hub.messenger.status()["pending"]:
        time.sleep(0.5)
    total = hub.db.one("SELECT COUNT(*) AS n FROM submissions")["n"]
    return {"assignments": len(made), "students": len(STUDENTS), "submissions": total, "graded_by_teacher": graded}
