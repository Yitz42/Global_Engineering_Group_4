package org.meshlearn.core

import fi.iki.elonen.NanoHTTPD
import java.io.File
import java.io.FileInputStream
import java.math.BigDecimal
import java.math.MathContext
import java.net.URLDecoder
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.logging.Level
import java.util.logging.Logger

/** A very small web layer like web.py, on NanoHTTPD. */
object Html {
    /** Python's html.escape(s, quote=True). */
    fun esc(v: Any?): String {
        val s = if (v == null) "" else pyStr(v)
        val sb = StringBuilder(s.length)
        for (c in s) when (c) {
            '&' -> sb.append("&amp;"); '<' -> sb.append("&lt;"); '>' -> sb.append("&gt;")
            '"' -> sb.append("&quot;"); '\'' -> sb.append("&#x27;"); else -> sb.append(c)
        }
        return sb.toString()
    }

    private const val SAFE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~"

    /** urllib.parse.quote(s, safe=safe). */
    fun quote(s: String, safe: String = "/"): String {
        val sb = StringBuilder()
        for (b in s.toByteArray(Charsets.UTF_8)) {
            val c = (b.toInt() and 0xFF).toChar()
            if (b >= 0 && (c in SAFE || c in safe)) sb.append(c) else sb.append("%%%02X".format(b.toInt() and 0xFF))
        }
        return sb.toString()
    }

    fun unquote(s: String): String = try {
        URLDecoder.decode(s.replace("+", "%2B"), "UTF-8")
    } catch (e: IllegalArgumentException) {
        s
    }

    /** urllib.parse.parse_qs, keeping the last value of each name. */
    fun parseQs(qs: String?): Map<String, String> {
        val out = LinkedHashMap<String, String>()
        if (qs.isNullOrEmpty()) return out
        for (part in qs.split('&')) {
            if (part.isEmpty()) continue
            val k = part.substringBefore('=')
            val v = part.substringAfter('=', "")
            fun dec(x: String) = try { URLDecoder.decode(x, "UTF-8") } catch (e: IllegalArgumentException) { x }
            out[dec(k)] = dec(v)
        }
        return out
    }

    /** Python's format(x, "g"): 6 significant digits, trailing zeros dropped. */
    fun g(x: Double?): String {
        if (x == null) return "None"
        if (x == 0.0) return "0"
        val bd = BigDecimal(x).round(MathContext(6)).stripTrailingZeros()
        val exp = bd.precision() - bd.scale() - 1
        return if (exp in -4..5) bd.toPlainString() else {
            val digits = bd.unscaledValue().abs().toString()
            val mant = if (digits.length == 1) digits else digits[0] + "." + digits.substring(1)
            (if (x < 0) "-" else "") + mant + "e" + (if (exp < 0) "-" else "+") + Math.abs(exp).toString().padStart(2, '0')
        }
    }

    fun size(bytes: Double): String {
        var num = bytes
        for (unit in listOf("B", "KB", "MB", "GB")) {
            if (num < 1024 || unit == "GB") {
                return if (unit == "B" || unit == "KB") String.format(Locale.US, "%.0f %s", num, unit)
                else String.format(Locale.US, "%.1f %s", num, unit)
            }
            num /= 1024
        }
        return String.format(Locale.US, "%.1f GB", num)
    }

    fun whenText(ts: Double?): String =
        if (ts == null || ts == 0.0) "" else SimpleDateFormat("MMM dd, HH:mm", Locale.US).format(Date((ts * 1000).toLong()))

    /** A file of the student site's look or scripts, shared with the Python phone (meshlearn/site). */
    fun site(name: String): String =
        Html::class.java.getResource("/site/$name")!!.readText(Charsets.UTF_8).replace("\r\n", "\n")

    private val ICONS: Map<String, Any?> by lazy { Json.parse(site("icons.json")).asMap()!! }

    /** Drawings for young learners, one set on every phone. */
    private val PICTURES: Map<String, Any?> by lazy { Json.parse(site("pictures.json")).asMap()!! }
    private const val MAX_PICS = 12

