package org.meshlearn.phone

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import android.text.InputType
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

/**
 * The teacher's screen on the phone: is it running, what address students open, is the radio
 * connected, and the one-time setup. Built in code (no layout files) to keep the app small.
 */
class MainActivity : Activity() {
    private lateinit var settings: Settings
    private lateinit var statusText: TextView
    private lateinit var startButton: Button
    private lateinit var radioLabel: TextView
    private val ui = Handler(Looper.getMainLooper())
    private val fields = LinkedHashMap<String, EditText>()
    private lateinit var modeGroup: RadioGroup
    private lateinit var autostart: CheckBox

    private val refresh = object : Runnable {
        override fun run() {
            showStatus()
            ui.postDelayed(this, 2000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settings = Settings(this)
        setContentView(buildUi())
        loadFields()
    }

    override fun onResume() {
        super.onResume()
        ui.post(refresh)
    }

    override fun onPause() {
        super.onPause()
        ui.removeCallbacks(refresh)
    }

    // -- layout -----------------------------------------------------------------

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun heading(text: String) = TextView(this).apply {
        this.text = text
        textSize = 20f
        setTypeface(typeface, Typeface.BOLD)
        setPadding(0, dp(20), 0, dp(6))
    }

    private fun note(text: String) = TextView(this).apply {
        this.text = text
        textSize = 14f
        alpha = 0.75f
    }

    private fun field(key: String, label: String, hint: String = "", number: Boolean = false): LinearLayout =
        LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(TextView(context).apply { text = label; textSize = 15f; setPadding(0, dp(8), 0, 0) })
            val edit = EditText(context).apply {
                this.hint = hint
                inputType = if (number) InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_SIGNED else InputType.TYPE_CLASS_TEXT
                isSingleLine = true
            }
            fields[key] = edit
            addView(edit, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
        }

    private fun button(text: String, onClick: () -> Unit) = Button(this).apply {
        this.text = text
        isAllCaps = false
        setOnClickListener { onClick() }
    }

    private fun buildUi(): View {
        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(16), dp(8), dp(16), dp(32))
        }
        statusText = TextView(this).apply { textSize = 16f; setLineSpacing(0f, 1.25f) }
        col.addView(heading("Status"))
        col.addView(statusText)
        startButton = button("Start") { toggleRunning() }
        col.addView(startButton)
        col.addView(button("Open the student site") { openSite("/", "Student site") })
        col.addView(button("Phone status and loading assignments") { openSite("/status", "Phone status") })
        col.addView(button("Send waiting messages now") {
            PhoneService.instance?.retryNow()
            Toast.makeText(this, "Retrying every waiting message", Toast.LENGTH_SHORT).show()
        })

        col.addView(heading("Setup"))
        col.addView(note("Set these once. Changes take effect when you press Save (the site restarts if it is running)."))
        col.addView(field("name", "This phone's name (the teacher sees it)", "North village"))
        col.addView(field("hub", "Teacher hub's radio id", "!a1b2c3d4"))

        col.addView(TextView(this).apply { text = "How this phone reaches its radio"; textSize = 15f; setPadding(0, dp(12), 0, 0) })
        modeGroup = RadioGroup(this).apply {
            addView(RadioButton(context).apply { id = MODE_BLE; text = "Bluetooth" })
            addView(RadioButton(context).apply { id = MODE_WIFI; text = "Wi-Fi (radio on the same network)" })
            addView(RadioButton(context).apply { id = MODE_NONE; text = "No radio yet (work waits on the phone)" })
        }
        col.addView(modeGroup)
        radioLabel = note("")
        col.addView(radioLabel)
        col.addView(button("Find my radio (Bluetooth)") { findRadio() })
        col.addView(field("radio_host", "Wi-Fi radio address", "192.168.43.20"))
        col.addView(field("channel", "Radio channel index (use your private channel, not 0)", "3", number = true))
        col.addView(field("hops", "Most relays a packet may take (blank: the radio's setting)", "", number = true))

        col.addView(heading("Advanced"))
        col.addView(field("batch", "Seconds to collect students' work before sending", "120", number = true))
        col.addView(field("pin", "Teacher PIN for loading assignments from another device (optional)", ""))
        col.addView(field("kiwix", "Kiwix address for the big encyclopedias (optional)", "http://192.168.43.1:8090"))
        col.addView(field("port", "Web port", "8080", number = true))
        autostart = CheckBox(this).apply { text = "Start again when the phone turns on" }
        col.addView(autostart)
        col.addView(button("Save") { save() })
        col.addView(note("\nMemory card folder for the library and course packs:\n${Settings.dataDir(this)}\n" +
            "Put collections in its 'library' folder and course packs in 'assignments' (with a card reader, or over school Wi-Fi from the status page)."))

        return ScrollView(this).apply { addView(col) }
    }

