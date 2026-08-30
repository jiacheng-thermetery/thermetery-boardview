// Minimal C bridge between the Swift shell and the embedded CPython
// interpreter. Swift cannot call the CPython C API's variadic/macro-heavy
// surface directly, so PythonRuntime.swift funnels everything through these
// three functions. All of them must be called from the single python-worker
// queue (see PythonRuntime.swift) — CPython state is GIL-serialized and
// board_export keeps a single-board module global.
#ifndef PYSHIM_H
#define PYSHIM_H

// Start the interpreter (isolated config, UTF-8 mode, no bytecode writing —
// the signed bundle is read-only) and import board_export.
//   python_home:      <bundle>/python            (stdlib installed by utils.sh)
//   app_dir:          <bundle>/app               (staged parser core)
//   app_packages_dir: <bundle>/app_packages      (numpy; added via site.addsitedir
//                                                 so .pth/.fwork resolution works)
// Returns 0 on success, a negative step code on failure (details on stderr).
int bv_py_start(const char *python_home,
                const char *app_dir,
                const char *app_packages_dir);

// Call board_export.<func>(args...) and return its result — always a JSON
// string per docs/android_contract.md §2. arg1/arg2 may be NULL to pass
// fewer arguments (NULL arg1 means a zero-arg call). Returns a malloc'd
// UTF-8 string the caller must release with bv_py_free, or NULL if the call
// itself failed (board_export never raises by contract, so NULL means the
// bridge/runtime is broken, not a parse error).
char *bv_py_call(const char *func, const char *arg1, const char *arg2);

void bv_py_free(char *result);

#endif
