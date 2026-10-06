import Foundation

struct ApprovalChallengeResponse: Decodable, Sendable {
    let challengeId: String
    let proposalId: String
    let keyId: String
    let intentHash: String
    let signedPayloadB64: String
    let issuedAt: Int
    let expiresAt: Int
}

struct SignedTradeApprovalPayload: Decodable, Equatable, Sendable {
    let version: Int
    let purpose: String
    let challengeId: String
    let proposalId: String
    let clientOrderId: String
    let intentHash: String
    let workspace: String
    let account: String
    let broker: String
    let mode: String
    let ticker: String
    let side: String
    let quantity: String
    let orderType: String?
    let limitPrice: String?
    let replacesProposalId: String?
    let requoteId: String?
    let nonce: String
    let issuedAt: Int
    let expiresAt: Int
    let keyId: String
}

/// A pending Trading 212 practice proposal as the loopback backend lists it
/// (`GET /api/t212-practice/proposals`). It never carries an account id.
struct PracticeProposal: Decodable, Equatable, Identifiable, Sendable {
    var id: String { proposalId }
    let proposalId: String
    let ticker: String
    let action: String
    let quantity: String
    let orderType: String
    let limitPrice: String
    let timeValidity: String
    let mode: String
    let broker: String
    let status: String
    let reasoning: String?
    let notional: String?

    enum CodingKeys: String, CodingKey {
        case proposalId = "proposal_id"
        case ticker, action, quantity, mode, broker, status, reasoning, notional
        case orderType = "order_type"
        case limitPrice = "limit_price"
        case timeValidity = "time_validity"
    }
}

struct PracticeProposalList: Decodable, Sendable {
    let proposals: [PracticeProposal]
}

/// What the app will sign for a practice order. The app signs PRACTICE only
/// (never LIVE), only for the UK workspace, and only a LIMIT DAY order on the
/// Trading 212 practice venue that matches the proposal it showed.
nonisolated enum PracticeApprovalPolicy {
    static let mode = "PRACTICE"
    static let broker = "t212_practice"
    static let orderType = "LIMIT"
    static let timeValidity = "DAY"
    static let workspace = Workspace.uk
}

nonisolated enum PracticeApprovalCopy {
    static let banner = "PRACTICE · Trading 212 demo · virtual funds"
    static let sectionTitle = "Practice Approvals (Trading 212 demo)"
    static let explanation = "Review the server-frozen practice order. Your local key signs these exact fields; Touch ID confirms one LIMIT DAY order to the Trading 212 demo account. Live trading is never signed here."
    static let approveTitle = "Sign and approve practice order"
}

/// Enrolment rules that depend on the venue the backend reports.
nonisolated enum ApprovalEnrollment {
    /// The backend reports "paper" or "practice" when it holds local execution authority.
    static func allowsEnrolment(mode: String) -> Bool {
        mode == "paper" || mode == "practice"
    }

    /// Where the one-time enrolment token sits, beside the ledger the backend reported:
    /// the workspace's paper ledger, or the separate UK practice ledger. Any other
    /// mode or workspace pairing has no token path.
    static func tokenURL(
        workspace: Workspace,
        mode: String,
        home: URL = FileManager.default.homeDirectoryForCurrentUser
    ) -> URL? {
        let base = home.appendingPathComponent("Library/Application Support/Growin/workspaces")
        switch (mode, workspace) {
        case ("paper", _):
            return base.appendingPathComponent("\(workspace.rawValue)/execution.sqlite3.enrollment-token")
        case ("practice", .uk):
            return base.appendingPathComponent("uk-t212-practice/execution.sqlite3.enrollment-token")
        default:
            return nil
        }
    }
}

struct TradeApprovalReview: Identifiable, Sendable {
    let challenge: ApprovalChallengeResponse
    let payload: SignedTradeApprovalPayload
    let signedBytes: Data

    var id: String { challenge.challengeId }

