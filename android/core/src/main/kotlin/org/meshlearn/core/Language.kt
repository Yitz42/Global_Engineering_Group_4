package org.meshlearn.core

import java.util.Calendar
import java.util.Date
import java.util.Locale

/**
 * The words students see, in the school's language: a port of language.py, giving the same words.
 * English comes from meshlearn/lang/eng.json, packed into the app by core/build.gradle.kts. A school's
 * words arrive with the course pack and may be partly translated: any word missing shows in English.
 */
class Strings(pack: Map<String, Any?>? = null) {
    val code: String
    val tag: String
    val name: String
    val dir: String
    private val strings: MutableMap<String, String> = LinkedHashMap(SOURCE_STRINGS)
    private val lists: MutableMap<String, List<String>> = LinkedHashMap(SOURCE_LISTS)

    init {
        val p = pack ?: SOURCE
        code = (p["code"] as? String)?.ifEmpty { null } ?: "eng"
        tag = (p["tag"] as? String)?.ifEmpty { null } ?: code
        name = (p["name"] as? String)?.ifEmpty { null } ?: code
        dir = if (p["dir"] == "rtl") "rtl" else "ltr"
        p["strings"].asMap()?.forEach { (key, text) ->
            if (key in strings && text is String && text.isNotBlank() && check(key, text).isEmpty()) strings[key] = text
        }
        val given = p["lists"].asMap()
        for ((key, want) in listOf("days" to 7, "months" to 12)) {
            val value = given?.get(key).asList() ?: continue
            if (value.size == want && value.all { it is String && it.isNotEmpty() }) lists[key] = value.map { it as String }
        }
    }

    operator fun invoke(key: String, vararg values: Pair<String, Any?>): String = fill(strings[key] ?: key, values.toMap())

    /** The .one or .other form, with {n} filled in. */
    fun n(key: String, count: Int, vararg values: Pair<String, Any?>): String =
        invoke(if (count == 1) "$key.one" else "$key.other", "n" to count, *values)

    /** Escaped words with ready-made HTML (like a link) put in at the placeholders. */
    fun html(key: String, vararg parts: Pair<String, String>): String = fill(Html.esc(strings[key] ?: key), parts.toMap())

    /** A day as in date.short: weekday, month and day in this language. */
    fun date(day: Date): String {
        val cal = Calendar.getInstance().apply { time = day }
        val weekday = (cal.get(Calendar.DAY_OF_WEEK) + 5) % 7   // Monday 0 … Sunday 6, as Python's weekday()
        return invoke("date.short", "weekday" to lists["days"]!![weekday],
            "month" to lists["months"]!![cal.get(Calendar.MONTH)], "day" to cal.get(Calendar.DAY_OF_MONTH))
    }

    /** A due date's short weekday and day number, or null when there is no date. */
    fun dayParts(due: String?): Pair<String, String>? {
        val m = Regex("(\\d{4})-(\\d{2})-(\\d{2})").matchEntire((due ?: "").trim().take(10)) ?: return null
        val (y, mo, d) = m.destructured
        val cal = Calendar.getInstance().apply { isLenient = false; clear(); set(y.toInt(), mo.toInt() - 1, d.toInt()) }
        val weekday = try { (cal.get(Calendar.DAY_OF_WEEK) + 5) % 7 } catch (e: IllegalArgumentException) { return null }
        return lists["days"]!![weekday] to d.toInt().toString()
    }

    fun whenText(ts: Double?): String {
        if (ts == null || ts == 0.0) return ""
        val cal = Calendar.getInstance().apply { timeInMillis = (ts * 1000).toLong() }
        return invoke("date.when", "month" to lists["months"]!![cal.get(Calendar.MONTH)],
            "day" to String.format(Locale.US, "%02d", cal.get(Calendar.DAY_OF_MONTH)),
            "time" to String.format(Locale.US, "%02d:%02d", cal.get(Calendar.HOUR_OF_DAY), cal.get(Calendar.MINUTE)))
    }

    fun level(name: String): String = LEVEL_KEYS[name]?.let { invoke(it) } ?: name

    /** An answer-checking message from Assignments.validateAnswers, in this language. */
    fun error(message: String): String {
        for ((pattern, key, names) in ERRORS) {
            val m = pattern.matchEntire(message) ?: continue
            return invoke(key, *names.mapIndexed { i, n -> n to m.groupValues[i + 1] }.toTypedArray())
        }
        return message
    }

    companion object {
        private val PLACEHOLDER = Regex("\\{(\\w+)\\}")
        private val SOURCE: Map<String, Any?> =
            Json.parse(Strings::class.java.getResource("/lang/eng.json")!!.readText(Charsets.UTF_8)).asMap()!!
        @Suppress("UNCHECKED_CAST")
        private val SOURCE_STRINGS: Map<String, String> = SOURCE["strings"].asMap()!! as Map<String, String>
        @Suppress("UNCHECKED_CAST")
        private val SOURCE_LISTS: Map<String, List<String>> = SOURCE["lists"].asMap()!! as Map<String, List<String>>
        val ENGLISH: Strings by lazy { Strings() }

        private val LEVEL_KEYS = mapOf("Not started" to "level.not_started", "Just started" to "level.just_started",
            "Needs work" to "level.needs_work", "Developing" to "level.developing", "Good" to "level.good",
            "Strong" to "level.strong")

        private val ERRORS = listOf(
            Triple(Regex("Please answer this question\\."), "error.blank", emptyList()),
            Triple(Regex("Choose one of the options\\."), "error.choose", emptyList()),
            Triple(Regex("Enter a number, like 12, 3\\.5, 3,5 or 1/2\\."), "error.number", emptyList()),
            Triple(Regex("Keep this under (\\d+) characters\\."), "error.too_long", listOf("n")),
            Triple(Regex("Keep this under (\\d+) characters \\((\\d+) now\\)\\."), "error.too_long_now", listOf("n", "now")),
        )

        fun placeholders(text: String): Set<String> = PLACEHOLDER.findAll(text).map { it.groupValues[1] }.toSet()

        /** "" if a translation is usable, else what is wrong with it. */
        fun check(key: String, text: String): String {
            val english = SOURCE_STRINGS[key] ?: return "not a word MeshLearn uses"
            val missing = placeholders(english) - placeholders(text)
            if (missing.isNotEmpty()) return "keep " + missing.sorted().joinToString(" and ") { "{$it}" }
            val extra = placeholders(text) - placeholders(english)
            if (extra.isNotEmpty()) return "remove " + extra.sorted().joinToString(" and ") { "{$it}" } + ": the phone has nothing to put there"
            return ""
        }

        fun fill(text: String, values: Map<String, Any?>): String = PLACEHOLDER.replace(text) { m ->
            val name = m.groupValues[1]
            if (values.containsKey(name)) pyStr(values[name]) else m.value
        }
    }
}
