package org.meshlearn.core

import java.math.BigDecimal

/**
 * Minimal JSON that behaves like Python's json module, so the phone and the Python hub
 * agree byte for byte where it matters (the assignment version stamp).
 *
 * Values are plain Kotlin types, mirroring Python's: Map (insertion-ordered), List,
 * String, Long (integer literals), Double (anything with a point or exponent), Boolean, null.
 */
object Json {
    class ParseError(msg: String) : IllegalArgumentException(msg)

    fun parse(text: String): Any? {
        val p = Parser(text)
        p.skipWs()
        val v = p.value()
        p.skipWs()
        if (!p.done()) throw ParseError("extra data at ${p.pos}")
        return v
    }

    /**
     * Like Python's json.dumps(value, separators=(",", ":"), ensure_ascii=False[, sort_keys=True]).
     * With [spaced], uses Python's default separators (", " and ": ").
     */
    fun dumps(value: Any?, sortKeys: Boolean = false, spaced: Boolean = false): String {
        val sb = StringBuilder()
        write(sb, value, sortKeys, if (spaced) ", " else ",", if (spaced) ": " else ":")
        return sb.toString()
    }

    private fun write(sb: StringBuilder, v: Any?, sortKeys: Boolean, comma: String, colon: String) {
        when (v) {
            null -> sb.append("null")
            is String -> writeString(sb, v)
            is Boolean -> sb.append(if (v) "true" else "false")
            is Int, is Long, is Short, is Byte -> sb.append(v.toString())
            is Double -> sb.append(pyFloatRepr(v))
            is Float -> sb.append(pyFloatRepr(v.toDouble()))
            is Map<*, *> -> {
                sb.append('{')
                val entries = v.entries.map { it.key.toString() to it.value }
                val ordered = if (sortKeys) entries.sortedBy { it.first } else entries
                ordered.forEachIndexed { i, (k, value) ->
                    if (i > 0) sb.append(comma)
                    writeString(sb, k)
                    sb.append(colon)
                    write(sb, value, sortKeys, comma, colon)
                }
                sb.append('}')
            }
            is Iterable<*> -> {
                sb.append('[')
                v.forEachIndexed { i, item ->
                    if (i > 0) sb.append(comma)
                    write(sb, item, sortKeys, comma, colon)
                }
                sb.append(']')
            }
            is Array<*> -> write(sb, v.toList(), sortKeys, comma, colon)
            else -> writeString(sb, v.toString())
        }
    }

    private fun writeString(sb: StringBuilder, s: String) {
        sb.append('"')
        for (c in s) {
            when (c) {
                '"' -> sb.append("\\\"")
                '\\' -> sb.append("\\\\")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                '\b' -> sb.append("\\b")
                '\u000c' -> sb.append("\\f")
                else -> if (c < ' ') sb.append(String.format("\\u%04x", c.code)) else sb.append(c)
            }
        }
        sb.append('"')
    }

    /** Python's repr() for a float: shortest digits, ".0" on whole numbers, exponent outside 1e-4..1e16. */
    fun pyFloatRepr(d: Double): String {
        if (d.isNaN()) return "NaN"
        if (d.isInfinite()) return if (d > 0) "Infinity" else "-Infinity"
        if (d == 0.0) return if (1.0 / d < 0) "-0.0" else "0.0"
        val bd = BigDecimal(d.toString()).stripTrailingZeros()
        val digits = bd.unscaledValue().abs().toString()
        val exp = digits.length - 1 - bd.scale()      // decimal exponent of the first digit
        val sign = if (d < 0) "-" else ""
        return if (exp in -4..15) {
            val plain = bd.abs().toPlainString()
            sign + if (plain.contains('.')) plain else "$plain.0"
        } else {
            val mantissa = if (digits.length == 1) digits else digits[0] + "." + digits.substring(1)
            val e = if (exp < 0) "-" + (-exp).toString().padStart(2, '0') else "+" + exp.toString().padStart(2, '0')
            "$sign${mantissa}e$e"
        }
    }

    private class Parser(val s: String) {
        var pos = 0
        fun done() = pos >= s.length
        fun skipWs() { while (pos < s.length && s[pos] in " \t\r\n") pos++ }

        fun value(): Any? {
            if (done()) throw ParseError("unexpected end")
            return when (val c = s[pos]) {
                '{' -> obj()
                '[' -> arr()
                '"' -> str()
                't' -> lit("true", true)
                'f' -> lit("false", false)
                'n' -> lit("null", null)
                else -> if (c == '-' || c.isDigit()) num() else if (s.startsWith("NaN", pos)) lit("NaN", Double.NaN)
                else if (s.startsWith("Infinity", pos)) lit("Infinity", Double.POSITIVE_INFINITY)
                else throw ParseError("unexpected '$c' at $pos")
            }
        }

