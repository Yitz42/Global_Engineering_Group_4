package org.meshlearn.core

import java.io.BufferedInputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.Executors
import java.util.logging.Level
import java.util.logging.Logger

/**
 * A Meshtastic radio reached over Wi-Fi (its TCP API on port 4403), like the Python
 * transport's `host=` option. Each protobuf is framed as 0x94 0xC3 <length, 2 bytes> <bytes>.
 * Reconnects by itself; [status] says what it is doing, for the app's screen.
 */
class TcpRadioTransport(
    private val host: String,
    private val port: Int = 4403,
    private val channel: Int = 0,
    private val hopLimit: Int? = null,
    minIntervalS: Double = 3.0,
) : Transport {
    private val log = Logger.getLogger("TcpRadioTransport")
    private val pacer = Pacer(minIntervalS)
    private val rx = Executors.newSingleThreadExecutor { Thread(it, "radio-rx").apply { isDaemon = true } }
    @Volatile override var nodeId: String = ""
        private set
    @Volatile var status: String = "Not connected"
        private set
    @Volatile private var socket: Socket? = null
    @Volatile private var out: OutputStream? = null
    @Volatile private var closed = false

    override fun start(onFrame: FrameHandler) {
        Thread({ runLoop(onFrame) }, "radio-tcp").apply { isDaemon = true }.start()
    }

    private fun runLoop(onFrame: FrameHandler) {
        while (!closed) {
            try {
                status = "Connecting to $host…"
                val s = Socket()
                s.connect(InetSocketAddress(host, port), 10_000)
                socket = s
                out = s.getOutputStream()
                writeFrame(MeshtasticProto.wantConfig())
                status = "Connected to $host"
                read(BufferedInputStream(s.getInputStream()), onFrame)
            } catch (e: Exception) {
                if (!closed) log.log(Level.INFO, "radio link to $host dropped: ${e.message}")
            } finally {
                runCatching { socket?.close() }
                socket = null
                out = null
            }
            if (closed) break
            status = "Lost the radio at $host; retrying"
            Thread.sleep(5000)
        }
    }

    private fun read(input: InputStream, onFrame: FrameHandler) {
        while (!closed) {
            if (input.read() != 0x94) continue
            if (input.read() != 0xC3) continue
            val hi = input.read(); val lo = input.read()
            if (hi < 0 || lo < 0) throw java.io.EOFException()
            val len = (hi shl 8) or lo
            if (len > 512) continue           // not a frame start after all
            val buf = ByteArray(len)
            var n = 0
            while (n < len) {
                val r = input.read(buf, n, len - n)
                if (r < 0) throw java.io.EOFException()
                n += r
            }
            when (val msg = runCatching { MeshtasticProto.parseFromRadio(buf) }.getOrNull()) {
                is MeshtasticProto.FromRadio.MyInfo -> nodeId = MeshtasticProto.nodeIdOf(msg.nodeNum)
                is MeshtasticProto.FromRadio.Packet -> if (msg.portnum == MeshtasticProto.PRIVATE_APP) {
                    val src = MeshtasticProto.nodeIdOf(msg.from)
                    rx.execute { runCatching { onFrame(msg.payload, src) }.onFailure { log.log(Level.WARNING, "frame handler failed", it) } }
                }
                else -> {}
            }
        }
    }

    @Synchronized
    private fun writeFrame(bytes: ByteArray) {
        val o = out ?: throw IllegalStateException("radio not connected")
        o.write(byteArrayOf(0x94.toByte(), 0xC3.toByte(), (bytes.size shr 8).toByte(), bytes.size.toByte()) + bytes)
        o.flush()
    }

    override fun send(frame: ByteArray, dest: String) {
        pacer.waitTurn()
        writeFrame(MeshtasticProto.toRadioPacket(frame, dest, channel, hopLimit))
    }

    override fun close() {
        closed = true
        runCatching { socket?.close() }
        rx.shutdownNow()
    }
}
