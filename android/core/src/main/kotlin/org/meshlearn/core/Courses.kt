package org.meshlearn.core

/** Courses and their units (port of courses.py, the parts the phone uses). */
object Courses {
    val OTHER: Map<String, Any?> = mapOf(
        "id" to "", "title" to "Other assignments", "description" to "Assignments not filed under a course.", "units" to emptyList<Any?>(),
    )

    class CourseError(msg: String) : IllegalArgumentException(msg)

    fun slug(text: String?, fallback: String = "x"): String {
        val out = (text ?: "").lowercase().replace(Regex("[^a-z0-9]+"), "-").trim('-').take(24)
        return out.ifEmpty { fallback }
    }

    private fun clip(v: Any?, n: Int) = (if (v == null) "" else pyStr(v)).trim().take(n)

    fun normalizeCourse(data: Any?): Map<String, Any?> {
        val d = data.asMap() ?: throw CourseError("a course must be an object")
        val title = clip(d["title"].takeIf { truthy(it) }, 80)
        if (title.isEmpty()) throw CourseError("the course needs a title")
        val courseId = slug((d["id"].takeIf { truthy(it) } ?: title).toString(), "course")
        val units = ArrayList<Any?>()
        val seen = HashSet<String>()
        (d["units"].asList() ?: emptyList()).forEachIndexed { i, raw ->
            val u = if (raw is String) mapOf("title" to raw) else raw.asMap() ?: return@forEachIndexed
            val unitTitle = clip(u["title"].takeIf { truthy(it) }, 80)
            if (unitTitle.isEmpty()) return@forEachIndexed
            var unitId = slug((u["id"].takeIf { truthy(it) } ?: unitTitle).toString(), "unit${i + 1}")
            while (unitId in seen) unitId += "-2"
            seen.add(unitId)
            val unit = linkedMapOf<String, Any?>("id" to unitId, "title" to unitTitle)
            val read = clip(u["read"].takeIf { truthy(it) }, 1000)
            if (read.isNotEmpty()) unit["read"] = read.take(60)
            units.add(unit)
        }
        val course = linkedMapOf<String, Any?>(
            "id" to courseId, "title" to title, "description" to clip(d["description"].takeIf { truthy(it) }, 240), "units" to units,
        )
        if (truthy(d["comingSoon"])) course["comingSoon"] = true
        return course
    }

    fun units(course: Map<String, Any?>?): List<Map<String, Any?>> =
        (course?.get("units").asList() ?: emptyList()).mapNotNull { it.asMap() }

    fun unitOf(course: Map<String, Any?>?, unitId: String): Map<String, Any?>? = units(course).firstOrNull { it["id"] == unitId }
}

/**
 * Comprehension levels, as in comprehension.py (the parts the student site shows). A unit's
 * score is the average of the 20 most recent question scores in it; a course's is the
 * average of its units that have answers.
 */
object Comprehension {
    private const val RECENT = 20

    fun levelOf(score: Double?, answered: Int): String = when {
        score == null || answered == 0 -> "Not started"
        answered < 3 -> "Just started"
        score >= 85 -> "Strong"
        score >= 65 -> "Good"
        score >= 40 -> "Developing"
        else -> "Needs work"
    }

    /** Python's round(): halves go to the even neighbour. */
    fun pyRound(x: Double): Long = Math.rint(x).toLong()

    private class Scored(val course: String, val unit: String, val score: Double, val at: Double)

    private fun d(v: Any?): Double? = (v as? Number)?.toDouble()

    private fun questionScores(records: List<Map<String, Any?>>): List<Scored> {
        val out = ArrayList<Scored>()
        val practice = LinkedHashMap<Pair<String, String>, MutableList<Map<String, Any?>>>()
        for (r in records) {
            if (r["mode"] == "practice") {
                practice.getOrPut(r["student"].toString() to r["aid"].toString()) { ArrayList() }.add(r)
                continue
            }
            val scores = r["scores"].asMap() ?: emptyMap()
            for (q in Assignments.questions(r)) {
                val got = d(scores[q["id"]]) ?: continue
                val points = d(q["points"]) ?: 0.0
                if (points == 0.0) continue
                out.add(Scored(r["course"]?.toString() ?: "", r["unit"]?.toString() ?: "", (got / points).coerceIn(0.0, 1.0), d(r["at"]) ?: 0.0))
            }
        }
        for (tries in practice.values) {
            tries.sortBy { d(it["at"]) ?: 0.0 }
            val first = tries[0]
            for (q in Assignments.questions(first)) {
                val qid = q["id"]
                val points = d(q["points"]) ?: 0.0
                val rightAt = tries.indexOfFirst { t ->
                    val s = d((t["scores"].asMap() ?: emptyMap())[qid])
                    s != null && s >= points
                }.takeIf { it >= 0 }
                if (rightAt == null && tries.all { d((it["scores"].asMap() ?: emptyMap())[qid]) == null }) continue
                val score = when (rightAt) { null -> 0.0; 0 -> 1.0; 1 -> 0.6; else -> 0.3 }
                val last = if (rightAt != null) tries[rightAt] else tries.last()
                out.add(Scored(first["course"]?.toString() ?: "", first["unit"]?.toString() ?: "", score, d(last["at"]) ?: 0.0))
            }
        }
        out.sortBy { it.at }
        return out
    }

    class UnitSummary(val id: String, val number: Int, val title: String, val answered: Int, val score: Long?, val level: String)
    class Summary(val score: Long?, val level: String, val answered: Int, val units: List<UnitSummary>)

    /** One student's summary for one course. */
    fun summarize(records: List<Map<String, Any?>>, course: Map<String, Any?>): Summary {
        val cid = course["id"]?.toString() ?: ""
        val scores = questionScores(records).filter { it.course == cid }
        val mine = records.filter { (it["course"]?.toString() ?: "") == cid }
        val courseUnits = Courses.units(course)
        val unitIds = if (courseUnits.isNotEmpty()) courseUnits.map { it["id"].toString() }
        else mine.map { it["unit"]?.toString() ?: "" }.toSortedSet().toList()
        val units = unitIds.mapIndexed { i, uid ->
            val uq = scores.filter { it.unit == uid }.takeLast(RECENT)
            val m = if (uq.isEmpty()) null else uq.sumOf { it.score } / uq.size
            val title = courseUnits.firstOrNull { it["id"] == uid }?.get("title")?.toString() ?: uid.ifEmpty { "No unit" }
            UnitSummary(uid, i + 1, title, uq.size, m?.let { pyRound(it * 100) }, levelOf(m?.let { it * 100 }, uq.size))
        }
        val scored = units.mapNotNull { it.score }
        val score = if (scored.isEmpty()) null else pyRound(scored.sum().toDouble() / scored.size)
        val answered = units.sumOf { it.answered }
        return Summary(score, levelOf(score?.toDouble(), answered), answered, units)
    }
}
