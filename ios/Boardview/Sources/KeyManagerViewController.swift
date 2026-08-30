import UIKit
import UniformTypeIdentifiers

/// Per-format decryption-key screen, ported from `KeyManagerActivity.kt`.
/// Strings, colors, and the validate/save/clear flows are kept verbatim;
/// validation goes through `board_export.validate_key` exactly like Android
/// (shared with the desktop key manager via src/key_store.py).
final class KeyManagerViewController: UIViewController {
    // The Android screen hardcodes its own dark palette (distinct from the
    // viewer chrome color — do not unify).
    private enum Palette {
        static let background = UIColor(hexRGB: 0x0d1024)
        static let card = UIColor(hexRGB: 0x161b34)
        static let title = UIColor(hexRGB: 0xcdd6ff)
        static let subtitle = UIColor(hexRGB: 0x8b93b8)
        static let good = UIColor(hexRGB: 0x5bd97a)
        static let warn = UIColor(hexRGB: 0xffc14d)
        static let neutral = UIColor(hexRGB: 0x8b93b8)
        static let bad = UIColor(hexRGB: 0xff7a6b)
        static let accent = UIColor(hexRGB: 0x19C37D)  // pcb_accent
    }

    private final class Slot {
        let format: String
        let title: String
        let hint: String
        let input = UITextView()
        let status = UILabel()
        init(format: String, title: String, hint: String) {
            self.format = format
            self.title = title
            self.hint = hint
        }
    }

    private lazy var slots: [Slot] = [
        Slot(format: "fz",
             title: "ASUS  ·  .fz  (RC6)",
             hint: "Paste the FZKey — 44 × 32-bit hex words. Fully verified here."),
        Slot(format: "xzzpcb",
             title: "XZZ  ·  .pcb  (DES)",
             hint: "Paste the XZZ key (hex). Confirmed only by opening a board."),
    ]

    private var slotAwaitingFile: Slot?

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = Palette.background

