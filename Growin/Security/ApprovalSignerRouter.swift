import Foundation

/// Which kind of signer serves a workspace.
enum ApprovalSignerRoute: Equatable, Sendable {
    /// India: CryptoKit Secure Enclave key, Touch ID per signature.
    case secureEnclave
    /// UK: the existing software key. Never used for India.
    case localSoftware
}

enum ApprovalSignerRouterError: LocalizedError, Equatable {
    case relayIsIndiaOnly
    case indiaLedgerKeyMismatch

    var errorDescription: String? {
        switch self {
        case .relayIsIndiaOnly:
            return "Relay orders are signed in the India workspace only."
        case .indiaLedgerKeyMismatch:
            return "The India ledger already has a different approval key enrolled. Keys cannot be replaced, so India needs a fresh ledger path before this Secure Enclave key can be enrolled. Ask for the fresh-ledger steps in the runbook; nothing was changed."
        }
    }
}

/// What Settings should do after the backend reports its enrolled key for a workspace.
enum ApprovalEnrolmentDecision: Equatable, Sendable {
    case enrol
    case alreadyEnrolled
    /// India has a key from before the Secure Enclave switch (P-22). Do not adopt it.
    case freshLedgerRequired
    case signerMismatch
}

/// The one door every approval signature goes through. India always routes to the
/// Secure Enclave signer and UK keeps the software signer. There is no switch,
/// setting or fallback that sends India to a software key.
final class ApprovalSignerRouter: @unchecked Sendable {
    static let shared = ApprovalSignerRouter(
        india: .shared,
        uk: .shared
    )

    private let india: SecureEnclaveApprovalSigner
    private let uk: LocalApprovalSigner

    init(india: SecureEnclaveApprovalSigner, uk: LocalApprovalSigner) {
        self.india = india
        self.uk = uk
    }

    func route(for workspace: Workspace) -> ApprovalSignerRoute {
        switch workspace {
        case .india: return .secureEnclave
        case .uk: return .localSoftware
        }
    }

    func isConfigured(for workspace: Workspace) -> Bool {
        switch route(for: workspace) {
        case .secureEnclave: return india.isConfigured(for: workspace)
        case .localSoftware: return uk.isConfigured(for: workspace)
        }
    }

    func createIdentityIfNeeded(for workspace: Workspace) throws -> ApprovalSignerIdentity {
        switch route(for: workspace) {
        case .secureEnclave: return try india.createIdentityIfNeeded(for: workspace)
        case .localSoftware: return try uk.createIdentityIfNeeded(for: workspace)
        }
    }

    func identity(for workspace: Workspace) throws -> ApprovalSignerIdentity {
        switch route(for: workspace) {
        case .secureEnclave: return try india.identity(for: workspace)
        case .localSoftware: return try uk.identity(for: workspace)
        }
    }

    /// Signs on the calling thread (blocks on Touch ID for India).
    func sign(_ payload: Data, for workspace: Workspace, flow: ApprovalSigningFlow) throws -> Data {
        switch route(for: workspace) {
        case .secureEnclave:
            return try india.sign(payload, flow: flow, for: workspace)
        case .localSoftware:
            // UK behaves exactly as before. Relay orders do not exist for UK.
            guard flow != .relayOrder else { throw ApprovalSignerRouterError.relayIsIndiaOnly }
            return try uk.sign(payload, for: workspace)
        }
    }

    /// Same as `sign`, with the blocking Touch ID call kept off the main actor.
    func signAsync(_ payload: Data, for workspace: Workspace, flow: ApprovalSigningFlow) async throws -> Data {
        switch route(for: workspace) {
        case .secureEnclave:
            return try await india.signAsync(payload, flow: flow, for: workspace)
        case .localSoftware:
            guard flow != .relayOrder else { throw ApprovalSignerRouterError.relayIsIndiaOnly }
            return try uk.sign(payload, for: workspace)
        }
    }

    // MARK: Legacy adoption (UK only; India always starts with a fresh Secure Enclave key)

    func canAdoptLegacyKey(into workspace: Workspace) -> Bool {
        route(for: workspace) == .localSoftware && uk.canAdoptLegacyKey(into: workspace)
    }

    func adoptLegacyKey(into workspace: Workspace, expectedKeyID: String) throws -> ApprovalSignerIdentity {
        try uk.adoptLegacyKey(into: workspace, expectedKeyID: expectedKeyID)
    }

    // MARK: Enrolment

    /// P-22: keys cannot rotate in a ledger, so an India ledger that already holds a
    /// different key (for example the old software key) is never adopted.
    static func enrolmentDecision(
        workspace: Workspace,
        localKeyID: String,
        backendEnrolled: Bool,
        backendKeyID: String?
    ) -> ApprovalEnrolmentDecision {
        guard backendEnrolled else { return .enrol }
        if backendKeyID == localKeyID { return .alreadyEnrolled }
        return workspace == .india ? .freshLedgerRequired : .signerMismatch
    }
}
