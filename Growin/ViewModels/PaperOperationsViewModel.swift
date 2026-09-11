import Foundation
import Observation

enum PaperOperationsInFlightAction: Equatable, Sendable {
    case start
    case stop
    case refresh
    case load
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
    var inFlightAction: PaperOperationsInFlightAction?
    var unreconciledIntent = false

    private let client: PaperOperationsClient
    private let aiService: AIService
    private let signer: PaperApprovalSigning

    var sessionState: String { session.state }
    var isStarting: Bool { inFlightAction == .start }

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
        signer: PaperApprovalSigning? = nil
    ) {
        self.client = client
        self.aiService = aiService ?? AIService()
        self.signer = signer ?? LocalPaperApprovalSigner()
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
            lastEvidence = PaperOperationsEvidence(
                snapshotSymbol: loaded.instrument.symbol,
                source: loaded.source,
                bid: loaded.bid,
                ask: loaded.ask
            )
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

    private func durableReasonAfterEvidence() -> BlockingReason? {
        if !signer.isConfigured {
            return .signerMissing
        }
        if unreconciledIntent {
            return .unreconciled
        }
        return nil
    }

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