        let scroll = UIScrollView()
        scroll.alwaysBounceVertical = true
        let stack = UIStackView()
        stack.axis = .vertical
        stack.spacing = 16
        stack.isLayoutMarginsRelativeArrangement = true
        stack.layoutMargins = UIEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)

        // Header row: title + Done.
        let headerRow = UIStackView()
        headerRow.axis = .horizontal
        headerRow.alignment = .center
        let titleLabel = UILabel()
        titleLabel.text = "Decryption keys"
        titleLabel.font = .systemFont(ofSize: 22, weight: .semibold)
        titleLabel.textColor = .white
        let doneButton = UIButton(type: .system)
        doneButton.setTitle("Done", for: .normal)
        doneButton.tintColor = Palette.accent
        doneButton.addTarget(self, action: #selector(closeTapped), for: .touchUpInside)
        headerRow.addArrangedSubview(titleLabel)
        headerRow.addArrangedSubview(UIView())
        headerRow.addArrangedSubview(doneButton)
        stack.addArrangedSubview(headerRow)

        let subtitle = UILabel()
        subtitle.text = "Keys are stored only on this device (excluded from backups) and supplied automatically when you open an encrypted board."
        subtitle.font = .systemFont(ofSize: 13)
        subtitle.textColor = Palette.subtitle
        subtitle.numberOfLines = 0
        stack.addArrangedSubview(subtitle)

        for slot in slots {
            stack.addArrangedSubview(makeCard(for: slot))
        }

        let licenses = UIButton(type: .system)
        licenses.setTitle("Third-party licenses", for: .normal)
        licenses.titleLabel?.font = .systemFont(ofSize: 13)
        licenses.tintColor = Palette.subtitle
        licenses.addTarget(self, action: #selector(licensesTapped), for: .touchUpInside)
        stack.addArrangedSubview(licenses)

        scroll.translatesAutoresizingMaskIntoConstraints = false
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(scroll)
        scroll.addSubview(stack)
        NSLayoutConstraint.activate([
            scroll.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            scroll.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            scroll.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            stack.topAnchor.constraint(equalTo: scroll.contentLayoutGuide.topAnchor),
            stack.bottomAnchor.constraint(equalTo: scroll.contentLayoutGuide.bottomAnchor),
            stack.leadingAnchor.constraint(equalTo: scroll.contentLayoutGuide.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: scroll.contentLayoutGuide.trailingAnchor),
            stack.widthAnchor.constraint(equalTo: scroll.frameLayoutGuide.widthAnchor),
        ])

        refreshStatuses()
    }

    private func makeCard(for slot: Slot) -> UIView {
        let card = UIStackView()
        card.axis = .vertical
        card.spacing = 8
        card.isLayoutMarginsRelativeArrangement = true
        card.layoutMargins = UIEdgeInsets(top: 14, left: 14, bottom: 14, right: 14)
        card.backgroundColor = Palette.card
        card.layer.cornerRadius = 10

        let title = UILabel()
        title.text = slot.title
        title.font = .systemFont(ofSize: 15, weight: .semibold)
        title.textColor = .white
        card.addArrangedSubview(title)

        let hint = UILabel()
        hint.text = slot.hint
        hint.font = .systemFont(ofSize: 12)
        hint.textColor = Palette.subtitle
        hint.numberOfLines = 0
        card.addArrangedSubview(hint)

        slot.input.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
        slot.input.textColor = Palette.title
        slot.input.backgroundColor = Palette.background
        slot.input.layer.cornerRadius = 6
        slot.input.autocorrectionType = .no
        slot.input.autocapitalizationType = .none
        slot.input.spellCheckingType = .no
        slot.input.heightAnchor.constraint(greaterThanOrEqualToConstant: 64).isActive = true
        card.addArrangedSubview(slot.input)

        slot.status.font = .systemFont(ofSize: 12)
        slot.status.textColor = Palette.neutral
        slot.status.numberOfLines = 0
        card.addArrangedSubview(slot.status)

        let buttons = UIStackView()
        buttons.axis = .horizontal
        buttons.distribution = .fillEqually
        buttons.spacing = 8
        let actions: [(String, Selector)] = [
            ("Load file", #selector(loadFileTapped(_:))),
            ("Validate", #selector(validateTapped(_:))),
            ("Save", #selector(saveTapped(_:))),
            ("Clear", #selector(clearTapped(_:))),
        ]
        for (index, (label, selector)) in actions.enumerated() {
            let button = UIButton(type: .system)
            button.setTitle(label, for: .normal)
            button.titleLabel?.font = .systemFont(ofSize: 14, weight: .medium)
            button.tintColor = Palette.accent
            button.tag = tagFor(slot: slot, action: index)
            button.addTarget(self, action: selector, for: .touchUpInside)
            buttons.addArrangedSubview(button)
        }
        card.addArrangedSubview(buttons)
        return card
    }

    private func tagFor(slot: Slot, action: Int) -> Int {
        (slots.firstIndex { $0 === slot } ?? 0) * 10 + action
    }

    private func slotFor(tag: Int) -> Slot {
        slots[min(tag / 10, slots.count - 1)]
    }

    // MARK: - Actions

    @objc private func closeTapped() { dismiss(animated: true) }

    @objc private func licensesTapped() {
        present(LicensesViewController(), animated: true)
    }

    @objc private func loadFileTapped(_ sender: UIButton) {
        slotAwaitingFile = slotFor(tag: sender.tag)
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.item], asCopy: true)
        picker.delegate = self
        present(picker, animated: true)
    }

    @objc private func validateTapped(_ sender: UIButton) {
        validate(slotFor(tag: sender.tag))
    }

    private func validate(_ slot: Slot, completion: ((String) -> Void)? = nil) {
        let text = slot.input.text.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty {
            setStatus(slot, "Not set.", status: "neutral")
            completion?("empty")
            return
        }
        setStatus(slot, "Checking…", status: "neutral")
        PythonRuntime.shared.call("validate_key", [slot.format, text]) { [weak self] raw in
            guard let self else { return }
            var status = "error"
            var message = ""
            if let data = raw.data(using: .utf8),
               let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
                status = json["status"] as? String ?? "error"
                message = json["message"] as? String ?? ""
            }
            self.setStatus(slot, message.isEmpty ? status : message, status: status)
            completion?(status)
        }
    }

    @objc private func saveTapped(_ sender: UIButton) {
        let slot = slotFor(tag: sender.tag)
        let text = slot.input.text.trimmingCharacters(in: .whitespacesAndNewlines)
        validate(slot) { [weak self] status in
            guard let self else { return }
            guard status == "valid" || status == "unverified" else {
                self.toast("Not saved — the key is not well-formed.")
                return
            }
            KeyVault.save(slot.format, key: text)
            self.toast("Key saved for \(slot.format).")
            self.refreshStatuses()
        }
    }

    @objc private func clearTapped(_ sender: UIButton) {
        let slot = slotFor(tag: sender.tag)
        let existed = KeyVault.clear(slot.format)
        slot.input.text = ""
        refreshStatuses()
        toast(existed ? "Key cleared for \(slot.format)." : "No saved key to clear.")
    }

    private func refreshStatuses() {
        for slot in slots {
            if let saved = KeyVault.load(slot.format) {
                if slot.input.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    slot.input.text = saved
                }
                setStatus(slot, "Saved on this device.", status: "valid")
            } else if slot.input.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                setStatus(slot, "Not set.", status: "neutral")
            }
        }
    }

    private func setStatus(_ slot: Slot, _ text: String, status: String) {
        slot.status.text = text
        switch status {
        case "valid": slot.status.textColor = Palette.good
        case "unverified": slot.status.textColor = Palette.warn
        case "neutral": slot.status.textColor = Palette.neutral
        default: slot.status.textColor = Palette.bad
        }
    }

    /// Android uses Toast; a transient overlay label is the iOS stand-in.
    private func toast(_ text: String) {
        let label = UILabel()
        label.text = text
        label.font = .systemFont(ofSize: 13)
        label.textColor = .white
        label.backgroundColor = UIColor.black.withAlphaComponent(0.8)
        label.textAlignment = .center
        label.numberOfLines = 0
        label.layer.cornerRadius = 8
        label.clipsToBounds = true
        label.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(label)
        NSLayoutConstraint.activate([
            label.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            label.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -24),
            label.widthAnchor.constraint(lessThanOrEqualTo: view.widthAnchor, constant: -48),
            label.heightAnchor.constraint(greaterThanOrEqualToConstant: 36),
        ])
        UIView.animate(withDuration: 0.3, delay: 1.8, options: []) {
            label.alpha = 0
        } completion: { _ in
            label.removeFromSuperview()
        }
    }
}

extension KeyManagerViewController: UIDocumentPickerDelegate {
    func documentPicker(_ controller: UIDocumentPickerViewController,
                        didPickDocumentsAt urls: [URL]) {
        guard let url = urls.first, let slot = slotAwaitingFile else { return }
        slotAwaitingFile = nil
        DispatchQueue.global(qos: .userInitiated).async {
            let text = (try? String(contentsOf: url, encoding: .utf8))?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            try? FileManager.default.removeItem(at: url)
            DispatchQueue.main.async { [weak self] in
                slot.input.text = text
                self?.validate(slot)
            }
        }
    }
}

extension UIColor {
    convenience init(hexRGB hex: Int) {
        self.init(red: CGFloat((hex >> 16) & 0xFF) / 255.0,
                  green: CGFloat((hex >> 8) & 0xFF) / 255.0,
                  blue: CGFloat(hex & 0xFF) / 255.0,
                  alpha: 1)
    }
}
