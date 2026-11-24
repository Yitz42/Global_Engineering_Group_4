package org.meshlearn.core

import org.meshlearn.core.Html.esc
import org.meshlearn.core.Html.icon
import org.meshlearn.core.Html.quote
import java.io.File
import java.util.zip.ZipFile

/**
 * Interactive lessons on the card (PhET simulations, Blockly Games, GeoGebra), served straight from their zip files and
 * walled off from the student site. A port of zipapps.py, giving the same pages and limits.
 */
class ZipApps(private val phone: Phone, private val page: (Request, String, String, Int) -> Response) {
    fun routes(app: App) {
        app.add("GET", "/library/open", ::open)
        app.add("GET", "/library/app/<path:path>", ::file)
        app.add("GET", "/site/<path:path>") { vendor(it.params["path"] ?: "") }
        app.add("GET", "/about", ::about)
        app.add("GET", "/join", ::join)
    }

    private fun onCard(rel: String): File? {
        val root = phone.libraryDir.canonicalFile
        val target = File(root, rel).canonicalFile
        return target.takeIf { it.path.startsWith(root.path + File.separator) && it.path.lowercase().endsWith(".zip") && it.isFile }
    }

    /** One activity or tool, full width, with its credit beside it. */
    private fun open(req: Request): Response {
        val w = phone.words(req)
        val rel = req.query["f"] ?: ""
        val item = phone.libraryItems().firstOrNull {
            it["file"].toString().replace(File.separatorChar, '/') == rel && it["kind"] in setOf("zip", "ggb")
        }
        if (item == null || onCard(rel) == null) return page(req, w("notfound.title"), "<p>${esc(w("library.no_file"))}</p>", 404)
        phone.remember(req, rel)
        val base = "/library/app/" + quote(rel)
        val credit = item["credit"]?.toString() ?: ""
        val show = if (item["kind"] == "ggb") GEOGEBRA_APPS.joinToString("") { (k, t) ->
            "<a class='card item' href='$base/_start.html?app=$k&amp;lang=${esc(w.tag)}'><span class='sub maths'>${icon("maths")}</span>" +
                "<span class='grow'><strong>${esc(t)}</strong></span>${icon("chev", "arr")}</a>"
        } else "<iframe class='appframe' src='$base/index.html' sandbox='allow-scripts' allow='fullscreen' title='${esc(item["title"])}'></iframe>"
        val body = "<p class='meta'><a href='/library'>${esc(w("library.back"))}</a></p>$show" +
            (if (credit.isNotEmpty()) "<p class='meta credit'>${esc(credit)}</p>" else "")
        return page(req, item["title"].toString(), body, 200)
    }

    private fun file(req: Request): Response {
        val path = req.params["path"] ?: ""
        val cut = path.lowercase().indexOf(".zip/")
        val card = (if (cut > 0) onCard(path.substring(0, cut + 4)) else null) ?: return notFound()
        val name = path.substring(cut + 5)
        val host = (req.headers["host"] ?: "").takeIf { HOST.matches(it) } ?: "localhost"
        val base = "http://$host/library/app/${quote(path.substring(0, cut + 4))}/"
        val own = if (path.substring(0, cut + 4) == GEOGEBRA_ZIP) " allow-same-origin" else ""
        val headers = linkedMapOf(
            // Scripts run, in an origin of their own; nothing but this zip's files can be reached.
            "Content-Security-Policy" to "sandbox allow-scripts$own; default-src $base 'unsafe-inline' 'unsafe-eval' data: blob:; " +
                "form-action 'none'; base-uri 'none'",
            "Access-Control-Allow-Origin" to "*", "X-Content-Type-Options" to "nosniff", "Cache-Control" to "max-age=86400",
            "Referrer-Policy" to "no-referrer")
        if (name == "_start.html") {
            val html = geogebraPage(req.query["app"] ?: "", req.query["lang"] ?: "", phone.words(req)("library.back"))
            return Response(html.toByteArray(Charsets.UTF_8), 200, "text/html; charset=utf-8", headers)
        }
        var data = try {
            ZipFile(card).use { z ->
                val entry = z.getEntry(name) ?: return notFound()
                if (entry.isDirectory || entry.size > MAX_ENTRY) return notFound()
                z.getInputStream(entry).use { it.readBytes() }
            }
        } catch (e: Exception) { return notFound() }
        val kind = contentType(name)
        if (kind.startsWith("text/html") && own.isEmpty()) data = addShim(data)
        return Response(data, 200, kind, headers)
    }

    private fun about(req: Request): Response {
        val w = phone.words(req)
        val rows = ABOUT.joinToString("") { (n, t) -> "<li><strong>${esc(n)}</strong> <span class='meta'>${esc(t)}</span></li>" }
        return page(req, w("about.title"), "<ul class='units card'>$rows</ul>", 200)
    }

    /** A QR code for this phone's student site, for the app's "How students join" screen. */
    private fun join(req: Request): Response {
        val w = phone.words(req)
        var url = req.query["u"] ?: ""
        if (!Regex("http://[A-Za-z0-9.\\-:]{3,60}/?").matches(url)) {
            url = "http://" + ((req.headers["host"] ?: "").takeIf { HOST.matches(it) } ?: "192.168.43.1:8080")
        }
        return page(req, w("join.title"), "<div class='qrbox'><div data-qr='${esc(url)}'></div><p class='grade'>${esc(url.substring(7))}</p></div>" +
            "<p>${esc(w("join.text"))}</p><script>${Html.site("qr.js")}</script>", 200)
    }

