import UIKit
import WebKit
import UniformTypeIdentifiers
import os

/// The single fullscreen screen of the app: a WKWebView hosting the shared
/// JS renderer, plus the native file/folder pickers and the key dialog.
/// Behavior mirrors the Android `MainActivity.kt` + `BoardviewBridge.kt`
/// (docs/android_contract.md §4/§6); user-facing strings are kept verbatim.
final class ViewController: UIViewController {
    static let shellLog = Logger(subsystem: "com.thermetery.boardview", category: "Boardview")
    static let jsLog = Logger(subsystem: "com.thermetery.boardview", category: "BoardviewJS")

    private static let chromeColor = UIColor(red: 0x10 / 255.0, green: 0x14 / 255.0,
                                             blue: 0x17 / 255.0, alpha: 1)  // board_background

    private var webView: WKWebView!
    private var pageReady = false
    private var pendingJs: [String] = []
    private var boardOpen = false
    private var tracesInFlight = false
    private var pickingFolder = false

    private let maxKeyPrompts = 3           // mirrors viewer.py:_load_with_key_prompt
    private let maxAscMembers = 64
    private let maxAscMemberBytes: Int64 = 32 * 1024 * 1024

    private struct ParseAttempt {
        let file: URL           // copy under caches/boards/ (file, or dir for .asc sets)
        let displayName: String
        var key: String?
        var promptsUsed: Int
        var triedRemembered: Bool
        var rememberFormat: String?
    }

    // The window.Android shim: injected at document start (before viewer.js
    // runs — it captures `hasAndroid` once at module load) so the renderer's
    // dev-harness never activates. All five methods are fire-and-forget on
    // Android too; none of their return values is consumed. Console output
    // is piped to the shell, replacing WebChromeClient.onConsoleMessage.
    private static let bridgeShimJS = """
    (function () {
      function post(m) { try { window.webkit.messageHandlers.bridge.postMessage(m); } catch (e) {} }
      window.Android = {
        openFilePicker: function () { post({cmd: 'openFilePicker'}); },
        openFolderPicker: function () { post({cmd: 'openFolderPicker'}); },
        loadTraces: function () { post({cmd: 'loadTraces'}); },
        openKeyManager: function () { post({cmd: 'openKeyManager'}); },
        log: function (msg) { post({cmd: 'log', msg: String(msg)}); }
      };
      function pipe(level, args) {
        post({cmd: 'console', level: level,
              msg: Array.prototype.map.call(args, String).join(' ')});
      }
      var origLog = console.log, origWarn = console.warn, origError = console.error;
      console.log = function () { pipe('log', arguments); origLog.apply(console, arguments); };
      console.warn = function () { pipe('warn', arguments); origWarn.apply(console, arguments); };
      console.error = function () { pipe('error', arguments); origError.apply(console, arguments); };
      window.addEventListener('error', function (e) {
        post({cmd: 'console', level: 'error',
              msg: String(e.message) + ' (' + String(e.filename) + ':' + String(e.lineno) + ')'});
      });
    })();
    """

    // MARK: - Lifecycle

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = Self.chromeColor

