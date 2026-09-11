import SwiftUI

struct PaperOperationsSessionCard: View {
    @Bindable var viewModel: PaperOperationsViewModel
    @State private var confirmStop = false

    var body: some View {
        SovereignCard {
            VStack(alignment: .leading, spacing: 16) {
                sessionChip
                instrumentPicker
                quantityField
                actionRow
                transientErrors
            }
        }
        .confirmationDialog(
            PaperOperationsCopy.stopDialogTitle,
            isPresented: $confirmStop,
            titleVisibility: .visible
        ) {
            Button(PaperOperationsCopy.stopDialogConfirm, role: .destructive) {
                Task { await viewModel.stopLocalReplay() }
            }
            Button(PaperOperationsCopy.stopDialogDismiss, role: .cancel) {}
        } message: {
            Text(PaperOperationsCopy.stopDialogBody)
        }
    }

    private var sessionChip: some View {
        let running = viewModel.sessionState == "RUNNING"
        return HStack(spacing: 8) {
            if running {
                Rectangle()
                    .fill(Color.brutalChartreuse)
                    .frame(width: 8, height: 8)
            }
            Text(running ? PaperOperationsCopy.chipLive : PaperOperationsCopy.chipStopped)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold))
                .foregroundStyle(Color.brutalOffWhite)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(running ? PaperOperationsCopy.chipLive : PaperOperationsCopy.chipStopped)
    }

    private var instrumentPicker: some View {
        VStack(alignment: .leading, spacing: 8) {
            Picker(selection: $viewModel.selectedInstrumentSymbol) {
                if viewModel.session.instruments.isEmpty {
                    Text(PaperOperationsCopy.pickerEmpty).tag(Optional<String>.none)
                } else {
                    Text(PaperOperationsCopy.pickerPrompt).tag(Optional<String>.none)
                    ForEach(viewModel.session.instruments, id: \.symbol) { instrument in
                        Text(instrument.symbol).tag(Optional(instrument.symbol))
                    }
                }
            } label: {
                Text(PaperOperationsCopy.pickerPrompt)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold))
            }
            .disabled(viewModel.session.instruments.isEmpty || viewModel.inFlightAction != nil)
        }
    }

    private var quantityField: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(PaperOperationsCopy.quantityLabel)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold))
                .foregroundStyle(Color.brutalOffWhite)
            TextField(PaperOperationsCopy.quantityLabel, text: $viewModel.quantity)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                .textFieldStyle(.plain)
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                .background(Color.brutalRecessed)
                .border(Color.white.opacity(0.15), width: 1)
                .foregroundStyle(Color.brutalOffWhite)
                .disabled(viewModel.inFlightAction != nil)
        }
    }

    private var actionRow: some View {
        let busy = viewModel.inFlightAction != nil
        let stopped = viewModel.sessionState == "STOPPED"
        let running = viewModel.sessionState == "RUNNING"
        let startEnabled = stopped && !busy
        let stopEnabled = running && !busy
        let refreshEnabled = !busy
        let loadEnabled = running && viewModel.selectedInstrumentSymbol != nil && !busy

        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 16) {
                PaperOperationsWorkflowButton(
                    title: PaperOperationsCopy.startLocalReplay,
                    inFlight: viewModel.inFlightAction == .start,
                    enabled: startEnabled,
                    accent: viewModel.accentedWorkflowAction == .start
                ) {
                    Task { await viewModel.startLocalReplay() }
                }
                .accessibilityHint(startEnabled ? "" : (viewModel.blockingReason?.copy ?? ""))

                PaperOperationsWorkflowButton(
                    title: PaperOperationsCopy.stopLocalReplay,
                    inFlight: viewModel.inFlightAction == .stop,
                    enabled: stopEnabled,
                    accent: false,
                    destructive: true
                ) {
                    confirmStop = true
                }
            }

            HStack(spacing: 16) {
                Button {
                    Task { await viewModel.refreshSessionStatus() }
                } label: {
                    HStack(spacing: 8) {
                        if viewModel.inFlightAction == .refresh {
                            ProgressView().controlSize(.small)
                        }
                        Text(PaperOperationsCopy.refreshSessionStatus)
                            .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                    }
                }
                .sovereignButtonStyle()
                .disabled(!refreshEnabled)
                .opacity(refreshEnabled ? 1 : 0.3)

                Button {
                    Task { await viewModel.loadSnapshotEvidence() }
                } label: {
                    HStack(spacing: 8) {
                        if viewModel.inFlightAction == .load {
                            ProgressView().controlSize(.small)
                        }
                        Text(PaperOperationsCopy.loadSnapshotEvidence)
                            .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                    }
                }
                .sovereignButtonStyle()
                .disabled(!loadEnabled)
                .opacity(loadEnabled ? 1 : 0.3)
                .accessibilityHint(loadEnabled ? "" : (viewModel.blockingReason?.copy ?? ""))
            }
        }
    }

    private var transientErrors: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let message = viewModel.startFailedMessage {
                Text(message)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                    .foregroundStyle(Color.growinRed)
            }
            if let message = viewModel.stopFailedMessage {
                Text(message)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                    .foregroundStyle(Color.growinRed)
            }
            if let message = viewModel.statusFailedMessage {
                Text(message)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                    .foregroundStyle(Color.growinRed)
            }
            if let message = viewModel.snapshotFailedMessage {
                Text(message)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                    .foregroundStyle(Color.growinRed)
            }
        }
    }
}
