package org.meshlearn.core

import java.io.ByteArrayOutputStream
import java.security.SecureRandom

/**
 * The handful of Meshtastic protobuf messages the phone needs, encoded by hand so the app
 * carries no protobuf library. Field numbers follow meshtastic/protobufs (mesh.proto):
 *
 *   ToRadio   { MeshPacket packet = 1; uint32 want_config_id = 3; }
 *   FromRadio { uint32 id = 1; MeshPacket packet = 2; MyNodeInfo my_info = 3; uint32 config_complete_id = 7; }
 *   MeshPacket{ fixed32 from = 1; fixed32 to = 2; uint32 channel = 3; Data decoded = 4; fixed32 id = 6;
 *               uint32 hop_limit = 9; bool want_ack = 10; }
 *   Data      { PortNum portnum = 1; bytes payload = 2; }
 *   MyNodeInfo{ uint32 my_node_num = 1; }
 *
 * MeshLearn frames travel on PRIVATE_APP (256), like the Python transport.
 */
object MeshtasticProto {
    const val PRIVATE_APP = 256
    const val BROADCAST = 0xFFFFFFFFL

    // Bluetooth service and characteristics of a Meshtastic radio, as in Meshtastic's own
    // Python library (meshtastic/ble_interface.py). CoreTest checks them character by character.
    const val SERVICE_UUID = "6ba1b218-15a8-461f-9fa8-5dcae273eafd"
    const val TORADIO_UUID = "f75c76d2-129e-4dad-a1dd-7866124401e7"
    const val FROMRADIO_UUID = "2c55e69e-4993-11ed-b878-0242ac120002"
    const val FROMNUM_UUID = "ed9da18c-a800-4f66-a670-aa7547e34453"

    private val random = SecureRandom()

    /** "!a1b2c3d4" for node number 0xa1b2c3d4. */
    fun nodeIdOf(num: Long): String = "!%08x".format(num and 0xFFFFFFFFL)

    /** Node number from "!a1b2c3d4" (or plain hex/decimal). */
    fun nodeNumOf(id: String): Long {
        val t = id.trim()
        return if (t.startsWith("!")) t.substring(1).toLong(16) else t.toLongOrNull() ?: t.toLong(16)
    }

    fun wantConfig(configId: Long = (random.nextInt().toLong() and 0x7FFFFFFF) or 1): ByteArray {
        val w = Writer()
        w.varintField(3, configId)
        return w.bytes()
    }

    /** ToRadio carrying one MeshLearn frame to [dest] on [channel]. */
    fun toRadioPacket(payload: ByteArray, dest: String, channel: Int, hopLimit: Int? = null, wantAck: Boolean = true,
                      packetId: Long = (random.nextInt().toLong() and 0xFFFFFFFFL).let { if (it == 0L) 1L else it }): ByteArray {
        val data = Writer().apply {
            varintField(1, PRIVATE_APP.toLong())
            bytesField(2, payload)
        }.bytes()
        val packet = Writer().apply {
            fixed32Field(2, nodeNumOf(dest))
            if (channel != 0) varintField(3, channel.toLong())
            bytesField(4, data)
            fixed32Field(6, packetId)
            if (hopLimit != null) varintField(9, hopLimit.toLong())
            if (wantAck) varintField(10, 1)
        }.bytes()
        return Writer().apply { bytesField(1, packet) }.bytes()
    }

    sealed class FromRadio {
        data class MyInfo(val nodeNum: Long) : FromRadio()
        data class Packet(val from: Long, val to: Long, val channel: Int, val portnum: Int, val payload: ByteArray) : FromRadio()
        data class ConfigComplete(val id: Long) : FromRadio()
        object Other : FromRadio()
    }

