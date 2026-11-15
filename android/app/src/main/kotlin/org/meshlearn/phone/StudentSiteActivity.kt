package org.meshlearn.phone

import android.annotation.SuppressLint
import android.app.Activity
import android.os.Build
import android.os.Bundle
import android.window.OnBackInvokedDispatcher
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout

/** The student site inside the app. Pages opened here count as "on the phone itself" (teacher actions allowed). */
class StudentSiteActivity : Activity() {
    private lateinit var web: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        web = WebView(this)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.webViewClient = WebViewClient()
        // A WebView ignores its own padding, so pad a frame around it instead.
        val frame = FrameLayout(this).apply { addView(web) }
        padForSystemBars(frame)
        setContentView(frame)
        title = intent.getStringExtra(EXTRA_TITLE) ?: "Student site"
        if (savedInstanceState == null) web.loadUrl(intent.getStringExtra(EXTRA_URL) ?: "http://127.0.0.1:8080/")
        else web.restoreState(savedInstanceState)
        if (Build.VERSION.SDK_INT >= 33) {
            // Android 13+ back gesture: go back a page in the site before leaving it.
            onBackInvokedDispatcher.registerOnBackInvokedCallback(OnBackInvokedDispatcher.PRIORITY_DEFAULT) {
                if (web.canGoBack()) web.goBack() else finish()
            }
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        web.saveState(outState)
    }

    @Deprecated("Android 12 and older; newer versions use the callback above")
    @Suppress("GestureBackNavigation")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else @Suppress("DEPRECATION") super.onBackPressed()
    }

    companion object {
        const val EXTRA_URL = "url"
        const val EXTRA_TITLE = "title"
    }
}
