import XCTest

/// The only place a GrowinUITests case may create an `XCUIApplication`.
///
/// The app launched by a UI test runs in its own process, so it cannot see the XCTest
/// environment. Without this key `GrowinApp.init` would run the launch migrations
/// against the real Keychain. `GrowinTests` has a source scan that keeps this file the
/// only creator of `XCUIApplication` and keeps the key and value below in step with
/// `LaunchMigrations`. This target cannot import the app, so the literals are repeated.
enum GrowinLaunch {
    @MainActor
    static func makeApp() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["GROWIN_SKIP_LAUNCH_MIGRATIONS"] = "1"
        return app
    }
}