    fun hasPicture(key: String): Boolean = key in PICTURES
    fun pictureName(key: String, own: Map<String, Map<String, Any?>> = emptyMap(), lang: String = ""): String =
        own[key]?.let { OwnPics.nameOf(it, lang) } ?: PICTURES[key].asMap()?.get("n")?.toString() ?: key

    fun pictureStroke(key: String): String = (PICTURES[key].asMap()?.get("s") ?: "#1a1a1a").toString()

    fun picturePaths(key: String, fill: String? = null): String {
        val p = PICTURES[key].asMap() ?: return ""
        return (if (p["d"] != null) "<path d='${p["d"]}' fill='${fill ?: p["f"]}'/>" else "") + (if (p["x"] != null) "<path d='${p["x"]}' fill='none'/>" else "")
    }

    /** One drawing, from the picture library or the school's own (OwnPics.kt), or "" when there is none. */
    fun picture(key: String, cls: String = "", own: Map<String, Map<String, Any?>> = emptyMap()): String {
        own[key]?.let { return OwnPics.svg(it, cls) }
        if (key !in PICTURES) return ""
        return "<svg class='pic${if (cls.isEmpty()) "" else " $cls"}' viewBox='0 0 24 24' stroke='${pictureStroke(key)}' aria-hidden='true'>${picturePaths(key)}</svg>"
    }

    /** The pictures a question shows: "apple*3, ball" -> apple, apple, apple, ball (at most MAX_PICS). */
    fun picturesOf(spec: String, own: Map<String, Map<String, Any?>> = emptyMap()): List<String> = spec.split(",").flatMap { part ->
        val key = part.trim().substringBefore('*')
        val count = part.trim().substringAfter('*', "").trim().toIntOrNull() ?: 1
        if (key in PICTURES || key in own) List(count.coerceIn(1, MAX_PICS)) { key } else emptyList()
    }.take(MAX_PICS)

    /** A question's pictures in a frame. Read aloud as the names of what is shown, never how many. */
    fun picturesHtml(spec: String, own: Map<String, Map<String, Any?>> = emptyMap(), lang: String = ""): String {
        val keys = picturesOf(spec, own)
        if (keys.isEmpty()) return ""
        val names = keys.distinct().joinToString(", ") { pictureName(it, own, lang) }
        return "<div class='pics n${keys.size}' role='img' aria-label='${esc(names)}'>${keys.joinToString("") { picture(it, own = own) }}</div>"
    }

    /** A line icon from the designs. Decorative: the words beside it carry the meaning. */
    fun icon(name: String, cls: String = ""): String =
        "<svg class='i${if (cls.isEmpty()) "" else " $cls"}' viewBox='0 0 24 24' aria-hidden='true'><path d='${ICONS[name]}'/></svg>"

    val CSS = """
:root{--bg:#f7f7f4;--card:#fff;--ink:#1d1d1b;--muted:#5b5b57;--line:#d9d9d3;--accent:#1f5fbf;--accent-ink:#fff;--good:#1d7a3a;--bad:#b3261e;--warn:#8a5a00}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#22221f;--ink:#efefea;--muted:#b4b4ad;--line:#3a3a36;--accent:#7fb0ff;--accent-ink:#0b1a33;--good:#6fcf8a;--bad:#ff8a80;--warn:#f0c060}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}
header strong{font-size:18px;margin-right:auto}
header a{color:var(--accent);text-decoration:none;font-weight:600}
main{max-width:860px;margin:0 auto;padding:16px}
main.wide{max-width:1160px}
h1{font-size:26px;line-height:1.25;margin:8px 0 16px}
h2{font-size:20px;margin:24px 0 8px}
a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0}
.muted{color:var(--muted)}
.good{color:var(--good)} .bad{color:var(--bad)} .warn{color:var(--warn)}
label{display:block;font-weight:600;margin:12px 0 4px}
input[type=text],input[type=number],input[type=date],textarea,select{width:100%;font:inherit;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink)}
textarea{min-height:120px}
fieldset{border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:16px 0}
legend{font-weight:700;padding:0 6px}
.option{display:flex;gap:10px;align-items:center;padding:8px 0;font-weight:400}
.option input{width:22px;height:22px}
button,.button{display:inline-block;font:inherit;font-weight:700;padding:12px 18px;border-radius:8px;border:0;background:var(--accent);color:var(--accent-ink);text-decoration:none;cursor:pointer;margin:8px 8px 0 0}
button.secondary,.button.secondary{background:transparent;color:var(--accent);border:1px solid var(--accent)}
table{width:100%;border-collapse:collapse;margin:8px 0}
th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
.error{color:var(--bad);font-weight:600;margin:4px 0}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;border:1px solid var(--line);font-size:14px;white-space:nowrap}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr));gap:12px}
.stat{font-size:28px;font-weight:700}
"""

