import Foundation
import Security
import Testing
@testable import Growin

/// Every Keychain test here uses its own service, so the operator's real
/// `san.Growin.credentials.v1` items are never read, moved or deleted. The
/// canary values below are fake. No test calls the migration with its default
/// service; `noTestCanReachTheProductionKeychain` enforces that.
@Suite(.serialized)
struct LegacyT212KeychainCleanupTests {
    private typealias Cleanup = LegacyT212KeychainCleanup

    private static let canary = Data("CANARY-KEY-1234567890".utf8)

    private final class Recorder: @unchecked Sendable {
        var lines: [String] = []
        var calls: [(service: String, account: String)] = []
    }

    private struct Rig {
        let service: String
        let raw: RawKeychain
        let defaults: UserDefaults
        let suite: String

        init() {
            service = "san.Growin.credentials.v1.test.\(UUID().uuidString)"
            raw = RawKeychain(service: service)
            suite = "growin.test.legacy-t212.\(UUID().uuidString)"
            defaults = UserDefaults(suiteName: suite)!
        }

        func seed(_ accounts: [String], in keychain: RawKeychain? = nil) throws {
            for account in accounts {
                try (keychain ?? raw).set(LegacyT212KeychainCleanupTests.canary, account: account)
            }
        }

        func tearDown(extra: [String] = []) {
            for account in Cleanup.legacyAccounts + extra { raw.remove(account: account) }
            defaults.removePersistentDomain(forName: suite)
        }
    }

    /// Items that look like the legacy ones but are not. All must survive.
    private static var decoyAccounts: [String] {
        let flat = Cleanup.flatAccounts[0]
        return [
            flat + "2",                                   // longer account, same prefix
            "x" + flat,                                   // longer account, same suffix
            "uk:" + flat + "X",                           // scoped, longer
            "india:" + flat,                              // other workspace scope
            "shared:" + flat,                             // other scope
            "uk:" + CredentialName.trading212IsaApiKey.rawValue,
            "uk:" + CredentialName.alpacaApiKey.rawValue,
            "shared:" + CredentialName.openaiApiKey.rawValue,
            "india:" + CredentialName.approvalSigningKey.rawValue,
            CredentialName.approvalSigningKey.rawValue,   // Secure Enclave approval key account
            "breezeApiKey",
        ]
    }

    // MARK: Behavior

    @Test func removesExactlyTheLegacyAccountsAndNothingElse() throws {
        let rig = Rig()
        // One neighbour service sorts before the test service and one after, so a delete
        // that ignores the service cannot get lucky whichever order Keychain picks.
        let neighbours = [rig.service.replacingOccurrences(of: ".test.", with: ".test!."),
                          rig.service.replacingOccurrences(of: ".test.", with: ".test~.")]
            .map { RawKeychain(service: $0) }
        defer {
            rig.tearDown(extra: Self.decoyAccounts)
            for neighbour in neighbours {
                for account in Cleanup.legacyAccounts { neighbour.remove(account: account) }
            }
        }
        // Seed order matters: SecItemDelete removes one arbitrary match, usually the oldest.
        // Victims go in first and the targets last, so a query that is too broad kills a
        // decoy instead of getting lucky and hitting a target.
        // Same account string, different service: must survive.
        for neighbour in neighbours { try rig.seed(Cleanup.legacyAccounts, in: neighbour) }
        try rig.seed(Self.decoyAccounts)
        try rig.seed(Cleanup.legacyAccounts)

        let recorder = Recorder()
        let outcome = Cleanup.runOnce(
            service: rig.service, defaults: rig.defaults,
            log: { recorder.lines.append($0) })

        #expect(outcome == Cleanup.Outcome(skipped: false, removed: 4, failedStatuses: []))
        for account in Cleanup.legacyAccounts {
            #expect(try rig.raw.data(account: account) == nil, "legacy item still present")
        }
        for account in Self.decoyAccounts {
            #expect(try rig.raw.data(account: account) == Self.canary, "decoy was deleted")
        }
        for neighbour in neighbours {
            for account in Cleanup.legacyAccounts {
                #expect(try neighbour.data(account: account) == Self.canary, "other-service item was deleted")
            }
        }
        #expect(recorder.lines == ["removed 4 legacy items"])
        #expect(rig.defaults.bool(forKey: Cleanup.completionKey))
    }

