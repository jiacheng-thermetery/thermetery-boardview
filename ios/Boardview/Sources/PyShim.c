// See PyShim.h. Init sequence follows the Python-Apple-support testbed
// (vendor/testbed/TestbedTests/TestbedTests.m).
#include "PyShim.h"

#include <Python/Python.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static PyObject *g_board_export = NULL;

static int set_config_string(PyConfig *config, wchar_t **field,
                             const char *value) {
    wchar_t *decoded = Py_DecodeLocale(value, NULL);
    if (decoded == NULL) {
        return -1;
    }
    PyStatus status = PyConfig_SetString(config, field, decoded);
    PyMem_RawFree(decoded);
    return PyStatus_Exception(status) ? -1 : 0;
}

int bv_py_start(const char *python_home,
                const char *app_dir,
                const char *app_packages_dir) {
    PyPreConfig preconfig;
    PyPreConfig_InitIsolatedConfig(&preconfig);
    preconfig.utf8_mode = 1;
    PyStatus status = Py_PreInitialize(&preconfig);
    if (PyStatus_Exception(status)) {
        fprintf(stderr, "bv_py_start: pre-init failed: %s\n",
                status.err_msg ? status.err_msg : "?");
        return -1;
    }

    PyConfig config;
    PyConfig_InitIsolatedConfig(&config);
    config.buffered_stdio = 0;         // logs must appear immediately
    config.write_bytecode = 0;         // the signed bundle is read-only
    config.install_signal_handlers = 0;

    if (set_config_string(&config, &config.home, python_home) != 0) {
        PyConfig_Clear(&config);
        return -2;
    }
    status = PyConfig_Read(&config);
    if (PyStatus_Exception(status)) {
        fprintf(stderr, "bv_py_start: config read failed: %s\n",
                status.err_msg ? status.err_msg : "?");
        PyConfig_Clear(&config);
        return -3;
    }
    status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) {
        fprintf(stderr, "bv_py_start: init failed: %s\n",
                status.err_msg ? status.err_msg : "?");
        return -4;
    }

    // app_packages goes through site.addsitedir (runs .pth machinery), the
    // staged app dir is a plain sys.path prepend — same as the testbed.
    int rc = 0;
    PyObject *site = NULL, *addsitedir = NULL, *added = NULL;
    PyObject *sys_path = NULL, *app_str = NULL;

    site = PyImport_ImportModule("site");
    if (site == NULL) { rc = -5; goto done; }
    addsitedir = PyObject_GetAttrString(site, "addsitedir");
    if (addsitedir == NULL) { rc = -5; goto done; }
    added = PyObject_CallFunction(addsitedir, "s", app_packages_dir);
    if (added == NULL) { rc = -5; goto done; }

    sys_path = PySys_GetObject("path");  // borrowed
    if (sys_path == NULL) { rc = -6; goto done; }
    app_str = PyUnicode_FromString(app_dir);
    if (app_str == NULL || PyList_Insert(sys_path, 0, app_str) != 0) {
        rc = -6; goto done;
    }

    g_board_export = PyImport_ImportModule("board_export");
    if (g_board_export == NULL) { rc = -7; goto done; }

done:
    if (rc != 0) {
        PyErr_Print();
    }
    Py_XDECREF(app_str);
    Py_XDECREF(added);
    Py_XDECREF(addsitedir);
    Py_XDECREF(site);
    // Release the GIL: GCD may run later queue items on other threads, so
    // every bv_py_call re-acquires it via PyGILState_Ensure.
    PyEval_SaveThread();
    return rc;
}

char *bv_py_call(const char *func, const char *arg1, const char *arg2) {
    if (g_board_export == NULL || func == NULL) {
        return NULL;
    }
    PyGILState_STATE gil = PyGILState_Ensure();
    char *out = NULL;
    PyObject *callable = NULL, *result = NULL;

    callable = PyObject_GetAttrString(g_board_export, func);
    if (callable == NULL) { goto done; }

    if (arg1 == NULL) {
        result = PyObject_CallFunction(callable, NULL);
    } else if (arg2 == NULL) {
        result = PyObject_CallFunction(callable, "s", arg1);
    } else {
        result = PyObject_CallFunction(callable, "ss", arg1, arg2);
    }
    if (result == NULL) { goto done; }

    const char *utf8 = PyUnicode_AsUTF8(result);
    if (utf8 != NULL) {
        out = strdup(utf8);
    }

done:
    if (out == NULL) {
        PyErr_Print();
    }
    Py_XDECREF(result);
    Py_XDECREF(callable);
    PyGILState_Release(gil);
    return out;
}

void bv_py_free(char *result) {
    free(result);
}
