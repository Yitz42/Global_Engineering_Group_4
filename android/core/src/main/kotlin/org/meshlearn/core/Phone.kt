package org.meshlearn.core

import org.meshlearn.core.Html.esc
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

    init {
        dataDir.mkdirs()
        installStarterLibrary()
        db = makeDb(File(dataDir, "phone.db"), SCHEMA)
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

    // -- language -------------------------------------------------------------

    private fun loadLanguage() {
        val data = db.one("SELECT value FROM settings WHERE key = 'language'")?.str("value")?.let {
            try { Json.parse(it).asMap() } catch (e: Exception) { null }
        } ?: emptyMap()
        val first = Strings(data["school"].asMap())
        val all = linkedMapOf(first.code to first)
        val second = data["second"].asMap()
        val secondCode = second?.get("code") as? String
        if (second != null && !secondCode.isNullOrEmpty() && secondCode != first.code) {
            val other = Strings(second)
            all[other.code] = other
        }
        school = first
        languages = all
    }

    /** Keep the school's words from a course pack. Returns true if they changed. */
    fun importLanguage(data: Any?): Boolean {
        val m = data.asMap() ?: return false
        val first = m["school"].asMap() ?: return false
        val value = Json.dumps(mapOf("school" to first, "second" to m["second"].asMap()), sortKeys = true)
        if (db.one("SELECT value FROM settings WHERE key = 'language'")?.str("value") == value) return false
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('language', ?)", value)
        loadLanguage()
        return true
    }

    /** The student's language: the one they switched to, else the school's. */
    fun words(req: Request): Strings = languages[req.cookies["lang"] ?: ""] ?: school

    // -- assignments ----------------------------------------------------------

    /** Store a packed assignment from the radio or a course pack. Returns true if new or changed. */
    fun importAssignment(packed: Map<String, Any?>, open: Boolean? = null, due: String? = null): Boolean {
        val assignment = Assignments.unpack(packed)
        val aid = assignment["id"]?.toString() ?: ""
        val version = (packed["v"] as? String)?.takeIf { it.isNotEmpty() } ?: Assignments.versionOf(assignment)
        if (aid.isEmpty() || !truthy(assignment["questions"])) return false
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
        for (f in (packDir.listFiles() ?: emptyArray()).sortedBy { it.name }) {
            if (!f.name.endsWith(".json") || f.name == "courses.json" || f.name == "language.json") continue
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
            }
            else -> log.info("phone ignoring ${message.type} message from ${message.src}")
        }
    }

    /** Remember when a student opened an assignment, to time their work. */
    fun opened(student: String, aid: String) {
        db.execute("INSERT OR REPLACE INTO opens (student, aid, opened_at) VALUES (?, ?, ?)", student, aid, Messenger.now())
    }

    /** Save a submission now; the batcher sends it with others shortly. */
    fun submit(assignment: Map<String, Any?>, student: String, answers: Map<String, String>, duration: Double? = null): String {
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
        db.execute("INSERT INTO submissions (sub_id, assignment_id, version, student, answers, submitted_at, duration) " +
            "VALUES (?, ?, ?, ?, ?, ?, ?)", subId, aid, assignment["version"], student, Json.dumps(answers, spaced = true),
            Messenger.now(), dur)
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
        val who = req.cookies["student"]
        val section = when {
            req.path.startsWith("/library") -> "/library"
            req.path.startsWith("/results") -> "/results"
            req.path == "/" || req.path.startsWith("/c/") || req.path.startsWith("/a/") -> "/"
            else -> ""
        }
        val tabs = mutableListOf("/" to w("tab.home"), "/library" to w("tab.library"))
        if (!who.isNullOrEmpty()) tabs.add("/results" to w("tab.results"))
        return "<nav class='tabs'>" + tabs.joinToString("") { (href, label) ->
            "<a href='$href'${if (href == section) " aria-current='page'" else ""}>${esc(label)}</a>"
        } + "</nav>"
    }

    /** With a second language, a button in the header switches to the other one. */
    private fun languageSwitch(req: Request, w: Strings): String {
        val other = languages.values.firstOrNull { it.code != w.code } ?: return ""
        val back = req.path + (if (req.query.isEmpty()) "" else
            "?" + req.query.entries.joinToString("&") { (k, v) -> quote(k, "") + "=" + quote(v, "") })
        return "<form method='post' action='/lang' class='lang'><input type='hidden' name='to' value='${esc(other.code)}'>" +
            "<input type='hidden' name='back' value='${esc(back)}'>" +
            "<button title='${esc(w("lang.switch"))}' lang='${esc(other.tag)}' dir='${other.dir}'>${esc(other.name)}</button></form>"
    }

    private fun page(req: Request, title: String, body: String): String {
        val w = words(req)
        val who = req.cookies["student"]
        val site = "MeshLearn · $name"
        val html = Html.page(title, body, languageSwitch(req, w) + nav(req, w), site = site, extraCss = Html.STUDENT_CSS,
            lang = w.tag, dir = w.dir)
        // The student's name sits on the right of the header line, so they can see who is signed in.
        return if (who.isNullOrEmpty()) html
        else html.replaceFirst("<header><strong>${esc(site)}</strong>",
            "<header><strong><span>${esc(site)}</span><span class='who'>${esc(who)}</span></strong>")
    }

    private fun routes() {
        app.add("GET", "/", ::home)
        app.add("POST", "/login", ::login)
        app.add("POST", "/logout", ::logout)
        app.add("POST", "/lang", ::switchLanguage)
        app.add("GET", "/c/<cid>", ::coursePage)
        app.add("GET", "/a/<aid>", ::assignmentForm)
        app.add("POST", "/a/<aid>", ::assignmentSubmit)
        app.add("GET", "/results", ::results)
        app.add("GET", "/library", ::library)
        app.add("GET", "/library/files/<path:path>", ::libraryFile)
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

    private fun assignmentsIn(courseId: String, openOnly: Boolean = true): List<MutableMap<String, Any?>> {
        val rows = db.all("SELECT id FROM assignments" + (if (openOnly) " WHERE open = 1" else "") +
            " ORDER BY due = '', due, received DESC")
        val known = courseTree().map { it["id"].toString() }.toSet()
        return rows.mapNotNull { getAssignment(it.str("id")!!) }.filter { a ->
            val c = a["course"]?.toString() ?: ""
            (if (c in known) c else "") == courseId
        }
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

    private fun student(req: Request): String? = req.cookies["student"]?.takeIf { it.isNotEmpty() }

    fun home(req: Request): Response {
        val w = words(req)
        val student = student(req)
        if (student == null) {
            val link = "<a href='/library'>${esc(w("welcome.library"))}</a>"
            val body = "<p class='lead'>${esc(w("welcome.lead"))}</p>" +
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
            val openItems = assignmentsIn(course["id"].toString())
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
        val body = homeTop(todo, mine, w) + "<h2>${esc(w("home.courses"))}</h2>" +
            (if (cards.isNotEmpty()) "<div class='grid'>$cards</div>" else "<p class='muted'>${esc(w("home.no_courses"))}</p>") +
            "<form method='post' action='/logout'><button class='secondary'>${esc(w("home.change_name"))}</button></form>"
        return Response(page(req, w("home.title", "name" to student), body))
    }

    /** How much is done, the one thing to do next as a big card, then the rest (Ages 8 to 11 design). */
    private fun homeTop(todo: List<Pair<Map<String, Any?>, String>>, mine: Map<String, Row>, w: Strings): String {
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
            return out.toString() + "<div class='card'><p class='grade good' style='margin-top:0'>${esc(w("home.all_done"))}</p>" +
                "<p>${esc(w("home.all_done_text"))}</p>" +
                "<a class='button secondary' href='/library'>${esc(w("tab.library"))}</a></div>"
        }
        val (a, courseTitle) = todo[0]
        val practice = a["mode"] == "practice"
        val qs = Assignments.questions(a)
        val meta = listOf(courseTitle, w.n("card.questions", qs.size), Html.minutesFor(qs, w)).filter { it.isNotEmpty() }.joinToString(" · ")
        val dueText = Html.dueLabel(a["due"]?.toString(), turnedIn = false, words = w).first
        out.append("<section class='next' aria-labelledby='next-label'><span class='label' id='next-label'>${esc(w("home.next"))}</span>" +
            "<h2>${esc(a["title"])}</h2><p class='meta'>${esc(meta)}</p>" +
            (if (dueText.isNotEmpty()) "<p class='due'>${esc(dueText)}</p>" else "") +
            "<a class='button' href='/a/${esc(a["id"])}'>${esc(if (practice) w("card.practice") else w("card.start"))} →</a></section>")
        if (todo.size > 1) {
            out.append("<h2>${esc(w("home.coming_up"))}</h2>")
            for ((b, course) in todo.subList(1, minOf(todo.size, 5))) {
                val due = Html.dueLabel(b["due"]?.toString(), turnedIn = false, words = w).first
                val m = listOf(course, due.ifEmpty { if (b["mode"] == "practice") w("card.practice_tag") else "" })
                    .filter { it.isNotEmpty() }.joinToString(" · ")
                out.append("<a class='row' href='/a/${esc(b["id"])}'><span class='grow'><strong>${esc(b["title"])}</strong>" +
                    "<span class='meta'>${esc(m)}</span></span><span class='chev' aria-hidden='true'>›</span></a>")
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
        val items = assignmentsIn(cid)
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

    fun login(req: Request): Response {
        val n = (req.form["student"] ?: "").trim().split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ").take(40)
        if (n.isEmpty()) return Response.redirect("/")
        return Response.redirect("/").setCookie("student", n)
    }

    fun logout(req: Request): Response = Response.redirect("/").setCookie("student", "", maxAge = 0)

    fun switchLanguage(req: Request): Response {
        var back = req.form["back"] ?: "/"
        if (!back.startsWith("/") || back.startsWith("//")) back = "/"
        val code = req.form["to"] ?: ""
        val response = Response.redirect(back)
        return if (code in languages) response.setCookie("lang", code) else response
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
        if (truthy(a["instructions"])) parts.append("<div class='card'><p>${esc(a["instructions"])}</p></div>")
        parts.append("<p class='meta'>${esc(w("form.meta", "count" to count, "minutes" to Html.minutesFor(qs, w)))}</p>")
        if (errors.isNotEmpty()) parts.append("<p class='error' role='alert'>${esc(w.n("form.fix", errors.size))}</p>")
        parts.append("<form id='aform' method='post' action='/a/${esc(a["id"])}' data-key='${esc(student + "|" + a["id"])}' novalidate>")
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
                "${esc(q["prompt"])}</legend>"
            val cls = if (err != null) "q bad" else "q"
            when (q["type"]) {
                "choice" -> {
                    val opts = (q["options"].asList() ?: emptyList()).mapIndexed { j, opt ->
                        val on = value == j.toString()
                        "<label class='option${if (on) " on" else ""}'><input type='radio' name='$qid' value='$j'" +
                            "${if (on) " checked" else ""}$described> ${esc(opt)}</label>"
                    }.joinToString("")
                    parts.append("<fieldset class='$cls'>$legend$errHtml$opts</fieldset>")
                }
                "long" -> {
                    val limit = (q["max_chars"] as? Number)?.toLong() ?: 600L
                    parts.append("<fieldset class='$cls'>$legend$errHtml" +
                        "<label for='$qid'>${esc(w("form.long", "n" to limit))}</label>" +
                        "<textarea id='$qid' name='$qid' maxlength='$limit'$described>${esc(value)}</textarea>" +
                        "<p class='counter' id='$qid-count' aria-live='polite'>${value.length} / $limit</p></fieldset>")
                }
                else -> {
                    val number = q["type"] == "number"
                    val mode = if (number) " inputmode='decimal'" else ""
                    val hint = if (number) w("form.number") else w("form.short")
                    parts.append("<fieldset class='$cls'>$legend$errHtml" +
                        "<label for='$qid'>${esc(hint)}</label>" +
                        "<input type='text' id='$qid' name='$qid' value='${esc(value)}' maxlength='${Assignments.MAX_TEXT_CHARS}'$mode$described>" +
                        "</fieldset>")
                }
            }
        }
        parts.append("<div class='submitbar'><p class='meta' id='answered' aria-live='polite' " +
            "data-words='${esc(w("form.answered"))}'>${esc(count)}</p>" +
            "<button class='big'>${esc(w("form.submit"))}</button></div></form><script>${Html.ASSIGNMENT_JS}</script>")
        return parts.toString()
    }

    private fun openAssignment(req: Request): MutableMap<String, Any?>? =
        getAssignment(req.params["aid"] ?: "")?.takeIf { it["open"] == true }

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
        if (errors.isNotEmpty()) return Response(page(req, a["title"].toString(), renderForm(req, a, student, raw, errors)), 400)
        if (a["mode"] != "practice" &&
            db.one("SELECT 1 AS x FROM submissions WHERE assignment_id = ? AND student = ?", a["id"], student) != null) {
            return Response.redirect("/results")
        }
        submit(a, student, answers)
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
        val body = "<div class='card'><p class='good grade'>${esc(w("submitted.done"))}</p>" +
            "${sendCountdown(w)}<p>${esc(w("submitted.no_wait"))}</p>" +
            "<p class='meta'>${esc(w("submitted.later"))}</p></div>" +
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
        rows.forEachIndexed { i, s ->
            val a: Map<String, Any?> = getAssignment(s.str("assignment_id")!!)
                ?: mapOf("title" to s.str("assignment_id"), "questions" to emptyList<Any?>())
            val sent = when (messenger.messageStatus(s.long("msg_id"))) {
                "acked" -> "<span class='pill good'>${esc(w("results.acked"))}</span>"
                "pending" -> "<span class='pill warn'>${esc(w("results.pending"))}</span>"
                "failed" -> "<span class='pill bad'>${esc(w("results.failed"))}</span>"
                else -> "<span class='pill'>${esc(w("results.saved"))}</span>"
            }
            if (i == 0) {
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
                else "<span class='end small'>${esc(w("results.reading"))}</span>"
            val meta = listOf(course, w.whenText(s.double("submitted_at"))).filter { it.isNotEmpty() }.joinToString(" · ")
            out.append("<div class='row${if (complete) "" else " wait"}'><span class='grow'><strong>${esc(a["title"])}</strong>" +
                "<span class='meta'>${esc(meta)}</span></span>$end</div>")
        }
        val records = myRecords(student)
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
                score >= points -> { right++; "right" to "✓" }
                score > 0 -> "part" to "½"
                else -> {
                    if (missed.isEmpty()) {
                        var given = answers[q["id"].toString()]?.toString() ?: ""
                        val options = q["options"].asList() ?: emptyList()
                        if (q["type"] == "choice" && given.isNotEmpty() && given.all { it.isDigit() } && given.toInt() < options.size) {
                            given = options[given.toInt()].toString()
                        }
                        missed = "<div class='missed'><strong>${idx + 1}. ${esc(q["prompt"])}</strong>" +
                            "<span class='meta'>${esc(w("results.you_said", "answer" to given.ifEmpty { "—" }))}</span></div>"
                    }
                    "wrong" to "✗"
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
            "<p class='meta'>${esc(meta)}</p>$headline$grid$missed$sent$again</section>"
    }

    /** Books and lessons on the card. Whole courses (entries with a "shelf", such as a Kolibri channel) get
     *  one card each here and a page of their own, so a thousand videos don't fill this one. */
    fun library(req: Request): Response {
        val w = words(req)
        val query = (req.query["q"] ?: "").trim().lowercase()
        val shelf = req.query["shelf"] ?: ""
        val part = req.query["c"] ?: ""
        var items: List<Map<String, Any?>> = libraryItems()
        val body = StringBuilder("<label for='q'>${esc(w("library.search_label"))}</label><form method='get' action='/library' class='search'>" +
            "<input type='text' id='q' name='q' value='${esc(query)}'><button>${esc(w("library.search"))}</button></form>")
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
            val shelves = items.mapNotNull { it["shelf"]?.toString()?.ifEmpty { null } }.groupingBy { it }.eachCount().toSortedMap()
            if (shelves.isNotEmpty()) {
                body.append("<h2>${esc(w("library.courses"))}</h2>")
                for ((name, n) in shelves) body.append(libraryCard("/library?shelf=" + quote(name), name, w("library.lessons", "n" to n)))
            }
            body.append(libraryGroups(w, items.filter { it["shelf"]?.toString().isNullOrEmpty() }, byName = true))
        }
        if (items.isEmpty()) body.append("<p class='muted'>${esc(w("library.nothing"))}</p>")
        return Response(page(req, w("library.title"), body.toString()))
    }

    private fun libraryCard(href: String, title: String, meta: String) =
        "<a class='card item' href='$href'><strong>${esc(title)}</strong><span class='meta' style='display:block'>${esc(meta)}</span></a>"

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
                val credit = item["credit"]?.toString() ?: ""
                if (credit.isNotEmpty()) note += " · $credit"
                val kind = KIND_KEYS[item["kind"]?.toString() ?: ""]?.let { w(it) } ?: (item["kind"]?.toString() ?: "")
                out.append(libraryCard(href, item["title"].toString(),
                    "$kind · ${Html.size((item["size"] as? Number)?.toDouble() ?: 0.0)}$note"))
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
        val VIEWABLE = setOf(".html", ".htm", ".txt", ".pdf", ".epub", ".png", ".jpg", ".jpeg", ".svg", ".mp3", ".mp4", ".webm", ".zim")
        /** Library file types, as the key of the word a student knows. */
        val KIND_KEYS = mapOf("html" to "kind.page", "htm" to "kind.page", "txt" to "kind.text", "pdf" to "kind.pdf",
            "epub" to "kind.book", "png" to "kind.picture", "jpg" to "kind.picture", "jpeg" to "kind.picture",
            "svg" to "kind.picture", "mp3" to "kind.audio", "mp4" to "kind.video", "webm" to "kind.video", "zim" to "kind.encyclopedia")
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
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"""

        /** Python's str.capitalize(): first letter upper, the rest lower. */
        fun capitalize(s: String) = if (s.isEmpty()) s else s.substring(0, 1).uppercase() + s.substring(1).lowercase()
    }
}
