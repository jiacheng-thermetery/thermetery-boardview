package com.thermetery.boardview

import android.annotation.SuppressLint
import android.app.AlertDialog
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.net.Uri
import android.os.Bundle
import android.provider.OpenableColumns
import android.text.InputType
import android.util.Log
import android.webkit.ConsoleMessage
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import org.json.JSONObject
import java.io.File
import java.io.IOException
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Single fullscreen-WebView activity hosting the JS renderer
 * (assets/viewer.html, owned by the renderer agent). Implements the shell
 * side of the contract (docs/android_contract.md §4 and §6):
 *
 *  - `window.Android` bridge: openFilePicker / loadTraces / log
 *  - SAF ACTION_OPEN_DOCUMENT + ACTION_VIEW intents from file managers
 *  - copies the picked stream to cacheDir/boards/<displayName> and KEEPS it
 *    for the whole session (load_traces re-reads the path lazily)
 *  - parses on the single Python worker thread, never the UI thread
 *  - key_required: native dialog, 3 retries (mirrors
 *    viewer.py:_load_with_key_prompt), optional remember-on-device
 */
class MainActivity : ComponentActivity() {

    companion object {
        private const val TAG = "Boardview"

        /** Mirrors viewer.py:_load_with_key_prompt — three prompts, then give up. */
        private const val MAX_KEY_PROMPTS = 3

        /** SAF cannot filter on extension; accept everything and let the parser decide. */
        private val PICKER_MIME_TYPES = arrayOf("*/*")
    }

    private lateinit var webView: WebView
    private var pageReady = false
    private val pendingJs = ArrayDeque<String>()

    @Volatile
    private var boardOpen = false
    private val tracesInFlight = AtomicBoolean(false)

