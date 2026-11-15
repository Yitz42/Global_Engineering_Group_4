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

    fun page(title: String, body: String, nav: String = "", site: String = "MeshLearn"): String =
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>" +
            "<meta name='viewport' content='width=device-width,initial-scale=1'>" +
            "<title>${esc(title)} · ${esc(site)}</title><style>$CSS</style></head>" +
            "<body><header><strong>${esc(site)}</strong>$nav</header>" +
            "<main id='main'><h1>${esc(title)}</h1>$body</main></body></html>"

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
        headers["Set-Cookie"] = "$name=${Html.quote(value)}; Path=/; Max-Age=$maxAge; SameSite=Lax"
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
            val out = resp.file?.let { f ->
                newFixedLengthResponse(status(resp.status, ""), resp.contentType, FileInputStream(f), f.length())
            } ?: newFixedLengthResponse(status(resp.status, ""), resp.contentType, resp.body.inputStream(), resp.body.size.toLong())
            resp.headers.forEach { (k, v) -> out.addHeader(k, v) }
            return out
        }
    }

    private fun status(code: Int, text: String) = object : NanoHTTPD.Response.IStatus {
        override fun getDescription() = "$code ${text.ifEmpty { REASONS[code] ?: "OK" }}"
        override fun getRequestStatus() = code
    }

    companion object {
        private val REASONS = mapOf(200 to "OK", 303 to "See Other", 400 to "Bad Request", 404 to "Not Found",
            405 to "Method Not Allowed", 413 to "Payload Too Large", 500 to "Internal Server Error")
    }
}
