package com.thermetery.boardview

import android.app.Application

class BoardviewApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        // Contract §6: Python startup in Application.onCreate on a
        // background thread. PythonRuntime runs Python.start() on its
        // worker thread and logs board_export.ping() once up.
        PythonRuntime.start(this)
    }
}
