import Foundation
import Observation

enum PaperOperationsInFlightAction: Equatable, Sendable {
    case start
    case stop
    case refresh
    case load
    case prepare
}

@Observable
@MainActor
final class PaperOperationsViewModel {
    var session: PaperSessionStatus = .stopped
    var selectedInstrument: IndiaInstrumentDTO?
    var quantity: String = "1"
    var snapshot: PaperMarketSnapshot?
    var lastEvidence: PaperOperationsEvidence?
    var blockingReason: BlockingReason? = .stopped
    var startFailedMessage: String?
    var stopFailedMessage: String?
    var statusFailedMessage: String?
    var snapshotFailedMessage: String?
    var prepareFailedMessage: String?
    var inFlightAction: PaperOperationsInFlightAction?
    var pendingTradeApproval: TradeApprovalReview?
    var unreconciledIntent = false {
        didSet { applyUnreconciledGate() }
    }
    var rejectionReasons: [String] = []
    var regimeId: String?
    var modelVersion: String?
    var regimeObservedAt: String?
    var sourceSnapshotId: String?
    var simulatorFillPrice: String?
    var simulatorDrawdownPct: String?
    var simulatorDecision: String?
    var swarmRiskQuantity: String?
    var swarmSpreadPct: String?
    var swarmReasonCode: String?

    private let client: PaperOperationsClient
    private let aiService: AIService
    private let signer: PaperApprovalSigning
    private let tradeApprover: PaperTradeApproving

    var sessionState: String { session.state }
    var isStarting: Bool { inFlightAction == .start }

    var blockingSlotCopy: String {
        if let blockingReason {
            return blockingReason.copy
        }
        if canPrepare {
            return PaperOperationsCopy.evidenceComplete
        }
        return PaperOperationsCopy.missingSnapshot
    }

    var disabledPrepareAccessibilityHint: String {
        canPrepare ? "" : blockingSlotCopy
    }

    var selectedInstrumentSymbol: String? {
        get { selectedInstrument?.symbol }
        set {
            guard let newValue,
                  let match = session.instruments.first(where: { $0.symbol == newValue }) else {
                return
            }
            selectedInstrument = match
        }
    }

    var canPrepare: Bool {
        guard session.state == "RUNNING" else { return false }
        guard let selected = selectedInstrument,
              session.instruments.contains(selected) else { return false }
        guard Self.isPositiveDecimal(quantity) else { return false }
        guard let snapshot, snapshot.source == "local-replay" else { return false }
        guard signer.isConfigured else { return false }
        guard !unreconciledIntent else { return false }
        if let blockingReason {
            switch blockingReason {
            case .stopped, .missingSnapshot, .staleSnapshot, .malformed,
                 .signerMissing, .unreconciled, .admissionDenied, .rejectedAfterPrepare:
                return false
            }
        }
        return true
    }

    init(
        client: PaperOperationsClient,
        aiService: AIService? = nil,
        signer: PaperApprovalSigning? = nil,
        tradeApprover: PaperTradeApproving? = nil
    ) {
        self.client = client
        let service = aiService ?? AIService()
        self.aiService = service
        self.signer = signer ?? LocalPaperApprovalSigner()
        self.tradeApprover = tradeApprover ?? AIServicePaperTradeApprover(service: service)
    }

    func startLocalReplay() async {
        guard inFlightAction == nil else { return }
        inFlightAction = .start
        startFailedMessage = nil
        defer { inFlightAction = nil }

        do {
            let started = try await client.startSession()
            session = started
            syncSelectedInstrument(allowImplicitSingle: true)
            guard session.instruments.count == 1, let symbol = selectedInstrument?.symbol else {
                blockingReason = selectedInstrument == nil ? .missingSnapshot : durableReasonAfterEvidence()
                return
            }
            await fetchSnapshot(symbol: symbol, keepLastEvidenceOnFailure: false)
        } catch {
            startFailedMessage = PaperOperationsCopy.startFailed
            if session.state != "RUNNING" {
                blockingReason = .stopped
            }
        }
    }

