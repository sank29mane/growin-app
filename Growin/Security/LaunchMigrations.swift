import Foundation

/// Every Keychain and UserDefaults migration that runs when the app launches,
/// behind one guard.
///
/// Why the guard exists: `GrowinTests` is app-hosted (TEST_HOST is Growin.app), so
/// `xcodebuild test` launches the real app and runs `GrowinApp.init` against the
/// developer's real Keychain and real `san.Growin` defaults. Tests must never do
/// that. The same is true of Xcode previews and of UI tests, which launch the app
/// in a separate process. So three kinds of launch skip every migration here.
///
/// A skip signal is any one of these, because a false positive only skips a
/// migration for one launch, while a false negative touches the operator's real Keychain:
/// - A test host: `XCTestConfigurationFilePath`, `XCTestBundlePath` or
///   `XCTestSessionIdentifier` in the environment, or the `XCTestCase` class loaded.
///   A release build of the app never links XCTest.
/// - An Xcode preview: `XCODE_RUNNING_FOR_PREVIEWS` is "1".
/// - An explicit request: `GROWIN_SKIP_LAUNCH_MIGRATIONS` is "1". UI tests launch the
///   app in its own process, where none of the XCTest signals are present, so every
///   `XCUIApplication` in GrowinUITests sets this (see `GrowinUITests/LaunchSupport.swift`).
enum LaunchMigrations {
    static let testHostEnvironmentKeys = [
        "XCTestConfigurationFilePath",
        "XCTestBundlePath",
        "XCTestSessionIdentifier",
    ]
    static let previewEnvironmentKey = "XCODE_RUNNING_FOR_PREVIEWS"
    static let skipEnvironmentKey = "GROWIN_SKIP_LAUNCH_MIGRATIONS"

    static func shouldSkip(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        xctestCaseClassLoaded: Bool = NSClassFromString("XCTestCase") != nil
    ) -> Bool {
        xctestCaseClassLoaded
            || testHostEnvironmentKeys.contains { environment[$0] != nil }
            || environment[previewEnvironmentKey] == "1"
            || environment[skipEnvironmentKey] == "1"
    }

    /// The migration steps, injectable so a test can prove the guard without a Keychain.
    struct Steps {
        var legacyUserDefaults: () -> Void
        var flatItemsToScoped: () -> Void
        var legacyT212Cleanup: () -> Void

        static func production() -> Steps {
            Steps(
                legacyUserDefaults: { _ = KeychainStore.shared.migrateLegacyUserDefaults() },
                flatItemsToScoped: { _ = KeychainStore.shared.migrateFlatItemsToScoped() },
                legacyT212Cleanup: {
                    LegacyT212KeychainCleanup.runOnce(service: KeychainStore.productionService)
                })
        }
    }

    /// The launch entry point for the app. `GrowinApp.init` calls this and nothing else.
    /// It always uses the real environment and the real steps. Tests cannot reach it
    /// meaningfully: a test-hosted process skips, and a scan keeps tests from calling it.
    static func runAtLaunch() {
        runAtLaunch(skip: shouldSkip(), steps: .production())
    }

    /// The injectable form. It has no defaults on purpose: every caller states both
    /// the skip decision and the steps, so a test cannot get the production steps by omission.
    static func runAtLaunch(skip: Bool, steps: Steps) {
        guard !skip else { return }
        // One-time removal of the old Trading 212 key and secret. Never reads a value.
        steps.legacyT212Cleanup()
        // Move legacy secrets out of UserDefaults before any view model reads them.
        // Failed items remain in UserDefaults so migration is lossless and retryable.
        steps.legacyUserDefaults()
        // Then move flat Keychain items to their scoped accounts (copy, verify, delete).
        // Failed items stay in place and retry on the next launch.
        steps.flatItemsToScoped()
    }
}
