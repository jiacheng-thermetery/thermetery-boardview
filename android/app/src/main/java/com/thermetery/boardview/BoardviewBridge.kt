package com.thermetery.boardview

import android.util.Log
import android.webkit.JavascriptInterface

/**
 * `window.Android` — the JS-to-Kotlin bridge (contract
 * docs/android_contract.md §4). Methods are invoked on a WebView-internal
 * thread, never the UI thread; everything is forwarded accordingly.
 */
class BoardviewBridge(private val activity: MainActivity) {

    @JavascriptInterface
    fun openFilePicker() {
        activity.runOnUiThread { activity.launchFilePicker() }
    }

    @JavascriptInterface
    fun openFolderPicker() {
        // eM-Test Expert .asc boards are folders of sibling files; the
        // tree grant lets the shell copy the whole set (contract §4/§6).
        activity.runOnUiThread { activity.launchFolderPicker() }
    }

    @JavascriptInterface
    fun loadTraces() {
        activity.requestTraces()
    }

    @JavascriptInterface
    fun openKeyManager() {
        activity.runOnUiThread { activity.launchKeyManager() }
    }

    @JavascriptInterface
    fun log(msg: String) {
        Log.i("BoardviewJS", msg)
    }
}
