import Foundation
import Security
import Testing
@testable import Growin

/// Every Keychain test here passes its own private service to the migration, so
/// the operator's real `san.Growin.credentials.v1` items are not read, moved or
/// deleted by these tests. The canary values below are fake.
///
/// That is not the whole story: GrowinTests is app-hosted, so the test runner
/// launches the real app and `GrowinApp.init` runs. Keeping that launch away from the
/// real Keychain is the job of the `LaunchMigrations` test-host guard, covered in
/// `LaunchMigrationsTests`. This file only guarantees that no test names the
/// production service (`noTestCanReachTheProductionKeychain`) and that every
/// migration call states its service (`everyMigrationCallStatesItsService`).
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
    }

    @Test func theDeleteQueryPinsOneItemAndNeverPrompts() {
        let query = Cleanup.exactQuery(service: "svc", account: "acct")
        #expect(Set(query.keys) == [
            kSecClass as String, kSecAttrService as String, kSecAttrAccount as String,
            kSecUseAuthenticationUI as String,
        ])
        #expect(query[kSecClass as String] as? String == kSecClassGenericPassword as String)
        #expect(query[kSecAttrService as String] as? String == "svc")
        #expect(query[kSecAttrAccount as String] as? String == "acct")
        #expect(query[kSecUseAuthenticationUI as String] as? String == kSecUseAuthenticationUIFail as String)
    }

    @Test func noTestCanReachTheProductionKeychain() throws {
        // Needles are assembled so this file does not trip its own scan.
        let offenders = try SourceTree.swiftSources(under: "GrowinTests")
            .filter { $0.text.contains("production" + "Service") }
            .map(\.path)
        #expect(offenders == [])
    }

    // MARK: Every call states its service

    /// Counts calls to the migration whose first argument is not `service:`. The needle is
    /// assembled so this file does not trip its own scan.
    private static func callsWithoutService(in text: String) -> Int {
        let needle = "run" + "Once("
        var count = 0
        var rest = Substring(text)
        while let found = rest.range(of: needle) {
            let after = rest[found.upperBound...]
            if !after.drop(while: { $0.isWhitespace }).hasPrefix("service:") { count += 1 }
            rest = after
        }
        return count
    }

    @Test func everyMigrationCallStatesItsService() throws {
        for source in try Self.allSwiftSources() {
            #expect(Self.callsWithoutService(in: source.text) == 0, "\(source.path) calls the migration without service:")
        }
        // The migration itself has no default service to fall back on.
        #expect(!(try SourceTree.contents(Self.migrationPath)).contains("production" + "Service"))
    }

    @Test func theCallScanCatchesPlantedCalls() {
        let call = "run" + "Once"
        #expect(Self.callsWithoutService(in: "\(call)()") == 1)
        #expect(Self.callsWithoutService(in: "\(call)(defaults: d)") == 1)
        #expect(Self.callsWithoutService(in: "\(call)(log: { _ in }, service: s)") == 1)
        #expect(Self.callsWithoutService(in: "\(call)(service: s)") == 0)
        #expect(Self.callsWithoutService(in: "\(call)(\n            service: s,\n defaults: d)") == 0)
        #expect(Self.callsWithoutService(in: "\(call)() \(call)(service: s) \(call)(defaults: d)") == 2)
    }

    // MARK: Duplicates, bounds, prompts and retry cap

    /// A fake keychain holding N matches per account. Each delete removes one, like the file keychain.
    private final class FakeKeychain: @unchecked Sendable {
        var matches: [String: Int]
        var calls: [String] = []
        init(_ matches: [String: Int]) { self.matches = matches }
        func delete(_ service: String, _ account: String) -> OSStatus {
            calls.append(account)
            if let left = matches[account], left > 0 {
                matches[account] = left - 1
                return errSecSuccess
            }
            return errSecItemNotFound
        }
    }

    @Test func duplicateMatchesAreAllDeletedBeforeCompletionIsRecorded() {
        let rig = Rig()
        defer { rig.tearDown() }
        let accounts = Cleanup.legacyAccounts
        let fake = FakeKeychain([accounts[0]: 3, accounts[2]: 2])

        let outcome = Cleanup.runOnce(
            service: rig.service, defaults: rig.defaults,
            delete: { fake.delete($0, $1) }, log: { _ in })

        #expect(outcome.removed == 5)
        #expect(outcome.failedStatuses.isEmpty)
        #expect(fake.matches.values.allSatisfy { $0 == 0 }, "a duplicate survived")
        #expect(fake.calls.count == 9, "each account is deleted until not-found")
        #expect(rig.defaults.bool(forKey: Cleanup.completionKey))
    }

    @Test func theDeleteLoopIsBoundedAndTheBoundIsAnError() {
        let limit = Cleanup.maxDeletesPerAccount
        let first = Cleanup.legacyAccounts[0]

        // One under the bound still finishes cleanly (needs the final not-found call).
        let ok = Rig()
        defer { ok.tearDown() }
        let fakeOK = FakeKeychain([first: limit - 1])
        let finished = Cleanup.runOnce(service: ok.service, defaults: ok.defaults,
                                       delete: { fakeOK.delete($0, $1) }, log: { _ in })
        #expect(finished.removed == limit - 1)
        #expect(finished.failedStatuses.isEmpty)

        // At the bound the migration cannot tell the matches are gone, so it reports an error.
        let capped = Rig()
        defer { capped.tearDown() }
        let fakeHuge = FakeKeychain([first: 1000])
        let result = Cleanup.runOnce(service: capped.service, defaults: capped.defaults,
                                     delete: { fakeHuge.delete($0, $1) }, log: { _ in })
        #expect(fakeHuge.calls.filter { $0 == first }.count == limit)
        #expect(result.failedStatuses == [Cleanup.tooManyMatchesStatus])
        #expect(!capped.defaults.bool(forKey: Cleanup.completionKey))
    }

    @Test func aNonNotFoundErrorStopsThatAccountImmediately() {
        let rig = Rig()
        defer { rig.tearDown() }
        let first = Cleanup.legacyAccounts[0]
        let counter = Recorder()
        let outcome = Cleanup.runOnce(
            service: rig.service, defaults: rig.defaults,
            delete: { _, account in
                guard account == first else { return errSecItemNotFound }
                counter.calls.append(("", account))
                return counter.calls.count == 1 ? errSecSuccess : errSecAuthFailed
            },
            log: { _ in })

        #expect(counter.calls.count == 2, "no retry after an error")
        #expect(outcome.removed == 1)
        #expect(outcome.failedStatuses == [errSecAuthFailed])
    }

    @Test func retriesStopAfterThreeFailedLaunchesWithOneHelpfulLine() {
        let rig = Rig()
        defer { rig.tearDown() }
        let recorder = Recorder()
        func launch() -> Cleanup.Outcome {
            Cleanup.runOnce(
                service: rig.service, defaults: rig.defaults,
                delete: { service, account in
                    recorder.calls.append((service, account))
                    return errSecInteractionNotAllowed
                },
                log: { recorder.lines.append($0) })
        }

        let first = launch()
        let second = launch()
        #expect(!first.gaveUp && !second.gaveUp)
        #expect(!recorder.lines.contains(Cleanup.giveUpMessage))

        let third = launch()
        #expect(third.gaveUp)
        #expect(recorder.lines.filter { $0 == Cleanup.giveUpMessage }.count == 1)
        #expect(Cleanup.giveUpMessage.contains("Keychain Access"))
        #expect(rig.defaults.integer(forKey: Cleanup.failedLaunchesKey) == 3)

        let linesBefore = recorder.lines.count
        let callsBefore = recorder.calls.count
        let fourth = launch()
        #expect(fourth.skipped)
        #expect(recorder.calls.count == callsBefore, "no Keychain call after giving up")
        #expect(recorder.lines.count == linesBefore, "silent after giving up")
        #expect(!rig.defaults.bool(forKey: Cleanup.completionKey))
    }

    @Test func aSuccessfulLaunchBeforeTheCapStillCompletes() {
        let rig = Rig()
        defer { rig.tearDown() }
        Cleanup.runOnce(service: rig.service, defaults: rig.defaults,
                        delete: { _, _ in errSecInteractionNotAllowed }, log: { _ in })
        Cleanup.runOnce(service: rig.service, defaults: rig.defaults,
                        delete: { _, _ in errSecInteractionNotAllowed }, log: { _ in })
        let recovered = Cleanup.runOnce(service: rig.service, defaults: rig.defaults, log: { _ in })

        #expect(recovered.failedStatuses.isEmpty)
        #expect(!recovered.gaveUp)
        #expect(rig.defaults.bool(forKey: Cleanup.completionKey))
    }
}
