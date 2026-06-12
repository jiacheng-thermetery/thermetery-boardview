package com.thermetery.boardview

import android.content.Context
import android.util.Log
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * Owns the embedded Python interpreter and the single background thread all
 * Python calls run on (Python is effectively single-threaded under the GIL —
 * contract docs/android_contract.md §6).
 *
 * `start()` is called from [BoardviewApplication.onCreate]; the actual
 * `Python.start()` happens on the worker thread, so the UI thread is never
 * blocked. Because every later call is funneled through the same
 * single-thread executor, callers are guaranteed to run after startup
 * completed (or observe the startup failure).
 */
object PythonRuntime {
    private const val TAG = "BoardviewPy"

    private val executor: ExecutorService = Executors.newSingleThreadExecutor { r ->
        Thread(r, "python-worker")
    }

    /** Start Python on the worker thread and log the native-kernel ping. */
    fun start(context: Context) {
        val appContext = context.applicationContext
        executor.execute {
            try {
                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(appContext))
                }
                // Contract §2: ping() reports whether each native kernel
                // loaded — proves the jniLibs + bare-soname loader path
                // works on-device. Log it at startup.
                val ping = boardExport().callAttr("ping").toString()
                Log.i(TAG, "board_export.ping(): $ping")
            } catch (t: Throwable) {
                // Later submit() blocks will surface the failure through
                // their own catch paths (Python.getInstance throws).
                Log.e(TAG, "Python startup failed", t)
            }
        }
    }

    /**
     * Run [block] on the Python worker thread. Ordering after [start] is
     * guaranteed by the single-thread executor. The block must catch its
     * own exceptions and report them to the renderer.
     */
    fun submit(block: () -> Unit) {
        executor.execute(block)
    }

    /** The `board_export` module. Must be called on the worker thread. */
    fun boardExport(): PyObject = Python.getInstance().getModule("board_export")
}