    @Test func runsOnceThenDoesNothing() throws {
        let rig = Rig()
        defer { rig.tearDown() }
        let recorder = Recorder()
        Cleanup.runOnce(service: rig.service, defaults: rig.defaults, log: { recorder.lines.append($0) })

        // An item that shows up after completion is left alone: the migration is finished.
        try rig.seed([Cleanup.legacyAccounts[0]])
        let second = Cleanup.runOnce(
            service: rig.service, defaults: rig.defaults,
            delete: { service, account in
                recorder.calls.append((service, account))
                return errSecSuccess
            },
            log: { recorder.lines.append($0) })

        #expect(second.skipped)
        #expect(recorder.calls.isEmpty)
        #expect(try rig.raw.data(account: Cleanup.legacyAccounts[0]) == Self.canary)
        #expect(recorder.lines == ["removed 0 legacy items"], "second run must not log")
    }

    @Test func emptyKeychainIsASuccessNotAFailure() {
        let rig = Rig()
        defer { rig.tearDown() }
        let recorder = Recorder()
        let outcome = Cleanup.runOnce(service: rig.service, defaults: rig.defaults, log: { recorder.lines.append($0) })

        #expect(outcome == Cleanup.Outcome(skipped: false, removed: 0, failedStatuses: []))
        #expect(recorder.lines == ["removed 0 legacy items"])
        #expect(rig.defaults.bool(forKey: Cleanup.completionKey))
    }

    @Test func versionedCompletionKey() {
        #expect(Cleanup.completionKey.contains(".v1."))
    }

    @Test func eachDeleteTargetsExactlyOneServiceAndAccountPair() {
        let rig = Rig()
        defer { rig.tearDown() }
        let recorder = Recorder()
        Cleanup.runOnce(
            service: rig.service, defaults: rig.defaults,
            delete: { service, account in
                recorder.calls.append((service, account))
                return errSecItemNotFound
            },
            log: { _ in })

        #expect(recorder.calls.map(\.account) == Cleanup.legacyAccounts)
        #expect(recorder.calls.allSatisfy { $0.service == rig.service })
        #expect(Cleanup.legacyAccounts.count == 4)
        #expect(Set(Cleanup.legacyAccounts).count == 4)
    }

    @Test func keychainErrorsNeverFailLaunchAndAreRetried() {
        let rig = Rig()
        defer { rig.tearDown() }
        let recorder = Recorder()
        let denied = errSecInteractionNotAllowed

        let failing = Cleanup.runOnce(
            service: rig.service, defaults: rig.defaults,
            delete: { _, _ in denied },
            log: { recorder.lines.append($0) })

        #expect(failing.failedStatuses == Array(repeating: denied, count: 4))
        #expect(failing.removed == 0)
        #expect(!rig.defaults.bool(forKey: Cleanup.completionKey), "a failed run must not be recorded as done")
        let allowed = #/^(legacy cleanup keychain status -?\d+|removed \d+ legacy items)$/#
        #expect(recorder.lines.allSatisfy { $0.wholeMatch(of: allowed) != nil }, "\(recorder.lines)")
        #expect(recorder.lines.contains("legacy cleanup keychain status \(denied)"))

        // Next launch the Keychain is healthy: the migration finishes.
        let retry = Cleanup.runOnce(service: rig.service, defaults: rig.defaults, log: { _ in })
        #expect(retry.failedStatuses.isEmpty)
        #expect(rig.defaults.bool(forKey: Cleanup.completionKey))
    }

    @Test func oneFailingItemDoesNotStopTheOthers() throws {
        let rig = Rig()
        defer { rig.tearDown() }
        try rig.seed(Cleanup.legacyAccounts)
        let blocked = Cleanup.legacyAccounts[1]

        let outcome = Cleanup.runOnce(
            service: rig.service, defaults: rig.defaults,
            delete: { service, account in
                account == blocked ? errSecAuthFailed : Cleanup.deleteExactItem(service: service, account: account)
            },
            log: { _ in })

        #expect(outcome.removed == 3)
        #expect(outcome.failedStatuses == [errSecAuthFailed])
        #expect(try rig.raw.data(account: blocked) == Self.canary)
        #expect(!rig.defaults.bool(forKey: Cleanup.completionKey))
    }

    @Test func logsHoldNoSecretAndNoAccountName() throws {
        let rig = Rig()
        let second = Rig()
        defer { rig.tearDown(); second.tearDown() }
        try rig.seed(Cleanup.legacyAccounts)
        let recorder = Recorder()
        Cleanup.runOnce(service: rig.service, defaults: rig.defaults, log: { recorder.lines.append($0) })
        Cleanup.runOnce(
            service: rig.service, defaults: second.defaults,
            delete: { _, _ in errSecAuthFailed }, log: { recorder.lines.append($0) })

        let joined = recorder.lines.joined(separator: "\n").lowercased()
        #expect(!joined.contains("canary"))
        for account in Cleanup.legacyAccounts {
            #expect(!joined.contains(account.lowercased()), "log leaked an account name")
        }
        #expect(!joined.contains(rig.service.lowercased()), "log leaked the service name")
    }

