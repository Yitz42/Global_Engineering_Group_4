package org.meshlearn.phone

import android.os.Build
import android.view.View
import android.view.WindowInsets

/**
 * Android 15+ draws apps edge to edge, behind the status bar, title bar and gesture bar. The
 * top inset Android passes to the content already includes the title bar, so padding by the
 * insets keeps everything visible. Older versions lay out normally and need nothing.
 */
fun padForSystemBars(view: View) {
    val base = intArrayOf(view.paddingLeft, view.paddingTop, view.paddingRight, view.paddingBottom)
    view.setOnApplyWindowInsetsListener { v, insets ->
        if (Build.VERSION.SDK_INT >= 35) {
            val b = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout() or WindowInsets.Type.ime())
            v.setPadding(base[0] + b.left, base[1] + b.top, base[2] + b.right, base[3] + b.bottom)
        }
        insets
    }
}
