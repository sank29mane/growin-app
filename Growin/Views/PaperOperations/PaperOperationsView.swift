import SwiftUI

struct PaperOperationsView: View {
    @Bindable var viewModel: PaperOperationsViewModel

    var body: some View {
        SovereignContainer {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    PaperOperationsHeaderView()
                    PaperOperationsBlockingSlot(
                        copy: viewModel.blockingSlotCopy,
                        blocked: !viewModel.canPrepare
                    )
                    PaperOperationsSessionCard(viewModel: viewModel)
                    PaperOperationsEvidenceGrid(viewModel: viewModel)
                    prepareRow
                    PaperOperationsLifecycleStrip(viewModel: viewModel)
                }
                .padding(24)
            }
        }
        .sheet(item: $viewModel.pendingTradeApproval) { review in
            TradeApprovalSheet(review: review) {
                try await viewModel.completeTradeApproval(review)
            }
        }
    }

    private var prepareRow: some View {
        let enabled = viewModel.canPrepare && viewModel.inFlightAction == nil
        let accent = viewModel.accentedWorkflowAction == .prepare

        return VStack(alignment: .leading, spacing: 8) {
            PaperOperationsWorkflowButton(
                title: PaperOperationsCopy.preparePaperIntent,
                inFlight: viewModel.inFlightAction == .prepare,
                enabled: enabled,
                accent: accent
            ) {
                Task { await viewModel.preparePaperIntent() }
            }
            .accessibilityHint(enabled ? "" : viewModel.disabledPrepareAccessibilityHint)

            if !viewModel.canPrepare {
                Text(viewModel.blockingSlotCopy)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                    .foregroundStyle(Color.brutalOffWhite)
                    .lineSpacing(8)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let message = viewModel.prepareFailedMessage {
                Text(message)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                    .foregroundStyle(Color.growinRed)
                    .lineSpacing(8)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.top, 8)
    }
}
