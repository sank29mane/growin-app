import SwiftUI

/// Review and approve a batch of India relay orders (halve or flatten sells) one at
/// a time. Every value on screen is parsed from the bytes that will be signed; the
/// backend's own summary is never shown, only a flag when it disagreed. Each order
/// needs its own click and its own Touch ID. A failure stops the batch.
struct RelayOrderBatchView: View {
    @Bindable var model: RelayOrderBatchViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            ForEach(Array(model.items.enumerated()), id: \.element.id) { index, item in
                RelayOrderRow(position: index + 1, item: item)
            }
            if let failure = model.failure {
                VStack(alignment: .leading, spacing: 4) {
                    Label("Batch stopped: \(failure.code)", systemImage: "exclamationmark.octagon.fill")
                        .foregroundStyle(.red)
                    Text(failure.message)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Text("Nothing was retried. Remaining orders were not sent.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            } else if model.isFinished {
                Label("All \(model.items.count) orders approved. No order was placed with the broker.", systemImage: "checkmark.seal.fill")
                    .foregroundStyle(.green)
            }
            controls
        }
        .padding()
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("India relay orders")
                .font(.headline)
            if let batchId = model.batchId {
                Text("Batch \(batchId)")
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
            }
            Text("\(model.completedCount) of \(model.items.count) approved. Each order asks for Touch ID.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var controls: some View {
        if model.failure == nil, !model.isFinished, let index = model.currentIndex {
            switch model.items[index].state {
            case .queued:
                Button("Review order \(index + 1)") {
                    Task { await model.prepareNext() }
                }
                .disabled(model.isBusy)
            case .reviewing:
                Button("Approve order \(index + 1) with Touch ID") {
                    Task { await model.approveCurrent() }
                }
                .disabled(model.isBusy)
                .keyboardShortcut(.defaultAction)
            case .signing:
                ProgressView("Waiting for Touch ID")
            default:
                EmptyView()
            }
        }
    }
}

private struct RelayOrderRow: View {
    let position: Int
    let item: RelayOrderBatchViewModel.Item

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text("\(position). \(item.proposalId)")
                    .font(.subheadline.bold())
                Spacer()
                Text(statusText)
                    .font(.caption)
                    .foregroundStyle(statusColor)
            }
            if let review {
                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 2) {
                    row("Side", review.side.uppercased())
                    row("Quantity", "\(review.quantity)")
                    row("Stock", review.stockCode)
                    row("ISIN", review.isin)
                    row("Limit", review.limitPrice)
                    row("Notional", review.notionalText)
                    row("Reason", review.reason)
                    row("Batch", review.batchId ?? "none")
                }
                .font(.caption)
                if review.hasSummaryDisagreement {
                    Label(
                        "The server's summary differed from the signed bytes (\(review.summaryDisagreements.joined(separator: ", "))). The values above come from the bytes.",
                        systemImage: "exclamationmark.triangle.fill"
                    )
                    .font(.caption)
                    .foregroundStyle(.orange)
                }
            }
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 8).fill(.quaternary.opacity(0.4)))
    }

    private func row(_ label: String, _ value: String) -> some View {
        GridRow {
            Text(label).foregroundStyle(.secondary)
            Text(value).textSelection(.enabled)
        }
    }

    private var review: RelayOrderReview? {
        switch item.state {
        case .reviewing(let review), .signing(let review), .completed(let review, _): return review
        default: return nil
        }
    }

    private var statusText: String {
        switch item.state {
        case .queued: return "Queued"
        case .reviewing: return "Ready for Touch ID"
        case .signing: return "Signing"
        case .completed: return "Approved"
        case .failed(let failure): return "Failed: \(failure.code)"
        case .notAttempted: return "Not attempted"
        }
    }

    private var statusColor: Color {
        switch item.state {
        case .completed: return .green
        case .failed: return .red
        case .reviewing, .signing: return .orange
        default: return .secondary
        }
    }
}
