import CryptoKit
import Foundation

enum LocalApprovalSignerError: LocalizedError {
    case notConfigured
    case invalidStoredKey

    var errorDescription: String? {
        switch self {
        case .notConfigured:
            return "Set up local paper-trade approval in Settings first."
        case .invalidStoredKey:
            return "The local approval key in Keychain is invalid. Re-enrollment is required."
        }
    }
}

/// Software P-256 signer for bootstrapped local development.
///
/// The private key is stored as a device-bound, when-unlocked generic Keychain
/// item. It is never included in logs or API calls.
/// This provides signed, replay-resistant paper approvals without requiring an
/// Apple Developer identity, but it does not provide Secure Enclave isolation.
final class LocalApprovalSigner: @unchecked Sendable {
    static let shared = LocalApprovalSigner()

    private let keychainAccount = "approvalSoftwareP256PrivateKey.v1"

    private init() {}

    var isConfigured: Bool {
        (try? identity()) != nil
    }

    /// Creates the local key only during explicit enrollment. Approval never
    /// regenerates a missing or invalid key because that would change identity.
    func createIdentityIfNeeded() throws -> ApprovalSignerIdentity {
        if let rawKey = try KeychainStore.shared.data(for: keychainAccount) {
            let privateKey = try decodePrivateKey(rawKey)
            return identity(for: privateKey)
        }

        let privateKey = P256.Signing.PrivateKey()
        try KeychainStore.shared.set(privateKey.rawRepresentation, for: keychainAccount)
        return identity(for: privateKey)
    }

    func identity() throws -> ApprovalSignerIdentity {
        guard let rawKey = try KeychainStore.shared.data(for: keychainAccount) else {
            throw LocalApprovalSignerError.notConfigured
        }
        return identity(for: try decodePrivateKey(rawKey))
    }

    /// Signs the exact canonical bytes supplied and reviewed by the caller.
    func sign(_ payload: Data) throws -> Data {
        guard let rawKey = try KeychainStore.shared.data(for: keychainAccount) else {
            throw LocalApprovalSignerError.notConfigured
        }
        let privateKey = try decodePrivateKey(rawKey)
        return try privateKey.signature(for: payload).derRepresentation
    }

    private func decodePrivateKey(_ rawKey: Data) throws -> P256.Signing.PrivateKey {
        guard let privateKey = try? P256.Signing.PrivateKey(rawRepresentation: rawKey) else {
            throw LocalApprovalSignerError.invalidStoredKey
        }
        return privateKey
    }

    private func identity(for privateKey: P256.Signing.PrivateKey) -> ApprovalSignerIdentity {
        let publicKey = privateKey.publicKey.x963Representation
        let digest = SHA256.hash(data: publicKey)
        let keyID = digest.map { String(format: "%02x", $0) }.joined()
        return ApprovalSignerIdentity(keyID: keyID, publicKeyX963: publicKey)
    }
}
