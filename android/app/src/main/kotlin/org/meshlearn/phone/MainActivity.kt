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
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.graphics.drawable.RippleDrawable
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import android.text.Editable
import android.text.InputFilter
import android.text.InputType
import android.text.TextWatcher
import android.view.Gravity
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.widget.CheckBox
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import android.window.OnBackInvokedDispatcher
import org.meshlearn.core.StorageMove
import java.io.File

/**
 * The phone keeper's screens, following the "Phone setup" and "Getting lessons onto a phone"
 * designs: first-time setup one step per screen, then a status screen that says whether
 * everything works and what to do when it doesn't, plus settings and adding lessons.
 * Built in code (no layout files) to keep the app small.
 */
class MainActivity : Activity() {
    private enum class Screen { WELCOME, NAME, RADIO, SCHOOL, CHECK, DONE, JOIN, STATUS, LESSONS, SETTINGS }

    private lateinit var settings: Settings
    private lateinit var frame: FrameLayout
    private val ui = Handler(Looper.getMainLooper())
    private val stack = ArrayList<Screen>()
    /** A setup step opened from Settings: it saves and goes back instead of going on. */
    private var editing = false
    /** The screen's live update, run every 2 seconds while it shows. */
    private var live: (() -> Unit)? = null
    /** Run when the screen goes away (stops a Bluetooth scan). */
    private var leave: (() -> Unit)? = null
    private var startAfterPermission = false
    private var scanAfterPermission = false
    private var rescanOnResume = false

    // Settings screen parts the storage code updates.
    private val fields = LinkedHashMap<String, EditText>()
    private lateinit var storageGroup: RadioGroup
    private lateinit var storageNote: TextView
    private lateinit var folderNote: TextView
    private var settingStorage = false   // true while the screen itself checks a storage button

    private val refresh = object : Runnable {
        override fun run() {
            live?.invoke()
            ui.postDelayed(this, 2000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settings = Settings(this)
        frame = FrameLayout(this)
        padForSystemBars(frame)
        setContentView(frame)
        val saved = savedInstanceState?.getStringArrayList("screens")?.mapNotNull { n -> Screen.values().firstOrNull { it.name == n } }
        if (!saved.isNullOrEmpty()) {
            stack.addAll(saved)
            editing = savedInstanceState.getBoolean("editing")
        } else {
            stack.add(if (settings.setupDone) Screen.STATUS else Screen.WELCOME)
        }
        if (Build.VERSION.SDK_INT >= 33) {
            // Apps for Android 16+ get back gestures here, not in onBackPressed.
            onBackInvokedDispatcher.registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT) { back() }
        }
        render()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putStringArrayList("screens", ArrayList(stack.map { it.name }))
        outState.putBoolean("editing", editing)
    }

    override fun onResume() {
        super.onResume()
        when {
            rescanOnResume && stack.last() == Screen.RADIO -> { rescanOnResume = false; render() }
            stack.last() == Screen.SETTINGS -> showStorage()   // a memory card may have been put in or taken out
        }
        ui.post(refresh)
    }

    override fun onPause() {
        super.onPause()
        ui.removeCallbacks(refresh)
    }

    /** Android 7-12 only; newer versions use the callback registered in onCreate. */
    @Deprecated("Android 13+ uses the back callback registered in onCreate")
    @SuppressLint("GestureBackNavigation")
    override fun onBackPressed() = back()

    // -- moving between screens ----------------------------------------------------

    private fun go(screen: Screen, edit: Boolean = editing) {
        stack.add(screen)
        editing = edit
        render()
    }

    private fun reset(vararg screens: Screen) {
        stack.clear()
        stack.addAll(screens)
        editing = false
        render()
    }

    private fun back() {
        if (stack.size <= 1) {
            finish()
            return
        }
        stack.removeAt(stack.size - 1)
        if (stack.last() == Screen.SETTINGS || stack.last() == Screen.STATUS) editing = false
        render()
    }

    private fun render() {
        leave?.invoke()
        leave = null
        live = null
        val view = when (stack.last()) {
            Screen.WELCOME -> welcomeScreen()
            Screen.NAME -> nameScreen()
            Screen.RADIO -> radioScreen()
            Screen.SCHOOL -> schoolScreen()
            Screen.CHECK -> checkScreen()
            Screen.DONE -> joinScreen(firstTime = true)
            Screen.JOIN -> joinScreen(firstTime = false)
            Screen.STATUS -> statusScreen()
            Screen.LESSONS -> lessonsScreen()
            Screen.SETTINGS -> settingsScreen()
        }
        frame.removeAllViews()
        frame.addView(view, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))
        live?.invoke()
    }

    // -- building blocks -------------------------------------------------------------

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun shape(fill: Int, radius: Int, line: Int = 0, lineWidth: Int = 0) = GradientDrawable().apply {
        setColor(fill)
        cornerRadius = dp(radius).toFloat()
        if (lineWidth > 0) setStroke(dp(lineWidth), line)
    }

    private fun ripple(fill: Int, radius: Int, line: Int = 0, lineWidth: Int = 0) =
        RippleDrawable(ColorStateList.valueOf(0x22000000), shape(fill, radius, line, lineWidth), null)

    private fun tap(v: View, onClick: () -> Unit) {
        v.isClickable = true
        v.isFocusable = true
        v.setOnClickListener { onClick() }
    }

    private fun space(n: Int, across: Boolean = false) = GradientDrawable().apply {
        setColor(Color.TRANSPARENT)
        if (across) setSize(dp(n), 1) else setSize(1, dp(n))
    }

