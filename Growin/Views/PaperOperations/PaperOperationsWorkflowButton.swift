import SwiftUI

struct PaperOperationsWorkflowButtonStyle: ButtonStyle {
    var enabled: Bool
    var accent: Bool
    var destructive: Bool = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .foregroundStyle(foreground)
            .background(Color.brutalRecessed)
            .border(Color.white.opacity(0.15), width: 1)
    }

    private var foreground: Color {
        if !enabled {
            return Color.brutalOffWhite.opacity(0.3)
        }
        if accent {
            return Color.brutalChartreuse
        }
        if destructive {
            return Color.growinRed
        }
        return Color.brutalOffWhite
    }
}

struct PaperOperationsWorkflowButton: View {
    let title: String
    var inFlight: Bool
    var enabled: Bool
    var accent: Bool
    var destructive: Bool = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                if inFlight {
                    ProgressView()
                        .controlSize(.small)
                }
                Text(title)
            }
        }
        .buttonStyle(
            PaperOperationsWorkflowButtonStyle(
                enabled: enabled,
                accent: accent,
                destructive: destructive
            )
        )
        .disabled(!enabled)
        .accessibilityLabel(title)
        .accessibilityAddTraits(.isButton)
    }
}
