import SwiftUI

struct ChatReasoningTraceView: View, Equatable {
    static func == (lhs: ChatReasoningTraceView, rhs: ChatReasoningTraceView) -> Bool {
        lhs.steps.count == rhs.steps.count && 
        lhs.isProcessing == rhs.isProcessing &&
        lhs.steps.last?.timestamp == rhs.steps.last?.timestamp
    }

    let steps: [ReasoningStep]
    let isProcessing: Bool
    @State private var isExpanded = false

    private let columns = [
        GridItem(.flexible(), spacing: 12),
        GridItem(.flexible(), spacing: 12)
    ]

    var body: some View {
        GlassCard(cornerRadius: 16) {
            VStack(alignment: .leading, spacing: 12) {
                // Header / Collapsed Trigger
                Button(action: {
                    withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) {
                        isExpanded.toggle()
                    }
                }) {
                    HStack {
                        Label(isProcessing ? "THINKING..." : "INTELLIGENCE VERIFIED", 
                              systemImage: isProcessing ? "brain.head.profile" : "shield.checkered")
                            .font(.system(size: 10, weight: .black))
                            .foregroundStyle(isProcessing ? .blue : .green)
                            .symbolEffect(.pulse, isActive: isProcessing)

                        if !isExpanded && steps.count > 0 {
                            Text("• \(steps.count) steps")
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                        }

                        Spacer()

                        Image(systemName: "chevron.down")
                            .font(.system(size: 10, weight: .bold))
                            .rotationEffect(.degrees(isExpanded ? 180 : 0))
                            .foregroundStyle(.secondary)
                    }
                }
                .buttonStyle(.plain)

                if isExpanded {
                    LazyVGrid(columns: columns, spacing: 12) {
                        ForEach(steps) { step in
                            ReasoningStepChip(step: step, isProcessing: isProcessing)
                                .transition(.asymmetric(
                                    insertion: .modifier(active: SlotModifier(offset: 20, opacity: 0), identity: SlotModifier(offset: 0, opacity: 1)),
                                    removal: .opacity
                                ))
                        }
                    }
                    .padding(.top, 4)
                } else if isProcessing && !steps.isEmpty {
                    // Show only the latest step when collapsed but processing
                    if let lastStep = steps.last {
                        ReasoningStepChip(step: lastStep, isProcessing: isProcessing)
                            .transition(.slotMachine)
                    }
                }
            }
            .padding(12)
        }
        .padding(.horizontal)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(isProcessing ? "AI Reasoning Trace: Thinking" : "AI Reasoning Trace: Verified")
        .accessibilityLiveRegion(.polite)
        .accessibilityAddTraits(.updatesFrequently)
    }
}

struct ReasoningStepChip: View, Equatable {
    static func == (lhs: ReasoningStepChip, rhs: ReasoningStepChip) -> Bool {
        lhs.step.id == rhs.step.id && lhs.isProcessing == rhs.isProcessing
    }

    let step: ReasoningStep
    let isProcessing: Bool

    var body: some View {
        if isProcessing && step.content == nil {
            // Processing/Indeterminate state with Metal-accelerated NPU Glow
            TimelineView(.animation) { timeline in
                let time = timeline.date.timeIntervalSinceReferenceDate
                chipContent
                    .background(
                        ZStack {
                            Color.black.opacity(0.3)
                            colorForAgent(step.agent)
                                .opacity(0.15)
                                .colorEffect(ShaderLibrary.npuGlow(.float(time)))
                        }
                    )
                    .cornerRadius(12)
                    .overlay(
                        RoundedRectangle(cornerRadius: 12)
                            .stroke(Color.white.opacity(0.15), lineWidth: 1)
                            .colorEffect(ShaderLibrary.npuGlow(.float(time)))
                    )
            }
        } else {
            // Static/Completed state
            chipContent
                .background(
                    ZStack {
                        Color.black.opacity(0.3)
                        LinearGradient(
                            colors: [colorForAgent(step.agent).opacity(0.15), .clear],
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
        }
    }

    private var chipContent: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: iconForAction(step.action))
                    .font(.system(size: 12))
                    .symbolEffect(.variableColor.iterative, isActive: isProcessing && step.content == nil)
                
                Text(step.agent.uppercased())
                    .font(.system(size: 9, weight: .black))
                    .kerning(1)
            }
            .foregroundStyle(colorForAgent(step.agent))

            Text(step.action)
                .font(.system(size: 11, weight: .bold))
                .foregroundStyle(.white)
                .lineLimit(1)

            if let content = step.content, !content.isEmpty {
                Text(content)
                    .font(.system(size: 9, design: .monospaced))
                    .foregroundStyle(.white.opacity(0.6))
                    .lineLimit(2)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func iconForAction(_ action: String) -> String {
        let lower = action.lowercased()
        if lower.contains("started") { return "play.circle.fill" }
        if lower.contains("finished") { return "checkmark.circle.fill" }
        if lower.contains("intent") { return "target" }
        if lower.contains("call") { return "wrench.and.screwdriver.fill" }
        if lower.contains("debat") { return "bubble.left.and.bubble.right.fill" }
        if lower.contains("think") { return "lightbulb.fill" }
        return "cpu"
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

// MARK: - Transitions & Modifiers

struct SlotModifier: ViewModifier {
    var offset: CGFloat
    var opacity: Double

    func body(content: Content) -> some View {
        content
            .offset(y: offset)
            .opacity(opacity)
    }
}

extension AnyTransition {
    static var slotMachine: AnyTransition {
        .asymmetric(
            insertion: .modifier(active: SlotModifier(offset: 20, opacity: 0), identity: SlotModifier(offset: 0, opacity: 1)),
            removal: .opacity
        )
    }
}