    // -- status -------------------------------------------------------------------

    private fun showStatus() {
        val st = PhoneService.instance?.snapshot() ?: PhoneService.state
        startButton.text = if (st.running) "Stop" else "Start"
        statusText.text = if (!st.running) {
            "Stopped." + (if (st.error.isNotEmpty()) "\nCould not start: ${st.error}" else "") +
                (if (settings.hubId.isBlank()) "\nFill in the teacher hub's radio id below first." else "")
        } else {
            val addrs = Settings.localAddresses().ifEmpty { listOf("<this phone's hotspot address>") }
            "Running.\nStudents open: " + addrs.joinToString(" or ") { "http://$it:${st.port}" } +
                "\n(Turn on this phone's Wi-Fi hotspot so students can join it.)" +
                "\nRadio: ${st.radio}" + (if (st.nodeId.isNotEmpty()) "\nThis phone's radio id: ${st.nodeId}" else "") +
                "\nMessages waiting for the radio: ${st.pending} · packets sent: ${st.framesSent}"
        }
        radioLabel.text = if (settings.bleName.isNotEmpty() || settings.bleAddress.isNotEmpty())
            "Bluetooth radio: ${settings.bleName.ifEmpty { "?" }} (${settings.bleAddress})" else "No Bluetooth radio chosen yet."
    }

    private fun toggleRunning() {
        if (PhoneService.instance != null) {
            PhoneService.stop(this)
        } else {
            if (!ensurePermissions()) return
            PhoneService.start(this)
        }
        ui.postDelayed({ showStatus() }, 500)
    }

    private fun openSite(path: String, title: String) {
        if (PhoneService.instance == null) {
            Toast.makeText(this, "Press Start first.", Toast.LENGTH_SHORT).show()
            return
        }
        startActivity(Intent(this, StudentSiteActivity::class.java)
            .putExtra(StudentSiteActivity.EXTRA_URL, "http://127.0.0.1:${settings.port}$path")
            .putExtra(StudentSiteActivity.EXTRA_TITLE, title))
    }

    // -- settings -----------------------------------------------------------------

    private fun loadFields() {
        fields["name"]!!.setText(settings.phoneName)
        fields["hub"]!!.setText(settings.hubId)
        fields["radio_host"]!!.setText(settings.radioHost)
        fields["channel"]!!.setText(settings.channel.toString())
        fields["hops"]!!.setText(if (settings.hopLimit >= 0) settings.hopLimit.toString() else "")
        fields["batch"]!!.setText(settings.batchSeconds.toString())
        fields["pin"]!!.setText(settings.adminPin)
        fields["kiwix"]!!.setText(settings.kiwixUrl)
        fields["port"]!!.setText(settings.port.toString())
        modeGroup.check(when (settings.radioMode) { "wifi" -> MODE_WIFI; "none" -> MODE_NONE; else -> MODE_BLE })
        autostart.isChecked = settings.autostart
    }

    private fun text(key: String) = fields[key]!!.text.toString().trim()

