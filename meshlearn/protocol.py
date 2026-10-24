"""Wire format for messages sent over the mesh.

LoRa packets are small: the Meshtastic library accepts up to 233 bytes of
payload. A message is a JSON pair [type, payload], compressed with zlib using a
preset dictionary of words common in assignments (so even short messages
compress well), then split into numbered frames:

    magic (1) | version << 4 | flags (1) | message id (4) | seq (1) | total (1) | data

zlib's own Adler-32 checksum, verified on decompression, catches corruption.

The END flag marks the last frame of each transmission burst. When a receiver
sees it but is still missing pieces, it asks for exactly those pieces (NACK)
instead of the sender repeating the whole message.
"""

from __future__ import annotations

import json
import os
import struct
import time
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MAGIC = 0xA7
VERSION = 2
FLAG_END = 0x01
HEADER = struct.Struct(">BBIBB")
MAX_FRAME_BYTES = 228  # under Meshtastic's 233-byte payload limit
CHUNK_BYTES = MAX_FRAME_BYTES - HEADER.size
MAX_FRAMES = 255

# Message types
HELLO = "h"    # phone -> hub: name, free space, which assignment versions it holds
ASSIGN = "a"   # hub -> phone: a full assignment (new or changed)
OPEN = "o"     # hub -> phone: show/hide an assignment the phone already holds (1 packet)
NEED = "n"     # phone -> hub: "you opened something I don't have; please send it"
SUBMIT = "s"   # phone -> hub: a batch of student submissions
RESULT = "r"   # hub -> phone: a batch of grades
ACK = "k"      # message received in full
NACK = "x"     # message incomplete: these pieces are missing

# Preset compression dictionary. Both ends must hold exactly these bytes, so
# changing it means bumping VERSION. zlib favours matches near the end, so the
# most common fragments come last.
ZDICT = (
    " because between example community explain describe compare answer question correct "
    "Qual O que Por que Explique Descreva resposta pergunta Welche Was Warum Erkläre Beschreibe Antwort Frage "
    "the water cycle energy plants people school family river food sun light number fraction decimal "
    "How many What is the Which of these Which statement is true? In your own words, In one word, "
    "Read the lesson in the Library first. Answer every question. Give one example from daily life. "
    '"Q":[{"t":0,"p":"Which ","o":["","",""],"a":0},{"t":1,"p":"What is ","a":0,"l":0},'
    '{"t":2,"p":"What ","c":[""]},{"t":3,"p":"Explain ","s":3}],'
    '"T":"","S":"","I":"","D":"2026-","M":"p","v":"","i":"a'
    '["s",{"s":[{"id":"s","a":"a","v":"","u":"","x":{"q1":"","q2":"","q3":"","q4":"","q5":""},"t":1}]}]'
    '["r",{"r":[{"id":"s","sc":0,"mx":0,"ok":1,"q":{"q1":1,"q2":0}}]}]'
    '["o",{"a":"a","v":"","o":1,"d":"2026-"}]["a",{"a":{"i":"a","v":"","T":"'
).encode("utf-8")


class ProtocolError(ValueError):
    pass


@dataclass
class Message:
    msg_id: int
    type: str
    payload: dict
    src: str = ""


def new_msg_id() -> int:
    return struct.unpack(">I", os.urandom(4))[0]


def compress(raw: bytes) -> bytes:
    co = zlib.compressobj(9, zlib.DEFLATED, 15, 9, zlib.Z_DEFAULT_STRATEGY, ZDICT)
    return co.compress(raw) + co.flush()


def decompress(body: bytes) -> bytes:
    do = zlib.decompressobj(15, zdict=ZDICT)
    out = do.decompress(body) + do.flush()
    if not do.eof:
        raise zlib.error("truncated stream")
    return out


