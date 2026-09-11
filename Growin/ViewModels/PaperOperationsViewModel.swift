import Foundation
import Observation

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
    var isStarting = false
    var unreconciledIntent = false

    private let client: PaperOperationsClient
    private let aiService: AIService
    private let signer: PaperApprovalSigning

    var sessionState: String { session.state }

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
        isStarting = true
        startFailedMessage = nil
        defer { isStarting = false }

        do {
            let started = try await client.startSession()
            session = started
            selectedInstrument = started.instruments.first
            guard let symbol = selectedInstrument?.symbol else {
                blockingReason = .missingSnapshot
                return
            }
            do {
                let loaded = try await client.snapshot(symbol: symbol)
                snapshot = loaded
                lastEvidence = PaperOperationsEvidence(
                    snapshotSymbol: loaded.instrument.symbol,
                    source: loaded.source,
                    bid: loaded.bid,
                    ask: loaded.ask
                )
                blockingReason = durableReasonAfterSuccessfulStart()
            } catch {
                snapshot = nil
                blockingReason = mapSnapshotFailure(error)
            }
        } catch {
            startFailedMessage = PaperOperationsCopy.startFailed
            if session.state != "RUNNING" {
                blockingReason = .stopped
            }
        }
    }

    func applyMalformedSnapshotPayload(_ data: Data) {
        do {
            _ = try PaperOperationsModels.decodeSnapshot(data)
        } catch {
            blockingReason = .malformed
        }
    }

    private func durableReasonAfterSuccessfulStart() -> BlockingReason? {
        if !signer.isConfigured {
            return .signerMissing
        }
        if unreconciledIntent {
            return .unreconciled
        }
        return nil
    }

    private func mapSnapshotFailure(_ error: Error) -> BlockingReason {
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
