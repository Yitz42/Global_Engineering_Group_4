package org.meshlearn.core

import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock
import java.util.logging.Level
import java.util.logging.Logger
import kotlin.concurrent.withLock

typealias MessageHandler = (Message) -> Unit

/**
 * Reliable store-and-forward messaging over a lossy transport; a port of messenger.py.
 *
 * Outgoing messages are saved first, so nothing is lost if the phone reboots. The first try
 * sends every frame; a receiver missing pieces replies NACK and only those are resent; if
 * nothing comes back, one probe frame asks the receiver to NACK or ACK.
 */
class Messenger(
    val transport: Transport,
    private val db: Db,
    var onMessage: MessageHandler,
    private val retryS: Double = 30.0,
    private val maxRetryS: Double = 600.0,
    private val maxAttempts: Int = 50,
    private val pollS: Double = 0.25,
) {
    private val log = Logger.getLogger("Messenger")
    @Volatile var framesSent = 0L
        private set
    @Volatile private var heardAt = 0.0
    private val assembler = Assembler()
    @Volatile private var running = false
    private var thread: Thread? = null
    private val wakeLock = ReentrantLock()
    private val wakeCond = wakeLock.newCondition()
    private var woken = false

    init {
        db.executeScript(SCHEMA)
    }

    // -- public API ---------------------------------------------------------

    fun start() {
        transport.start(::onFrame)
        running = true
        thread = Thread(::loop, "messenger").apply { isDaemon = true; start() }
    }

    fun stop() {
        running = false
        wake()
        thread?.join(5000)
        transport.close()
    }

    /** Queue a message for reliable delivery and return its id. */
    fun send(dest: String, type: String, payload: Map<String, Any?>): Long {
        val (msgId, _) = Protocol.encode(type, payload)   // fail fast if it is too big
        db.execute("INSERT INTO outbox (msg_id, dest, type, payload, created) VALUES (?, ?, ?, ?, ?)",
            msgId, dest, type, Json.dumps(payload), now())
        wake()
        return msgId
    }

    fun status(): Map<String, Long> {
        val counts = linkedMapOf("pending" to 0L, "acked" to 0L, "failed" to 0L)
        for (r in db.all("SELECT status, COUNT(*) AS n FROM outbox GROUP BY status")) {
            counts[r.str("status") ?: continue] = r.long("n") ?: 0
        }
        counts["frames_sent"] = framesSent
        return counts
    }

    /** When a whole message (an ACK counts) last arrived, in epoch seconds; 0 if never. */
    fun lastHeard(): Double {
        val saved = runCatching { db.one("SELECT MAX(received) AS t FROM seen")?.double("t") }.getOrNull() ?: 0.0
        return maxOf(heardAt, saved)
    }

    fun messageStatus(msgId: Long?): String? {
        if (msgId == null) return null
        return db.one("SELECT status FROM outbox WHERE msg_id = ?", msgId)?.str("status")
    }

    /** Retry every pending message immediately (e.g. when a link comes back). */
    fun retryNow() {
        db.execute("UPDATE outbox SET next_try = 0 WHERE status = 'pending'")
        wake()
    }

    // -- sending ------------------------------------------------------------

    private fun wake() = wakeLock.withLock { woken = true; wakeCond.signalAll() }

    private fun loop() {
        while (running) {
            try {
                sendDue()
            } catch (e: Exception) {
                log.log(Level.WARNING, "send loop failed", e)
            }
            wakeLock.withLock {
                if (!woken) wakeCond.await((pollS * 1000).toLong(), TimeUnit.MILLISECONDS)
                woken = false
            }
        }
    }

    private fun transmit(frame: ByteArray, dest: String) {
        transport.send(frame, dest)
        framesSent++
    }

    private fun sendDue() {
        val due = db.all(
            "SELECT msg_id, dest, type, payload, attempts, resend FROM outbox " +
                "WHERE status = 'pending' AND next_try <= ? ORDER BY created LIMIT 5", now())
        for (row in due) {
            if (!running) return
            val msgId = row.long("msg_id")!!
            val dest = row.str("dest")!!
            val payload = Json.parse(row.str("payload")!!).asMap() ?: emptyMap()
            var attempts = row.long("attempts")!!.toInt()
            val resend = row.str("resend")
            val frames = Protocol.encode(row.str("type")!!, payload, msgId).second
            val burst: List<ByteArray> = when {
                attempts == 0 -> frames                                    // first try: everything
                resend != null -> {
                    val wanted = (Json.parse(resend).asList() ?: emptyList())
                        .mapNotNull { (it as? Number)?.toInt() }.filter { it in frames.indices }
                    val b = wanted.map { Protocol.setEnd(frames[it], false) }.toMutableList()
                    if (b.isNotEmpty()) b[b.size - 1] = Protocol.setEnd(b.last())  // only what was asked for
                    b
                }
                else -> listOf(frames.last())                              // probe: one packet
            }
            // Record the attempt BEFORE transmitting: a fast NACK must not be overwritten.
            attempts += 1
            val delay = minOf(retryS * (1 shl minOf(attempts - 1, 10)), maxRetryS)
            val status = if (attempts >= maxAttempts) "failed" else "pending"
            db.execute("UPDATE outbox SET attempts = ?, next_try = ?, status = ?, resend = NULL " +
                "WHERE msg_id = ? AND status = 'pending'", attempts, now() + delay, status, msgId)
            try {
                for (f in burst) transmit(f, dest)
            } catch (e: Exception) {
                log.log(Level.INFO, "send to $dest failed: ${e.message}")
            }
        }
    }

    /** ACK/NACK: fire and forget (a lost one is covered by the sender's probe). */
    private fun sendControl(type: String, payload: Map<String, Any?>, dest: String) {
        try {
            for (f in Protocol.encode(type, payload).second) transmit(f, dest)
        } catch (e: Exception) {
            log.log(Level.INFO, "$type to $dest failed: ${e.message}")
        }
    }

    // -- receiving ----------------------------------------------------------

    private fun alreadyHandled(src: String, msgId: Long) =
        db.one("SELECT 1 AS x FROM seen WHERE src = ? AND msg_id = ?", src, msgId) != null

    private fun onFrame(frame: ByteArray, src: String) {
        val info = try {
            Protocol.parseHeader(frame)
        } catch (e: ProtocolError) {
            return
        }
        if (alreadyHandled(src, info.msgId)) {
            if (info.end) sendControl(Protocol.ACK, mapOf("id" to info.msgId), src)  // our ACK was lost
            return
        }
        val result = try {
            assembler.add(frame, src)
        } catch (e: ProtocolError) {
            log.fine("dropping message from $src: ${e.message}")
            return
        }
        if (result.missing != null) {
            sendControl(Protocol.NACK, mapOf("id" to info.msgId, "m" to result.missing), src)
            return
        }
        val message = result.message ?: return
        heardAt = now()
        when (message.type) {
            Protocol.ACK -> {
                db.execute("UPDATE outbox SET status = 'acked' WHERE msg_id = ?", (message.payload["id"] as? Number)?.toLong())
                return
            }
            Protocol.NACK -> {
                onNack(message.payload)
                return
            }
        }
        sendControl(Protocol.ACK, mapOf("id" to message.msgId), src)
        val firstTime = db.execute("INSERT OR IGNORE INTO seen (src, msg_id, received) VALUES (?, ?, ?)",
            src, message.msgId, now()) == 1
        if (firstTime) {
            try {
                onMessage(message)
            } catch (e: Exception) {
                log.log(Level.WARNING, "handler failed for ${message.type} message from $src", e)
            }
        }
    }

    private fun onNack(payload: Map<String, Any?>) {
        val missing = (payload["m"].asList() ?: emptyList()).filter { it is Long || it is Int }.map { (it as Number).toInt() }
        if (missing.isEmpty()) return
        db.execute("UPDATE outbox SET resend = ?, next_try = 0 WHERE msg_id = ? AND status = 'pending'",
            Json.dumps(missing), (payload["id"] as? Number)?.toLong())
        wake()
    }

    companion object {
        const val SCHEMA = """
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
)"""

        fun now(): Double = System.currentTimeMillis() / 1000.0
    }
}
