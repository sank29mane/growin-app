import CryptoKit
import Foundation
import LocalAuthentication
import Security

enum ApprovalSignerError: LocalizedError {
    case unavailable
    case notConfigured
    case indiaOnly
    case invalidKeyBlob
    case keyMismatch
    case purposeNotAllowed
    case workspaceMismatch
    case modeNotAllowed
    case invalidSignatureProduced
    case deviceLocked
    case underlying(Error)

    var errorDescription: String? {
        switch self {
        case .unavailable:
            return "Secure Enclave Touch ID approval is unavailable on this Mac."
        case .notConfigured:
            return "Set up India Touch ID approval in Settings first."
        case .indiaOnly:
            return "The Secure Enclave approval key is only used for the India workspace."
        case .invalidKeyBlob:
            return "The India approval key in Keychain is invalid. Re-enrolment is required."
        case .keyMismatch:
            return "The bytes to sign name a different key than this Mac's India approval key. Nothing was signed."
        case .purposeNotAllowed:
            return "The bytes to sign are for a different kind of approval than this screen. Nothing was signed."
        case .workspaceMismatch:
            return "The bytes to sign are for a different workspace. Nothing was signed."
        case .modeNotAllowed:
            return "The bytes to sign are for an order mode this screen does not approve. Nothing was signed."
        case .invalidSignatureProduced:
            return "The Secure Enclave returned a signature that does not verify. Nothing was sent."
        case .deviceLocked:
            return "The Secure Enclave would not create the key. Unlock this Mac and try again."
        case .underlying(let error):
            return error.localizedDescription
        }
    }
}

nonisolated struct ApprovalSignerIdentity: Equatable, Sendable {
    let keyID: String
    let publicKeyX963: Data

    /// key_id is the lowercase hex sha256 of the 65-byte X9.63 public key.
    init(publicKeyX963: Data) {
        self.publicKeyX963 = publicKeyX963
        self.keyID = SHA256.hash(data: publicKeyX963).map { String(format: "%02x", $0) }.joined()
    }

    init(keyID: String, publicKeyX963: Data) {
        self.keyID = keyID
        self.publicKeyX963 = publicKeyX963
    }
}

/// What a given screen is allowed to sign. Each flow accepts exactly one purpose,
/// so paper bytes cannot be signed on the relay screen and the reverse (T-63-17).
nonisolated enum ApprovalSigningFlow: Equatable, Sendable {
    case paperApproval
    case controlClear
    case relayOrder

    var allowedPurpose: SignedPurpose {
        switch self {
        case .paperApproval: return .paperDispatch
        case .controlClear: return .controlClear
        case .relayOrder: return .relayOrder
        }
    }
}

// MARK: - Key backend (the seam tests inject into)

/// One P-256 key held as an opaque blob. The app ships only the Secure Enclave
/// implementation. A software implementation exists only in the test target.
nonisolated protocol ApprovalKeyBackend: Sendable {
    /// Makes a new key and returns the blob to store. Never prompts.
    func createKeyBlob() throws -> Data
    /// The public key for a stored blob. Never prompts.
    func publicKeyX963(blob: Data) throws -> Data
    /// DER ECDSA/SHA-256 over `payload`. The Secure Enclave evaluates the key's
    /// access control through `context`, which is where Touch ID is asked.
    func signDER(blob: Data, payload: Data, context: LAContext) throws -> Data
}

