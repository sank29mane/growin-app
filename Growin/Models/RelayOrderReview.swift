import Foundation

/// What the backend says it is asking the operator to sign. It is only ever a
/// claim. The review never displays these values; it compares them with the
/// parsed bytes and flags any difference (T-63-16).
nonisolated struct RelaySummaryClaim: Decodable, Equatable, Sendable {
    var side: String?
    var quantity: Int?
    var stockCode: String?
    var isin: String?
    var limitPrice: String?
    var reason: String?
    var batchId: String?
}

/// Challenge answer for a SHADOW relay intent. Only the challenge id, proposal id
/// and the VM-minted bytes are required; everything else is an optional claim.
nonisolated struct RelayApprovalChallenge: Decodable, Equatable, Sendable {
    let challengeId: String
    let proposalId: String
    let signedPayloadB64: String
    let keyId: String?
    let summary: RelaySummaryClaim?
}

nonisolated enum RelayOrderReviewError: LocalizedError, Equatable {
    case invalidEnvelope
    case notARelayOrder
    case challengeMismatch
    case proposalMismatch
    case batchMismatch
    case expired

    var errorDescription: String? {
        switch self {
        case .invalidEnvelope:
            return "The relay challenge did not contain readable bytes to sign."
        case .notARelayOrder:
            return "The bytes to sign are not a relay order. Nothing was shown for approval."
        case .challengeMismatch:
            return "The signed bytes belong to a different challenge than the one the server named."
        case .proposalMismatch:
            return "The signed bytes belong to a different proposal than the one requested."
        case .batchMismatch:
            return "The signed bytes belong to a different batch than the one being approved."
        case .expired:
            return "The challenge expired before it was signed. Nothing was sent."
        }
    }
}

/// One relay order, described only by the bytes that will be signed.
nonisolated struct RelayOrderReview: Identifiable, Equatable, Sendable {
    let challengeId: String
    let proposalId: String
    let signedBytes: Data
    let payload: RelayOrderPayload
    /// Names of fields where the backend's own summary differed from the bytes.
    /// The summary is ignored; the operator sees the bytes' values plus this flag.
    let summaryDisagreements: [String]

    var id: String { challengeId }
    var intent: RelayOrderIntentFields { payload.intent }
    var side: String { intent.side }
    var quantity: Int { intent.quantity }
    var stockCode: String { intent.stockCode }
    var isin: String { intent.isin }
    var limitPrice: String { intent.limitPrice }
    var reason: String { intent.reason }
    var batchId: String? { intent.batchId }
    var hasSummaryDisagreement: Bool { !summaryDisagreements.isEmpty }

    /// quantity x limit, exact (the limit has at most two decimals).
    var notional: Decimal {
        Decimal(intent.quantity) * (Decimal(string: intent.limitPrice, locale: Locale(identifier: "en_US_POSIX")) ?? 0)
    }

    var notionalText: String {
        let formatter = NumberFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.numberStyle = .decimal
        formatter.usesGroupingSeparator = false
        formatter.minimumFractionDigits = 2
        formatter.maximumFractionDigits = 2
        return formatter.string(from: notional as NSDecimalNumber) ?? "\(notional)"
    }

    init(challenge: RelayApprovalChallenge, expectedProposalId: String, now: Date = Date()) throws {
        guard let bytes = Data(base64Encoded: challenge.signedPayloadB64), !bytes.isEmpty else {
            throw RelayOrderReviewError.invalidEnvelope
        }
        // Every displayed value comes from this parse and from nowhere else.
        guard case .relayOrder(let payload) = try SignedPayloadInspector.inspect(bytes) else {
            throw RelayOrderReviewError.notARelayOrder
        }
        guard payload.challengeId == challenge.challengeId else {
            throw RelayOrderReviewError.challengeMismatch
        }
        guard payload.intent.proposalId == expectedProposalId, challenge.proposalId == expectedProposalId else {
            throw RelayOrderReviewError.proposalMismatch
        }
        guard payload.expiresAt > Int(now.timeIntervalSince1970) else {
            throw RelayOrderReviewError.expired
        }
        self.challengeId = payload.challengeId
        self.proposalId = payload.intent.proposalId
        self.signedBytes = bytes
        self.payload = payload
        self.summaryDisagreements = Self.disagreements(claim: challenge.summary, keyId: challenge.keyId, payload: payload)
    }

    private static func disagreements(
        claim: RelaySummaryClaim?,
        keyId: String?,
        payload: RelayOrderPayload
    ) -> [String] {
        var names: [String] = []
        let intent = payload.intent
        if let keyId, keyId != payload.keyId { names.append("key_id") }
        guard let claim else { return names }
        if let side = claim.side, side.lowercased() != intent.side { names.append("side") }
        if let quantity = claim.quantity, quantity != intent.quantity { names.append("quantity") }
        if let stockCode = claim.stockCode, stockCode != intent.stockCode { names.append("stock_code") }
        if let isin = claim.isin, isin != intent.isin { names.append("isin") }
        if let limit = claim.limitPrice, !sameDecimal(limit, intent.limitPrice) { names.append("limit_price") }
        if let reason = claim.reason, reason != intent.reason { names.append("reason") }
        if let batchId = claim.batchId, batchId != intent.batchId { names.append("batch_id") }
        return names
    }

    private static func sameDecimal(_ left: String, _ right: String) -> Bool {
        let posix = Locale(identifier: "en_US_POSIX")
        guard let a = Decimal(string: left, locale: posix), let b = Decimal(string: right, locale: posix) else {
            return false
        }
        return a == b
    }
}