    fun page(title: String, body: String, nav: String = "", site: String = "MeshLearn", extraCss: String = "",
             lang: String = "en", dir: String = "ltr", bodyCls: String = "", before: String = "", after: String = ""): String =
        "<!doctype html><html lang='${esc(lang)}'${if (dir == "rtl") " dir='rtl'" else ""}><head><meta charset='utf-8'>" +
            "<meta name='viewport' content='width=device-width,initial-scale=1'>" +
            "<title>${esc(title)} · ${esc(site)}</title><style>$CSS$extraCss</style></head>" +
            "<body${if (bodyCls.isEmpty()) "" else " class='$bodyCls'"}><header><strong>${esc(site)}</strong>$nav</header>" +
            "<main id='main'>" + (if (bodyCls.isEmpty()) "<h1>${esc(title)}</h1>"
            else "<div class='titlebar'>$before<h1>${esc(title)}</h1>$after</div>") + "$body</main></body></html>"

    /**
     * The student site's look, on top of [CSS]: made for a phone held in one hand. Big tap targets,
     * whole cards that open, tabs that show where you are. The files in meshlearn/site, as the Python phone.
     */
    val STUDENT_CSS: String = site("student.css") + site("looks.css")

    /**
     * Counts down to when the phone sends the student's work by radio (data-in seconds from now),
     * then says it is on its way. Plain ES5.
     */
    val COUNTDOWN_JS: String = site("countdown.js")

    /**
     * Keeps a student's answers in the browser while they work (so a closed tab or a flat battery loses
     * nothing), counts what is answered, and shows which choice is picked. Plain ES5 for old phones.
     */
    val ASSIGNMENT_JS: String = site("assignment.js")
    val LISTEN_JS: String = site("listen.js")   // reads aloud, where the device has a voice for the language
    val SIGNIN_JS: String = site("signin.js")   // finds a name in the class list
    val IDLE_JS: String = site("idle.js")       // goes back to "Who are you?" when nobody taps for a while
    val MATH_JS: String = site("math.js")         // equations (KaTeX), only on pages that have some
    val READING_JS: String = site("reading.js")   // bigger text, easy-to-read letters, more space
    val SCRATCH_JS: String = site("scratch.js")   // a scratchpad per question; stays in the browser
    val RESUME_JS: String = site("resume.js")   // "Keep going" on work this student has begun

    /** Forgets a saved draft once the work is safely turned in. */
    const val CLEAR_DRAFT_JS =
        "(function(){var el=document.getElementById('clear-draft');if(!el)return;" +
            "try{window.localStorage.removeItem('meshlearn-draft:'+el.getAttribute('data-key'));}catch(e){}})();"

    /** Seconds a student typically needs per question, as in comprehension.py. */
    private val EXPECTED_S = mapOf("choice" to 30, "number" to 60, "text" to 30, "long" to 180)

    /** "about 5 min" for a list of questions. */
    fun minutesFor(questions: List<Map<String, Any?>>, words: Strings = Strings.ENGLISH): String {
        val s = questions.sumOf { EXPECTED_S[it["type"]] ?: 45 }
        return words("time.about_min", "n" to maxOf(1, Math.round(s / 60.0).toInt()))
    }

