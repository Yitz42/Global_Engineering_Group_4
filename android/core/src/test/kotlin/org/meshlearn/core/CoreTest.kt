package org.meshlearn.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.Base64
import java.util.Random
import java.util.concurrent.ConcurrentHashMap

/** The Kotlin phone must read and write exactly what the Python code does (vectors from gen_kotlin_shared.py). */
class CoreTest {
    private val vectors: Map<String, Any?> =
        Json.parse(javaClass.getResource("/vectors.json")!!.readText()).asMap()!!

    private fun canon(v: Any?) = Json.dumps(v, sortKeys = true)

    /** The radio's Bluetooth IDs, copied from Meshtastic's ble_interface.py. One wrong character and the phone can't talk to it. */
    @Test fun bluetoothIdsMatchMeshtastic() {
        assertEquals("6ba1b218-15a8-461f-9fa8-5dcae273eafd", MeshtasticProto.SERVICE_UUID)
        assertEquals("f75c76d2-129e-4dad-a1dd-7866124401e7", MeshtasticProto.TORADIO_UUID)
        assertEquals("2c55e69e-4993-11ed-b878-0242ac120002", MeshtasticProto.FROMRADIO_UUID)
        assertEquals("ed9da18c-a800-4f66-a670-aa7547e34453", MeshtasticProto.FROMNUM_UUID)
    }

    @Test fun zdictMatchesPython() {
        assertEquals((vectors["zdict_len"] as Number).toInt(), Zdict.BYTES.size)
    }

    @Test fun readsPythonFrames() {
        for (m in vectors["messages"].asList()!!.map { it.asMap()!! }) {
            val asm = Assembler()
            var got: Message? = null
            for (f in m["frames"].asList()!!) got = asm.add(Base64.getDecoder().decode(f as String), "hub").message ?: got
            assertNotNull("message ${m["type"]} did not assemble", got)
            assertEquals(m["type"], got!!.type)
            assertEquals((m["msg_id"] as Number).toLong(), got.msgId)
            assertEquals(canon(m["payload"]), canon(got.payload))
        }
    }

    @Test fun roundTripsOwnFramesAndKeepsHeaderLayout() {
        for (m in vectors["messages"].asList()!!.map { it.asMap()!! }) {
            val payload = m["payload"].asMap()!!
            val id = (m["msg_id"] as Number).toLong()
            val (_, frames) = Protocol.encode(m["type"] as String, payload, id)
            val python = m["frames"].asList()!!.map { Base64.getDecoder().decode(it as String) }
            // Header: magic, version|END on the last frame only, id, seq, total.
            frames.forEachIndexed { seq, f ->
                assertEquals(0xA7, f[0].toInt() and 0xFF)
                assertEquals((2 shl 4) or (if (seq == frames.size - 1) 1 else 0), f[1].toInt() and 0xFF)
                assertEquals(seq, f[6].toInt() and 0xFF)
                assertEquals(frames.size, f[7].toInt() and 0xFF)
                assertTrue(f.size <= Protocol.MAX_FRAME_BYTES)
            }
            assertEquals(python[0].copyOfRange(0, 6).toList(), frames[0].copyOfRange(0, 6).toList())
            val asm = Assembler()
            var got: Message? = null
            for (f in frames) got = asm.add(f, "phone").message ?: got
            assertEquals(canon(payload), canon(got!!.payload))
        }
    }

    @Test fun nackListsMissingPieces() {
        val big = vectors["messages"].asList()!!.map { it.asMap()!! }.first { it["type"] == "a" }
        val frames = big["frames"].asList()!!.map { Base64.getDecoder().decode(it as String) }
        assertTrue("sample assignment should need several frames", frames.size >= 2)
        val asm = Assembler()
        val result = asm.add(frames.last(), "hub")
        assertEquals((0 until frames.size - 1).toList(), result.missing)
    }

    @Test fun assignmentsMatchPython() {
        for (v in vectors["assignments"].asList()!!.map { it.asMap()!! }) {
            val a = v["assignment"].asMap()!!
            assertEquals(canon(v["packed"]), canon(Assignments.pack(a)))
            assertEquals(v["version"], Assignments.versionOf(a))
            val unpacked = Assignments.unpack(Assignments.pack(a))
            assertEquals(v["version"], Assignments.versionOf(unpacked))
        }
    }

    @Test fun parsesNumbersLikePython() {
        for ((text, want) in vectors["parse_number"].asMap()!!) {
            assertEquals("parse_number($text)", (want as? Number)?.toDouble(), Assignments.parseNumber(text))
        }
    }

    @Test fun formatsFloatsLikePython() {
        for ((repr, value) in vectors["float_repr"].asMap()!!) assertEquals(repr, Json.pyFloatRepr((value as Number).toDouble()))
        assertEquals("3", Html.g(3.0)); assertEquals("2.5", Html.g(2.5)); assertEquals("0.333333", Html.g(1.0 / 3))
    }

    @Test fun validatesAndGradesPractice() {
        val a = vectors["assignments"].asList()!!.map { it.asMap()!! }[2]["assignment"].asMap()!!
        val (answers, errors) = Assignments.validateAnswers(a, mapOf("q1" to "1", "q2" to "3/4", "q3" to "  Chlorophyll. ", "q4" to "9", "q5" to ""))
        assertEquals(setOf("q4", "q5"), errors.keys)
        assertEquals("3/4", answers["q2"])
        val fb = Assignments.practiceFeedback(a, mapOf("q1" to "1", "q2" to "0,75", "q3" to "chlorophyll", "q4" to "0", "q5" to "x"))
        assertEquals(mapOf("q1" to true, "q2" to true, "q3" to true, "q4" to false, "q5" to null), fb)
    }

    // -- messenger and phone over a lossy in-memory link ---------------------------------------

    private class Link(val dropRate: Double, seed: Long) {
        val handlers = ConcurrentHashMap<String, FrameHandler>()
        val rng = Random(seed)
        fun node(id: String) = object : Transport {
            override val nodeId = id
            override fun start(onFrame: FrameHandler) { handlers[id] = onFrame }
            override fun send(frame: ByteArray, dest: String) {
                if (synchronized(rng) { rng.nextDouble() } < dropRate) return
                val h = handlers[dest] ?: return
                Thread { h(frame, id) }.start()
            }
        }
    }

    private fun tmp(): File = Files.createTempDirectory("meshlearn-test").toFile()
    private fun db(dir: File, name: String, schema: String = "") = JdbcDb(File(dir, name).path, schema)

