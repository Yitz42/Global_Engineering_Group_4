"""Wire format for messages sent over the mesh.

LoRa packets are small (Meshtastic allows roughly 230 bytes of payload), so a
message is JSON, zlib-compressed, then split into numbered frames:

    magic "ML" | version (1 byte) | message id (4 bytes) | seq (1) | total (1) | data

The receiver reassembles frames by message id, checks a CRC32 over the
decompressed payload, and hands back a complete Message.
"""

from __future__ import annotations

import json
import os
import struct
import time
import zlib
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MAGIC = b"ML"
VERSION = 1
HEADER = struct.Struct(">2sBIBB")
MAX_FRAME_BYTES = 200
CHUNK_BYTES = MAX_FRAME_BYTES - HEADER.size
MAX_FRAMES = 255

# Message types
HELLO = "hello"        # phone -> hub: announce itself
ASSIGN = "assign"      # hub -> phone: an assignment to host
SUBMIT = "submit"      # phone -> hub: a student's answers
RESULT = "result"      # hub -> phone: grade for a submission
ACK = "ack"            # either way: message received


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


def _payload_crc(payload: dict) -> int:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return zlib.crc32(canonical.encode("utf-8"))


def encode(msg_type: str, payload: dict, msg_id: Optional[int] = None) -> Tuple[int, List[bytes]]:
    """Turn a message into a list of frames small enough for one LoRa packet each."""
    if msg_id is None:
        msg_id = new_msg_id()
    envelope = {"t": msg_type, "p": payload, "c": _payload_crc(payload)}
    raw = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    body = zlib.compress(raw, 9)
    chunks = [body[i:i + CHUNK_BYTES] for i in range(0, len(body), CHUNK_BYTES)] or [b""]
    if len(chunks) > MAX_FRAMES:
        raise ProtocolError(
            f"message needs {len(chunks)} frames; the limit is {MAX_FRAMES} "
            f"(about {MAX_FRAMES * CHUNK_BYTES // 1024} KB compressed)"
        )
    total = len(chunks)
    frames = [HEADER.pack(MAGIC, VERSION, msg_id, seq, total) + chunk for seq, chunk in enumerate(chunks)]
    return msg_id, frames


def parse_header(frame: bytes) -> Tuple[int, int, int, bytes]:
    if len(frame) < HEADER.size:
        raise ProtocolError("frame too short")
    magic, version, msg_id, seq, total = HEADER.unpack_from(frame)
    if magic != MAGIC:
        raise ProtocolError("not a MeshLearn frame")
    if version != VERSION:
        raise ProtocolError(f"unsupported protocol version {version}")
    if total == 0 or seq >= total:
        raise ProtocolError("bad frame numbering")
    return msg_id, seq, total, frame[HEADER.size:]


def decode_body(msg_id: int, body: bytes, src: str = "") -> Message:
    try:
        envelope = json.loads(zlib.decompress(body).decode("utf-8"))
    except (zlib.error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"corrupt message body: {exc}") from exc
    payload = envelope.get("p")
    if not isinstance(payload, dict) or _payload_crc(payload) != envelope.get("c"):
        raise ProtocolError("checksum mismatch")
    return Message(msg_id=msg_id, type=str(envelope.get("t")), payload=payload, src=src)


@dataclass
class _Partial:
    total: int
    chunks: Dict[int, bytes] = field(default_factory=dict)
    first_seen: float = field(default_factory=time.monotonic)


class Assembler:
    """Collects frames from many senders and returns messages once complete."""

    def __init__(self, timeout_s: float = 600.0):
        self.timeout_s = timeout_s
        self._partials: Dict[Tuple[str, int], _Partial] = {}

    def add(self, frame: bytes, src: str) -> Optional[Message]:
        msg_id, seq, total, chunk = parse_header(frame)
        self._expire()
        key = (src, msg_id)
        partial = self._partials.get(key)
        if partial is None or partial.total != total:
            partial = self._partials[key] = _Partial(total=total)
        partial.chunks[seq] = chunk
        if len(partial.chunks) < total:
            return None
        del self._partials[key]
        body = b"".join(partial.chunks[i] for i in range(total))
        return decode_body(msg_id, body, src)

    def pending(self) -> int:
        return len(self._partials)

    def _expire(self) -> None:
        cutoff = time.monotonic() - self.timeout_s
        for key in [k for k, p in self._partials.items() if p.first_seen < cutoff]:
            del self._partials[key]
