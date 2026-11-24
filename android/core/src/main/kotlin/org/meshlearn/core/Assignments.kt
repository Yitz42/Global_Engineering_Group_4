package org.meshlearn.core

import java.security.SecureRandom
import java.util.zip.CRC32

/**
 * The parts of meshlearn/assignments.py the phone needs: the compact radio form, the version
 * stamp shared with the hub, answer validation, and practice-mode feedback.
 *
 * Assignments stay as parsed-JSON maps, exactly as in Python, so the two stay easy to compare.
 */
object Assignments {
    val QUESTION_TYPES = listOf("choice", "number", "text", "long")
    private val KEY_FIELDS = listOf("answer", "tolerance", "accept")
    const val MAX_TEXT_CHARS = 200
    const val MAX_LONG_CHARS = 1500

    private val random = SecureRandom()

    fun newId(prefix: String = ""): String {
        val b = ByteArray(4)
        random.nextBytes(b)
        return prefix + b.joinToString("") { "%02x".format(it.toInt() and 0xFF) }
    }

    private val TOP_KEYS = linkedMapOf(
        "id" to "i", "title" to "T", "subject" to "S", "instructions" to "I", "due" to "D", "mode" to "M",
        "version" to "v", "course" to "C", "unit" to "U", "course_title" to "Ct", "unit_title" to "Ut", "unit_no" to "Un",
    )
    private val OPTIONAL_TOP = listOf("version", "course_title", "unit_title", "unit_no")
    private val Q_KEYS = linkedMapOf(
        "id" to "i", "prompt" to "p", "options" to "o", "answer" to "a", "tolerance" to "l", "accept" to "c",
        "points" to "s", "max_chars" to "m", "pic" to "g", "opic" to "og",
    )
    private val Q_DEFAULTS = mapOf<String, Any>("points" to 1.0, "tolerance" to 0.0, "max_chars" to 600L)

    /** Python's `value == default` for numbers (1 == 1.0). */
    private fun pyEquals(a: Any?, b: Any?): Boolean =
        if (a is Number && b is Number) a.toDouble() == b.toDouble() else a == b

    private fun num(v: Any?): Any? = if (v is Double && v == Math.floor(v) && !v.isInfinite() && Math.abs(v) < 1e18) v.toLong() else v

    fun pack(assignment: Map<String, Any?>): Map<String, Any?> {
        val out = LinkedHashMap<String, Any?>()
        for ((key, short) in TOP_KEYS) {
            val value = assignment[key]
            if (key == "mode") {
                if (value == "practice") out[short] = "p"
            } else if (value != null && value != "") {
                out[short] = value
            }
        }
        val questions = ArrayList<Any?>()
        var infoBefore = ""
        (assignment["questions"].asList() ?: emptyList()).forEachIndexed { i, item ->
            val q = item.asMap() ?: return@forEachIndexed
            val cq = LinkedHashMap<String, Any?>()
            cq["t"] = QUESTION_TYPES.indexOf(q["type"]).toLong()
            for ((key, short) in Q_KEYS) {
                if (!q.containsKey(key)) continue
                val value = q[key]
                if (key == "id" && value == "q${i + 1}") continue
                if (key in Q_DEFAULTS && pyEquals(value, Q_DEFAULTS[key])) continue
                cq[short] = num(value)
            }
            val info = q["info"]?.toString() ?: ""   // several questions about one text: it travels once, then 1 = "the same again"
            if (info.isNotEmpty()) cq["n"] = if (info == infoBefore) 1L else info
            infoBefore = info
            packTr(q["tr"]).takeIf { it.isNotEmpty() }?.let { cq["x"] = it }
            questions.add(cq)
        }
        out["Q"] = questions
        packTr(assignment["tr"]).takeIf { it.isNotEmpty() }?.let { out["X"] = it }
        return out
    }