        let config = WKWebViewConfiguration()
        config.userContentController.add(WeakScriptMessageHandler(self), name: "bridge")
        config.userContentController.addUserScript(WKUserScript(
            source: Self.bridgeShimJS, injectionTime: .atDocumentStart, forMainFrameOnly: true))

        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.backgroundColor = Self.chromeColor
        webView.scrollView.backgroundColor = Self.chromeColor
        webView.isOpaque = false
        // The renderer implements pan/pinch itself with Pointer Events on a
        // touch-action:none canvas; the scroll view's own gestures would
        // double-transform.
        webView.scrollView.isScrollEnabled = false
        webView.scrollView.bounces = false
        webView.scrollView.bouncesZoom = false
        webView.scrollView.pinchGestureRecognizer?.isEnabled = false
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        #if DEBUG
        if #available(iOS 16.4, *) {
            webView.isInspectable = true   // WebView.setWebContentsDebuggingEnabled parity
        }
        #endif

        webView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(webView)
        NSLayoutConstraint.activate([
            webView.topAnchor.constraint(equalTo: view.topAnchor),
            webView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            webView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            webView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
        ])

        guard let webDir = Bundle.main.resourceURL?.appendingPathComponent("web") else {
            Self.shellLog.error("Missing web assets directory in bundle")
            return
        }
        webView.loadFileURL(webDir.appendingPathComponent("viewer.html"),
                            allowingReadAccessTo: webDir)
    }

    override var preferredStatusBarStyle: UIStatusBarStyle { .lightContent }

    // MARK: - JS plumbing (mirrors MainActivity.runJs + pendingJs queue)

    private func runJs(_ script: String) {
        dispatchPrecondition(condition: .onQueue(.main))
        if pageReady {
            webView.evaluateJavaScript(script, completionHandler: nil)
        } else {
            pendingJs.append(script)
        }
    }

    /// JSON-encode arbitrary text into a JS string literal (JSONObject.quote
    /// parity). Board/trace payloads are NOT quoted — valid JSON is injected
    /// directly as an object literal to avoid a double parse.
    private func jsQuote(_ text: String) -> String {
        let data = (try? JSONSerialization.data(withJSONObject: [text])) ?? Data("[\"\"]".utf8)
        let array = String(data: data, encoding: .utf8) ?? "[\"\"]"
        return String(array.dropFirst().dropLast())
    }

    private func postStatus(_ text: String) {
        runJs("window.bv && bv.onStatus(\(jsQuote(text)));")
    }

    private func postError(_ text: String) {
        Self.shellLog.warning("Error -> renderer: \(text, privacy: .public)")
        runJs("window.bv && bv.onError(\(jsQuote(text)));")
    }

    // MARK: - Incoming files

    /// Entry point for "open with Boardview" (SceneDelegate). With
    /// LSSupportsOpeningDocumentsInPlace=false iOS hands us an app-local
    /// inbox copy; in-place URLs (e.g. drag & drop) need the security scope.
    func handleExternalOpen(url: URL, openInPlace: Bool) {
        // Clean up only transient system-made copies (Documents/Inbox, tmp) —
        // never a file the user owns, e.g. our own Documents via file sharing.
        let path = url.standardizedFileURL.path
        let transient = path.contains("/Documents/Inbox/") || path.contains("/tmp/")
        handleIncomingFile(url: url, securityScoped: openInPlace,
                           deleteOriginal: !openInPlace && transient)
    }

    private func sanitizeFileName(_ name: String) -> String {
        let sanitized = name.replacingOccurrences(
            of: "[\\\\/:*?\"<>|]", with: "_", options: .regularExpression)
        return sanitized.isEmpty ? "board" : sanitized
    }

    private func boardsDir() -> URL {
        FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("boards", isDirectory: true)
    }

    private func rememberedKeyFormat(forExtension ext: String) -> String? {
        switch ext {
        case "fz": return "fz"
        case "pcb": return "xzzpcb"
        default: return nil
        }
    }

    private func handleIncomingFile(url: URL, securityScoped: Bool, deleteOriginal: Bool) {
        let displayName = url.lastPathComponent.isEmpty ? "board" : url.lastPathComponent
        let ext = (displayName as NSString).pathExtension.lowercased()
        if ext == "asc" {
            showAscFolderHint(displayName: displayName)
            return
        }
        postStatus("Parsing \(displayName)…")

        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let scoped = securityScoped && url.startAccessingSecurityScopedResource()
            defer { if scoped { url.stopAccessingSecurityScopedResource() } }
            do {
                let dir = boardsDir()
                try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
                // Keep the copy for the whole session — the lazy topology
                // build re-reads the path later; never delete after parse.
                let dest = dir.appendingPathComponent(sanitizeFileName(displayName))
                try? FileManager.default.removeItem(at: dest)
                try FileManager.default.copyItem(at: url, to: dest)
                if deleteOriginal {
                    try? FileManager.default.removeItem(at: url)
                }
                DispatchQueue.main.async { [self] in
                    let format = rememberedKeyFormat(forExtension: ext)
                    let key = format.flatMap { KeyVault.load($0) }
                    parseBoard(ParseAttempt(
                        file: dest, displayName: displayName, key: key,
                        promptsUsed: 0, triedRemembered: format != nil,
                        rememberFormat: nil))
                }
            } catch {
                DispatchQueue.main.async { [self] in
                    postError("Could not read \(displayName): \(error.localizedDescription)")
                    postStatus("")
                }
            }
        }
    }

    private func handleIncomingTree(url: URL) {
        let dirName = sanitizeFileName(url.lastPathComponent.isEmpty ? "board" : url.lastPathComponent)
        postStatus("Parsing \(dirName)…")
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let scoped = url.startAccessingSecurityScopedResource()
            defer { if scoped { url.stopAccessingSecurityScopedResource() } }
            let fm = FileManager.default
            var isDir: ObjCBool = false
            guard fm.fileExists(atPath: url.path, isDirectory: &isDir), isDir.boolValue else {
                DispatchQueue.main.async { [self] in
                    postError("Could not open the picked folder.")
                    postStatus("")
                }
                return
            }
            do {
                // Fresh copy every pick — never mix stale and new members.
                let dest = boardsDir().appendingPathComponent(dirName, isDirectory: true)
                try? fm.removeItem(at: dest)
                try fm.createDirectory(at: dest, withIntermediateDirectories: true)
                var copied = 0
                let children = try fm.contentsOfDirectory(
                    at: url, includingPropertiesForKeys: [.isRegularFileKey, .fileSizeKey])
                for child in children {
                    if copied >= maxAscMembers { break }
                    let values = try? child.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])
                    guard values?.isRegularFile == true else { continue }
                    guard child.lastPathComponent.lowercased().hasSuffix(".asc") else { continue }
                    if let size = values?.fileSize, Int64(size) > maxAscMemberBytes {
                        Self.shellLog.warning("Skipping oversized .asc member \(child.lastPathComponent, privacy: .public)")
                        continue
                    }
                    try fm.copyItem(at: child,
                                    to: dest.appendingPathComponent(sanitizeFileName(child.lastPathComponent)))
                    copied += 1
                }
                DispatchQueue.main.async { [self] in
                    if copied == 0 {
                        postError("\(dirName) contains no .asc files — pick the folder holding the parts/pins/… .asc set.")
                        postStatus("")
                        return
                    }
                    // ASC sets are plaintext — no key slot applies.
                    parseBoard(ParseAttempt(
                        file: dest, displayName: dirName, key: nil,
                        promptsUsed: 0, triedRemembered: true, rememberFormat: nil))
                }
            } catch {
                DispatchQueue.main.async { [self] in
                    postError("Could not copy \(dirName): \(error.localizedDescription)")
                    postStatus("")
                }
            }
        }
    }

    private func showAscFolderHint(displayName: String) {
        let alert = UIAlertController(
            title: "Pick the board folder",
            message: "\(displayName) is one member of an eM-Test Expert ICT set — the board is the whole folder (parts/pins/nails/format/nets .asc together). Pick the folder that contains it.",
            preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "Pick folder", style: .default) { [weak self] _ in
            self?.launchFolderPicker()
        })
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        present(alert, animated: true)
    }

    // MARK: - Parse flow (mirrors MainActivity.parseBoard/handleParseResult)

    private func parseBoard(_ attempt: ParseAttempt) {
        postStatus("Parsing \(attempt.displayName)…")
        PythonRuntime.shared.call("open_board", [attempt.file.path, attempt.key]) { [weak self] raw in
            self?.handleParseResult(attempt, raw: raw)
        }
    }

    private func handleParseResult(_ attempt: ParseAttempt, raw: String) {
        guard let data = raw.data(using: .utf8),
              let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            postError("Could not load \(attempt.displayName): malformed parser reply")
            postStatus("")
            return
        }
        if json["ok"] as? Bool == true {
            if let format = attempt.rememberFormat, let key = attempt.key {
                KeyVault.save(format, key: key)
            }
            boardOpen = true
            tracesInFlight = false
            runJs("window.bv && bv.onBoard(\(raw));")
            postStatus("")
            return
        }

        let error = json["error"] as? String ?? ""
        let reason = json["reason"] as? String ?? ""
        guard error == "key_required" else {
            let message = !reason.isEmpty ? reason : (!error.isEmpty ? error : "unknown error")
            postError("Could not load \(attempt.displayName): \(message)")
            postStatus("")
            return
        }

        var format = json["format"] as? String ?? "unknown"
        if format.isEmpty { format = "unknown" }

        // A remembered key is auto-supplied once, silently — it does not
        // count as a prompt.
        if !attempt.triedRemembered, let saved = KeyVault.load(format) {
            var retry = attempt
            retry.key = saved
            retry.triedRemembered = true
            parseBoard(retry)
            return
        }
        if attempt.promptsUsed >= maxKeyPrompts {
            postError("Giving up after several attempts — \(attempt.displayName) cannot open without a valid key.")
            postStatus("")
            return
        }

        let failurePrefix: String?
        if attempt.key == nil {
            failurePrefix = nil
        } else if attempt.promptsUsed == 0 {
            failurePrefix = "The remembered key did not work."
        } else {
            failurePrefix = "That key did not work."
        }
        showKeyDialog(displayName: attempt.displayName, format: format,
                      failurePrefix: failurePrefix) { [weak self] entered, remember in
            var retry = attempt
            retry.key = entered
            retry.promptsUsed += 1
            retry.triedRemembered = true
            retry.rememberFormat = remember ? format : nil
            self?.parseBoard(retry)
        } onGiveUp: { [weak self] in
            self?.postError("Could not open \(attempt.displayName) — a valid key is required.")
            self?.postStatus("")
        }
    }

    private func showKeyDialog(displayName: String, format: String, failurePrefix: String?,
                               onSubmit: @escaping (String, Bool) -> Void,
                               onGiveUp: @escaping () -> Void) {
        let title: String
        let ask: String
        switch format {
        case "fz":
            title = "ASUS FZ key required"
            ask = "\(displayName) is an RC6-encrypted ASUS .fz file and no usable key was available.\n\nPaste the FZKey (44 × 32-bit hex words):"
        case "xzzpcb", "xzz":
            title = "XZZ key required"
            ask = "\(displayName) is a DES-encrypted XZZ .pcb file and no valid key was available.\n\nPaste the XZZ key (16 hex digits):"
        default:
            title = "Decryption key required"
            ask = "\(displayName) needs a decryption key.\n\nPaste the key:"
        }
        let message = failurePrefix.map { "\($0)\n\n\(ask)" } ?? ask

        // Android uses a multi-line field + a "Remember on this device"
        // checkbox; UIAlertController has neither, so remembering becomes a
        // second action. Keys pasted into the single-line field keep their
        // token separators (the parsers tokenize on any whitespace).
        let alert = UIAlertController(title: title, message: message, preferredStyle: .alert)
        alert.addTextField { field in
            field.placeholder = "Key"
            field.autocorrectionType = .no
            field.autocapitalizationType = .none
            field.spellCheckingType = .no
        }
        let submit: (Bool) -> Void = { [weak alert] remember in
            let entered = (alert?.textFields?.first?.text ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if entered.isEmpty {
                onGiveUp()   // empty == cancel (viewer.py)
            } else {
                onSubmit(entered, remember)
            }
        }
        alert.addAction(UIAlertAction(title: "Open", style: .default) { _ in submit(false) })
        alert.addAction(UIAlertAction(title: "Open and Remember", style: .default) { _ in submit(true) })
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in onGiveUp() })
        present(alert, animated: true)
    }

    // MARK: - Traces

    private func requestTraces() {
        if !boardOpen {
            postError("Open a board first.")
            return
        }
        if tracesInFlight { return }
        tracesInFlight = true
        postStatus("Building topology…")
        PythonRuntime.shared.call("load_traces") { [weak self] raw in
            guard let self else { return }
            self.tracesInFlight = false
            defer { self.postStatus("") }
            guard let data = raw.data(using: .utf8),
                  let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
                self.postError("Trace build failed: malformed exporter result")
                return
            }
            if json["ok"] as? Bool == true {
                self.runJs("window.bv && bv.onTraces(\(raw));")
            } else {
                let reason = json["reason"] as? String
                    ?? (json["error"] as? String ?? "unknown error")
                self.postError("Trace build failed: \(reason)")
            }
        }
    }

    // MARK: - Pickers / secondary screens

    private func launchFilePicker() {
        // Android passes MIME */* and lets the parser decide; same here.
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.item], asCopy: true)
        picker.delegate = self
        picker.allowsMultipleSelection = false
        pickingFolder = false
        present(picker, animated: true)
    }

    private func launchFolderPicker() {
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.folder])
        picker.delegate = self
        picker.allowsMultipleSelection = false
        pickingFolder = true
        present(picker, animated: true)
    }

    private func launchKeyManager() {
        let manager = KeyManagerViewController()
        manager.modalPresentationStyle = .formSheet
        present(manager, animated: true)
    }
}

