import Foundation
import Security
import Testing
@testable import Growin

/// Every test uses its own Keychain service so the operator's real
/// `san.Growin.credentials.v1` items are never read, moved or deleted.
@Suite(.serialized)
struct KeychainScopeTests {
    private static func makeStore() -> (KeychainStore, String) {
        let service = "san.Growin.credentials.v1.test.\(UUID().uuidString)"
        return (KeychainStore(service: service), service)
    }

    /// Removes every item the test could have created under its private service.
    private static func cleanUp(_ store: KeychainStore) {
        for name in CredentialName.allCases {
            try? store.removeLegacyFlatItem(name)
            for scope in [KeychainScope.shared, .workspace(.uk), .workspace(.india)] {
                try? store.remove(name, scope: scope)
            }
        }
    }

    /// A Keychain status problem on the host is a failure, never a skip.
    private static func requireKeychain(_ body: () throws -> Void, sourceLocation: SourceLocation = #_sourceLocation) {
        do {
            try body()
        } catch KeychainStoreError.status(let status) {
            Issue.record("Keychain blocked with OSStatus \(status)", sourceLocation: sourceLocation)
        } catch {
            Issue.record("Unexpected error: \(error)", sourceLocation: sourceLocation)
        }
    }

    @Test func policyTableMatchesOperatorDecision() {
        let shared: [CredentialName] = [.openaiApiKey, .geminiApiKey, .finnhubApiKey, .newsApiKey, .tavilyApiKey]
        let uk: [CredentialName] = [
            .trading212IsaApiKey, .trading212IsaApiSecret,
            .t212InvestKey, .t212InvestSecret, .t212IsaKey, .t212IsaSecret,
            .alpacaApiKey, .alpacaSecretKey,
        ]
        for name in shared { #expect(name.policy == .shared, "\(name)") }
        for name in uk { #expect(name.policy == .fixed(.uk), "\(name)") }
        #expect(CredentialName.approvalSigningKey.policy == .perWorkspace)
        #expect(CredentialName.approvalSigningKey.rawValue == "approvalSoftwareP256PrivateKey.v1")
        // 63-03: the India Secure Enclave key blob is the one added credential, India only.
        #expect(CredentialName.approvalSecureEnclaveKey.policy == .fixed(.india))
        #expect(CredentialName.allCases.count == shared.count + uk.count + 2)
        #expect(KeychainScope.shared.accountPrefix == "shared")
        #expect(KeychainScope.workspace(.uk).accountPrefix == "uk")
        #expect(KeychainScope.workspace(.india).accountPrefix == "india")
    }

    @Test func ukCredentialIsInvisibleToIndiaScopeAndRawIndiaAccount() {
        let (store, service) = Self.makeStore()
        defer { Self.cleanUp(store) }
        Self.requireKeychain {
            try store.set("uk-secret", for: .alpacaApiKey, scope: .workspace(.uk))
            #expect(try store.string(for: .alpacaApiKey, scope: .workspace(.uk)) == "uk-secret")

            #expect(throws: KeychainStoreError.self) {
                _ = try store.string(for: .alpacaApiKey, scope: .workspace(.india))
            }
            #expect(throws: KeychainStoreError.self) {
                try store.set("x", for: .alpacaApiKey, scope: .workspace(.india))
            }
            #expect(throws: KeychainStoreError.self) {
                _ = try store.data(for: .alpacaApiKey, scope: .shared)
            }
            // The raw india: account does not exist in the real Keychain.
            #expect(try RawKeychain(service: service).data(account: "india:alpacaApiKey") == nil)
            #expect(try RawKeychain(service: service).data(account: "uk:alpacaApiKey") != nil)
        }
    }

    @Test func scopeNotAllowedNamesTheCredential() {
        let (store, _) = Self.makeStore()
        defer { Self.cleanUp(store) }
        do {
            _ = try store.string(for: .alpacaApiKey, scope: .workspace(.india))
            Issue.record("Expected scopeNotAllowed")
        } catch KeychainStoreError.scopeNotAllowed(let name) {
            #expect(name == .alpacaApiKey)
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
    }

    @Test func sharedCredentialReadsOnlyWithSharedScope() {
        let (store, service) = Self.makeStore()
        defer { Self.cleanUp(store) }
        Self.requireKeychain {
            try store.set("llm-key", for: .openaiApiKey, scope: .shared)
            #expect(try store.string(for: .openaiApiKey, scope: .shared) == "llm-key")
            #expect(throws: KeychainStoreError.self) {
                _ = try store.string(for: .openaiApiKey, scope: .workspace(.uk))
            }
            #expect(throws: KeychainStoreError.self) {
                _ = try store.string(for: .openaiApiKey, scope: .workspace(.india))
            }
            #expect(try RawKeychain(service: service).data(account: "shared:openaiApiKey") != nil)
        }
    }

    // MARK: Production guard (a test-prefixed store is never refused)

    @Test func aTestPrefixedStoreIsNotTheProductionStore() {
        let (store, _) = Self.makeStore()
        #expect(!store.isProductionStore)
    }

    @Test func aTestPrefixedStoreIsNeverRefusedInsideTheTestHost() {
        // Positive control: this process really is a test host, so the production guard is live.
        #expect(TestHostDetection.isTestHost())
        let (store, _) = Self.makeStore()
        defer { Self.cleanUp(store) }
        // requireKeychain records any thrown error, so a refusal here is a failure.
        Self.requireKeychain {
            try store.set("CANARY-GUARD-1", for: .alpacaApiKey, scope: .workspace(.uk))
            #expect(try store.string(for: .alpacaApiKey, scope: .workspace(.uk)) == "CANARY-GUARD-1")
            try store.remove(.alpacaApiKey, scope: .workspace(.uk))
            #expect(try store.string(for: .alpacaApiKey, scope: .workspace(.uk)) == nil)
        }
    }

    @Test func theMigrationsRunOnATestPrefixedStore() {
        let (store, _) = Self.makeStore()
        defer { Self.cleanUp(store) }
        let suite = "growin.test.guard.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        // Nothing to move, so nothing fails. A refused store would list every candidate.
        #expect(store.migrateLegacyUserDefaults(defaults) == [])
        #expect(store.migrateFlatItemsToScoped() == [])
    }

    // MARK: Service isolation canary
    //
    // Runtime proof, independent of the source scans, that every Keychain query carries its own
    // service. If a query lost its service (stripped, swapped, merged over) a call on store A would
    // read, overwrite or delete the item that lives under store B. Both services are test-prefixed
    // and every item is removed again. This is the failure that would let `cleanUp` below wipe the
    // operator's real items.
    //
    // A failure message must never show a credential, so no `#expect` argument holds a String or Data
    // read from the Keychain: each comparison is computed into a Bool first and only the Bool is asserted.

    private static let canaryScopedAccount = "uk:alpacaApiKey"
    private static let canaryFlatAccount = CredentialName.alpacaApiKey.rawValue
    private static let canaryApprovalAccount = "uk:" + CredentialName.approvalSigningKey.rawValue

    /// Store A holds nothing of its own, so its reads are nil whatever store B holds. A remove, an empty
    /// set, a real set and a second remove on A all leave B's item byte-identical.
    @Test func aStoreNeverSeesOrChangesAnotherServicesItem() {
        let (a, _) = Self.makeStore()
        let (b, serviceB) = Self.makeStore()
        defer {
            Self.cleanUp(a)
            Self.cleanUp(b)
        }
        Self.requireKeychain {
            let planted = Data("CANARY-B".utf8)
            try b.set(planted, for: .alpacaApiKey, scope: .workspace(.uk))
            let rawB = RawKeychain(service: serviceB)
            let plantedReadsBack = try rawB.data(account: Self.canaryScopedAccount) == planted
            #expect(plantedReadsBack, "positive control")

            let aDataIsNil = try a.data(for: .alpacaApiKey, scope: .workspace(.uk)) == nil
            let aStringIsNil = try a.string(for: .alpacaApiKey, scope: .workspace(.uk)) == nil
            #expect(aDataIsNil)
            #expect(aStringIsNil)

            try a.remove(.alpacaApiKey, scope: .workspace(.uk))
            let afterRemove = try rawB.data(account: Self.canaryScopedAccount) == planted
            #expect(afterRemove, "B survives a remove on A")
            try a.set("", for: .alpacaApiKey, scope: .workspace(.uk))
            let afterEmptySet = try rawB.data(account: Self.canaryScopedAccount) == planted
            #expect(afterEmptySet, "B survives an empty set on A")
            try a.set("CANARY-A", for: .alpacaApiKey, scope: .workspace(.uk))
            let afterSet = try rawB.data(account: Self.canaryScopedAccount) == planted
            #expect(afterSet, "B survives a set on A")
            let aReadsItsOwn = try a.string(for: .alpacaApiKey, scope: .workspace(.uk)) == "CANARY-A"
            #expect(aReadsItsOwn)
            try a.remove(.alpacaApiKey, scope: .workspace(.uk))
            let afterSecondRemove = try rawB.data(account: Self.canaryScopedAccount) == planted
            #expect(afterSecondRemove, "B survives a second remove on A")
            let bStillReadsItsOwn = try b.string(for: .alpacaApiKey, scope: .workspace(.uk)) == "CANARY-B"
            #expect(bStillReadsItsOwn)
        }
    }

    /// The helper every test here uses to tidy up wipes every credential name on A, and the legacy
    /// calls and migrations run on A. None of it may reach B's scoped, flat or approval items.
    @Test func aStoreCleanUpAndLegacyCallsLeaveAnotherServicesItemsAlone() {
        let (a, _) = Self.makeStore()
        let (b, serviceB) = Self.makeStore()
        defer {
            Self.cleanUp(a)
            Self.cleanUp(b)
        }
        Self.requireKeychain {
            let rawB = RawKeychain(service: serviceB)
            let scoped = Data("CANARY-SCOPED-B".utf8)
            let flat = Data("CANARY-FLAT-B".utf8)
            let approval = Data("CANARY-APPROVAL-B".utf8)
            try b.set(scoped, for: .alpacaApiKey, scope: .workspace(.uk))
            try rawB.set(flat, account: Self.canaryFlatAccount)
            try b.set(approval, for: .approvalSigningKey, scope: .workspace(.uk))

            let aFlatIsNil = try a.legacyFlatData(for: .alpacaApiKey) == nil
            #expect(aFlatIsNil)
            try a.removeLegacyFlatItem(.alpacaApiKey)
            Self.cleanUp(a)
            let migrationsMoved = a.migrateFlatItemsToScoped()
            #expect(migrationsMoved.isEmpty)

            let scopedSurvives = try rawB.data(account: Self.canaryScopedAccount) == scoped
            let flatSurvives = try rawB.data(account: Self.canaryFlatAccount) == flat
            let approvalSurvives = try rawB.data(account: Self.canaryApprovalAccount) == approval
            #expect(scopedSurvives, "B's scoped item survives A's cleanUp and legacy calls")
            #expect(flatSurvives, "B's flat item survives A's cleanUp and legacy calls")
            #expect(approvalSurvives, "B's approval item survives A's cleanUp and legacy calls")
        }
    }

    /// The raw test helper is bound to its service in both directions.
    @Test func rawKeychainOnOneServiceCannotSeeOrRemoveAnother() {
        let (_, serviceA) = Self.makeStore()
        let (_, serviceB) = Self.makeStore()
        let rawA = RawKeychain(service: serviceA)
        let rawB = RawKeychain(service: serviceB)
        let account = "canary-raw"
        defer {
            rawA.remove(account: account)
            rawB.remove(account: account)
        }
        Self.requireKeychain {
            let bytesB = Data("CANARY-RAW-B".utf8)
            try rawB.set(bytesB, account: account)
            let aSeesNothing = try rawA.data(account: account) == nil
            #expect(aSeesNothing, "A cannot see B's item")
            rawA.remove(account: account)
            let bSurvivesARemove = try rawB.data(account: account) == bytesB
            #expect(bSurvivesARemove, "B survives a raw remove on A")

            let bytesA = Data("CANARY-RAW-A".utf8)
            try rawA.set(bytesA, account: account)
            let aReadsItsOwn = try rawA.data(account: account) == bytesA
            #expect(aReadsItsOwn)
            rawB.remove(account: account)
            let aSurvivesBRemove = try rawA.data(account: account) == bytesA
            let bIsGone = try rawB.data(account: account) == nil
            #expect(aSurvivesBRemove, "A survives a raw remove on B")
            #expect(bIsGone)
        }
    }

    @Test func approvalKeyIsPerWorkspaceAndNeverShared() {
        let (store, _) = Self.makeStore()
        defer { Self.cleanUp(store) }
        Self.requireKeychain {
            let key = Data(repeating: 7, count: 32)
            try store.set(key, for: .approvalSigningKey, scope: .workspace(.uk))
            #expect(try store.data(for: .approvalSigningKey, scope: .workspace(.uk)) == key)
            #expect(try store.data(for: .approvalSigningKey, scope: .workspace(.india)) == nil)
            #expect(throws: KeychainStoreError.self) {
                _ = try store.data(for: .approvalSigningKey, scope: .shared)
            }
            #expect(throws: KeychainStoreError.self) {
                try store.set(key, for: .approvalSigningKey, scope: .shared)
            }
        }
    }

    @Test func emptyValueRemovesTheScopedItem() {
        let (store, _) = Self.makeStore()
        defer { Self.cleanUp(store) }
        Self.requireKeychain {
            try store.set("v", for: .newsApiKey, scope: .shared)
            try store.set("", for: .newsApiKey, scope: .shared)
            #expect(try store.string(for: .newsApiKey, scope: .shared) == nil)
        }
    }

    @Test func flatMigrationMovesSharedAndUkItemsAndIsIdempotent() {
        let (store, service) = Self.makeStore()
        defer { Self.cleanUp(store) }
        Self.requireKeychain {
            try RawKeychain(service: service).set(Data("shared-flat".utf8), account: CredentialName.openaiApiKey.rawValue)
            try RawKeychain(service: service).set(Data("uk-flat".utf8), account: CredentialName.t212InvestKey.rawValue)
            let approval = Data(repeating: 9, count: 32)
            try RawKeychain(service: service).set(approval, account: CredentialName.approvalSigningKey.rawValue)

            let failures = store.migrateFlatItemsToScoped()
            #expect(failures.isEmpty)

            #expect(try store.string(for: .openaiApiKey, scope: .shared) == "shared-flat")
            #expect(try store.string(for: .t212InvestKey, scope: .workspace(.uk)) == "uk-flat")
            #expect(try store.legacyFlatData(for: .openaiApiKey) == nil)
            #expect(try store.legacyFlatData(for: .t212InvestKey) == nil)

            // The approval key is not moved by this migration.
            #expect(try store.legacyFlatData(for: .approvalSigningKey) == approval)
            #expect(try store.data(for: .approvalSigningKey, scope: .workspace(.uk)) == nil)

            // A second run changes nothing.
            #expect(store.migrateFlatItemsToScoped().isEmpty)
            #expect(try store.string(for: .openaiApiKey, scope: .shared) == "shared-flat")
            #expect(try store.string(for: .t212InvestKey, scope: .workspace(.uk)) == "uk-flat")
            #expect(try store.legacyFlatData(for: .approvalSigningKey) == approval)
        }
    }

    @Test func flatMigrationNeverOverwritesADifferingScopedValue() {
        let (store, service) = Self.makeStore()
        defer { Self.cleanUp(store) }
        Self.requireKeychain {
            try store.set("scoped-new", for: .tavilyApiKey, scope: .shared)
            try RawKeychain(service: service).set(Data("flat-old".utf8), account: CredentialName.tavilyApiKey.rawValue)

            let failures = store.migrateFlatItemsToScoped()
            #expect(failures == [.tavilyApiKey])
            #expect(try store.string(for: .tavilyApiKey, scope: .shared) == "scoped-new")
            #expect(try store.legacyFlatData(for: .tavilyApiKey) == Data("flat-old".utf8))
        }
    }

    @Test func userDefaultsMigrationWritesTheScopedAccountAndClearsTheKey() throws {
        let (store, _) = Self.makeStore()
        let suiteName = "san.Growin.tests.defaults.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer {
            Self.cleanUp(store)
            defaults.removePersistentDomain(forName: suiteName)
        }
        defaults.set("gem-legacy", forKey: "geminiApiKey")
        defaults.set("alp-legacy", forKey: "alpacaApiKey")

        let failures = store.migrateLegacyUserDefaults(defaults)
        #expect(failures.isEmpty)
        let gemini = try store.string(for: .geminiApiKey, scope: .shared)
        let alpaca = try store.string(for: .alpacaApiKey, scope: .workspace(.uk))
        #expect(gemini == "gem-legacy")
        #expect(alpaca == "alp-legacy")
        #expect(defaults.string(forKey: "geminiApiKey") == nil)
        #expect(defaults.string(forKey: "alpacaApiKey") == nil)
    }

    // MARK: Source probe

    /// Every .swift file under Growin/, as (repo-relative path, contents). Symlink-safe via SourceTree.
    private static func appSources() throws -> [(path: String, text: String)] {
        try SourceTree.swiftSources()
    }

    @Test func everyCredentialCallSiteNamesAScope() throws {
        let sources = try Self.appSources()
        #expect(sources.count > 10, "source probe found too few files")
        var unscoped: [String] = []
        for (path, text) in sources {
            for (index, line) in text.split(separator: "\n", omittingEmptySubsequences: false).enumerated() {
                let location = "\(path):\(index + 1)"
                if line.contains("@KeychainStorage(") && !line.contains("scope:") {
                    unscoped.append(location)
                }
                if line.contains("KeychainStore.shared.") && !line.contains("scope:") {
                    let isSigner = path == "Growin/Security/LocalApprovalSigner.swift"
                    let isLaunchMigration = path == "Growin/Security/LaunchMigrations.swift" && line.contains("migrate")
                    if !isSigner && !isLaunchMigration {
                        unscoped.append(location)
                    }
                }
            }
        }
        #expect(unscoped.isEmpty, "Unscoped Keychain call sites: \(unscoped)")
    }
}
