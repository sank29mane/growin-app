import XCTest

/// The only place a GrowinUITests case may create an `XCUIApplication`.
///
/// The app launched by a UI test runs in its own process, so it cannot see the XCTest
/// environment. Without this key `GrowinApp.init` would run the launch migrations
/// against the real Keychain. `GrowinTests` has a source scan that keeps this file the
/// only creator of `XCUIApplication` and keeps the key and value below in step with
/// `LaunchMigrations`. This target cannot import the app, so the literals are repeated.
///
/// The second key, `GROWIN_UI_TEST_HOST`, is what `TestHostDetection` reads, so the app
/// process counts as a test host and the Keychain guard refuses the production service in it.
/// The suppression key stays a launch-migration switch only.
enum GrowinLaunch {
    @MainActor
    static func makeApp() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["GROWIN_SKIP_LAUNCH_MIGRATIONS"] = "1"
        app.launchEnvironment["GROWIN_UI_TEST_HOST"] = "1"
        return app
    }
}