/// CryptoKit Secure Enclave key, access control `[.privateKeyUsage, .biometryCurrentSet]`
/// (D-01). The blob is only usable by this Mac's Secure Enclave; no SecKey
/// permanent key and no Apple team are involved.
nonisolated struct SecureEnclaveKeyBackend: ApprovalKeyBackend {
    func createKeyBlob() throws -> Data {
        guard SecureEnclave.isAvailable else {
            throw ApprovalSignerError.unavailable
        }
        var accessError: Unmanaged<CFError>?
        guard let access = SecAccessControlCreateWithFlags(
            nil,
            kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
            [.privateKeyUsage, .biometryCurrentSet],
            &accessError
        ) else {
            throw ApprovalSignerError.underlying(
                accessError?.takeRetainedValue() ?? ApprovalSignerError.unavailable
            )
        }
        do {
            let key = try SecureEnclave.P256.Signing.PrivateKey(
                compactRepresentable: false,
                accessControl: access
            )
            return key.dataRepresentation
        } catch {
            // errSecInteractionNotAllowed: the key class is "when unlocked", so a
            // locked Mac cannot create it. Say so instead of a raw OSStatus.
            if (error as NSError).code == Int(errSecInteractionNotAllowed) {
                throw ApprovalSignerError.deviceLocked
            }
            throw ApprovalSignerError.underlying(error)
        }
    }

    func publicKeyX963(blob: Data) throws -> Data {
        guard SecureEnclave.isAvailable else {
            throw ApprovalSignerError.unavailable
        }
        do {
            return try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: blob)
                .publicKey.x963Representation
        } catch {
            throw ApprovalSignerError.invalidKeyBlob
        }
    }

    func signDER(blob: Data, payload: Data, context: LAContext) throws -> Data {
        do {
            let key = try SecureEnclave.P256.Signing.PrivateKey(
                dataRepresentation: blob,
                authenticationContext: context
            )
            return try key.signature(for: payload).derRepresentation
        } catch {
            throw ApprovalSignerError.underlying(error)
        }
    }
}

// MARK: - Authentication context (the injected authenticator)

/// Supplies the LAContext the Secure Enclave evaluates for one signature.
/// Injected so tests can prove "fresh context, no reuse window, reason names the
/// order" without a fingerprint. A later change can feed this from the practice
/// approval authenticator so both flows share one prompt path.
nonisolated protocol ApprovalAuthContextProviding: Sendable {
    func makeContext(reason: String) -> LAContext
}

/// A new LAContext per call with no authentication reuse window (D-06, P-19).
/// Touch ID only: the fallback button is hidden and `.biometryCurrentSet` on the
/// key refuses a passcode anyway.
nonisolated struct FreshTouchIDContextProvider: ApprovalAuthContextProviding {
    func makeContext(reason: String) -> LAContext {
        let context = LAContext()
        context.localizedReason = reason
        context.touchIDAuthenticationAllowableReuseDuration = 0
        context.localizedFallbackTitle = ""
        return context
    }
}

// MARK: - Signer

/// Everything needed to sign, resolved and checked before any prompt.
nonisolated struct PreparedApprovalSignature: @unchecked Sendable {
    let blob: Data
    let payload: Data
    let context: LAContext
    let publicKeyX963: Data
}

/// India approval signer: one Secure Enclave key, Touch ID for every signature.
/// It signs the exact bytes supplied, after parsing them itself and refusing a
/// purpose, workspace, mode or key_id that does not fit the flow.
final class SecureEnclaveApprovalSigner: @unchecked Sendable {
    static let shared = SecureEnclaveApprovalSigner(
        store: .shared,
        backend: SecureEnclaveKeyBackend(),
        contexts: FreshTouchIDContextProvider()
    )

    private let store: KeychainStore
    private let backend: any ApprovalKeyBackend
    private let contexts: any ApprovalAuthContextProviding

    init(
        store: KeychainStore,
        backend: any ApprovalKeyBackend,
        contexts: any ApprovalAuthContextProviding
    ) {
        self.store = store
        self.backend = backend
        self.contexts = contexts
    }

    func isConfigured(for workspace: Workspace) -> Bool {
        (try? identity(for: workspace)) != nil
    }

