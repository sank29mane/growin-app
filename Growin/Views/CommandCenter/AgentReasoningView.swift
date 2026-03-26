import SwiftUI

/// AgentReasoningView: Archival financial ledger for AI thought traces.
/// Follows Sovereign Ledger aesthetics (Phase 40).
struct AgentReasoningView: View {
    let events: [AgentEvent]
    let isStreaming: Bool
    
    var body: some View {
        SovereignContainer {
            VStack(alignment: .leading, spacing: 0) {
                // Header
                HStack {
                    Text("AGENT REASONING ARCHIVE")
                        .font(SovereignTheme.Fonts.notoSerif(size: 20))
                        .foregroundStyle(Color.brutalOffWhite)
                    
                    Spacer()
                    
                    if isStreaming {
                        HStack(spacing: 8) {
                            Circle()
                                .fill(Color.brutalChartreuse)
                                .frame(width: 6, height: 6)
                            Text("LIVE TRACE")
                                .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                                .foregroundStyle(Color.brutalChartreuse)
                        }
                    }
                }
                .padding()
                
                // Technical Ledger Header
                HStack(spacing: 0) {
                    Text("AGENT / STATUS")
                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 10))
                        .foregroundStyle(Color.brutalOffWhite.opacity(0.4))
                        .frame(width: 120, alignment: .leading)
                        .padding(.leading)
                    
                    Rectangle()
                        .fill(Color.white.opacity(0.15))
                        .frame(width: 1)
                    
                    Text("TECHNICAL LOGIC TRACE")
                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 10))
                        .foregroundStyle(Color.brutalOffWhite.opacity(0.4))
                        .padding(.leading)
                    
                    Spacer()
                }
                .frame(height: 30)
                .background(Color.brutalRecessed)
                .border(Color.white.opacity(0.15), width: 1)
                
                ScrollView {
                    VStack(spacing: 0) {
                        if events.isEmpty {
                            EmptyTraceView()
                        } else {
                            // Columnar Ledger Content
                            HStack(alignment: .top, spacing: 0) {
                                // Left Column: Agent Metadata
                                VStack(alignment: .leading, spacing: 20) {
                                    ForEach(events.indices, id: \.self) { index in
                                        EventMetadataView(event: events[index])
                                    }
                                    Spacer()
                                }
                                .frame(width: 120)
                                
                                // Strictly vertical line (Technical Border)
                                Rectangle()
                                    .fill(Color.white.opacity(0.15))
                                    .frame(width: 1)
                                
                                // Right Column: Content Trace
                                VStack(alignment: .leading, spacing: 20) {
                                    ForEach(events.indices, id: \.self) { index in
                                        EventContentView(event: events[index])
                                    }
                                    Spacer()
                                }
                            }
                            .padding(.vertical)
                        }
                    }
                }
            }
        }
    }
    
    private func formatTime(_ timestamp: Double) -> String {
        let date = Date(timeIntervalSince1970: timestamp)
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss.SSS"
        return formatter.string(from: date)
    }
}

private struct EventMetadataView: View {
    let event: AgentEvent
    
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(event.agent.uppercased())
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 9, weight: .bold))
                .foregroundStyle(Color.brutalChartreuse)
            
            Text(event.status.uppercased())
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 11))
                .foregroundStyle(Color.brutalOffWhite)
            
            Text(formatTime(event.timestamp))
                .font(SovereignTheme.Fonts.monacoTechnical(size: 8))
                .foregroundStyle(Color.brutalOffWhite.opacity(0.3))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 12)
    }
    
    private func formatTime(_ timestamp: Double) -> String {
        let date = Date(timeIntervalSince1970: timestamp)
        let formatter = DateFormatter()
        formatter.dateFormat = "HH:mm:ss.SSS"
        return formatter.string(from: date)
    }
}

private struct EventContentView: View {
    let event: AgentEvent
    
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let step = event.step, let content = step.content {
                Text(content)
                    .font(SovereignTheme.Fonts.monacoTechnical(size: 12))
                    .foregroundStyle(Color.brutalOffWhite)
                    .lineSpacing(4)
            } else {
                Text("SYSTEM::\(event.eventType.uppercased())")
                    .font(SovereignTheme.Fonts.monacoTechnical(size: 11))
                    .foregroundStyle(Color.brutalOffWhite.opacity(0.5))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 16)
    }
}

private struct EmptyTraceView: View {
    var body: some View {
        VStack {
            Spacer()
            Text("WAITING FOR ARCHIVAL INPUT...")
                .font(SovereignTheme.Fonts.monacoTechnical(size: 12))
                .foregroundStyle(Color.brutalOffWhite.opacity(0.3))
            Spacer()
        }
        .frame(maxWidth: .infinity, minHeight: 400)
    }
}

#Preview {
    AgentReasoningView(
        events: [
            AgentEvent(
                eventType: "thought",
                agent: "PORTFOLIO_ADVISOR",
                status: "analyzing",
                step: ReasoningStep(agent: "PORTFOLIO_ADVISOR", action: "analysis", content: "Evaluating current exposure to LSE:VUSA and comparing with global benchmark performance. Portfolio beta is currently 1.12, which is higher than target 1.0.", timestamp: Date().timeIntervalSince1970),
                timestamp: Date().timeIntervalSince1970
            ),
            AgentEvent(
                eventType: "action",
                agent: "EXECUTION_AGENT",
                status: "executing",
                step: ReasoningStep(agent: "EXECUTION_AGENT", action: "rebalance", content: "Generating buy/sell orders for 34 instruments to align with the new Sovereign Strategy. Target is 2% cash buffer.", timestamp: Date().timeIntervalSince1970 + 2.0),
                timestamp: Date().timeIntervalSince1970 + 2.0
            )
        ],
        isStreaming: true
    )
    .frame(height: 500)
    .padding()
}
