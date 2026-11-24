package org.meshlearn.core

import org.meshlearn.core.Html.esc
import org.meshlearn.core.Html.icon
import org.meshlearn.core.Html.picture
import org.meshlearn.core.Html.quote
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/** Someone on the class list: name, picture, look ("young", "mid", "teen") and hashed secret. */
class Person(val name: String, val pic: String, val look: String, val secret: String, val lang: String = "")

/**
 * Who is using the phone: picking yourself by picture, name or PIN. A port of phonesignin.py, giving the same pages.
 * The class list comes with the lessons; without one, students type their name as before.
 */
class SignIn(private val phone: Phone, private val db: Db, private val page: (Request, String, String) -> String) {
    var roster: List<Person> = emptyList()
        private set
    private var ways: Map<String, String> = emptyMap()
    private var signoutMin = 0L
    private val tries = HashMap<String, Pair<Int, Double>>()

    fun routes(app: App) {
        app.add("GET", "/who") { whoPage(it) }
        app.add("POST", "/who", ::whoSubmit)
        app.add("GET", "/new", ::newPage)
        app.add("GET", "/me", ::mePage)
    }

    /** The class list from the school's data. */
    fun load(data: Map<String, Any?>) {
        roster = (data["students"].asList() ?: emptyList()).mapNotNull { item ->
            val m = item.asMap() ?: return@mapNotNull null
            val name = (m["n"] as? String)?.ifEmpty { null } ?: return@mapNotNull null
            val look = (m["a"] as? String)?.takeIf { it in LOOK_ORDER } ?: "mid"
            Person(name, m["p"] as? String ?: "", look, m["s"] as? String ?: "", m["l"] as? String ?: "")
        }
        val given = data["signin"].asMap() ?: emptyMap()
        ways = mapOf("young" to (given["young"] as? String ?: "pic"), "mid" to (given["mid"] as? String ?: "list"),
            "teen" to (given["teen"] as? String ?: "list"))
        signoutMin = (data["signout"] as? Number)?.toLong()?.takeIf { it in 0..240 } ?: 0L
    }

    private fun people(): List<Person> {
        val known = roster.map { it.name.lowercase() }.toSet()
        return roster + db.all("SELECT * FROM newcomers ORDER BY name")
            .map { Person(it.str("name")!!, it.str("pic") ?: "", it.str("look") ?: "mid", "") }.filter { it.name.lowercase() !in known }
    }

    fun person(name: String?): Person? = (name ?: "").lowercase().let { n -> people().firstOrNull { it.name.lowercase() == n } }

    private fun needsSecret(p: Person?): Boolean = p != null && p.secret.isNotEmpty() && ways[p.look] in setOf("secret", "pin")

    /** Goes in the cookie of a student who gave their secret, so nobody can sign in by typing a cookie. */
    private fun proof(name: String): String {
        var key = db.one("SELECT value FROM settings WHERE key = 'cookie_key'")?.str("value")
        if (key == null) {
            val bytes = ByteArray(16).also { SecureRandom().nextBytes(it) }
            db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('cookie_key', ?)", hex(bytes))
            key = db.one("SELECT value FROM settings WHERE key = 'cookie_key'")!!.str("value")!!
        }
        val mac = Mac.getInstance("HmacSHA256").apply { init(SecretKeySpec(key.toByteArray(), "HmacSHA256")) }
        return hex(mac.doFinal(name.toByteArray(Charsets.UTF_8))).take(24)
    }

    /** The signed-in student's name, or null. */
    fun student(req: Request): String? {
        val cookie = req.cookies["student"] ?: ""
        val name = cookie.substringBefore('~')
        if (name.isEmpty()) return null
        if (roster.isNotEmpty() && needsSecret(person(name)) && !same(cookie.substringAfter('~', ""), proof(name))) return null
        return name
    }

    private fun signIn(p: Person): Response =
        Response.redirect("/").setCookie("student", p.name + (if (needsSecret(p)) "~" + proof(p.name) else ""))

    // -- pages ----------------------------------------------------------------

    fun face(p: Person): String = if (Html.hasPicture(p.pic)) picture(p.pic, "face")
        else "<span class='avatar' aria-hidden='true'>${esc(p.name.take(1).uppercase())}</span>"