def encode(msg_type: str, payload: dict, msg_id: Optional[int] = None) -> Tuple[int, List[bytes]]:
    """Turn a message into frames that each fit one LoRa packet. The last frame carries END."""
    if msg_id is None:
        msg_id = new_msg_id()
    raw = json.dumps([msg_type, payload], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    body = compress(raw)
    chunks = [body[i:i + CHUNK_BYTES] for i in range(0, len(body), CHUNK_BYTES)] or [b""]
    if len(chunks) > MAX_FRAMES:
        raise ProtocolError(
            f"message needs {len(chunks)} packets; the limit is {MAX_FRAMES} "
            f"(about {MAX_FRAMES * CHUNK_BYTES // 1024} KB compressed)"
        )
    total = len(chunks)
    frames = [_frame(msg_id, seq, total, chunk, FLAG_END if seq == total - 1 else 0) for seq, chunk in enumerate(chunks)]
    return msg_id, frames


def _frame(msg_id: int, seq: int, total: int, chunk: bytes, flags: int) -> bytes:
    return HEADER.pack(MAGIC, (VERSION << 4) | flags, msg_id, seq, total) + chunk


def set_end(frame: bytes, end: bool = True) -> bytes:
    """Mark (or unmark) a frame as the last one in a burst."""
    verflags = frame[1] | FLAG_END if end else frame[1] & ~FLAG_END
    return frame[:1] + bytes([verflags]) + frame[2:]


def frame_count(msg_type: str, payload: dict) -> int:
    return len(encode(msg_type, payload, msg_id=0)[1])


@dataclass
class FrameInfo:
    msg_id: int
    seq: int
    total: int
    end: bool
    data: bytes


def parse_header(frame: bytes) -> FrameInfo:
    if len(frame) < HEADER.size:
        raise ProtocolError("frame too short")
    magic, verflags, msg_id, seq, total = HEADER.unpack_from(frame)
    if magic != MAGIC:
        raise ProtocolError("not a MeshLearn frame")
    if verflags >> 4 != VERSION:
        raise ProtocolError(f"unsupported protocol version {verflags >> 4}")
    if total == 0 or seq >= total:
        raise ProtocolError("bad frame numbering")
    return FrameInfo(msg_id, seq, total, bool(verflags & FLAG_END), frame[HEADER.size:])


def decode_body(msg_id: int, body: bytes, src: str = "") -> Message:
    try:
        msg_type, payload = json.loads(decompress(body).decode("utf-8"))
    except (zlib.error, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise ProtocolError(f"corrupt message body: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(msg_type, str):
        raise ProtocolError("malformed message")
    return Message(msg_id=msg_id, type=msg_type, payload=payload, src=src)


@dataclass
class _Partial:
    total: int
    chunks: Dict[int, bytes] = field(default_factory=dict)
    first_seen: float = field(default_factory=time.monotonic)


@dataclass
class AddResult:
    message: Optional[Message] = None
    missing: Optional[List[int]] = None  # set when a burst ended with pieces still missing


class Assembler:
    """Collects frames from many senders; returns a message once complete,
    or the list of missing pieces when a burst ends short."""

    def __init__(self, timeout_s: float = 3600.0):
        self.timeout_s = timeout_s
        self._partials: Dict[Tuple[str, int], _Partial] = {}

    def add(self, frame: bytes, src: str) -> AddResult:
        info = parse_header(frame)
        self._expire()
        key = (src, info.msg_id)
        partial = self._partials.get(key)
        if partial is None or partial.total != info.total:
            partial = self._partials[key] = _Partial(total=info.total)
        partial.chunks[info.seq] = info.data
        if len(partial.chunks) < info.total:
            if info.end:
                return AddResult(missing=[i for i in range(info.total) if i not in partial.chunks])
            return AddResult()
        del self._partials[key]
        body = b"".join(partial.chunks[i] for i in range(info.total))
        return AddResult(message=decode_body(info.msg_id, body, src))

    def pending(self) -> int:
        return len(self._partials)

    def _expire(self) -> None:
        cutoff = time.monotonic() - self.timeout_s
        for key in [k for k, p in self._partials.items() if p.first_seen < cutoff]:
            del self._partials[key]
