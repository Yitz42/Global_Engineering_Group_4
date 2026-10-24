"""Reliable, store-and-forward messaging on top of a lossy transport.

Outgoing messages are saved to SQLite first, so nothing is lost if the phone
reboots or the radio link is down for hours.

Lost pieces are resent selectively:

* The first attempt sends every frame; the last one carries the END flag.
* A receiver that sees END but is missing pieces replies NACK with their
  numbers, and the sender resends only those.
* If the sender hears nothing before its timer runs out, it sends a single
  probe (the last frame, flagged END). The receiver answers with a NACK, or
  with an ACK if it already has the whole message. One packet instead of all.

The receiver acknowledges every complete message and ignores duplicates.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Callable, Dict, List, Optional

from . import protocol
from .transport import Transport

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    msg_id INTEGER PRIMARY KEY,
    dest TEXT NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    resend TEXT,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
    src TEXT NOT NULL,
    msg_id INTEGER NOT NULL,
    received REAL NOT NULL,
    PRIMARY KEY (src, msg_id)
);
"""

MessageHandler = Callable[[protocol.Message], None]


class Messenger:
    def __init__(
        self,
        transport: Transport,
        db_path: str,
        on_message: MessageHandler,
        retry_s: float = 30.0,
        max_retry_s: float = 600.0,
        max_attempts: int = 50,
        poll_s: float = 0.25,
    ):
        self.transport = transport
        self.on_message = on_message
        self.retry_s = retry_s
        self.max_retry_s = max_retry_s
        self.max_attempts = max_attempts
        self.poll_s = poll_s
        self.frames_sent = 0
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._assembler = protocol.Assembler()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        self.transport.start(self._on_frame)
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="messenger", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.transport.close()

    def send(self, dest: str, msg_type: str, payload: dict) -> int:
        """Queue a message for reliable delivery and return its id."""
        msg_id, _ = protocol.encode(msg_type, payload)  # fail fast if it is too big
        with self._lock:
            self._db.execute(
                "INSERT INTO outbox (msg_id, dest, type, payload, created) VALUES (?, ?, ?, ?, ?)",
                (msg_id, dest, msg_type, json.dumps(payload), time.time()),
            )
            self._db.commit()
        self._wake.set()
        return msg_id

    def status(self) -> Dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT status, COUNT(*) FROM outbox GROUP BY status").fetchall()
        counts = {"pending": 0, "acked": 0, "failed": 0}
        counts.update(dict(rows))
        counts["frames_sent"] = self.frames_sent
        return counts

    def message_status(self, msg_id: Optional[int]) -> Optional[str]:
        if msg_id is None:
            return None
        with self._lock:
            row = self._db.execute("SELECT status FROM outbox WHERE msg_id = ?", (msg_id,)).fetchone()
        return row[0] if row else None

    def retry_now(self) -> None:
        """Retry every pending message immediately (e.g. when a link comes back)."""
        with self._lock:
            self._db.execute("UPDATE outbox SET next_try = 0 WHERE status = 'pending'")
            self._db.commit()
        self._wake.set()

    # -- sending ------------------------------------------------------------

    def _loop(self) -> None:
        while self._running:
            self._send_due()
            self._wake.wait(self.poll_s)
            self._wake.clear()

    def _transmit(self, frame: bytes, dest: str) -> None:
        self.transport.send(frame, dest)
        self.frames_sent += 1

    def _send_due(self) -> None:
        now = time.time()
        with self._lock:
            due = self._db.execute(
                "SELECT msg_id, dest, type, payload, attempts, resend FROM outbox "
                "WHERE status = 'pending' AND next_try <= ? ORDER BY created LIMIT 5",
                (now,),
            ).fetchall()
        for msg_id, dest, msg_type, payload, attempts, resend in due:
            if not self._running:
                return
            _, frames = protocol.encode(msg_type, json.loads(payload), msg_id=msg_id)
            if attempts == 0:
                burst = frames                                   # first try: everything
            elif resend:
                wanted = [i for i in json.loads(resend) if 0 <= i < len(frames)]
                burst = [protocol.set_end(frames[i], False) for i in wanted]
                if burst:
                    burst[-1] = protocol.set_end(burst[-1])      # only what was asked for
            else:
                burst = [frames[-1]]                             # probe: one packet
            try:
                for frame in burst:
                    self._transmit(frame, dest)
            except Exception:
                log.exception("send to %s failed", dest)
            attempts += 1
            delay = min(self.retry_s * (2 ** min(attempts - 1, 10)), self.max_retry_s)
            status = "failed" if attempts >= self.max_attempts else "pending"
            with self._lock:
                self._db.execute(
                    "UPDATE outbox SET attempts = ?, next_try = ?, status = ?, resend = NULL "
                    "WHERE msg_id = ? AND status = 'pending'",
                    (attempts, time.time() + delay, status, msg_id),
                )
                self._db.commit()

    def _send_control(self, msg_type: str, payload: dict, dest: str) -> None:
        """ACK/NACK: fire and forget (a lost one is covered by the sender's probe)."""
        _, frames = protocol.encode(msg_type, payload)
        try:
            for frame in frames:
                self._transmit(frame, dest)
        except Exception:
            log.exception("%s to %s failed", msg_type, dest)

    # -- receiving ----------------------------------------------------------

    def _already_handled(self, src: str, msg_id: int) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM seen WHERE src = ? AND msg_id = ?", (src, msg_id)).fetchone() is not None

    def _on_frame(self, frame: bytes, src: str) -> None:
        try:
            info = protocol.parse_header(frame)
        except protocol.ProtocolError as exc:
            log.debug("ignoring frame from %s: %s", src, exc)
            return
        if self._already_handled(src, info.msg_id):
            if info.end:  # sender is still waiting: our ACK was lost
                self._send_control(protocol.ACK, {"id": info.msg_id}, src)
            return
        try:
            result = self._assembler.add(frame, src)
        except protocol.ProtocolError as exc:
            log.debug("dropping message from %s: %s", src, exc)
            return
        if result.missing is not None:
            self._send_control(protocol.NACK, {"id": info.msg_id, "m": result.missing}, src)
            return
        message = result.message
        if message is None:
            return
        if message.type == protocol.ACK:
            with self._lock:
                self._db.execute("UPDATE outbox SET status = 'acked' WHERE msg_id = ?", (message.payload.get("id"),))
                self._db.commit()
            return
        if message.type == protocol.NACK:
            self._on_nack(message.payload)
            return
        self._send_control(protocol.ACK, {"id": message.msg_id}, src)
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO seen (src, msg_id, received) VALUES (?, ?, ?)",
                (src, message.msg_id, time.time()),
            )
            self._db.commit()
            first_time = cur.rowcount == 1
        if first_time:
            try:
                self.on_message(message)
            except Exception:
                log.exception("handler failed for %s message from %s", message.type, src)

    def _on_nack(self, payload: dict) -> None:
        missing: List[int] = [int(i) for i in payload.get("m") or [] if isinstance(i, int)]
        if not missing:
            return
        with self._lock:
            self._db.execute(
                "UPDATE outbox SET resend = ?, next_try = 0 WHERE msg_id = ? AND status = 'pending'",
                (json.dumps(missing), payload.get("id")),
            )
            self._db.commit()
        self._wake.set()
