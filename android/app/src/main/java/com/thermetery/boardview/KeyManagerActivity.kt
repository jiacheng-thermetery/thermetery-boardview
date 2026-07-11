package com.thermetery.boardview

import android.graphics.Color
import android.os.Bundle
import android.text.InputType
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import org.json.JSONObject

/**
 * Standalone "Decryption keys" screen, reachable any time from the viewer
 * toolbar — unlike the open-a-board key prompt, this lets the user provision
 * keys up front, load them from a file (no 44-word hand-typing), validate
 * them offline, and clear them.
 *
 * Keys persist via [KeyVault] to `filesDir/keys/<format>.txt`; the same files
 * the open-board flow reads. Validation is delegated to
 * `board_export.validate_key` on the Python worker (ASUS .fz is fully checked
 * via RC6 parity; XZZ .pcb is structurally checked only).
 */
class KeyManagerActivity : ComponentActivity() {

    private data class Slot(
        val format: String,      // KeyVault slug, also board_export format
        val title: String,
        val hint: String,
        val input: EditText,
        val status: TextView,
    )

    private val slots = mutableListOf<Slot>()

    /** Which slot's "Load from file" is awaiting a SAF result. */
    private var pendingFileFormat: String? = null

    private val openKeyFile =
        registerForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
            val fmt = pendingFileFormat
            pendingFileFormat = null
            if (uri == null || fmt == null) return@registerForActivityResult
            try {
                val text = contentResolver.openInputStream(uri)?.use { stream ->
                    stream.readBytes().toString(Charsets.UTF_8)
                } ?: ""
                slots.firstOrNull { it.format == fmt }?.let { slot ->
                    slot.input.setText(text.trim())
                    validate(slot)
                }
            } catch (t: Throwable) {
                toast("Could not read key file: ${t.message}")
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val d = resources.displayMetrics.density
        fun dp(v: Int) = (v * d).toInt()

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#0d1024"))
            setPadding(dp(20), dp(24), dp(20), dp(24))
        }

