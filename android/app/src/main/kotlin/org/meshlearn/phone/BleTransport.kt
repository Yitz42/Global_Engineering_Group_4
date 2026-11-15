package org.meshlearn.phone

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
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
        gatt = if (Build.VERSION.SDK_INT >= 23) device.connectGatt(context, false, callback, BluetoothDevice.TRANSPORT_LE)
        else device.connectGatt(context, false, callback)
    }

    private fun retryLater() {
        if (closed) return
        ops.execute {
            Thread.sleep(5000)
            if (!closed && !ready) connect()
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
                ops.execute { setUp(g) }
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                ready = false
                done(false)
                g.close()
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
            if (c.uuid == FROMNUM) ops.execute { drain() }
        }

        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) {
            if (c.uuid == FROMNUM) ops.execute { drain() }
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
                    g.writeDescriptor(d, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE) == BluetoothGatt.GATT_SUCCESS
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
                g.writeCharacteristic(c, bytes, BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) == BluetoothGatt.GATT_SUCCESS
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
                    rx.execute { runCatching { onFrame(payload, src) }.onFailure { Log.w(TAG, "frame handler failed", it) } }
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
        runCatching { gatt?.disconnect() }
        runCatching { gatt?.close() }
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
