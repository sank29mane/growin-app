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
        let accent = viewModel.canPrepare && viewModel.inFlightAction == nil

        return VStack(alignment: .leading, spacing: 8) {
            Button {
                Task { await viewModel.preparePaperIntent() }
            } label: {
                HStack(spacing: 8) {
                    if viewModel.inFlightAction == .prepare {
                        ProgressView()
                            .controlSize(.small)
                    }
                    Text(PaperOperationsCopy.preparePaperIntent)
                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                .foregroundStyle(prepareColor(enabled: enabled, accent: accent))
                .background(Color.brutalRecessed)
                .border(Color.white.opacity(0.15), width: 1)
            }
            .buttonStyle(.plain)
            .disabled(!enabled)
            .accessibilityLabel(PaperOperationsCopy.preparePaperIntent)
            .accessibilityHint(enabled ? "" : viewModel.disabledPrepareAccessibilityHint)
            .accessibilityAddTraits(.isButton)

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

    private func prepareColor(enabled: Bool, accent: Bool) -> Color {
        if !enabled {
            return Color.brutalOffWhite.opacity(0.3)
        }
        if accent {
            return Color.brutalChartreuse
        }
        return Color.brutalOffWhite
    }
}
