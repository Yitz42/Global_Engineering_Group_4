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
        // Admin actions need the phone itself or the PIN.
        assertTrue(String(phone.app.dispatch(req("POST", "/status/reload")).body).contains("Only the teacher"))
        phone.stop(); hub.stop()
    }

    /** The Kotlin and Python phones must serve the same student site: same stylesheet, same script. */
    @Test fun studentSiteMatchesPythonPhone() {
        val webPy = File(System.getProperty("meshlearn.repo"), "meshlearn/web.py").readText().replace("\r\n", "\n")
        fun block(start: String) = webPy.substringAfter(start).substringBefore("\"\"\"").trim()
        assertEquals(block("STUDENT_CSS = \"\"\""), Html.STUDENT_CSS.trim())
        assertEquals(block("ASSIGNMENT_JS = r\"\"\""), Html.ASSIGNMENT_JS.trim())
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
