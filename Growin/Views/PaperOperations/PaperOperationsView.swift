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
                }
                .padding(24)
            }
        }
    }
}
