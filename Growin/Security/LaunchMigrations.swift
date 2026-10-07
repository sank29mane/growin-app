import Foundation

/// Every Keychain and UserDefaults migration that runs when the app launches,
/// behind one guard.
///
/// Why the guard exists: `GrowinTests` is app-hosted (TEST_HOST is Growin.app), so
/// `xcodebuild test` launches the real app and runs `GrowinApp.init` against the
/// developer's real Keychain and real `san.Growin` defaults. Tests must never do
/// that, so a test-hosted launch skips every migration here.
///
/// How a test host is detected: any one of these signals is enough, because a
/// false positive only skips a migration for one launch, while a false negative
/// touches the operator's real Keychain.
/// - `XCTestConfigurationFilePath`, `XCTestBundlePath` or `XCTestSessionIdentifier`
///   in the environment. The test runner sets them in the host process.
/// - The `XCTestCase` class is loaded. The runner injects XCTest into the host
///   even when an environment variable is missing, and a release build of the
///   app never links it.
enum LaunchMigrations {
    static let testHostEnvironmentKeys = [
        "XCTestConfigurationFilePath",
        "XCTestBundlePath",
        "XCTestSessionIdentifier",
    ]

    static func isTestHosted(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        xctestCaseClassLoaded: Bool = NSClassFromString("XCTestCase") != nil
    ) -> Bool {
        xctestCaseClassLoaded || testHostEnvironmentKeys.contains { environment[$0] != nil }
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

    /// The only launch entry point for migrations. `GrowinApp.init` calls this and nothing else.
    static func runAtLaunch(
        testHosted: Bool = isTestHosted(),
        steps: Steps = .production()
    ) {
        guard !testHosted else { return }
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