    private fun waitFor(timeoutMs: Long = 20_000, cond: () -> Boolean) {
        val end = System.currentTimeMillis() + timeoutMs
        while (!cond()) {
            if (System.currentTimeMillis() > end) throw AssertionError("timed out")
            Thread.sleep(50)
        }
    }

    @Test fun messengerDeliversOverLossyLink() {
        val link = Link(0.3, 7)
        val dir = tmp()
        val received = ArrayList<Message>()
        val a = Messenger(link.node("hub"), db(dir, "a.db"), {}, retryS = 0.2, maxRetryS = 1.0)
        val b = Messenger(link.node("phone-1"), db(dir, "b.db"), { synchronized(received) { received.add(it) } }, retryS = 0.2, maxRetryS = 1.0)
        a.start(); b.start()
        val big = vectors["messages"].asList()!!.map { it.asMap()!! }.first { it["type"] == "a" }["payload"].asMap()!!
        val id = a.send("phone-1", Protocol.ASSIGN, big)
        waitFor { a.messageStatus(id) == "acked" && synchronized(received) { received.isNotEmpty() } }
        assertEquals(1, received.size)
        assertEquals(canon(big), canon(received[0].payload))
        a.stop(); b.stop()
    }

    @Test fun phoneFlowMatchesHubExpectations() {
        val link = Link(0.0, 1)
        val dir = tmp()
        val hubGot = ArrayList<Message>()
        val hub = Messenger(link.node("hub"), db(dir, "hub.db"), { synchronized(hubGot) { hubGot.add(it) } }, retryS = 0.5)
        val phone = Phone(File(dir, "phone"), "hub", "Test phone", { f, s -> JdbcDb(f.path, s) },
            { h -> Messenger(link.node("phone-1"), db(dir, "outbox.db"), h, retryS = 0.5) }, batchS = 0.2)
        hub.start(); phone.start()

        // A graded assignment arrives by radio, then the teacher opens it with one OPEN packet.
        val v = vectors["assignments"].asList()!!.map { it.asMap()!! }[1]
        val packed = LinkedHashMap(v["packed"].asMap()!!).apply { put("v", v["version"]) }
        hub.send("phone-1", Protocol.ASSIGN, mapOf("a" to packed, "o" to 0, "d" to ""))
        waitFor { phone.getAssignment("a1b2c3") != null }
        assertEquals(false, phone.getAssignment("a1b2c3")!!["open"])
        hub.send("phone-1", Protocol.OPEN, mapOf("a" to "a1b2c3", "v" to v["version"], "o" to 1, "d" to "2026-12-01"))
        waitFor { phone.getAssignment("a1b2c3")!!["open"] == true }

        // A student logs in and turns it in through the web pages.
        fun req(method: String, path: String, body: String = "", cookie: String = "") = Request(method, path, emptyMap(),
            mapOf("cookie" to cookie), body.toByteArray(), "192.168.43.10")
        val login = phone.app.dispatch(req("POST", "/login", "student=Lena%20M%C3%BCller"))
        assertEquals(303, login.status)
        val cookie = login.headers["Set-Cookie"]!!.substringBefore(";")
        assertTrue(String(phone.app.dispatch(req("GET", "/", cookie = cookie)).body).contains("Fractions and plants"))
        val form = phone.app.dispatch(req("GET", "/a/a1b2c3", cookie = cookie))
        assertEquals(200, form.status)
        val formHtml = String(form.body)
        assertTrue(formHtml.contains("Question 1 of 5") && formHtml.contains("meshlearn-draft:") && formHtml.contains("0 / 500"))
        assertTrue(String(phone.app.dispatch(req("GET", "/", cookie = cookie)).body).contains("You have 1 assignment to do."))
        val bad = phone.app.dispatch(req("POST", "/a/a1b2c3", "q1=1", cookie))
        assertEquals(400, bad.status)
        val ok = phone.app.dispatch(req("POST", "/a/a1b2c3", "q1=1&q2=3%2C5&q3=chlorophyll&q4=1&q5=We+share+pizza", cookie))
        assertEquals(200, ok.status)

        waitFor { synchronized(hubGot) { hubGot.any { it.type == Protocol.SUBMIT } } }
        val sub = hubGot.first { it.type == Protocol.SUBMIT }.payload["s"].asList()!![0].asMap()!!
        assertEquals("a1b2c3", sub["a"]); assertEquals(v["version"], sub["v"]); assertEquals("Lena Müller", sub["u"])
        assertEquals("3,5", sub["x"].asMap()!!["q2"])
        assertTrue(sub["d"] is Long)

        // The hub's grades come back and show on the results page.
        hub.send("phone-1", Protocol.RESULT, mapOf("r" to listOf(mapOf("id" to sub["id"], "sc" to 4.0, "mx" to 7.0, "ok" to 0,
            "q" to mapOf("q1" to 1.0, "q2" to 0.0, "q3" to 1.0, "q4" to 1.0, "q5" to null)))))
        waitFor { String(phone.app.dispatch(req("GET", "/results", cookie = cookie)).body).contains("4 / 7 so far") }

        // Library: the starter pages are installed and served; paths can't escape the folder.
        val lib = String(phone.app.dispatch(req("GET", "/library")).body)
        assertTrue(lib.contains("Fractions"))
        assertEquals(200, phone.app.dispatch(req("GET", "/library/files/starter/fractions.html")).status)
        assertEquals(404, phone.app.dispatch(req("GET", "/library/files/../phone.db")).status)
        // A course from Kolibri gets one card, its own page in course order, and credits.
        val index = (1..45).joinToString(",", "[", "]") { n ->
            """{"title":"Somo $n","file":"kolibri/$n.mp4","kind":"mp4","shelf":"Khan (Kiswahili)","collection":"${if (n < 5) "Z: Kuongeza" else "A: Kutoa"}","credit":"Khan Academy · CC BY-NC-SA"}"""
        }
        File(phone.libraryDir, "kolibri").mkdirs()
        (1..45).forEach { File(phone.libraryDir, "kolibri/$it.mp4").writeText("x".repeat(10)) }
        File(phone.libraryDir, "index.json").writeText(index)
        val home = String(phone.app.dispatch(req("GET", "/library")).body)
        assertTrue(home.contains("<a class='card item' href='/library?shelf=Khan") && home.contains("Lessons: 45") && !home.contains("Somo 1<"))
        fun shelf(c: String = "") = String(phone.app.dispatch(Request("GET", "/library",
            mapOf("shelf" to "Khan (Kiswahili)", "c" to c), emptyMap(), ByteArray(0), "127.0.0.1")).body)
        assertTrue(shelf().indexOf("Z: Kuongeza") in 1 until shelf().indexOf("A: Kutoa") && !shelf().contains("Somo 1<"))
        assertTrue(shelf("Z: Kuongeza").contains("Somo 4<") && !shelf("Z: Kuongeza").contains("Somo 5<"))
        assertTrue(shelf("Z: Kuongeza").contains("Khan Academy · CC BY-NC-SA"))
        assertEquals(2L..4L, App.byteRange("bytes=2-4", 10))
        assertEquals(2L..9L, App.byteRange("bytes=2-", 10))
        assertEquals(7L..9L, App.byteRange("bytes=-3", 10))
        assertEquals(null, App.byteRange("bytes=10-", 10))
        assertEquals(null, App.byteRange("", 10))
        // Admin actions need the phone itself or the PIN.
        assertTrue(String(phone.app.dispatch(req("POST", "/status/reload")).body).contains("Only the teacher"))
        phone.stop(); hub.stop()
    }