    companion object {
        private const val MAX_ENTRY = 60L * 1024 * 1024
        private const val GEOGEBRA_ZIP = "tools/geogebra.zip"
        private val HOST = Regex("[A-Za-z0-9.\\-:\\[\\]]{1,100}")
        private val VENDOR = Regex("[a-z_]+(/fonts)?/[A-Za-z0-9_.\\-]+")
        private const val GEOGEBRA_CREDIT = "GeoGebra © GeoGebra GmbH, https://www.geogebra.org. Free for non-commercial use."
        private val GEOGEBRA_APPS = listOf("graphing" to "Graphing", "geometry" to "Geometry", "3d" to "3D", "scientific" to "Calculator",
            "classic" to "Classic")
        private val ABOUT = listOf("KaTeX" to "MIT · katex.org", "Signature Pad" to "MIT · Szymon Nowak",
            "OpenDyslexic" to "SIL Open Font License · Abbie Gonzalez", "qrcode-generator" to "MIT · Kazuhiko Arase", "Papa Parse" to "MIT · Matthew Holt",
            "PhET" to "Simulation by PhET Interactive Simulations, University of Colorado Boulder, licensed under CC BY 4.0 (https://phet.colorado.edu).",
            "Blockly Games" to "Google · public domain", "GeoGebra" to GEOGEBRA_CREDIT, "Kolibri" to "Learning Equality · learningequality.org")
        private val TYPES = mapOf("js" to "text/javascript; charset=utf-8", "mjs" to "text/javascript; charset=utf-8",
            "html" to "text/html; charset=utf-8", "css" to "text/css; charset=utf-8", "json" to "application/json", "svg" to "image/svg+xml",
            "wasm" to "application/wasm", "woff" to "font/woff", "woff2" to "font/woff2", "cur" to "image/x-icon", "mp3" to "audio/mpeg",
            "ogg" to "audio/ogg", "wav" to "audio/wav", "png" to "image/png", "jpg" to "image/jpeg", "jpeg" to "image/jpeg", "gif" to "image/gif")

        fun contentType(name: String): String = TYPES[name.substringAfterLast('.', "").lowercase()] ?: Html.mimeFor(name)

        private fun notFound() = Response("not found".toByteArray(), 404, "text/plain")

        /** Ready-made libraries the student site uses (site/vendor). */
        fun vendor(path: String): Response {
            if (!path.startsWith("vendor/") || !VENDOR.matches(path.substring(7)) || ".." in path) return notFound()
            val data = ZipApps::class.java.getResourceAsStream("/site/$path")?.use { it.readBytes() } ?: return notFound()
            return Response(data, 200, contentType(path), linkedMapOf("Cache-Control" to "max-age=86400"))
        }

        /** Give the page storage that lasts while it is open: a walled-off page has none of its own. */
        fun addShim(page: ByteArray): ByteArray {
            val text = String(page, Charsets.ISO_8859_1)   // byte for byte
            val tag = "<script>" + String(Html.site("appshim.js").toByteArray(Charsets.UTF_8), Charsets.ISO_8859_1) + "</script>"
            val m = Regex("<head[^>]*>", RegexOption.IGNORE_CASE).find(text.take(20000))
            val out = if (m != null) text.substring(0, m.range.last + 1) + tag + text.substring(m.range.last + 1) else tag + text
            return out.toByteArray(Charsets.ISO_8859_1)
        }

        private fun geogebraPage(appName: String, langTag: String, back: String): String {
            val app = if (GEOGEBRA_APPS.any { it.first == appName }) appName else "graphing"
            val lang = if (Regex("[A-Za-z-]{2,8}").matches(langTag)) langTag else "en"
            val params = Json.dumps(linkedMapOf("appName" to app, "showToolBar" to (app != "scientific"), "showAlgebraInput" to true,
                "showMenuBar" to false, "language" to lang.substringBefore('-'), "enableFileFeatures" to false, "preventFocus" to true))
            return "<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>" +
                "<title>GeoGebra</title><style>html,body{margin:0;height:100%;overflow:hidden;font:15px system-ui,sans-serif}" +
                ".top{display:flex;gap:12px;align-items:center;height:44px;padding:0 12px;background:#1f5fbf}.top a{color:#fff;font-weight:700}" +
                ".foot{height:26px;padding:0 12px;font-size:12px;line-height:26px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}</style></head>" +
                "<body><div class='top'><a href='/library'>${esc(back)}</a></div><div id='ggb'></div><div class='foot'>${esc(GEOGEBRA_CREDIT)}</div>" +
                "<script src='GeoGebra/deployggb.js'></script><script>" +
                "var p=$params;function h(){return Math.max(200,window.innerHeight-70)}p.width=window.innerWidth;p.height=h();" +
                "var a=new GGBApplet(p,true);a.setHTML5Codebase('GeoGebra/HTML5/5.0/web3d/');a.inject('ggb');" +
                "window.addEventListener('resize',function(){try{ggbApplet.setSize(window.innerWidth,h())}catch(e){}});" +
                "</script></body></html>"
        }
    }
}
