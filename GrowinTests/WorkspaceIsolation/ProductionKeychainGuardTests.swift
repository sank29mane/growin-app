import Foundation
import Security
import Testing
@testable import Growin

/// The runtime guard that keeps a test host away from the operator's real Keychain.
///
/// These tests never name the production service (the source scans in
/// `LaunchMigrationsTests` and `tests/backend/test_swift_scan_bypasses.py` forbid it). They
/// prove the decision with the pure predicate and with a test-prefixed service used as the
/// "protected" one, which exercises the same code the production service goes through. That
/// the guard sits before every SecItem call is proved by `tests/backend/test_keychain_test_host_guard.py`.
@MainActor
@Suite(.serialized)
struct ProductionKeychainGuardTests {
    private static func testService() -> String {
        "san.Growin.credentials.v1.test.\(UUID().uuidString)"
    }

    /// The code of the refusal, nil when the call is allowed, a marker for any other error.
    private static func refusalCode(service: String, guarding: String, testHost: Bool) -> String? {
        do {
            try KeychainStore.checkAccess(service: service, protectedService: guarding, testHost: testHost)
            return nil
        } catch let error as KeychainStoreError {
            return error.code
        } catch {
            return "UNEXPECTED_ERROR"
        }
    }

    // MARK: The predicate

