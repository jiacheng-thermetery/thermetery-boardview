import Foundation

/// Remembered decryption keys, mirroring the Android `KeyVault.kt`: one
/// plain-text file per format under an app-private directory, never entering
/// cloud backup (Android sets `allowBackup="false"`; here the directory is
/// marked excluded from backup). Keys are supplied back into
/// `board_export.open_board(path, key:)` explicitly — the Python-side
/// `key_store` persistence is not used, same as Android.
enum KeyVault {
    private static func keysDir() -> URL {
        let base = FileManager.default.urls(
            for: .applicationSupportDirectory, in: .userDomainMask)[0]
        return base.appendingPathComponent("keys", isDirectory: true)
    }

    /// Android sanitizes with `[^A-Za-z0-9._-]` → `_`.
    private static func fileURL(for format: String) -> URL {
        let sanitized = format.replacingOccurrences(
            of: "[^A-Za-z0-9._-]", with: "_", options: .regularExpression)
        return keysDir().appendingPathComponent("\(sanitized).txt")
    }

    static func save(_ format: String, key: String) {
        let dir = keysDir()
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        var dirURL = dir
        try? dirURL.setResourceValues(values)
        try? (key + "\n").write(to: fileURL(for: format), atomically: true, encoding: .utf8)
    }

    static func load(_ format: String) -> String? {
        guard let text = try? String(contentsOf: fileURL(for: format), encoding: .utf8) else {
            return nil
        }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    /// Returns whether a saved key existed.
    @discardableResult
    static func clear(_ format: String) -> Bool {
        let url = fileURL(for: format)
        let existed = FileManager.default.fileExists(atPath: url.path)
        try? FileManager.default.removeItem(at: url)
        return existed
    }
}