    init(challenge: ApprovalChallengeResponse, expectedProposal: TradeProposalData, expectedWorkspace: Workspace) throws {
        guard let bytes = Data(base64Encoded: challenge.signedPayloadB64) else {
            throw TradeApprovalReviewError.invalidEnvelope
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let payload = try decoder.decode(SignedTradeApprovalPayload.self, from: bytes)
        guard
            payload.version == 1,
            payload.purpose == "growin.execution.dispatch",
            payload.mode == "PAPER",
            payload.challengeId == challenge.challengeId,
            payload.proposalId == challenge.proposalId,
            payload.proposalId == expectedProposal.proposalId,
            payload.keyId == challenge.keyId,
            payload.intentHash == challenge.intentHash,
            payload.issuedAt == challenge.issuedAt,
            payload.expiresAt == challenge.expiresAt,
            payload.expiresAt > Int(Date().timeIntervalSince1970),
            payload.workspace == expectedWorkspace.rawValue
        else {
            throw TradeApprovalReviewError.invalidEnvelope
        }
        guard
            payload.ticker.trimmingCharacters(in: .whitespacesAndNewlines)
                .uppercased() == expectedProposal.ticker
                .trimmingCharacters(in: .whitespacesAndNewlines).uppercased(),
            payload.side.uppercased() == expectedProposal.action.uppercased(),
            Decimal(string: payload.quantity) == expectedProposal.quantity
        else {
            throw TradeApprovalReviewError.proposalMismatch
        }
        self.challenge = challenge
        self.payload = payload
        self.signedBytes = bytes
    }

    /// Review of a PRACTICE order. It is rejected unless the signed payload is a PRACTICE
    /// LIMIT DAY order on the practice venue for the UK workspace whose ticker, side,
    /// quantity, limit price and order type equal the proposal the user was shown. LIVE and
    /// any unknown mode are refused.
    init(challenge: ApprovalChallengeResponse, expectedPractice proposal: PracticeProposal, expectedWorkspace: Workspace) throws {
        guard let bytes = Data(base64Encoded: challenge.signedPayloadB64) else {
            throw TradeApprovalReviewError.invalidEnvelope
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let payload = try decoder.decode(SignedTradeApprovalPayload.self, from: bytes)
        guard
            expectedWorkspace == PracticeApprovalPolicy.workspace,
            payload.version == 1,
            payload.purpose == "growin.execution.dispatch",
            payload.mode == PracticeApprovalPolicy.mode,
            proposal.mode == PracticeApprovalPolicy.mode,
            payload.broker == PracticeApprovalPolicy.broker,
            proposal.broker == PracticeApprovalPolicy.broker,
            payload.challengeId == challenge.challengeId,
            payload.proposalId == challenge.proposalId,
            payload.proposalId == proposal.proposalId,
            payload.keyId == challenge.keyId,
            payload.intentHash == challenge.intentHash,
            payload.issuedAt == challenge.issuedAt,
            payload.expiresAt == challenge.expiresAt,
            payload.expiresAt > Int(Date().timeIntervalSince1970),
            payload.workspace == expectedWorkspace.rawValue
        else {
            throw TradeApprovalReviewError.invalidEnvelope
        }
        guard
            payload.orderType == PracticeApprovalPolicy.orderType,
            proposal.orderType == PracticeApprovalPolicy.orderType,
            proposal.timeValidity == PracticeApprovalPolicy.timeValidity,
            let signedLimit = payload.limitPrice.flatMap({ Decimal(string: $0) }),
            let shownLimit = Decimal(string: proposal.limitPrice),
            signedLimit == shownLimit,
            payload.ticker.trimmingCharacters(in: .whitespacesAndNewlines)
                == proposal.ticker.trimmingCharacters(in: .whitespacesAndNewlines),
            payload.side.uppercased() == proposal.action.uppercased(),
            let signedQuantity = Decimal(string: payload.quantity),
            let shownQuantity = Decimal(string: proposal.quantity),
            signedQuantity == shownQuantity
        else {
            throw TradeApprovalReviewError.proposalMismatch
        }
        self.challenge = challenge
        self.payload = payload
        self.signedBytes = bytes
    }

    init(challenge: ApprovalChallengeResponse, payload: SignedTradeApprovalPayload, signedBytes: Data) {
        self.challenge = challenge
        self.payload = payload
        self.signedBytes = signedBytes
    }

    static func testingPlaceholder(proposal: TradeProposalData) -> TradeApprovalReview {
        let now = Int(Date().timeIntervalSince1970)
        let challenge = ApprovalChallengeResponse(
            challengeId: "test-challenge",
            proposalId: proposal.proposalId,
            keyId: "test-key",
            intentHash: "test-intent",
            signedPayloadB64: Data("{}".utf8).base64EncodedString(),
            issuedAt: now,
            expiresAt: now + 600
        )
        let payload = SignedTradeApprovalPayload(
            version: 1,
            purpose: "growin.execution.dispatch",
            challengeId: challenge.challengeId,
            proposalId: proposal.proposalId,
            clientOrderId: "test-client-order",
            intentHash: challenge.intentHash,
            workspace: "india",
            account: "paper",
            broker: "local-paper",
            mode: "PAPER",
            ticker: proposal.ticker,
            side: proposal.action,
            quantity: "\(proposal.quantity)",
            orderType: nil,
            limitPrice: nil,
            replacesProposalId: nil,
            requoteId: nil,
            nonce: "test-nonce",
            issuedAt: challenge.issuedAt,
            expiresAt: challenge.expiresAt,
            keyId: challenge.keyId
        )
        return TradeApprovalReview(challenge: challenge, payload: payload, signedBytes: Data())
    }
}

enum TradeApprovalReviewError: LocalizedError {
    case invalidEnvelope
    case proposalMismatch
    case signerMismatch
    case workspaceMismatch

    var errorDescription: String? {
        switch self {
        case .invalidEnvelope:
            return "The server approval challenge did not match the reviewed trade."
        case .proposalMismatch:
            return "The frozen approval fields do not match the selected trade proposal."
        case .signerMismatch:
            return "The enrolled approval key does not match this workspace."
        case .workspaceMismatch:
            return "The backend is running a different workspace."
        }
    }
}