    /** Question info packs as in Python (a shared text travels once) and shows beside each question, with arrows and review. */
    @Test fun questionInfoAndReview() {
        val rain = "Rainfall:\nJanuary 120 mm"
        val packed = Json.parse("""{"i":"r1","T":"Rain","I":"Use the table.","Q":[{"t":1,"p":"Total?","n":"Rainfall:\nJanuary 120 mm"},""" +
            """{"t":1,"p":"Difference?","n":1},{"t":2,"p":"Why?"}]}""").asMap()!!
        val a = Assignments.unpack(packed)
        assertEquals(listOf(rain, rain, null), Assignments.questions(a).map { it["info"] })
        assertEquals(Json.dumps(packed["Q"], sortKeys = true), Json.dumps(Assignments.pack(a)["Q"], sortKeys = true))
        val dir = tmp()
        val link = Link(0.0, 1)
        val phone = Phone(File(dir, "phone"), "hub", "Test phone", { f, s -> JdbcDb(f.path, s) },
            { h -> Messenger(link.node("phone-1"), db(dir, "outbox.db"), h, retryS = 0.5) }, batchS = 0.2)
        phone.importAssignment(packed, open = true)
        val html = String(phone.app.dispatch(Request("GET", "/a/r1", emptyMap(), mapOf("cookie" to "student=Ana"), ByteArray(0), "1.2.3.4")).body)
        assertEquals(3, html.split("<details class='qinfo'").size - 1)
        assertEquals(2, html.split("<summary>Question info</summary>").size - 1)
        for (part in listOf("id='back'", "id='next'", "id='toreview'", "id='review'", "January 120 mm", "data-prompt='Why?'")) assertTrue(html.contains(part))
        // Three languages: all on the welcome page, the other two as buttons later.
        val langDir = File(System.getProperty("meshlearn.repo"), "meshlearn/lang")
        fun pack(code: String) = Json.parse(File(langDir, "$code.json").readText(Charsets.UTF_8)).asMap()!!
        assertTrue(phone.importLanguage(mapOf("school" to pack("eng"), "second" to pack("swa"), "more" to listOf(pack("fra")))))
        fun page(path: String, cookie: String) = String(phone.app.dispatch(Request("GET", path, emptyMap(), mapOf("cookie" to cookie), ByteArray(0), "1.2.3.4")).body, Charsets.UTF_8)
        // The school's ages choose the looks, also after a restart; students pick theirs.
        assertTrue(page("/", "student=Ana").contains("<body class='st look-mid'>"))
        assertTrue(phone.importLanguage(mapOf("school" to pack("eng"), "second" to pack("swa"), "more" to listOf(pack("fra")),
            "ages" to listOf("early", "upper"))))
        val again = Phone(File(dir, "phone"), "hub", "Test phone", { f, s -> JdbcDb(f.path, s) },
            { h -> Messenger(link.node("phone-1b"), db(dir, "outbox2.db"), h, retryS = 0.5) }, batchS = 0.2)
        for (p in listOf(phone, again)) {
            fun get(path: String, cookie: String, method: String = "GET", body: String = "") = p.app.dispatch(
                Request(method, path, emptyMap(), mapOf("cookie" to cookie), body.toByteArray(), "1.2.3.4"))
            fun html(path: String, cookie: String) = String(get(path, cookie).body, Charsets.UTF_8)
            assertTrue(html("/", "student=Ana").contains("<body class='st look-teen'>"))
            assertTrue(html("/", "").contains("How old are you?"))
            assertTrue(get("/look", "", "POST", "to=young").headers["Set-Cookie"]!!.startsWith("look=young"))
            val young = html("/", "student=Ana; look=young")
            assertTrue(young.contains("class='bigplay'") && young.contains("Stars this week: 0") && young.contains("</span>My stars</a>"))
            assertTrue(html("/results", "student=Ana; look=young").contains("<h1>My stars</h1>"))
            assertTrue(html("/", "student=Ana").contains("class='tiles'"))
            assertTrue(html("/a/r1", "student=Ana").contains("id='qprog'"))
        }
        val welcome = page("/", "")
        for (name in listOf("English", "Kiswahili", "Français")) assertTrue(welcome.contains(">$name</button>"))
        val french = page("/library", "student=Ana; lang=fra")
        assertTrue(french.contains("<html lang='fr'>") && french.contains(">Kiswahili</button>") && french.contains(">English</button>"))
    }

