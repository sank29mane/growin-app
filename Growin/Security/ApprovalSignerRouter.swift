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
    case softwareFlowNotAllowed
    case softwarePayloadNotCanonical
    case softwareModeNotPaper
    case indiaLedgerKeyMismatch

    var errorDescription: String? {
        switch self {
        case .relayIsIndiaOnly:
            return "Relay orders are signed in the India workspace only."
        case .softwareFlowNotAllowed:
            return "The software approval key only signs paper approvals. Nothing was signed."
        case .softwarePayloadNotCanonical:
            return "The bytes to sign are not an exact paper approval, so the software key refused to sign them. Nothing was signed."
        case .softwareModeNotPaper:
            return "The software approval key only signs PAPER mode approvals. Nothing was signed."
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
            // UK behaves as before for real paper approvals. Relay orders do not exist for UK.
            try requireSoftwareSignable(payload, for: workspace, flow: flow)
            return try uk.sign(payload, for: workspace)
        }
    }

    /// Same as `sign`, with the blocking Touch ID call kept off the main actor.
    func signAsync(_ payload: Data, for workspace: Workspace, flow: ApprovalSigningFlow) async throws -> Data {
        switch route(for: workspace) {
        case .secureEnclave:
            return try await india.signAsync(payload, flow: flow, for: workspace)
        case .localSoftware:
            try requireSoftwareSignable(payload, for: workspace, flow: flow)
            return try uk.sign(payload, for: workspace)
        }
    }

    /// The software key has no Touch ID, so the router cannot trust the caller's flow
    /// label (P2 on #558). The signed bytes decide: they must parse, carry the paper
    /// approval purpose, and name this workspace. Relay bytes never get through,
    /// whatever label they arrive under. Runs before the software key is read.
    private func requireSoftwareSignable(_ payload: Data, for workspace: Workspace, flow: ApprovalSigningFlow) throws {
        guard flow == .paperApproval else {
            throw flow == .relayOrder ? ApprovalSignerRouterError.relayIsIndiaOnly : ApprovalSignerRouterError.softwareFlowNotAllowed
        }
        // Unparseable bytes throw here, before the key is touched.
        let inspected = try SignedPayloadInspector.inspect(payload)
        guard inspected.purpose != .relayOrder else {
            throw ApprovalSignerRouterError.relayIsIndiaOnly
        }
        guard inspected.purpose == flow.allowedPurpose else {
            throw ApprovalSignerError.purposeNotAllowed
        }
        guard inspected.workspace == workspace.rawValue else {
            throw ApprovalSignerError.workspaceMismatch
        }
        try requireCanonicalPaperApproval(payload)
    }

    /// Exact key set of the backend paper approval (backend/execution/approval.py,
    /// `payload = {...}` in the dispatch challenge). The software gate is an allowlist:
    /// one key more or less and the key stays unused. `SignedPayloadInspector` is
    /// deliberately lenient about extra keys, so it cannot be the gate on its own.
    private static let canonicalPaperKeys: Set<String> = [
        "version", "purpose", "challenge_id", "proposal_id", "client_order_id", "intent_hash",
        "workspace", "account", "broker", "mode", "ticker", "side", "quantity", "order_type",
        "limit_price", "replaces_proposal_id", "requote_id", "admitted_quantity", "currency",
        "price", "notional", "evidence_hash", "nonce", "issued_at", "expires_at", "key_id",
    ]

    /// Keys that only exist on India relay envelopes (growin-orders/1 O4). Seeing one
    /// anywhere, at any depth, is a relay order in a paper costume.
    private static let relayOnlyKeys: Set<String> = [
        "limits_sha256", "params_sha256", "intent", "intent_id", "isin", "stock_code",
        "exchange", "product", "validity", "batch_id",
    ]

    private static func containsRelayKey(_ value: StrictJSONValue) -> Bool {
        switch value {
        case .object(let object):
            return object.contains { relayOnlyKeys.contains($0.key) || Self.containsRelayKey($0.value) }
        case .array(let items):
            return items.contains(where: Self.containsRelayKey)
        default:
            return false
        }
    }

    /// Strict allowlist for the UK software key: exact canonical paper schema, flat
    /// scalar values only, mode PAPER. Refuses PRACTICE and LIVE on this branch.
    /// NOTE for rebase: PR #557 adds a separate biometric-gated practice path. Practice
    /// signing belongs there, behind Touch ID, and is reconciled when #557 lands. It must
    /// not be opened up here by widening this software gate.
    private func requireCanonicalPaperApproval(_ payload: Data) throws {
        guard case .object(let object) = try StrictJSONParser.parse(payload) else {
            throw SignedPayloadInspectionError.malformed
        }
        for value in object.values where Self.containsRelayKey(value) {
            throw ApprovalSignerRouterError.relayIsIndiaOnly
        }
        if !Set(object.keys).isDisjoint(with: Self.relayOnlyKeys) {
            throw ApprovalSignerRouterError.relayIsIndiaOnly
        }
        guard Set(object.keys) == Self.canonicalPaperKeys else {
            throw ApprovalSignerRouterError.softwarePayloadNotCanonical
        }
        for value in object.values {
            switch value {
            case .string, .integer, .null: continue
            default: throw ApprovalSignerRouterError.softwarePayloadNotCanonical
            }
        }
        guard case .string("PAPER")? = object["mode"] else {
            throw ApprovalSignerRouterError.softwareModeNotPaper
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
