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
        #expect(CredentialName.allCases.count == shared.count + uk.count + 1)
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