        private fun lit(word: String, v: Any?): Any? {
            if (!s.startsWith(word, pos)) throw ParseError("bad literal at $pos")
            pos += word.length
            return v
        }

        private fun obj(): Map<String, Any?> {
            val out = LinkedHashMap<String, Any?>()
            pos++; skipWs()
            if (pos < s.length && s[pos] == '}') { pos++; return out }
            while (true) {
                skipWs()
                if (done() || s[pos] != '"') throw ParseError("expected key at $pos")
                val k = str()
                skipWs()
                if (done() || s[pos] != ':') throw ParseError("expected ':' at $pos")
                pos++; skipWs()
                out[k] = value()
                skipWs()
                if (done()) throw ParseError("unterminated object")
                when (s[pos]) {
                    ',' -> pos++
                    '}' -> { pos++; return out }
                    else -> throw ParseError("expected ',' or '}' at $pos")
                }
            }
        }

        private fun arr(): List<Any?> {
            val out = ArrayList<Any?>()
            pos++; skipWs()
            if (pos < s.length && s[pos] == ']') { pos++; return out }
            while (true) {
                skipWs()
                out.add(value())
                skipWs()
                if (done()) throw ParseError("unterminated array")
                when (s[pos]) {
                    ',' -> pos++
                    ']' -> { pos++; return out }
                    else -> throw ParseError("expected ',' or ']' at $pos")
                }
            }
        }

        private fun str(): String {
            val sb = StringBuilder()
            pos++
            while (true) {
                if (done()) throw ParseError("unterminated string")
                val c = s[pos++]
                when (c) {
                    '"' -> return sb.toString()
                    '\\' -> {
                        if (done()) throw ParseError("bad escape")
                        when (val e = s[pos++]) {
                            '"' -> sb.append('"'); '\\' -> sb.append('\\'); '/' -> sb.append('/')
                            'b' -> sb.append('\b'); 'f' -> sb.append('\u000c'); 'n' -> sb.append('\n')
                            'r' -> sb.append('\r'); 't' -> sb.append('\t')
                            'u' -> {
                                if (pos + 4 > s.length) throw ParseError("bad \\u escape")
                                sb.append(s.substring(pos, pos + 4).toInt(16).toChar()); pos += 4
                            }
                            else -> throw ParseError("bad escape \\$e")
                        }
                    }
                    else -> sb.append(c)
                }
            }
        }

        private fun num(): Any {
            val start = pos
            if (s[pos] == '-') pos++
            if (s.startsWith("Infinity", pos)) { pos += 8; return Double.NEGATIVE_INFINITY }
            while (pos < s.length && s[pos].isDigit()) pos++
            var isFloat = false
            if (pos < s.length && s[pos] == '.') { isFloat = true; pos++; while (pos < s.length && s[pos].isDigit()) pos++ }
            if (pos < s.length && (s[pos] == 'e' || s[pos] == 'E')) {
                isFloat = true; pos++
                if (pos < s.length && (s[pos] == '+' || s[pos] == '-')) pos++
                while (pos < s.length && s[pos].isDigit()) pos++
            }
            val text = s.substring(start, pos)
            if (isFloat) return text.toDouble()
            return text.toLongOrNull() ?: text.toDouble()
        }
    }
}

// -- small helpers for reading parsed JSON the way the Python code does with dict.get --------

@Suppress("UNCHECKED_CAST")
fun Any?.asMap(): Map<String, Any?>? = this as? Map<String, Any?>

@Suppress("UNCHECKED_CAST")
fun Any?.asList(): List<Any?>? = this as? List<Any?>

/** Python truthiness. */
fun truthy(v: Any?): Boolean = when (v) {
    null -> false
    is Boolean -> v
    is Number -> v.toDouble() != 0.0
    is String -> v.isNotEmpty()
    is Collection<*> -> v.isNotEmpty()
    is Map<*, *> -> v.isNotEmpty()
    else -> true
}

/** str(value) for the simple values that appear in messages. */
fun pyStr(v: Any?): String = when (v) {
    null -> "None"
    is Double -> Json.pyFloatRepr(v)
    is Boolean -> if (v) "True" else "False"
    else -> v.toString()
}

fun Any?.toDoubleOrNull(): Double? = when (this) {
    is Number -> this.toDouble()
    is String -> this.trim().toDoubleOrNull()
    else -> null
}