    /**
     * A due date in words, and its style: ("Due tomorrow", "soon"), ("Overdue · was due Thu, Oct 16", "late").
     * Empty when there is no date. Work already turned in is never "overdue".
     */
    fun dueLabel(due: String?, turnedIn: Boolean, today: java.util.Calendar = java.util.Calendar.getInstance(),
                 words: Strings = Strings.ENGLISH): Pair<String, String> {
        if (due.isNullOrBlank()) return "" to ""
        val parsed = try {
            SimpleDateFormat("yyyy-MM-dd", Locale.US).apply { isLenient = false }.parse(due.trim().take(10))
        } catch (e: Exception) {
            null
        } ?: return words("due.on", "date" to due) to ""
        val nice = words.date(parsed)
        val start = (today.clone() as java.util.Calendar).apply {
            set(java.util.Calendar.HOUR_OF_DAY, 0); set(java.util.Calendar.MINUTE, 0)
            set(java.util.Calendar.SECOND, 0); set(java.util.Calendar.MILLISECOND, 0)
        }
        val days = Math.round((parsed.time - start.timeInMillis) / 86_400_000.0).toInt()
        return when {
            turnedIn -> words("due.was", "date" to nice) to "done"
            days < 0 -> words("due.overdue", "date" to nice) to "late"
            days == 0 -> words("due.today") to "soon"
            days == 1 -> words("due.tomorrow") to "soon"
            else -> words("due.on", "date" to nice) to ""
        }
    }

    private val MIME = mapOf(
        "html" to "text/html", "htm" to "text/html", "txt" to "text/plain", "css" to "text/css", "js" to "text/javascript",
        "json" to "application/json", "pdf" to "application/pdf", "epub" to "application/epub+zip", "png" to "image/png",
        "jpg" to "image/jpeg", "jpeg" to "image/jpeg", "svg" to "image/svg+xml", "mp3" to "audio/mpeg", "mp4" to "video/mp4",
        "webm" to "video/webm", "gif" to "image/gif",
    )

    fun mimeFor(name: String): String {
        val type = MIME[name.substringAfterLast('.', "").lowercase()] ?: "application/octet-stream"
        return if (type.startsWith("text/")) "$type; charset=utf-8" else type
    }
}

class Request(
    val method: String,
    val path: String,
    val query: Map<String, String>,
    val headers: Map<String, String>,
    val body: ByteArray,
    val client: String,
) {
    var params: Map<String, String> = emptyMap()
    val cookies: Map<String, String> by lazy {
        val out = LinkedHashMap<String, String>()
        (headers["cookie"] ?: "").split(';').forEach { part ->
            val k = part.substringBefore('=').trim()
            if (k.isNotEmpty() && part.contains('=')) out[k] = Html.unquote(part.substringAfter('=').trim().trim('"'))
        }
        out
    }
    val form: Map<String, String> by lazy { Html.parseQs(String(body, Charsets.UTF_8)) }

    /** True when the request comes from the phone itself. */
    val isLocal: Boolean get() = client == "127.0.0.1" || client == "::1" || client == "0:0:0:0:0:0:0:1" || client.startsWith("/127.")
}

class Response(
    val body: ByteArray = ByteArray(0),
    val status: Int = 200,
    val contentType: String = "text/html; charset=utf-8",
    val headers: MutableMap<String, String> = LinkedHashMap(),
) {
    var file: File? = null

    constructor(html: String, status: Int = 200) : this(html.toByteArray(Charsets.UTF_8), status)

    fun setCookie(name: String, value: String, maxAge: Int = 60 * 60 * 24 * 180): Response {
        headers["Set-Cookie"] = "$name=${Html.quote(value)}; Path=/; Max-Age=$maxAge; SameSite=Lax; HttpOnly"
        return this
    }

    companion object {
        fun redirect(location: String) = Response(ByteArray(0), 303).also { it.headers["Location"] = location }
        fun file(f: File) = Response(ByteArray(0), 200, Html.mimeFor(f.name)).also { it.file = f }
    }
}

typealias Handler = (Request) -> Response

/** Routes like web.App: "<name>" matches one path segment, "<name:path>" the rest. */
class App(private val maxBody: Int = 256 * 1024) {
    private val log = Logger.getLogger("App")
    private class Route(val method: String, val regex: Regex, val names: List<String>, val fn: Handler)
    private val routes = ArrayList<Route>()

    fun add(method: String, pattern: String, fn: Handler) {
        val names = ArrayList<String>()
        val converted = Regex("<(\\w+)(:path)?>").replace(pattern) { m ->
            names.add(m.groupValues[1])
            if (m.groupValues[2].isNotEmpty()) "(.+)" else "([^/]+)"
        }
        routes.add(Route(method, Regex("^$converted$"), names, fn))
    }

