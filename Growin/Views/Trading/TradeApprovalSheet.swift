import SwiftUI

struct TradeApprovalSheet: View {
    let review: TradeApprovalReview
    let onApprove: () async throws -> Void
    let title: String
    let explanation: String
    let approveTitle: String

    init(
        review: TradeApprovalReview,
        title: String = "Paper trade approval",
        explanation: String = "Review the server-frozen order. Your local key signs these exact fields; any later change invalidates approval.",
        approveTitle: String = "Sign and approve paper order",
        onApprove: @escaping () async throws -> Void
    ) {
        self.review = review
        self.title = title
        self.explanation = explanation
        self.approveTitle = approveTitle
        self.onApprove = onApprove
    }

    @Environment(\.dismiss) private var dismiss
    @State private var isApproving = false
    @State private var errorMessage: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack {
                Label(title, systemImage: "checkmark.shield")
                    .font(.title2.bold())
                Spacer()
                Text("PAPER ONLY")
                    .font(.caption.bold())
                    .foregroundStyle(.orange)
            }

            Text(explanation)
                .foregroundStyle(.secondary)

            Grid(alignment: .leading, horizontalSpacing: 24, verticalSpacing: 10) {
                row("Action", review.payload.side)
                row("Ticker", review.payload.ticker)
                row("Quantity", review.payload.quantity)
                row("Account", review.payload.account)
                row("Workspace", review.payload.workspace)
                row("Broker", review.payload.broker)
                row("Mode", review.payload.mode)
                if let orderType = review.payload.orderType {
                    row("Order type", orderType)
                }
                if let limitPrice = review.payload.limitPrice {
                    row("Limit price", limitPrice)
                }
                if let parent = review.payload.replacesProposalId, !parent.isEmpty {
                    row("Replaces", String(parent.prefix(16)) + "…")
                }
                if let requote = review.payload.requoteId, !requote.isEmpty {
                    row("Re-quote", String(requote.prefix(16)) + "…")
                }
                row("Expires", Date(timeIntervalSince1970: TimeInterval(review.payload.expiresAt)).formatted())
                row("Intent", String(review.payload.intentHash.prefix(16)) + "…")
            }
            .padding()
            .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 12))

            if let errorMessage {
                Label(errorMessage, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout)
                    .foregroundStyle(.red)
            }

            HStack {
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button {
                    approve()
                } label: {
                    if isApproving {
                        ProgressView().controlSize(.small)
                    } else {
                        Label(approveTitle, systemImage: "signature")
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(isApproving || isExpired)
            }
        }
        .padding(24)
        .frame(width: 520)
        .interactiveDismissDisabled(isApproving)
    }

    @ViewBuilder
    private func row(_ label: String, _ value: String) -> some View {
        GridRow {
            Text(label).foregroundStyle(.secondary)
            Text(value).font(.body.monospaced())
        }
    }

    private var isExpired: Bool {
        review.payload.expiresAt <= Int(Date().timeIntervalSince1970)
    }

    private func approve() {
        isApproving = true
        errorMessage = nil
        Task {
            do {
                try await onApprove()
                dismiss()
            } catch {
                errorMessage = error.localizedDescription
            }
            isApproving = false
        }
    }
}
