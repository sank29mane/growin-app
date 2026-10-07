import Foundation

/// The two execution workspaces. There is deliberately no default value: every
/// caller names one explicitly.
nonisolated enum Workspace: String, CaseIterable, Codable, Sendable {
    case uk
    case india

    var displayName: String {
        switch self {
        case .uk: return "UK"
        case .india: return "India"
        }
    }
}

/// Where a Keychain item lives. The prefix becomes part of the Keychain account,
/// and generic-password uniqueness is service plus account, so it is a namespace.
nonisolated enum KeychainScope: Equatable, Sendable {
    case shared
    case workspace(Workspace)

    var accountPrefix: String {
        switch self {
        case .shared: return "shared"
        case .workspace(let workspace): return workspace.rawValue
        }
    }
}

nonisolated enum CredentialPolicy: Equatable, Sendable {
    case shared
    case fixed(Workspace)
    case perWorkspace

    func allows(_ scope: KeychainScope) -> Bool {
        switch self {
        case .shared:
            return scope == .shared
        case .fixed(let required):
            return scope == .workspace(required)
        case .perWorkspace:
            if case .workspace = scope { return true }
            return false
        }
    }
}

/// Every Keychain credential the app stores. Raw values are the legacy flat
/// account names.
nonisolated enum CredentialName: String, CaseIterable, Sendable {
    case openaiApiKey
    case geminiApiKey
    case finnhubApiKey
    case trading212IsaApiKey
    case trading212IsaApiSecret
    case alpacaApiKey
    case alpacaSecretKey
    case newsApiKey
    case tavilyApiKey
    case t212InvestKey
    case t212InvestSecret
    case t212IsaKey
    case t212IsaSecret
    case approvalSigningKey = "approvalSoftwareP256PrivateKey.v1"

    /// Operator decision 2026-10-05 (research-a2): LLM and news keys are shared,
    /// Trading 212 and Alpaca keys belong to UK, the approval key is per
    /// workspace (decision 3). The switch is exhaustive on purpose: a new
    /// credential cannot compile until someone classifies it.
    var policy: CredentialPolicy {
        switch self {
        case .openaiApiKey,
             .geminiApiKey,
             .finnhubApiKey,
             .newsApiKey,
             .tavilyApiKey:
            return .shared
        case .trading212IsaApiKey,
             .trading212IsaApiSecret,
             .t212InvestKey,
             .t212InvestSecret,
             .t212IsaKey,
             .t212IsaSecret,
             .alpacaApiKey,
             .alpacaSecretKey:
            return .fixed(.uk)
        case .approvalSigningKey:
            return .perWorkspace
        }
    }

    /// The scope a fixed or shared credential lives in. Per-workspace
    /// credentials have no single scope, so this is nil for them.
    var fixedScope: KeychainScope? {
        switch policy {
        case .shared: return .shared
        case .fixed(let workspace): return .workspace(workspace)
        case .perWorkspace: return nil
        }
    }
}

/// The workspace the operator picked in Settings. Nothing is preselected: the
/// value is absent until the operator chooses one.
nonisolated enum WorkspaceSelection {
    static let defaultsKey = "selectedWorkspace"

    static func current(_ defaults: UserDefaults = .standard) -> Workspace? {
        defaults.string(forKey: defaultsKey).flatMap(Workspace.init(rawValue:))
    }

    static func set(_ workspace: Workspace?, _ defaults: UserDefaults = .standard) {
        if let workspace {
            defaults.set(workspace.rawValue, forKey: defaultsKey)
        } else {
            defaults.removeObject(forKey: defaultsKey)
        }
    }
}
