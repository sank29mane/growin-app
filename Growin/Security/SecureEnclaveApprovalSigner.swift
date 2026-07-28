import CryptoKit
import Foundation
import LocalAuthentication
import Security

enum ApprovalSignerError: LocalizedError {
    case unavailable
    case notConfigured
    case invalidPublicKey
    case status(OSStatus)
    case underlying(Error)

    var errorDescription: String? {
        switch self {
        case .unavailable:
            return "Secure Enclave Touch ID approval is unavailable on this Mac."
        case .notConfigured:
            return "Set up Touch ID trade approval in Settings first."
        case .invalidPublicKey:
            return "The approval public key could not be exported."
        case .status(let status):
            if status == errSecMissingEntitlement {
                return "Growin must be signed with an Apple development team before Secure Enclave approval can be enabled."
            }
            return SecCopyErrorMessageString(status, nil) as String? ?? "Secure Enclave error \(status)."
        case .underlying(let error):
            let nsError = error as NSError
            if nsError.code == Int(errSecMissingEntitlement) {
                return "Growin must be signed with an Apple development team before Secure Enclave approval can be enabled."
            }
            return error.localizedDescription
        }
    }
}

struct ApprovalSignerIdentity: Equatable, Sendable {
    let keyID: String
    let publicKeyX963: Data
}

final class SecureEnclaveApprovalSigner: @unchecked Sendable {
    static let shared = SecureEnclaveApprovalSigner()

    private let keyTag = Data("san.Growin.approval-key.v1".utf8)

    private init() {}

    var isConfigured: Bool {
        (try? loadPrivateKey(context: nil)) != nil
    }

    /// Key creation is intentionally separate from signing. Call this only from
    /// the explicit enrollment action; approval never regenerates a missing key.
    func createIdentityIfNeeded() throws -> ApprovalSignerIdentity {
        if let existingKey = try loadPrivateKey(context: nil) {
            return try identity(for: existingKey)
        }

        var accessError: Unmanaged<CFError>?
        guard let access = SecAccessControlCreateWithFlags(
            nil,
            kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            [.privateKeyUsage, .biometryCurrentSet],
            &accessError
        ) else {
            throw ApprovalSignerError.underlying(accessError!.takeRetainedValue() as Error)
        }

        let attributes: [String: Any] = [
            kSecAttrKeyType as String: kSecAttrKeyTypeECSECPrimeRandom,
            kSecAttrKeySizeInBits as String: 256,
            kSecAttrTokenID as String: kSecAttrTokenIDSecureEnclave,
            kSecPrivateKeyAttrs as String: [
                kSecAttrIsPermanent as String: true,
                kSecAttrApplicationTag as String: keyTag,
                kSecAttrAccessControl as String: access,
            ],
        ]

        var createError: Unmanaged<CFError>?
        guard let privateKey = SecKeyCreateRandomKey(attributes as CFDictionary, &createError) else {
            if let error = createError?.takeRetainedValue() {
                throw ApprovalSignerError.underlying(error as Error)
            }
            throw ApprovalSignerError.unavailable
        }
        return try identity(for: privateKey)
    }

    func identity() throws -> ApprovalSignerIdentity {
        guard let privateKey = try loadPrivateKey(context: nil) else {
            throw ApprovalSignerError.notConfigured
        }
        return try identity(for: privateKey)
    }

    /// Signs the exact bytes supplied by the server. The caller must display and
    /// verify the decoded challenge before invoking this method.
    func sign(_ payload: Data, reason: String) throws -> Data {
        let context = LAContext()
        context.localizedReason = reason
        context.touchIDAuthenticationAllowableReuseDuration = 0

        guard let privateKey = try loadPrivateKey(context: context) else {
            throw ApprovalSignerError.notConfigured
        }
        var signError: Unmanaged<CFError>?
        guard let signature = SecKeyCreateSignature(
            privateKey,
            .ecdsaSignatureMessageX962SHA256,
            payload as CFData,
            &signError
        ) as Data? else {
            if let error = signError?.takeRetainedValue() {
                throw ApprovalSignerError.underlying(error as Error)
            }
            throw ApprovalSignerError.unavailable
        }
        return signature
    }

    private func loadPrivateKey(context: LAContext?) throws -> SecKey? {
        var query: [String: Any] = [
            kSecClass as String: kSecClassKey,
            kSecAttrKeyType as String: kSecAttrKeyTypeECSECPrimeRandom,
            kSecAttrApplicationTag as String: keyTag,
            kSecReturnRef as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        if let context {
            query[kSecUseAuthenticationContext as String] = context
        } else {
            let noninteractiveContext = LAContext()
            noninteractiveContext.interactionNotAllowed = true
            query[kSecUseAuthenticationContext as String] = noninteractiveContext
        }

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound || status == errSecInteractionNotAllowed {
            return nil
        }
        guard status == errSecSuccess else {
            throw ApprovalSignerError.status(status)
        }
        return item as! SecKey?
    }

    private func identity(for privateKey: SecKey) throws -> ApprovalSignerIdentity {
        guard let publicKey = SecKeyCopyPublicKey(privateKey) else {
            throw ApprovalSignerError.invalidPublicKey
        }
        var exportError: Unmanaged<CFError>?
        guard let external = SecKeyCopyExternalRepresentation(publicKey, &exportError) as Data? else {
            if let error = exportError?.takeRetainedValue() {
                throw ApprovalSignerError.underlying(error as Error)
            }
            throw ApprovalSignerError.invalidPublicKey
        }
        guard external.count == 65, external.first == 0x04 else {
            throw ApprovalSignerError.invalidPublicKey
        }
        let digest = SHA256.hash(data: external)
        let keyID = digest.map { String(format: "%02x", $0) }.joined()
        return ApprovalSignerIdentity(keyID: keyID, publicKeyX963: external)
    }
}
