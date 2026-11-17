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

    fun page(title: String, body: String, nav: String = "", site: String = "MeshLearn", extraCss: String = "",
             lang: String = "en", dir: String = "ltr"): String =
        "<!doctype html><html lang='${esc(lang)}'${if (dir == "rtl") " dir='rtl'" else ""}><head><meta charset='utf-8'>" +
            "<meta name='viewport' content='width=device-width,initial-scale=1'>" +
            "<title>${esc(title)} · ${esc(site)}</title><style>$CSS$extraCss</style></head>" +
            "<body><header><strong>${esc(site)}</strong>$nav</header>" +
            "<main id='main'><h1>${esc(title)}</h1>$body</main></body></html>"

    /**
     * The student site's look, on top of [CSS]: made for a phone held in one hand. Big tap targets,
     * whole cards that open, tabs that show where you are. Kept identical to STUDENT_CSS in web.py.
     */
    val STUDENT_CSS = """
body{font-size:18px}
header{position:sticky;top:0;z-index:5;flex-direction:column;align-items:stretch;gap:8px;padding:10px 12px}
header strong{font-size:16px;margin:0;display:flex;justify-content:space-between;gap:8px}
.who{font-weight:400;color:var(--muted);font-size:15px}
nav.tabs{display:flex;gap:6px}
nav.tabs a{flex:1;display:flex;align-items:center;justify-content:center;white-space:nowrap;padding:9px 4px;border-radius:10px;border:1px solid var(--line);font-size:16px;color:var(--accent);text-decoration:none}
nav.tabs a[aria-current=page]{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
main{padding:12px 14px 32px}
h1{font-size:24px;margin:6px 0 10px}
.lead{font-size:19px;margin:0 0 14px}
a.card{display:block;color:var(--ink);text-decoration:none}
a.card:hover,a.card:focus{border-color:var(--accent)}
a.card h2,a.card h3{color:var(--accent);margin:0 0 6px}
a.card h3{font-size:19px}
.meta{color:var(--muted);font-size:15px;margin:4px 0}
.due{font-size:15px;font-weight:700;margin:6px 0}
.due.late{color:var(--bad)} .due.soon{color:var(--warn)} .due.done{color:var(--muted);font-weight:400}
.go{display:block;text-align:center;margin:12px 0 0}
.bar{height:10px;border-radius:99px;background:var(--line);overflow:hidden;margin:10px 0 4px}
.bar span{display:block;height:100%;background:var(--good)}
fieldset.q{background:var(--card);margin:14px 0;padding:14px}
fieldset.q legend{float:left;width:100%;padding:0;margin:0 0 10px;font-size:19px;line-height:1.35}
fieldset.q legend+*{clear:both}
.qnum{display:block;color:var(--muted);font-size:14px;font-weight:600;margin-bottom:2px}
.q label:not(.option){font-weight:400;font-size:15px;color:var(--muted);margin:4px 0}
.option{border:1px solid var(--line);border-radius:10px;padding:12px;margin:8px 0;min-height:52px;font-size:18px;color:var(--ink);cursor:pointer}
.option.on{border-color:var(--accent);outline:2px solid var(--accent)}
.q.bad{border-color:var(--bad)}
.counter{font-size:14px;color:var(--muted);text-align:right;margin:4px 0 0}
.submitbar{position:sticky;bottom:0;background:var(--bg);border-top:1px solid var(--line);padding:10px 0;margin-top:12px}
.submitbar .meta{margin:0 0 6px}
button.big,.button.big{display:block;width:100%;text-align:center;padding:15px;font-size:19px;margin:8px 0 0}
.grade{font-size:22px;font-weight:700;margin:6px 0}
.units{list-style:none;padding:0;margin:8px 0 0}
.units li{padding:8px 0;border-top:1px solid var(--line)}
a.item{padding:12px 14px;margin:8px 0}
a.item strong{color:var(--accent)}
form.search{display:flex;gap:8px;align-items:center;margin:0 0 8px}
form.search input{flex:1}
form.search button{margin:0}
form.lang{align-self:flex-end;margin:0}
form.lang button{margin:0;padding:6px 14px;font-size:15px;background:transparent;color:var(--accent);border:1px solid var(--line)}
[dir=rtl] fieldset.q legend{float:right}
[dir=rtl] .counter{text-align:left}
[dir=rtl] button,[dir=rtl] .button{margin:8px 0 0 8px}
[dir=rtl] button.big,[dir=rtl] .button.big,[dir=rtl] form.search button,[dir=rtl] form.lang button{margin:8px 0 0}
[dir=rtl] th,[dir=rtl] td{text-align:right}
.week{margin:0 0 14px}
.week .wk{display:flex;justify-content:space-between;gap:8px;font-size:16px}
.week .bar{height:14px;margin:6px 0 0}
.next{background:var(--accent);color:var(--accent-ink);border-radius:22px;padding:20px;margin:0 0 16px}
.next .label{font-size:15px;font-weight:700;letter-spacing:.05em;text-transform:uppercase;opacity:.85}
.next h2{margin:8px 0 6px;font-size:23px;line-height:1.25;color:inherit}
.next .meta{color:inherit;opacity:.9}
.next .due{color:inherit}
.next .button{display:block;text-align:center;background:var(--card);color:var(--accent);font-size:20px;padding:16px;border-radius:16px;margin:14px 0 0}
.row{display:flex;align-items:center;gap:12px;background:var(--card);border:2px solid var(--line);border-radius:18px;padding:12px 14px;margin:8px 0;color:var(--ink);text-decoration:none}
.row.wait{border-style:dashed;background:transparent}
.row .grow{flex:1;min-width:0;display:flex;flex-direction:column}
.row .meta{margin:0}
.row .end{font-weight:700;text-align:right}
.row .end.small{font-weight:400;font-size:15px;color:var(--muted)}
.chev{color:var(--muted);font-size:22px}
.squares{display:grid;grid-template-columns:repeat(auto-fill,minmax(40px,1fr));gap:6px;margin:10px 0}
.squares span{display:flex;align-items:center;justify-content:center;height:40px;border-radius:10px;font-weight:800}
.squares .right{background:var(--good);color:var(--card)}
.squares .part{background:var(--warn);color:var(--card)}
.squares .wrong{border:3px solid var(--bad);color:var(--bad)}
.squares .wait{border:2px dashed var(--line);color:var(--muted)}
.new{display:inline-block;padding:2px 10px;border-radius:99px;background:var(--accent);color:var(--accent-ink);font-size:14px;font-weight:800}
.bigscore{font-size:24px;font-weight:800;margin:6px 0}
.missed{background:var(--bg);border:1px solid var(--line);border-radius:14px;padding:12px 14px;margin:10px 0 0}
.missed strong{display:block}
.sendin{font-weight:700}
"""

    /**
     * Counts down to when the phone sends the student's work by radio (data-in seconds from now),
     * then says it is on its way. Kept identical to COUNTDOWN_JS in web.py. Plain ES5.
     */
    const val COUNTDOWN_JS = """
(function(){var el=document.getElementById('sendin');if(!el)return;
var left=parseInt(el.getAttribute('data-in'),10)||0,t0=new Date().getTime();
function tick(){var s=Math.max(0,Math.round(left-(new Date().getTime()-t0)/1000));
if(s<=0){el.textContent=el.getAttribute('data-now');return;}
var m=Math.floor(s/60),r=s%60;el.textContent=el.getAttribute('data-words').replace('{time}',m+':'+(r<10?'0':'')+r);
setTimeout(tick,1000);}tick();})();
"""

    /**
     * Keeps a student's answers in the browser while they work (so a closed tab or a flat battery loses
     * nothing), counts what is answered, and shows which choice is picked. Plain ES5 for old phones.
     */
    const val ASSIGNMENT_JS = """
(function(){var f=document.getElementById('aform');if(!f)return;var key='meshlearn-draft:'+f.getAttribute('data-key');
var store=null;try{store=window.localStorage;}catch(e){}
function all(){return f.querySelectorAll('input[type=text],input[type=radio],textarea');}
function save(){if(!store)return;var d={},els=all();for(var i=0;i<els.length;i++){var el=els[i];
if(el.type==='radio'){if(el.checked)d[el.name]=el.value;}else if(el.value)d[el.name]=el.value;}
try{store.setItem(key,JSON.stringify(d));}catch(e){}}
function restore(){if(!store)return;var d;try{d=JSON.parse(store.getItem(key)||'{}')||{};}catch(e){d={};}
var els=all();for(var i=0;i<els.length;i++){var el=els[i];if(!d.hasOwnProperty(el.name))continue;
if(el.type==='radio'){if(!f.querySelector('input[name="'+el.name+'"]:checked')&&el.value===d[el.name])el.checked=true;}
else if(!el.value)el.value=d[el.name];}}
function update(){var qs=f.querySelectorAll('fieldset.q'),done=0;for(var i=0;i<qs.length;i++){
var q=qs[i],ok=false,els=q.querySelectorAll('input,textarea');for(var j=0;j<els.length;j++){var el=els[j];
if(el.type==='radio'){var lab=el.parentNode;if(el.checked){ok=true;lab.className='option on';}else lab.className='option';}
else if(el.value.replace(/\s/g,''))ok=true;
if(el.tagName==='TEXTAREA'){var c=document.getElementById(el.id+'-count');if(c)c.innerHTML=el.value.length+' / '+el.getAttribute('maxlength');}}
if(ok)done++;}var a=document.getElementById('answered');
if(a)a.textContent=(a.getAttribute('data-words')||'{done} of {total} answered').replace('{done}',done).replace('{total}',qs.length);}
restore();update();f.addEventListener('input',function(){save();update();});f.addEventListener('change',function(){save();update();});
var bad=f.querySelector('[aria-invalid=true]');if(bad)bad.focus();})();
"""

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
