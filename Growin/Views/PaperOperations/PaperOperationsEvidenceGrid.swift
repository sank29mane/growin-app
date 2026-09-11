import SwiftUI

struct PaperOperationsEvidenceGrid: View {
    @Bindable var viewModel: PaperOperationsViewModel

    private let columns = [
        GridItem(.flexible(), spacing: 16),
        GridItem(.flexible(), spacing: 16),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            LazyVGrid(columns: columns, spacing: 16) {
                PaperOperationsEvidenceCard(
                    title: PaperOperationsCopy.cardRegime,
                    sessionStopped: sessionStopped,
                    rows: regimeRows
                )
                PaperOperationsEvidenceCard(
                    title: PaperOperationsCopy.cardSimulator,
                    sessionStopped: sessionStopped,
                    rows: simulatorRows
                )
                PaperOperationsEvidenceCard(
                    title: PaperOperationsCopy.cardSwarm,
                    sessionStopped: sessionStopped,
                    rows: swarmRows
                )
                PaperOperationsEvidenceCard(
                    title: PaperOperationsCopy.cardSnapshot,
                    sessionStopped: sessionStopped,
                    rows: snapshotRows
                )
            }

            if !viewModel.rejectionReasons.isEmpty {
                rejectionList
            }
        }
    }

    private var sessionStopped: Bool {
        viewModel.sessionState == "STOPPED"
    }

    private var regimeRows: [PaperOperationsEvidenceRow] {
        [
            PaperOperationsEvidenceRow(label: "regime_id", value: viewModel.regimeId),
            PaperOperationsEvidenceRow(label: "model_version", value: viewModel.modelVersion),
            PaperOperationsEvidenceRow(label: "observed_at", value: viewModel.regimeObservedAt),
            hashedRow(label: "source_snapshot_id", value: viewModel.sourceSnapshotId),
        ]
    }

    private var simulatorRows: [PaperOperationsEvidenceRow] {
        [
            PaperOperationsEvidenceRow(label: "fill price", value: viewModel.simulatorFillPrice),
            PaperOperationsEvidenceRow(label: "drawdown %", value: viewModel.simulatorDrawdownPct),
            PaperOperationsEvidenceRow(label: "decision", value: viewModel.simulatorDecision),
        ]
    }

    private var swarmRows: [PaperOperationsEvidenceRow] {
        [
            PaperOperationsEvidenceRow(label: "risk quantity", value: viewModel.swarmRiskQuantity),
            PaperOperationsEvidenceRow(label: "spread %", value: viewModel.swarmSpreadPct),
            PaperOperationsEvidenceRow(label: "reason code", value: viewModel.swarmReasonCode),
        ]
    }

    private var snapshotRows: [PaperOperationsEvidenceRow] {
        [
            PaperOperationsEvidenceRow(label: "symbol", value: snapshotSymbol),
            PaperOperationsEvidenceRow(label: "bid", value: snapshotBid),
            PaperOperationsEvidenceRow(label: "ask", value: snapshotAsk),
            PaperOperationsEvidenceRow(label: "quote_observed_at", value: snapshotQuoteObservedAt),
            hashedRow(label: "snapshot_id", value: snapshotId),
            PaperOperationsEvidenceRow(label: "source", value: snapshotSource),
        ]
    }

    private var rejectionList: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(
                viewModel.rejectionReasons.count == 1
                    ? PaperOperationsCopy.rejectionReason
                    : PaperOperationsCopy.rejectionReasons
            )
            .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold))
            .foregroundStyle(Color.brutalOffWhite)

            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(viewModel.rejectionReasons.enumerated()), id: \.offset) { _, reason in
                        Text(reason)
                            .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                            .foregroundStyle(Color.growinRed)
                            .lineSpacing(8)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxHeight: 192)
        }
    }

    private func hashedRow(label: String, value: String?) -> PaperOperationsEvidenceRow {
        guard let value, !value.isEmpty else {
            return PaperOperationsEvidenceRow(label: label, value: nil)
        }
        return PaperOperationsEvidenceRow(
            label: label,
            value: PaperOperationsCopy.truncatedHash(value),
            help: value
        )
    }

    private var snapshotSymbol: String? {
        viewModel.snapshot?.instrument.symbol ?? viewModel.lastEvidence?.snapshotSymbol
    }

    private var snapshotBid: String? {
        viewModel.snapshot?.bid ?? viewModel.lastEvidence?.bid
    }

    private var snapshotAsk: String? {
        viewModel.snapshot?.ask ?? viewModel.lastEvidence?.ask
    }

    private var snapshotQuoteObservedAt: String? {
        if let date = viewModel.snapshot?.quoteObservedAt {
            return Self.isoStamp.string(from: date)
        }
        return viewModel.lastEvidence?.quoteObservedAt
    }

    private var snapshotId: String? {
        viewModel.snapshot?.snapshotId ?? viewModel.lastEvidence?.snapshotId
    }

    private var snapshotSource: String? {
        viewModel.snapshot?.source ?? viewModel.lastEvidence?.source
    }

    private static let isoStamp: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
}
