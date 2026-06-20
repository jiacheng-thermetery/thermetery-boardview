package com.thermetery.boardview

import android.content.Context
import android.util.Log
import java.io.File
import java.io.IOException

/**
 * Remembered decryption keys: `filesDir/keys/<format>.txt` (contract §6).
 * Plain files in app-internal storage; `android:allowBackup="false"` keeps
 * them out of cloud backups.
 */
object KeyVault {
    private const val TAG = "Boardview"

    private fun keyFile(context: Context, format: String): File {
        val safe = format.replace(Regex("[^A-Za-z0-9._-]"), "_")
        return File(File(context.filesDir, "keys"), "$safe.txt")
    }

    /** The remembered key for [format], or null if none is stored. */
    fun load(context: Context, format: String): String? = try {
        keyFile(context, format)
            .takeIf { it.isFile }
            ?.readText(Charsets.UTF_8)
            ?.trim()
            ?.takeIf { it.isNotEmpty() }
    } catch (e: IOException) {
        Log.w(TAG, "Could not read remembered key for $format", e)
        null
    }

    /** Persist a working key for [format] ("remember on this device"). */
    fun save(context: Context, format: String, key: String) {
        try {
            val f = keyFile(context, format)
            f.parentFile?.mkdirs()
            f.writeText(key + "\n", Charsets.UTF_8)
            Log.i(TAG, "Remembered key for $format at ${f.path}")
        } catch (e: IOException) {
            Log.w(TAG, "Could not save key for $format (still usable this session)", e)
        }
    }

    /** True if a remembered key exists for [format]. */
    fun has(context: Context, format: String): Boolean = load(context, format) != null

    /** Forget the remembered key for [format]. Returns true if one was removed. */
    fun clear(context: Context, format: String): Boolean = try {
        val f = keyFile(context, format)
        if (f.isFile) {
            val ok = f.delete()
            Log.i(TAG, "Cleared remembered key for $format ($ok)")
            ok
        } else {
            false
        }
    } catch (e: SecurityException) {
        Log.w(TAG, "Could not clear key for $format", e)
        false
    }
}