    /** Signing in by picture, secret picture and PIN; pictures in questions; keep reading; right answers: as the Python phone. */
    @Test fun signInPicturesReadingAndRightAnswers() {
        assertEquals("8b86cf26c69f90f40d1f77b5a6864445", SignIn.hash("Tomás", "4321"))   // the same value is in test_meshlearn.py
        val langDir = File(System.getProperty("meshlearn.repo"), "meshlearn/lang")
        val eng = Json.parse(File(langDir, "eng.json").readText(Charsets.UTF_8)).asMap()!!
        val dir = tmp()
        val link = Link(0.0, 1)
        val phone = Phone(File(dir, "phone"), "hub", "Test phone", { f, s -> JdbcDb(f.path, s) },
            { h -> Messenger(link.node("phone-1"), db(dir, "outbox.db"), h, retryS = 0.5) }, batchS = 0.2)
        fun go(path: String, cookie: String = "", method: String = "GET", body: String = "", headers: Map<String, String> = emptyMap()): Response {
            val (p, q) = path.split("?", limit = 2).let { it[0] to (it.getOrNull(1) ?: "") }
            return phone.app.dispatch(Request(method, p, Html.parseQs(q), mapOf("cookie" to cookie) + headers, body.toByteArray(), "1.2.3.4"))
        }
        fun html(path: String, cookie: String = "") = String(go(path, cookie).body, Charsets.UTF_8)
        fun school(way: String, vararg students: Map<String, Any?>) = phone.importLanguage(mapOf("school" to eng, "second" to null, "more" to emptyList<Any?>(),
            "ages" to listOf("early"), "students" to students.toList(), "signin" to mapOf("young" to way, "mid" to "list", "teen" to "pin"), "signout" to 10L))
        assertTrue(html("/").contains("Type your name"))
        assertTrue(school("pic", mapOf("n" to "Lena", "p" to "cat", "a" to "young")))
        assertTrue(html("/").contains("<h1>Who are you?</h1>") && html("/").contains("data-name='Lena'"))
        assertTrue(html("/who?n=Lena").contains("Are you Lena?"))
        assertTrue(go("/who", "", "POST", "n=Lena").headers["Set-Cookie"]!!.startsWith("student=Lena;"))
        assertTrue(html("/", "student=Lena").contains("class='pic face'") && html("/", "student=Lena").contains("data-min='10'"))
        // Secret picture: not by typing the name or the cookie.
        school("secret", mapOf("n" to "Lena", "p" to "cat", "a" to "young", "s" to SignIn.hash("Lena", "star")))
        assertEquals(6, html("/who?n=Lena").split("name='secret'").size - 1)
        assertTrue(html("/who?n=Lena").contains("value='star'"))
        assertTrue(String(go("/who", "", "POST", "n=Lena&secret=ball").body).contains("Not that one"))
        assertTrue(html("/", "student=Lena").contains("Who are you?") && html("/", "student=Lena~abc").contains("Who are you?"))
        assertTrue(go("/login", "", "POST", "student=lena").headers["Set-Cookie"] == null)
        val cookie = Html.unquote(go("/who", "", "POST", "n=Lena&secret=star").headers["Set-Cookie"]!!.substringBefore(';'))
        assertTrue(html("/", cookie).contains("Hi, Lena"))
        // PIN, with guesses slowed down.
        school("pic", mapOf("n" to "Zara", "p" to "dog", "a" to "teen", "s" to SignIn.hash("Zara", "4321")))
        repeat(5) { assertTrue(String(go("/who", "", "POST", "n=Zara&secret=0000").body).contains("not right")) }
        assertTrue(String(go("/who", "", "POST", "n=Zara&secret=4321").body).contains("Too many tries"))
        // A newcomer picks a picture and is there next time.
        assertTrue(go("/login", "", "POST", "student=Ama&pic=frog").headers["Set-Cookie"]!!.startsWith("student=Ama"))
        assertTrue(html("/").contains("data-name='Ama'"))

        // Pictures: the same packed form, shown by name and never by number.
        val packed = Json.parse("""{"i":"p1","T":"Count","M":"p","Q":[{"t":0,"p":"How many?","o":["2","3"],"a":1,"g":"apple*3,ball"},""" +
            """{"t":0,"p":"Which swims?","o":["Goat","Fish"],"a":1,"og":["goat","fish"]}]}""").asMap()!!
        val a = Assignments.unpack(packed)
        assertEquals(Json.dumps(packed["Q"], sortKeys = true), Json.dumps(Assignments.pack(a)["Q"], sortKeys = true))
        phone.importAssignment(packed, open = true)
        val form = html("/a/p1", "student=Ama")
        assertTrue(form.contains("<div class='pics n4' role='img' aria-label='Apple, Ball'>"))
        assertEquals(2, form.split("class='opts short'").size - 1)
        assertEquals("Fish", Assignments.keyText(Assignments.questions(a)[1]))
        assertEquals("0.75 (± 0.001)", Assignments.keyText(mapOf("type" to "number", "answer" to 0.75, "tolerance" to 0.001)))

        // Keep reading.
        go("/library/files/starter/fractions.html", "student=Ama")
        go("/library/files/starter/photosynthesis.html", "student=Ama", headers = mapOf("range" to "bytes=100-"))
        assertTrue(html("/library", "student=Ama").contains("class='keepread' href='/library/files/starter/fractions.html'"))
        assertTrue(!html("/library", "student=Kofi").contains("Keep reading"))

        // Right answers arrive with the marks and show only to that student.
        school("pic", mapOf("n" to "Lena", "p" to "cat", "a" to "mid"))
        val graded = Json.parse("""{"i":"g1","T":"Graded","Q":[{"t":0,"p":"Which?","o":["A","B"]}]}""").asMap()!!
        phone.importAssignment(graded, open = true)
        go("/a/g1", "student=Lena", "POST", "q1=0")
        val sub = phone.db.one("SELECT sub_id FROM submissions WHERE assignment_id = 'g1'")!!.str("sub_id")!!
        fun result(extra: Map<String, Any?>) = phone.onMessage(Message(1L, Protocol.RESULT, mapOf("r" to listOf(mapOf(
            "id" to sub, "sc" to 0L, "mx" to 1L, "ok" to 1L, "q" to mapOf("q1" to 0L)) + extra)), "hub"))
        result(emptyMap())
        assertTrue(html("/results", "student=Lena").contains("You said: A") && !html("/results", "student=Lena").contains("Right answer"))
        result(mapOf("an" to mapOf("q1" to "B")))
        assertTrue(html("/results", "student=Lena").contains("Right answer: B"))
    }

