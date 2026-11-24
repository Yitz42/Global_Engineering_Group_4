package org.meshlearn.core

import org.meshlearn.core.Html.esc
import org.meshlearn.core.Html.icon
import org.meshlearn.core.Html.g
import org.meshlearn.core.Html.quote
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.util.logging.Level
import java.util.logging.Logger

/**
 * The student phone: hosts assignments and the offline library over Wi-Fi, keeps every
 * assignment it is given, and batches students' work onto the radio. A port of phone.py;
 * the pages, messages and database match the Python phone. Every word students see comes
 * from the school's language ([Strings]), which arrives with the course pack.
 */
class Phone(
    val dataDir: File,
    var hubId: String,
    var name: String,
    makeDb: (File, String) -> Db,
    messengerFactory: (MessageHandler) -> Messenger,
    kiwixUrl: String = "",
    var batchS: Double = 120.0,
    var adminPin: String = "",
) {
    private val log = Logger.getLogger("Phone")
    val libraryDir = File(dataDir, "library")
    val packDir = File(dataDir, "assignments")
    var kiwixUrl = kiwixUrl.trimEnd('/')
    val db: Db
    val messenger: Messenger
    val app = App()
    private val flushLock = Object()
    @Volatile private var flushNow = false
    @Volatile private var running = false
    @Volatile var school: Strings = Strings.ENGLISH
        private set
    @Volatile var languages: Map<String, Strings> = mapOf(school.code to school)
        private set
    private var looks: List<String> = listOf("mid")   // declared before init, which loads the school's
    private val signIn: SignIn

    /** The school's own pictures, and the language each student switched to. (Before init, which fills them.) */
    private var own: Map<String, Map<String, Any?>> = emptyMap()
    private var voices: Map<String, File> = emptyMap()
    private val voiceDir = File(dataDir, "voice")
    private val langOf = java.util.concurrent.ConcurrentHashMap<String, String>()

    init {
        dataDir.mkdirs()
        installStarterLibrary()
        db = makeDb(File(dataDir, "phone.db"), SCHEMA)
        for (column in listOf("duration REAL", "right_answers TEXT", "signals TEXT", "notes TEXT")) {   // added after the first release
            try { db.execute("ALTER TABLE submissions ADD COLUMN $column") } catch (e: Exception) { /* already there */ }
        }
        signIn = SignIn(this, db, ::page)
        loadVoices()
        loadPictures()
        loadLanguage()
        messenger = messengerFactory(::onMessage)
        loadPackFolder()
        routes()
    }

    // -- lifecycle ------------------------------------------------------------

    fun start() {
        messenger.start()
        running = true
        Thread(::batchLoop, "batcher").apply { isDaemon = true; start() }
    }

    fun stop() {
        running = false
        flush()
        messenger.stop()
    }

    /** Batch students' saved work into a radio message now instead of waiting. */
    fun flush() = synchronized(flushLock) { flushNow = true; flushLock.notifyAll() }

    private fun installStarterLibrary() {
        val target = File(libraryDir, "starter")
        if (target.isDirectory) return
        target.mkdirs()
        for (fname in STARTER_FILES) {
            val res = Phone::class.java.getResourceAsStream("/starter_library/$fname") ?: continue
            res.use { input -> File(target, fname).outputStream().use { input.copyTo(it) } }
        }
    }

    val nodeId: String get() = messenger.transport.nodeId

    fun inventory(): Map<String, String> =
        db.all("SELECT id, version FROM assignments").associate { it.str("id")!! to it.str("version")!! }

    fun sayHello() {
        // Only the newest hello matters: a phone restarted often must not queue a pile of them.
        messenger.dropPending(hubId, Protocol.HELLO)
        messenger.send(hubId, Protocol.HELLO, linkedMapOf(
            "n" to name, "f" to dataDir.usableSpace / (1024 * 1024), "l" to libraryItems().size.toLong(),
            "sw" to VERSION, "inv" to inventory(),
        ))
    }

    // -- the school's own pictures --------------------------------------------

    private fun loadPictures() {
        val data = db.one("SELECT value FROM settings WHERE key = 'pictures'")?.str("value")?.let {
            try { Json.parse(it).asMap() } catch (e: Exception) { null }
        } ?: emptyMap()
        own = data.mapNotNull { (k, v) -> v.asMap()?.let { k to it } }.toMap()
    }

    /** Keep the school's own pictures, from a lesson, a Wi-Fi copy or a card. A whole one is never replaced by its
     *  name alone (a photo whose lesson came by radio). */
    fun importPictures(data: Any?): Int {
        var added = 0
        for ((key, raw) in (data.asMap() ?: emptyMap()).entries.take(500)) {
            val pic = (if (OwnPics.KEY.matches(key)) OwnPics.clean(raw) else null) ?: continue
            if (pic != own[key] && (OwnPics.whole(pic) || !OwnPics.whole(own[key]))) {
                own = own + (key to pic)
                added++
            }
        }
        if (added > 0) db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('pictures', ?)", Json.dumps(own))
        return added
    }

    // -- the teacher's recordings ---------------------------------------------

    private fun loadVoices() {
        voices = Voices.folderFiles(File(packDir, "voice")) + Voices.folderFiles(voiceDir)
    }

    /** Where the teacher's recording of these words is, when this phone has one (listen.js plays it). */
    private fun voice(text: String, w: Strings): String {
        val key = if (voices.isEmpty()) null else Voices.find(voices, text, listOf(w.code) + (languages.keys - w.code))
        return if (key == null) "" else " data-voice='/voice/$key'"
    }

    /** Fetch the recordings this phone lacks over Wi-Fi, and drop those no assignment uses any more. */
    private fun syncVoices(hubUrl: String, wanted: Map<String, Any?>): Int {
        val have = Voices.folderFiles(voiceDir)
        var count = 0
        for ((key, about) in wanted.entries.take(2000)) {
            val list = about.asList() ?: continue
            val ext = list.getOrNull(0) as? String ?: continue
            val size = (list.getOrNull(1) as? Number)?.toInt() ?: continue
            if (!Voices.KEY.matches(key) || ext !in Voices.TYPES || size !in 1..Voices.MAX_BYTES) continue
            if (have[key]?.let { it.extension == ext && it.length() == size.toLong() } == true) continue
            try {
                val conn = URL(hubUrl.trimEnd('/') + "/voice/" + key).openConnection() as HttpURLConnection
                conn.connectTimeout = 20_000
                conn.readTimeout = 20_000
                val data = try {
                    if (conn.responseCode != 200) continue
                    conn.inputStream.use { it.readBytes() }
                } finally {
                    conn.disconnect()
                }
                if (data.size == size && Voices.looksLike(data, ext)) {
                    Voices.save(voiceDir, key, ext, data)
                    count++
                }
            } catch (e: java.io.IOException) {
                log.warning("could not copy recording $key")
            }
        }
        for (key in have.keys - wanted.keys) Voices.remove(voiceDir, key)
        loadVoices()
        return count
    }

    // -- language -------------------------------------------------------------

    private fun loadLanguage() {
        val data = db.one("SELECT value FROM settings WHERE key = 'language'")?.str("value")?.let {
            try { Json.parse(it).asMap() } catch (e: Exception) { null }
        } ?: emptyMap()
        for (r in db.all("SELECT key, value FROM settings WHERE key LIKE 'lang:%'")) langOf[r.str("key")!!.substring(5)] = r.str("value") ?: ""
        val first = Strings(data["school"].asMap())
        val all = linkedMapOf(first.code to first)
        signIn.load(data)
        val have = signIn.roster.map { it.look }.toSet()   // the class list says which ages there are
        looks = if (have.isEmpty()) looksFor(data["ages"]) else SignIn.LOOK_ORDER.filter { it in have }
        for (item in listOf(data["second"]) + (data["more"].asList() ?: emptyList())) {
            val pack = item.asMap() ?: continue
            val code = pack["code"] as? String
            if (!code.isNullOrEmpty() && code !in all) all[code] = Strings(pack)
        }
        school = first
        languages = all
    }

    /** Keep the school's words from a course pack. Returns true if they changed. */
    fun importLanguage(data: Any?): Boolean {
        val m = data.asMap() ?: return false
        val first = m["school"].asMap() ?: return false
        val value = Json.dumps(mapOf("school" to first, "second" to m["second"].asMap(),
            "more" to (m["more"].asList() ?: emptyList()).mapNotNull { it.asMap() },
            "ages" to (m["ages"].asList() ?: emptyList()).filterIsInstance<String>(),
            "students" to (m["students"].asList() ?: emptyList()), "signin" to (m["signin"].asMap() ?: emptyMap()),
            "signout" to m["signout"]), sortKeys = true)
        if (db.one("SELECT value FROM settings WHERE key = 'language'")?.str("value") == value) return false
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('language', ?)", value)
        loadLanguage()
        return true
    }

    /** The looks a school's ages need, youngest first. Ages 8 to 11 until the hub has said. */
    private fun looksFor(ages: Any?): List<String> {
        val found = (ages.asList() ?: emptyList()).mapNotNull { LOOKS[it] }.toSet()
        return listOf("young", "mid", "teen").filter { it in found }.ifEmpty { listOf("mid") }
    }

    /** Which of the three looks this student sees: their age on the class list, else the one they chose. */
    fun look(req: Request): String {
        val me = if (signIn.roster.isEmpty()) null else signIn.person((req.cookies["student"] ?: "").substringBefore('~'))
        val chosen = me?.look ?: req.cookies["look"] ?: ""
        return if (chosen in looks) chosen else if ("mid" in looks) "mid" else looks.last()
    }

    /** The student's language: the one they switched to, else the one on the class list, else the school's. Someone
     *  who has not signed in yet gets the one they last tapped in this browser. */
    fun words(req: Request): Strings {
        val name = (req.cookies["student"] ?: "").substringBefore('~')
        val person = if (name.isNotEmpty() && signIn.roster.isNotEmpty()) signIn.person(name) else null
        val codes = listOf(if (name.isEmpty()) "" else langOf[name.lowercase()] ?: "", person?.lang ?: "", req.cookies["lang"] ?: "")
        return codes.firstNotNullOfOrNull { languages[it] } ?: school
    }

    // -- assignments ----------------------------------------------------------

    /** Store a packed assignment from the radio or a course pack. Returns true if new or changed. */
    fun importAssignment(packed: Map<String, Any?>, open: Boolean? = null, due: String? = null): Boolean {
        val assignment = Assignments.unpack(packed)
        val aid = assignment["id"]?.toString() ?: ""
        val version = (packed["v"] as? String)?.takeIf { it.isNotEmpty() } ?: Assignments.versionOf(assignment)
        if (aid.isEmpty() || !truthy(assignment["questions"])) return false
        importPictures(packed["Px"])
        val row = db.one("SELECT version, open, due, received FROM assignments WHERE id = ?", aid)
        val newOpen = if (open != null) (if (open) 1L else 0L) else (row?.long("open") ?: 0L)
        val newDue = due ?: (row?.str("due")?.takeIf { it.isNotEmpty() } ?: assignment["due"]?.toString() ?: "")
        val changed = row == null || row.str("version") != version
        val data = Json.dumps(assignment)
        if (row == null) {
            db.execute("INSERT INTO assignments (id, version, data, open, due, received) VALUES (?, ?, ?, ?, ?, ?)",
                aid, version, data, newOpen, newDue, Messenger.now())
        } else {
            val received = if (changed) Messenger.now() else row.double("received") ?: Messenger.now()
            db.execute("UPDATE assignments SET version = ?, data = ?, open = ?, due = ?, received = ? WHERE id = ?",
                version, data, newOpen, newDue, received, aid)
        }
        return changed
    }

    fun importCourses(courses: List<Any?>): Int {
        var count = 0
        courses.forEachIndexed { position, c ->
            val course = try {
                Courses.normalizeCourse(c)
            } catch (e: Courses.CourseError) {
                return@forEachIndexed
            }
            db.execute("INSERT OR REPLACE INTO courses (id, data, position) VALUES (?, ?, ?)",
                course["id"], Json.dumps(course), position.toLong())
            count++
        }
        return count
    }

    /** Load assignments copied onto the memory card (data/assignments/ *.json). */
    fun loadPackFolder(): Int {
        loadVoices()
        if (!packDir.isDirectory) return 0
        var count = 0
        val coursesFile = File(packDir, "courses.json")
        if (coursesFile.exists()) {
            try {
                importCourses(Json.parse(coursesFile.readText()).asMap()?.get("courses").asList() ?: emptyList())
            } catch (e: Exception) {
                log.warning("skipping unreadable courses.json in the course pack")
            }
        }
        val languageFile = File(packDir, "language.json")
        if (languageFile.exists()) {
            try {
                importLanguage(Json.parse(languageFile.readText(Charsets.UTF_8)))
            } catch (e: Exception) {
                log.warning("skipping unreadable language.json in the course pack")
            }
        }
        val picturesFile = File(packDir, "pictures.json")
        if (picturesFile.exists()) {
            try {
                importPictures(Json.parse(picturesFile.readText(Charsets.UTF_8)).asMap()?.get("pictures"))
            } catch (e: Exception) {
                log.warning("skipping unreadable pictures.json in the course pack")
            }
        }
        for (f in (packDir.listFiles() ?: emptyArray()).sortedBy { it.name }) {
            if (!f.name.endsWith(".json") || f.name in setOf("courses.json", "language.json", "pictures.json")) continue
            try {
                val packed = Json.parse(f.readText()).asMap() ?: continue
                if (importAssignment(packed)) count++
            } catch (e: Exception) {
                log.warning("skipping unreadable course pack file ${f.name}")
            }
        }
        return count
    }

    /** Copy every assignment, and the school's words, from the teacher hub over Wi-Fi (no radio). */
    fun syncFromHub(hubUrl: String): Int {
        val url = hubUrl.trimEnd('/') + "/api/pack?node=" + quote(nodeId, "") + "&name=" + quote(name, "")
        val conn = URL(url).openConnection() as HttpURLConnection
        conn.connectTimeout = 20_000
        conn.readTimeout = 20_000
        val data = try {
            if (conn.responseCode != 200) throw IllegalStateException("HTTP Error ${conn.responseCode}")
            Json.parse(conn.inputStream.use { String(it.readBytes(), Charsets.UTF_8) }).asMap() ?: emptyMap()
        } finally {
            conn.disconnect()
        }
        importCourses(data["courses"].asList() ?: emptyList())
        importLanguage(data["language"])
        importPictures(data["pictures"])
        data["voices"].asMap()?.let { syncVoices(hubUrl, it) }
        return (data["assignments"].asList() ?: emptyList()).count { p -> p.asMap()?.let { importAssignment(it) } == true }
    }

    fun getAssignment(aid: String): MutableMap<String, Any?>? {
        val row = db.one("SELECT data, version, open, due FROM assignments WHERE id = ?", aid) ?: return null
        val a = LinkedHashMap(Json.parse(row.str("data")!!).asMap() ?: return null)
        a["version"] = row.str("version")
        a["open"] = (row.long("open") ?: 0L) != 0L
        a["due"] = row.str("due") ?: ""
        return a
    }

    // -- mesh -----------------------------------------------------------------

    fun onMessage(message: Message) {
        val p = message.payload
        when (message.type) {
            Protocol.ASSIGN -> p["a"].asMap()?.let { importAssignment(it, open = truthy(p["o"]), due = p["d"] as? String) }
            Protocol.OPEN -> {
                val aid = p["a"]?.let { pyStr(it) } ?: ""
                val version = p["v"]?.let { pyStr(it) } ?: ""
                val row = db.one("SELECT version FROM assignments WHERE id = ?", aid)
                if (row != null && row.str("version") == version) {
                    db.execute("UPDATE assignments SET open = ?, due = ? WHERE id = ?",
                        if (truthy(p["o"])) 1L else 0L, (p["d"] as? String)?.takeIf { truthy(it) } ?: "", aid)
                } else if (truthy(p["o"])) {
                    messenger.send(hubId, Protocol.NEED, linkedMapOf("a" to aid, "v" to version))
                }
            }
            Protocol.RESULT -> for (r in p["r"].asList() ?: emptyList()) {
                val m = r.asMap() ?: continue
                db.execute("UPDATE submissions SET score = ?, max = ?, complete = ?, detail = ? WHERE sub_id = ?",
                    m["sc"], m["mx"], if (truthy(m["ok"])) 1L else 0L, Json.dumps(m["q"].asMap() ?: emptyMap<String, Any?>(), spaced = true),
                    m["id"])
                m["an"].asMap()?.let { db.execute("UPDATE submissions SET right_answers = ? WHERE sub_id = ?", Json.dumps(it), m["id"]) }
                m["nt"].asMap()?.let { db.execute("UPDATE submissions SET notes = ? WHERE sub_id = ?", Json.dumps(it), m["id"]) }   // the teacher's short notes
            }
            else -> log.info("phone ignoring ${message.type} message from ${message.src}")
        }
    }

    /** Remember when a student opened an assignment, to time their work. */
    fun opened(student: String, aid: String) {
        db.execute("INSERT OR REPLACE INTO opens (student, aid, opened_at) VALUES (?, ?, ?)", student, aid, Messenger.now())
    }

    /** Save a submission now; the batcher sends it with others shortly. */
    fun submit(assignment: Map<String, Any?>, student: String, answers: Map<String, String>, duration: Double? = null,
               signals: Map<String, Any?> = emptyMap()): String {
        val subId = Assignments.newId("s")
        val aid = assignment["id"].toString()
        var dur = duration
        if (dur == null) {
            val row = db.one("SELECT opened_at FROM opens WHERE student = ? AND aid = ?", student, aid)
            val openedAt = row?.double("opened_at")
            if (openedAt != null && Messenger.now() - openedAt in 0.0..MAX_OPEN_S) {
                dur = maxOf(1.0, Math.rint((Messenger.now() - openedAt) * 10) / 10)   // under a second still counts
            }
        }
        db.execute("INSERT INTO submissions (sub_id, assignment_id, version, student, answers, submitted_at, duration, signals) " +
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", subId, aid, assignment["version"], student, Json.dumps(answers, spaced = true),
            Messenger.now(), dur, if (signals.isEmpty()) null else Json.dumps(signals))
        db.execute("DELETE FROM opens WHERE student = ? AND aid = ?", student, aid)
        if ((db.one("SELECT COUNT(*) AS n FROM submissions WHERE msg_id IS NULL")?.long("n") ?: 0) >= MAX_BATCH) flush()
        return subId
    }

    /** Send waiting submissions as one message once the oldest has waited batchS. */
    fun flushSubmissions(force: Boolean = false): Long? {
        val rows = db.all("SELECT * FROM submissions WHERE msg_id IS NULL ORDER BY submitted_at LIMIT ?", MAX_BATCH.toLong())
        if (rows.isEmpty()) return null
        if (!force && rows.size < MAX_BATCH && Messenger.now() - (rows[0].double("submitted_at") ?: 0.0) < batchS) return null
        val batch = rows.map { r ->
            val item = linkedMapOf<String, Any?>(
                "id" to r.str("sub_id"), "a" to r.str("assignment_id"), "v" to r.str("version"), "u" to r.str("student"),
                "x" to Json.parse(r.str("answers")!!), "t" to Comprehension.pyRound(r.double("submitted_at") ?: 0.0),
            )
            r.double("duration")?.takeIf { it != 0.0 }?.let { item["d"] = Comprehension.pyRound(it) }
            r.str("signals")?.let { item.putAll(Json.parse(it).asMap() ?: emptyMap()) }
            item
        }
        val msgId = messenger.send(hubId, Protocol.SUBMIT, mapOf("s" to batch))
        for (r in rows) db.execute("UPDATE submissions SET msg_id = ? WHERE sub_id = ?", msgId, r.str("sub_id"))
        return msgId
    }

    private fun batchLoop() {
        while (running) {
            try {
                flushSubmissions()
            } catch (e: Exception) {
                log.log(Level.WARNING, "sending submissions failed", e)
            }
            synchronized(flushLock) {
                if (!flushNow) flushLock.wait((minOf(1.0, maxOf(batchS / 4, 0.1)) * 1000).toLong())
                flushNow = false
            }
        }
    }

    // -- library --------------------------------------------------------------

    /** Everything on the card: the fetcher's index first, then any other files. */
    fun libraryItems(): List<MutableMap<String, Any?>> {
        val items = ArrayList<MutableMap<String, Any?>>()
        val known = HashSet<String>()
        val index = File(libraryDir, "index.json")
        if (index.exists()) {
            try {
                for (entry in Json.parse(index.readText()).asList() ?: emptyList()) {
                    val e = LinkedHashMap(entry.asMap() ?: continue)
                    val rel = e["file"]?.toString() ?: ""
                    val f = File(libraryDir, rel)
                    if (rel.isNotEmpty() && f.isFile) {
                        e["size"] = f.length()
                        items.add(e)
                        known.add(File(rel).path)
                    }
                }
            } catch (e: Exception) {
                log.warning("could not read library index")
            }
        }
        libraryDir.walkTopDown().filter { it.isFile }.sortedWith(compareBy({ it.parentFile.path }, { it.name })).forEach { f ->
            val ext = "." + f.name.substringAfterLast('.', "").lowercase()
            val rel = f.relativeTo(libraryDir).path
            if (ext !in VIEWABLE || rel in known || f.name.startsWith(".")) return@forEach
            val title = capitalize(f.name.substringBeforeLast('.').replace("_", " ").replace("-", " ").trim())
            val folder = File(rel).parent ?: ""
            items.add(linkedMapOf("title" to title, "file" to rel, "size" to f.length(),
                "collection" to (if (folder.isNotEmpty()) capitalize(folder) else ""), "kind" to ext.removePrefix(".")))
        }
        return items
    }

    // -- web ------------------------------------------------------------------

    /** Big tabs; the one for this page is highlighted. */
    private fun nav(req: Request, w: Strings): String {
        val who = student(req)
        val section = when {
            req.path.startsWith("/library") -> "/library"
            req.path.startsWith("/results") -> "/results"
            req.path == "/" || req.path.startsWith("/c/") || req.path.startsWith("/a/") -> "/"
            else -> ""
        }
        val young = look(req) == "young"
        val signedIn = !who.isNullOrEmpty()
        val tabs = mutableListOf(Triple("/", if (young && signedIn) w("young.work") else w("tab.home"), "home"))
        if (!(young && signedIn)) tabs.add(Triple("/library", w("tab.library"), "books"))   // young students: a card on their first page
        if (signedIn) tabs.add(Triple("/results", if (young) w("young.stars") else w("tab.results"), if (look(req) == "teen") "chart" else "star"))
        return "<nav class='tabs'>" + tabs.joinToString("") { (href, label, name) ->
            "<a href='$href'${if (href == section) " aria-current='page'" else ""}><span>${icon(name)}</span>${esc(label)}</a>"
        } + "</nav>"
    }

    /** With a second language, a button in the header switches to the other one. */
    private fun languageSwitch(req: Request, w: Strings): String {
        val others = languages.values.filter { it.code != w.code }
        if (others.isEmpty() || (req.path == "/" && student(req) == null)) return ""
        val back = req.path + (if (req.query.isEmpty()) "" else
            "?" + req.query.entries.joinToString("&") { (k, v) -> quote(k, "") + "=" + quote(v, "") })
        val buttons = others.joinToString("") { s ->
            "<button name='to' value='${esc(s.code)}' title='${esc(w("lang.switch"))}' lang='${esc(s.tag)}' dir='${s.dir}'>${esc(s.name)}</button>"
        }
        return "<form method='post' action='/lang' class='lang'><input type='hidden' name='back' value='${esc(back)}'>$buttons</form>"
    }

    /** On the welcome page: every language this school learns in, each in its own words. */
    private fun languagePicker(w: Strings): String {
        if (languages.size < 2) return ""
        val buttons = languages.values.joinToString("") { s ->
            "<button name='to' value='${esc(s.code)}' aria-pressed='${s.code == w.code}' lang='${esc(s.tag)}' dir='${s.dir}'>${esc(s.name)}</button>"
        }
        return "<form method='post' action='/lang' class='card'><strong>${esc(w("welcome.language"))}</strong>" +
            "<input type='hidden' name='back' value='/'><div class='langpick'>$buttons</div></form>"
    }

    private fun page(req: Request, title: String, body: String): String {
        val w = words(req)
        val who = student(req)
        val site = "MeshLearn · $name"
        val home = !who.isNullOrEmpty() && req.path == "/"
        val listen = "<button type='button' class='aa' data-reading aria-label='${esc(w("tools.reading"))}'>Aa</button>" +
            "<button type='button' class='listen' data-listen style='display:none' aria-label='${esc(w("listen.page"))}'>${icon("speaker")}</button>"
        val me = if (who != null && signIn.roster.isNotEmpty()) signIn.person(who) else null
        val face = if (me != null) signIn.face(me) else if (who != null) "<span class='avatar' aria-hidden='true'>${esc(who.take(1).uppercase())}</span>" else ""
        val html = Html.page(title, body + signIn.idleForm(who != null) + readingPanel(w) + "<script>${Html.LISTEN_JS}</script><script>${Html.MATH_JS}</script>", languageSwitch(req, w) + nav(req, w), site = site,
            extraCss = Html.STUDENT_CSS, lang = w.tag, dir = w.dir, bodyCls = "st look-" + look(req),
            before = if (home) face else "", after = listen)
        // The student's name sits on the right of the header line, so they can see who is signed in.
        return if (who.isNullOrEmpty()) html
        else html.replaceFirst("<header><strong>${esc(site)}</strong>",
            "<header><strong><span>${esc(site)}</span><a class='who' href='/me'>${esc(who)}</a></strong>")
    }

    /** Bigger text, easy-to-read letters (OpenDyslexic) and more space; the browser remembers the choice. */
    private fun readingPanel(w: Strings): String = "<div id='readpanel' role='group' aria-label='${esc(w("tools.reading"))}' style='display:none'>" +
        "<button type='button' data-down>A− ${esc(w("tools.smaller"))}</button><button type='button' data-up>A+ ${esc(w("tools.bigger"))}</button>" +
        "<button type='button' data-font aria-pressed='false'>${esc(w("tools.letters"))}</button>" +
        "<button type='button' data-space aria-pressed='false'>${esc(w("tools.space"))}</button>" +
        "<button type='button' data-shut>${esc(w("tools.close"))}</button></div><script>${Html.READING_JS}</script>"

    private fun scratchpad(w: Strings): String = "<div id='scratch' style='display:none'><div class='bar2'><strong>${esc(w("tools.scratch"))}</strong>" +
        "<button type='button' data-undo>${esc(w("tools.undo"))}</button><button type='button' data-clear>${esc(w("tools.clear"))}</button>" +
        "<button type='button' data-shut>${esc(w("tools.close"))}</button></div>" +
        "<canvas aria-label='${esc(w("tools.scratch"))}'></canvas></div><script>${Html.SCRATCH_JS}</script>"

    /** For ZipApps: the last thing this student opened. */
    fun remember(req: Request, rel: String) { student(req)?.let { rememberReading(it, rel) } }

    private fun routes() {
        app.add("GET", "/", ::home)
        app.add("POST", "/login", ::login)
        app.add("POST", "/logout", ::logout)
        app.add("POST", "/lang", ::switchLanguage)
        app.add("POST", "/look", ::switchLook)
        signIn.routes(app)
        ZipApps(this) { r, title, body, status -> Response(page(r, title, body), status) }.routes(app)
        app.add("GET", "/c/<cid>", ::coursePage)
        app.add("GET", "/a/<aid>", ::assignmentForm)
        app.add("POST", "/a/<aid>", ::assignmentSubmit)
        app.add("GET", "/results", ::results)
        app.add("GET", "/library", ::library)
        app.add("GET", "/library/files/<path:path>", ::libraryFile)
        app.add("GET", "/voice/<key>") { Voices.response(voices, it.params["key"] ?: "") }
        app.add("GET", "/status") { status(it) }
        app.add("POST", "/status/sync", ::statusSync)
        app.add("POST", "/status/reload", ::statusReload)
    }

    // -- courses and progress ---------------------------------------------------

    /** Courses with their units: from the course pack, plus any known only from radio assignments, plus 'Other'. */
    fun courseTree(): List<MutableMap<String, Any?>> {
        val tree = db.all("SELECT data FROM courses ORDER BY position")
            .mapNotNull { r -> Json.parse(r.str("data")!!).asMap()?.let { LinkedHashMap(it) } }.toMutableList<MutableMap<String, Any?>>()
        tree.forEach { c -> c["units"] = Courses.units(c).map { LinkedHashMap(it) }.toMutableList() }
        val known = LinkedHashMap<String, MutableMap<String, Any?>>()
        tree.forEach { known[it["id"].toString()] = it }
        var loose = false
        for (r in db.all("SELECT data FROM assignments")) {
            val a = Json.parse(r.str("data")!!).asMap() ?: continue
            val cid = a["course"]?.toString() ?: ""
            if (cid.isEmpty()) { loose = true; continue }
            var course = known[cid]
            if (course == null) {
                course = linkedMapOf("id" to cid, "title" to (a["course_title"]?.toString()?.ifEmpty { null } ?: cid),
                    "description" to "", "units" to ArrayList<MutableMap<String, Any?>>())
                known[cid] = course
                tree.add(course)
            }
            val uid = a["unit"]?.toString() ?: ""
            if (uid.isNotEmpty() && Courses.unitOf(course, uid) == null) {
                @Suppress("UNCHECKED_CAST")
                (course["units"] as MutableList<MutableMap<String, Any?>>).add(linkedMapOf(
                    "id" to uid, "title" to (a["unit_title"]?.toString()?.ifEmpty { null } ?: uid), "_no" to (a["unit_no"] ?: "")))
            }
        }
        for (course in tree) {
            @Suppress("UNCHECKED_CAST")
            val units = course["units"] as MutableList<MutableMap<String, Any?>>
            if (units.any { it.containsKey("_no") }) units.sortBy { pyStr(it["_no"] ?: "").ifEmpty { "999" }.toIntOrNull() ?: 999 }
        }
        if (loose) tree.add(LinkedHashMap(Courses.OTHER))
        return tree
    }

    /** A course's title; "Other assignments" in the student's language. */
    private fun title(course: Map<String, Any?>, w: Strings): String =
        if (course["id"] == "") w("course.other.title") else course["title"].toString()

    private fun description(course: Map<String, Any?>, w: Strings): String =
        if (course["id"] == "") w("course.other.description") else (course["description"]?.toString() ?: "")

    private fun assignmentsIn(courseId: String, openOnly: Boolean = true, w: Strings? = null): List<MutableMap<String, Any?>> {
        val rows = db.all("SELECT id FROM assignments" + (if (openOnly) " WHERE open = 1" else "") +
            " ORDER BY due = '', due, received DESC")
        val known = courseTree().map { it["id"].toString() }.toSet()
        return rows.mapNotNull { getAssignment(it.str("id")!!) }.filter { a ->
            val c = a["course"]?.toString() ?: ""
            (if (c in known) c else "") == courseId
        }.map { if (w == null) it else Assignments.localized(it, w.code) }
    }

    /** This student's graded work, for the comprehension summary. */
    private fun myRecords(student: String): List<Map<String, Any?>> {
        val out = ArrayList<Map<String, Any?>>()
        for (s in db.all("SELECT * FROM submissions WHERE student = ? AND score IS NOT NULL ORDER BY submitted_at", student)) {
            val a = getAssignment(s.str("assignment_id")!!) ?: continue
            out.add(mapOf("student" to student, "aid" to a["id"], "course" to (a["course"] ?: ""), "unit" to (a["unit"] ?: ""),
                "mode" to (a["mode"] ?: "graded"), "at" to s.double("submitted_at"), "duration" to s.double("duration"),
                "questions" to a["questions"], "answers" to Json.parse(s.str("answers")!!),
                "scores" to Json.parse(s.str("detail") ?: "{}")))
        }
        return out
    }

    private fun level(level: String, score: Long?, w: Strings): String {
        val cls = mapOf("Strong" to "good", "Good" to "good", "Developing" to "warn", "Needs work" to "bad")[level] ?: ""
        return "<span class='pill $cls'>${esc(w.level(level))}${if (score == null) "" else " · $score%"}</span>"
    }

    /** One assignment as a card that opens it: what it is, when it is due, and what to do next. */
    private fun assignmentCard(a: Map<String, Any?>, sub: Row?, w: Strings, courseTitle: String = ""): String {
        val practice = a["mode"] == "practice"
        val (state, action) = when {
            sub == null -> "" to (if (practice) w("card.practice") else w("card.start"))
            practice -> "<span class='pill good'>${esc(w("card.practiced"))}</span>" to w("card.practice_again")
            sub["score"] == null -> "<span class='pill warn'>${esc(w("card.waiting"))}</span>" to ""
            (sub.long("complete") ?: 0L) == 0L ->
                "<span class='pill warn'>${esc(w("card.so_far", "score" to g(sub.double("score")), "max" to g(sub.double("max"))))}</span>" to ""
            else -> "<span class='pill good'>${esc(w("card.score", "score" to g(sub.double("score")), "max" to g(sub.double("max"))))}</span>" to ""
        }
        val qs = Assignments.questions(a)
        val meta = listOfNotNull(courseTitle.ifEmpty { null }, w.n("card.questions", qs.size), Html.minutesFor(qs, w),
            if (practice) w("card.practice_tag") else null).joinToString(" · ")
        val (dueText, dueCls) = Html.dueLabel(a["due"]?.toString(), turnedIn = sub != null, words = w)
        val due = if (dueText.isNotEmpty()) "<p class='due $dueCls'>${esc(dueText)}</p>" else ""
        val go = if (action.isNotEmpty()) "<span class='button go'>${esc(action)}</span>" else ""
        return "<a class='card task' href='/a/${esc(a["id"])}'><h3>${esc(a["title"])}</h3>" +
            "<p class='meta'>${esc(meta)}</p>$due$state$go</a>"
    }

    private fun myLatest(student: String): Map<String, Row> = db.all(
        "SELECT assignment_id, MAX(submitted_at) AS at, score, max, complete FROM submissions " +
            "WHERE student = ? GROUP BY assignment_id", student).associateBy { it.str("assignment_id")!! }

    private fun student(req: Request): String? = signIn.student(req)

    fun home(req: Request): Response {
        val w = words(req)
        val student = student(req)
        if (student == null) {
            val link = "<a href='/library'>${esc(w("welcome.library"))}</a>"
            if (signIn.roster.isNotEmpty()) {
                val who = "<div class='compact'>${languagePicker(w)}${agePicker(req, w)}</div>" + signIn.whoBody(req, w) +
                    "<p>${w.html("welcome.just_read", "link" to link)}</p><script>${Html.SIGNIN_JS}</script>"
                return Response(page(req, w("signin.who"), who))
            }
            val body = languagePicker(w) + agePicker(req, w) + "<p class='lead'>${esc(w("welcome.lead"))}</p>" +
                "<form method='post' action='/login' class='card'><label for='student'>${esc(w("welcome.name"))}</label>" +
                "<input type='text' id='student' name='student' maxlength='40' autocomplete='name' required>" +
                "<button class='big'>${esc(w("welcome.start"))}</button></form>" +
                "<p>${w.html("welcome.just_read", "link" to link)}</p>"
            return Response(page(req, w("welcome.title"), body))
        }
        val mine = myLatest(student)
        val records = myRecords(student)
        val cards = StringBuilder()
        val todo = ArrayList<Pair<Map<String, Any?>, String>>()
        for (course in courseTree()) {
            val openItems = assignmentsIn(course["id"].toString(), w = w)
            if (course["id"] == "" && openItems.isEmpty()) continue
            val courseTitle = title(course, w)
            todo += openItems.filter { it["id"] !in mine }.map { it to courseTitle }
            if (truthy(course["comingSoon"])) {
                cards.append("<div class='card' style='opacity:.6'><h2 style='margin-top:0'>${esc(courseTitle)}</h2>" +
                    "<p class='muted'>${esc(description(course, w))}</p><span class='pill'>${esc(w("course.coming_later"))}</span></div>")
                continue
            }
            val summary = Comprehension.summarize(records, course)
            val done = openItems.count { it["id"] in mine }
            val href = esc(course["id"]).ifEmpty { "_other" }
            val progress = if (openItems.isEmpty()) "<p class='meta'>${esc(w("course.nothing_now"))}</p>" else {
                val pct = done * 100 / openItems.size
                val doneText = esc(w("course.done", "done" to done, "total" to openItems.size))
                "<div class='bar' role='img' aria-label='$doneText'><span style='width:$pct%'></span></div>" +
                    "<p class='meta'>$doneText</p>"
            }
            cards.append("<a class='card' href='/c/$href'><h2>${esc(courseTitle)}</h2>" +
                "<p class='muted'>${esc(description(course, w))}</p>$progress${level(summary.level, summary.score, w)}</a>")
        }
        val change = "<form method='post' action='/logout'><button class='secondary'>${esc(w("home.change_name"))}</button></form>"
        val body = when (look(req)) {
            "young" -> homeYoung(todo, student, w)
            "teen" -> homeTeen(todo, records, student, w)
            else -> homeTop(todo, mine, student, w) + "<h2>${esc(w("home.courses"))}</h2>" +
                (if (cards.isNotEmpty()) "<div class='grid'>$cards</div>" else "<p class='muted'>${esc(w("home.no_courses"))}</p>")
        }
        return Response(page(req, w("home.title", "name" to student), body + change + "<script>${Html.RESUME_JS}</script>"))
    }

    /** The button that opens an assignment; says "Keep going" when the browser holds answers (RESUME_JS). */
    private fun start(a: Map<String, Any?>, student: String, w: Strings): String {
        val label = if (a["mode"] == "practice") w("card.practice") else w("card.start")
        return "<a class='button' href='/a/${esc(a["id"])}' data-draft='${esc(student + "|" + a["id"])}' " +
            "data-resume='${esc(w("home.resume"))}'><span class='lb'>${esc(label)}</span> ${icon("arrow", "arr")}</a>"
    }

    private fun sub(a: Map<String, Any?>): String = subjectIcon(a).let { "<span class='sub $it'>${icon(it)}</span>" }

    private fun allDone(student: String, w: Strings): String = "<div class='card'><p class='grade good' style='margin-top:0'>${esc(w("home.all_done"))}</p>" +
        "<p>${esc(w("home.all_done_text"))}</p><a class='button secondary' href='/library'>${esc(w("tab.library"))}</a></div>" + keepReading(student, w)

    /** Ages 5 to 7: one big thing to play, the rest as picture cards, and this week's stars. */
    private fun homeYoung(todo: List<Pair<Map<String, Any?>, String>>, student: String, w: Strings): String {
        val out = StringBuilder("<h2>${esc(w("young.today"))}</h2>")
        val play = icon("play", "fillplay")
        if (todo.isNotEmpty()) {
            val a = todo[0].first
            out.append("<a class='bigplay' href='/a/${esc(a["id"])}'>${sub(a)}<strong>${esc(a["title"])}</strong>" +
                "<span class='go2'>$play${esc(w("young.play"))}</span></a>")
            for ((b, _) in todo.drop(1).take(3)) {
                out.append("<a class='ycard' href='/a/${esc(b["id"])}'>${sub(b)}<span class='grow'>${esc(b["title"])}</span>" +
                    "<span class='ply' aria-hidden='true'>$play</span></a>")
            }
        } else {
            out.append("<div class='done'><span class='tick'>${icon("check")}</span><p class='grade'>${esc(w("home.all_done"))}</p></div>")
        }
        out.append("<a class='ycard' href='/library'><span class='sub book'>${icon("book")}</span>" +
            "<span class='grow'>${esc(w("tab.library"))}</span><span class='ply' aria-hidden='true'>$play</span></a>")
        val week = Messenger.now() - 7 * 86400
        val stars = db.all("SELECT * FROM submissions WHERE student = ?", student)
            .filter { (it.double("submitted_at") ?: 0.0) >= week }.sumOf { starsOf(it) ?: 0 }
        return out.toString() + "<div class='weekstars'>${icon("star")}<span>${esc(w("young.stars_n", "n" to stars))}</span></div>"
    }

    /** Ages 12 and up: the next thing, everything due by date, and every course at a glance. */
    private fun homeTeen(todo: List<Pair<Map<String, Any?>, String>>, records: List<Map<String, Any?>>, student: String, w: Strings): String {
        val lead = if (todo.isEmpty()) w("home.caught_up") else w.n("home.todo", todo.size)
        val out = StringBuilder("<p class='lead'>${esc(lead)}</p>")
        out.append(if (todo.isNotEmpty()) nextCard(todo[0].first, todo[0].second, student, w) else allDone(student, w))
        val rows = StringBuilder()
        for ((a, course) in todo.drop(1).take(8)) {
            val cls = Html.dueLabel(a["due"]?.toString(), turnedIn = false, words = w).second
            val parts = w.dayParts(a["due"]?.toString())
            val day = if (parts != null) "<span class='day $cls'>${esc(parts.first)}<b>${parts.second}</b></span>" else "<span class='day'>–</span>"
            val kind = if (a["mode"] == "practice") w("card.practice_tag") else w.n("card.questions", Assignments.questions(a).size)
            val meta = listOf(course, kind).filter { it.isNotEmpty() }.joinToString(" · ")
            rows.append("<a href='/a/${esc(a["id"])}'>$day<span class='grow'><strong>${esc(a["title"])}</strong><span class='meta'>${esc(meta)}</span></span></a>")
        }
        if (rows.isNotEmpty()) out.append("<h2>${esc(w("home.coming_up"))}</h2><div class='list'>$rows</div>")
        if (todo.size > 9) out.append("<p class='meta'>${esc(w("home.more", "n" to todo.size - 9))}</p>")
        return out.toString() + "<h2>${esc(w("home.courses"))}</h2>" +
            courseTiles(records, w).ifEmpty { "<p class='muted'>${esc(w("home.no_courses"))}</p>" }
    }

    private fun courseTiles(records: List<Map<String, Any?>>, w: Strings, markedOnly: Boolean = false): String {
        val tiles = StringBuilder()
        for (course in courseTree()) {
            if (course["id"] == "" && assignmentsIn("").isEmpty()) continue
            val name = esc(title(course, w))
            if (truthy(course["comingSoon"])) {
                if (!markedOnly) tiles.append("<div class='tile' style='opacity:.6'><b>$name</b><small>${esc(w("course.coming_later"))}</small></div>")
                continue
            }
            val s = Comprehension.summarize(records, course)
            if (markedOnly && s.answered == 0) continue
            val score = if (s.score == null) "–" else "${s.score}%"
            tiles.append("<a class='tile' href='/c/${esc(course["id"]).ifEmpty { "_other" }}'><b>$name</b><strong>$score</strong>" +
                "<div class='bar'><span style='width:${s.score ?: 0}%'></span></div><small>${esc(w.level(s.level))}</small></a>")
        }
        return if (tiles.isEmpty()) "" else "<div class='tiles'>$tiles</div>"
    }

    private fun nextCard(a: Map<String, Any?>, courseTitle: String, student: String, w: Strings): String {
        val qs = Assignments.questions(a)
        val meta = listOf(courseTitle, w.n("card.questions", qs.size), Html.minutesFor(qs, w)).filter { it.isNotEmpty() }.joinToString(" · ")
        val dueText = Html.dueLabel(a["due"]?.toString(), turnedIn = false, words = w).first
        return "<section class='next' aria-labelledby='next-label'><span class='label' id='next-label'>${esc(w("home.next"))}</span>" +
            "<div class='nhead'>${sub(a)}<h2>${esc(a["title"])}</h2></div><p class='meta'>${esc(meta)}</p>" +
            (if (dueText.isNotEmpty()) "<p class='due'>${esc(dueText)}</p>" else "") + start(a, student, w) + "</section>"
    }

    /** How much is done, the one thing to do next as a big card, then the rest (Ages 8 to 11 design). */
    private fun homeTop(todo: List<Pair<Map<String, Any?>, String>>, mine: Map<String, Row>, student: String, w: Strings): String {
        val total = todo.size + courseTree().sumOf { c -> assignmentsIn(c["id"].toString()).count { it["id"] in mine } }
        val lead = if (todo.isEmpty()) w("home.caught_up") else w.n("home.todo", todo.size)
        val out = StringBuilder("<p class='lead'>${esc(lead)}</p>")
        if (total > 0) {
            val done = total - todo.size
            val text = esc(w("home.week_done", "done" to done, "total" to total))
            out.append("<div class='week'><div class='wk'><strong>${esc(w("home.week"))}</strong><span class='meta'>$text</span></div>" +
                "<div class='bar' role='img' aria-label='$text'><span style='width:${done * 100 / total}%'></span></div></div>")
        }
        if (todo.isEmpty()) {
            return out.toString() + allDone(student, w)
        }
        out.append(nextCard(todo[0].first, todo[0].second, student, w))
        if (todo.size > 1) {
            out.append("<h2>${esc(w("home.coming_up"))}</h2>")
            for ((b, course) in todo.subList(1, minOf(todo.size, 5))) {
                val due = Html.dueLabel(b["due"]?.toString(), turnedIn = false, words = w).first
                val m = listOf(course, due.ifEmpty { if (b["mode"] == "practice") w("card.practice_tag") else "" })
                    .filter { it.isNotEmpty() }.joinToString(" · ")
                out.append("<a class='row' href='/a/${esc(b["id"])}'>${sub(b)}<span class='grow'><strong>${esc(b["title"])}</strong>" +
                    "<span class='meta'>${esc(m)}</span></span><span class='chev' aria-hidden='true'>${icon("chev", "arr")}</span></a>")
            }
            if (todo.size > 5) out.append("<p class='meta'>${esc(w("home.more", "n" to todo.size - 5))}</p>")
        }
        return out.toString()
    }

    fun coursePage(req: Request): Response {
        val w = words(req)
        val student = student(req) ?: return Response.redirect("/")
        val cid = if (req.params["cid"] == "_other") "" else req.params["cid"] ?: ""
        val course = courseTree().firstOrNull { it["id"] == cid }
            ?: return Response(page(req, w("notfound.title"), "<p>${esc(w("course.not_here"))}</p>"), 404)
        val mine = myLatest(student)
        val summary = Comprehension.summarize(myRecords(student), course)
        val byUnit = summary.units.associateBy { it.id }
        val items = assignmentsIn(cid, w = w)
        val units = Courses.units(course)
        val sections = StringBuilder()
        units.forEachIndexed { i, unit ->
            val inUnit = items.filter { (it["unit"] ?: "") == unit["id"] }
            val u = byUnit[unit["id"]]
            val read = (unit["read"]?.toString()?.ifEmpty { null } ?: unit["title"].toString())
            sections.append("<h2>${esc(w("course.unit", "n" to i + 1, "title" to unit["title"]))} ${level(u?.level ?: "Not started", u?.score, w)}</h2>" +
                (inUnit.joinToString("") { assignmentCard(it, mine[it["id"]], w) }
                    .ifEmpty { "<p class='muted'>${esc(w("course.nothing_in_unit"))}</p>" }) +
                "<p><a class='button secondary' href='/library?q=${quote(read)}'>${esc(w("course.find_readings"))}</a></p>")
        }
        val unitIds = units.map { it["id"] }.toSet()
        val loose = items.filter { (it["unit"] ?: "") !in unitIds }
        if (loose.isNotEmpty()) {
            val heading = if (units.isEmpty()) w("course.assignments") else w("course.other_assignments")
            sections.append("<h2>${esc(heading)}</h2>" + loose.joinToString("") { assignmentCard(it, mine[it["id"]], w) })
        }
        val body = "<p class='muted'>${esc(description(course, w))}</p>" +
            "<p>${w.html("course.your_level", "level" to level(summary.level, summary.score, w))}</p>$sections"
        return Response(page(req, title(course, w), body))
    }

    fun login(req: Request): Response = signIn.login(req)

    fun logout(req: Request): Response = Response.redirect("/").setCookie("student", "", maxAge = 0)

    fun switchLanguage(req: Request): Response {
        var back = req.form["back"] ?: "/"
        if (!back.startsWith("/") || back.startsWith("//")) back = "/"
        val code = req.form["to"] ?: ""
        val response = Response.redirect(back)
        if (code !in languages) return response
        val name = student(req) ?: return response.setCookie("lang", code)
        langOf[name.lowercase()] = code   // on a shared phone the choice is the student's, not the browser's
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", "lang:" + name.lowercase(), code)
        return response
    }

    fun switchLook(req: Request): Response {
        val look = req.form["to"] ?: ""
        val response = Response.redirect("/")
        return if (look in looks) response.setCookie("look", look) else response
    }

    /** On the welcome page, when the school has students of different ages. */
    private fun agePicker(req: Request, w: Strings): String {
        if (looks.size < 2) return ""
        val buttons = looks.joinToString("") { "<button name='to' value='$it' aria-pressed='${it == look(req)}'>${esc(w("look.$it"))}</button>" }
        return "<form method='post' action='/look' class='card agepick'><strong>${esc(w("welcome.age"))}</strong>" +
            "<div class='langpick'>$buttons</div></form>"
    }

    /**
     * The assignment as a list of question cards, with big choices, a character count on written
     * answers, and a bar at the bottom saying how many are answered. Answers are kept in the browser
     * while the student works (ASSIGNMENT_JS), keyed by student and assignment.
     */
    private fun renderForm(req: Request, a: Map<String, Any?>, student: String, values: Map<String, String>,
                           errors: Map<String, String>): String {
        val w = words(req)
        val parts = StringBuilder()
        val qs = Assignments.questions(a)
        val n = qs.size
        val count = w.n("card.questions", n)
        val dueText = Html.dueLabel(a["due"]?.toString(), turnedIn = false, words = w).first
        val subtitle = listOf(dueText, w("form.saved_here")).filter { it.isNotEmpty() }.joinToString(" · ")
        parts.append("<p class='meta formmeta'>${esc(w("form.meta", "count" to count, "minutes" to Html.minutesFor(qs, w)))}</p>")
        if (errors.isNotEmpty()) parts.append("<p class='error' role='alert'>${esc(w.n("form.fix", errors.size))}</p>")
        parts.append("<form id='aform' method='post' action='/a/${esc(a["id"])}' data-key='${esc(student + "|" + a["id"])}' " +
            "data-none='${esc(w("form.no_answer"))}' data-flagged='${esc(w("form.flagged"))}' " +
            "data-unsure='${esc(w("form.unsure"))}' data-change='${esc(w("form.change"))}' data-next='${esc(w("form.next"))}' data-review='${esc(w("form.review"))}' " +
            "data-star=\"${icon("star")}\" novalidate>")
        parts.append("<div class='qhead'><a class='qx' href='/' aria-label='${esc(w("form.close"))}'>${icon("close")}</a>" +
            "<span class='qt'><b>${esc(a["title"])}</b><small>${esc(subtitle)}</small></span>" +
            "<button type='button' class='scratchopen' id='scratchopen' aria-label='${esc(w("tools.scratch"))}'>${icon("pencil")}</button>" +
            "<button type='button' class='aa' data-reading aria-label='${esc(w("tools.reading"))}'>Aa</button>" +
            "<button type='button' class='listen' data-listen style='display:none' aria-label='${esc(w("listen.button"))}'>" +
            "${icon("speaker")}<span>${esc(w("listen.button"))}</span></button></div><div class='qprog' id='qprog'></div>")
        qs.forEachIndexed { idx, q ->
            val i = idx + 1
            val qid = esc(q["id"])
            val value = values[q["id"].toString()] ?: ""
            val err = errors[q["id"].toString()]
            val errHtml = if (err != null) "<p class='error' id='$qid-err'>${esc(w.error(err))}</p>" else ""
            val described = if (err != null) " aria-describedby='$qid-err' aria-invalid='true'" else ""
            val points = (q["points"] as? Number)?.toDouble() ?: 1.0
            val pointsText = if (points == 1.0) w("form.points.one") else w("form.points.other", "n" to g(points))
            val legend = "<legend><span class='qnum'>${esc(w("form.qnum", "i" to i, "n" to n, "points" to pointsText))}</span>" +
                "${esc(q["prompt"])}</legend>" + Html.picturesHtml(q["pic"]?.toString() ?: "", own, w.code)
            val cls = if (err != null) "q bad" else "q"
            val info = questionInfo(a, q, i, w)
            val flag = "<label class='flag unsure'><input type='checkbox' name='unsure-$qid' value='1'" +
                "${if (("unsure-" + q["id"]) in values) " checked" else ""}> ${esc(w("form.unsure"))}</label>" +
                "<label class='flag'><input type='checkbox' data-flag='$qid'> ${esc(w("form.flag"))}</label></fieldset></div>"
            parts.append("<div class='qwrap${if (info.isNotEmpty()) " has" else ""}' data-prompt='${esc(q["prompt"])}'${voice(q["prompt"].toString(), w)}>$info")
            when (q["type"]) {
                "choice" -> {
                    val opic = q["opic"].asList() ?: emptyList()
                    val short = if (opic.any { truthy(it) } || (q["options"].asList() ?: emptyList()).all { pyStr(it).length <= 4 }) " short" else ""
                    val opts = (q["options"].asList() ?: emptyList()).mapIndexed { j, opt ->
                        val on = value == j.toString()
                        "<label class='option${if (on) " on" else ""}'><input type='radio' name='$qid' value='$j'" +
                            "${if (on) " checked" else ""}$described>" +
                            "<span class='ltr' aria-hidden='true'>${'A' + j}</span>${opic.getOrNull(j)?.let { Html.picture(it.toString(), own = own) } ?: ""}<span class='otx'${voice(pyStr(opt), w)}>${esc(opt)}</span></label>"
                    }.joinToString("")
                    parts.append("<fieldset class='$cls'>$legend$errHtml<div class='opts$short'>$opts</div>$flag")
                }
                "long" -> {
                    val limit = (q["max_chars"] as? Number)?.toLong() ?: 600L
                    parts.append("<fieldset class='$cls'>$legend$errHtml" +
                        "<label for='$qid'>${esc(w("form.long", "n" to limit))}</label>" +
                        "<textarea id='$qid' name='$qid' maxlength='$limit'$described>${esc(value)}</textarea>" +
                        "<p class='counter' id='$qid-count' aria-live='polite'>${value.length} / $limit</p>$flag")
                }
                else -> {
                    val number = q["type"] == "number"
                    val mode = if (number) " inputmode='decimal'" else ""
                    val hint = if (number) w("form.number") else w("form.short")
                    parts.append("<fieldset class='$cls'>$legend$errHtml" +
                        "<label for='$qid'>${esc(hint)}</label>" +
                        "<input type='text' id='$qid' name='$qid' value='${esc(value)}' maxlength='${Assignments.MAX_TEXT_CHARS}'$mode$described>" +
                        flag)
                }
            }
        }
        parts.append("<input type='hidden' name='first' id='first' value=''>")
        parts.append("<section id='review' aria-label='${esc(w("form.review_title"))}'><h2>${esc(w("form.review_title"))}</h2>" +
            "<p class='meta'>${esc(w("form.review_text"))}</p><div id='reviewlist'></div></section>")
        parts.append("<div class='submitbar'><p class='meta' id='answered' aria-live='polite' " +
            "data-words='${esc(w("form.answered"))}'>${esc(count)}</p>" +
            "<button class='big' id='submit'>${esc(w("form.submit"))}</button><div class='stepnav'>" +
            "<button type='button' class='secondary' id='back'><span class='arr' aria-hidden='true'>←</span> ${esc(w("form.back"))}</button>" +
            "<button type='button' class='secondary' id='toreview'>${esc(w("form.review"))}</button>" +
            "<button type='button' id='next'><span id='nextlabel'>${esc(w("form.next"))}</span> <span class='arr' aria-hidden='true'>→</span></button>" +
            "</div></div></form>${scratchpad(w)}<script>${Html.ASSIGNMENT_JS}</script>")
        return parts.toString()
    }

    /**
     * The panel that goes with a question: its info, and the assignment's instructions. Students pull it
     * down on a phone; it sits beside the question on a wide screen. Open at the first question.
     */
    private fun questionInfo(a: Map<String, Any?>, q: Map<String, Any?>, i: Int, w: Strings): String {
        val hasInfo = truthy(q["info"])
        var text = if (hasInfo) "<p>${esc(q["info"])}</p>" else ""
        if (truthy(a["instructions"])) {
            text += (if (text.isNotEmpty()) "<h3>${esc(w("form.instructions"))}</h3>" else "") + "<p>${esc(a["instructions"])}</p>"
        }
        if (text.isEmpty()) return ""
        val label = if (hasInfo) w("form.info") else w("form.instructions")
        return "<details class='qinfo'${if (i == 1) " open" else ""}><summary>${esc(label)}</summary>" +
            "<div class='txt' tabindex='0'>$text</div></details>"
    }

    private fun openAssignment(req: Request): MutableMap<String, Any?>? =
        getAssignment(req.params["aid"] ?: "")?.takeIf { it["open"] == true }?.let { Assignments.localized(it, words(req).code) }

    fun assignmentForm(req: Request): Response {
        val w = words(req)
        val student = student(req) ?: return Response.redirect("/")
        val a = openAssignment(req) ?: return Response(page(req, w("open.not_title"),
            "<p>${esc(w("open.not_text"))}</p><p><a href='/'>${esc(w("open.back"))}</a></p>"), 404)
        if (a["mode"] != "practice") {
            val done = db.one("SELECT submitted_at FROM submissions WHERE assignment_id = ? AND student = ?", a["id"], student)
            if (done != null) {
                val body = "<div class='card'><p>${esc(w("open.already", "when" to w.whenText(done.double("submitted_at"))))}</p></div>" +
                    "<a class='button big' href='/results'>${esc(w("open.see_results"))}</a>" +
                    "<a class='button big secondary' href='/'>${esc(w("nav.back_home"))}</a>"
                return Response(page(req, a["title"].toString(), body))
            }
        }
        opened(student, a["id"].toString())
        return Response(page(req, a["title"].toString(), renderForm(req, a, student, emptyMap(), emptyMap())))
    }

    fun assignmentSubmit(req: Request): Response {
        val w = words(req)
        val student = student(req) ?: return Response.redirect("/")
        val a = openAssignment(req) ?: return Response.redirect("/")
        val raw = Assignments.questions(a).associate { it["id"].toString() to (req.form[it["id"].toString()] ?: "") }
        val (answers, errors) = Assignments.validateAnswers(a, raw)
        val unsure = Assignments.questions(a).map { it["id"].toString() }.filter { !req.form["unsure-$it"].isNullOrEmpty() }.toSet()
        if (errors.isNotEmpty()) {
            val kept = raw + unsure.associate { "unsure-$it" to "1" }
            return Response(page(req, a["title"].toString(), renderForm(req, a, student, kept, errors)), 400)
        }
        if (a["mode"] != "practice" &&
            db.one("SELECT 1 AS x FROM submissions WHERE assignment_id = ? AND student = ?", a["id"], student) != null) {
            return Response.redirect("/results")
        }
        val signals = Assignments.signalsOf(a, unsure, req.form["first"] ?: "", answers) +
            (if (w.code != school.code) mapOf("l" to w.code) else emptyMap())   // so the teacher knows which language they work in
        submit(a, student, answers, signals = signals)
        // Turned in: forget the draft the browser kept while the student worked.
        val clearDraft = "<div id='clear-draft' data-key='${esc(student + "|" + a["id"])}'></div><script>${Html.CLEAR_DRAFT_JS}</script>"
        if (a["mode"] == "practice") {
            val feedback = Assignments.practiceFeedback(a, answers)
            val items = Assignments.questions(a).joinToString("") { q ->
                val ok = feedback[q["id"].toString()]
                val mark = when (ok) {
                    true -> "<span class='good'>${esc(w("practice.correct"))}</span>"
                    false -> "<span class='bad'>${esc(w("practice.not_quite"))}</span>"
                    null -> "<span class='muted'>${esc(w("practice.teacher_reads"))}</span>"
                }
                "<li>${esc(q["prompt"])}: $mark</li>"
            }
            val right = feedback.values.count { it == true }
            val checked = feedback.values.count { it != null }
            val body = "<div class='card'><p class='grade'>${esc(w("practice.right", "right" to right, "checked" to checked))}</p>" +
                "<p class='meta'>${esc(w("practice.note"))}</p><ol>$items</ol></div>" +
                "<a class='button big' href='/a/${esc(a["id"])}'>${esc(w("practice.try_again"))}</a>" +
                "<a class='button big secondary' href='/'>${esc(w("nav.back_home"))}</a>" + clearDraft
            return Response(page(req, w("practice.title"), body))
        }
        if (look(req) == "young") {
            val stars = icon("star").repeat(3)
            val body = "<div class='done'><div class='bigstars' aria-hidden='true'>$stars</div></div><p class='lead'>${esc(w("submitted.done"))}</p>" +
                "<a class='button big' href='/results'>${esc(w("young.see_stars"))}</a>" +
                "<a class='button big secondary' href='/'>${esc(w("nav.back_home"))}</a>" + clearDraft
            return Response(page(req, w("young.well_done"), body))
        }
        val tick = icon("check")
        val body = "<div class='done'><span class='tick'>$tick</span><p class='grade'>${esc(w("submitted.done"))}</p></div>" +
            "<div class='card'><h2 style='margin-top:0'>${esc(w("submitted.where"))}</h2><ol class='steps'>" +
            "<li class='ok'><b>$tick</b><span>${esc(w("submitted.saved"))}</span></li>" +
            "<li><b>2</b><span>${esc(w("submitted.sent"))}${sendCountdown(w)}</span></li>" +
            "<li><b>3</b><span>${esc(w("results.acked"))}</span></li><li><b>4</b><span>${esc(w("submitted.marked"))}</span></li></ol>" +
            "<p class='meta'>${esc(w("submitted.later"))}</p></div>" +
            "<p class='meta'>${esc(w("submitted.no_wait"))} ${esc(w("submitted.safe"))}</p>" +
            "<a class='button big' href='/'>${esc(w("nav.back_home"))}</a>" + clearDraft
        return Response(page(req, w("submitted.title"), body))
    }

    /** Seconds until saved work goes out by radio; null when nothing is waiting to be batched. */
    fun nextSendIn(): Double? {
        val rows = db.all("SELECT submitted_at FROM submissions WHERE msg_id IS NULL ORDER BY submitted_at LIMIT ?", MAX_BATCH)
        if (rows.isEmpty()) return null
        if (rows.size >= MAX_BATCH) return 0.0
        return maxOf(0.0, (rows[0].double("submitted_at") ?: 0.0) + batchS - Messenger.now())
    }

    /** "It goes to your teacher in about 1:45.", counting down in the browser (COUNTDOWN_JS). */
    private fun sendCountdown(w: Strings): String {
        val left = nextSendIn()
        if (left == null || left < 1) return "<p class='sendin'>${esc(w("submitted.sending"))}</p>"
        val s = (left + 0.5).toLong()
        val words = w("submitted.sends_in", "time" to "{time}")
        val now = words.replace("{time}", "${s / 60}:${(s % 60).toString().padStart(2, '0')}")
        return "<p class='sendin' id='sendin' aria-live='polite' data-in='$s' data-words='${esc(words)}' " +
            "data-now='${esc(w("submitted.sending"))}'>${esc(now)}</p><script>${Html.COUNTDOWN_JS}</script>"
    }

    fun results(req: Request): Response {
        val w = words(req)
        val student = student(req) ?: return Response.redirect("/")
        val out = StringBuilder()
        val rows = db.all("SELECT * FROM submissions WHERE student = ? ORDER BY submitted_at DESC", student)
        if (look(req) == "young") return Response(page(req, w("young.stars"), resultsYoung(rows, w)))
        val teen = look(req) == "teen"
        rows.forEachIndexed { i, s ->
            val a: Map<String, Any?> = Assignments.localized(getAssignment(s.str("assignment_id")!!)
                ?: mapOf("title" to s.str("assignment_id"), "questions" to emptyList<Any?>()), w.code)
            val sent = when (messenger.messageStatus(s.long("msg_id"))) {
                "acked" -> "<span class='pill good'>${esc(w("results.acked"))}</span>"
                "pending" -> "<span class='pill warn'>${esc(w("results.pending"))}</span>"
                "failed" -> "<span class='pill bad'>${esc(w("results.failed"))}</span>"
                else -> "<span class='pill'>${esc(w("results.saved"))}</span>"
            }
            if (i == 0 && !teen) {
                out.append(newestResult(s, a, sent, w) + (if (rows.size > 1) "<h2>${esc(w("results.earlier"))}</h2>" else ""))
                return@forEachIndexed
            }
            val course = courseTitleOf(a, w)
            if (s["score"] == null) {
                out.append("<div class='row wait'><span class='grow'><strong>${esc(a["title"])}</strong>" +
                    "<span class='meta'>${esc(course)}</span></span><span class='end small'>$sent</span></div>")
                return@forEachIndexed
            }
            val complete = (s.long("complete") ?: 0L) != 0L
            val end = if (complete) "<span class='end'>${g(s.double("score"))} / ${g(s.double("max"))}</span>"
                else if (teen) "<span class='end small'>" +   // no big card for the newest here: how far the marking is, and where the work is
                    "${esc(w("results.so_far", "score" to g(s.double("score")), "max" to g(s.double("max"))))}<br>${esc(w("results.reading"))}<br>$sent</span>"
                else "<span class='end small'>${esc(w("results.reading"))}</span>"
            val meta = listOf(course, w.whenText(s.double("submitted_at"))).filter { it.isNotEmpty() }.joinToString(" · ")
            out.append("<div class='row${if (complete) "" else " wait"}'><span class='grow'><strong>${esc(a["title"])}</strong>" +
                "<span class='meta'>${esc(meta)}</span></span>$end</div>" + teacherNotes(a, s, w))
        }
        val records = myRecords(student)
        if (teen) {
            val body = courseTiles(records, w, markedOnly = true) + toReview(rows, w) +
                (if (out.isNotEmpty()) "<h2>${esc(w("results.all"))}</h2>$out" else "<p class='muted'>${esc(w("results.nothing"))}</p>")
            return Response(page(req, w("results.title"), body))
        }
        val progress = StringBuilder()
        for (course in courseTree()) {
            val summary = Comprehension.summarize(records, course)
            if (summary.answered == 0) continue
            val units = summary.units.filter { it.answered > 0 }
                .joinToString("") { "<li>${esc(w("course.unit", "n" to it.number, "title" to it.title))} ${level(it.level, it.score, w)}</li>" }
            progress.append("<div class='card'><h3 style='margin:0'>${esc(title(course, w))} " +
                "${level(summary.level, summary.score, w)}</h3><ul class='units'>$units</ul></div>")
        }
        val body = (if (out.isNotEmpty()) out.toString() else "<p class='muted'>${esc(w("results.nothing"))}</p>") +
            (if (progress.isNotEmpty()) "<h2>${esc(w("results.how"))}</h2>$progress" else "")
        return Response(page(req, w("results.title"), body))
    }

    /** What the student answered, as words. */
    private fun given(q: Map<String, Any?>, answers: Map<String, Any?>): String {
        val said = answers[q["id"].toString()]?.toString() ?: ""
        val options = q["options"].asList() ?: emptyList()
        return if (q["type"] == "choice" && said.isNotEmpty() && said.all { it.isDigit() } && (said.toIntOrNull() ?: Int.MAX_VALUE) < options.size)
            options[said.toInt()].toString() else said.ifEmpty { "—" }
    }

    /** The right answer to a question this student missed: from the hub after the due date, or the practice key. */
    private fun rightAnswer(a: Map<String, Any?>, q: Map<String, Any?>, s: Row, w: Strings): String {
        val sent = s.str("right_answers")?.let { try { Json.parse(it).asMap() } catch (e: Exception) { null } } ?: emptyMap()
        val text = sent[q["id"].toString()]?.toString()?.ifEmpty { null } ?: if (a["mode"] == "practice") Assignments.keyText(q) else ""
        return if (text.isEmpty()) "" else "<span class='rightans'>${esc(w("results.answer", "answer" to text))}</span>"
    }

    /** Short notes the teacher wrote back, with the question each is about. */
    private fun teacherNotes(a: Map<String, Any?>, s: Row, w: Strings): String {
        val notes = s.str("notes")?.let { try { Json.parse(it).asMap() } catch (e: Exception) { null } } ?: return ""
        val out = StringBuilder()
        Assignments.questions(a).forEachIndexed { i, q ->
            val text = notes[q["id"].toString()] as? String ?: ""
            if (text.isNotBlank()) out.append("<div class='tnote'><span class='meta'>${i + 1}. ${esc(q["prompt"])}</span>" +
                "<span>${esc(w("results.note", "note" to text))}</span></div>")
        }
        return out.toString()
    }

    /** Ages 5 to 7: stars out of three for each piece of work. */
    private fun resultsYoung(rows: List<Row>, w: Strings): String {
        val out = StringBuilder()
        val seen = HashSet<String>()
        for (s in rows) {
            val aid = s.str("assignment_id")!!
            if (!seen.add(aid)) continue
            val a: Map<String, Any?> = Assignments.localized(getAssignment(aid) ?: mapOf("id" to "", "title" to aid, "questions" to emptyList<Any?>()), w.code)
            val got = starsOf(s)
            val stars = if (got == null) "<p class='meta'>${esc(if (s["score"] != null) w("results.reading") else w("results.not_graded"))}</p>"
                else "<div class='stars' role='img' aria-label='$got / 3'>" +
                    (0 until 3).joinToString("") { icon("star", if (it < got) "on" else "") } + "</div>"
            val again = if (a["mode"] == "practice" && a["open"] == true && (got ?: 0) < 3)
                "<a class='button' href='/a/${esc(a["id"])}'>${icon("redo")}${esc(w("young.more"))}</a>" else ""
            out.append("<div class='ycard col'><div class='top'>${sub(a)}<span class='grow'>${esc(a["title"])}</span></div>$stars${teacherNotes(a, s, w)}$again</div>")
        }
        return if (out.isNotEmpty()) out.toString() else "<p class='muted'>${esc(w("results.nothing"))}</p>"
    }

    /** Ages 12 and up: the latest questions answered wrong, up to five. */
    private fun toReview(rows: List<Row>, w: Strings): String {
        val out = StringBuilder()
        var count = 0
        val seen = HashSet<String>()
        for (s in rows) {
            val aid = s.str("assignment_id")!!
            if (aid in seen || s["score"] == null || count >= 5) continue
            seen.add(aid)
            val a = Assignments.localized(getAssignment(aid) ?: continue, w.code)
            val detail = Json.parse(s.str("detail") ?: "{}").asMap() ?: emptyMap()
            val answers = Json.parse(s.str("answers") ?: "{}").asMap() ?: emptyMap()
            Assignments.questions(a).forEachIndexed { n, q ->
                if ((detail[q["id"].toString()] as? Number)?.toDouble() == 0.0 && count < 5) {
                    count++
                    out.append("<div class='missed'><span class='meta'>${esc(a["title"])} · ${n + 1}</span><strong>${esc(q["prompt"])}</strong>" +
                        "<span class='meta'>${esc(w("results.you_said", "answer" to given(q, answers)))}</span>${rightAnswer(a, q, s, w)}</div>")
                }
            }
        }
        return if (out.isEmpty()) "" else "<h2>${esc(w("results.to_review"))}</h2>$out"
    }

    private fun courseTitleOf(a: Map<String, Any?>, w: Strings): String {
        val course = courseTree().firstOrNull { it["id"] != "" && it["id"] == a["course"] }
        return if (course != null) title(course, w) else (a["course_title"]?.toString() ?: "")
    }

    /** The latest work as a big card: how many right, a square per question, and the first one missed. */
    private fun newestResult(s: Row, a: Map<String, Any?>, sent: String, w: Strings): String {
        val detail = Json.parse(s.str("detail") ?: "{}").asMap() ?: emptyMap()
        val answers = Json.parse(s.str("answers") ?: "{}").asMap() ?: emptyMap()
        val graded = s["score"] != null
        val squares = StringBuilder()
        var right = 0
        var missed = ""
        val qs = Assignments.questions(a)
        qs.forEachIndexed { idx, q ->
            val score = if (graded) (detail[q["id"].toString()] as? Number)?.toDouble() else null
            val points = (q["points"] as? Number)?.toDouble()?.takeIf { it > 0 } ?: 1.0
            val (cls, mark) = when {
                score == null -> "wait" to "…"
                score >= points -> { right++; "right" to icon("check") }
                score > 0 -> "part" to "½"
                else -> {
                    if (missed.isEmpty()) {
                        missed = "<div class='missed'><strong>${idx + 1}. ${esc(q["prompt"])}</strong>" +
                            "<span class='meta'>${esc(w("results.you_said", "answer" to given(q, answers)))}</span>${rightAnswer(a, q, s, w)}</div>"
                    }
                    "wrong" to icon("cross")
                }
            }
            squares.append("<span class='$cls' aria-hidden='true'>$mark</span>")
        }
        val total = qs.size
        val headline = when {
            !graded -> "<p class='meta'>${esc(w("results.not_graded"))}</p>"
            (s.long("complete") ?: 0L) != 0L -> "<p class='bigscore${if (total > 0 && right * 10 >= total * 7) " good" else ""}'>" +
                "${esc(w("results.right", "right" to right, "total" to total))}</p>" +
                "<p class='meta'>${g(s.double("score"))} / ${g(s.double("max"))}</p>"
            else -> "<p class='bigscore'>${esc(w("results.so_far", "score" to g(s.double("score")), "max" to g(s.double("max"))))}</p>" +
                "<p class='meta'>${esc(w("results.still_grading"))}</p>"
        }
        val grid = if (squares.isNotEmpty() && graded)
            "<div class='squares' role='img' aria-label='${esc(w("results.right", "right" to right, "total" to total))}'>$squares</div>" else ""
        val again = if (a["mode"] == "practice" && a["id"] != null && a["open"] == true)
            "<a class='button big' href='/a/${esc(a["id"])}'>${esc(w("results.try_again"))}</a>" else ""
        val meta = listOf(courseTitleOf(a, w), w("results.turned_in", "when" to w.whenText(s.double("submitted_at"))))
            .filter { it.isNotEmpty() }.joinToString(" · ")
        return "<section class='card' aria-labelledby='newest'><span class='new' id='newest'>${esc(w("results.newest"))}</span>" +
            "<h2 style='margin:10px 0 2px;color:var(--ink)'>${esc(a["title"])}</h2>" +
            "<p class='meta'>${esc(meta)}</p>$headline$grid$missed${teacherNotes(a, s, w)}$sent$again</section>"
    }

    /** Books and lessons on the card. Whole courses (entries with a "shelf", such as a Kolibri channel) get
     *  one card each here and a page of their own, so a thousand videos don't fill this one. */
    fun library(req: Request): Response {
        val w = words(req)
        val query = (req.query["q"] ?: "").trim().lowercase()
        val shelf = req.query["shelf"] ?: ""
        val part = req.query["c"] ?: ""
        var items: List<Map<String, Any?>> = libraryItems()
        val body = StringBuilder("<form method='get' action='/library' class='search'>" +
            "<label class='vh' for='q'>${esc(w("library.search_label"))}</label>${icon("search")}" +
            "<input type='text' id='q' name='q' value='${esc(query)}' placeholder='${esc(w("library.search_label"))}'><button>${esc(w("library.search"))}</button></form>")
        if (query.isNotEmpty()) {
            items = items.filter { query in "${it["title"] ?: ""} ${it["collection"] ?: ""} ${it["shelf"] ?: ""}".lowercase() }
            body.append("<p class='meta'>${esc(w("library.showing", "q" to query))} <a href='/library'>${esc(w("library.show_all"))}</a></p>")
            body.append(libraryGroups(w, items))
        } else if (shelf.isNotEmpty()) {
            items = items.filter { it["shelf"] == shelf }
            val parts = items.groupBy { it["collection"]?.toString()?.ifEmpty { null } ?: w("library.other") }
            val link = "/library?shelf=" + quote(shelf)
            val chosen = parts[part]
            if (chosen != null) {
                body.append("<p class='meta'><a href='/library'>${esc(w("library.title"))}</a> › " +
                    "<a href='$link'>${esc(shelf)}</a></p>" + libraryGroups(w, chosen))
            } else {
                body.append("<p class='meta'><a href='/library'>${esc(w("library.title"))}</a></p><h2>${esc(shelf)}</h2>")
                if (items.size <= SHELF_FLAT || parts.size == 1) body.append(libraryGroups(w, items))
                else for ((name, entries) in parts) {
                    body.append(libraryCard("$link&amp;c=${quote(name)}", name, w("library.lessons", "n" to entries.size)))
                }
            }
        } else {
            body.append(keepReading(student(req) ?: "", w))
            val shelves = items.mapNotNull { it["shelf"]?.toString()?.ifEmpty { null } }.groupingBy { it }.eachCount().toSortedMap()
            if (shelves.isNotEmpty()) {
                body.append("<h2>${esc(w("library.courses"))}</h2>")
                for ((name, n) in shelves) body.append(libraryCard("/library?shelf=" + quote(name), name, w("library.lessons", "n" to n)))
            }
            body.append(libraryGroups(w, items.filter { it["shelf"]?.toString().isNullOrEmpty() }, byName = true))
        }
        if (items.isEmpty()) body.append("<p class='muted'>${esc(w("library.nothing"))}</p>")
        body.append("<p class='meta'><a href='/about'>${esc(w("about.title"))}</a></p>")
        return Response(page(req, w("library.title"), body.toString()))
    }

    /** The last thing this student opened in the library. */
    private fun keepReading(student: String, w: Strings): String {
        val row = (if (student.isEmpty()) null else db.one("SELECT * FROM reading WHERE student = ?", student)) ?: return ""
        val file = row.str("file")!!
        if (!File(libraryDir, file).isFile) return ""
        val href = if (file.endsWith(".zip")) "/library/open?f=" + quote(file, "") else "/library/files/" + quote(file)
        return "<a class='keepread' href='$href'><span class='sub book'>${icon("book")}</span>" +
            "<span class='grow'><small>${esc(w("library.keep"))}</small><strong>${esc(row.str("title"))}</strong>" +
            "<span>${esc(row.str("collection"))}</span></span>${icon("chev", "arr")}</a>"
    }

    private fun rememberReading(student: String, rel: String) {
        val item = libraryItems().firstOrNull { it["file"].toString().replace(File.separatorChar, '/') == rel } ?: return
        val from = item["shelf"]?.toString()?.ifEmpty { null } ?: item["collection"]?.toString() ?: ""
        db.execute("INSERT OR REPLACE INTO reading (student, file, title, collection, at) VALUES (?, ?, ?, ?, ?)",
            student, rel, item["title"].toString(), from, Messenger.now())
    }

    private fun libraryCard(href: String, title: String, meta: String, kind: String = "shelf"): String {
        val name = when (kind) {
            "mp4", "webm" -> "video"; "mp3" -> "speaker"; "html", "htm" -> "page"
            "png", "jpg", "jpeg", "svg" -> "picture"; "shelf", "zim" -> "books"; "zip" -> "puzzle"; "ggb" -> "maths"; else -> "book"
        }
        return "<a class='card item' href='$href'><span class='sub'>${icon(name)}</span><span class='grow'>" +
            "<strong>${esc(title)}</strong><span class='meta' style='display:block'>${esc(meta)}</span></span></a>"
    }

    /** Items under a heading per collection, in card order (the course's order) or by name. */
    private fun libraryGroups(w: Strings, items: List<Map<String, Any?>>, byName: Boolean = false): String {
        val groups = items.groupBy { it["collection"]?.toString()?.ifEmpty { null } ?: w("library.other") }
        val out = StringBuilder()
        for ((group, entries) in if (byName) groups.toSortedMap() else groups) {
            out.append("<h2>${esc(group)}</h2>")
            for (item in entries) {
                val file = item["file"].toString()
                var href = "/library/files/" + quote(file.replace(File.separatorChar, '/'))
                var note = ""
                if (item["kind"] == "zim") {
                    val book = File(file).name.substringBeforeLast('.')
                    if (kiwixUrl.isNotEmpty()) href = "${esc(kiwixUrl)}/content/${quote(book)}"
                    else note = " · " + w("library.kiwix")
                }
                if (item["kind"] == "zip" || item["kind"] == "ggb") href = "/library/open?f=" + quote(file.replace(File.separatorChar, '/'), "")
                val credit = item["credit"]?.toString() ?: ""
                if (credit.isNotEmpty() && item["kind"] != "zip" && item["kind"] != "ggb") note += " · $credit"   // those show it beside the activity
                val kind = KIND_KEYS[item["kind"]?.toString() ?: ""]?.let { w(it) } ?: (item["kind"]?.toString() ?: "")
                out.append(libraryCard(href, item["title"].toString(),
                    "$kind · ${Html.size((item["size"] as? Number)?.toDouble() ?: 0.0)}$note", item["kind"]?.toString() ?: ""))
            }
        }
        return out.toString()
    }

    fun libraryFile(req: Request): Response {
        val root = libraryDir.canonicalFile
        val target = File(root, req.params["path"] ?: "").canonicalFile
        if (!target.path.startsWith(root.path + File.separator) || !target.isFile) {
            val w = words(req)
            return Response(page(req, w("notfound.title"), "<p>${esc(w("library.no_file"))}</p>"), 404)
        }
        val who = student(req)
        if (who != null && (req.headers["range"] ?: "") in setOf("", "bytes=0-")) {   // opened, not a video player's later pieces
            rememberReading(who, target.relativeTo(root).path.replace(File.separatorChar, '/'))
        }
        return Response.file(target)
    }

    // -- status and admin -----------------------------------------------------

    /** Course-pack changes come from the phone itself, or anyone with the PIN. */
    private fun isAdmin(req: Request): Boolean = req.isLocal || (adminPin.isNotEmpty() && req.form["pin"] == adminPin)

    fun status(req: Request, notice: String = ""): Response {
        val w = words(req)
        val q = messenger.status()
        val held = db.one("SELECT COUNT(*) AS n, SUM(open) AS o FROM assignments")
        val waiting = db.one("SELECT COUNT(*) AS n FROM submissions WHERE msg_id IS NULL")?.long("n") ?: 0
        fun pinField(id: String) = if (adminPin.isEmpty()) "" else
            "<label for='$id'>${esc(w("status.pin"))}</label><input type='text' id='$id' name='pin' inputmode='numeric'>"
        val names = languages.values.joinToString(" · ") { it.name }
        val body = (if (notice.isNotEmpty()) "<p class='good' role='status'>${esc(notice)}</p>" else "") +
            "<div class='grid'>" +
            "<div class='card'><div class='stat'>${held?.long("n") ?: 0}</div>${esc(w("status.stored", "n" to (held?.long("o") ?: 0)))}</div>" +
            "<div class='card'><div class='stat'>$waiting</div>${esc(w("status.waiting"))}</div>" +
            "<div class='card'><div class='stat'>${q["pending"]}</div>${esc(w("status.radio"))}" +
            "<div class='muted'>${esc(w("status.packets", "n" to q["frames_sent"]))}</div></div>" +
            "<div class='card'><div class='stat'>${Html.size(dataDir.usableSpace.toDouble())}</div>${esc(w("status.free"))}</div></div>" +
            "<p>${esc(w("status.language", "name" to names))}</p>" +
            "<h2>${esc(w("status.copy_heading"))}</h2>" +
            "<form method='post' action='/status/sync' class='card'>" +
            "<p>${esc(w("status.copy_text"))}</p>" +
            "<label for='hub_url'>${esc(w("status.hub_address"))}</label>" +
            "<input type='text' id='hub_url' name='hub_url' placeholder='http://192.168.1.20:8000' dir='ltr' required>" +
            "${pinField("pin-sync")}<button>${esc(w("status.copy_button"))}</button></form>" +
            "<form method='post' action='/status/reload' class='card'>" +
            "<p>${esc(w("status.reload_text"))}</p>" +
            "${pinField("pin-reload")}<button class='secondary'>${esc(w("status.reload_button"))}</button></form>" +
            "<p class='muted'>${esc(w("status.footer", "hub" to hubId, "node" to nodeId, "dir" to dataDir.path))}</p>"
        return Response(page(req, w("status.title"), body))
    }

    fun statusSync(req: Request): Response {
        if (!isAdmin(req)) return status(req, words(req)("status.only_teacher"))
        var url = (req.form["hub_url"] ?: "").trim()
        if (!url.startsWith("http://") && !url.startsWith("https://")) url = "http://$url"
        val count = try {
            syncFromHub(url)
        } catch (e: Exception) {
            return status(req, words(req)("status.cant_reach", "url" to url, "error" to (e.message ?: e.javaClass.simpleName)))
        }
        return status(req, words(req)("status.copied", "n" to count))
    }

    fun statusReload(req: Request): Response {
        if (!isAdmin(req)) return status(req, words(req)("status.only_teacher"))
        val count = loadPackFolder()
        sayHello()
        return status(req, words(req)("status.loaded", "n" to count))
    }

    companion object {
        const val VERSION = "0.1.0-android"
        const val MAX_OPEN_S = 6 * 3600.0
        const val MAX_BATCH = 20
        const val SHELF_FLAT = 40   // a course with more lessons than this lists its topics first
        private val LOOKS = mapOf<Any?, String>("early" to "young", "primary" to "mid", "lower" to "teen", "upper" to "teen", "adult" to "teen")

        private val SUBJECTS = listOf(Regex("math|matem|mathe|hisab|number") to "maths",
            Regex("scien|cienc|wissen|sayansi|plant|natur") to "plant",
            Regex("read|writ|liter|lese|lectur|leitura|kusoma|story") to "book")

        /** The picture for an assignment's course. */
        fun subjectIcon(a: Map<String, Any?>): String {
            val text = "${a["course"] ?: ""} ${a["course_title"] ?: ""} ${a["subject"] ?: ""}".lowercase()
            return SUBJECTS.firstOrNull { it.first.containsMatchIn(text) }?.second ?: "pencil"
        }

        /** 0 to 3 stars for marked work; null while it waits for a mark. */
        fun starsOf(s: Row): Int? {
            val score = s.double("score") ?: return null
            val max = s.double("max") ?: 0.0
            if ((s.long("complete") ?: 0L) == 0L || max == 0.0) return null
            return (3 * score / max + 0.5).toInt()
        }
        val VIEWABLE = setOf(".html", ".htm", ".txt", ".pdf", ".epub", ".png", ".jpg", ".jpeg", ".svg", ".mp3", ".mp4", ".webm", ".zim")
        /** Library file types, as the key of the word a student knows. */
        val KIND_KEYS = mapOf("html" to "kind.page", "htm" to "kind.page", "txt" to "kind.text", "pdf" to "kind.pdf",
            "epub" to "kind.book", "png" to "kind.picture", "jpg" to "kind.picture", "jpeg" to "kind.picture",
            "svg" to "kind.picture", "mp3" to "kind.audio", "mp4" to "kind.video", "webm" to "kind.video", "zim" to "kind.encyclopedia", "zip" to "kind.app", "ggb" to "kind.tool")
        val STARTER_FILES = listOf("fractions.html", "photosynthesis.html", "using-this-library.html")

        const val SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY, version TEXT NOT NULL, data TEXT NOT NULL,
    open INTEGER NOT NULL DEFAULT 0, due TEXT NOT NULL DEFAULT '', received REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
    sub_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL, version TEXT, student TEXT NOT NULL,
    answers TEXT NOT NULL, submitted_at REAL NOT NULL, msg_id INTEGER,
    score REAL, max REAL, complete INTEGER, detail TEXT, duration REAL
);
CREATE TABLE IF NOT EXISTS courses (id TEXT PRIMARY KEY, data TEXT NOT NULL, position INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS opens (
    student TEXT NOT NULL, aid TEXT NOT NULL, opened_at REAL NOT NULL, PRIMARY KEY (student, aid)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS newcomers (name TEXT PRIMARY KEY, pic TEXT NOT NULL, look TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reading (
    student TEXT PRIMARY KEY, file TEXT NOT NULL, title TEXT NOT NULL, collection TEXT NOT NULL, at REAL NOT NULL
)"""

        /** Python's str.capitalize(): first letter upper, the rest lower. */
        fun capitalize(s: String) = if (s.isEmpty()) s else s.substring(0, 1).uppercase() + s.substring(1).lowercase()
    }
}