    /** Translations without their stamps, and nothing empty. */
    private fun packTr(tr: Any?): Map<String, Any?> = (tr.asMap() ?: emptyMap()).mapValues { (_, t) ->
        (t.asMap() ?: emptyMap()).filter { (k, v) -> k != "s" && truthy(v) }
    }.filterValues { it.isNotEmpty() }

    /** The assignment as a student of this language sees it: translated where it can be, the original elsewhere. */
    fun localized(assignment: Map<String, Any?>, code: String): MutableMap<String, Any?> {
        val out = LinkedHashMap(assignment)
        val top = assignment["tr"].asMap()?.get(code).asMap() ?: emptyMap()
        val qs = questions(assignment)
        if (top.isEmpty() && qs.none { it["tr"].asMap()?.containsKey(code) == true }) return out
        fun text(v: Any?): String? = (v as? String)?.takeIf { it.isNotEmpty() }
        text(top["T"])?.let { out["title"] = it }
        if (truthy(assignment["instructions"])) text(top["I"])?.let { out["instructions"] = it }
        out["questions"] = qs.map { original ->
            val q = LinkedHashMap(original)
            val t = original["tr"].asMap()?.get(code).asMap() ?: emptyMap()
            text(t["p"])?.let { q["prompt"] = it }
            if (truthy(q["info"])) text(t["n"])?.let { q["info"] = it }
            val done = t["o"].asList()
            val options = q["options"].asList()
            if (done != null && options != null) q["options"] = options.mapIndexed { j, o -> text(done.getOrNull(j)) ?: o }
            q
        }
        return out
    }

    fun unpack(packed: Map<String, Any?>): Map<String, Any?> {
        val out = LinkedHashMap<String, Any?>()
        for ((key, short) in TOP_KEYS) out[key] = packed[short] ?: ""
        out["mode"] = if (packed["M"] == "p") "practice" else "graded"
        val questions = ArrayList<Any?>()
        var infoBefore = ""
        (packed["Q"].asList() ?: emptyList()).forEachIndexed { i, item ->
            val cq = item.asMap() ?: emptyMap()
            val type = QUESTION_TYPES[((cq["t"] as? Number)?.toInt() ?: 0)]
            val q = LinkedHashMap<String, Any?>()
            q["id"] = (cq["i"] as? String)?.takeIf { it.isNotEmpty() } ?: "q${i + 1}"
            q["type"] = type
            q["prompt"] = cq["p"] ?: ""
            q["points"] = (cq["s"] ?: 1.0).toDoubleOrNull() ?: 1.0
            if ("o" in cq) q["options"] = (cq["o"].asList() ?: emptyList()).toList()
            if ("a" in cq) q["answer"] = if (type == "choice") (cq["a"] as Number).toLong() else (cq["a"] as Number).toDouble()
            if (type == "number" && "a" in cq) q["tolerance"] = (cq["l"] ?: 0.0).toDoubleOrNull() ?: 0.0
            if ("c" in cq) q["accept"] = (cq["c"].asList() ?: emptyList()).toList()
            if (type == "long") q["max_chars"] = ((cq["m"] ?: 600L) as Number).toLong()
            if (truthy(cq["g"])) q["pic"] = cq["g"].toString()
            cq["og"].asList()?.let { list -> q["opic"] = list.map { it?.toString() ?: "" } }
            val info = (cq["n"] as? String) ?: if (truthy(cq["n"])) infoBefore else ""
            if (info.isNotEmpty()) q["info"] = info
            infoBefore = info
            cq["x"].asMap()?.let { q["tr"] = it }
            questions.add(q)
        }
        out["questions"] = questions
        packed["X"].asMap()?.let { out["tr"] = it }
        for (key in OPTIONAL_TOP) if (!truthy(out[key])) out.remove(key)
        return out
    }