    @Test func plaintextDefaultsCopiesAreRemovedAndOtherDefaultsSurvive() {
        let rig = Rig()
        defer { rig.tearDown() }
        for key in Cleanup.legacyDefaultsKeys { rig.defaults.set("CANARY-KEY-1234567890", forKey: key) }
        rig.defaults.set("uk", forKey: WorkspaceSelection.defaultsKey)
        rig.defaults.set("CANARY-OTHER", forKey: CredentialName.openaiApiKey.rawValue)

        Cleanup.runOnce(service: rig.service, defaults: rig.defaults, log: { _ in })

        for key in Cleanup.legacyDefaultsKeys { #expect(rig.defaults.object(forKey: key) == nil) }
        #expect(rig.defaults.string(forKey: WorkspaceSelection.defaultsKey) == "uk")
        #expect(rig.defaults.string(forKey: CredentialName.openaiApiKey.rawValue) == "CANARY-OTHER")
    }

    // MARK: Source guards

    private static let migrationPath = "Growin/Security/LegacyT212KeychainCleanup.swift"

    /// Files that spell a legacy account name, ignoring case.
    private static func offenders(in sources: [(path: String, text: String)]) -> [String] {
        let needles = Cleanup.flatAccounts.map { $0.lowercased() }
        return sources
            .filter { $0.path != migrationPath }
            .filter { source in
                let text = source.text.lowercased()
                return needles.contains { text.contains($0) }
            }
            .map(\.path)
    }

    private static func allSwiftSources() throws -> [(path: String, text: String)] {
        try ["Growin", "GrowinTests", "GrowinUITests"].flatMap { try SourceTree.swiftSources(under: $0) }
    }

    @Test func legacyAccountNamesAppearOnlyInTheMigrationFile() throws {
        let sources = try Self.allSwiftSources()
        // Positive control: the scan really sees the migration file and the names.
        let migration = try #require(sources.first { $0.path == Self.migrationPath })
        for name in Cleanup.flatAccounts { #expect(migration.text.contains(name)) }
        #expect(sources.contains { $0.path.hasPrefix("GrowinTests/") })

        #expect(Self.offenders(in: sources) == [])
    }

    @Test func theNameScanCatchesPlantedViolations() {
        let key = Cleanup.flatAccounts[0]
        let secret = Cleanup.flatAccounts[1]
        let planted: [(path: String, text: String)] = [
            ("A.swift", "case \(key)"),
            ("B.swift", "let a = \"uk:\(secret)\""),
            ("C.swift", "// \(key.lowercased())"),
            ("D.swift", "KeychainStore.shared.string(for: .\(key), scope: .shared)"),
            ("E.swift", "let clean = 1"),
            (Self.migrationPath, key),
        ]
        #expect(Self.offenders(in: planted) == ["A.swift", "B.swift", "C.swift", "D.swift"])
    }

    @Test func theMigrationNeverReadsAKeychainValue() throws {
        let text = try SourceTree.contents(Self.migrationPath)
        for banned in ["SecItemCopyMatching", "kSecReturnData", "kSecReturnAttributes", "kSecReturnRef",
                       "kSecReturnPersistentRef", "kSecValueData", "SecItemAdd", "SecItemUpdate", "print(", "NSLog("] {
            #expect(!text.contains(banned), "\(banned) must not appear in the migration")
        }
        #expect(text.components(separatedBy: "SecItemDelete(").count == 2, "exactly one delete call")
        // The one delete query pins class, service and account.
        let body = try #require(text.components(separatedBy: "static func deleteExactItem").dropFirst().first)
        let queryBlock = String(body.prefix(400))
        for pinned in ["kSecClassGenericPassword", "kSecAttrService", "kSecAttrAccount"] {
            #expect(queryBlock.contains(pinned), "\(pinned) missing from the delete query")
        }
        #expect(!queryBlock.contains("kSecMatch"))
    }

    @Test func theAppRunsTheMigrationAtLaunch() throws {
        let app = try SourceTree.contents("Growin/GrowinApp.swift")
        let initBody = try #require(app.components(separatedBy: "init() {").dropFirst().first)
        let upToBody = String(initBody.prefix(900))
        #expect(upToBody.contains("LegacyT212KeychainCleanup.runOnce("))
    }

    @Test func noTestCanReachTheProductionKeychain() throws {
        // Needles are assembled so this file does not trip its own scan.
        let needles = ["runOnce" + "()", "production" + "Service"]
        let offenders = try SourceTree.swiftSources(under: "GrowinTests")
            .filter { source in needles.contains { source.text.contains($0) } }
            .map(\.path)
        #expect(offenders == [])
    }
}