    /** Not-sure ticks and changed answers, as the Python phone sends them; and the teacher's note back. */
    @Test fun signalsAndTeacherNotes() {
        val a = mapOf("id" to "a1", "title" to "T", "mode" to "graded", "questions" to listOf(
            mapOf("id" to "q1", "type" to "choice", "prompt" to "A?", "options" to listOf("x", "y"), "points" to 1.0),
            mapOf("id" to "q2", "type" to "number", "prompt" to "B?", "points" to 1.0),
            mapOf("id" to "q3", "type" to "text", "prompt" to "C?", "points" to 1.0),
            mapOf("id" to "q4", "type" to "long", "prompt" to "D?", "points" to 1.0)))
        val answers = mapOf("q1" to "1", "q2" to "0.75", "q3" to "oxygen", "q4" to "x")
        val firsts = """{"q1":"0","q2":"3/4","q3":"Oxygen ","q4":"y","q9":"1"}"""
        assertEquals("""{"n":5,"f":{"q1":"0"}}""", Json.dumps(Assignments.signalsOf(a, setOf("q1", "q3"), firsts, answers)))
        assertEquals(emptyMap<String, Any?>(), Assignments.signalsOf(a, emptySet(), """{"q1":"7","q2":"abc"}""", answers))
        assertEquals(emptyMap<String, Any?>(), Assignments.signalsOf(a, emptySet(), "[not json", answers))

        val dir = tmp()
        val link = Link(0.0, 1)
        var radio: Messenger? = null
        val phone = Phone(File(dir, "phone"), "hub", "Test phone", { f, s -> JdbcDb(f.path, s) },
            { h -> Messenger(link.node("phone-1"), db(dir, "outbox.db"), h, retryS = 0.5).also { radio = it } }, batchS = 0.2)
        assertTrue(radio != null)
        phone.importAssignment(Assignments.pack(a + mapOf("version" to "v1")), open = true)
        fun ask(method: String, path: String, form: String = "") = phone.app.dispatch(Request(method, path, emptyMap(),
            mapOf("cookie" to "student=Ana", "content-type" to "application/x-www-form-urlencoded"), form.toByteArray(), "1.2.3.4"))
        assertTrue(String(ask("GET", "/a/a1").body).contains("name='unsure-q1'"))
        assertTrue(String(ask("POST", "/a/a1", "q1=1&unsure-q1=1").body).contains("name='unsure-q1' value='1' checked"))
        ask("POST", "/a/a1", "q1=1&q2=5&q3=oxygen&q4=z&unsure-q3=1&first=%7B%22q1%22%3A%220%22%7D")
        val id = phone.db.one("SELECT sub_id, signals FROM submissions")!!
        assertEquals("""{"n":4,"f":{"q1":"0"}}""", id.str("signals"))
        phone.onMessage(Message(1L, Protocol.RESULT, mapOf("r" to listOf(mapOf("id" to id.str("sub_id"), "sc" to 1, "mx" to 4, "ok" to 1,
            "q" to mapOf("q1" to 1), "nt" to mapOf("q4" to "<i>Say why</i>")))), "hub"))
        assertTrue(String(ask("GET", "/results").body).contains("From your teacher: &lt;i&gt;Say why&lt;/i&gt;"))
    }

    /** Translated questions and the teacher's recordings, as the Python phone. */
    @Test fun translationsAndRecordings() {
        assertEquals("3df9c89daaec75f4", Voices.key("samaki", "swa"))   // the Python test has the same one
        assertEquals(Voices.key("Ni kipi kinaogelea?", "swa"), Voices.key(" Ni kipi\u00a0 kinaogelea? ", "swa"))
        val only = mapOf(Voices.key("3", "eng") to File("x"), Voices.key("Name one.", "eng") to File("y"))
        assertEquals(null, Voices.find(only, "3", listOf("swa", "eng")))   // a number is read differently in each language
        assertEquals(Voices.key("Name one.", "eng"), Voices.find(only, "Name one.", listOf("swa", "eng")))
        val a = mapOf("id" to "a1", "title" to "Animals", "mode" to "graded", "version" to "v1", "instructions" to "Read.", "tr" to mapOf("swa" to mapOf("T" to "Wanyama")),
            "questions" to listOf(mapOf("id" to "q1", "type" to "choice", "prompt" to "Which can swim?", "options" to listOf("fish", "goat", "3"), "points" to 1.0, "info" to "Look.",
                "tr" to mapOf("swa" to mapOf("p" to "Ni kipi kinaogelea?", "o" to listOf("samaki", "mbuzi", ""), "n" to "Tazama.", "s" to "stamp"))),
                mapOf("id" to "q2", "type" to "text", "prompt" to "Name one.", "points" to 1.0)))
        val packed = Assignments.pack(a)
        assertEquals("""{"swa":{"p":"Ni kipi kinaogelea?","o":["samaki","mbuzi",""],"n":"Tazama."}}""", Json.dumps(packed["Q"].asList()!![0].asMap()!!["x"]))
        assertEquals("""{"swa":{"T":"Wanyama"}}""", Json.dumps(packed["X"]))
        val dir = tmp()
        val link = Link(0.0, 1)
        fun start() = Phone(File(dir, "phone"), "hub", "Test phone", { f, s -> JdbcDb(f.path, s) },
            { h -> Messenger(link.node("phone-1"), db(dir, "outbox.db"), h, retryS = 0.5) }, batchS = 0.2)
        val webm = byteArrayOf(0x1a, 0x45, 0xdf.toByte(), 0xa3.toByte()) + ByteArray(50)
        val key = Voices.key("Ni kipi kinaogelea?", "swa")
        File(dir, "phone/assignments/voice").mkdirs()
        File(dir, "phone/assignments/voice/$key.webm").writeBytes(webm)
        File(dir, "phone/assignments/voice/evil.webm").writeBytes(webm)
        File(dir, "phone/assignments/voice/${Voices.key("samaki", "swa")}.exe").writeBytes(webm)
        val phone = start()
        fun pack(code: String, tag: String) = mapOf("code" to code, "tag" to tag, "name" to code, "dir" to "ltr", "strings" to emptyMap<String, Any?>())
        phone.importLanguage(mapOf("school" to pack("eng", "en"), "second" to pack("swa", "sw"), "ages" to listOf("primary")))
        phone.importAssignment(packed, open = true)
        fun ask(path: String, lang: String, method: String = "GET", form: String = "") = phone.app.dispatch(Request(method, path, emptyMap(),
            mapOf("cookie" to "student=Ana; lang=$lang", "content-type" to "application/x-www-form-urlencoded"), form.toByteArray(), "1.2.3.4"))
        fun html(path: String, lang: String) = String(ask(path, lang).body, Charsets.UTF_8)
        assertTrue(html("/", "swa").contains("Wanyama") && html("/", "eng").contains("Animals"))
        val page = html("/a/a1", "swa")
        for (part in listOf("Ni kipi kinaogelea?", ">samaki<", "Tazama.", "Name one.", ">3<", "Read.", "data-voice='/voice/$key'")) assertTrue(part, page.contains(part))
        assertTrue(!page.contains("Which can swim?") && html("/a/a1", "eng").contains("Which can swim?") && !html("/a/a1", "eng").contains("data-voice='/voice/"))
        assertEquals(200, ask("/voice/$key", "swa").status)
        assertEquals("audio/webm", ask("/voice/$key", "swa").contentType)
        for (bad in listOf("0".repeat(16), "evil", Voices.key("samaki", "swa"))) assertEquals(404, ask("/voice/$bad", "swa").status)
        ask("/a/a1", "swa", "POST", "q1=1&q2=samaki")
        val id = phone.db.one("SELECT sub_id FROM submissions")!!.str("sub_id")
        phone.onMessage(Message(1L, Protocol.RESULT, mapOf("r" to listOf(mapOf("id" to id, "sc" to 1, "mx" to 2, "ok" to 1, "q" to mapOf("q1" to 0, "q2" to 1)))), "hub"))
        assertTrue(html("/results", "swa").contains("Wanyama") && html("/results", "swa").contains("mbuzi"))
    }

