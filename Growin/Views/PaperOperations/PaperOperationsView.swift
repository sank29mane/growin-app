import SwiftUI

struct PaperOperationsView: View {
    @Bindable var viewModel: PaperOperationsViewModel

    var body: some View {
        SovereignContainer {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    header
                    modeStrip
                    blockingSlot
                    startLocalReplayButton
                    emptyEvidence
                }
                .padding(24)
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("PAPER OPERATIONS")
                .font(SovereignTheme.Fonts.notoSerif(size: 24))
                .foregroundStyle(Color.brutalOffWhite)

            Text(PaperOperationsCopy.subtitle)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                .foregroundStyle(Color.brutalChartreuse)
        }
    }

    private var modeStrip: some View {
        Text(PaperOperationsCopy.modeStrip)
            .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold))
            .foregroundStyle(Color.brutalOffWhite)
    }

    private var blockingSlot: some View {
        let blocked = viewModel.blockingReason != nil
        return HStack(alignment: .top, spacing: 8) {
            Rectangle()
                .fill(blocked ? Color.growinRed : Color.white.opacity(0.15))
                .frame(width: 4)

            Text(viewModel.blockingReason?.copy ?? PaperOperationsCopy.evidenceComplete)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                .foregroundStyle(blocked ? Color.growinRed : Color.brutalOffWhite)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(minHeight: 48, alignment: .leading)
        .padding(16)
        .background(Color.brutalCharcoal)
        .border(Color.white.opacity(0.15), width: 1)
    }

    private var startLocalReplayButton: some View {
        let isNextSafe = viewModel.sessionState == "STOPPED" && !viewModel.isStarting
        return Button {
            Task {
                await viewModel.startLocalReplay()
            }
        } label: {
            HStack(spacing: 8) {
                if viewModel.isStarting {
                    ProgressView()
                        .controlSize(.small)
                }
                Text("Start Local Replay")
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .foregroundStyle(isNextSafe ? Color.brutalChartreuse : Color.brutalOffWhite.opacity(0.3))
            .background(Color.brutalRecessed)
            .border(Color.white.opacity(0.15), width: 1)
        }
        .buttonStyle(.plain)
        .disabled(viewModel.sessionState != "STOPPED" || viewModel.isStarting)
        .accessibilityLabel("Start Local Replay")
        .accessibilityAddTraits(.isButton)
    }

    @ViewBuilder
    private var emptyEvidence: some View {
        if viewModel.sessionState == "STOPPED" {
            VStack(alignment: .leading, spacing: 8) {
                Text(PaperOperationsCopy.emptyHeading)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                    .foregroundStyle(Color.brutalOffWhite)
                Text(PaperOperationsCopy.emptyBody)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                    .foregroundStyle(Color.brutalOffWhite.opacity(0.6))
            }
        }
        if let message = viewModel.startFailedMessage {
            Text(message)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                .foregroundStyle(Color.growinRed)
        }
    }
}