    fun dispatch(req: Request): Response {
        // A walled-off activity (ZipApps.kt) must not be able to press our buttons.
        if (req.method == "POST" && req.headers["origin"] == "null") return Response("Not allowed".toByteArray(), 403, "text/plain")
        var pathMatched = false
        for (r in routes) {
            val m = r.regex.matchEntire(req.path) ?: continue
            pathMatched = true
            if (r.method != req.method) continue
            req.params = r.names.mapIndexed { i, n -> n to Html.unquote(m.groupValues[i + 1]) }.toMap()
            return r.fn(req)
        }
        if (pathMatched) return Response("Method not allowed".toByteArray(), 405, "text/plain")
        return Response(Html.page("Not found", "<p>That page does not exist.</p><p><a href='/'>Go home</a></p>"), 404)
    }

    /** A NanoHTTPD server for this app. Call start() on it. */
    fun server(host: String?, port: Int): NanoHTTPD = object : NanoHTTPD(host, port) {
        override fun serve(session: IHTTPSession): NanoHTTPD.Response {
            val length = session.headers["content-length"]?.toIntOrNull() ?: 0
            if (length > maxBody) return newFixedLengthResponse(status(413, "Request too large"), "text/plain", "Request too large")
            val body = if (length > 0) {
                val buf = ByteArray(length)
                var read = 0
                val input = session.inputStream
                while (read < length) {
                    val n = input.read(buf, read, length - read)
                    if (n < 0) break
                    read += n
                }
                buf.copyOf(read)
            } else ByteArray(0)
            val req = Request(session.method.name, session.uri, Html.parseQs(session.queryParameterString), session.headers, body,
                session.remoteIpAddress ?: "")
            val resp = try {
                dispatch(req)
            } catch (e: Exception) {
                log.log(Level.WARNING, "error handling ${req.method} ${req.path}", e)
                Response(Html.page("Something went wrong", "<p>Sorry, that did not work. Please try again.</p>"), 500)
            }
            val out = resp.file?.let { f -> sendFile(f, resp, session.headers["range"] ?: "") } ?: newFixedLengthResponse(status(resp.status, ""), resp.contentType, resp.body.inputStream(), resp.body.size.toLong())
            resp.headers.forEach { (k, v) -> out.addHeader(k, v) }
            return out
        }
    }

    private fun sendFile(f: File, resp: Response, range: String): NanoHTTPD.Response {
        val size = f.length()
        val span = byteRange(range, size)
        val input = FileInputStream(f)
        if (span != null) input.channel.position(span.first)
        val length = if (span != null) span.last - span.first + 1 else size
        val out = NanoHTTPD.newFixedLengthResponse(status(if (span != null) 206 else resp.status, ""), resp.contentType, input, length)
        out.addHeader("Accept-Ranges", "bytes")
        if (span != null) out.addHeader("Content-Range", "bytes ${span.first}-${span.last}/$size")
        return out
    }

    private fun status(code: Int, text: String) = object : NanoHTTPD.Response.IStatus {
        override fun getDescription() = "$code ${text.ifEmpty { REASONS[code] ?: "OK" }}"
        override fun getRequestStatus() = code
    }

    companion object {
        /** First and last byte a "Range: bytes=..." header asks for (what video players send to start or skip
         *  ahead), or null to send the whole file. */
        fun byteRange(header: String, size: Long): LongRange? {
            val m = Regex("^bytes=(\\d{0,18})-(\\d{0,18})$").find(header.trim()) ?: return null
            val (from, to) = m.destructured
            if (from.isEmpty() && to.isEmpty()) return null
            val start = if (from.isNotEmpty()) from.toLong() else maxOf(size - to.toLong(), 0)
            val end = if (from.isNotEmpty() && to.isNotEmpty()) minOf(to.toLong(), size - 1) else size - 1
            return if (start <= end) start..end else null
        }

        private val REASONS = mapOf(200 to "OK", 206 to "Partial Content", 303 to "See Other", 400 to "Bad Request", 403 to "Forbidden", 404 to "Not Found",
            405 to "Method Not Allowed", 413 to "Payload Too Large", 500 to "Internal Server Error")
    }
}
