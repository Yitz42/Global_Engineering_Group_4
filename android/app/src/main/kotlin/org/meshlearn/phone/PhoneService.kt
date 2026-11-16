package org.meshlearn.phone

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import fi.iki.elonen.NanoHTTPD
import org.meshlearn.core.Messenger
import org.meshlearn.core.NoRadioTransport
import org.meshlearn.core.Phone
import org.meshlearn.core.TcpRadioTransport
import org.meshlearn.core.Transport
import org.meshlearn.core.long
import java.io.File

/**
 * Runs the student site and the radio link all day, with the screen off, as a foreground
 * service (Android shows a notification while it runs).
 */
class PhoneService : Service() {
    private var phone: Phone? = null
    private var server: NanoHTTPD? = null
    private var transport: Transport? = null
    private var outbox: AndroidDb? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startInForeground()
        if (phone == null) {
            try {
                startPhone()
            } catch (e: Exception) {
                Log.e(TAG, "could not start", e)
                state = state.copy(running = false, error = e.message ?: e.javaClass.simpleName)
                stopSelf()
            }
        }
        return START_STICKY
    }

    private fun startPhone() {
        val s = Settings(this)
        val dataDir = Settings.dataDir(this)
        val hops = s.hopLimit.takeIf { it >= 0 }
        val t: Transport = when (s.radioMode) {
            "ble" -> if (s.bleAddress.isNotEmpty()) BleTransport(this, s.bleAddress, s.channel, hops) else NoRadioTransport()
            "wifi" -> if (s.radioHost.isNotEmpty()) TcpRadioTransport(s.radioHost, channel = s.channel, hopLimit = hops) else NoRadioTransport()
            else -> NoRadioTransport()
        }
        transport = t
        val p = Phone(
            dataDir, s.hubId.trim(), s.phoneName.trim().ifEmpty { "Student phone" },
            makeDb = { f, schema -> AndroidDb(f, schema) },
            messengerFactory = { handler ->
                val db = AndroidDb(File(dataDir, "outbox.db"), "").also { outbox = it }
                Messenger(t, db, handler, retryS = 30.0)
            },
            kiwixUrl = s.kiwixUrl, batchS = s.batchSeconds.toDouble(), adminPin = s.adminPin,
        )
        p.start()
        if (s.hubId.isNotBlank()) p.sayHello()
        val srv = p.app.server(null, s.port)     // every network, including the hotspot
        srv.start(NanoHTTPD.SOCKET_READ_TIMEOUT, true)
        phone = p
        server = srv
        wakeLock = (getSystemService(Context.POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "meshlearn:phone").apply {
                @Suppress("WakelockTimeout")   // held all school day on purpose; released when the service stops
                acquire()
            }
        state = PhoneState(running = true, port = s.port, dataDir = dataDir.path)
        instance = this
    }

    /** For the app's screen. */
    fun snapshot(): PhoneState {
        val p = phone ?: return state
        val radio = when (val t = transport) {
            is BleTransport -> t.status
            is TcpRadioTransport -> t.status
            is NoRadioTransport -> "No radio set up: work is saved and waits"
            else -> ""
        }
        val radioOk = when (val t = transport) {
            is BleTransport -> t.status.startsWith("Connected")
            is TcpRadioTransport -> t.status.startsWith("Connected")
            else -> false
        }
        val q = runCatching { p.messenger.status() }.getOrDefault(emptyMap())
        fun count(sql: String) = runCatching { p.db.one(sql)?.long("n") }.getOrNull() ?: 0L
        return state.copy(
            radio = radio, radioOk = radioOk, nodeId = p.nodeId,
            pending = q["pending"] ?: 0, framesSent = q["frames_sent"] ?: 0,
            unbatched = count("SELECT COUNT(*) AS n FROM submissions WHERE msg_id IS NULL"),
            assignments = count("SELECT COUNT(*) AS n FROM assignments"),
            lastHeard = runCatching { p.messenger.lastHeard() }.getOrDefault(0.0),
        )
    }

    /** Send students' saved work now: batch it, and retry anything waiting. */
    fun sendNow() {
        phone?.flush()
        phone?.messenger?.retryNow()
    }

    /** Read course packs put in the assignments folder (memory card or cable). Returns how many. */
    fun reloadPacks(): Int {
        val p = phone ?: return 0
        val n = p.loadPackFolder()
        if (p.hubId.isNotBlank()) p.sayHello()
        return n
    }

    private fun startInForeground() {
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(NotificationChannel(CHANNEL, "Student site", NotificationManager.IMPORTANCE_LOW))
        }
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val builder = if (Build.VERSION.SDK_INT >= 26) Notification.Builder(this, CHANNEL) else @Suppress("DEPRECATION") Notification.Builder(this)
        val notification = builder
            .setContentTitle("MeshLearn is running")
            .setContentText("Students can open the site on this phone's hotspot.")
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    override fun onDestroy() {
        runCatching { server?.stop() }
        runCatching { phone?.stop() }
        runCatching { phone?.db?.close() }
        runCatching { outbox?.close() }   // closed too, so the folder can be moved
        runCatching { wakeLock?.release() }
        phone = null
        outbox = null
        server = null
        instance = null
        state = state.copy(running = false)
        super.onDestroy()
    }

    data class PhoneState(
        val running: Boolean = false,
        val port: Int = 8080,
        val dataDir: String = "",
        val radio: String = "",
        val radioOk: Boolean = false,
        val nodeId: String = "",
        val pending: Long = 0,
        val framesSent: Long = 0,
        val unbatched: Long = 0,
        val assignments: Long = 0,
        val lastHeard: Double = 0.0,
        val error: String = "",
    )

    companion object {
        private const val TAG = "PhoneService"
        private const val CHANNEL = "phone"
        private const val NOTIFICATION_ID = 1
        @Volatile var state = PhoneState()
        @Volatile var instance: PhoneService? = null

        fun start(context: Context) {
            Settings(context).wantRunning = true
            val i = Intent(context, PhoneService::class.java)
            if (Build.VERSION.SDK_INT >= 26) context.startForegroundService(i) else context.startService(i)
        }

        fun stop(context: Context) {
            Settings(context).wantRunning = false
            context.stopService(Intent(context, PhoneService::class.java))
        }
    }
}
