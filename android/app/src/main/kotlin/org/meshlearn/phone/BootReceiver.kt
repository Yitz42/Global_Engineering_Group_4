package org.meshlearn.phone

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/** Starts the student site again after the phone reboots, if it was running before. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        val s = Settings(context)
        if (!s.autostart || !s.wantRunning) return
        try {
            PhoneService.start(context)
        } catch (e: IllegalStateException) {
            // Android 15 and later also send this when a force-stopped app is opened again, and refuse the start then.
            // The app's own screen starts the service then (MainActivity.onResume).
            Log.w("BootReceiver", "not allowed to start yet", e)
        }
    }
}