    /** The school's own pictures and each student's language, as the Python phone. */
    @Test fun ownPicturesAndLanguages() {
        val code = "11DpD7a7 02RR"   // the Python test has the same one
        assertEquals("<svg class='pic own' viewBox='0 0 240 240' aria-hidden='true'><path d='M81 201L81 50L159 50' fill='none' stroke='#8a4b1c' " +
            "style='stroke-width:8'/><path d='M120 120l.1 0' fill='none' stroke='#1d1d1b' style='stroke-width:14'/></svg>",
            OwnPics.svg(mapOf("n" to "door", "s" to code), ""))
        assertEquals(mapOf("n" to "our well", "l" to mapOf("por" to "o poço"), "b" to "goat", "s" to "11AA"), OwnPics.clean(mapOf("n" to " our  well ",
            "l" to mapOf("por" to "o poço", "X" to "no", "deu" to 3), "s" to "11AA 99ZZ 1 11'A 11A", "b" to "goat", "f" to "red", "z" to 1)))
        val photo = java.util.Base64.getEncoder().encodeToString(byteArrayOf(-1, -40, -1) + ByteArray(300) { 120 })
        for (bad in listOf(null, emptyList<Any>(), mapOf("s" to "11AA"), mapOf("n" to " "), mapOf("n" to "x", "b" to "none"), mapOf("n" to "x", "j" to "AAAA"),
            mapOf("n" to "x", "j" to "<svg>"), mapOf("n" to "x", "j" to java.util.Base64.getEncoder().encodeToString(byteArrayOf(-1, -40, -1) + ByteArray(30000) { 120 })))) {
            assertEquals(null, OwnPics.clean(bad))
        }
        val dir = tmp()
        val link = Link(0.0, 1)
        fun start() = Phone(File(dir, "phone"), "hub", "Test phone", { f, s -> JdbcDb(f.path, s) },
            { h -> Messenger(link.node("phone-1"), db(dir, "outbox.db"), h, retryS = 0.5) }, batchS = 0.2)
        var phone = start()
        val a = mapOf("id" to "a1", "title" to "T", "mode" to "graded", "version" to "v1", "questions" to listOf(mapOf("id" to "q1", "type" to "choice",
            "prompt" to "Which?", "options" to listOf("A", "B"), "points" to 1.0, "pic" to "my-well*2, goat", "opic" to listOf("my-shot", "goat"))))
        val packed = Assignments.pack(a) + mapOf("Px" to mapOf("my-well" to mapOf("n" to "our well", "s" to code), "my-shot" to mapOf("n" to "market"), "goat" to mapOf("n" to "x", "s" to "11AA")))
        phone.importAssignment(packed, open = true)
        fun ask(path: String, cookie: String, method: String = "GET", form: String = "") = phone.app.dispatch(Request(method, path, emptyMap(),
            mapOf("cookie" to cookie, "content-type" to "application/x-www-form-urlencoded"), form.toByteArray(), "1.2.3.4"))
        fun html(path: String, cookie: String) = String(ask(path, cookie).body, Charsets.UTF_8)
        assertEquals(3, html("/a/a1", "student=Ana").split("M81 201L81 50L159 50").size)
        assertTrue(html("/a/a1", "student=Ana").contains(">market</text>") && html("/a/a1", "student=Ana").contains("aria-label='our well, Goat'"))
        phone.importPictures(mapOf("my-shot" to mapOf("n" to "market", "j" to photo)))
        phone.importAssignment(packed)   // by radio again: the photo stays
        assertTrue(html("/a/a1", "student=Ana").contains("data:image/jpeg;base64,"))

        fun pack(code: String, tag: String) = mapOf("code" to code, "tag" to tag, "name" to code, "dir" to "ltr", "strings" to emptyMap<String, Any?>())
        phone.importLanguage(mapOf("school" to pack("eng", "en"), "second" to pack("swa", "sw"), "more" to emptyList<Any>(), "ages" to listOf("primary"),
            "students" to listOf(mapOf("n" to "Amara", "a" to "mid", "l" to "swa"), mapOf("n" to "Kofi", "a" to "mid"))))
        fun lang(cookie: String) = Regex("<html lang='([a-z]+)'").find(html("/", cookie))!!.groupValues[1]
        assertEquals(listOf("sw", "en", "en"), listOf(lang("student=Amara"), lang("student=Kofi"), lang("")))
        assertTrue(ask("/lang", "student=Kofi", "POST", "to=swa&back=/").headers["Set-Cookie"] == null)
        assertEquals(listOf("sw", "en", "sw"), listOf(lang("student=Kofi"), lang("student=Zoe"), lang("lang=swa")))
        phone = start()   // after a restart
        assertEquals("sw", lang("student=Kofi"))
        assertTrue(html("/a/a1", "student=Ana").contains("M81 201L81 50L159 50"))
        ask("/a/a1", "student=Kofi", "POST", "q1=0")
        assertEquals("{\"l\":\"swa\"}", phone.db.one("SELECT signals FROM submissions")!!.str("signals"))
    }

