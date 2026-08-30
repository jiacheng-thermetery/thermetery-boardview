import UIKit

/// Read-only third-party license text, ported from `LicensesActivity.kt`:
/// same dark styling, monospaced selectable text, loaded off the main
/// thread from the shared `web/third_party_licenses.txt` asset.
final class LicensesViewController: UIViewController {
    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(hexRGB: 0x0d1024)

        let titleLabel = UILabel()
        titleLabel.text = "Third-party licenses"
        titleLabel.font = .systemFont(ofSize: 22, weight: .semibold)
        titleLabel.textColor = .white

        let doneButton = UIButton(type: .system)
        doneButton.setTitle("Done", for: .normal)
        doneButton.tintColor = UIColor(hexRGB: 0x19C37D)
        doneButton.addTarget(self, action: #selector(closeTapped), for: .touchUpInside)

        let header = UIStackView(arrangedSubviews: [titleLabel, UIView(), doneButton])
        header.axis = .horizontal
        header.alignment = .center

        let textView = UITextView()
        textView.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        textView.textColor = UIColor(hexRGB: 0xcdd6ff)
        textView.backgroundColor = .clear
        textView.isEditable = false
        textView.isSelectable = true
        textView.alwaysBounceVertical = true
        textView.text = "Loading…"

        let stack = UIStackView(arrangedSubviews: [header, textView])
        stack.axis = .vertical
        stack.spacing = 12
        stack.isLayoutMarginsRelativeArrangement = true
        stack.layoutMargins = UIEdgeInsets(top: 20, left: 20, bottom: 8, right: 20)
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor),
            stack.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            stack.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: view.trailingAnchor),
        ])

        DispatchQueue.global(qos: .userInitiated).async {
            let url = Bundle.main.resourceURL?
                .appendingPathComponent("web/third_party_licenses.txt")
            let text: String
            do {
                guard let url else { throw CocoaError(.fileNoSuchFile) }
                text = try String(contentsOf: url, encoding: .utf8)
            } catch {
                text = "Could not load the license file: \(error.localizedDescription)"
            }
            DispatchQueue.main.async {
                textView.text = text
            }
        }
    }

    @objc private func closeTapped() { dismiss(animated: true) }
}
