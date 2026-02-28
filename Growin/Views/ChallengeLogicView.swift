import SwiftUI

struct ChallengeLogicView: View {
    @Bindable var viewModel: AIStrategyViewModel
    @State private var challengeText: String = ""
    @Environment(\.dismiss) private var dismiss
    
    var body: some View {
        ZStack {
            MeshBackground()
            
            VStack(spacing: 24) {
                // Header
                AppHeader(
                    title: "Challenge Logic",
                    subtitle: "Correct AI reasoning trajectories",
                    icon: "brain.head.profile"
                )
                .padding(.horizontal)
                
                VStack(alignment: .leading, spacing: 16) {
                    Text("IDENTIFY DISCREPANCY")
                        .premiumTypography(.overline)
                    
                    TextEditor(text: $challengeText)
                        .frame(height: 150)
                        .padding()
                        .background(
                            RoundedRectangle(cornerRadius: 16)
                                .fill(Color.growinSurface.opacity(0.6))
                                .overlay(
                                    RoundedRectangle(cornerRadius: 16)
                                        .stroke(Color.white.opacity(0.1), lineWidth: 1)
                                )
                        )
                        .premiumTypography(.body)
                        .placeholder(when: challengeText.isEmpty) {
                            Text("e.g., 'The risk assessment ignores the upcoming rate decision' or 'Re-evaluate weighting for TSLA'...")
                                .premiumTypography(.body)
                                .foregroundStyle(Color.textTertiary)
                                .padding(.horizontal, 20)
                                .padding(.top, 25)
                        }
                    
                    Text("Challenging the logic will trigger an **R-Stitch Revision**, re-evaluating the strategy trajectory using LLM-augmented reasoning.")
                        .premiumTypography(.caption)
                        .foregroundStyle(Color.textSecondary)
                }
                .padding(.horizontal)
                
                Spacer()
                
                // Optimistic UI Status
                if let status = viewModel.optimisticStatus {
                    HStack {
                        ProgressView()
                            .tint(Color.stitchNeonIndigo)
                        Text(status)
                            .premiumTypography(.body)
                            .foregroundStyle(Color.stitchNeonIndigo)
                    }
                    .padding()
                    .background(Capsule().fill(Color.stitchNeonIndigo.opacity(0.1)))
                    .transition(.scale.combined(with: .opacity))
                }
                
                HStack(spacing: 16) {
                    Button("Cancel") { dismiss() }
                        .premiumTypography(.title)
                        .foregroundStyle(.secondary)
                    
                    PremiumButton(title: "Restitch Strategy", icon: "arrow.triangle.2.circlepath") {
                        Task {
                            await viewModel.challengeStrategy(challenge: challengeText)
                            if viewModel.errorMessage == nil {
                                dismiss()
                            }
                        }
                    }
                    .disabled(challengeText.isEmpty || viewModel.isStreaming)
                }
                .padding()
            }
        }
    }
}

// Helper for placeholder
extension View {
    func placeholder<Content: View>(
        when shouldShow: Bool,
        alignment: Alignment = .topLeading,
        @ViewBuilder placeholder: () -> Content) -> some View {

        ZStack(alignment: alignment) {
            placeholder().opacity(shouldShow ? 1 : 0)
            self
        }
    }
}
