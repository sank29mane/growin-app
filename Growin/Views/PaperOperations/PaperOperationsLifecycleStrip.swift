import SwiftUI

struct PaperOperationsLifecycleStrip: View {
    @Bindable var viewModel: PaperOperationsViewModel

    var body: some View {
        SovereignCard {
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .top, spacing: 8) {
                    ForEach(PaperOperationsLifecycleStep.allCases, id: \.self) { step in
                        lifecycleLabel(step)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }

                HStack(alignment: .top, spacing: 16) {
                    PaperOperationsWorkflowButton(
                        title: PaperOperationsCopy.acknowledgeLocalFill,
                        inFlight: viewModel.inFlightAction == .acknowledge,
                        enabled: viewModel.canAcknowledgeLocalFill && viewModel.inFlightAction == nil,
                        accent: viewModel.accentedWorkflowAction == .acknowledge
                    ) {
                        viewModel.acknowledgeLocalFill()
                    }
                    .accessibilityHint(acknowledgeHint)

                    PaperOperationsWorkflowButton(
                        title: PaperOperationsCopy.reconcilePaperOutcome,
                        inFlight: viewModel.inFlightAction == .reconcile,
                        enabled: viewModel.canReconcilePaperOutcome && viewModel.inFlightAction == nil,
                        accent: viewModel.accentedWorkflowAction == .reconcile
                    ) {
                        Task { await viewModel.reconcilePaperOutcome() }
                    }
                    .accessibilityHint(reconcileHint)
                }

                if let message = viewModel.acknowledgeFailedMessage {
                    Text(message)
                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                        .foregroundStyle(Color.growinRed)
                        .lineSpacing(8)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if let message = viewModel.reconcileFailedMessage {
                    Text(message)
                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                        .foregroundStyle(Color.growinRed)
                        .lineSpacing(8)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var acknowledgeHint: String {
        if viewModel.canAcknowledgeLocalFill { return "" }
        return viewModel.blockingSlotCopy
    }

    private var reconcileHint: String {
        if viewModel.canReconcilePaperOutcome { return "" }
        return viewModel.blockingSlotCopy
    }

    private func lifecycleLabel(_ step: PaperOperationsLifecycleStep) -> some View {
        let order = PaperOperationsLifecycleStep.allCases
        let currentIndex = order.firstIndex(of: viewModel.lifecycleStep) ?? 0
        let stepIndex = order.firstIndex(of: step) ?? 0
        let isCurrent = step == viewModel.lifecycleStep
        let isCompleted = stepIndex < currentIndex
        let isBlocked = isCurrent && isBlockedCurrentStep

        return HStack(alignment: .center, spacing: 4) {
            if isCompleted {
                Rectangle()
                    .fill(Color.brutalChartreuse)
                    .frame(width: 4, height: 4)
            }
            Text(step.rawValue)
                .font(
                    isCurrent
                        ? SovereignTheme.Fonts.spaceGrotesk(size: 16)
                        : SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold)
                )
                .foregroundStyle(stepColor(isCurrent: isCurrent, isCompleted: isCompleted, isBlocked: isBlocked))
                .lineSpacing(isCurrent ? 8 : 0)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityLabel(step.rawValue)
        .accessibilityAddTraits(isCurrent ? .isSelected : [])
    }

    private var isBlockedCurrentStep: Bool {
        if viewModel.acknowledgeFailedMessage != nil, viewModel.lifecycleStep == .signed {
            return true
        }
        if viewModel.reconcileFailedMessage != nil, viewModel.lifecycleStep == .acknowledged {
            return true
        }
        switch viewModel.blockingReason {
        case .malformed, .staleSnapshot, .admissionDenied, .rejectedAfterPrepare, .signerMissing:
            return true
        default:
            return false
        }
    }

    private func stepColor(isCurrent: Bool, isCompleted: Bool, isBlocked: Bool) -> Color {
        if isBlocked {
            return Color.growinRed
        }
        if isCurrent || isCompleted {
            return Color.brutalOffWhite
        }
        return Color.brutalOffWhite.opacity(0.3)
    }
}