// MARK: - WKScriptMessageHandler (the window.Android side)

extension ViewController: WKScriptMessageHandler {
    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.name == "bridge",
              let body = message.body as? [String: Any],
              let cmd = body["cmd"] as? String else { return }
        switch cmd {
        case "openFilePicker":
            launchFilePicker()
        case "openFolderPicker":
            launchFolderPicker()
        case "loadTraces":
            requestTraces()
        case "openKeyManager":
            launchKeyManager()
        case "log":
            Self.jsLog.info("\(body["msg"] as? String ?? "", privacy: .public)")
        case "console":
            let text = "\(body["msg"] as? String ?? "")"
            if (body["level"] as? String) == "error" {
                Self.jsLog.error("\(text, privacy: .public)")
            } else {
                Self.jsLog.info("\(text, privacy: .public)")
            }
        default:
            break
        }
    }
}

// MARK: - WKNavigationDelegate (pageReady gate)

extension ViewController: WKNavigationDelegate {
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // WKWebView re-creates scroll gestures on navigation; keep them off.
        webView.scrollView.pinchGestureRecognizer?.isEnabled = false
        if !pageReady {
            pageReady = true
            let queued = pendingJs
            pendingJs.removeAll()
            for script in queued {
                webView.evaluateJavaScript(script, completionHandler: nil)
            }
        }
    }
}

// MARK: - UIDocumentPickerDelegate

extension ViewController: UIDocumentPickerDelegate {
    func documentPicker(_ controller: UIDocumentPickerViewController,
                        didPickDocumentsAt urls: [URL]) {
        guard let url = urls.first else { return }
        if pickingFolder {
            handleIncomingTree(url: url)
        } else {
            // asCopy:true hands us a temporary app-local copy — no security
            // scope needed; remove it after staging into caches/boards.
            handleIncomingFile(url: url, securityScoped: false, deleteOriginal: true)
        }
    }
}

// Breaks the WKUserContentController → handler retain cycle.
private final class WeakScriptMessageHandler: NSObject, WKScriptMessageHandler {
    private weak var delegate: WKScriptMessageHandler?
    init(_ delegate: WKScriptMessageHandler) {
        self.delegate = delegate
    }
    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        delegate?.userContentController(userContentController, didReceive: message)
    }
}