    fun parseFromRadio(bytes: ByteArray): FromRadio {
        var result: FromRadio = FromRadio.Other
        Reader(bytes).forEachField { field, wire, r ->
            when {
                field == 2 && wire == 2 -> result = parsePacket(r.lengthDelimited())
                field == 3 && wire == 2 -> {
                    var num = 0L
                    Reader(r.lengthDelimited()).forEachField { f, w, rr -> if (f == 1 && w == 0) num = rr.varint() else rr.skip(w) }
                    result = FromRadio.MyInfo(num)
                }
                field == 7 && wire == 0 -> result = FromRadio.ConfigComplete(r.varint())
                else -> r.skip(wire)
            }
        }
        return result
    }

    private fun parsePacket(bytes: ByteArray): FromRadio {
        var from = 0L; var to = 0L; var channel = 0; var portnum = 0; var payload: ByteArray? = null
        Reader(bytes).forEachField { field, wire, r ->
            when {
                field == 1 && wire == 5 -> from = r.fixed32()
                field == 2 && wire == 5 -> to = r.fixed32()
                field == 3 && wire == 0 -> channel = r.varint().toInt()
                field == 4 && wire == 2 -> Reader(r.lengthDelimited()).forEachField { f, w, rr ->
                    when {
                        f == 1 && w == 0 -> portnum = rr.varint().toInt()
                        f == 2 && w == 2 -> payload = rr.lengthDelimited()
                        else -> rr.skip(w)
                    }
                }
                else -> r.skip(wire)
            }
        }
        val p = payload ?: return FromRadio.Other   // encrypted for someone else, or not decoded
        return FromRadio.Packet(from, to, channel, portnum, p)
    }

    // -- protobuf wire format -------------------------------------------------

    class Writer {
        private val out = ByteArrayOutputStream()
        fun bytes(): ByteArray = out.toByteArray()
        private fun rawVarint(v: Long) {
            var x = v
            while (x and 0x7FL.inv() != 0L) { out.write(((x and 0x7F) or 0x80).toInt()); x = x ushr 7 }
            out.write(x.toInt())
        }
        private fun tag(field: Int, wire: Int) = rawVarint(((field shl 3) or wire).toLong())
        fun varintField(field: Int, v: Long) { tag(field, 0); rawVarint(v) }
        fun fixed32Field(field: Int, v: Long) {
            tag(field, 5)
            for (i in 0 until 4) out.write(((v shr (8 * i)) and 0xFF).toInt())
        }
        fun bytesField(field: Int, b: ByteArray) { tag(field, 2); rawVarint(b.size.toLong()); out.write(b) }
    }

    class Reader(private val b: ByteArray) {
        private var pos = 0
        fun forEachField(fn: (field: Int, wire: Int, r: Reader) -> Unit) {
            while (pos < b.size) {
                val tag = varint()
                fn((tag ushr 3).toInt(), (tag and 7).toInt(), this)
            }
        }
        fun varint(): Long {
            var shift = 0; var result = 0L
            while (true) {
                if (pos >= b.size) throw ProtocolError("truncated varint")
                val x = b[pos++].toInt() and 0xFF
                result = result or ((x and 0x7F).toLong() shl shift)
                if (x and 0x80 == 0) return result
                shift += 7
                if (shift > 63) throw ProtocolError("varint too long")
            }
        }
        fun fixed32(): Long {
            if (pos + 4 > b.size) throw ProtocolError("truncated fixed32")
            var v = 0L
            for (i in 0 until 4) v = v or ((b[pos + i].toLong() and 0xFF) shl (8 * i))
            pos += 4
            return v
        }
        fun lengthDelimited(): ByteArray {
            val n = varint().toInt()
            if (n < 0 || pos + n > b.size) throw ProtocolError("truncated field")
            return b.copyOfRange(pos, pos + n).also { pos += n }
        }
        fun skip(wire: Int) {
            when (wire) {
                0 -> varint()
                1 -> pos += 8
                2 -> lengthDelimited()
                5 -> pos += 4
                else -> throw ProtocolError("unsupported wire type $wire")
            }
        }
    }
}
