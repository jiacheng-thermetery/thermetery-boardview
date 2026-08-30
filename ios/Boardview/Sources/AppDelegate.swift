import UIKit

@main
final class AppDelegate: UIResponder, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
    ) -> Bool {
        // Python starts at process launch on its worker queue, mirroring
        // BoardviewApplication.onCreate — later parse calls queue behind it.
        PythonRuntime.shared.start()
        return true
    }

    func application(
        _ application: UIApplication,
        configurationForConnecting connectingSceneSession: UISceneSession,
        options: UIScene.ConnectionOptions
    ) -> UISceneConfiguration {
        let config = UISceneConfiguration(name: "Default", sessionRole: connectingSceneSession.role)
        config.delegateClass = SceneDelegate.self
        return config
    }
}

final class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession,
               options connectionOptions: UIScene.ConnectionOptions) {
        guard let windowScene = scene as? UIWindowScene else { return }
        let window = UIWindow(windowScene: windowScene)
        window.rootViewController = ViewController()
        window.makeKeyAndVisible()
        self.window = window
        if !connectionOptions.urlContexts.isEmpty {
            handle(urlContexts: connectionOptions.urlContexts)
        }
    }

    // "Open with Boardview" from the Files app / share sheet — the iOS
    // equivalent of the Android ACTION_VIEW intent filters.
    func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
        handle(urlContexts: URLContexts)
    }

    private func handle(urlContexts: Set<UIOpenURLContext>) {
        guard let context = urlContexts.first,
              let viewController = window?.rootViewController as? ViewController else { return }
        viewController.handleExternalOpen(
            url: context.url,
            openInPlace: context.options.openInPlace)
    }
}
