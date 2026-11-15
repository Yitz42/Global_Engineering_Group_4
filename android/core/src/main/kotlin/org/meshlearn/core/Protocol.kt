package org.meshlearn.core

import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.security.SecureRandom
import java.util.zip.DataFormatException
import java.util.zip.Deflater
import java.util.zip.Inflater

/**
 * Wire format for messages over the mesh; a port of meshlearn/protocol.py.
 *
 *     magic (1) | version << 4 | flags (1) | message id (4) | seq (1) | total (1) | data
 *
 * A message is the JSON pair [type, payload], zlib-compressed with the shared preset
 * dictionary, split into frames that each fit one LoRa packet. The END flag marks the
 * last frame of a burst so the receiver can ask for exactly the pieces it is missing.
 */
object Protocol {
    const val MAGIC = 0xA7
    val VERSION = Zdict.VERSION
    const val FLAG_END = 0x01
    const val HEADER_SIZE = 8
    const val MAX_FRAME_BYTES = 219   // firmware 2.7/2.8 silently drop anything bigger
    const val CHUNK_BYTES = MAX_FRAME_BYTES - HEADER_SIZE
    const val MAX_FRAMES = 255

    const val HELLO = "h"
    const val ASSIGN = "a"
    const val OPEN = "o"
    const val NEED = "n"
    const val SUBMIT = "s"
    const val RESULT = "r"
    const val ACK = "k"
    const val NACK = "x"

    private val random = SecureRandom()

    fun newMsgId(): Long = random.nextInt().toLong() and 0xFFFFFFFFL

    fun compress(raw: ByteArray): ByteArray {
        val d = Deflater(9)
        d.setDictionary(Zdict.BYTES)
        d.setInput(raw)
        d.finish()
        val out = ByteArrayOutputStream()
        val buf = ByteArray(1024)
        while (!d.finished()) out.write(buf, 0, d.deflate(buf))
        d.end()
        return out.toByteArray()
    }

    fun decompress(body: ByteArray): ByteArray {
        val inf = Inflater()
        try {
            inf.setInput(body)
            val out = ByteArrayOutputStream()
            val buf = ByteArray(1024)
            while (!inf.finished()) {
                val n = inf.inflate(buf)
                if (n == 0) {
                    when {
                        inf.needsDictionary() -> inf.setDictionary(Zdict.BYTES)
                        inf.needsInput() -> throw ProtocolError("truncated stream")
                    }
                } else out.write(buf, 0, n)
            }
            return out.toByteArray()
        } catch (e: DataFormatException) {
            throw ProtocolError("corrupt message body: ${e.message}")
        } finally {
            inf.end()
        }
    }

    /** Turn a message into frames that each fit one LoRa packet. The last frame carries END. */
    fun encode(type: String, payload: Map<String, Any?>, msgId: Long? = null): Pair<Long, List<ByteArray>> {
        val id = msgId ?: newMsgId()
        val raw = Json.dumps(listOf(type, payload)).toByteArray(Charsets.UTF_8)
        val body = compress(raw)
        val chunks = if (body.isEmpty()) listOf(ByteArray(0))
        else (body.indices step CHUNK_BYTES).map { body.copyOfRange(it, minOf(it + CHUNK_BYTES, body.size)) }
        if (chunks.size > MAX_FRAMES) {
            throw ProtocolError("message needs ${chunks.size} packets; the limit is $MAX_FRAMES " +
                "(about ${MAX_FRAMES * CHUNK_BYTES / 1024} KB compressed)")
        }
        val total = chunks.size
        val frames = chunks.mapIndexed { seq, chunk -> frame(id, seq, total, chunk, if (seq == total - 1) FLAG_END else 0) }
        return id to frames
    }

    private fun frame(msgId: Long, seq: Int, total: Int, chunk: ByteArray, flags: Int): ByteArray {
        val bb = ByteBuffer.allocate(HEADER_SIZE + chunk.size)
        bb.put(MAGIC.toByte())
        bb.put(((VERSION shl 4) or flags).toByte())
        bb.putInt(msgId.toInt())
        bb.put(seq.toByte())
        bb.put(total.toByte())
        bb.put(chunk)
        return bb.array()
    }

