import SwiftUI

struct ReasoningTraceView: View, Equatable {
    static func == (lhs: ReasoningTraceView, rhs: ReasoningTraceView) -> Bool {
        // Simple equatable implementation for view optimization
        return lhs.viewModel.strategy?.strategy_id == rhs.viewModel.strategy?.strategy_id &&
               lhs.viewModel.isStreaming == rhs.viewModel.isStreaming &&
               lhs.viewModel.streamingEvents.count == rhs.viewModel.streamingEvents.count
    }
    @Bindable var viewModel: AIStrategyViewModel
    @Environment(\.dismiss) private var dismiss
    
    var body: some View {
        ZStack {
            MeshBackground()
            
            VStack(spacing: 0) {
                // Header
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("REASONING TRACE")
                            .premiumTypography(.overline)
                        Text("AI Logic Trajectory")
                            .premiumTypography(.title)
                    }
                    Spacer()
                    Button(action: { dismiss() }) {
                        Image(systemName: "xmark.circle.fill")
                            .font(.title2)
                            .foregroundStyle(.secondary)
                    }
                }
                .padding()
                
                ScrollView {
                    VStack(spacing: 16) {
                        if viewModel.streamingEvents.isEmpty && !viewModel.isStreaming {
                            ContentUnavailableView("No Logic Traces", systemImage: "brain")
                        } else {
                            // Agent Activity Feed
                            VStack(alignment: .leading, spacing: 12) {
                                Text("AGENT ACTIVITY")
                                    .premiumTypography(.overline)
                                    .padding(.horizontal)
                                
                                ForEach(viewModel.streamingEvents, id: \.timestamp) { event in
                                    ReasoningChip(
                                        agent: event.agent,
                                        action: event.step?.action ?? event.status,
                                        isActive: event.status == "working"
                                    )
                                    .padding(.horizontal)
                                    .transition(.move(edge: .leading).combined(with: .opacity))
                                    .phaseAnimator([0, 1], trigger: event.status) { content, phase in
                                        content
                                            .opacity(event.status == "working" ? (phase == 0 ? 0.6 : 1.0) : 1.0)
                                            .offset(x: event.status == "working" ? (phase == 0 ? -2 : 2) : 0)
                                    }
                                }
                            }
                            
                            // Hierarchical Logic Tree
                            if let strategy = viewModel.strategy {
                                VStack(alignment: .leading, spacing: 12) {
                                    Text("STRATEGY LOGIC TREE")
                                        .premiumTypography(.overline)
                                        .padding(.horizontal)
                                    
                                    ForEach(strategy.reasoningTrace) { step in
                                        LogicTreeItem(
                                            title: step.action,
                                            content: step.content ?? "",
                                            isExpanded: true, // For demo, expand all
                                            onToggle: {}
                                        )
                                        .padding(.horizontal)
                                    }
                                }
                            }
                        }
                    }
                    .padding(.vertical)
                }
                
                // Explain-Back Loop (SOTA Pattern)
                VStack(spacing: 16) {
                    Divider().background(Color.white.opacity(0.1))
                    
                    VStack(alignment: .leading, spacing: 8) {
                        Text("EXPLAIN-BACK VERIFICATION")
                            .premiumTypography(.overline)
                            .foregroundStyle(Color.stitchNeonIndigo)
                        
                        Text("The AI interpreted your goal as 'Aggressive Alpha Recovery' by prioritizing high-conviction tech deltas. Does this align with your intent?")
                            .premiumTypography(.body)
                            .foregroundStyle(Color.textSecondary)
                    }
                    .padding()
                    .background(Color.growinSurface.opacity(0.6))
                    .clipShape(RoundedRectangle(cornerRadius: 16))
                    .padding(.horizontal)
                    
                    HStack(spacing: 12) {
                        PremiumButton(title: "Yes, Proceed", icon: "checkmark.circle.fill") {
                            // Action to implement or confirm
                        }
                        
                        PremiumButton(title: "No, Challenge", icon: "exclamationmark.bubble.fill", color: .growinRed) {
                            // Action to open ChallengeLogicView
                        }
                    }
                    .padding()
                }
                .background(.ultraThinMaterial)
            }
        }
    }
}