    /** The sign-in page when there is a class list. */
    fun whoBody(req: Request, w: Strings): String {
        val look = phone.look(req)
        val mine = people().filter { it.look == look }
        if (look == "young" || (look == "mid" && ways["mid"] == "pic")) {
            val cards = mine.joinToString("") {
                "<a class='kid' data-name='${esc(it.name)}' href='/who?n=${quote(it.name, "")}'>${face(it)}<strong>${esc(it.name)}</strong></a>"
            }
            return "<p class='lead'>${esc(w("signin.tap"))}</p><div class='kids' id='kids'>$cards</div>" +
                "<a class='button big secondary' href='/new'>${esc(w("signin.not_here"))}</a>"
        }
        val sorted = mine.sortedBy { it.name.lowercase() }
        val rows = sorted.joinToString("") {
            "<a data-name='${esc(it.name)}' href='/who?n=${quote(it.name, "")}'>${face(it)}<span class='grow'><strong>${esc(it.name)}</strong></span>" +
                "${icon("chev", "arr")}</a>"
        }
        var find = ""
        if (look == "teen") {
            find = "<label for='find'>${esc(w("welcome.name"))}</label><input type='text' id='find' autocomplete='off' placeholder='${esc(w("signin.type"))}'>"
        } else if (sorted.size > 8) {
            val letters = listOf("") + sorted.map { it.name.take(1).uppercase() }.toSortedSet()
            find = "<div class='letters'>" + letters.joinToString("") {
                "<button type='button' data-letter='${esc(it)}' aria-pressed='${it.isEmpty()}'>${esc(it).ifEmpty { "★" }}</button>"
            } + "</div>"
        }
        return "<div id='lastone' style='display:none' data-label='${esc(w("signin.last"))}' data-im='${esc(w("signin.im"))}'></div>$find" +
            "<div class='list' id='kids'>$rows</div><p class='meta' id='nobody' style='display:none'>${esc(w("signin.none"))}</p>" +
            "<a class='button big secondary' href='/new'>${esc(w("signin.not_here"))}</a>"
    }

    /** Are you Lena? Then the secret picture or PIN, when the teacher turned it on. */
    private fun whoPage(req: Request, wrong: String = ""): Response {
        val w = phone.words(req)
        val p = person(req.query["n"]?.ifEmpty { null } ?: req.form["n"]) ?: return Response.redirect("/")
        val head = "<div class='done'>${face(p)}</div>"
        val hidden = "<input type='hidden' name='n' value='${esc(p.name)}'>"
        val note = if (wrong.isEmpty()) "" else "<p class='error' role='alert'>${esc(wrong)}</p>"
        val back = "<a class='button big secondary' href='/'>${esc(if (needsSecret(p)) w("form.change") else w("signin.no"))}</a>"
        if (!needsSecret(p)) {
            return Response(page(req, w("signin.are_you", "name" to p.name), "$head<div class='yesno'>$back<form method='post' action='/who'>$hidden" +
                "<button class='big yes'>${icon("check")} ${esc(w("signin.yes"))}</button></form></div>"))
        }
        if (ways[p.look] == "secret") {
            val buttons = secretChoices(p).joinToString("") {
                "<button name='secret' value='${esc(it)}' aria-label='${esc(Html.pictureName(it))}'>${picture(it)}</button>"
            }
            return Response(page(req, p.name, "$head<p class='lead'>${esc(w("signin.secret"))}</p>$note" +
                "<form method='post' action='/who' class='secrets'>$hidden$buttons</form>$back"))
        }
        return Response(page(req, p.name, "$head<form method='post' action='/who' class='card'>$hidden" +
            "<label for='pin'>${esc(w("signin.pin"))}</label>$note<input type='password' id='pin' name='secret' " +
            "class='pin' inputmode='numeric' pattern='[0-9]*' maxlength='4' autocomplete='off' required autofocus>" +
            "<button class='big'>${esc(w("welcome.start"))}</button></form>$back"))
    }

    /** Six pictures, the right one among them, always in the same order for one student. */
    private fun secretChoices(p: Person): List<String> {
        val right = SECRET_PICTURES.firstOrNull { hash(p.name, it) == p.secret } ?: SECRET_PICTURES[0]
        val others = SECRET_PICTURES.filter { it != right }.sortedBy { hash(p.name + "#", it) }.take(5)
        return (listOf(right) + others).sortedBy { hash(p.name + "@", it) }
    }