    func stopLocalReplay() async {
        guard inFlightAction == nil else { return }
        inFlightAction = .stop
        stopFailedMessage = nil
        defer { inFlightAction = nil }

        do {
            let stopped = try await client.stopSession()
            session = stopped
            syncSelectedInstrument(allowImplicitSingle: true)
            if unreconciledIntent {
                blockingReason = .unreconciled
            } else {
                blockingReason = .stopped
            }
        } catch {
            stopFailedMessage = PaperOperationsCopy.stopFailed
        }
    }

    func refreshSessionStatus() async {
        guard inFlightAction == nil else { return }
        inFlightAction = .refresh
        statusFailedMessage = nil
        defer { inFlightAction = nil }

        let wasStale = blockingReason?.kind == .staleSnapshot
        do {
            let current = try await client.currentSession()
            session = current
            syncSelectedInstrument(allowImplicitSingle: true)
            if session.state != "RUNNING" {
                blockingReason = unreconciledIntent ? .unreconciled : .stopped
            } else if wasStale {
                blockingReason = .staleSnapshot
            } else if selectedInstrument == nil || snapshot == nil {
                blockingReason = .missingSnapshot
            } else {
                blockingReason = durableReasonAfterEvidence()
            }
        } catch {
            statusFailedMessage = PaperOperationsCopy.statusFailed
        }
    }

    func loadSnapshotEvidence() async {
        guard inFlightAction == nil else { return }
        guard session.state == "RUNNING", let symbol = selectedInstrumentSymbol else { return }
        inFlightAction = .load
        snapshotFailedMessage = nil
        defer { inFlightAction = nil }

        await fetchSnapshot(symbol: symbol, keepLastEvidenceOnFailure: true)
        if blockingReason?.kind == .missingSnapshot {
            snapshotFailedMessage = PaperOperationsCopy.snapshotFailed(symbol: symbol)
        }
    }

    func preparePaperIntent() async {
        guard inFlightAction == nil, canPrepare else { return }
        guard let symbol = selectedInstrument?.symbol else { return }
        inFlightAction = .prepare
        prepareFailedMessage = nil
        defer { inFlightAction = nil }

        do {
            let response = try await client.prepareIndiaPaper(symbol: symbol, quantity: quantity)
            applyAdmission(response.admission)
            if let regime = response.regime {
                applyRegime(regime)
            }
            if let snapshot {
                lastEvidence = captureEvidence(from: snapshot)
            }
            guard response.admission.isAdmitted else {
                pendingTradeApproval = nil
                blockingReason = .admissionDenied(reasonCode: response.admission.reasonCode)
                return
            }
            let trimmed = quantity.trimmingCharacters(in: .whitespacesAndNewlines)
            guard let qty = Decimal(string: trimmed) else {
                prepareFailedMessage = PaperOperationsCopy.prepareFailed
                return
            }
            let proposal = TradeProposalData(
                proposalId: response.proposalId,
                ticker: "NSE:CASH:\(symbol)",
                action: "BUY",
                quantity: qty,
                reasoning: nil,
                status: response.state
            )
            pendingTradeApproval = try await tradeApprover.requestTradeApproval(proposal: proposal)
        } catch PaperOperationsClientError.paperPreparationDenied {
            pendingTradeApproval = nil
            blockingReason = .rejectedAfterPrepare(reasonCode: "PAPER_PREPARATION_DENIED")
        } catch {
            pendingTradeApproval = nil
            prepareFailedMessage = PaperOperationsCopy.prepareFailed
        }
    }

    func completeTradeApproval(_ review: TradeApprovalReview) async throws {
        let identity = try signer.identity()
        guard identity.keyID == review.payload.keyId else {
            throw TradeApprovalReviewError.signerMismatch
        }
        let signature = try signer.sign(review.signedBytes)
        _ = try await aiService.completeTradeApproval(review, signature: signature)
        pendingTradeApproval = nil
    }

    func applyMalformedSnapshotPayload(_ data: Data) {
        do {
            _ = try PaperOperationsModels.decodeSnapshot(data)
        } catch {
            blockingReason = .malformed
        }
    }

