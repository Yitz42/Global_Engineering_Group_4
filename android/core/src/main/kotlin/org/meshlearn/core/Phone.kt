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
 * the pages, messages and database match the Python phone.
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

    init {
        dataDir.mkdirs()
        installStarterLibrary()
        db = makeDb(File(dataDir, "phone.db"), SCHEMA)
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

    private fun flush() = synchronized(flushLock) { flushNow = true; flushLock.notifyAll() }

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
        messenger.send(hubId, Protocol.HELLO, linkedMapOf(
            "n" to name, "f" to dataDir.usableSpace / (1024 * 1024), "l" to libraryItems().size.toLong(),
            "sw" to VERSION, "inv" to inventory(),
        ))
    }

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
        for (f in (packDir.listFiles() ?: emptyArray()).sortedBy { it.name }) {
            if (!f.name.endsWith(".json") || f.name == "courses.json") continue
            try {
                val packed = Json.parse(f.readText()).asMap() ?: continue
                if (importAssignment(packed)) count++
            } catch (e: Exception) {
                log.warning("skipping unreadable course pack file ${f.name}")
            }
        }
        return count
    }

    /** Copy every assignment from the teacher hub over Wi-Fi (no radio). */
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
                "collection" to (if (folder.isNotEmpty()) capitalize(folder) else "Other"), "kind" to ext.removePrefix(".")))
        }
        return items
    }

    // -- web ------------------------------------------------------------------

    private fun nav(req: Request): String {
        val who = req.cookies["student"]
        var out = "<a href='/'>Courses</a><a href='/library'>Library</a>"
        if (!who.isNullOrEmpty()) out += "<a href='/results'>My results</a><span class='muted'>Hi, ${esc(who)}</span>"
        return out
    }

    private fun page(req: Request, title: String, body: String) = Html.page(title, body, nav(req), site = "MeshLearn · $name")

    private fun routes() {
        app.add("GET", "/", ::home)
        app.add("POST", "/login", ::login)
        app.add("POST", "/logout", ::logout)
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

    private fun level(level: String, score: Long?): String {
        val cls = mapOf("Strong" to "good", "Good" to "good", "Developing" to "warn", "Needs work" to "bad")[level] ?: ""
        return "<span class='pill $cls'>${esc(level)}${if (score == null) "" else " · $score%"}</span>"
    }

    private fun questionCount(a: Map<String, Any?>) = Assignments.questions(a).size

    private fun assignmentCard(a: Map<String, Any?>, sub: Row?): String {
        val state = when {
            sub == null -> "<span class='pill'>To do</span>"
            sub["score"] == null -> "<span class='pill warn'>Turned in · waiting for grade</span>"
            (sub.long("complete") ?: 0L) == 0L ->
                "<span class='pill warn'>${g(sub.double("score"))} / ${g(sub.double("max"))} so far · teacher still grading</span>"
            else -> "<span class='pill good'>Score ${g(sub.double("score"))} / ${g(sub.double("max"))}</span>"
        }
        val due = if (truthy(a["due"])) " · due ${esc(a["due"])}" else ""
        val n = questionCount(a)
        return "<div class='card'><h3 style='margin:0 0 4px'><a href='/a/${esc(a["id"])}'>${esc(a["title"])}</a></h3>" +
            "<p class='muted' style='margin:0 0 8px'>$n question${if (n == 1) "" else "s"}$due" +
            "${if (a["mode"] == "practice") " · practice" else ""}</p>$state</div>"
    }

    private fun myLatest(student: String): Map<String, Row> = db.all(
        "SELECT assignment_id, MAX(submitted_at) AS at, score, max, complete FROM submissions " +
            "WHERE student = ? GROUP BY assignment_id", student).associateBy { it.str("assignment_id")!! }

    private fun student(req: Request): String? = req.cookies["student"]?.takeIf { it.isNotEmpty() }

    fun home(req: Request): Response {
        val student = student(req)
        if (student == null) {
            val body = "<div class='card'><p>Welcome! Type your name so your teacher knows whose work this is.</p>" +
                "<form method='post' action='/login'><label for='student'>Your name</label>" +
                "<input type='text' id='student' name='student' maxlength='40' autocomplete='name' required>" +
                "<button>Start</button></form></div>" +
                "<p>Or go straight to the <a href='/library'>library</a>.</p>"
            return Response(page(req, "Welcome", body))
        }
        val mine = myLatest(student)
        val records = myRecords(student)
        val cards = StringBuilder()
        val todo = ArrayList<Map<String, Any?>>()
        for (course in courseTree()) {
            val openItems = assignmentsIn(course["id"].toString())
            if (course["id"] == "" && openItems.isEmpty()) continue
            todo += openItems.filter { it["id"] !in mine }
            if (truthy(course["comingSoon"])) {
                cards.append("<div class='card' style='opacity:.6'><h2 style='margin-top:0'>${esc(course["title"])}</h2>" +
                    "<p class='muted'>${esc(course["description"] ?: "")}</p><span class='pill'>Coming later</span></div>")
                continue
            }
            val summary = Comprehension.summarize(records, course)
            val left = openItems.count { it["id"] !in mine }
            val href = esc(course["id"]).ifEmpty { "_other" }
            cards.append("<div class='card'><h2 style='margin-top:0'><a href='/c/$href'>${esc(course["title"])}</a></h2>" +
                "<p class='muted'>${esc(course["description"] ?: "")}</p>" +
                "<p>${openItems.size} open · $left to do</p>${level(summary.level, summary.score)}</div>")
        }
        val todoHtml = if (todo.isNotEmpty()) "<h2>To do</h2>" + todo.take(5).joinToString("") { assignmentCard(it, null) } else ""
        val body = todoHtml + "<h2>Your courses</h2>" +
            (if (cards.isNotEmpty()) "<div class='grid'>$cards</div>" else "<p class='muted'>No courses yet. Your teacher will send them.</p>") +
            "<form method='post' action='/logout'><button class='secondary'>Not you? Change name</button></form>"
        return Response(page(req, "Hi, $student", body))
    }

    fun coursePage(req: Request): Response {
        val student = student(req) ?: return Response.redirect("/")
        val cid = if (req.params["cid"] == "_other") "" else req.params["cid"] ?: ""
        val course = courseTree().firstOrNull { it["id"] == cid }
            ?: return Response(page(req, "Not found", "<p>That course is not on this phone.</p>"), 404)
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
            sections.append("<h2>Unit ${i + 1}: ${esc(unit["title"])} ${level(u?.level ?: "Not started", u?.score)}</h2>" +
                (inUnit.joinToString("") { assignmentCard(it, mine[it["id"]]) }.ifEmpty { "<p class='muted'>Nothing open in this unit right now.</p>" }) +
                "<p><a href='/library?q=${quote(read)}'>Find readings in the library</a></p>")
        }
        val unitIds = units.map { it["id"] }.toSet()
        val loose = items.filter { (it["unit"] ?: "") !in unitIds }
        if (loose.isNotEmpty()) {
            sections.append((if (units.isEmpty()) "<h2>Assignments</h2>" else "<h2>Other assignments</h2>") +
                loose.joinToString("") { assignmentCard(it, mine[it["id"]]) })
        }
        val body = "<p class='muted'>${esc(course["description"] ?: "")}</p>" +
            "<p>Your level in this course: ${level(summary.level, summary.score)}</p>$sections"
        return Response(page(req, course["title"].toString(), body))
    }

    fun login(req: Request): Response {
        val n = (req.form["student"] ?: "").trim().split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ").take(40)
        if (n.isEmpty()) return Response.redirect("/")
        return Response.redirect("/").setCookie("student", n)
    }

    fun logout(req: Request): Response = Response.redirect("/").setCookie("student", "", maxAge = 0)

    private fun renderForm(a: Map<String, Any?>, values: Map<String, String>, errors: Map<String, String>): String {
        val parts = StringBuilder()
        if (truthy(a["instructions"])) parts.append("<div class='card'><p>${esc(a["instructions"])}</p></div>")
        if (errors.isNotEmpty()) {
            parts.append("<p class='error' role='alert'>Please fix ${errors.size} answer${if (errors.size > 1) "s" else ""} below.</p>")
        }
        parts.append("<form method='post' action='/a/${esc(a["id"])}' novalidate>")
        Assignments.questions(a).forEachIndexed { idx, q ->
            val i = idx + 1
            val qid = esc(q["id"])
            val value = values[q["id"].toString()] ?: ""
            val err = errors[q["id"].toString()]
            val errHtml = if (err != null) "<p class='error' id='$qid-err'>${esc(err)}</p>" else ""
            val described = if (err != null) " aria-describedby='$qid-err' aria-invalid='true'" else ""
            val pts = " <span class='muted'>(${g((q["points"] as? Number)?.toDouble() ?: 1.0)} pt)</span>"
            when (q["type"]) {
                "choice" -> {
                    val opts = (q["options"].asList() ?: emptyList()).mapIndexed { j, opt ->
                        "<label class='option'><input type='radio' name='$qid' value='$j'" +
                            "${if (value == j.toString()) " checked" else ""}$described> ${esc(opt)}</label>"
                    }.joinToString("")
                    parts.append("<fieldset><legend>$i. ${esc(q["prompt"])}$pts</legend>$errHtml$opts</fieldset>")
                }
                "long" -> {
                    val limit = (q["max_chars"] as? Number)?.toLong() ?: 600L
                    parts.append("<fieldset><legend>$i. ${esc(q["prompt"])}$pts</legend>$errHtml" +
                        "<label for='$qid'>Your answer (up to $limit characters)</label>" +
                        "<textarea id='$qid' name='$qid' maxlength='$limit'$described>${esc(value)}</textarea></fieldset>")
                }
                else -> {
                    val number = q["type"] == "number"
                    val mode = if (number) " inputmode='decimal'" else ""
                    val hint = if (number) "A number, like 12, 3.5, 3,5 or 1/2" else "Short answer"
                    parts.append("<fieldset><legend>$i. ${esc(q["prompt"])}$pts</legend>$errHtml" +
                        "<label for='$qid'>$hint</label>" +
                        "<input type='text' id='$qid' name='$qid' value='${esc(value)}' maxlength='${Assignments.MAX_TEXT_CHARS}'$mode$described>" +
                        "</fieldset>")
                }
            }
        }
        parts.append("<button>Check and submit</button></form>")
        return parts.toString()
    }

    private fun openAssignment(req: Request): MutableMap<String, Any?>? =
        getAssignment(req.params["aid"] ?: "")?.takeIf { it["open"] == true }

    fun assignmentForm(req: Request): Response {
        val student = student(req) ?: return Response.redirect("/")
        val a = openAssignment(req) ?: return Response(page(req, "Not open",
            "<p>That assignment is not open right now.</p><p><a href='/'>Back to assignments</a></p>"), 404)
        if (a["mode"] != "practice") {
            val done = db.one("SELECT submitted_at FROM submissions WHERE assignment_id = ? AND student = ?", a["id"], student)
            if (done != null) {
                val body = "<p>You already turned this in on ${Html.whenText(done.double("submitted_at"))}.</p>" +
                    "<p><a href='/results'>See your results</a></p>"
                return Response(page(req, a["title"].toString(), body))
            }
        }
        opened(student, a["id"].toString())
        return Response(page(req, a["title"].toString(), renderForm(a, emptyMap(), emptyMap())))
    }

    fun assignmentSubmit(req: Request): Response {
        val student = student(req) ?: return Response.redirect("/")
        val a = openAssignment(req) ?: return Response.redirect("/")
        val raw = Assignments.questions(a).associate { it["id"].toString() to (req.form[it["id"].toString()] ?: "") }
        val (answers, errors) = Assignments.validateAnswers(a, raw)
        if (errors.isNotEmpty()) return Response(page(req, a["title"].toString(), renderForm(a, raw, errors)), 400)
        if (a["mode"] != "practice" &&
            db.one("SELECT 1 AS x FROM submissions WHERE assignment_id = ? AND student = ?", a["id"], student) != null) {
            return Response.redirect("/results")
        }
        submit(a, student, answers)
        if (a["mode"] == "practice") {
            val feedback = Assignments.practiceFeedback(a, answers)
            val items = Assignments.questions(a).joinToString("") { q ->
                val ok = feedback[q["id"].toString()]
                val mark = when (ok) {
                    true -> "<span class='good'>Correct</span>"
                    false -> "<span class='bad'>Not quite</span>"
                    null -> "<span class='muted'>Your teacher will read this one</span>"
                }
                "<li>${esc(q["prompt"])}: $mark</li>"
            }
            val right = feedback.values.count { it == true }
            val checked = feedback.values.count { it != null }
            val body = "<p>You got <strong>$right</strong> of $checked auto-checked questions right.</p><ol>$items</ol>" +
                "<a class='button' href='/a/${esc(a["id"])}'>Try again</a><a class='button secondary' href='/'>Back</a>"
            return Response(page(req, "Practice results", body))
        }
        val body = "<div class='card'><p class='good'><strong>Done! Your work is saved on this phone.</strong></p>" +
            "<p>It will reach your teacher over the radio. That can take a while, and you do not need to wait here.</p></div>" +
            "<a class='button' href='/'>Back to assignments</a>"
        return Response(page(req, "Submitted", body))
    }

    fun results(req: Request): Response {
        val student = student(req) ?: return Response.redirect("/")
        val out = StringBuilder()
        for (s in db.all("SELECT * FROM submissions WHERE student = ? ORDER BY submitted_at DESC", student)) {
            val title = getAssignment(s.str("assignment_id")!!)?.get("title") ?: s.str("assignment_id")
            val sent = when (messenger.messageStatus(s.long("msg_id"))) {
                "acked" -> "reached your teacher"; "pending" -> "on its way"; "failed" -> "could not be sent"
                else -> "saved, sending soon"
            }
            val grade = when {
                s["score"] == null -> "Not graded yet"
                (s.long("complete") ?: 0L) != 0L -> "<strong>${g(s.double("score"))} / ${g(s.double("max"))}</strong>"
                else -> "${g(s.double("score"))} / ${g(s.double("max"))} so far (teacher still grading)"
            }
            out.append("<tr><td>${esc(title)}</td><td>${Html.whenText(s.double("submitted_at"))}</td><td>$sent</td><td>$grade</td></tr>")
        }
        val records = myRecords(student)
        val progress = StringBuilder()
        for (course in courseTree()) {
            val summary = Comprehension.summarize(records, course)
            if (summary.answered == 0) continue
            val units = summary.units.filter { it.answered > 0 }
                .joinToString("") { "<li>Unit ${it.number}: ${esc(it.title)} ${level(it.level, it.score)}</li>" }
            progress.append("<div class='card'><h3 style='margin-top:0'>${esc(course["title"])} " +
                "${level(summary.level, summary.score)}</h3><ul>$units</ul></div>")
        }
        val body = (if (progress.isNotEmpty()) "<h2>How you are doing</h2>$progress" else "") +
            "<h2>Everything you turned in</h2>" +
            (if (out.isNotEmpty()) "<table><tr><th>Assignment</th><th>Turned in</th><th>Delivery</th><th>Grade</th></tr>$out</table>"
            else "<p class='muted'>Nothing turned in yet.</p>")
        return Response(page(req, "My results", body))
    }

    fun library(req: Request): Response {
        val query = (req.query["q"] ?: "").trim().lowercase()
        var items = libraryItems()
        if (query.isNotEmpty()) {
            items = items.filter { query in ("${it["title"] ?: ""} ${it["collection"] ?: ""}").lowercase() }.toMutableList()
        }
        val groups = sortedMapOf<String, MutableList<Map<String, Any?>>>()
        for (item in items) groups.getOrPut(item["collection"]?.toString()?.ifEmpty { null } ?: "Other") { ArrayList() }.add(item)
        val sections = StringBuilder()
        for ((group, entries) in groups) {
            val rows = StringBuilder()
            for (item in entries) {
                val file = item["file"].toString()
                val href = "/library/files/" + quote(file.replace(File.separatorChar, '/'))
                var link = "<a href='$href'>${esc(item["title"])}</a>"
                var note = ""
                if (item["kind"] == "zim") {
                    val book = File(file).name.substringBeforeLast('.')
                    if (kiwixUrl.isNotEmpty()) link = "<a href='${esc(kiwixUrl)}/content/${quote(book)}'>${esc(item["title"])}</a>"
                    else note = " <span class='muted'>· open with the Kiwix app</span>"
                }
                rows.append("<tr><td>$link$note</td><td class='muted'>${esc(item["kind"] ?: "")}</td>" +
                    "<td class='muted'>${Html.size((item["size"] as? Number)?.toDouble() ?: 0.0)}</td></tr>")
            }
            sections.append("<h2>${esc(group)}</h2><table>$rows</table>")
        }
        val search = "<form method='get' action='/library'><label for='q'>Search the library</label>" +
            "<input type='text' id='q' name='q' value='${esc(query)}'><button>Search</button></form>"
        return Response(page(req, "Library", search + sections.ifEmpty { "<p class='muted'>Nothing found.</p>" }))
    }

    fun libraryFile(req: Request): Response {
        val root = libraryDir.canonicalFile
        val target = File(root, req.params["path"] ?: "").canonicalFile
        if (!target.path.startsWith(root.path + File.separator) || !target.isFile) {
            return Response(page(req, "Not found", "<p>That file is not in the library.</p>"), 404)
        }
        return Response.file(target)
    }

    // -- status and admin -----------------------------------------------------

    /** Course-pack changes come from the phone itself, or anyone with the PIN. */
    private fun isAdmin(req: Request): Boolean = req.isLocal || (adminPin.isNotEmpty() && req.form["pin"] == adminPin)

    fun status(req: Request, notice: String = ""): Response {
        val q = messenger.status()
        val held = db.one("SELECT COUNT(*) AS n, SUM(open) AS o FROM assignments")
        val waiting = db.one("SELECT COUNT(*) AS n FROM submissions WHERE msg_id IS NULL")?.long("n") ?: 0
        fun pinField(id: String) = if (adminPin.isEmpty()) "" else
            "<label for='$id'>Teacher PIN</label><input type='text' id='$id' name='pin' inputmode='numeric'>"
        val body = (if (notice.isNotEmpty()) "<p class='good' role='status'>${esc(notice)}</p>" else "") +
            "<div class='grid'>" +
            "<div class='card'><div class='stat'>${held?.long("n") ?: 0}</div>assignments stored (${held?.long("o") ?: 0} open)</div>" +
            "<div class='card'><div class='stat'>$waiting</div>submissions waiting to be batched</div>" +
            "<div class='card'><div class='stat'>${q["pending"]}</div>messages waiting for the radio" +
            "<div class='muted'>${q["frames_sent"]} radio packets sent since start</div></div>" +
            "<div class='card'><div class='stat'>${Html.size(dataDir.usableSpace.toDouble())}</div>free on the card</div></div>" +
            "<h2>Copy assignments from the teacher (no radio)</h2>" +
            "<form method='post' action='/status/sync' class='card'>" +
            "<p>When this phone is on the same Wi-Fi as the teacher's laptop, copy every assignment at once.</p>" +
            "<label for='hub_url'>Teacher hub address</label>" +
            "<input type='text' id='hub_url' name='hub_url' placeholder='http://192.168.1.20:8000' required>" +
            "${pinField("pin-sync")}<button>Copy assignments</button></form>" +
            "<form method='post' action='/status/reload' class='card'>" +
            "<p>Copied a course pack onto the memory card? Load it now.</p>" +
            "${pinField("pin-reload")}<button class='secondary'>Reload from card</button></form>" +
            "<p class='muted'>Teacher hub radio: ${esc(hubId)} · this phone's radio: ${esc(nodeId)} · " +
            "data folder: ${esc(dataDir.path)}</p>"
        return Response(page(req, "Phone status", body))
    }

    fun statusSync(req: Request): Response {
        if (!isAdmin(req)) return status(req, "Only the teacher can do that (open this page on the phone itself, or enter the PIN).")
        var url = (req.form["hub_url"] ?: "").trim()
        if (!url.startsWith("http://") && !url.startsWith("https://")) url = "http://$url"
        val count = try {
            syncFromHub(url)
        } catch (e: Exception) {
            return status(req, "Could not reach the teacher hub at $url: ${e.message ?: e.javaClass.simpleName}")
        }
        return status(req, "Copied from the teacher hub: $count new or updated assignments.")
    }

    fun statusReload(req: Request): Response {
        if (!isAdmin(req)) return status(req, "Only the teacher can do that (open this page on the phone itself, or enter the PIN).")
        val count = loadPackFolder()
        sayHello()
        return status(req, "Loaded from the card: $count new or updated assignments.")
    }

    companion object {
        const val VERSION = "0.1.0-android"
        const val MAX_OPEN_S = 6 * 3600.0
        const val MAX_BATCH = 20
        val VIEWABLE = setOf(".html", ".htm", ".txt", ".pdf", ".epub", ".png", ".jpg", ".jpeg", ".svg", ".mp3", ".mp4", ".webm", ".zim")
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
)"""

        /** Python's str.capitalize(): first letter upper, the rest lower. */
        fun capitalize(s: String) = if (s.isEmpty()) s else s.substring(0, 1).uppercase() + s.substring(1).lowercase()
    }
}