    private fun column(gap: Int = 0) = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        if (gap > 0) { dividerDrawable = space(gap); showDividers = LinearLayout.SHOW_DIVIDER_MIDDLE }
    }

    private fun row(gap: Int = 0) = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        if (gap > 0) { dividerDrawable = space(gap, across = true); showDividers = LinearLayout.SHOW_DIVIDER_MIDDLE }
    }

    private fun label(text: CharSequence, size: Float = 17f, color: Int = INK, bold: Boolean = false) = TextView(this).apply {
        this.text = text
        textSize = size
        setTextColor(color)
        if (bold) typeface = Typeface.DEFAULT_BOLD
        setLineSpacing(0f, 1.2f)
    }

    private fun h1(text: String) = label(text, 28f, INK, true).apply { setLineSpacing(0f, 1.05f) }

    private fun icon(res: Int, color: Int, size: Int = 24) = ImageView(this).apply {
        setImageResource(res)
        setColorFilter(color)
        layoutParams = LinearLayout.LayoutParams(dp(size), dp(size))
    }

    private fun iconBox(res: Int, fg: Int, bg: Int, box: Int = 44, size: Int = 24, radius: Int = 12) = FrameLayout(this).apply {
        background = shape(bg, radius)
        addView(icon(res, fg, size), FrameLayout.LayoutParams(dp(size), dp(size), Gravity.CENTER))
        layoutParams = LinearLayout.LayoutParams(dp(box), dp(box))
    }

    private fun iconButton(res: Int, description: String, onClick: () -> Unit) = FrameLayout(this).apply {
        contentDescription = description
        background = RippleDrawable(ColorStateList.valueOf(0x22000000), null, shape(Color.BLACK, 24))
        addView(icon(res, INK), FrameLayout.LayoutParams(dp(24), dp(24), Gravity.CENTER))
        layoutParams = LinearLayout.LayoutParams(dp(48), dp(48))
        tap(this, onClick)
    }

    /** A grow-to-fill child of a row. */
    private fun fill(v: View): View = v.apply { layoutParams = LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f) }

    private fun titled(title: String, sub: String?, titleSize: Float = 17f, bold: Boolean = true) = column(2).apply {
        addView(label(title, titleSize, INK, bold))
        if (!sub.isNullOrEmpty()) addView(label(sub, 14f, MUTED))
    }

    private fun bigButton(text: String, color: Int = BLUE, onClick: () -> Unit) = TextView(this).apply {
        this.text = text
        textSize = 19f
        typeface = Typeface.DEFAULT_BOLD
        gravity = Gravity.CENTER
        minHeight = dp(58)
        setPadding(dp(16), dp(8), dp(16), dp(8))
        tap(this, onClick)
        look(this, true, color)
    }

    /** A big button switched on (its colour) or off (grey, does nothing). */
    private fun look(b: TextView, on: Boolean, color: Int = BLUE) {
        b.isEnabled = on
        b.background = ripple(if (on) color else TRACK, 14)
        b.setTextColor(if (on) Color.WHITE else OFF_TEXT)
    }

    private fun link(text: String, size: Float = 16f, center: Boolean = true, onClick: () -> Unit) = TextView(this).apply {
        this.text = text
        textSize = size
        typeface = Typeface.DEFAULT_BOLD
        setTextColor(BLUE)
        gravity = if (center) Gravity.CENTER else Gravity.CENTER_VERTICAL
        minHeight = dp(48)
        background = RippleDrawable(ColorStateList.valueOf(0x22000000), null, shape(Color.BLACK, 8))
        tap(this, onClick)
    }

    private fun input(value: String, hint: String) = EditText(this).apply {
        setText(value)
        this.hint = hint
        textSize = 19f
        setTextColor(INK)
        setHintTextColor(HINT)
        isSingleLine = true
        minHeight = dp(56)
        setPadding(dp(16), 0, dp(16), 0)
        background = shape(Color.WHITE, 12, BLUE, 2)
    }

    /** An on/off switch in the design's green. */
    private fun onOff() = Switch(this).apply {
        val states = arrayOf(intArrayOf(android.R.attr.state_checked), intArrayOf())
        thumbTintList = ColorStateList(states, intArrayOf(GREEN, Color.WHITE))
        trackTintList = ColorStateList(states, intArrayOf(GREEN_TRACK, TRACK))
    }

    private fun fieldLabel(text: String) = label(text, 15f, INK, true).apply { setPadding(0, dp(8), 0, 0) }

    private fun EditText.onChange(f: () -> Unit) = addTextChangedListener(object : TextWatcher {
        override fun afterTextChanged(s: Editable?) = f()
        override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
        override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}
    })

    /** A blue note with an "i". */
    private fun tip(text: String) = row(12).apply {
        gravity = Gravity.TOP
        setPadding(dp(14), dp(12), dp(14), dp(12))
        background = shape(BLUE_SOFT, 12)
        addView(icon(R.drawable.ic_info, TIP_TEXT, 22))
        addView(fill(label(text, 15f, TIP_TEXT)))
    }

    /** A white card holding rows separated by thin lines. */
    private fun listCard(vararg rows: View) = column().apply {
        background = shape(Color.WHITE, 16, LINE, 1)
        clipToOutline = true
        rows.forEachIndexed { i, r ->
            if (i > 0) addView(View(context).apply { setBackgroundColor(DIVIDER) }, LinearLayout.LayoutParams(MATCH_PARENT, 1))
            addView(r)
        }
    }

    private fun listRow(iconRes: Int?, title: String, sub: String? = null, onClick: () -> Unit) = row(14).apply {
        minimumHeight = dp(60)
        setPadding(dp(16), dp(8), dp(12), dp(8))
        background = RippleDrawable(ColorStateList.valueOf(0x22000000), null, shape(Color.BLACK, 0))
        if (iconRes != null) addView(icon(iconRes, BLUE, 22))
        addView(fill(titled(title, sub, 17f, bold = false)))
        addView(icon(R.drawable.ic_chevron, MUTED, 20))
        tap(this, onClick)
    }

    /** A heading row that opens and closes what is under it. */
    private fun expander(title: String, sub: String?, body: View) = column().apply {
        background = shape(Color.WHITE, 14, LINE, 1)
        val chevron = icon(R.drawable.ic_chevron, MUTED, 20).apply { rotation = 90f }
        val head = row(12).apply {
            minimumHeight = dp(56)
            setPadding(dp(14), dp(8), dp(14), dp(8))
            addView(fill(titled(title, sub, 17f)))
            addView(chevron)
        }
        body.visibility = View.GONE
        tap(head) {
            val open = body.visibility != View.VISIBLE
            body.visibility = if (open) View.VISIBLE else View.GONE
            chevron.rotation = if (open) 270f else 90f
        }
        addView(head)
        addView(body)
    }

    private fun numbered(steps: List<String>) = column(8).apply {
        steps.forEachIndexed { i, s ->
            addView(row(12).apply {
                gravity = Gravity.TOP
                setPadding(dp(14), dp(12), dp(14), dp(12))
                background = shape(Color.WHITE, 14, LINE, 1)
                addView(label("${i + 1}", 15f, BLUE, true).apply {
                    gravity = Gravity.CENTER
                    background = shape(BLUE_SOFT, 15)
                    layoutParams = LinearLayout.LayoutParams(dp(30), dp(30))
                })
                addView(fill(label(s, 16f, INK)))
            })
        }
    }

    /** The page: a header, the content that scrolls, and the buttons kept at the bottom. */
    private fun page(bg: Int, header: View?, content: View, bottom: View?): View {
        setBars(bg)
        return column().apply {
            setBackgroundColor(bg)
            header?.let { addView(it, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT)) }
            addView(ScrollView(context).apply {
                isFillViewport = true
                addView(content, FrameLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT))
            }, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
            bottom?.let { addView(it, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT)) }
        }
    }

    private fun content(top: Int = 24) = column(14).apply { setPadding(dp(24), dp(top), dp(24), dp(12)) }

    private fun bottomBar(vararg views: View) = column(6).apply {
        setPadding(dp(24), dp(12), dp(24), dp(24))
        views.forEach { addView(it) }
    }

    private fun backHeader(title: String, bg: Int = Color.WHITE) = row(4).apply {
        setPadding(dp(8), dp(8), dp(8), dp(8))
        setBackgroundColor(bg)
        addView(iconButton(R.drawable.ic_back, "Back") { back() })
        addView(label(title, 20f, INK, true))
    }

    /** "Step 2 of 4" with the progress bars, or a plain back header when changing one setting. */
    private fun stepHeader(step: Int, editTitle: String): View {
        if (editing) return backHeader(editTitle)
        return column(10).apply {
            setPadding(dp(12), dp(10), dp(12), dp(10))
            setBackgroundColor(Color.WHITE)
            addView(row(4).apply {
                addView(iconButton(R.drawable.ic_back, "Back") { back() })
                addView(label("Step $step of 4", 15f, MUTED, true))
            })
            addView(LinearLayout(context).apply {
                setPadding(dp(12), 0, dp(12), 0)
                for (i in 1..4) {
                    addView(View(context).apply {
                        background = shape(if (i < step) GREEN else if (i == step) BLUE else TRACK, 3)
                    }, LinearLayout.LayoutParams(0, dp(6), 1f).apply { marginStart = dp(3); marginEnd = dp(3) })
                }
            })
        }
    }

    @Suppress("DEPRECATION")
    private fun setBars(color: Int) {
        frame.setBackgroundColor(color)
        if (Build.VERSION.SDK_INT < 35) {
            window.statusBarColor = color
            window.navigationBarColor = color
        }
        var flags = View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR
        if (Build.VERSION.SDK_INT >= 26) flags = flags or View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR
        window.decorView.systemUiVisibility = flags
    }

    // -- first-time setup ----------------------------------------------------------------

    private fun welcomeScreen(): View {
        val c = column(18).apply { setPadding(dp(24), dp(40), dp(24), dp(16)) }
        c.addView(row(14).apply {
            addView(iconBox(R.drawable.ic_phone, BLUE, BLUE_SOFT, 72, 40, 20))
            addView(label("· · ·", 22f, DOTS, true))
            addView(iconBox(R.drawable.ic_radio, AMBER_DARK, AMBER_SOFT, 72, 40, 20))
        })
        c.addView(label("Set up this phone for students", 30f, INK, true).apply { setLineSpacing(0f, 1.05f) })
        c.addView(label("It takes about 5 minutes. Have these ready:", 18f, BODY))
        fun ready(res: Int, fg: Int, bg: Int, title: String, sub: String) = row(14).apply {
            gravity = Gravity.TOP
            setPadding(dp(14), dp(14), dp(14), dp(14))
            background = shape(SOFT, 14)
            addView(iconBox(res, fg, bg, 40, 22))
            addView(fill(titled(title, sub)))
        }
        c.addView(ready(R.drawable.ic_radio, AMBER_DARK, AMBER_SOFT, "The radio", "Charged and switched on, next to this phone"))
        c.addView(ready(R.drawable.ic_join, BLUE, BLUE_SOFT, "The school's radio id",
            "From the teacher's sheet: 8 letters and numbers, and a channel number"))
        c.addView(ready(R.drawable.ic_wifi, GREEN, GREEN_SOFT, "Nothing else", "No internet or SIM card needed"))
        val start = bigButton("Start setup  →") { go(Screen.NAME, false) }
        val expert = link("I know what I'm doing: all settings") {
            settings.setupDone = true
            reset(Screen.STATUS, Screen.SETTINGS)
        }
        return page(Color.WHITE, null, c, bottomBar(start, expert))
    }

    private fun nameScreen(): View {
        val c = content(28)
        c.addView(h1("What should we call this phone?"))
        c.addView(label("The teacher sees this name next to the work that comes from here. " +
            "The name of the village or place works well.", 17f, BODY))
        c.addView(fieldLabel("Phone name"))
        val edit = input(settings.phoneName.takeIf { it != "Student phone" } ?: "", "For example: North village")
        c.addView(edit)
        c.addView(label("You can change this later.", 14f, MUTED))
        val next = bigButton(if (editing) "Save" else "Next") {
            settings.phoneName = edit.text.toString().trim()
            if (editing) { restartIfRunning(); back() } else go(Screen.RADIO)
        }
        val check = { look(next, edit.text.isNotBlank()) }
        edit.onChange(check)
        check()
        return page(Color.WHITE, stepHeader(1, "Phone name"), c, bottomBar(next))
    }

    private class RadioRow(val view: LinearLayout, val check: ImageView, val name: String)

    private fun radioScreen(): View {
        val c = content()
        c.addView(h1("Find the radio"))
        val intro = label("Keep the radio switched on and close to this phone. Tap it when it shows up.", 17f, BODY)
        c.addView(intro)
        val searching = label("Looking for radios nearby…", 15f, MUTED)
        c.addView(searching)
        val list = column(10)
        c.addView(list)
        val pinTip = tip("If the phone asks for a PIN, type the number on the radio's screen. A radio with no screen uses 123456.")
        c.addView(pinTip)
        val help = mutableListOf("Check the radio is switched on and its light is blinking.", "Hold it right next to this phone.")
        if (Build.VERSION.SDK_INT < 31) help += "This Android version only finds radios while Location is on: turn it on in the quick settings."
        help += "Still nothing? Close the Meshtastic app if it is open: the radio talks to one phone at a time."
        val helpBox = expander("I don't see my radio", null, numbered(help).apply { setPadding(dp(12), 0, dp(12), dp(12)) })
        c.addView(helpBox)
        val lookAgain = link("Look again", 15f, center = false) { render() }
        c.addView(lookAgain)

        val wifiBox = column(6)
        val host = input(settings.radioHost, "192.168.43.20").apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
        }
        wifiBox.addView(fieldLabel("The radio's Wi-Fi address"))
        wifiBox.addView(host)
        wifiBox.addView(label("The radio shows it in its own app, under Network. It must be on this phone's hotspot or the same Wi-Fi.", 14f, MUTED))
        c.addView(wifiBox)

        var wifi = settings.radioMode == "wifi"
        val rows = LinkedHashMap<String, RadioRow>()
        var picked: String? = settings.bleAddress.takeIf { it.isNotEmpty() }
        val use = bigButton(if (editing) "Save" else "Use this radio") {
            if (wifi) {
                settings.radioHost = host.text.toString().trim()
                settings.radioMode = "wifi"
            } else {
                val addr = picked ?: return@bigButton
                settings.bleAddress = addr
                settings.bleName = rows[addr]?.name ?: settings.bleName
                settings.radioMode = "ble"
            }
            if (editing) { restartIfRunning(); back() } else go(Screen.SCHOOL)
        }
        fun update() {
            rows.forEach { (addr, r) ->
                val on = addr == picked
                r.view.background = ripple(if (on) BLUE_SOFT else Color.WHITE, 14, if (on) BLUE else LINE, 2)
                r.check.visibility = if (on) View.VISIBLE else View.INVISIBLE
            }
            pinTip.visibility = if (!wifi && picked != null && picked in rows) View.VISIBLE else View.GONE
            look(use, if (wifi) host.text.isNotBlank() else picked != null && picked in rows)
        }
        host.onChange { update() }

        fun found(addr: String, name: String, detail: String) {
            if (addr in rows) return
            val check = icon(R.drawable.ic_check, BLUE, 26)
            val v = row(14).apply {
                minimumHeight = dp(72)
                setPadding(dp(14), dp(10), dp(14), dp(10))
                addView(iconBox(R.drawable.ic_radio, AMBER_DARK, AMBER_SOFT))
                addView(fill(titled(name, detail)))
                addView(check)
            }
            rows[addr] = RadioRow(v, check, name)
            tap(v) { picked = addr; update() }
            list.addView(v)
            searching.text = if (rows.size == 1) "Found 1 radio" else "Found ${rows.size} radios"
            update()
        }

        val switchLink = link("", 15f, center = false) {}
        fun showMode() {
            for (v in listOf(intro, searching, list, helpBox, lookAgain)) v.visibility = if (wifi) View.GONE else View.VISIBLE
            wifiBox.visibility = if (wifi) View.VISIBLE else View.GONE
            switchLink.text = if (wifi) "Find a Bluetooth radio instead" else "The radio is on Wi-Fi instead"
            update()
        }
        switchLink.setOnClickListener {
            wifi = !wifi
            showMode()
            if (!wifi && rows.isEmpty()) render()
        }
        c.addView(switchLink)
        c.addView(link(if (editing) "No radio for now" else "I don't have the radio yet: skip", 15f, center = false) {
            settings.radioMode = "none"
            if (editing) { restartIfRunning(); back() } else go(Screen.SCHOOL)
        })
        showMode()
        if (!wifi) scanRadios(::found) { problem -> searching.text = problem }
        return page(Color.WHITE, stepHeader(2, "Radio"), c, bottomBar(use))
    }

    private fun schoolScreen(): View {
        val c = content()
        c.addView(h1("Join the school"))
        c.addView(label("Type the school's radio id and channel number. They are on the teacher's sheet " +
            "and on the Phones page of the teacher hub.", 17f, BODY))
        c.addView(fieldLabel("School radio id"))
        val id = EditText(this).apply {
            setText(settings.hubId.removePrefix("!"))
            hint = "a1b2c3d4"
            textSize = 20f
            typeface = Typeface.MONOSPACE
            letterSpacing = 0.12f
            setTextColor(INK)
            setHintTextColor(HINT)
            isSingleLine = true
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS or InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
            background = null
            setPadding(dp(12), 0, dp(12), 0)
            filters = arrayOf(InputFilter { src, start, end, _, _, _ ->
                val typed = src.subSequence(start, end).toString()
                val hex = typed.lowercase().filter { it in "0123456789abcdef" }
                if (hex == typed) null else hex
            }, InputFilter.LengthFilter(8))
        }
        val idBox = row().apply {
            setPadding(dp(2), dp(2), dp(2), dp(2))   // keep the "!" inside the border
            addView(label("!", 19f, MUTED, true).apply {
                gravity = Gravity.CENTER
                setPadding(dp(14), 0, dp(14), 0)
                background = GradientDrawable().apply {
                    setColor(BANG)
                    val r = dp(10).toFloat()
                    cornerRadii = floatArrayOf(r, r, 0f, 0f, 0f, 0f, r, r)
                }
                layoutParams = LinearLayout.LayoutParams(WRAP_CONTENT, MATCH_PARENT)
            })
            addView(id, LinearLayout.LayoutParams(0, dp(52), 1f))
        }
        c.addView(idBox)
        val idHint = label("", 14f, MUTED)
        c.addView(idHint)
        c.addView(fieldLabel("Channel number"))
        val channel = EditText(this).apply {
            setText(if (settings.channel > 0) settings.channel.toString() else "")
            hint = "3"
            textSize = 20f
            setTextColor(INK)
            setHintTextColor(HINT)
            isSingleLine = true
            inputType = InputType.TYPE_CLASS_NUMBER
            filters = arrayOf(InputFilter.LengthFilter(1))
            setPadding(dp(14), 0, dp(14), 0)
            background = shape(Color.WHITE, 12, LINE, 2)
            layoutParams = LinearLayout.LayoutParams(dp(120), dp(56))
        }
        c.addView(channel)
        val chHint = label("", 14f, MUTED)
        c.addView(chHint)
        val next = bigButton(if (editing) "Save" else "Next") {
            settings.hubId = "!" + id.text.toString()
            settings.channel = channel.text.toString().toIntOrNull() ?: 0
            if (editing) { restartIfRunning(); back() } else go(Screen.CHECK)
        }
        fun check() {
            val n = id.text.length
            val idOk = n == 8
            idBox.background = shape(Color.WHITE, 12, if (n == 0) BLUE else if (idOk) GREEN else AMBER_TEXT, 2)
            idHint.text = when { n == 0 -> "8 letters and numbers, from the teacher"; idOk -> "Looks right"; else -> "${8 - n} more to go" }
            idHint.setTextColor(if (idOk) GREEN_TEXT else MUTED)
            val ch = channel.text.toString().toIntOrNull()
            val chOk = ch != null && ch in 1..7
            chHint.text = if (ch == 0) "Not 0: channel 0 is public, so anyone nearby could read students' work."
                else "A number from 1 to 7. Never 0: it is public."
            chHint.setTextColor(if (ch == 0) AMBER_TEXT else MUTED)
            look(next, idOk && chOk)
        }
        id.onChange(::check)
        channel.onChange(::check)
        check()
        return page(Color.WHITE, stepHeader(3, "School"), c, bottomBar(next))
    }

    private inner class CheckRow {
        val badgeIcon = icon(R.drawable.ic_check, Color.WHITE, 20)
        val badge = FrameLayout(this@MainActivity).apply {
            addView(badgeIcon, FrameLayout.LayoutParams(dp(20), dp(20), Gravity.CENTER))
            layoutParams = LinearLayout.LayoutParams(dp(36), dp(36))
        }
        val title = label("", 17f, INK, true)
        val detail = label("", 15f, BODY)
        val action = TextView(this@MainActivity).apply {
            textSize = 16f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.WHITE)
            gravity = Gravity.CENTER
            minHeight = dp(48)
            setPadding(dp(16), 0, dp(16), 0)
            background = ripple(AMBER_DARK, 10)
        }
        val view = row(14).apply {
            gravity = Gravity.TOP
            setPadding(dp(14), dp(14), dp(14), dp(14))
            addView(badge)
            addView(fill(column(2).apply {
                addView(title)
                addView(detail)
                addView(action, LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply { topMargin = dp(8) })
            }))
        }

        fun set(state: Int, t: String, d: String, actionText: String? = null, onAction: (() -> Unit)? = null) {
            title.text = t
            detail.text = d
            view.background = if (state == ACTION) shape(AMBER_BG, 14, AMBER_LINE, 2) else shape(SOFT, 14)
            badge.background = shape(when (state) { DONE -> GREEN; ACTION -> AMBER_DARK; else -> TRACK }, 18)
            badgeIcon.setImageResource(when (state) { DONE -> R.drawable.ic_check; ACTION -> R.drawable.ic_warning; WAIT -> R.drawable.ic_retry; else -> R.drawable.ic_info })
            badgeIcon.setColorFilter(if (state == DONE || state == ACTION) Color.WHITE else MUTED)
            action.visibility = if (actionText != null) View.VISIBLE else View.GONE
            action.text = actionText ?: ""
            action.setOnClickListener { onAction?.invoke() }
        }
    }

    private fun checkScreen(): View {
        // Start MeshLearn, or restart it so it uses the settings just chosen.
        if (PhoneService.instance == null) startService() else restartService()
        val c = content()
        c.addView(h1("Checking everything works"))
        val intro = label("", 17f, BODY)
        c.addView(intro)
        val radio = CheckRow()
        val school = CheckRow()
        val wifi = CheckRow()
        val lessons = CheckRow()
        c.addView(column(10).apply { listOf(radio, school, wifi, lessons).forEach { addView(it.view) } })
        val finish = bigButton("Finish") { go(Screen.DONE) }
        val anyway = link("Finish without the Wi-Fi check") { go(Screen.DONE) }
        live = {
            val st = snapshot()
            val books = libraryCount()   // MeshLearn puts the starter library in once it starts
            val none = settings.radioMode == "none"
            when {
                none -> radio.set(SKIP, "No radio yet", "Students' work waits on this phone until a radio is set up in Settings.")
                st.radioOk -> radio.set(DONE, "Talking to the radio", "${radioName()} is connected.")
                !st.running && st.error.isNotEmpty() -> radio.set(WAIT, "Talking to the radio", "MeshLearn could not start: ${st.error}")
                else -> radio.set(WAIT, "Talking to the radio", st.radio.ifEmpty { "Starting MeshLearn…" })
            }
            when {
                none -> school.set(SKIP, "Reaching the school", "Checked once there is a radio.")
                st.lastHeard > 0 -> school.set(DONE, "Reaching the school", "The school answered ${ago(st.lastHeard)}.")
                else -> school.set(WAIT, "Reaching the school",
                    "Waiting for the school to answer. This can take a few minutes; you can finish and it keeps trying.")
            }
            val hotspot = Settings.hotspotAddress()
            if (hotspot != null) {
                wifi.set(DONE, "Student Wi-Fi is on", "Students join this phone's hotspot.")
            } else {
                wifi.set(ACTION, "Turn on Wi-Fi for students",
                    "Students connect to this phone's hotspot. Android needs you to switch it on.", "Open hotspot settings") { openHotspotSettings() }
            }
            if (st.assignments > 0 || books > 0) {
                lessons.set(DONE, "Library and lessons", "${plural(st.assignments, "assignment")} and ${plural(books.toLong(), "library collection")} on this phone.")
            } else {
                lessons.set(SKIP, "Library and lessons", "None yet. Add them later from the status screen.")
            }
            intro.text = when {
                hotspot == null -> "Almost there. One thing needs you."
                !none && !st.radioOk -> "Students can join. The radio check keeps going while you finish."
                else -> "All done. This phone is ready for students."
            }
            if (hotspot != null) { finish.text = "Finish"; look(finish, true, GREEN) } else { finish.text = "Waiting for the hotspot"; look(finish, false) }
            anyway.visibility = if (hotspot == null) View.VISIBLE else View.GONE
        }
        return page(Color.WHITE, stepHeader(4, "Check"), c, bottomBar(finish, anyway))
    }

    /** "Ready for students" at the end of setup, or "How students join" from the status screen. */
    private fun joinScreen(firstTime: Boolean): View {
        val c = content(if (firstTime) 32 else 16)
        if (firstTime) {
            c.addView(iconBox(R.drawable.ic_check, GREEN, GREEN_SOFT, 64, 36, 32))
            c.addView(h1("Ready for students"))
        }
        c.addView(label("Show students this screen. They follow the two steps on their own phone.", 17f, BODY))
        val wifiLine = label("", 16f, INK)
        val address = label("", 22f, INK, true).apply { typeface = Typeface.create(Typeface.MONOSPACE, Typeface.BOLD) }
        c.addView(column(14).apply {
            setPadding(dp(18), dp(18), dp(18), dp(18))
            background = shape(Color.WHITE, 16, LINE, 2)
            addView(column(2).apply { addView(label("1. Join the Wi-Fi", 17f, INK, true)); addView(wifiLine) })
            addView(column(2).apply { addView(label("2. Open", 17f, INK, true)); addView(address) })
        })
        if (firstTime) {
            c.addView(row(14).apply {
                minimumHeight = dp(64)
                setPadding(dp(14), dp(8), dp(14), dp(8))
                background = shape(SOFT, 14)
                addView(fill(titled("Start by itself", "When the phone turns on, MeshLearn starts too")))
                addView(onOff().apply {
                    isChecked = settings.autostart
                    setOnCheckedChangeListener { _, on -> settings.autostart = on }
                    contentDescription = "Start by itself"
                })
            })
        }
        c.addView(label("Keep the phone plugged in or charged. It works with the screen off.", 14f, MUTED))
        live = {
            val hotspot = Settings.hotspotAddress()
            val addr = hotspot ?: Settings.localAddresses().firstOrNull()
            wifiLine.text = if (hotspot != null) "This phone's hotspot (its name is in Android's hotspot settings)"
                else "Turn on this phone's hotspot first"
            address.text = if (addr != null) "$addr:${settings.port}" else "192.168.43.1:${settings.port}"
        }
        val done = bigButton("Done") {
            if (firstTime) { settings.setupDone = true; reset(Screen.STATUS) } else back()
        }
        return page(Color.WHITE, if (firstTime) null else backHeader("How students join"), c, bottomBar(done))
    }

    // -- every day ---------------------------------------------------------------------

    private inner class Tile(name: String) {
        val value = label("", 16f, INK, true)
        val view = column(4).apply {
            setPadding(dp(10), dp(12), dp(10), dp(12))
            addView(label(name, 13f, MUTED))
            addView(value)
        }

        fun set(text: String, color: Int = INK, warn: Boolean = false) {
            value.text = text
            value.setTextColor(color)
            view.background = shape(Color.WHITE, 14, if (warn) AMBER_LINE else LINE, if (warn) 2 else 1)
        }
    }

    private fun statusScreen(): View {
        val header = row(8).apply {
            setPadding(dp(20), dp(14), dp(12), dp(6))
            addView(fill(column().apply {
                addView(label("MeshLearn", 20f, INK, true))
                addView(label(settings.phoneName, 14f, MUTED))
            }))
            addView(iconButton(R.drawable.ic_settings, "Settings") { go(Screen.SETTINGS, false) })
        }
        val c = column(12).apply { setPadding(dp(16), dp(8), dp(16), dp(16)) }

        MeshLearnApp.lastCrash(this)?.let { report -> c.addView(crashCard(report)) }

        val stateIcon = icon(R.drawable.ic_check, GREEN, 30)
        val stateBadge = FrameLayout(this).apply {
            addView(stateIcon, FrameLayout.LayoutParams(dp(30), dp(30), Gravity.CENTER))
            layoutParams = LinearLayout.LayoutParams(dp(52), dp(52))
        }
        val stateTitle = label("", 22f, Color.WHITE, true)
        val stateText = label("", 15f, Color.WHITE)
        val stateCard = row(14).apply {
            setPadding(dp(18), dp(18), dp(18), dp(18))
            addView(stateBadge)
            addView(fill(column(2).apply { addView(stateTitle); addView(stateText) }))
        }
        c.addView(stateCard)

        val radioTile = Tile("Radio")
        val schoolTile = Tile("School")
        val waitingTile = Tile("Waiting")
        c.addView(row(8).apply { listOf(radioTile, schoolTile, waitingTile).forEach { addView(fill(it.view)) } })

        val hotspotTip = row(12).apply {
            setPadding(dp(14), dp(12), dp(14), dp(12))
            background = shape(AMBER_BG, 14, AMBER_LINE, 2)
            addView(fill(titled("The hotspot looks off", "Students can't join until this phone's hotspot is on.", 16f)))
            addView(TextView(context).apply {
                text = "Turn on"
                textSize = 15f
                typeface = Typeface.DEFAULT_BOLD
                setTextColor(Color.WHITE)
                gravity = Gravity.CENTER
                minHeight = dp(44)
                setPadding(dp(14), 0, dp(14), 0)
                background = ripple(AMBER_DARK, 10)
                tap(this) { openHotspotSettings() }
            })
        }
        c.addView(hotspotTip)

        var trying = false
        val retry = bigButton("Try again") {}
        retry.setOnClickListener {
            trying = true
            retry.text = "Looking for the radio…"
            look(retry, false)
            restartService {
                ui.postDelayed({ trying = false; retry.text = "Try again"; look(retry, true) }, 10_000)
            }
        }
        val why = label("", 15f, AMBER_TEXT, true)
        val trouble = column(8).apply {
            addView(why)
            addView(label("Try these", 18f, INK, true).apply { setPadding(0, dp(6), 0, 0) })
            addView(numbered(listOf("Check the radio is switched on and its light blinks.", "Charge it, or plug it in.",
                "Move it within a few steps of this phone.")))
            addView(retry)
            addView(link("Use a different radio") { go(Screen.RADIO, true) })
        }
        c.addView(trouble)

        val joinSub = label("", 14f, JOIN_SUB)
        c.addView(row(14).apply {
            setPadding(dp(16), dp(14), dp(16), dp(14))
            background = ripple(BLUE, 16)
            addView(icon(R.drawable.ic_join, Color.WHITE, 30))
            addView(fill(column(2).apply { addView(label("Show students how to join", 17f, Color.WHITE, true)); addView(joinSub) }))
            tap(this) { go(Screen.JOIN) }
        })
        c.addView(listCard(
            listRow(R.drawable.ic_phone, "See what students see") { openSite("/", "Student site") },
            listRow(R.drawable.ic_book, "Add lessons and books", "From the memory card or the teacher's laptop") { go(Screen.LESSONS) },
            listRow(R.drawable.ic_send, "Send students' work now") {
                if (PhoneService.instance == null) {
                    Toast.makeText(this, "Start MeshLearn first.", Toast.LENGTH_SHORT).show()
                } else {
                    PhoneService.instance?.sendNow()
                    Toast.makeText(this, "Sending everything that is waiting", Toast.LENGTH_SHORT).show()
                }
            },
        ))

        // Each step of connecting to the radio, for whoever helps when it won't connect.
        val logText = label("", 13f, BODY).apply { typeface = Typeface.MONOSPACE; setLineSpacing(0f, 1.15f) }
        val radioDetails = expander("Radio details", "Each step of connecting, to send if it won't connect",
            column(10).apply {
                setPadding(dp(14), 0, dp(14), dp(14))
                addView(logText)
                addView(bigButton("Send radio details") {
                    val st = snapshot()
                    val report = "MeshLearn radio details\nPhone: ${Build.MANUFACTURER} ${Build.MODEL}, Android ${Build.VERSION.RELEASE}\n" +
                        "Radio: ${settings.bleName} (${settings.bleAddress}), channel ${settings.channel}\nNow: ${st.radio}\n\n" +
                        BleTransport.recentEvents().joinToString("\n")
                    val send = Intent(Intent.ACTION_SEND).setType("text/plain")
                        .putExtra(Intent.EXTRA_SUBJECT, "MeshLearn radio details")
                        .putExtra(Intent.EXTRA_TEXT, report)
                    runCatching { startActivity(Intent.createChooser(send, "Send the radio details")) }
                })
            })
        c.addView(radioDetails)

        val power = TextView(this).apply {
            textSize = 16f
            gravity = Gravity.CENTER
            minHeight = dp(52)
        }
        val bottom = column().apply { setPadding(dp(16), dp(8), dp(16), dp(20)); addView(power) }

        live = {
            val st = snapshot()
            val none = settings.radioMode == "none"
            val hotspot = Settings.hotspotAddress()
            fun state(bg: Int, iconRes: Int, fg: Int, title: String, text: String, dark: Boolean) {
                stateCard.background = if (bg == Color.WHITE) shape(bg, 18, LINE, 1) else if (bg == AMBER_BG) shape(bg, 18, AMBER_LINE, 2) else shape(bg, 18)
                stateBadge.background = shape(if (dark) Color.WHITE else fg, 26)
                stateIcon.setImageResource(iconRes)
                stateIcon.setColorFilter(if (dark) bg else Color.WHITE)
                stateTitle.text = title
                stateTitle.setTextColor(if (dark) Color.WHITE else INK)
                stateText.text = text
                stateText.setTextColor(if (dark) ON_GREEN else BODY)
            }
            val radioDown = st.running && !none && !st.radioOk
            when {
                !st.running && st.error.isNotEmpty() -> state(AMBER_BG, R.drawable.ic_warning, AMBER_DARK,
                    "MeshLearn could not start", "${st.error}\nCheck Settings, then press Start.", false)
                !st.running -> state(Color.WHITE, R.drawable.ic_pause, MUTED, "MeshLearn is paused",
                    "Students can't open it until you start it. Saved work stays on this phone.", false)
                none -> state(BLUE_SOFT, R.drawable.ic_info, BLUE, "Working without a radio",
                    "Students can join. Their work is saved here until a radio is set up.", false)
                st.radioOk -> state(GREEN, R.drawable.ic_check, GREEN, "Everything is working",
                    if (st.lastHeard > 0) "Students can join and their work reaches school." else "Students can join. Waiting to hear from the school.", true)
                else -> state(AMBER_BG, R.drawable.ic_warning, AMBER_DARK, "The radio isn't connected",
                    "Students can keep working. Their work is saved on this phone and goes to school once the radio is back.", false)
            }
            when {
                none -> radioTile.set("Not set up", MUTED)
                !st.running -> radioTile.set("Paused", MUTED)
                st.radioOk -> radioTile.set("Connected", GREEN_TEXT)
                st.radio.startsWith("Connecting") || st.radio.startsWith("Reading") -> radioTile.set("Connecting", AMBER_TEXT, warn = true)
                st.radio.startsWith("Bluetooth is off") -> radioTile.set("Bluetooth off", AMBER_TEXT, warn = true)
                else -> radioTile.set("Not found", AMBER_TEXT, warn = true)
            }
            why.text = "Right now: ${st.radio}"
            why.visibility = if (st.radio.isNotEmpty()) View.VISIBLE else View.GONE
            radioDetails.visibility = if (settings.radioMode == "ble") View.VISIBLE else View.GONE
            logText.text = BleTransport.recentEvents().takeLast(30).joinToString("\n").ifEmpty { "Nothing yet. Start MeshLearn to connect." }
            schoolTile.set(if (st.lastHeard > 0) ago(st.lastHeard).replaceFirstChar { it.uppercase() } else "Not yet")
            val waiting = st.pending + st.unbatched
            waitingTile.set(if (waiting == 0L) "Nothing" else plural(waiting, "message"))
            trouble.visibility = if (radioDown || trying) View.VISIBLE else View.GONE
            hotspotTip.visibility = if (st.running && hotspot == null) View.VISIBLE else View.GONE
            val addr = hotspot ?: Settings.localAddresses().firstOrNull()
            joinSub.text = if (addr != null) "$addr:${settings.port}" else "Turn on the hotspot first"
            if (st.running) {
                power.text = "Pause MeshLearn"
                power.setTextColor(PAUSE_TEXT)
                power.typeface = Typeface.DEFAULT
                power.background = ripple(Color.WHITE, 12, LINE, 1)
                power.setOnClickListener { askToPause() }
            } else {
                power.text = "Start MeshLearn"
                power.setTextColor(Color.WHITE)
                power.typeface = Typeface.DEFAULT_BOLD
                power.background = ripple(GREEN, 14)
                power.setOnClickListener { startService(); ui.postDelayed({ live?.invoke() }, 600) }
            }
        }
        return page(SOFT, header, c, bottom)
    }

    /** "MeshLearn stopped unexpectedly", with a way to send the details on. */
    private fun crashCard(report: String): View {
        fun small(text: String, fill: Int, fg: Int, onClick: () -> Unit) = TextView(this).apply {
            this.text = text
            textSize = 15f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(fg)
            gravity = Gravity.CENTER
            minHeight = dp(44)
            setPadding(dp(14), 0, dp(14), 0)
            background = ripple(fill, 10, if (fill == Color.WHITE) LINE else 0, if (fill == Color.WHITE) 1 else 0)
            tap(this, onClick)
        }
        return column(10).apply {
            setPadding(dp(16), dp(14), dp(16), dp(14))
            background = shape(Color.WHITE, 16, AMBER_LINE, 2)
            addView(titled("MeshLearn stopped unexpectedly", "Sending the details helps get it fixed. They contain no student work.", 17f))
            addView(row(8).apply {
                addView(small("Send details", AMBER_DARK, Color.WHITE) {
                    val send = Intent(Intent.ACTION_SEND).setType("text/plain")
                        .putExtra(Intent.EXTRA_SUBJECT, "MeshLearn crash report")
                        .putExtra(Intent.EXTRA_TEXT, report)
                    runCatching { startActivity(Intent.createChooser(send, "Send the crash details")) }
                })
                addView(small("Dismiss", Color.WHITE, INK) {
                    MeshLearnApp.clearCrash(this@MainActivity)
                    render()
                })
            })
        }
    }

    private fun askToPause() {
        AlertDialog.Builder(this)
            .setTitle("Pause MeshLearn?")
            .setMessage("Students can't open the site while it is paused. Their saved work stays on this phone.")
            .setPositiveButton("Pause") { _, _ ->
                PhoneService.stop(this)
                ui.postDelayed({ live?.invoke() }, 500)
            }
            .setNegativeButton("Keep running", null)
            .show()
    }

    private fun lessonsScreen(): View {
        val c = column(14).apply { setPadding(dp(16), dp(12), dp(16), dp(16)) }
        c.addView(label("ON THIS PHONE NOW", 13f, MUTED, true).apply { letterSpacing = 0.08f })
        val lessonsLine = label("", 17f, INK, true)
        val libraryLine = label("Counting the library…", 15f, BODY)
        c.addView(row(14).apply {
            gravity = Gravity.TOP
            setPadding(dp(16), dp(16), dp(16), dp(16))
            background = shape(Color.WHITE, 16, LINE, 1)
            addView(iconBox(R.drawable.ic_book, GREEN, GREEN_SOFT, 40, 22))
            addView(fill(column(4).apply { addView(lessonsLine); addView(libraryLine) }))
        })
        c.addView(label("Small changes come by radio on their own. Use one of these for a new term, a new course, or new books.", 16f, BODY))
        val place = Settings.currentPlace(this)
        fun option(res: Int, fg: Int, bg: Int, title: String, sub: String, onClick: () -> Unit) = row(14).apply {
            minimumHeight = dp(84)
            setPadding(dp(16), dp(14), dp(12), dp(14))
            background = ripple(Color.WHITE, 16, LINE, 1)
            addView(iconBox(res, fg, bg))
            addView(fill(titled(title, sub)))
            addView(icon(R.drawable.ic_chevron, MUTED, 20))
            tap(this, onClick)
        }
        c.addView(option(R.drawable.ic_laptop, BLUE, BLUE_SOFT, "From the teacher's laptop",
            "At school, on the same Wi-Fi as the laptop. Lessons only, about 10 seconds.") {
            openSite("/status", "Copy from the teacher's laptop")
        })
        val onCard = place.kind == Settings.CARD
        c.addView(option(R.drawable.ic_card, AMBER_DARK, AMBER_SOFT,
            if (onCard) "From a memory card" else "From files copied by cable",
            if (onCard) "The teacher filled it on a laptop. Lessons and books." else "Copied onto this phone from a computer over USB. Lessons and books.") {
            loadFromFiles(place)
        })
        c.addView(row(12).apply {
            gravity = Gravity.TOP
            setPadding(dp(4), dp(4), dp(4), 0)
            addView(icon(R.drawable.ic_info, MUTED, 20))
            addView(fill(label("Students' work stays on the phone. Copying lessons never removes answers that haven't reached school yet.", 14f, MUTED)))
        })
        live = {
            val st = snapshot()
            lessonsLine.text = if (st.running) "${plural(st.assignments, "assignment")} on this phone" else "Start MeshLearn to see the lessons"
        }
        Thread {
            val lib = File(place.folder, StorageMove.LIBRARY)
            val n = libraryCount()
            val bytes = StorageMove.sizeOf(lib)
            runOnUiThread {
                libraryLine.text = "Library: ${plural(n.toLong(), "collection")} · ${size(bytes)}\n" +
                    "${size(place.free)} free on ${placeName(place.kind)}"
            }
        }.start()
        return page(SOFT, backHeader("Add lessons and books", SOFT), c, null)
    }

    private fun loadFromFiles(place: Settings.Place) {
        val service = PhoneService.instance
        if (service == null) {
            Toast.makeText(this, "Start MeshLearn first.", Toast.LENGTH_SHORT).show()
            return
        }
        Thread {
            val n = runCatching { service.reloadPacks() }
            runOnUiThread {
                val folder = File(place.folder, "assignments").path
                val count = n.getOrDefault(0)
                AlertDialog.Builder(this)
                    .setTitle(if (count > 0) "Lessons loaded" else "No lessons found")
                    .setMessage(if (count > 0) "${plural(count.toLong(), "assignment")} read from the files. Students see them the next time they open MeshLearn."
                        else "MeshLearn looked in:\n$folder\n\nPut a course pack there (the teacher hub's \"Fill a memory card\" makes one), then try again." +
                            (n.exceptionOrNull()?.message?.let { "\n\n$it" } ?: ""))
                    .setPositiveButton("OK", null)
                    .show()
                live?.invoke()
            }
        }.start()
    }

    // -- settings -----------------------------------------------------------------------

    private fun settingsScreen(): View {
        val c = column(10).apply { setPadding(dp(16), dp(16), dp(16), dp(16)) }
        fun setting(name: String, value: String, onClick: () -> Unit) = row(12).apply {
            minimumHeight = dp(64)
            setPadding(dp(16), dp(8), dp(12), dp(8))
            background = RippleDrawable(ColorStateList.valueOf(0x22000000), null, shape(Color.BLACK, 0))
            addView(fill(column(2).apply { addView(label(name, 14f, MUTED)); addView(label(value, 17f, INK)) }))
            addView(icon(R.drawable.ic_chevron, MUTED, 20))
            tap(this, onClick)
        }
        val auto = row(12).apply {
            minimumHeight = dp(64)
            setPadding(dp(16), dp(8), dp(12), dp(8))
            addView(fill(titled("Start by itself", "When the phone turns on", 17f, bold = false)))
            addView(onOff().apply {
                isChecked = settings.autostart
                setOnCheckedChangeListener { _, on -> settings.autostart = on }
                contentDescription = "Start by itself"
            })
        }
        c.addView(listCard(
            setting("Phone name", settings.phoneName) { go(Screen.NAME, true) },
            setting("Radio", radioSummary()) { go(Screen.RADIO, true) },
            setting("School", if (settings.hubId.isBlank()) "Not set yet" else "Radio ${settings.hubId} · channel ${settings.channel}") { go(Screen.SCHOOL, true) },
            auto,
        ))

        // Where the files are kept
        storageGroup = RadioGroup(this).apply {
            for (choice in listOf(STORE_CARD, STORE_PHONE)) addView(RadioButton(context).apply {
                id = choice
                textSize = 16f
                setTextColor(INK)
                minHeight = dp(48)
                buttonTintList = ColorStateList.valueOf(BLUE)
            })
            setOnCheckedChangeListener { _, id ->
                if (settingStorage) return@setOnCheckedChangeListener
                val kind = if (id == STORE_CARD) Settings.CARD else Settings.PHONE
                Settings.places(this@MainActivity).firstOrNull { it.kind == kind }?.let { askToMove(it) }
            }
        }
        storageNote = label("", 14f, AMBER_TEXT)
        c.addView(column(4).apply {
            setPadding(dp(16), dp(14), dp(16), dp(10))
            background = shape(Color.WHITE, 16, LINE, 1)
            addView(label("Where MeshLearn keeps its files", 17f, INK, true))
            addView(label("Students' saved work, lessons and books. Changing this moves them now.", 14f, MUTED))
            addView(storageGroup)
            addView(storageNote)
        })

        // For technical helpers
        fields.clear()
        fun field(key: String, text: String, value: String, hint: String = "", number: Boolean = false) = column(4).apply {
            addView(label(text, 15f, INK))
            addView(EditText(context).apply {
                setText(value)
                this.hint = hint
                textSize = 16f
                setTextColor(INK)
                setHintTextColor(HINT)
                isSingleLine = true
                inputType = if (number) InputType.TYPE_CLASS_NUMBER else InputType.TYPE_CLASS_TEXT
                setPadding(dp(12), 0, dp(12), 0)
                background = shape(Color.WHITE, 10, FIELD_LINE, 1)
                fields[key] = this
            }, LinearLayout.LayoutParams(MATCH_PARENT, dp(48)))
        }
        val tech = column(12).apply {
            setPadding(dp(16), dp(4), dp(16), dp(16))
            addView(field("radio_host", "Radio's Wi-Fi address (only for a Wi-Fi radio)", settings.radioHost, "192.168.43.20"))
            addView(field("hops", "Most relays a message may take", if (settings.hopLimit >= 0) settings.hopLimit.toString() else "", "The radio's own setting", true))
            addView(field("batch", "Seconds to collect work before sending", settings.batchSeconds.toString(), "120", true))
            addView(field("pin", "Teacher PIN for adding lessons from a laptop", settings.adminPin))
            addView(field("kiwix", "Encyclopedia (Kiwix) address", settings.kiwixUrl, "http://192.168.43.1:8090"))
            addView(field("port", "Web port", settings.port.toString(), "8080", true))
            addView(bigButton("Save these") { saveTechnical() })
        }
        c.addView(expander("For technical helpers", "Leave these alone unless the teacher asks", tech))
        folderNote = label("", 14f, MUTED).apply { setPadding(dp(4), dp(4), dp(4), 0) }
        c.addView(folderNote)
        c.addView(link("Run setup again") { reset(Screen.WELCOME) })
        showStorage()
        return page(SOFT, backHeader("Settings"), c, null)
    }

    private fun radioSummary() = when (settings.radioMode) {
        "wifi" -> "${settings.radioHost.ifEmpty { "No address yet" }} · Wi-Fi"
        "none" -> "No radio yet: work waits on this phone"
        else -> if (settings.bleAddress.isEmpty()) "Not chosen yet" else "${settings.bleName.ifEmpty { settings.bleAddress }} · Bluetooth"
    }

    private fun radioName() = when (settings.radioMode) {
        "wifi" -> "The radio at ${settings.radioHost}"
        else -> settings.bleName.ifEmpty { "The radio" }
    }

    private fun text(key: String) = fields[key]!!.text.toString().trim()

    private fun saveTechnical() {
        settings.radioHost = text("radio_host")
        settings.hopLimit = text("hops").toIntOrNull()?.coerceIn(0, 7) ?: -1
        settings.batchSeconds = (text("batch").toIntOrNull() ?: 120).coerceIn(5, 3600)
        settings.adminPin = text("pin")
        settings.kiwixUrl = text("kiwix")
        settings.port = (text("port").toIntOrNull() ?: 8080).coerceIn(1024, 65535)
        restartIfRunning()
        Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
        render()
    }

    private fun restartIfRunning() {
        if (PhoneService.instance != null) restartService()
    }

    /**
     * Stop MeshLearn, wait until it has really let go of the radio, then start it again.
     * Starting too soon makes the radio refuse the new Bluetooth connection.
     */
    private fun restartService(then: () -> Unit = {}) {
        PhoneService.stop(this)
        val until = System.currentTimeMillis() + 10_000
        val waitForStop = object : Runnable {
            override fun run() {
                if (PhoneService.instance != null && System.currentTimeMillis() < until) {
                    ui.postDelayed(this, 200)
                } else {
                    ui.postDelayed({ startService(); then() }, 1500)
                }
            }
        }
        ui.postDelayed(waitForStop, 200)
    }

    // -- storage ------------------------------------------------------------------

    private fun placeName(kind: String) = if (kind == Settings.CARD) "the memory card" else "this phone"

    /** The two choices with their free space, the one in use checked, and where the folder is. */
    private fun showStorage() {
        if (!::storageGroup.isInitialized) return
        val places = Settings.places(this)
        val current = Settings.currentPlace(this)
        val card = places.firstOrNull { it.kind == Settings.CARD }
        val phone = places.first { it.kind == Settings.PHONE }
        settingStorage = true
        storageGroup.findViewById<RadioButton>(STORE_CARD).apply {
            visibility = if (card != null) View.VISIBLE else View.GONE
            if (card != null) text = "Memory card · ${size(card.free)} free of ${size(card.total)}"
        }
        storageGroup.findViewById<RadioButton>(STORE_PHONE).text = "This phone · ${size(phone.free)} free of ${size(phone.total)}"
        storageGroup.check(if (current.kind == Settings.CARD) STORE_CARD else STORE_PHONE)
        settingStorage = false
        storageNote.text = when {
            settings.storage == Settings.CARD && card == null ->
                "The memory card is not in. MeshLearn is using this phone's storage until it is back; " +
                    "work saved on the card stays there."
            card == null -> "No memory card in, so everything is on this phone."
            else -> ""
        }
        storageNote.setTextColor(if (settings.storage == Settings.CARD && card == null) AMBER_TEXT else MUTED)
        storageNote.visibility = if (storageNote.text.isEmpty()) View.GONE else View.VISIBLE
        val fill = if (current.kind == Settings.CARD) "with a card reader" else "over a USB cable from a computer"
        folderNote.text = "MeshLearn's folder, on ${placeName(current.kind)}:\n${current.folder}\n" +
            "Put collections in its 'library' folder and course packs in 'assignments' ($fill), or copy lessons from the teacher's laptop."
    }

    private fun size(bytes: Long) = org.meshlearn.core.Html.size(bytes.toDouble())

    private fun askToMove(to: Settings.Place) {
        val from = Settings.currentPlace(this)
        if (to.kind == from.kind) return
        val work = StorageMove.bytesToMove(from.folder, books = false)
        val books = StorageMove.sizeOf(File(from.folder, StorageMove.LIBRARY))
        if (work > to.free) {
            AlertDialog.Builder(this).setTitle("Not enough room")
                .setMessage("Students' work and lessons need ${size(work)}; ${placeName(to.kind)} has ${size(to.free)} free. Nothing was moved.")
                .setPositiveButton("OK", null).show()
            showStorage()
            return
        }
        val fits = work + books <= to.free
        val withBooks = CheckBox(this).apply {
            text = if (fits) "Also move the books (${size(books)})" else "Also move the books (${size(books)}: not enough room)"
            isChecked = fits && books > 0
            isEnabled = fits && books > 0
            if (books == 0L) visibility = View.GONE
        }
        val box = LinearLayout(this).apply { setPadding(dp(20), dp(8), dp(20), 0); addView(withBooks) }
        AlertDialog.Builder(this)
            .setTitle("Keep MeshLearn's files on ${placeName(to.kind)}?")
            .setMessage("Students' saved work, waiting messages and lessons (${size(work)}) move there now. " +
                "MeshLearn stops for a moment while they move." +
                (if (books > 0) "\n\nBooks left behind are not shown to students until they are copied there." else ""))
            .setView(box)
            .setPositiveButton("Move") { _, _ -> moveFiles(from, to, withBooks.isChecked) }
            .setNegativeButton("Cancel") { _, _ -> showStorage() }
            .setOnCancelListener { showStorage() }
            .show()
    }

    private fun moveFiles(from: Settings.Place, to: Settings.Place, books: Boolean) {
        val wasRunning = PhoneService.instance != null
        val progress = TextView(this).apply { setPadding(dp(20), dp(12), dp(20), dp(12)); text = "Stopping MeshLearn…" }
        val dialog = AlertDialog.Builder(this).setTitle("Moving to ${placeName(to.kind)}").setView(progress).setCancelable(false).show()
        Thread {
            if (wasRunning) {
                runOnUiThread { PhoneService.stop(this) }
                val until = System.currentTimeMillis() + 15_000
                while (PhoneService.instance != null && System.currentTimeMillis() < until) Thread.sleep(100)
            }
            val result = runCatching {
                if (PhoneService.instance != null) throw IllegalStateException("MeshLearn did not stop")
                StorageMove.move(from.folder, to.folder, books, to.free) { name -> runOnUiThread { progress.text = "Copying $name…" } }
            }
            runOnUiThread {
                dialog.dismiss()
                result.onSuccess {
                    settings.storage = to.kind
                    Toast.makeText(this, "Moved. MeshLearn now keeps its files on ${placeName(to.kind)}.", Toast.LENGTH_LONG).show()
                }.onFailure { e ->
                    val why = if (e is StorageMove.NotEnoughRoom) "It needs ${size(e.needed)} and there is ${size(e.free)} free."
                        else (e.message ?: e.javaClass.simpleName)
                    AlertDialog.Builder(this).setTitle("Nothing was moved")
                        .setMessage("$why\n\nMeshLearn still keeps everything on ${placeName(from.kind)}.")
                        .setPositiveButton("OK", null).show()
                }
                showStorage()
                if (wasRunning) PhoneService.start(this)
            }
        }.start()
    }

    // -- helpers ------------------------------------------------------------------

    private fun snapshot() = PhoneService.instance?.snapshot() ?: PhoneService.state

    private fun libraryCount(): Int =
        File(Settings.currentPlace(this).folder, StorageMove.LIBRARY).listFiles()?.count { !it.name.startsWith(".") } ?: 0

    private fun plural(n: Long, word: String) = if (n == 1L) "1 $word" else "$n ${word}s"

    /** "just now", "3 min ago", "2 h ago", "4 days ago". */
    private fun ago(epochSeconds: Double): String {
        val s = (System.currentTimeMillis() / 1000.0 - epochSeconds).toLong().coerceAtLeast(0)
        return when {
            s < 60 -> "just now"
            s < 3600 -> "${s / 60} min ago"
            s < 86400 -> "${s / 3600} h ago"
            else -> plural(s / 86400, "day") + " ago"
        }
    }

    private fun startService() {
        if (!ensurePermissions()) {
            startAfterPermission = true
            return
        }
        PhoneService.start(this)
    }

    private fun openSite(path: String, title: String) {
        if (PhoneService.instance == null) {
            Toast.makeText(this, "Start MeshLearn first.", Toast.LENGTH_SHORT).show()
            return
        }
        startActivity(Intent(this, StudentSiteActivity::class.java)
            .putExtra(StudentSiteActivity.EXTRA_URL, "http://127.0.0.1:${settings.port}$path")
            .putExtra(StudentSiteActivity.EXTRA_TITLE, title))
    }

    /** Android has no public screen for just the hotspot on every version: try it, then the network settings. */
    private fun openHotspotSettings() {
        val tries = listOf(
            Intent().setClassName("com.android.settings", "com.android.settings.TetherSettings"),
            Intent(android.provider.Settings.ACTION_WIRELESS_SETTINGS),
            Intent(android.provider.Settings.ACTION_SETTINGS),
        )
        for (i in tries) {
            try {
                startActivity(i)
                return
            } catch (_: Exception) {
            }
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
        val missing = neededPermissions()
        if (missing.isEmpty()) return true
        requestPermissions(missing.toTypedArray(), 1)
        return false
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (startAfterPermission) {
            startAfterPermission = false
            PhoneService.start(this)   // without Bluetooth it still serves students and saves their work
        }
        if (scanAfterPermission) {
            scanAfterPermission = false
            if (stack.last() == Screen.RADIO && neededPermissions().isEmpty()) render()
        }
        if (grantResults.any { it != PackageManager.PERMISSION_GRANTED }) {
            Toast.makeText(this, "Bluetooth permission is needed to talk to the radio.", Toast.LENGTH_LONG).show()
        }
    }

    private fun signal(rssi: Int) = when {
        rssi >= -60 -> "Very close · strong signal"
        rssi >= -80 -> "Nearby · good signal"
        else -> "Further away · weak signal"
    }

    /** Lists Meshtastic radios as they show up, for 30 seconds or until the screen changes. */
    @SuppressLint("MissingPermission")
    private fun scanRadios(found: (String, String, String) -> Unit, problem: (String) -> Unit) {
        val missing = neededPermissions().filter { it != "android.permission.POST_NOTIFICATIONS" }
        if (missing.isNotEmpty()) {
            problem("MeshLearn needs permission to look for the radio.")
            scanAfterPermission = true
            requestPermissions(missing.toTypedArray(), 2)
            return
        }
        val adapter = (getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager).adapter
        if (adapter == null) {
            problem("This phone has no Bluetooth. Use a Wi-Fi radio, or skip for now.")
            return
        }
        if (!adapter.isEnabled) {
            problem("Bluetooth is off. Turn it on in the quick settings, then come back here.")
            rescanOnResume = true
            return
        }
        // Radios already paired come first: they don't always advertise while connected.
        adapter.bondedDevices.filter { (it.name ?: "").startsWith("Meshtastic") }.forEach {
            found(it.address, it.name ?: it.address, "Paired with this phone")
        }
        val scanner = adapter.bluetoothLeScanner ?: run { problem("Bluetooth is not ready. Try again in a moment."); return }
        val callback = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                val name = result.scanRecord?.deviceName ?: result.device.name ?: result.device.address
                runOnUiThread { found(result.device.address, name, signal(result.rssi)) }
            }
        }
        val filter = ScanFilter.Builder().setServiceUuid(ParcelUuid(BleTransport.SERVICE)).build()
        runCatching {
            scanner.startScan(listOf(filter), ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(), callback)
        }.onFailure { problem("Could not look for radios: ${it.message}"); return }
        val stop = Runnable { runCatching { scanner.stopScan(callback) } }
        val timeUp = Runnable {
            stop.run()
            problem("Stopped looking. Not there? Read the tips below, then look again.")
        }
        ui.postDelayed(timeUp, 30_000)
        leave = {
            ui.removeCallbacks(timeUp)
            stop.run()
        }
    }

    companion object {
        private const val STORE_CARD = 201
        private const val STORE_PHONE = 202

        // Check states
        private const val DONE = 0
        private const val WAIT = 1
        private const val ACTION = 2
        private const val SKIP = 3

        // Colours from the design canvas
        private const val INK = 0xFF1D1D1B.toInt()
        private const val BODY = 0xFF4A4A46.toInt()
        private const val MUTED = 0xFF5B5B57.toInt()
        private const val HINT = 0xFF8A8A84.toInt()
        private const val DOTS = 0xFF8A8A84.toInt()
        private const val BLUE = 0xFF1F5FBF.toInt()
        private const val BLUE_SOFT = 0xFFEEF3FB.toInt()
        private const val TIP_TEXT = 0xFF173F80.toInt()
        private const val JOIN_SUB = 0xFFDBE7FA.toInt()
        private const val GREEN = 0xFF1D7A3A.toInt()
        private const val GREEN_SOFT = 0xFFE9F3EC.toInt()
        private const val GREEN_TRACK = 0xFF9BC8A8.toInt()
        private const val GREEN_TEXT = 0xFF1D5C31.toInt()
        private const val ON_GREEN = 0xFFE3F2E7.toInt()
        private const val AMBER_BG = 0xFFFDF3E2.toInt()
        private const val AMBER_SOFT = 0xFFFDF1DC.toInt()
        private const val AMBER_LINE = 0xFFE2B866.toInt()
        private const val AMBER_DARK = 0xFF8A5A00.toInt()
        private const val AMBER_TEXT = 0xFF6B4600.toInt()
        private const val SOFT = 0xFFF7F7F4.toInt()
        private const val LINE = 0xFFD9D9D3.toInt()
        private const val FIELD_LINE = 0xFFC9C9C2.toInt()
        private const val DIVIDER = 0xFFECECE6.toInt()
        private const val TRACK = 0xFFE2E2DC.toInt()
        private const val OFF_TEXT = 0xFF57574F.toInt()
        private const val BANG = 0xFFF1F1EC.toInt()
        private const val PAUSE_TEXT = 0xFF3F3F3C.toInt()
    }
}