    /** The answer key removed unless in practice mode. */
    fun publicView(assignment: Map<String, Any?>): Map<String, Any?> {
        val view = LinkedHashMap(assignment)
        if (view["mode"] != "practice") {
            view["questions"] = (assignment["questions"].asList() ?: emptyList()).map { q ->
                LinkedHashMap(q.asMap() ?: emptyMap()).apply { KEY_FIELDS.forEach { remove(it) } }
            }
        }
        return view
    }

    /** Content stamp shared by hub and phone (the due date is left out). Must match Python exactly. */
    fun versionOf(assignment: Map<String, Any?>): String {
        val view = LinkedHashMap(publicView(assignment))
        for (key in listOf("due", "version", "course_title", "unit_title", "unit_no")) view.remove(key)
        val canonical = Json.dumps(pack(view), sortKeys = true)
        val crc = CRC32()
        crc.update(canonical.toByteArray(Charsets.UTF_8))
        return "%08x".format(crc.value)
    }

    fun questions(assignment: Map<String, Any?>): List<Map<String, Any?>> =
        (assignment["questions"].asList() ?: emptyList()).mapNotNull { it.asMap() }

    private const val MAX_FIRST_CHARS = 40
    private const val MAX_UNSURE = 62
    private val NUMBER_RE = Regex("^[-+]?(\\d+([.,]\\d*)?|[.,]\\d+)([eE][-+]?\\d+)?$")

    /** Accept "3.5", "3,5" (comma decimals are common in Brazil and Germany) or "1/2". */
    fun parseNumber(input: String?): Double? {
        val text = (input ?: "").trim().replace(" ", "")
        if ("/" in text) {
            val a = parseNumber(text.substringBefore("/"))
            val b = parseNumber(text.substringAfter("/"))
            if (a == null || b == null || b == 0.0) return null
            return a / b
        }
        if (!NUMBER_RE.matches(text)) return null
        return text.replace(",", ".").toDouble()
    }

    private fun isDigits(s: String) = s.isNotEmpty() && s.all { it.isDigit() }
    private fun chars(s: String) = s.codePointCount(0, s.length)

    /** Check answers are complete and well-formed. Returns (answers, errors). */
    fun validateAnswers(assignment: Map<String, Any?>, raw: Map<String, String>): Pair<Map<String, String>, Map<String, String>> {
        val answers = LinkedHashMap<String, String>()
        val errors = LinkedHashMap<String, String>()
        for (q in questions(assignment)) {
            val id = q["id"].toString()
            val value = (raw[id] ?: "").trim()
            if (value.isEmpty()) {
                errors[id] = "Please answer this question."
                continue
            }
            when (q["type"]) {
                "choice" -> {
                    val n = (q["options"].asList() ?: emptyList()).size
                    if (!isDigits(value) || (value.toIntOrNull() ?: -1) !in 0 until n) {
                        errors[id] = "Choose one of the options."; continue
                    }
                }
                "number" -> if (parseNumber(value) == null) {
                    errors[id] = "Enter a number, like 12, 3.5, 3,5 or 1/2."; continue
                }
                "text" -> if (chars(value) > MAX_TEXT_CHARS) {
                    errors[id] = "Keep this under $MAX_TEXT_CHARS characters."; continue
                }
                "long" -> {
                    val limit = (q["max_chars"] as? Number)?.toInt() ?: 600
                    if (chars(value) > limit) {
                        errors[id] = "Keep this under $limit characters (${chars(value)} now)."; continue
                    }
                }
            }
            answers[id] = value
        }
        return answers to errors
    }

    private fun sameAnswer(q: Map<String, Any?>, a: String, b: String): Boolean = when (q["type"]) {
        "number" -> {
            val x = parseNumber(a)
            val y = parseNumber(b)
            x != null && y != null && Math.abs(x - y) <= 1e-9 * maxOf(1.0, Math.abs(x), Math.abs(y))
        }
        "text" -> normalizeText(a) == normalizeText(b)
        else -> a.trim() == b.trim()
    }