    @Test func onlyAProductionStoreInATestHostIsRefused() {
        let table: [(isProductionStore: Bool, testHost: Bool, refused: Bool)] = [
            (true, true, true),
            (true, false, false),
            (false, true, false),
            (false, false, false),
        ]
        for row in table {
            #expect(
                ProductionKeychainGuard.refuses(isProductionStore: row.isProductionStore, testHost: row.testHost)
                    == row.refused,
                "production \(row.isProductionStore), test host \(row.testHost)")
        }
    }

    @Test func theServiceFormAgreesWithThePredicate() {
        let service = Self.testService()
        let other = Self.testService()
        #expect(ProductionKeychainGuard.refuses(service: service, protectedService: service, testHost: true))
        #expect(!ProductionKeychainGuard.refuses(service: service, protectedService: service, testHost: false))
        #expect(!ProductionKeychainGuard.refuses(service: service, protectedService: other, testHost: true))
        // Exact match only: a longer or shorter service is a different store.
        #expect(!ProductionKeychainGuard.refuses(service: service + "x", protectedService: service, testHost: true))
        #expect(!ProductionKeychainGuard.refuses(service: String(service.dropLast()), protectedService: service, testHost: true))
    }

    // MARK: The typed refusal

    @Test func theStoreRefusesWithAStableCodeOnlyWhenProtectedAndInATestHost() {
        let service = Self.testService()
        let other = Self.testService()
        let code = "PRODUCTION_KEYCHAIN_REFUSED_UNDER_TESTS"
        #expect(ProductionKeychainGuard.refusalCode == code)
        #expect(Self.refusalCode(service: service, guarding: service, testHost: true) == code)
        #expect(Self.refusalCode(service: service, guarding: service, testHost: false) == nil)
        #expect(Self.refusalCode(service: service, guarding: other, testHost: true) == nil)
    }

    @Test func theRefusalMessageCarriesTheCodeAndNoServiceOrAccount() {
        let service = Self.testService()
        var message: String?
        do {
            try KeychainStore.checkAccess(service: service, protectedService: service, testHost: true)
            Issue.record("expected a refusal")
        } catch let error as KeychainStoreError {
            guard case .productionKeychainRefusedUnderTests = error else {
                Issue.record("wrong case: \(error.code)")
                return
            }
            message = error.errorDescription
        } catch {
            Issue.record("wrong error type")
        }
        #expect(message?.contains(ProductionKeychainGuard.refusalCode) == true)
        #expect(message?.contains(service) == false)
        #expect(message?.contains("san.Growin") == false)
        for name in CredentialName.allCases {
            #expect(message?.contains(name.rawValue) == false)
        }
    }

    // MARK: The legacy cleanup

    @Test func theLegacyCleanupRefusesAProtectedServiceBeforeAnyWork() {
        let service = Self.testService()
        let suite = "growin.test.guard.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        var deleteCalls = 0
        var lines: [String] = []

        let outcome = LegacyT212KeychainCleanup.runOnce(
            service: service,
            protectedService: service,
            testHost: true,
            defaults: defaults,
            delete: { _, _ in
                deleteCalls += 1
                return errSecSuccess
            },
            log: { lines.append($0) })

        #expect(outcome.refused)
        #expect(outcome.skipped)
        #expect(outcome.removed == 0)
        #expect(deleteCalls == 0, "no delete, so no SecItem call")
        // No completion mark, no failure count: a refused launch leaves the defaults as it found them.
        #expect(!defaults.bool(forKey: LegacyT212KeychainCleanup.completionKey))
        #expect(defaults.object(forKey: LegacyT212KeychainCleanup.failedLaunchesKey) == nil)
        #expect(lines.contains { $0.contains(ProductionKeychainGuard.refusalCode) })
        #expect(lines.allSatisfy { !$0.contains(service) })
    }

    @Test func theLegacyCleanupRunsWhenTheServiceIsNotProtectedOrNotATestHost() {
        let service = Self.testService()
        for (guarding, testHost) in [(Self.testService(), true), (service, false)] {
            let suite = "growin.test.guard.\(UUID().uuidString)"
            let defaults = UserDefaults(suiteName: suite)!
            defer { defaults.removePersistentDomain(forName: suite) }
            var deleteCalls = 0

            let outcome = LegacyT212KeychainCleanup.runOnce(
                service: service,
                protectedService: guarding,
                testHost: testHost,
                defaults: defaults,
                delete: { _, _ in
                    deleteCalls += 1
                    return errSecItemNotFound
                },
                log: { _ in })

            #expect(!outcome.refused)
            #expect(!outcome.skipped)
            #expect(deleteCalls == LegacyT212KeychainCleanup.legacyAccounts.count)
            #expect(defaults.bool(forKey: LegacyT212KeychainCleanup.completionKey))
        }
    }

    @Test func aRefusedDeleteIsTerminalAndLeavesTheDefaultsAlone() {
        let service = Self.testService()
        let suite = "growin.test.guard.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        for key in LegacyT212KeychainCleanup.legacyDefaultsKeys {
            defaults.set("CANARY-LEGACY", forKey: key)
        }
        var deleteCalls = 0

        // The service is not protected, so the front check passes. The delete itself reports a refusal.
        let outcome = LegacyT212KeychainCleanup.runOnce(
            service: service,
            defaults: defaults,
            delete: { _, _ in
                deleteCalls += 1
                return LegacyT212KeychainCleanup.refusedUnderTestsStatus
            },
            log: { _ in })

        #expect(outcome.refused)
        #expect(outcome.skipped)
        #expect(outcome.failedStatuses.isEmpty, "a refusal is not an ordinary Keychain failure")
        #expect(!outcome.gaveUp)
        #expect(deleteCalls == 1, "stops at the first refusal")
        for key in LegacyT212KeychainCleanup.legacyDefaultsKeys {
            #expect(defaults.string(forKey: key) == "CANARY-LEGACY", "legacy defaults key kept")
        }
        #expect(!defaults.bool(forKey: LegacyT212KeychainCleanup.completionKey))
        #expect(defaults.object(forKey: LegacyT212KeychainCleanup.failedLaunchesKey) == nil)
    }

    @Test func theRefusedStatusIsOutsideTheSecurityFrameworkRange() {
        let status = LegacyT212KeychainCleanup.refusedUnderTestsStatus
        #expect(status != errSecSuccess)
        #expect(status != errSecItemNotFound)
        #expect(!LegacyT212KeychainCleanup.stopImmediatelyStatuses.contains(status))
        // The real Security errors this app handles all sit above -30000.
        #expect(status < -30000)
    }

    // MARK: Shared test-host detection

    @Test func eachTestHostSignalAloneIsDetected() {
        for key in TestHostDetection.environmentKeys {
            #expect(TestHostDetection.isTestHost(environment: [key: "x"], xctestCaseClassLoaded: false), "\(key)")
        }
        #expect(TestHostDetection.isTestHost(environment: [:], xctestCaseClassLoaded: true))
        #expect(TestHostDetection.environmentKeys.count == 3)
    }

    @Test func aNormalLaunchIsNotATestHost() {
        let normal = ["HOME": "/Users/x", "PATH": "/usr/bin", "__CFBundleIdentifier": "san.Growin"]
        #expect(!TestHostDetection.isTestHost(environment: normal, xctestCaseClassLoaded: false))
        #expect(!TestHostDetection.isTestHost(environment: [:], xctestCaseClassLoaded: false))
    }

    @Test func previewAndSuppressionKeysAreNotTestHostSignals() {
        // They skip launch migrations (LaunchMigrations) but must not cut a debug launch off from its own Keychain.
        let env = [LaunchMigrations.previewEnvironmentKey: "1", LaunchMigrations.skipEnvironmentKey: "1"]
        #expect(!TestHostDetection.isTestHost(environment: env, xctestCaseClassLoaded: false))
        #expect(LaunchMigrations.shouldSkip(environment: env, xctestCaseClassLoaded: false))
    }

    @Test func launchMigrationsReadsTheSameDetection() {
        #expect(LaunchMigrations.testHostEnvironmentKeys == TestHostDetection.environmentKeys)
        for key in TestHostDetection.environmentKeys {
            #expect(LaunchMigrations.shouldSkip(environment: [key: "x"], xctestCaseClassLoaded: false))
        }
    }
}
