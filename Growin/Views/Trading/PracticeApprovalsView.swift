import SwiftUI

/// The banner every practice approval sits behind. It names the venue, the broker,
/// the LIMIT price and DAY, so a practice order can never be mistaken for a paper
/// order or a live one.
struct PracticeBanner: View {
    let broker: String
    let limitPrice: String?
    let orderType: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Label(PracticeApprovalCopy.banner, systemImage: "testtube.2")
                .font(.headline)
            Text("Broker \(broker) · \(orderType ?? "no order type") \(limitPrice ?? "no price") · DAY")
                .font(.caption.monospaced())
                .foregroundStyle(.secondary)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.blue.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
        .accessibilityIdentifier("practice-banner")
    }
}

/// Settings card for practice approvals. It is its own card, outside the paper
/// approval security card and outside every screen labelled PAPER or NO BROKER.
struct PracticeApprovalsSection: View {
    var body: some View {
        SettingsCard(title: PracticeApprovalCopy.sectionTitle, icon: "testtube.2") {
            PracticeApprovalsView()
        }
    }
}

/// Lists pending practice proposals from the loopback backend and opens the existing
/// signed approval sheet for one. The app signs PRACTICE only and never LIVE: the
/// review refuses anything else before the sheet can be shown.
struct PracticeApprovalsView: View {
    @State private var proposals: [PracticeProposal] = []
    @State private var pendingReview: TradeApprovalReview?
    @State private var statusMessage: String?
    @State private var statusIsError = false
    @State private var isLoading = false
    @State private var openingProposalId: String?

    private let workspace = PracticeApprovalPolicy.workspace

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            PracticeBanner(broker: PracticeApprovalPolicy.broker, limitPrice: nil, orderType: PracticeApprovalPolicy.orderType)

            Text(PracticeApprovalCopy.explanation)
                .font(.caption)
                .foregroundStyle(.secondary)

            if proposals.isEmpty {
                Text(isLoading ? "Looking for practice orders..." : "No practice orders are waiting for approval.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }

            ForEach(proposals) { proposal in
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(proposal.action) \(proposal.quantity) × \(proposal.ticker)")
                            .font(.body.monospaced())
                        Text("\(proposal.orderType) \(proposal.limitPrice) · \(proposal.timeValidity) · \(proposal.broker)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Review") { open(proposal) }
                        .disabled(openingProposalId != nil)
                }
            }

            if let statusMessage {
                Text(statusMessage)
                    .font(.caption)
                    .foregroundStyle(statusIsError ? .red : .green)
            }

            Button {
                Task { await refresh() }
            } label: {
                Label("Refresh practice orders", systemImage: "arrow.clockwise")
            }
            .disabled(isLoading)
        }
        .task { await refresh() }
        .sheet(item: $pendingReview) { review in
            TradeApprovalSheet(
                review: review,
                title: "Practice trade approval",
                explanation: PracticeApprovalCopy.explanation,
                approveTitle: PracticeApprovalCopy.approveTitle
            ) {
                try await sign(review)
            }
        }
    }

    private func refresh() async {
        isLoading = true
        defer { isLoading = false }
        do {
            proposals = try await AIService().practiceProposals()
        } catch {
            proposals = []
            statusIsError = true
            statusMessage = error.localizedDescription
        }
    }

    private func open(_ proposal: PracticeProposal) {
        openingProposalId = proposal.proposalId
        statusMessage = nil
        Task {
            do {
                pendingReview = try await AIService().requestPracticeApproval(proposal: proposal, workspace: workspace)
            } catch {
                statusIsError = true
                statusMessage = error.localizedDescription
            }
            openingProposalId = nil
        }
    }

    /// Signs only a PRACTICE review for the UK workspace, with the UK key the review names.
    private func sign(_ review: TradeApprovalReview) async throws {
        guard review.payload.workspace == workspace.rawValue,
              review.payload.mode == PracticeApprovalPolicy.mode else {
            throw TradeApprovalReviewError.invalidEnvelope
        }
        let identity = try LocalApprovalSigner.shared.identity(for: workspace)
        guard identity.keyID == review.payload.keyId else {
            throw TradeApprovalReviewError.signerMismatch
        }
        let signature = try LocalApprovalSigner.shared.sign(review.signedBytes, for: workspace)
        let result = try await AIService().completeTradeApproval(review, signature: signature, workspace: workspace)
        statusIsError = false
        statusMessage = result.message
        await refresh()
    }
}
