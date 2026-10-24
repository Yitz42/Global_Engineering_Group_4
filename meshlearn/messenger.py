"""Reliable, store-and-forward messaging on top of a lossy transport.

Outgoing messages are saved to SQLite first, so nothing is lost if the phone
reboots or the radio link is down for hours. A background thread resends any
message that has not been acknowledged, backing off between attempts. The
receiver acknowledges every complete message and ignores duplicates.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Callable, Dict, Optional

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
        return counts

    def message_status(self, msg_id: int) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT status FROM outbox WHERE msg_id = ?", (msg_id,)).fetchone()
        return row[0] if row else None

    def retry_now(self) -> None:
        """Resend every pending message immediately (e.g. when a link comes back)."""
        with self._lock:
            self._db.execute("UPDATE outbox SET next_try = 0 WHERE status = 'pending'")
            self._db.commit()
        self._wake.set()

    # -- internals ----------------------------------------------------------

    def _loop(self) -> None:
        while self._running:
            self._send_due()
            self._wake.wait(self.poll_s)
            self._wake.clear()

    def _send_due(self) -> None:
        now = time.time()
        with self._lock:
            due = self._db.execute(
                "SELECT msg_id, dest, type, payload, attempts FROM outbox "
                "WHERE status = 'pending' AND next_try <= ? ORDER BY created LIMIT 5",
                (now,),
            ).fetchall()
        for msg_id, dest, msg_type, payload, attempts in due:
            if not self._running:
                return
            _, frames = protocol.encode(msg_type, json.loads(payload), msg_id=msg_id)
            try:
                for frame in frames:
                    self.transport.send(frame, dest)
            except Exception:
                log.exception("send to %s failed", dest)
            attempts += 1
            delay = min(self.retry_s * (2 ** min(attempts - 1, 10)), self.max_retry_s)
            status = "failed" if attempts >= self.max_attempts else "pending"
            with self._lock:
                self._db.execute(
                    "UPDATE outbox SET attempts = ?, next_try = ?, status = ? "
                    "WHERE msg_id = ? AND status = 'pending'",
                    (attempts, time.time() + delay, status, msg_id),
                )
                self._db.commit()

    def _on_frame(self, frame: bytes, src: str) -> None:
        try:
            message = self._assembler.add(frame, src)
        except protocol.ProtocolError as exc:
            log.debug("ignoring frame from %s: %s", src, exc)
            return
        if message is None:
            return
        if message.type == protocol.ACK:
            acked = message.payload.get("id")
            with self._lock:
                self._db.execute("UPDATE outbox SET status = 'acked' WHERE msg_id = ?", (acked,))
                self._db.commit()
            return
        # Acknowledge every copy (our earlier ACK may have been lost) ...
        _, ack_frames = protocol.encode(protocol.ACK, {"id": message.msg_id})
        try:
            for frame_out in ack_frames:
                self.transport.send(frame_out, src)
        except Exception:
            log.exception("ack to %s failed", src)
        # ... but only act on the first one.
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
