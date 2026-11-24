package org.meshlearn.core

import org.meshlearn.core.Html.esc

/**
 * Pictures a school makes itself: drawings small enough for the radio, changed library pictures, and photos.
 * A port of ownpics.py (the format is described there), giving the same pictures.
 */
object OwnPics {
    private val ALPHABET = (35..126).map { it.toChar() }.filter { it !in "&'<>\\" }.joinToString("")
    private val STEPS = ALPHABET.length - 1
    private const val SIZE = 240
    private val COLOURS = listOf("#1d1d1b", "#8a4b1c", "#d0382c", "#1f7a3a", "#1f5fbf")
    private val WIDTHS = listOf(4, 8, 14)
    private const val MAX_LINES = 6000
    private const val MAX_PHOTO = 24 * 1024
    val KEY = Regex("my-[a-z0-9-]{1,27}")
    private val HEX = Regex("#[0-9a-f]{6}")
    private val CODE = Regex("[a-z]{3}")
    private val BASE64 = Regex("[A-Za-z0-9+/]+={0,2}")

    private fun isStroke(w: String) = w.length >= 4 && w.length % 2 == 0 && w[0] in '0'..'4' && w[1] in '0'..'2' && w.drop(2).all { it in ALPHABET }

    private fun tidy(text: Any?): String = (text as? String ?: "").split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ").let {
        if (it.codePointCount(0, it.length) <= 30) it else it.substring(0, it.offsetByCodePoints(0, 30))
    }

    /** A picture checked and trimmed, or null. Comes from a Wi-Fi copy, a card or the radio. */
    fun clean(raw: Any?): Map<String, Any?>? {
        val m = raw.asMap() ?: return null
        val name = tidy(m["n"])
        if (name.isEmpty()) return null
        val out = LinkedHashMap<String, Any?>()
        out["n"] = name
        val names = (m["l"].asMap() ?: emptyMap()).filterKeys { CODE.matches(it) }.mapValues { tidy(it.value) }.filterValues { it.isNotEmpty() }
        if (names.isNotEmpty()) out["l"] = names.entries.take(6).associate { it.key to it.value }
        val photo = m["j"] as? String ?: ""
        if (photo.isNotEmpty()) {
            // "/9j/" is how every JPEG starts in base64.
            if (!photo.startsWith("/9j/") || photo.length % 4 != 0 || photo.length * 3L / 4 - 2 > MAX_PHOTO || !BASE64.matches(photo)) return null
            out["j"] = photo
            return out
        }
        val base = m["b"] as? String ?: ""
        if (base.isNotEmpty()) {
            if (!Html.hasPicture(base)) return null
            out["b"] = base
            (m["f"] as? String)?.takeIf { HEX.matches(it) }?.let { out["f"] = it }
        }
        val lines = (m["s"] as? String ?: "").split(" ").filter { isStroke(it) }.joinToString(" ").take(MAX_LINES).split(" ").filter { isStroke(it) }.joinToString(" ")
        if (lines.isNotEmpty()) out["s"] = lines
        return out
    }

    fun whole(pic: Map<String, Any?>?): Boolean = pic != null && ("s" in pic || "b" in pic || "j" in pic)

    fun nameOf(pic: Map<String, Any?>, lang: String = ""): String = pic["l"].asMap()?.get(lang)?.toString() ?: pic["n"].toString()

    fun svg(pic: Map<String, Any?>, cls: String): String {
        val inner = StringBuilder()
        pic["j"]?.let { inner.append("<image href='data:image/jpeg;base64,$it' width='$SIZE' height='$SIZE' preserveAspectRatio='xMidYMid slice'/>") }
        pic["b"]?.let { inner.append("<g transform='scale(10)' stroke='${Html.pictureStroke(it.toString())}' style='stroke-width:1.5'>${Html.picturePaths(it.toString(), pic["f"] as? String)}</g>") }
        for (word in (pic["s"] as? String ?: "").split(" ")) {
            if (!isStroke(word)) continue
            val at = word.drop(2).map { Math.rint(ALPHABET.indexOf(it) * SIZE.toDouble() / STEPS).toInt() }
            val points = at.chunked(2)
            val d = "M" + points.joinToString("L") { "${it[0]} ${it[1]}" } + (if (points.size == 1) "l.1 0" else "")
            inner.append("<path d='$d' fill='none' stroke='${COLOURS[word[0] - '0']}' style='stroke-width:${WIDTHS[word[1] - '0']}'/>")
        }
        if (inner.isEmpty()) {   // still on its way: its name in a frame
            val name = pic["n"].toString()
            val label = if (name.codePointCount(0, name.length) <= 12) name else name.substring(0, name.offsetByCodePoints(0, 11)) + "…"
            inner.append("<rect x='6' y='6' width='228' height='228' rx='26' fill='none' stroke='#9a9a94' style='stroke-width:5;stroke-dasharray:14 12'/>" +
                "<text x='120' y='132' text-anchor='middle' font-size='34' fill='#4a4a46' stroke='none'>${esc(label)}</text>")
        }
        return "<svg class='pic own${if (cls.isEmpty()) "" else " $cls"}' viewBox='0 0 $SIZE $SIZE' aria-hidden='true'>$inner</svg>"
    }
}