    /** What goes with a student's answers: "n", the questions they ticked as not sure (one bit each, in order), and
     *  "f", their first answer where they changed it after moving on. As signals_of in assignments.py. */
    fun signalsOf(assignment: Map<String, Any?>, unsure: Set<String>, firstJson: String, answers: Map<String, String>): Map<String, Any?> {
        val out = LinkedHashMap<String, Any?>()
        val qs = questions(assignment)
        var mask = 0L
        qs.take(MAX_UNSURE).forEachIndexed { i, q -> if (q["id"].toString() in unsure) mask = mask or (1L shl i) }
        if (mask != 0L) out["n"] = mask
        val firsts = try { Json.parse(firstJson.ifEmpty { "{}" }).asMap() } catch (e: Exception) { null } ?: emptyMap()
        val changed = LinkedHashMap<String, Any?>()
        for (q in qs) {
            val id = q["id"].toString()
            val value = (firsts[id] as? String)?.trim() ?: continue
            if (q["type"] == "long" || value.isEmpty()) continue
            val cut = value.substring(0, value.offsetByCodePoints(0, minOf(MAX_FIRST_CHARS, chars(value))))
            if (q["type"] == "choice" && (!isDigits(cut) || (cut.toIntOrNull() ?: Int.MAX_VALUE) >= (q["options"].asList() ?: emptyList()).size)) continue
            if (q["type"] == "number" && parseNumber(cut) == null) continue
            if (!sameAnswer(q, cut, answers[id] ?: "")) changed[id] = cut
        }
        if (changed.isNotEmpty()) out["f"] = changed
        return out
    }

    private fun normalizeText(value: String): String =
        value.lowercase().split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ").trim(' ', '.', '!', '?')

    /** Points earned, or null when the teacher must grade it. */
    fun gradeQuestion(q: Map<String, Any?>, value: String): Double? {
        val points = (q["points"] as? Number)?.toDouble() ?: 1.0
        return when (q["type"]) {
            "long" -> null
            "choice" -> if (isDigits(value) && value.toLongOrNull() == (q["answer"] as? Number)?.toLong()) points else 0.0
            "number" -> {
                val got = parseNumber(value) ?: return 0.0
                val want = (q["answer"] as? Number)?.toDouble() ?: return 0.0
                val tol = (q["tolerance"] as? Number)?.toDouble() ?: 0.0
                if (Math.abs(got - want) <= tol + 1e-9) points else 0.0
            }
            "text" -> {
                val accepted = (q["accept"].asList() ?: emptyList()).map { normalizeText(it.toString()) }.toSet()
                if (normalizeText(value) in accepted) points else 0.0
            }
            else -> null
        }
    }

    /** For practice mode on the phone: true/false per auto-gradable question, null for the rest. */
    fun practiceFeedback(assignment: Map<String, Any?>, answers: Map<String, String>): Map<String, Boolean?> {
        val out = LinkedHashMap<String, Boolean?>()
        for (q in questions(assignment)) {
            val id = q["id"].toString()
            out[id] = if (q.containsKey("answer") || q.containsKey("accept")) {
                gradeQuestion(q, answers[id] ?: "")?.let { it > 0 }
            } else null
        }
        return out
    }

    /** The right answer of an auto-marked question, in words. */
    fun keyText(q: Map<String, Any?>): String {
        val options = q["options"].asList() ?: emptyList()
        val answer = q["answer"]
        return when (q["type"]) {
            "choice" -> (answer as? Number)?.toInt()?.let { options.getOrNull(it)?.toString() } ?: ""
            "number" -> if (answer !is Number) "" else {
                val tol = (q["tolerance"] as? Number)?.toDouble() ?: 0.0
                Html.g(answer.toDouble()) + (if (tol != 0.0) " (± ${Html.g(tol)})" else "")
            }
            "text" -> (q["accept"].asList() ?: emptyList()).joinToString(" or ")
            else -> ""
        }
    }
}