    private func fetchSnapshot(symbol: String, keepLastEvidenceOnFailure: Bool) async {
        do {
            let loaded = try await client.snapshot(symbol: symbol)
            snapshot = loaded
            lastEvidence = captureEvidence(from: loaded)
            blockingReason = durableReasonAfterEvidence()
        } catch {
            if !keepLastEvidenceOnFailure {
                snapshot = nil
            }
            blockingReason = mapSnapshotFailure(error)
        }
    }

    private func syncSelectedInstrument(allowImplicitSingle: Bool) {
        if let selected = selectedInstrument, session.instruments.contains(selected) {
            return
        }
        if allowImplicitSingle, session.instruments.count == 1 {
            selectedInstrument = session.instruments[0]
            return
        }
        selectedInstrument = nil
    }

    private func applyUnreconciledGate() {
        if unreconciledIntent {
            switch blockingReason {
            case .malformed, .staleSnapshot, .admissionDenied, .rejectedAfterPrepare:
                return
            default:
                blockingReason = .unreconciled
            }
        }
    }

    private func applyAdmission(_ admission: PaperAdmission) {
        simulatorFillPrice = admission.simulatorFillPrice
        simulatorDrawdownPct = admission.simulatorDrawdownPct
        simulatorDecision = admission.decision
        swarmRiskQuantity = admission.riskQuantity
        swarmSpreadPct = admission.currentSpreadPct
        swarmReasonCode = admission.reasonCode
        if admission.isAdmitted {
            rejectionReasons = []
        } else if !admission.reasonCode.isEmpty {
            rejectionReasons = [admission.reasonCode]
        }
    }

    private func applyRegime(_ regime: PaperRegimeEvidence) {
        if let regimeId = regime.regimeId {
            self.regimeId = String(regimeId)
        }
        if let modelVersion = regime.modelVersion {
            self.modelVersion = modelVersion
        }
        if let observedAt = regime.observedAt {
            regimeObservedAt = observedAt
        }
        if let sourceSnapshotId = regime.sourceSnapshotId {
            self.sourceSnapshotId = sourceSnapshotId
        }
    }

    private func durableReasonAfterEvidence() -> BlockingReason? {
        if !signer.isConfigured {
            return .signerMissing
        }
        if unreconciledIntent {
            return .unreconciled
        }
        return nil
    }

    private func captureEvidence(from snapshot: PaperMarketSnapshot) -> PaperOperationsEvidence {
        PaperOperationsEvidence(
            snapshotSymbol: snapshot.instrument.symbol,
            source: snapshot.source,
            bid: snapshot.bid,
            ask: snapshot.ask,
            quoteObservedAt: Self.isoStamp.string(from: snapshot.quoteObservedAt),
            snapshotId: snapshot.snapshotId,
            regimeId: regimeId,
            modelVersion: modelVersion,
            regimeObservedAt: regimeObservedAt,
            sourceSnapshotId: sourceSnapshotId,
            simulatorFillPrice: simulatorFillPrice,
            simulatorDrawdownPct: simulatorDrawdownPct,
            simulatorDecision: simulatorDecision,
            swarmRiskQuantity: swarmRiskQuantity,
            swarmSpreadPct: swarmSpreadPct,
            swarmReasonCode: swarmReasonCode,
            rejectionReasons: rejectionReasons
        )
    }

    private static let isoStamp: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private func mapSnapshotFailure(_ error: Error) -> BlockingReason {
        if case PaperOperationsClientError.staleSnapshot = error {
            return .staleSnapshot
        }
        if case PaperOperationsClientError.httpStatus(409, let detail) = error,
           detail.uppercased().contains("STALE") {
            return .staleSnapshot
        }
        if case PaperOperationsModels.DecodeError.malformedSnapshot = error {
            return .malformed
        }
        return .missingSnapshot
    }

    private static func isPositiveDecimal(_ raw: String) -> Bool {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard (1...32).contains(trimmed.count) else { return false }
        guard let value = Decimal(string: trimmed), value > 0 else { return false }
        return true
    }
}
