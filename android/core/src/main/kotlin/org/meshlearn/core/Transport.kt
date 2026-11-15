package org.meshlearn.core

import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.SocketException
import java.net.SocketTimeoutException
import java.util.Random
import java.util.logging.Level
import java.util.logging.Logger

typealias FrameHandler = (frame: ByteArray, src: String) -> Unit

/** Sends single frames to a named node and reports frames received. Port of transport.py. */
interface Transport {
    /** This node's id (a Meshtastic id like "!a1b2c3d4", or a simulator name). Empty until known. */
    val nodeId: String
    fun start(onFrame: FrameHandler)
    fun send(frame: ByteArray, dest: String)
    fun close() {}
}

/** Keeps a minimum gap between transmissions, as a real radio needs. */
class Pacer(private val minIntervalS: Double) {
    private var last = 0L
    @Synchronized
    fun waitTurn() {
        val gapNs = (minIntervalS * 1e9).toLong() - (System.nanoTime() - last)
        if (last != 0L && gapNs > 0) Thread.sleep(gapNs / 1_000_000, (gapNs % 1_000_000).toInt())
        last = System.nanoTime()
    }
}

/**
 * Simulated mesh, wire-compatible with the Python UdpSimTransport: each node is a UDP port,
 * and a datagram is [name length][sender name][frame]. Lets the Kotlin phone talk to the
 * Python hub on one computer with no radios.
 */
class UdpSimTransport(
    override val nodeId: String,
    listenPort: Int,
    private val peers: Map<String, InetSocketAddress>,
    private val dropRate: Double = 0.0,
    minIntervalS: Double = 0.0,
    seed: Long? = null,
) : Transport {
    private val log = Logger.getLogger("UdpSimTransport")
    private val pacer = Pacer(minIntervalS)
    private val rng = if (seed != null) Random(seed) else Random()
    private val socket = DatagramSocket(InetSocketAddress(InetAddress.getLoopbackAddress(), listenPort)).apply { soTimeout = 500 }
    @Volatile private var running = false

    override fun start(onFrame: FrameHandler) {
        running = true
        Thread({
            val buf = ByteArray(2048)
            while (running) {
                val packet = DatagramPacket(buf, buf.size)
                try {
                    socket.receive(packet)
                } catch (e: SocketTimeoutException) {
                    continue
                } catch (e: SocketException) {
                    break
                }
                val data = packet.data.copyOfRange(packet.offset, packet.offset + packet.length)
                if (data.isEmpty()) continue
                val nameLen = data[0].toInt() and 0xFF
                val src = String(data, 1, nameLen, Charsets.UTF_8)
                val frame = data.copyOfRange(1 + nameLen, data.size)
                try {
                    onFrame(frame, src)
                } catch (e: Exception) {
                    log.log(Level.WARNING, "frame handler failed", e)
                }
            }
        }, "udp-$nodeId").apply { isDaemon = true }.start()
    }

    override fun send(frame: ByteArray, dest: String) {
        val addr = peers[dest] ?: throw IllegalArgumentException("unknown peer '$dest'")
        pacer.waitTurn()
        if (rng.nextDouble() < dropRate) return
        val name = nodeId.toByteArray(Charsets.UTF_8)
        val datagram = byteArrayOf(name.size.toByte()) + name + frame
        socket.send(DatagramPacket(datagram, datagram.size, addr))
    }

    override fun close() {
        running = false
        socket.close()
    }

    companion object {
        /** Turn ["hub=127.0.0.1:47001", ...] into a peer map, like transport.parse_peers. */
        fun parsePeers(specs: List<String>): Map<String, InetSocketAddress> = specs.associate { spec ->
            val name = spec.substringBefore("=")
            val addr = spec.substringAfter("=", "")
            val host = addr.substringBeforeLast(":", "")
            val port = addr.substringAfterLast(":", "")
            require(name.isNotEmpty() && port.isNotEmpty()) { "bad peer spec '$spec'; expected name=host:port" }
            name to InetSocketAddress(host.ifEmpty { "127.0.0.1" }, port.toInt())
        }
    }
}

/** No radio at all: everything queues until one is connected. */
class NoRadioTransport : Transport {
    override val nodeId = ""
    override fun start(onFrame: FrameHandler) {}
    override fun send(frame: ByteArray, dest: String) = throw IllegalStateException("no radio connected")
}
