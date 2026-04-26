import SwiftUI

struct ChatReasoningTraceView: View {
    let steps: [ReasoningStep]
    let isProcessing: Bool

    var body: some View {
        GlassCard(cornerRadius: 16) {
            VStack(alignment: .leading, spacing: 12) {
                // Header
                HStack {
                    Label("AGENTIC TRACE", systemImage: "brain.head.profile")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(.secondary)

                    Spacer()

                    if isProcessing {
                        ProgressView()
                            .scaleEffect(0.6)
                            .tint(.blue)
                    } else {
                        Image(systemName: "checkmark.circle.fill")
                            .font(.system(size: 12))
                            .foregroundStyle(.green)
                    }
                }

                // Content Stream
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(alignment: .center, spacing: 12) {
                        ForEach(steps) { step in
                            ReasoningStepChip(step: step)
                                .transition(.move(edge: .bottom).combined(with: .opacity))
                        }
                    }
                    .padding(.bottom, 4)
                }
                // Animate smoothly when new items arrive
                .animation(.spring(response: 0.4, dampingFraction: 0.7), value: steps.count)
            }
        }
        .padding(.horizontal)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(isProcessing ? "AI Reasoning Trace: Thinking" : "AI Reasoning Trace: Verified")
        .accessibilityAddTraits(.updatesFrequently)
    }
}

struct ReasoningStepChip: View {
    let step: ReasoningStep
    @State private var appearOffset: CGFloat = 10

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 4) {
                Image(systemName: iconForAction(step.action))
                    .font(.system(size: 10))
                Text(step.agent.uppercased())
                    .font(.system(size: 9, weight: .bold))
            }
            .foregroundStyle(colorForAgent(step.agent))

            Text(step.action)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.white)

            if let content = step.content, !content.isEmpty {
                Text(content)
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.7))
                    .lineLimit(2)
                    .frame(maxWidth: 160, alignment: .leading)
            }
        }
        .padding(10)
        .background(
            ZStack {
                Color.black.opacity(0.2)
                LinearGradient(
                    colors: [colorForAgent(step.agent).opacity(0.2), .clear],
                    startPoint: .topLeading,
                    endPoint: .bottomTrailing
                )
            }
        )
        .cornerRadius(12)
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.white.opacity(0.1), lineWidth: 1)
        )
        .offset(y: appearOffset)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(step.agent) agent: \(step.action). \(step.content ?? "")")
        .onAppear {
            withAnimation(.spring(response: 0.5, dampingFraction: 0.6)) {
                appearOffset = 0
            }
        }
    }

    private func iconForAction(_ action: String) -> String {
        let lower = action.lowercased()
        if lower.contains("started") { return "play.circle.fill" }
        if lower.contains("finished") { return "checkmark.circle.fill" }
        if lower.contains("intent") { return "target" }
        if lower.contains("call") { return "wrench.and.screwdriver.fill" }
        if lower.contains("debat") { return "bubble.left.and.bubble.right.fill" }
        if lower.contains("think") { return "lightbulb.fill" }
        return "circle.fill"
    }

    private func colorForAgent(_ agent: String) -> Color {
        let lower = agent.lowercased()
        if lower.contains("portfolio") { return .green }
        if lower.contains("risk") { return .red }
        if lower.contains("trader") { return .orange }
        if lower.contains("research") { return .indigo }
        if lower.contains("coordinator") { return .cyan }
        return .blue
    }
}