    private val openDocument =
        registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
            if (uri != null) handleIncomingUri(uri)
        }

    // ---------------------------------------------------------------- setup

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        webView = WebView(this)
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            // file:///android_asset is always allowed regardless, but be explicit.
            allowFileAccess = true
        }
        webView.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView?, url: String?) {
                if (!pageReady) {
                    pageReady = true
                    while (pendingJs.isNotEmpty()) {
                        webView.evaluateJavascript(pendingJs.removeFirst(), null)
                    }
                }
            }
        }
        webView.webChromeClient = object : WebChromeClient() {
            override fun onConsoleMessage(message: ConsoleMessage): Boolean {
                Log.i(
                    "BoardviewJS",
                    "${message.message()} (${message.sourceId()}:${message.lineNumber()})",
                )
                return true
            }
        }
        if (applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE != 0) {
            WebView.setWebContentsDebuggingEnabled(true)
        }
        webView.addJavascriptInterface(BoardviewBridge(this), "Android")
        setContentView(webView)
        webView.loadUrl("file:///android_asset/viewer.html")

        maybeHandleViewIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        maybeHandleViewIntent(intent)
    }

    private fun maybeHandleViewIntent(intent: Intent?) {
        if (intent?.action == Intent.ACTION_VIEW) {
            intent.data?.let { handleIncomingUri(it) }
        }
    }

    // ------------------------------------------------------- bridge entries

    /** Called (on the UI thread) from BoardviewBridge.openFilePicker. */
    fun launchFilePicker() {
        try {
            openDocument.launch(PICKER_MIME_TYPES)
        } catch (t: Throwable) {
            Log.e(TAG, "Could not launch SAF picker", t)
            postError("Could not open the file picker: ${t.message}")
        }
    }

    /** Called (on the UI thread) from BoardviewBridge.openKeyManager. */
    fun launchKeyManager() {
        startActivity(Intent(this, KeyManagerActivity::class.java))
    }

    /** Called (on a WebView thread) from BoardviewBridge.loadTraces. */
    fun requestTraces() {
        if (!boardOpen) {
            postError("Open a board first.")
            return
        }
        if (!tracesInFlight.compareAndSet(false, true)) return
        postStatus("Building topology…")
        PythonRuntime.submit {
            try {
                val result = PythonRuntime.boardExport().callAttr("load_traces").toString()
                val json = JSONObject(result)
                if (json.optBoolean("ok")) {
                    // The exporter returns validated JSON. Inject it as an object
                    // literal so large trace payloads aren't escaped into a second
                    // string and parsed yet again by viewer.js.
                    runJs("window.bv && bv.onTraces($result);")
                } else {
                    val reason = json.optString("reason")
                        .ifEmpty { json.optString("error", "unknown error") }
                    postError("Trace build failed: $reason")
                }
            } catch (t: Throwable) {
                Log.e(TAG, "load_traces crashed", t)
                postError("Trace build failed: ${t.message}")
            } finally {
                tracesInFlight.set(false)
                postStatus("")
            }
        }
    }

    // ------------------------------------------------------------ file open

    private fun handleIncomingUri(uri: Uri) {
        val displayName = displayNameFor(uri)
        postStatus("Parsing $displayName…")
        PythonRuntime.submit {
            val dest: File = try {
                val boardsDir = File(cacheDir, "boards").apply { mkdirs() }
                val f = File(boardsDir, sanitizeFileName(displayName))
                contentResolver.openInputStream(uri).use { input ->
                    if (input == null) throw IOException("content stream unavailable")
                    f.outputStream().use { out -> input.copyTo(out) }
                }
                // Kept for the whole session — lazy topology re-reads this
                // path later. Do NOT delete after parse (contract §6).
                f
            } catch (t: Throwable) {
                Log.e(TAG, "Copy failed for $uri", t)
                postError("Could not read $displayName: ${t.message}")
                postStatus("")
                return@submit
            }
            val rememberedFormat = rememberedKeyFormat(displayName)
            val rememberedKey = rememberedFormat?.let { KeyVault.load(this, it) }
            parseBoard(
                ParseAttempt(
                    file = dest,
                    displayName = displayName,
                    key = rememberedKey,
                    promptsUsed = 0,
                    // Known encrypted extensions were checked before parsing,
                    // even when no saved key was present.
                    triedRemembered = rememberedFormat != null,
                    rememberFormat = null,
                )
            )
        }
    }

    private fun displayNameFor(uri: Uri): String {
        if (uri.scheme == "content") {
            try {
                contentResolver.query(
                    uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null,
                )?.use { c ->
                    if (c.moveToFirst()) {
                        val idx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                        if (idx >= 0) {
                            c.getString(idx)?.takeIf { it.isNotBlank() }?.let { return it }
                        }
                    }
                }
            } catch (t: Throwable) {
                Log.w(TAG, "DISPLAY_NAME query failed for $uri", t)
            }
        }
        return uri.lastPathSegment?.substringAfterLast('/')?.takeIf { it.isNotBlank() }
            ?: "board"
    }

    private fun sanitizeFileName(name: String): String =
        name.replace(Regex("[\\\\/:*?\"<>|]"), "_")

    /** Key slot implied by a filename, before an expensive keyless parse. */
    private fun rememberedKeyFormat(displayName: String): String? =
        when (displayName.substringAfterLast('.', "").lowercase()) {
            "fz" -> "fz"
            "pcb" -> "xzzpcb"
            else -> null
        }

    // ------------------------------------------------------------- parsing

    private data class ParseAttempt(
        val file: File,
        val displayName: String,
        val key: String?,
        /** User prompts consumed so far (the silent remembered-key retry is free). */
        val promptsUsed: Int,
        val triedRemembered: Boolean,
        /** Non-null: persist the key under this format slot on success. */
        val rememberFormat: String?,
    )

    /** May be called from any thread; the Python call runs on the worker. */
    private fun parseBoard(attempt: ParseAttempt) {
        postStatus("Parsing ${attempt.displayName}…")
        PythonRuntime.submit {
            val result: String = try {
                PythonRuntime.boardExport()
                    .callAttr("open_board", attempt.file.absolutePath, attempt.key)
                    .toString()
            } catch (t: Throwable) {
                Log.e(TAG, "open_board crashed", t)
                postError("Could not load ${attempt.displayName}: ${t.message}")
                postStatus("")
                return@submit
            }
            handleParseResult(attempt, result)
        }
    }

    /** Runs on the Python worker thread. */
    private fun handleParseResult(attempt: ParseAttempt, result: String) {
        val json = try {
            JSONObject(result)
        } catch (t: Throwable) {
            Log.e(TAG, "open_board returned malformed JSON", t)
            postError("Could not load ${attempt.displayName}: malformed exporter result")
            postStatus("")
            return
        }

        if (json.optBoolean("ok")) {
            val fmt = attempt.rememberFormat
            val key = attempt.key
            if (fmt != null && key != null) KeyVault.save(this, fmt, key)
            boardOpen = true
            runJs("window.bv && bv.onBoard($result);")
            postStatus("")
            return
        }

        val error = json.optString("error")
        val reason = json.optString("reason")
        if (error != "key_required") {
            postError(
                "Could not load ${attempt.displayName}: " +
                    reason.ifEmpty { error.ifEmpty { "unknown error" } }
            )
            postStatus("")
            return
        }

        val format = json.optString("format").ifEmpty { "unknown" }

        // Contract §6: pass a remembered key automatically on the first
        // key_required failure of that format (does not count as a prompt).
        if (!attempt.triedRemembered) {
            val remembered = KeyVault.load(this, format)
            if (remembered != null) {
                parseBoard(attempt.copy(key = remembered, triedRemembered = true))
                return
            }
        }

        if (attempt.promptsUsed >= MAX_KEY_PROMPTS) {
            // Mirrors viewer.py's "giving up after several attempts" warning.
            postError(
                "Giving up after several attempts — ${attempt.displayName} " +
                    "cannot open without a valid key."
            )
            postStatus("")
            return
        }

        val failurePrefix = when {
            attempt.key == null -> null
            attempt.promptsUsed == 0 -> "The remembered key did not work."
            else -> "That key did not work."
        }

        runOnUiThread {
            postStatus("")
            showKeyDialog(
                displayName = attempt.displayName,
                format = format,
                failurePrefix = failurePrefix,
                onSubmit = { key, remember ->
                    parseBoard(
                        attempt.copy(
                            key = key,
                            promptsUsed = attempt.promptsUsed + 1,
                            triedRemembered = true,
                            rememberFormat = if (remember) format else null,
                        )
                    )
                },
                onGiveUp = {
                    postError(
                        "Could not open ${attempt.displayName} — a valid key is required."
                    )
                    postStatus("")
                },
            )
        }
    }

    // ----------------------------------------------------------- key dialog

    /** Must be called on the UI thread. */
    private fun showKeyDialog(
        displayName: String,
        format: String,
        failurePrefix: String?,
        onSubmit: (key: String, remember: Boolean) -> Unit,
        onGiveUp: () -> Unit,
    ) {
        // Format-specific wording per contract §6
        // (ASUS .fz = 44 hex words / XZZ .pcb = 16 hex digits).
        val (title, ask) = when (format) {
            "fz" -> "ASUS FZ key required" to
                "$displayName is an RC6-encrypted ASUS .fz file and no usable " +
                "key was available.\n\nPaste the FZKey (44 × 32-bit hex words):"
            "xzzpcb", "xzz" -> "XZZ key required" to
                "$displayName is a DES-encrypted XZZ .pcb file and no valid " +
                "key was available.\n\nPaste the XZZ key (16 hex digits):"
            else -> "Decryption key required" to
                "$displayName needs a decryption key.\n\nPaste the key:"
        }
        val message = if (failurePrefix != null) "$failurePrefix\n\n$ask" else ask

        val density = resources.displayMetrics.density
        val pad = (20 * density).toInt()

        val input = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT or
                InputType.TYPE_TEXT_FLAG_MULTI_LINE or
                InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            isSingleLine = false
            minLines = 2
            maxLines = 6
            setHorizontallyScrolling(false)
        }
        val remember = CheckBox(this).apply {
            text = getString(R.string.key_dialog_remember)
        }
        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad / 2, pad, 0)
            addView(input)
            addView(remember)
        }

        AlertDialog.Builder(this)
            .setTitle(title)
            .setMessage(message)
            .setView(container)
            .setPositiveButton(R.string.key_dialog_open) { _, _ ->
                val entered = input.text.toString().trim()
                if (entered.isEmpty()) onGiveUp()       // empty == cancel (viewer.py)
                else onSubmit(entered, remember.isChecked)
            }
            .setNegativeButton(R.string.key_dialog_cancel) { _, _ -> onGiveUp() }
            .setOnCancelListener { onGiveUp() }
            .show()
    }

    // ------------------------------------------------------- JS dispatching

    /** Queue JS until viewer.html finished loading, then evaluate in order. */
    private fun runJs(script: String) {
        runOnUiThread {
            if (pageReady) webView.evaluateJavascript(script, null)
            else pendingJs.addLast(script)
        }
    }

    private fun postStatus(text: String) =
        runJs("window.bv && bv.onStatus(${JSONObject.quote(text)});")

    private fun postError(text: String) {
        Log.w(TAG, "Error -> renderer: $text")
        runJs("window.bv && bv.onError(${JSONObject.quote(text)});")
    }
}