    /** Activities on the card run walled off, and libraries are served by name only: as the Python phone. */
    @Test fun activitiesAreWalledOff() {
        val dir = tmp()
        val link = Link(0.0, 1)
        val phone = Phone(File(dir, "phone"), "hub", "Test phone", { f, s -> JdbcDb(f.path, s) },
            { h -> Messenger(link.node("phone-1"), db(dir, "outbox.db"), h, retryS = 0.5) }, batchS = 0.2)
        fun zip(name: String, vararg entries: Pair<String, String>) {
            val f = File(phone.libraryDir, name).apply { parentFile.mkdirs() }
            java.util.zip.ZipOutputStream(f.outputStream()).use { z ->
                for ((n, text) in entries) { z.putNextEntry(java.util.zip.ZipEntry(n)); z.write(text.toByteArray()); z.closeEntry() }
            }
        }
        zip("kolibri/sim.zip", "index.html" to "<!doctype html><html><head><title>é</title></head></html>", "a/pic.png" to "x", "a/b/" to "")
        zip("tools/geogebra.zip", "GeoGebra/deployggb.js" to "x")
        File(phone.libraryDir, "index.json").writeText("""[{"title":"Sim","file":"kolibri/sim.zip","kind":"zip","shelf":"PhET","credit":"By PhET"},""" +
            """{"title":"GeoGebra","file":"tools/geogebra.zip","kind":"ggb","collection":"Tools"}]""")
        fun get(path: String, method: String = "GET", vararg extra: Pair<String, String>): Response {
            val (p, q) = path.split("?", limit = 2).let { it[0] to (it.getOrNull(1) ?: "") }
            return phone.app.dispatch(Request(method, p, Html.parseQs(q), mapOf("cookie" to "student=Ana", "host" to "192.168.43.1:8080") + extra,
                ByteArray(0), "1.2.3.4"))
        }
        fun html(path: String) = String(get(path).body, Charsets.UTF_8)
        assertTrue(html("/library").contains("href='/library/open?f=tools%2Fgeogebra.zip'"))
        assertTrue(html("/library/open?f=kolibri%2Fsim.zip").contains("sandbox='allow-scripts'") && html("/library/open?f=kolibri%2Fsim.zip").contains("By PhET"))
        val sim = get("/library/app/kolibri/sim.zip/index.html")
        val csp = sim.headers["Content-Security-Policy"]!!
        assertTrue(csp.startsWith("sandbox allow-scripts; default-src http://192.168.43.1:8080/library/app/kolibri/sim.zip/ ") && "allow-same-origin" !in csp)
        assertTrue(String(sim.body, Charsets.UTF_8).startsWith("<!doctype html><html><head><script>(function(){function store()"))
        assertTrue(String(sim.body, Charsets.UTF_8).contains("<title>é</title>"))
        assertEquals("image/png", get("/library/app/kolibri/sim.zip/a/pic.png").contentType)
        for (bad in listOf("kolibri/sim.zip/none.js", "kolibri/sim.zip/a/b/", "kolibri/other.zip/index.html", "../phone.db.zip/x")) {
            assertEquals(404, get("/library/app/$bad").status)
        }
        assertTrue("default-src http://localhost/" in get("/library/app/kolibri/sim.zip/index.html", "GET", "host" to "evil'; script-src *").headers["Content-Security-Policy"]!!)
        assertEquals(403, get("/status/reload", "POST", "origin" to "null").status)
        assertTrue(get("/library/app/tools/geogebra.zip/_start.html?app=x").headers["Content-Security-Policy"]!!.startsWith("sandbox allow-scripts allow-same-origin;"))
        assertTrue(html("/library/app/tools/geogebra.zip/_start.html?app=x").contains("\"appName\""))
        for (ok in listOf("katex/katex.min.js", "katex/fonts/KaTeX_Main-Regular.woff2", "signature_pad/signature_pad.umd.min.js",
            "opendyslexic/regular.woff2", "qrcode_generator/qrcode.js")) assertEquals(200, get("/site/vendor/$ok").status)
        for (bad in listOf("looks.css", "vendor/../looks.css", "vendor/NOTICES.md", "vendor/nope/x.js")) assertEquals(404, get("/site/$bad").status)
        assertTrue(html("/library").contains("class='keepread' href='/library/open?f=kolibri%2Fsim.zip'"))
        assertTrue(html("/join?u=javascript:alert(1)").contains("data-qr='http://192.168.43.1:8080'"))
    }

    /** The Kotlin and Python phones must serve the same student site: same stylesheet, same script. */
    @Test fun studentSiteMatchesPythonPhone() {
        val site = File(System.getProperty("meshlearn.repo"), "meshlearn/site")
        fun text(name: String) = File(site, name).readText().replace("\r\n", "\n")
        assertEquals(text("student.css") + text("looks.css"), Html.STUDENT_CSS)
        assertEquals(text("assignment.js"), Html.ASSIGNMENT_JS)
        assertEquals(text("countdown.js"), Html.COUNTDOWN_JS)
    }

    /** The school's words, from the same files the Python phone reads, give the same pages. */
    @Test fun phoneSpeaksTheSchoolLanguage() {
        val langDir = File(System.getProperty("meshlearn.repo"), "meshlearn/lang")
        fun pack(code: String) = Json.parse(File(langDir, "$code.json").readText(Charsets.UTF_8)).asMap()!!

        val es = Strings(pack("spa"))
        val today = java.util.Calendar.getInstance().apply { set(2026, java.util.Calendar.NOVEMBER, 15, 10, 30) }
        assertEquals("Para el vie 20 de nov" to "", Html.dueLabel("2026-11-20", false, today, es))
        assertEquals("unos 3 min", Html.minutesFor(listOf(mapOf("type" to "long")), es))
        assertEquals("1 pregunta", es.n("card.questions", 1))
        assertEquals("5 preguntas", es.n("card.questions", 5))
        assertEquals("Usa menos de 600 caracteres (ahora hay 640).", es.error("Keep this under 600 characters (640 now)."))
        assertEquals("Necesita práctica", es.level("Needs work"))
        val partial = Strings(mapOf("code" to "hau", "tag" to "ha", "strings" to mapOf("tab.home" to "Gida", "card.score" to "broken {nope}")))
        assertEquals("Gida", partial("tab.home"))
        assertEquals("Library", partial("tab.library"))
        assertEquals("Score 3 / 4", partial("card.score", "score" to 3, "max" to 4))

        // A memory card with Arabic as the school language and English as the second.
        val dir = tmp()
        val packDir = File(dir, "phone/assignments").apply { mkdirs() }
        File(packDir, "language.json").writeText(Json.dumps(mapOf("school" to pack("ara"), "second" to pack("eng"))), Charsets.UTF_8)
        val link = Link(0.0, 1)
        val phone = Phone(File(dir, "phone"), "hub", "Test phone", { f, s -> JdbcDb(f.path, s) },
            { h -> Messenger(link.node("phone-1"), db(dir, "outbox.db"), h, retryS = 0.5) }, batchS = 0.2)
        fun req(method: String, path: String, body: String = "", cookie: String = "") = Request(method, path, emptyMap(),
            mapOf("cookie" to cookie), body.toByteArray(), "192.168.43.10")
        val home = String(phone.app.dispatch(req("GET", "/")).body, Charsets.UTF_8)
        assertTrue(home.contains("<html lang='ar' dir='rtl'>") && home.contains("اكتب اسمك") && home.contains("action='/lang'"))
        val switched = phone.app.dispatch(req("POST", "/lang", "to=eng&back=%2Flibrary"))
        assertEquals(303, switched.status)
        assertEquals("/library", switched.headers["Location"])
        val cookie = switched.headers["Set-Cookie"]!!.substringBefore(";")
        val library = String(phone.app.dispatch(req("GET", "/library", cookie = cookie)).body, Charsets.UTF_8)
        assertTrue(library.contains("<html lang='en'>") && library.contains("Search the library") && library.contains(">العربية</button>"))
        // Unknown languages and other sites are ignored.
        val ignored = phone.app.dispatch(req("POST", "/lang", "to=xyz&back=https%3A%2F%2Fexample.org%2F"))
        assertEquals("/", ignored.headers["Location"])
        assertNull(ignored.headers["Set-Cookie"])
    }

