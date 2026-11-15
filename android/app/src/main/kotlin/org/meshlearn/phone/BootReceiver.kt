package org.meshlearn.phone

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Starts the student site again after the phone reboots, if it was running before. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        val s = Settings(context)
        if (s.autostart && s.wantRunning) PhoneService.start(context)
    }
}