    private fun whoSubmit(req: Request): Response {
        val w = phone.words(req)
        val p = person(req.form["n"]) ?: return Response.redirect("/")
        if (!needsSecret(p)) return signIn(p)
        synchronized(tries) {
            var (count, since) = tries[p.name] ?: (0 to 0.0)
            if (count >= MAX_TRIES && Messenger.now() - since < LOCK_S) return whoPage(req, w("signin.wait"))
            if (count >= MAX_TRIES) count = 0
            if (same(hash(p.name, (req.form["secret"] ?: "").take(20)), p.secret)) {
                tries.remove(p.name)
                return signIn(p)
            }
            tries[p.name] = (count + 1) to Messenger.now()
            return whoPage(req, if (ways[p.look] == "pin") w("signin.pin_wrong")
                else w("signin.wrong") + (if (count >= 2) " " + w("signin.help") else ""))
        }
    }

    /** A student who is not on the list: a name and a picture, and they can start. */
    private fun newPage(req: Request): Response {
        val w = phone.words(req)
        val taken = people().map { it.pic }.toSet()
        val pics = STUDENT_PICTURES.filter { it !in taken }.take(12).joinToString("") {
            "<label class='picpick'><input type='radio' name='pic' value='$it'><span>${picture(it)}</span></label>"
        }
        return Response(page(req, w("signin.new_title"), "<p class='lead'>${esc(w("signin.new_text"))}</p>" +
            "<form method='post' action='/login' class='card'><label for='student'>${esc(w("welcome.name"))}</label>" +
            "<input type='text' id='student' name='student' maxlength='40' autocomplete='off' required>" +
            "<p><strong>${esc(w("signin.pick"))}</strong></p><div class='picks'>$pics</div>" +
            "<button class='big'>${esc(w("welcome.start"))}</button></form>"))
    }

    fun login(req: Request): Response {
        val name = (req.form["student"] ?: "").replace("~", " ").trim().split(Regex("\\s+")).filter { it.isNotEmpty() }.joinToString(" ").take(40)
        if (name.isEmpty()) return Response.redirect("/")
        if (roster.isNotEmpty()) {
            val p = person(name)
            if (p != null) return if (needsSecret(p)) Response.redirect("/who?n=" + quote(p.name, "")) else signIn(p)
            val pic = req.form["pic"] ?: ""
            db.execute("INSERT OR REPLACE INTO newcomers (name, pic, look) VALUES (?, ?, ?)", name,
                if (pic in STUDENT_PICTURES) pic else "", phone.look(req))
        }
        return Response.redirect("/").setCookie("student", name)
    }

    /** Finished? Pass the phone on. */
    private fun mePage(req: Request): Response {
        val w = phone.words(req)
        val name = student(req) ?: return Response.redirect("/")
        val p = person(name) ?: Person(name, "", "mid", "")
        val idle = if (signoutMin > 0 && roster.isNotEmpty()) "<p class='meta'>${esc(w("signin.idle", "n" to signoutMin))}</p>" else ""
        return Response(page(req, w("signin.finished", "name" to name), "<div class='done'>${face(p)}<p>${esc(w("signin.saved"))}</p></div>" +
            "<form method='post' action='/logout'><button class='big'>${esc(w("signin.pass"))}</button></form>" +
            "<a class='button big secondary' href='/'>${esc(w("signin.keep"))}</a>$idle"))
    }

    /** Goes back to "Who are you?" by itself when nobody taps for a while (IDLE_JS). */
    fun idleForm(signedIn: Boolean): String = if (!signedIn || roster.isEmpty() || signoutMin == 0L) "" else
        "<form method='post' action='/logout' id='idleout' data-min='$signoutMin'></form><script>${Html.IDLE_JS}</script>"

    companion object {
        val LOOK_ORDER = listOf("young", "mid", "teen")
        private const val MAX_TRIES = 5
        private const val LOCK_S = 60
        val STUDENT_PICTURES = ("cat lion butterfly fish flower bird sun tree boat apple star dog moon frog heart ball leaf hen cow goat rabbit " +
            "snail banana carrot maize mango cloud mountain fire car bicycle hat bee elephant turtle monkey").split(" ").filter { Html.hasPicture(it) }
        val SECRET_PICTURES = "apple ball house star moon key fish tree sun flower boat heart".split(" ").filter { Html.hasPicture(it) }

        private fun hex(b: ByteArray) = b.joinToString("") { "%02x".format(it) }

        /** How the class list carries a secret. Kept the same as secret_hash in phonesignin.py. */
        fun hash(name: String, secret: String): String =
            hex(MessageDigest.getInstance("SHA-256").digest("$name|$secret".toByteArray(Charsets.UTF_8))).take(32)

        private fun same(a: String, b: String) = MessageDigest.isEqual(a.toByteArray(Charsets.UTF_8), b.toByteArray(Charsets.UTF_8))
    }
}
