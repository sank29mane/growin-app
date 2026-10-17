import Foundation

/// The one definition of "this process is a test host". `LaunchMigrations` and the
/// production Keychain guard both read it, so the two can never disagree.
///
/// A signal is any one of these, because a false positive only costs one skipped
/// migration or one refused read, while a false negative touches the operator's real
/// Keychain: `XCTestConfigurationFilePath`, `XCTestBundlePath` or
/// `XCTestSessionIdentifier` in the environment, or the `XCTestCase` class loaded.
/// A release build of the app never links XCTest.
///
/// Not a signal here: Xcode previews and the UI-test suppression key. Those are launch
/// migration concerns only (see `LaunchMigrations.shouldSkip`). Treating them as a test
/// host would stop a debug launch from reading its own Keychain items.
nonisolated enum TestHostDetection {
    static let environmentKeys = [
        "XCTestConfigurationFilePath",
        "XCTestBundlePath",
        "XCTestSessionIdentifier",
    ]

    static func isTestHost(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        xctestCaseClassLoaded: Bool = NSClassFromString("XCTestCase") != nil
    ) -> Bool {
        xctestCaseClassLoaded || environmentKeys.contains { environment[$0] != nil }
    }
}

/// Refuses every Keychain call on the operator's production service when the process is
/// a test host.
///
/// Why: `GrowinTests` is app-hosted, so `xcodebuild test` runs the real Growin app with
/// the operator's real Keychain. Source scans on the test files catch the obvious ways a
/// test could reach it. They cannot catch indirect reach (a property wrapper, a service
/// string built at runtime, a helper that takes a service). This guard sits at the
/// lowest layer instead, so any path that ends in a Keychain call on the production
/// service fails closed before the call is made.
///
/// Production is unchanged: with no test-host signal the predicate is false and every
/// caller proceeds exactly as before.
nonisolated enum ProductionKeychainGuard {
    /// The operator's real Keychain service. Defined here, and only here, so the legacy
    /// cleanup (which must not name the store type) can refuse it too.
    static let productionService = "san.Growin.credentials.v1"

    /// Stable code for the refusal. Tests assert on this, never on message text.
    static let refusalCode = "PRODUCTION_KEYCHAIN_REFUSED_UNDER_TESTS"

    /// The whole decision, as a pure function so it is testable without the production service.
    static func refuses(isProductionStore: Bool, testHost: Bool) -> Bool {
        isProductionStore && testHost
    }

    /// The same decision from a service string and the service to protect. Production code
    /// always passes `productionService`; tests pass a test-prefixed pair to prove the refusal.
    static func refuses(service: String, protectedService: String, testHost: Bool) -> Bool {
        refuses(isProductionStore: service == protectedService, testHost: testHost)
    }

    /// The decision for the running process. Evaluated on every call, never cached: a stale
    /// false would be a false negative.
    static func refusesCurrentProcess(service: String) -> Bool {
        refuses(
            service: service,
            protectedService: productionService,
            testHost: TestHostDetection.isTestHost())
    }
}