    /** Mark (or unmark) a frame as the last one in a burst. */
    fun setEnd(frame: ByteArray, end: Boolean = true): ByteArray {
        val copy = frame.copyOf()
        val vf = copy[1].toInt() and 0xFF
        copy[1] = (if (end) vf or FLAG_END else vf and FLAG_END.inv()).toByte()
        return copy
    }

    fun frameCount(type: String, payload: Map<String, Any?>): Int = encode(type, payload, 0).second.size

    class FrameInfo(val msgId: Long, val seq: Int, val total: Int, val end: Boolean, val data: ByteArray)

    fun parseHeader(frame: ByteArray): FrameInfo {
        if (frame.size < HEADER_SIZE) throw ProtocolError("frame too short")
        val bb = ByteBuffer.wrap(frame)
        val magic = bb.get().toInt() and 0xFF
        val verflags = bb.get().toInt() and 0xFF
        val msgId = bb.int.toLong() and 0xFFFFFFFFL
        val seq = bb.get().toInt() and 0xFF
        val total = bb.get().toInt() and 0xFF
        if (magic != MAGIC) throw ProtocolError("not a MeshLearn frame")
        if (verflags shr 4 != VERSION) throw ProtocolError("unsupported protocol version ${verflags shr 4}")
        if (total == 0 || seq >= total) throw ProtocolError("bad frame numbering")
        return FrameInfo(msgId, seq, total, (verflags and FLAG_END) != 0, frame.copyOfRange(HEADER_SIZE, frame.size))
    }

    fun decodeBody(msgId: Long, body: ByteArray, src: String = ""): Message {
        val parsed = try {
            Json.parse(String(decompress(body), Charsets.UTF_8))
        } catch (e: Json.ParseError) {
            throw ProtocolError("corrupt message body: ${e.message}")
        }
        val pair = parsed.asList()
        if (pair == null || pair.size != 2) throw ProtocolError("malformed message")
        val type = pair[0] as? String ?: throw ProtocolError("malformed message")
        val payload = pair[1].asMap() ?: throw ProtocolError("malformed message")
        return Message(msgId, type, payload, src)
    }
}

class ProtocolError(msg: String) : IllegalArgumentException(msg)

data class Message(val msgId: Long, val type: String, val payload: Map<String, Any?>, val src: String = "")

/** Collects frames from many senders; returns a message once complete, or the missing pieces when a burst ends short. */
class Assembler(private val timeoutS: Double = 3600.0) {
    private class Partial(val total: Int) {
        val chunks = HashMap<Int, ByteArray>()
        val firstSeen = System.nanoTime()
    }

    class AddResult(val message: Message? = null, val missing: List<Int>? = null)

    private val partials = HashMap<Pair<String, Long>, Partial>()

    @Synchronized
    fun add(frame: ByteArray, src: String): AddResult {
        val info = Protocol.parseHeader(frame)
        expire()
        val key = src to info.msgId
        var partial = partials[key]
        if (partial == null || partial.total != info.total) {
            partial = Partial(info.total)
            partials[key] = partial
        }
        partial.chunks[info.seq] = info.data
        if (partial.chunks.size < info.total) {
            if (info.end) return AddResult(missing = (0 until info.total).filter { it !in partial.chunks })
            return AddResult()
        }
        partials.remove(key)
        val out = ByteArrayOutputStream()
        for (i in 0 until info.total) out.write(partial.chunks.getValue(i))
        return AddResult(message = Protocol.decodeBody(info.msgId, out.toByteArray(), src))
    }

    @Synchronized
    fun pending(): Int = partials.size

    private fun expire() {
        val cutoff = System.nanoTime() - (timeoutS * 1e9).toLong()
        partials.entries.removeIf { it.value.firstSeen < cutoff }
    }
}
