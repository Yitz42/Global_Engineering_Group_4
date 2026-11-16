package org.meshlearn.phone

import android.content.Context
import android.os.Environment
import android.os.StatFs
import java.io.File
import java.net.Inet4Address
import java.net.NetworkInterface

/** What the teacher sets up once on the phone. */
class Settings(context: Context) {
    private val prefs = context.getSharedPreferences("meshlearn", Context.MODE_PRIVATE)

    var phoneName: String
        get() = prefs.getString("name", "Student phone")!!
        set(v) = prefs.edit().putString("name", v).apply()
    var hubId: String
        get() = prefs.getString("hub", "")!!
        set(v) = prefs.edit().putString("hub", v).apply()
    /** "ble", "wifi" or "none". */
    var radioMode: String
        get() = prefs.getString("radio_mode", "ble")!!
        set(v) = prefs.edit().putString("radio_mode", v).apply()
    var bleAddress: String
        get() = prefs.getString("ble_address", "")!!
        set(v) = prefs.edit().putString("ble_address", v).apply()
    var bleName: String
        get() = prefs.getString("ble_name", "")!!
        set(v) = prefs.edit().putString("ble_name", v).apply()
    var radioHost: String
        get() = prefs.getString("radio_host", "")!!
        set(v) = prefs.edit().putString("radio_host", v).apply()
    var channel: Int
        get() = prefs.getInt("channel", 0)
        set(v) = prefs.edit().putInt("channel", v).apply()
    /** Most relays a packet may take; -1 = the radio's own setting. */
    var hopLimit: Int
        get() = prefs.getInt("hops", -1)
        set(v) = prefs.edit().putInt("hops", v).apply()
    var batchSeconds: Int
        get() = prefs.getInt("batch", 120)
        set(v) = prefs.edit().putInt("batch", v).apply()
    var adminPin: String
        get() = prefs.getString("pin", "")!!
        set(v) = prefs.edit().putString("pin", v).apply()
    var kiwixUrl: String
        get() = prefs.getString("kiwix", "")!!
        set(v) = prefs.edit().putString("kiwix", v).apply()
    var port: Int
        get() = prefs.getInt("port", 8080)
        set(v) = prefs.edit().putInt("port", v).apply()
    var autostart: Boolean
        get() = prefs.getBoolean("autostart", true)
        set(v) = prefs.edit().putBoolean("autostart", v).apply()
    /** Whether the service was running, so a reboot brings it back. */
    var wantRunning: Boolean
        get() = prefs.getBoolean("want_running", false)
        set(v) = prefs.edit().putBoolean("want_running", v).apply()
    /** Where MeshLearn keeps its files: CARD, PHONE, or "" (the card when there is one). */
    var storage: String
        get() = prefs.getString("storage", "")!!
        set(v) = prefs.edit().putString("storage", v).apply()

    /** One place MeshLearn can keep its files. [folder] is the MeshLearn folder inside it. */
    data class Place(val kind: String, val folder: File, val free: Long, val total: Long)

    companion object {
        const val CARD = "card"
        const val PHONE = "phone"

        /**
         * The memory card and the phone's own storage, whichever this phone has. Both are the app's
         * folder in Android/data/org.meshlearn.phone/files, which needs no permission and which a
         * computer can fill: the card in a card reader, the phone's storage over a USB cable.
         */
        fun places(context: Context): List<Place> {
            val out = LinkedHashMap<String, Place>()
            for (dir in context.getExternalFilesDirs(null).filterNotNull()) {
                val removable = runCatching { Environment.isExternalStorageRemovable(dir) }.getOrDefault(false)
                val kind = if (removable) CARD else PHONE
                if (kind !in out) out[kind] = place(kind, dir)
            }
            if (PHONE !in out) out[PHONE] = place(PHONE, context.filesDir)   // no shared storage: the app's private folder
            return listOf(CARD, PHONE).mapNotNull { out[it] }
        }

        private fun place(kind: String, base: File): Place {
            val stat = runCatching { StatFs(base.path) }.getOrNull()
            return Place(kind, File(base, "meshlearn"), stat?.availableBytes ?: 0L, stat?.totalBytes ?: 0L)
        }

        /** The place in use: the one chosen, else the card when there is one. */
        fun currentPlace(context: Context): Place {
            val all = places(context)
            val chosen = Settings(context).storage
            return all.firstOrNull { it.kind == chosen } ?: all.firstOrNull { it.kind == CARD } ?: all.first()
        }

        /** The MeshLearn folder: where students' work, lessons and books are kept. */
        fun dataDir(context: Context): File = currentPlace(context).folder.apply { mkdirs() }

        /** The phone's own addresses students can type, hotspot first. */
        fun localAddresses(): List<String> {
            val preferred = listOf("ap", "swlan", "softap", "wlan")
            return runCatching {
                NetworkInterface.getNetworkInterfaces().toList()
                    .filter { it.isUp && !it.isLoopback }
                    .sortedBy { iface -> preferred.indexOfFirst { iface.name.startsWith(it) }.let { if (it < 0) 99 else it } }
                    .flatMap { iface -> iface.inetAddresses.toList().filterIsInstance<Inet4Address>().map { it.hostAddress ?: "" } }
                    .filter { it.isNotEmpty() }
            }.getOrDefault(emptyList())
        }
    }
}
