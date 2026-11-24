package org.meshlearn.core

import java.io.File
import java.security.MessageDigest

/** Recordings of the teacher's voice, found by the language and the words that were said. A port of voices.py (the same keys and files). */
object Voices {
    val KEY = Regex("[0-9a-f]{16}")
    const val MAX_BYTES = 400 * 1024
    val TYPES = mapOf("webm" to "audio/webm", "ogg" to "audio/ogg", "m4a" to "audio/mp4", "mp3" to "audio/mpeg")

    fun key(text: String, lang: String): String {
        // Python's split(): every kind of white space, not only the plain space.
        val plain = text.split(Regex("[\\s\\u001c-\\u001f\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]+")).filter { it.isNotEmpty() }.joinToString(" ")
        return MessageDigest.getInstance("SHA-256").digest("$lang\n$plain".toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it.toInt() and 0xFF) }.take(16)
    }

    /** The key of the recording to play for this text: the student's language first, then the others they may know.
     *  A bare number is only read in the student's own language. */
    fun find(files: Map<String, File>, text: String, langs: List<String>): String? = langs.withIndex().firstNotNullOfOrNull { (n, lang) ->
        key(text, lang).takeIf { it in files && (n == 0 || text.any { c -> c.isLetter() }) }
    }

    fun looksLike(data: ByteArray, ext: String): Boolean {
        fun at(i: Int) = data.getOrNull(i)?.toInt()?.and(0xFF) ?: -1
        fun starts(from: Int, text: String) = text.indices.all { at(from + it) == text[it].code }
        return when (ext) {
            "webm" -> at(0) == 0x1a && at(1) == 0x45 && at(2) == 0xdf && at(3) == 0xa3
            "ogg" -> starts(0, "OggS")
            "m4a" -> starts(4, "ftyp")
            else -> starts(0, "ID3") || (at(0) == 0xFF && at(1) and 0xE0 == 0xE0 && at(1) != -1)
        }
    }

    fun folderFiles(folder: File): Map<String, File> = (folder.listFiles() ?: emptyArray()).sortedBy { it.name }
        .filter { KEY.matches(it.name.substringBefore('.')) && it.name.substringAfter('.', "") in TYPES }.associateBy { it.name.substringBefore('.') }

    fun remove(folder: File, key: String) {
        folderFiles(folder)[key]?.delete()
    }

    fun save(folder: File, key: String, ext: String, data: ByteArray) {
        folder.mkdirs()
        remove(folder, key)
        val tmp = File(folder, "$key.$ext.tmp")
        tmp.writeBytes(data)
        tmp.renameTo(File(folder, "$key.$ext"))
    }

    fun response(files: Map<String, File>, key: String): Response {
        val f = if (KEY.matches(key)) files[key] else null
        if (f == null || !f.isFile) return Response("not found".toByteArray(), 404, "text/plain")
        return Response(ByteArray(0), 200, TYPES[f.extension] ?: "audio/webm").also { it.file = f }
    }
}
