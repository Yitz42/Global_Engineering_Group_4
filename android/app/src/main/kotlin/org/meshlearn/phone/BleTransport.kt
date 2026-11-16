package org.meshlearn.phone

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.BluetoothStatusCodes
import android.content.Context
import android.os.Build
import android.util.Log
import org.meshlearn.core.FrameHandler
import org.meshlearn.core.MeshtasticProto
import org.meshlearn.core.Pacer
import org.meshlearn.core.Transport
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.TimeUnit

/**
 * The phone's LoRa radio over Bluetooth, speaking Meshtastic's BLE API directly:
 * write ToRadio protobufs to TORADIO, read FromRadio until empty whenever FROMNUM notifies.
 *
 * Android allows one Bluetooth operation at a time, so every operation runs on one thread
 * and waits for its callback. Received frames are handed to MeshLearn on another thread, so
 * a reply (an ACK) can be written without waiting on itself.
 */
@SuppressLint("MissingPermission")   // MainActivity asks for Bluetooth permission before the service starts
class BleTransport(
    private val context: Context,
    private val address: String,
    private val channel: Int,
    private val hopLimit: Int?,
    minIntervalS: Double = 3.0,
) : Transport {
    @Volatile override var nodeId: String = ""
        private set
    @Volatile var status: String = "Not connected"
        private set

    private val pacer = Pacer(minIntervalS)
    private val ops = Executors.newSingleThreadExecutor { Thread(it, "ble-ops").apply { isDaemon = true } }
    private val rx = Executors.newSingleThreadExecutor { Thread(it, "ble-rx").apply { isDaemon = true } }
    private val timer = Executors.newSingleThreadScheduledExecutor { Thread(it, "ble-timer").apply { isDaemon = true } }
    private var onFrame: FrameHandler = { _, _ -> }
    @Volatile private var gatt: BluetoothGatt? = null
    @Volatile private var ready = false
    @Volatile private var closed = false
    private var toRadio: BluetoothGattCharacteristic? = null
    private var fromRadio: BluetoothGattCharacteristic? = null
    private var fromNum: BluetoothGattCharacteristic? = null

    // The one operation in flight and its result.
    @Volatile private var latch: CountDownLatch? = null
    @Volatile private var opOk = false
    @Volatile private var readValue: ByteArray? = null
    @Volatile private var disconnected: CountDownLatch? = null

    override fun start(onFrame: FrameHandler) {
        this.onFrame = onFrame
        connect()
    }

    private fun connect() {
        if (closed) return
        val adapter = (context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager).adapter
        if (adapter == null || !adapter.isEnabled) {
            status = "Bluetooth is off"
            retryLater()
            return
        }
        val device: BluetoothDevice = try {
            adapter.getRemoteDevice(address)
        } catch (e: IllegalArgumentException) {
            status = "Radio address is not valid: $address"
            return
        }
        status = "Connecting to the radio…"
        @Suppress("DEPRECATION")   // still the call that works on Android 7 through 16
        val g = device.connectGatt(context, false, callback, BluetoothDevice.TRANSPORT_LE)
        gatt = g
        if (g == null) {
            status = "Bluetooth is not ready; trying again"
            retryLater()
        } else {
            watchConnecting(g)
        }
    }

    /**
     * Run [block] on [executor] without ever crashing the app: once [close] has stopped the
     * threads, or interrupted a wait, the work is simply dropped. (An exception escaping a
     * thread-pool task ends the whole app on Android.)
     */
    private fun later(executor: java.util.concurrent.ExecutorService, block: () -> Unit) {
        try {
            executor.execute {
                try {
                    block()
                } catch (e: InterruptedException) {
                    // closing
                } catch (e: Exception) {
                    if (!closed) Log.w(TAG, "radio task failed", e)
                }
            }
        } catch (e: RejectedExecutionException) {
            // closed
        }
    }

    /** Run [block] on the operations thread after [ms], keeping that thread free meanwhile. */
    private fun after(ms: Long, block: () -> Unit) {
        try {
            timer.schedule({ later(ops, block) }, ms, TimeUnit.MILLISECONDS)
        } catch (e: RejectedExecutionException) {
            // closed
        }
    }

    private fun retryLater() {
        if (closed) return
        after(5000) { if (!closed && !ready) connect() }
    }

    /** Android sometimes never answers a connection attempt; start again if it hangs. */
    private fun watchConnecting(g: BluetoothGatt) {
        after(40_000) {
            if (!closed && !ready && gatt === g) {
                Log.w(TAG, "connecting took too long; trying again")
                runCatching { g.close() }
                gatt = null
                status = "Lost the radio; reconnecting"
                connect()
            }
        }
    }

    /** Start one GATT operation and wait for its callback. */
    private fun op(timeoutS: Long = 10, start: () -> Boolean): Boolean {
        val l = CountDownLatch(1)
        latch = l
        opOk = false
        if (!start()) return false
        return l.await(timeoutS, TimeUnit.SECONDS) && opOk
    }

    private fun done(ok: Boolean) {
        opOk = ok
        latch?.countDown()
    }

    private val callback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, st: Int, newState: Int) {
            if (newState == BluetoothProfile.STATE_CONNECTED && st == BluetoothGatt.GATT_SUCCESS) {
                later(ops) { setUp(g) }
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED || st != BluetoothGatt.GATT_SUCCESS) {
                // Disconnected, or a connection that came up with an error (often status 133
                // just after the radio's last connection closed): start over in a moment.
                ready = false
                done(false)
                disconnected?.countDown()
                runCatching { g.close() }
                if (gatt === g) gatt = null
                if (!closed) {
                    status = "Lost the radio; reconnecting"
                    retryLater()
                }
            }
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, st: Int) = done(st == BluetoothGatt.GATT_SUCCESS)
        override fun onServicesDiscovered(g: BluetoothGatt, st: Int) = done(st == BluetoothGatt.GATT_SUCCESS)
        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, st: Int) = done(st == BluetoothGatt.GATT_SUCCESS)
        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, st: Int) = done(st == BluetoothGatt.GATT_SUCCESS)

        @Deprecated("Android 12 and older")
        override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, st: Int) {
            @Suppress("DEPRECATION")
            readValue = c.value
            done(st == BluetoothGatt.GATT_SUCCESS)
        }

        override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray, st: Int) {
            readValue = value
            done(st == BluetoothGatt.GATT_SUCCESS)
        }

        @Deprecated("Android 12 and older")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
            if (c.uuid == FROMNUM) later(ops) { drain() }
        }

        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) {
            if (c.uuid == FROMNUM) later(ops) { drain() }
        }
    }

    private fun setUp(g: BluetoothGatt) {
        op { g.requestMtu(512) }    // fine if refused: frames are small
        if (!op(20) { g.discoverServices() }) return fail(g, "Could not read the radio's services")
        val service = g.getService(SERVICE) ?: return fail(g, "That device is not a Meshtastic radio")
        toRadio = service.getCharacteristic(TORADIO)
        fromRadio = service.getCharacteristic(FROMRADIO)
        fromNum = service.getCharacteristic(FROMNUM)
        val num = fromNum ?: return fail(g, "Radio is missing FROMNUM")
        if (toRadio == null || fromRadio == null) return fail(g, "Radio is missing TORADIO/FROMRADIO")
        g.setCharacteristicNotification(num, true)
        num.getDescriptor(CCCD)?.let { d ->
            op {
                if (Build.VERSION.SDK_INT >= 33) {
                    g.writeDescriptor(d, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE) == BluetoothStatusCodes.SUCCESS
                } else {
                    @Suppress("DEPRECATION")
                    d.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                    @Suppress("DEPRECATION")
                    g.writeDescriptor(d)
                }
            }
        }
        // Ask for the radio's info (and its node number), then read everything it has queued.
        if (!write(MeshtasticProto.wantConfig())) return fail(g, "Pair the radio in Android's Bluetooth settings first (PIN on its screen)")
        drain()
        ready = true
        status = "Connected" + (if (nodeId.isNotEmpty()) " ($nodeId)" else "")
    }

    private fun fail(g: BluetoothGatt, why: String) {
        status = why
        Log.w(TAG, why)
        g.disconnect()
    }

    private fun write(bytes: ByteArray): Boolean {
        val g = gatt ?: return false
        val c = toRadio ?: return false
        return op {
            if (Build.VERSION.SDK_INT >= 33) {
                g.writeCharacteristic(c, bytes, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) == BluetoothStatusCodes.SUCCESS
            } else {
                @Suppress("DEPRECATION")
                c.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                @Suppress("DEPRECATION")
                c.value = bytes
                @Suppress("DEPRECATION")
                g.writeCharacteristic(c)
            }
        }
    }

    /** Read FROMRADIO until the radio has nothing more. Runs on the ops thread. */
    private fun drain() {
        val g = gatt ?: return
        val c = fromRadio ?: return
        repeat(500) {
            readValue = null
            if (!op { g.readCharacteristic(c) }) return
            val bytes = readValue ?: return
            if (bytes.isEmpty()) return
            when (val msg = runCatching { MeshtasticProto.parseFromRadio(bytes) }.getOrNull()) {
                is MeshtasticProto.FromRadio.MyInfo -> {
                    nodeId = MeshtasticProto.nodeIdOf(msg.nodeNum)
                    if (ready) status = "Connected ($nodeId)"
                }
                is MeshtasticProto.FromRadio.Packet -> if (msg.portnum == MeshtasticProto.PRIVATE_APP) {
                    val src = MeshtasticProto.nodeIdOf(msg.from)
                    val payload = msg.payload
                    later(rx) { onFrame(payload, src) }
                }
                else -> {}
            }
        }
    }

    override fun send(frame: ByteArray, dest: String) {
        pacer.waitTurn()
        if (!ready) throw IllegalStateException("radio not connected")
        val bytes = MeshtasticProto.toRadioPacket(frame, dest, channel, hopLimit)
        val ok = ops.submit<Boolean> { write(bytes) }.get(30, TimeUnit.SECONDS)
        if (!ok) throw IllegalStateException("radio did not accept the packet")
    }

    override fun close() {
        closed = true
        ready = false
        val g = gatt
        if (g != null) {
            // Let the radio hear the goodbye before letting go: closing at once can leave the
            // radio thinking this phone is still connected, so the next connection fails.
            val bye = CountDownLatch(1)
            disconnected = bye
            runCatching { g.disconnect() }
            runCatching { bye.await(1500, TimeUnit.MILLISECONDS) }
            runCatching { g.close() }
            gatt = null
        }
        timer.shutdownNow()
        ops.shutdownNow()
        rx.shutdownNow()
    }

    companion object {
        private const val TAG = "BleTransport"
        val SERVICE: UUID = UUID.fromString(MeshtasticProto.SERVICE_UUID)
        val TORADIO: UUID = UUID.fromString(MeshtasticProto.TORADIO_UUID)
        val FROMRADIO: UUID = UUID.fromString(MeshtasticProto.FROMRADIO_UUID)
        val FROMNUM: UUID = UUID.fromString(MeshtasticProto.FROMNUM_UUID)
        val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    }
}
