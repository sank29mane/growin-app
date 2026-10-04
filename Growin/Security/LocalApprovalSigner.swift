import CryptoKit
import Foundation

enum LocalApprovalSignerError: LocalizedError {
    case notConfigured
    case invalidStoredKey
    case duplicateKeyAcrossWorkspaces
    case legacyAdoptionNotAllowed
    case legacyKeyMismatch
    case workspaceKeyExists
    case noLegacyKey

    var errorDescription: String? {
        switch self {
        case .notConfigured:
            return "Set up local paper-trade approval in Settings first."
        case .invalidStoredKey:
            return "The local approval key in Keychain is invalid. Re-enrollment is required."
        case .duplicateKeyAcrossWorkspaces:
            return "The UK and India approval keys are identical. Each workspace needs its own key."
        case .legacyAdoptionNotAllowed:
            return "The existing local approval key can only be adopted by the UK workspace."
        case .legacyKeyMismatch:
            return "The existing local approval key does not match the key enrolled for this workspace."
        case .workspaceKeyExists:
            return "This workspace already has an approval key."
        case .noLegacyKey:
            return "There is no existing local approval key to adopt."
        }
    }
}

/// Software P-256 signer for bootstrapped local development.
///
/// Each workspace has its own private key, stored as a device-bound,
/// when-unlocked generic Keychain item in that workspace's scope. Keys are never
/// included in logs or API calls. This provides signed, replay-resistant paper
/// approvals without requiring an Apple Developer identity, but it does not
/// provide Secure Enclave isolation.
final class LocalApprovalSigner: @unchecked Sendable {
    static let shared = LocalApprovalSigner(store: .shared)

    private let store: KeychainStore

    init(store: KeychainStore) {
        self.store = store
    }

    // MARK: Per-workspace API

    func isConfigured(for workspace: Workspace) -> Bool {
        (try? identity(for: workspace)) != nil
    }

    /// Creates the workspace key only during explicit enrollment. Approval never
    /// regenerates a missing or invalid key because that would change identity.
    func createIdentityIfNeeded(for workspace: Workspace) throws -> ApprovalSignerIdentity {
        if let rawKey = try store.data(for: .approvalSigningKey, scope: .workspace(workspace)) {
            return makeIdentity(try decodePrivateKey(rawKey))
        }

        let privateKey = P256.Signing.PrivateKey()
        try store.set(privateKey.rawRepresentation, for: .approvalSigningKey, scope: .workspace(workspace))
        return makeIdentity(privateKey)
    }

    func identity(for workspace: Workspace) throws -> ApprovalSignerIdentity {
        guard let rawKey = try store.data(for: .approvalSigningKey, scope: .workspace(workspace)) else {
            throw LocalApprovalSignerError.notConfigured
        }
        return makeIdentity(try decodePrivateKey(rawKey))
    }

    /// Signs the exact canonical bytes supplied and reviewed by the caller.
    func sign(_ payload: Data, for workspace: Workspace) throws -> Data {
        guard let rawKey = try store.data(for: .approvalSigningKey, scope: .workspace(workspace)) else {
            throw LocalApprovalSignerError.notConfigured
        }
        let privateKey = try decodePrivateKey(rawKey)
        return try privateKey.signature(for: payload).derRepresentation
    }

    // MARK: Legacy no-workspace API (removed once Settings and Chat are converted)

    private let keychainAccount = "approvalSoftwareP256PrivateKey.v1"

    var isConfigured: Bool {
        (try? identity()) != nil
    }

    func createIdentityIfNeeded() throws -> ApprovalSignerIdentity {
        if let rawKey = try store.data(for: keychainAccount) {
            return makeIdentity(try decodePrivateKey(rawKey))
        }

        let privateKey = P256.Signing.PrivateKey()
        try store.set(privateKey.rawRepresentation, for: keychainAccount)
        return makeIdentity(privateKey)
    }

    func identity() throws -> ApprovalSignerIdentity {
        guard let rawKey = try store.data(for: keychainAccount) else {
            throw LocalApprovalSignerError.notConfigured
        }
        return makeIdentity(try decodePrivateKey(rawKey))
    }

    func sign(_ payload: Data) throws -> Data {
        guard let rawKey = try store.data(for: keychainAccount) else {
            throw LocalApprovalSignerError.notConfigured
        }
        let privateKey = try decodePrivateKey(rawKey)
        return try privateKey.signature(for: payload).derRepresentation
    }

    // MARK: Helpers

    private func decodePrivateKey(_ rawKey: Data) throws -> P256.Signing.PrivateKey {
        guard let privateKey = try? P256.Signing.PrivateKey(rawRepresentation: rawKey) else {
            throw LocalApprovalSignerError.invalidStoredKey
        }
        return privateKey
    }

    private func makeIdentity(_ privateKey: P256.Signing.PrivateKey) -> ApprovalSignerIdentity {
        let publicKey = privateKey.publicKey.x963Representation
        let digest = SHA256.hash(data: publicKey)
        let keyID = digest.map { String(format: "%02x", $0) }.joined()
        return ApprovalSignerIdentity(keyID: keyID, publicKeyX963: publicKey)
    }
}
