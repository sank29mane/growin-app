import SwiftUI

struct PaperOperationsView: View {
    @Bindable var viewModel: PaperOperationsViewModel

    var body: some View {
        SovereignContainer {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    PaperOperationsHeaderView()
                    PaperOperationsBlockingSlot(reason: viewModel.blockingReason)
                    PaperOperationsSessionCard(viewModel: viewModel)
                    emptyEvidence
                }
                .padding(24)
            }
        }
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
    }
}
