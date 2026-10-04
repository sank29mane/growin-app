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

    func isConfigured(for workspace: Workspace) -> Bool {
        (try? identity(for: workspace)) != nil
    }

    /// Creates the workspace key only during explicit enrollment. Approval never
    /// regenerates a missing or invalid key because that would change identity.
    func createIdentityIfNeeded(for workspace: Workspace) throws -> ApprovalSignerIdentity {
        if let rawKey = try storedKey(for: workspace) {
            return makeIdentity(try decodePrivateKey(rawKey))
        }

        let privateKey = P256.Signing.PrivateKey()
        try store.set(privateKey.rawRepresentation, for: .approvalSigningKey, scope: .workspace(workspace))
        return makeIdentity(privateKey)
    }

    func identity(for workspace: Workspace) throws -> ApprovalSignerIdentity {
        guard let rawKey = try storedKey(for: workspace) else {
            throw LocalApprovalSignerError.notConfigured
        }
        return makeIdentity(try decodePrivateKey(rawKey))
    }

    /// Signs the exact canonical bytes supplied and reviewed by the caller.
    func sign(_ payload: Data, for workspace: Workspace) throws -> Data {
        guard let rawKey = try storedKey(for: workspace) else {
            throw LocalApprovalSignerError.notConfigured
        }
        let privateKey = try decodePrivateKey(rawKey)
        return try privateKey.signature(for: payload).derRepresentation
    }

    // MARK: Legacy key adoption

    func hasLegacyFlatKey() -> Bool {
        ((try? store.legacyFlatData(for: .approvalSigningKey)) ?? nil) != nil
    }

    /// True while a flat key remains and the workspace either has no key yet or
    /// already holds the same bytes (an adoption whose flat delete failed).
    func canAdoptLegacyKey(into workspace: Workspace) -> Bool {
        guard workspace == .uk,
              let flat = (try? store.legacyFlatData(for: .approvalSigningKey)) ?? nil else {
            return false
        }
        guard let existing = (try? store.data(for: .approvalSigningKey, scope: .workspace(workspace))) ?? nil else {
            return true
        }
        return existing == flat
    }

    /// Moves the pre-58 flat approval key into UK, and only into UK. The key must
    /// be the one the UK ledger enrolled (`expectedKeyID`), because the ledger
    /// cannot rotate an enrolled key. India always starts with a fresh key.
    /// Copy, read back, compare, then delete the flat item.
    func adoptLegacyKey(into workspace: Workspace, expectedKeyID: String) throws -> ApprovalSignerIdentity {
        guard workspace == .uk else {
            throw LocalApprovalSignerError.legacyAdoptionNotAllowed
        }
        guard let flat = try store.legacyFlatData(for: .approvalSigningKey) else {
            throw LocalApprovalSignerError.noLegacyKey
        }
        let identity = makeIdentity(try decodePrivateKey(flat))
        guard identity.keyID == expectedKeyID else {
            throw LocalApprovalSignerError.legacyKeyMismatch
        }
        for other in Workspace.allCases where other != workspace {
            if try store.data(for: .approvalSigningKey, scope: .workspace(other)) == flat {
                throw LocalApprovalSignerError.duplicateKeyAcrossWorkspaces
            }
        }
        if let existing = try store.data(for: .approvalSigningKey, scope: .workspace(workspace)) {
            // A previous adoption copied the key but failed to delete the flat
            // item. Finish that cleanup; a different workspace key is refused.
            guard existing == flat else {
                throw LocalApprovalSignerError.workspaceKeyExists
            }
            try store.removeLegacyFlatItem(.approvalSigningKey)
            return identity
        }
        try store.set(flat, for: .approvalSigningKey, scope: .workspace(workspace))
        guard try store.data(for: .approvalSigningKey, scope: .workspace(workspace)) == flat else {
            throw KeychainStoreError.unexpectedData
        }
        try store.removeLegacyFlatItem(.approvalSigningKey)
        return identity
    }

    // MARK: Helpers

    /// Reads a workspace key and refuses it when another workspace holds the same bytes.
    private func storedKey(for workspace: Workspace) throws -> Data? {
        guard let rawKey = try store.data(for: .approvalSigningKey, scope: .workspace(workspace)) else {
            return nil
        }
        for other in Workspace.allCases where other != workspace {
            if try store.data(for: .approvalSigningKey, scope: .workspace(other)) == rawKey {
                throw LocalApprovalSignerError.duplicateKeyAcrossWorkspaces
            }
        }
        return rawKey
    }

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
