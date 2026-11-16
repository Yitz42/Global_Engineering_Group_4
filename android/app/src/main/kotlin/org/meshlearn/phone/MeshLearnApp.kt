package org.meshlearn.phone

import android.app.Application
import android.content.Context
import android.os.Build
import java.io.File
import java.io.PrintWriter
import java.io.StringWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Keeps what went wrong if MeshLearn ever stops unexpectedly, so the phone keeper can send it
 * to whoever looks after the app. Phones in the field have no other way to report a crash.
 */
class MeshLearnApp : Application() {
    override fun onCreate() {
        super.onCreate()
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, error ->
            runCatching { save(this, thread, error) }
            previous?.uncaughtException(thread, error)
        }
    }

    companion object {
        private fun file(context: Context) = File(context.filesDir, "last-crash.txt")

        private fun save(context: Context, thread: Thread, error: Throwable) {
            val trace = StringWriter().also { error.printStackTrace(PrintWriter(it)) }.toString()
            val version = runCatching { context.packageManager.getPackageInfo(context.packageName, 0).versionName }.getOrNull() ?: "?"
            val time = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date())
            file(context).writeText(
                "MeshLearn $version stopped unexpectedly at $time\n" +
                    "Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT}), ${Build.MANUFACTURER} ${Build.MODEL}\n" +
                    "Thread: ${thread.name}\n\n" + trace.take(20_000)
            )
        }

        /** The last crash report, or null if MeshLearn has not crashed since it was last cleared. */
        fun lastCrash(context: Context): String? = file(context).takeIf { it.exists() }?.readText()

        fun clearCrash(context: Context) {
            file(context).delete()
        }
    }
}
