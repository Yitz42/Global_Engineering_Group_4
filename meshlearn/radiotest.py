"""Bench test for two real Meshtastic radios plugged into one computer.

python -m meshlearn radio-test --a /dev/ttyUSB0 --b /dev/ttyUSB1 --channel 3

Radio A plays the hub and radio B the phone. The test sends the messages
MeshLearn really uses (opening an assignment, a batch of answers, a full
assignment, a batch of grades) through the same messenger the hub and phone
use, and reports how long each took and how many packets it cost.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Dict, List

from . import assignments as asg
from . import protocol
from .messenger import Messenger
from .transport import MeshtasticTransport

SAMPLE = os.path.join(os.path.dirname(__file__), "sample_assignment.json")


def _messages(assignment: dict) -> List[tuple]:
    """(label, sender 'a' or 'b', type, payload), in the order a school day would send them."""
    packed = asg.pack(assignment)
    aid, version = assignment["id"], asg.version_of(assignment)
    answers = {"q1": "3", "q2": "0,75", "q3": "chlorophyll", "q4": "1",
               "q5": "When we share a loaf of bread between four of us, each person gets one quarter."}
    now = round(time.time())
    batch = [{"id": f"s{i:07x}", "a": aid, "v": version, "u": name, "x": answers, "t": now - 60 * i, "d": 240 + 17 * i}
             for i, name in enumerate(["Ana", "Bruno", "Kofi", "Lena", "Mateus"])]
    grades = [{"id": b["id"], "sc": 5, "mx": 7, "ok": 1, "q": {"q1": 1, "q2": 1, "q3": 1, "q4": 0, "q5": 2}} for b in batch]
    return [
        ("Open an assignment the phone already has", "a", protocol.OPEN, {"a": aid, "v": version, "o": 1, "d": "2026-11-20"}),
        ("A batch of 5 students' answers", "b", protocol.SUBMIT, {"s": batch}),
        ("A full assignment the phone doesn't have", "a", protocol.ASSIGN, {"a": packed, "o": 1, "d": "2026-11-20"}),
        ("A batch of 5 grades", "a", protocol.RESULT, {"r": grades}),
    ]


def run(dev_a: str, dev_b: str, channel: int, hops, timeout_s: float, retry_s: float, log=print) -> int:
    log(f"Opening radio A ({dev_a}) and radio B ({dev_b})…")
    radios = {"a": MeshtasticTransport(device=dev_a, channel=channel, hop_limit=hops),
              "b": MeshtasticTransport(device=dev_b, channel=channel, hop_limit=hops)}
    ids = {k: r.node_id for k, r in radios.items()}
    log(f"  A is {ids['a']}, B is {ids['b']}; channel {channel}, "
        f"{'node default' if hops is None else hops} hops\n")

    arrived: Dict[int, float] = {}
    got = threading.Condition()

    def handler(message: protocol.Message) -> None:
        with got:
            arrived[message.msg_id] = time.monotonic()
            got.notify_all()

    folder = tempfile.mkdtemp(prefix="meshlearn-radiotest-")
    messengers = {k: Messenger(r, os.path.join(folder, f"{k}.db"), handler, retry_s=retry_s) for k, r in radios.items()}
    for m in messengers.values():
        m.start()

    with open(SAMPLE) as fh:
        assignment = asg.normalize_assignment(json.load(fh))
    rows, failures = [], 0
    try:
        for label, sender, msg_type, payload in _messages(assignment):
            other = "b" if sender == "a" else "a"
            frames = protocol.frame_count(msg_type, payload)
            sent_before = messengers[sender].frames_sent + messengers[other].frames_sent
            log(f"{label} ({'A→B' if sender == 'a' else 'B→A'}, {frames} packet{'s' if frames != 1 else ''})…")
            start = time.monotonic()
            msg_id = messengers[sender].send(ids[other], msg_type, payload)
            deadline = start + timeout_s
            delivered = acked = None
            while time.monotonic() < deadline:
                with got:
                    if delivered is None and msg_id in arrived:
                        delivered = arrived[msg_id] - start
                if messengers[sender].message_status(msg_id) == "acked":
                    acked = time.monotonic() - start
                    break
                time.sleep(0.2)
            on_air = messengers[sender].frames_sent + messengers[other].frames_sent - sent_before
            if acked is None:
                failures += 1
                log(f"  NOT CONFIRMED after {timeout_s:.0f} s" + (f" (arrived after {delivered:.1f} s)" if delivered else ""))
            else:
                log(f"  arrived after {delivered or acked:.1f} s, confirmed after {acked:.1f} s, "
                    f"{on_air} packets on air including resends and acknowledgements")
            rows.append((label, frames, delivered, acked, on_air))
    finally:
        for m in messengers.values():
            m.stop()

    log("\n| Message | Packets needed | Arrived (s) | Confirmed (s) | Packets on air |")
    log("|---|---|---|---|---|")
    for label, frames, delivered, acked, on_air in rows:
        fmt = lambda v: f"{v:.1f}" if v is not None else "—"
        log(f"| {label} | {frames} | {fmt(delivered or acked)} | {fmt(acked)} | {on_air} |")
    log(f"\n{len(rows) - failures} of {len(rows)} messages confirmed.")
    return 1 if failures else 0