        root.addView(TextView(this).apply {
            text = "Decryption keys"
            setTextColor(Color.WHITE)
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 22f)
        })
        root.addView(TextView(this).apply {
            text = "Keys are stored only on this device (excluded from backups) " +
                "and supplied automatically when you open an encrypted board."
            setTextColor(Color.parseColor("#8b93b8"))
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 13f)
            setPadding(0, dp(6), 0, dp(16))
        })

        addSlot(
            root, ::dp,
            format = "fz",
            title = "ASUS  ·  .fz  (RC6)",
            hint = "Paste the FZKey — 44 × 32-bit hex words. Fully verified here.",
        )
        addSlot(
            root, ::dp,
            format = "xzzpcb",
            title = "XZZ  ·  .pcb  (DES)",
            hint = "Paste the XZZ key (hex). Confirmed only by opening a board.",
        )

        root.addView(Button(this).apply {
            text = "Done"
            setOnClickListener { finish() }
            val lp = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            )
            lp.topMargin = dp(8)
            layoutParams = lp
        })

        setContentView(ScrollView(this).apply {
            setBackgroundColor(Color.parseColor("#0d1024"))
            addView(root)
        })

        refreshStatuses()
    }

    private fun addSlot(parent: LinearLayout, dp: (Int) -> Int, format: String, title: String, hint: String) {
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(Color.parseColor("#161b34"))
            setPadding(dp(16), dp(14), dp(16), dp(14))
            val lp = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT,
            )
            lp.bottomMargin = dp(14)
            layoutParams = lp
        }

        card.addView(TextView(this).apply {
            text = title
            setTextColor(Color.parseColor("#cdd6ff"))
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
        })
        card.addView(TextView(this).apply {
            text = hint
            setTextColor(Color.parseColor("#8b93b8"))
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            setPadding(0, dp(2), 0, dp(8))
        })

        val input = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT or
                InputType.TYPE_TEXT_FLAG_MULTI_LINE or
                InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            isSingleLine = false
            minLines = 2
            maxLines = 6
            setHorizontallyScrolling(false)
            setTextColor(Color.WHITE)
            setHintTextColor(Color.parseColor("#5b6488"))
            setHint("(no key set)")
        }
        card.addView(input)

        val status = TextView(this).apply {
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            setPadding(0, dp(6), 0, dp(8))
        }
        card.addView(status)

        val slot = Slot(format, title, hint, input, status)
        slots += slot

        val buttons = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        fun barButton(label: String, onClick: () -> Unit) = Button(this).apply {
            text = label
            setTextSize(TypedValue.COMPLEX_UNIT_SP, 12f)
            setOnClickListener { onClick() }
            val lp = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            lp.marginEnd = dp(4)
            layoutParams = lp
        }
        buttons.addView(barButton("Load file") {
            pendingFileFormat = format
            try {
                openKeyFile.launch(arrayOf("*/*"))
            } catch (t: Throwable) {
                toast("Could not open file picker: ${t.message}")
            }
        })
        buttons.addView(barButton("Validate") { validate(slot) })
        buttons.addView(barButton("Save") { save(slot) })
        buttons.addView(barButton("Clear") { clear(slot) })
        card.addView(buttons)

        parent.addView(card)
    }

    // ----------------------------------------------------------------- actions

    private fun validate(slot: Slot) {
        val text = slot.input.text.toString().trim()
        if (text.isEmpty()) {
            setStatus(slot, "Not set.", neutral = true)
            return
        }
        setStatus(slot, "Checking…", neutral = true)
        PythonRuntime.submit {
            val result = try {
                PythonRuntime.boardExport().callAttr("validate_key", slot.format, text).toString()
            } catch (t: Throwable) {
                """{"ok":false,"status":"error","message":"${t.message}"}"""
            }
            runOnUiThread { applyValidation(slot, result) }
        }
    }

    /** Returns the validation status string after applying it to the UI. */
    private fun applyValidation(slot: Slot, resultJson: String): String {
        val json = try { JSONObject(resultJson) } catch (t: Throwable) { JSONObject() }
        val ok = json.optBoolean("ok")
        val status = json.optString("status", if (ok) "ok" else "error")
        val message = json.optString("message", status)
        // Green for valid/unverified-but-well-formed; amber for unverified;
        // red for malformed/invalid/error.
        val good = status == "valid"
        val warn = status == "unverified"
        setStatus(slot, message, good = good, warn = warn)
        return status
    }

    private fun save(slot: Slot) {
        val text = slot.input.text.toString().trim()
        if (text.isEmpty()) {
            toast("Nothing to save — paste or load a key first.")
            return
        }
        setStatus(slot, "Checking…", neutral = true)
        PythonRuntime.submit {
            val result = try {
                PythonRuntime.boardExport().callAttr("validate_key", slot.format, text).toString()
            } catch (t: Throwable) {
                """{"ok":false,"status":"malformed","message":"${t.message}"}"""
            }
            runOnUiThread {
                val status = applyValidation(slot, result)
                if (status != "valid" && status != "unverified") {
                    // Definitively not a key — refuse to store junk.
                    toast("Not saved — the key is not well-formed.")
                } else {
                    KeyVault.save(this, slot.format, text)
                    toast("Key saved for ${slot.format}.")
                    refreshStatuses()
                }
            }
        }
    }

    private fun clear(slot: Slot) {
        val had = KeyVault.clear(this, slot.format)
        slot.input.setText("")
        refreshStatuses()
        toast(if (had) "Key cleared for ${slot.format}." else "No saved key to clear.")
    }

    // ------------------------------------------------------------------- utils

    /** Prefill saved keys and reflect saved/not-set state in each status line. */
    private fun refreshStatuses() {
        for (slot in slots) {
            val saved = KeyVault.load(this, slot.format)
            if (saved != null) {
                if (slot.input.text.isNullOrEmpty()) slot.input.setText(saved)
                setStatus(slot, "Saved on this device.", good = true)
            } else if (slot.input.text.isNullOrEmpty()) {
                setStatus(slot, "Not set.", neutral = true)
            }
        }
    }

    private fun setStatus(
        slot: Slot,
        text: String,
        good: Boolean = false,
        warn: Boolean = false,
        neutral: Boolean = false,
    ) {
        slot.status.text = text
        slot.status.setTextColor(
            when {
                good -> Color.parseColor("#5bd97a")
                warn -> Color.parseColor("#ffc14d")
                neutral -> Color.parseColor("#8b93b8")
                else -> Color.parseColor("#ff7a6b")
            }
        )
    }

    private fun toast(msg: String) =
        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
}