    /** Moving between the memory card and the phone: work always moves, books only if asked, nothing lost on failure. */
    @Test fun storageMoveKeepsWorkSafe() {
        val root = tmp()
        val card = File(root, "card/meshlearn")
        val phone = File(root, "phone/meshlearn")
        fun put(dir: File, path: String, text: String) = File(dir, path).apply { parentFile.mkdirs(); writeText(text) }
        put(card, "phone.db", "students' work")
        put(card, "outbox.db", "waiting messages")
        put(card, "assignments/a1.json", "{}")
        put(card, "library/wiki.zim", "x".repeat(5000))
        put(phone, "library/copied-by-cable.epub", "book")   // already on the phone: must survive

        // Not enough room: nothing changes anywhere.
        try {
            StorageMove.move(card, phone, books = true, freeAtTarget = 100)
            throw AssertionError("should not fit")
        } catch (e: StorageMove.NotEnoughRoom) {
            assertTrue(e.needed > 5000)
        }
        assertTrue(File(card, "phone.db").exists() && !File(phone, "phone.db").exists())

        // Work and lessons only: books stay on the card, the old databases are kept as a backup.
        StorageMove.move(card, phone, books = false, freeAtTarget = Long.MAX_VALUE)
        assertEquals("students' work", File(phone, "phone.db").readText())
        assertEquals("waiting messages", File(phone, "outbox.db").readText())
        assertTrue(File(phone, "assignments/a1.json").exists())
        assertTrue(!File(card, "phone.db").exists() && File(card, "phone.db" + StorageMove.BACKUP_SUFFIX).exists())
        assertTrue(!File(card, "assignments").exists())
        assertTrue(File(card, "library/wiki.zim").exists() && !File(phone, "library/wiki.zim").exists())

        // Back to the card with the books: they merge with what is there, and the phone's backup stays put.
        put(phone, "phone.db", "more work")
        StorageMove.move(phone, card, books = true, freeAtTarget = Long.MAX_VALUE)
        assertEquals("more work", File(card, "phone.db").readText())
        assertTrue(File(card, "library/wiki.zim").exists() && File(card, "library/copied-by-cable.epub").exists())
        assertTrue(!File(phone, "library").exists())

        // A copy that fails part way leaves the old place untouched and still the one in use.
        File(root, "blocker").writeText("a file where a folder must go")
        try {
            StorageMove.move(card, File(root, "blocker"), books = false, freeAtTarget = Long.MAX_VALUE)
            throw AssertionError("should fail")
        } catch (e: Exception) {
            // expected: the copy could not be written
        }
        assertEquals("more work", File(card, "phone.db").readText())
        assertTrue(File(card, "outbox.db").exists() && File(card, "assignments/a1.json").exists())
    }

    @Test fun dueDatesInWords() {
        val today = java.util.Calendar.getInstance().apply { set(2026, java.util.Calendar.NOVEMBER, 15, 10, 30) }
        assertEquals("Due today" to "soon", Html.dueLabel("2026-11-15", false, today))
        assertEquals("Due tomorrow" to "soon", Html.dueLabel("2026-11-16", false, today))
        assertEquals("Due Fri, Nov 20" to "", Html.dueLabel("2026-11-20", false, today))
        assertEquals("Overdue · was due Fri, Oct 16" to "late", Html.dueLabel("2026-10-16", false, today))
        assertEquals("Was due Fri, Oct 16" to "done", Html.dueLabel("2026-10-16", true, today))
        assertEquals("" to "", Html.dueLabel("", false, today))
        assertEquals("about 6 min", Html.minutesFor(listOf("choice", "number", "number", "long").map { mapOf("type" to it) }))
    }

    @Test fun meshtasticPacketsRoundTrip() {
        val frame = byteArrayOf(1, 2, 3, -1)
        val toRadio = MeshtasticProto.toRadioPacket(frame, "!a1b2c3d4", channel = 3, hopLimit = 2, packetId = 0x01020304)
        // A FromRadio carrying the same MeshPacket (field 2 instead of 1) must parse back.
        val fromRadio = toRadio.copyOf().also { it[0] = 0x12 }
        val parsed = MeshtasticProto.parseFromRadio(fromRadio) as MeshtasticProto.FromRadio.Packet
        assertEquals(0xa1b2c3d4L, parsed.to)
        assertEquals(3, parsed.channel)
        assertEquals(256, parsed.portnum)
        assertEquals(frame.toList(), parsed.payload.toList())
        assertEquals("!a1b2c3d4", MeshtasticProto.nodeIdOf(0xa1b2c3d4L))
        val pyVectors = vectors["meshtastic"].asMap()
        if (pyVectors != null) {   // present when generated with the meshtastic package installed
            val dec = Base64.getDecoder()
            assertEquals(pyVectors["to_radio"], Base64.getEncoder().encodeToString(
                MeshtasticProto.toRadioPacket(dec.decode(pyVectors["payload"] as String), "!a1b2c3d4", 3, 2, true, 0x01020304)))
            val info = MeshtasticProto.parseFromRadio(dec.decode(pyVectors["my_info"] as String)) as MeshtasticProto.FromRadio.MyInfo
            assertEquals(0xa1b2c3d4L, info.nodeNum)
            val pkt = MeshtasticProto.parseFromRadio(dec.decode(pyVectors["from_radio"] as String)) as MeshtasticProto.FromRadio.Packet
            assertEquals(0x11223344L, pkt.from)
            assertEquals(pyVectors["payload"], Base64.getEncoder().encodeToString(pkt.payload))
            assertEquals(MeshtasticProto.wantConfig(42).toList(), dec.decode(pyVectors["want_config"] as String).toList())
        }
        assertNull(null)
    }
}
