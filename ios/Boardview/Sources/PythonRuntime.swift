import Foundation
import os

/// Owns the embedded CPython interpreter. Mirrors the Android
/// `PythonRuntime.kt`: one serial worker queue is the only place the
/// interpreter is ever touched (CPython is GIL-serialized and board_export
/// keeps a single-board module global), `start()` runs at app launch, and a
/// `board_export.ping()` is logged immediately after startup to prove the
/// native kernels loaded (contract §2).
final class PythonRuntime {
    static let shared = PythonRuntime()

    static let log = Logger(subsystem: "com.thermetery.boardview", category: "BoardviewPy")

    private let queue = DispatchQueue(label: "python-worker", qos: .userInitiated)
    private var started = false
    private var startFailed = false

    private init() {}

    /// Start the interpreter on the worker queue. Safe to call once from
    /// application(_:didFinishLaunching...); later `call`s are queued behind
    /// it on the same serial queue, so ordering is guaranteed by construction.
    func start() {
        queue.async { [self] in
            guard !started && !startFailed else { return }
            guard let resourceURL = Bundle.main.resourceURL else {
                startFailed = true
                Self.log.error("Bundle has no resource URL; Python not started")
                return
            }

            // Redirect every python-side write (config.json, private/ keys)
            // into the sandbox, and point the kernel loader at the bundled
            // dylibs — both consumed by src/runtime_paths.py.
            let appSupport = FileManager.default.urls(
                for: .applicationSupportDirectory, in: .userDomainMask)[0]
            let dataDir = appSupport.appendingPathComponent("Thermetery Boardviewer")
            try? FileManager.default.createDirectory(
                at: dataDir, withIntermediateDirectories: true)
            setenv("BOARDVIEWER_DATA_DIR", dataDir.path, 1)
            let frameworksPath = Bundle.main.privateFrameworksPath
                ?? resourceURL.appendingPathComponent("Frameworks").path
            setenv("BOARDVIEW_NATIVE_DIR", frameworksPath, 1)

            let home = resourceURL.appendingPathComponent("python").path
            let appDir = resourceURL.appendingPathComponent("app").path
            let appPackages = resourceURL.appendingPathComponent("app_packages").path

            let rc = bv_py_start(home, appDir, appPackages)
            if rc != 0 {
                startFailed = true
                Self.log.error("Python startup failed (step \(rc))")
                return
            }
            started = true
            if let ping = bv_py_call("ping", nil, nil) {
                Self.log.info("board_export.ping(): \(String(cString: ping), privacy: .public)")
                bv_py_free(ping)
            } else {
                Self.log.error("board_export.ping() returned nothing")
            }
        }
    }

    /// Call a board_export function with up to two string arguments and hand
    /// the raw JSON-string result to `completion` on the main queue.
    /// board_export never raises across the bridge (contract §2), so a nil
    /// bridge result is translated into the contract's failure shape.
    func call(_ function: String, _ args: [String?] = [],
              completion: @escaping (String) -> Void) {
        queue.async { [self] in
            var result: String?
            if started {
                let a1 = args.count > 0 ? args[0] : nil
                let a2 = args.count > 1 ? args[1] : nil
                if let raw = bv_py_call(function, a1, a2) {
                    result = String(cString: raw)
                    bv_py_free(raw)
                }
            }
            let json = result ?? #"{"ok":false,"error":"parse_error","reason":"Python runtime is not available","format":"?"}"#
            DispatchQueue.main.async { completion(json) }
        }
    }
}
