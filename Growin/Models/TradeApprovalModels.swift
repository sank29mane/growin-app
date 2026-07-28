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

struct TradeApprovalReview: Identifiable, Sendable {
    let challenge: ApprovalChallengeResponse
    let payload: SignedTradeApprovalPayload
    let signedBytes: Data

    var id: String { challenge.challengeId }

    init(challenge: ApprovalChallengeResponse, expectedProposal: TradeProposalData) throws {
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
            payload.expiresAt > Int(Date().timeIntervalSince1970)
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
}

enum TradeApprovalReviewError: LocalizedError {
    case invalidEnvelope
    case proposalMismatch
    case signerMismatch

    var errorDescription: String? {
        switch self {
        case .invalidEnvelope:
            return "The server approval challenge did not match the reviewed trade."
        case .proposalMismatch:
            return "The frozen approval fields do not match the selected trade proposal."
        case .signerMismatch:
            return "The enrolled approval key does not match this workspace."
        }
    }
}