    /// Key creation is separate from signing. Call this only from the explicit
    /// enrolment action; approval never regenerates a missing key. Creating a
    /// Secure Enclave key does not prompt.
    func createIdentityIfNeeded(for workspace: Workspace) throws -> ApprovalSignerIdentity {
        try requireIndia(workspace)
        if let blob = try storedBlob() {
            return try identity(fromBlob: blob)
        }
        let blob = try backend.createKeyBlob()
        let created = try identity(fromBlob: blob)
        try store.set(blob, for: .approvalSecureEnclaveKey, scope: .workspace(.india))
        guard try store.data(for: .approvalSecureEnclaveKey, scope: .workspace(.india)) == blob else {
            throw KeychainStoreError.unexpectedData
        }
        return created
    }

    func identity(for workspace: Workspace) throws -> ApprovalSignerIdentity {
        try requireIndia(workspace)
        guard let blob = try storedBlob() else {
            throw ApprovalSignerError.notConfigured
        }
        return try identity(fromBlob: blob)
    }

    /// Checks everything that can be checked without the key prompting, then
    /// builds a fresh context. Any refusal happens here, before Touch ID.
    func prepare(_ payload: Data, flow: ApprovalSigningFlow, for workspace: Workspace) throws -> PreparedApprovalSignature {
        try requireIndia(workspace)
        // Parse first: a bad document never reaches the key.
        let inspected = try SignedPayloadInspector.inspect(payload)
        guard inspected.purpose == flow.allowedPurpose else {
            throw ApprovalSignerError.purposeNotAllowed
        }
        guard inspected.workspace == workspace.rawValue else {
            throw ApprovalSignerError.workspaceMismatch
        }
        if case .paperDispatch(let paper) = inspected, paper.mode != "PAPER" {
            throw ApprovalSignerError.modeNotAllowed
        }
        guard let blob = try storedBlob() else {
            throw ApprovalSignerError.notConfigured
        }
        let identity = try identity(fromBlob: blob)
        guard inspected.keyID == identity.keyID else {
            throw ApprovalSignerError.keyMismatch
        }
        let context = contexts.makeContext(reason: inspected.touchPrompt)
        return PreparedApprovalSignature(
            blob: blob,
            payload: payload,
            context: context,
            publicKeyX963: identity.publicKeyX963
        )
    }

    /// Signs on the calling thread. The Touch ID prompt blocks until answered.
    func sign(_ payload: Data, flow: ApprovalSigningFlow, for workspace: Workspace) throws -> Data {
        let prepared = try prepare(payload, flow: flow, for: workspace)
        return try Self.finish(prepared, backend: backend)
    }

    /// Same checks, same single prompt, but the blocking call runs off the main actor.
    func signAsync(_ payload: Data, flow: ApprovalSigningFlow, for workspace: Workspace) async throws -> Data {
        let prepared = try prepare(payload, flow: flow, for: workspace)
        let backend = self.backend
        return try await Task.detached(priority: .userInitiated) {
            try Self.finish(prepared, backend: backend)
        }.value
    }

    // MARK: Helpers

    nonisolated private static func finish(_ prepared: PreparedApprovalSignature, backend: any ApprovalKeyBackend) throws -> Data {
        let der = try backend.signDER(blob: prepared.blob, payload: prepared.payload, context: prepared.context)
        // Never hand back bytes that would not verify against the public key.
        guard let publicKey = try? P256.Signing.PublicKey(x963Representation: prepared.publicKeyX963),
              let signature = try? P256.Signing.ECDSASignature(derRepresentation: der),
              publicKey.isValidSignature(signature, for: prepared.payload) else {
            throw ApprovalSignerError.invalidSignatureProduced
        }
        return der
    }

    private func requireIndia(_ workspace: Workspace) throws {
        guard workspace == .india else {
            throw ApprovalSignerError.indiaOnly
        }
    }

    private func storedBlob() throws -> Data? {
        try store.data(for: .approvalSecureEnclaveKey, scope: .workspace(.india))
    }

    private func identity(fromBlob blob: Data) throws -> ApprovalSignerIdentity {
        let x963 = try backend.publicKeyX963(blob: blob)
        guard x963.count == 65, x963.first == 0x04 else {
            throw ApprovalSignerError.invalidKeyBlob
        }
        return ApprovalSignerIdentity(publicKeyX963: x963)
    }
}