    private fun save() {
        val hub = text("hub")
        if (hub.isNotEmpty() && !Regex("^!?[0-9a-fA-F]{8}$").matches(hub)) {
            Toast.makeText(this, "The hub's radio id looks like !a1b2c3d4 (8 letters and digits).", Toast.LENGTH_LONG).show()
            return
        }
        settings.phoneName = text("name").ifEmpty { "Student phone" }
        settings.hubId = if (hub.isEmpty() || hub.startsWith("!")) hub.lowercase() else "!" + hub.lowercase()
        settings.radioHost = text("radio_host")
        settings.channel = text("channel").toIntOrNull() ?: 0
        settings.hopLimit = text("hops").toIntOrNull()?.coerceIn(0, 7) ?: -1
        settings.batchSeconds = (text("batch").toIntOrNull() ?: 120).coerceIn(5, 3600)
        settings.adminPin = text("pin")
        settings.kiwixUrl = text("kiwix")
        settings.port = (text("port").toIntOrNull() ?: 8080).coerceIn(1024, 65535)
        settings.radioMode = when (modeGroup.checkedRadioButtonId) { MODE_WIFI -> "wifi"; MODE_NONE -> "none"; else -> "ble" }
        settings.autostart = autostart.isChecked
        loadFields()
        if (PhoneService.instance != null) {
            PhoneService.stop(this)
            ui.postDelayed({ PhoneService.start(this) }, 800)
        }
        Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
        if (settings.channel == 0) {
            Toast.makeText(this, "Channel 0 is the public channel. Use the team's private channel.", Toast.LENGTH_LONG).show()
        }
    }

    // -- Bluetooth ----------------------------------------------------------------

    private fun neededPermissions(): List<String> {
        val out = ArrayList<String>()
        if (Build.VERSION.SDK_INT >= 31) {
            out += Manifest.permission.BLUETOOTH_SCAN
            out += Manifest.permission.BLUETOOTH_CONNECT
        } else {
            out += Manifest.permission.ACCESS_FINE_LOCATION
        }
        if (Build.VERSION.SDK_INT >= 33) out += Manifest.permission.POST_NOTIFICATIONS
        return out.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
    }

    /** Asks for anything missing; returns true when everything is already granted. */
    private fun ensurePermissions(): Boolean {
        if (Build.VERSION.SDK_INT < 23) return true
        val missing = neededPermissions()
        if (missing.isEmpty()) return true
        requestPermissions(missing.toTypedArray(), 1)
        return false
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (grantResults.any { it != PackageManager.PERMISSION_GRANTED }) {
            Toast.makeText(this, "Bluetooth permission is needed to talk to the radio.", Toast.LENGTH_LONG).show()
        }
    }

    @SuppressLint("MissingPermission")
    private fun findRadio() {
        if (!ensurePermissions()) return
        val adapter = (getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager).adapter
        if (adapter == null || !adapter.isEnabled) {
            Toast.makeText(this, "Turn on Bluetooth first.", Toast.LENGTH_LONG).show()
            return
        }
        val scanner = adapter.bluetoothLeScanner ?: return
        val found = LinkedHashMap<String, String>()   // address -> name
        val names = ArrayList<String>()
        val listAdapter = android.widget.ArrayAdapter(this, android.R.layout.simple_list_item_1, names)
        // Radios already paired come first: they don't always advertise while connected.
        adapter.bondedDevices.filter { (it.name ?: "").startsWith("Meshtastic") }.forEach {
            found[it.address] = it.name ?: it.address
            names.add("${it.name} (paired)")
        }
        val callback = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                val addr = result.device.address
                if (addr in found) return
                val name = result.scanRecord?.deviceName ?: result.device.name ?: addr
                found[addr] = name
                runOnUiThread { names.add(name); listAdapter.notifyDataSetChanged() }
            }
        }
        val filter = ScanFilter.Builder().setServiceUuid(ParcelUuid(BleTransport.SERVICE)).build()
        scanner.startScan(listOf(filter), ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(), callback)
        val dialog = AlertDialog.Builder(this)
            .setTitle("Looking for Meshtastic radios…")
            .setAdapter(listAdapter) { _, which ->
                val addr = found.keys.elementAt(which)
                settings.bleAddress = addr
                settings.bleName = found[addr] ?: ""
                settings.radioMode = "ble"
                modeGroup.check(MODE_BLE)
                showStatus()
                Toast.makeText(this, "Chosen. If Android asks, type the PIN shown on the radio. Press Save to use it.", Toast.LENGTH_LONG).show()
            }
            .setNegativeButton("Cancel", null)
            .setOnDismissListener { runCatching { scanner.stopScan(callback) } }
            .create()
        dialog.show()
        ui.postDelayed({ runCatching { scanner.stopScan(callback) } }, 15_000)
    }

    companion object {
        private const val MODE_BLE = 101
        private const val MODE_WIFI = 102
        private const val MODE_NONE = 103
    }
}
