import SwiftUI
import Foundation

struct IntelligentConsoleView: View {
    @State private var backendStatus = BackendStatusViewModel.shared
    @State private var logs: [String] = []
    
    private let config = AppConfig.shared
    
    var systemStatus: GSystemStatus? { backendStatus.fullStatus?.system }
    var agents: [String: GAgentDetailedStatus]? { backendStatus.fullStatus?.agents }
    var specialistAgentsStatus: GSpecialistAgentsStatus? {
        guard let agents = agents else { return nil }
        return GSpecialistAgentsStatus(agents: agents)
    }
    
    var body: some View {
        ZStack {
            MeshBackground()
            
            ScrollView(showsIndicators: false) {
                VStack(spacing: 32) {
                    // Integrated Console Header
                    AppHeader(
                        title: "VITAL MONITOR",
                        subtitle: systemStatus?.status?.uppercased() ?? "OFFLINE",
                        icon: "bolt.fill"
                    )
                    .padding(.horizontal)
                    .padding(.top, 24)

                    if !backendStatus.isOnline {
                        PremiumButton(title: "BOOT ENGINE", icon: "power", color: .growinRed) {
                            backendStatus.launchBackend()
                        }
                        .padding(.horizontal)
                        .transition(.scale.combined(with: .opacity))
                    }

                    // Desktop Layout
                    HStack(alignment: .top, spacing: 24) {
                        VStack(alignment: .leading, spacing: 20) {
                            Text("SYSTEM METRICS")
                                .premiumTypography(.overline)
                            
                            metricsGrid
                        }
                        .frame(maxWidth: .infinity)
                        
                        VStack(alignment: .leading, spacing: 20) {
                            Text("AGENT CORE ARCHITECTURE")
                                .premiumTypography(.overline)
                                .padding(.horizontal)

                            VStack(spacing: 0) {
                                if let agents = agents {
                                    if let coord = agents["coordinator"] {
                                        AgentStatusBlock(name: "Coordinator", status: coord, icon: "cpu.fill")
                                        Divider().background(Color.secondary.opacity(0.2))
                                    }
                                    
                                    if let decision = agents["decision_agent"] {
                                        AgentStatusBlock(name: "Decision Agent", status: decision, icon: "brain.head.profile")
                                        Divider().background(Color.secondary.opacity(0.2))
                                    }
                                    
                                    // Specialists
                                    ForEach(agents.keys.sorted(), id: \.self) { key in
                                        if key != "coordinator" && key != "decision_agent" {
                                            if let status = agents[key] {
                                                AgentStatusBlock(name: key.replacingOccurrences(of: "_", with: " ").capitalized, 
                                                               status: status, 
                                                               icon: specialistIcon(for: key))
                                                Divider().background(Color.secondary.opacity(0.2))
                                            }
                                        }
                                    }
                                } else {
                                    ProgressView()
                                        .padding()
                                }
                            }
                            .glassEffect(.thin)
                            .cornerRadius(12)
                            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.secondary.opacity(0.2)))
                        }
                        .frame(maxWidth: .infinity)
                    }
                    
                    // Dynamic Architecture Diagram
                    DynamicArchitectureView(agents: specialistAgentsStatus)
                        .padding(.top, 8)
                    
                    // Live Logs Terminal
                    logSection
                }
                .padding()
            }
        }
        .onAppear(perform: fetchLogs)
        .navigationTitle("Intelligent Console")
    }
    
    private var metricsGrid: some View {
        LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 16) {
            MetricCard(title: "SERVER UPTIME", value: formatUptime(systemStatus?.uptime), icon: "clock.fill", color: .blue)
            MetricCard(title: "MEMORY ALLOC", value: "\(Int(systemStatus?.memoryMb ?? 0))MB", icon: "memorychip.fill", color: .purple)
            MetricCard(title: "MCP NODES", value: "\(systemStatus?.mcp?.serversCount ?? 0) ACTIVE", icon: "network", color: (systemStatus?.mcp?.connected ?? false) ? .green : .blue)
            MetricCard(title: "KERNEL THREAD_COUNT", value: "\(systemStatus?.activeThreads ?? 0)", icon: "terminal.fill", color: .orange)
        }
    }
    
    private func formatUptime(_ seconds: Double?) -> String {
        guard let seconds = seconds else { return "00:00:00" }
        let h = Int(seconds) / 3600
        let m = (Int(seconds) % 3600) / 60
        let s = Int(seconds) % 60
        return String(format: "%02d:%02d:%02d", h, m, s)
    }
    
    private var logSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("INTERNAL SYSTEM LOGS")
                    .font(.system(size: 12, weight: .bold, design: .monospaced))
                    .foregroundColor(.secondary)
                Spacer()
                
                HStack(spacing: 4) {
                    Circle().fill(.green).frame(width: 6, height: 6)
                    Text("LIVE").font(.system(size: 10, weight: .bold, design: .monospaced))
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(Color.green.opacity(0.1))
                .cornerRadius(4)
            }
            
            VStack(alignment: .leading, spacing: 4) {
                if logs.isEmpty {
                    Text("Awaiting log stream...")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundColor(.secondary)
                } else {
                    List {
                        ForEach(logs, id: \.self) { log in
                            Text(log)
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundColor(.green)
                                .listRowBackground(Color.clear)
                                .listRowInsets(EdgeInsets())
                        }
                    }
                    .listStyle(.plain)
                    .scrollContentBackground(.hidden)
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, minHeight: 250, alignment: .topLeading)
            .background(Color.black)
            .cornerRadius(12)
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.secondary.opacity(0.3)))
        }
    }
    
    private func specialistIcon(for key: String) -> String {
        switch key {
        case "quant_agent": return "chart.xyaxis.line"
        case "portfolio_agent": return "briefcase.fill"
        case "forecasting_agent": return "bolt.fill"
        case "research_agent": return "doc.text.magnifyingglass"
        case "social_agent": return "bubble.left.and.bubble.right.fill"
        case "whale_agent": return "water.waves"
        default: return "circle.grid.3x3.fill"
        }
    }

    private func fetchLogs() {
        guard let url = URL(string: "\(config.baseURL)/debug/logs") else { return }
        // Simple one-time fetch or polling for logs as before
        Task {
            do {
                let (data, _) = try await URLSession.shared.data(from: url)
                if let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let logLines = json["logs"] as? [String] {
                    await MainActor.run {
                        self.logs = logLines
                    }
                }
            } catch {
                print("Logs fetch error: \(error)")
            }
        }
    }
}

    

struct MetricCard: View {
    let title: String
    let value: String
    let icon: String
    let color: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Image(systemName: icon)
                    .foregroundColor(color)
                    .font(.system(size: 16))
                Text(title)
                    .premiumTypography(.overline)
            }

            Text(value)
                .premiumTypography(.title)
                .foregroundColor(.white)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .glassEffect(.thin)
        .cornerRadius(16)
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color.white.opacity(0.1), lineWidth: 1))
    }
}

struct AgentStatusBlock: View {
    let name: String
    let status: GAgentDetailedStatus
    let icon: String
    
    var body: some View {
        HStack(spacing: 16) {
            ZStack {
                Circle()
                    .fill(statusColor.opacity(0.1))
                    .frame(width: 40, height: 40)
                Image(systemName: icon)
                    .foregroundColor(statusColor)
                    .font(.system(size: 18))
            }
            
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(name)
                        .premiumTypography(.body)
                        .fontWeight(.bold)
                    if let model = status.model {
                        Text("(\(model))")
                            .premiumTypography(.caption)
                    }
                }
                
                Text(status.detail ?? "Idle")
                    .premiumTypography(.overline)
                    .foregroundColor(statusColor)
                    .lineLimit(1)
            }
            
            Spacer()
            
            VStack(alignment: .trailing, spacing: 2) {
                Text(status.status.uppercased())
                    .premiumTypography(.overline)
                    .foregroundColor(statusColor)
                
                if let model = status.model {
                    Text(model)
                        .premiumTypography(.overline)
                        .foregroundColor(.secondary)
                }
            }
        }
        .padding()
    }
    
    private var statusColor: Color {
        switch status.status.lowercased() {
        case "ready", "online": return Color.stitchNeonGreen
        case "working", "loading", "thinking": return Color.stitchNeonYellow
        case "error": return Color.growinRed
        default: return .secondary
        }
    }
}

struct StatusPulseLight: View {
    let isOnline: Bool
    @State private var animate = false
    
    var body: some View {
        ZStack {
            Circle()
                .fill(isOnline ? Color.green : Color.red)
                .frame(width: 12, height: 12)
            
            Circle()
                .stroke(isOnline ? Color.green : Color.red, lineWidth: 2)
                .frame(width: animate ? 24 : 12, height: animate ? 24 : 12)
                .opacity(animate ? 0 : 0.5)
        }
        .onAppear {
            withAnimation(Animation.easeInOut(duration: 1.5).repeatForever(autoreverses: false)) {
                animate = true
            }
        }
    }
}

// MARK: - Dynamic Architecture Diagram

struct DynamicArchitectureView: View {
    let agents: GSpecialistAgentsStatus?
    
    var body: some View {
        VStack(spacing: 20) {
            Text("REAL-TIME NEURAL PATHWAY")
                .font(.system(size: 10, weight: .bold, design: .monospaced))
                .foregroundColor(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
            
            VStack(spacing: 0) {
                // START: User Query
                NodeView(
                    title: "USER",
                    status: (agents?.coordinator?.status == "offline") ? "offline" : (isUserActive ? "working" : "ready"),
                    icon: "person.circle.fill",
                    isSmall: true
                )
                
                FlowLine(isActive: isUserActive)
                    .frame(height: 20)
                
                // Top: Coordinator
                NodeView(
                    title: "COORDINATOR",
                    status: agents?.coordinator?.status ?? "offline",
                    model: agents?.coordinator?.model,
                    icon: "cpu"
                )
                
                // Connector 1
                FlowLine(isActive: (agents?.coordinator?.status ?? "").lowercased() == "working" || (agents?.coordinator?.status ?? "").lowercased() == "online")
                    .frame(height: 30)
                
                // Middle: Specialists
                HStack(spacing: 8) {
                    if let specs = agents {
                        // Filter for specialists only
                        let keys = specs.keys.filter { $0 != "coordinator" && $0 != "decision_agent" }.sorted()
                        
                        ForEach(keys, id: \.self) { key in
                            if let s = specs[key] {
                                NodeView(
                                    title: key.replacingOccurrences(of: "_agent", with: "").uppercased(),
                                    status: s.status,
                                    icon: iconFor(key),
                                    isSmall: true
                                )
                            }
                        }
                    } else {
                        // Placeholder
                         ForEach(0..<4) { _ in
                             NodeView(title: "...", status: "offline", icon: "circle", isSmall: true)
                         }
                    }
                }
                
                // Connector 2 (Converging)
                FlowLine(isActive: (agents?.decisionAgent?.status ?? "").lowercased() == "working")
                    .frame(height: 30)
                
                // Bottom: Decision
                NodeView(
                    title: "DECISION CORE",
                    status: agents?.decisionAgent?.status ?? "offline",
                    model: agents?.decisionAgent?.model,
                    icon: "brain.head.profile"
                )
                
                FlowLine(isActive: (agents?.decisionAgent?.status ?? "").lowercased() == "ready")
                    .frame(height: 20)
                
                // END: User Response
                NodeView(
                    title: "USER",
                    status: (agents?.decisionAgent?.status == "ready") ? "working" : "offline",
                    icon: "checkmark.circle.fill",
                    isSmall: true
                )
            }
            .padding(.vertical)
        }
        .padding()
        .background(Color.white.opacity(0.03))
        .cornerRadius(16)
        .overlay(RoundedRectangle(cornerRadius: 16).stroke(Color.white.opacity(0.1)))
    }
    
    private var isUserActive: Bool {
        let status = (agents?.coordinator?.status ?? "").lowercased()
        return status == "online" || status == "working"
    }
    
    func iconFor(_ key: String) -> String {
        switch key {
        case "quant_agent": return "chart.bar"
        case "portfolio_agent": return "briefcase"
        case "forecasting_agent": return "arrow.up.right.circle"
        case "social_agent": return "bubble.left.and.bubble.right.fill"
        case "whale_agent": return "water.waves"
        default: return "doc.text.magnifyingglass"
        }
    }
}

struct NodeView: View {
    let title: String
    let status: String
    var model: String? = nil
    let icon: String
    var isSmall: Bool = false
    
    var isActive: Bool {
        ["working", "thinking", "analyzing", "generating utility text..."].contains(status.lowercased()) || status.lowercased().contains("working")
    }
    
    var prettyModel: String? {
        guard let m = model else { return nil }
        let low = m.lowercased()
        if low.contains("granite") { return "Granite 4.0 Tiny" }
        if low.contains("native-mlx") || low.contains("lfm") { return "LFM 2.5B (Native)" }
        if low.contains("mistral") { return "Mistral 7B" }
        if low.contains("gpt-4o") { return "GPT-4o" }
        if low.contains("claude") { return "Claude 3.5" }
        if low.contains("gemini") { return "Gemini 1.5" }
        return m
    }

    var statusColor: Color {
        if status.lowercased() == "offline" { return .gray.opacity(0.3) }
        return isActive ? .green : .blue
    }
    
    var body: some View {
        VStack(spacing: 6) {
            ZStack {
                Circle()
                    .fill(statusColor.opacity(0.15))
                    .frame(width: isSmall ? 40 : 60, height: isSmall ? 40 : 60)
                
                Image(systemName: icon)
                    .font(.system(size: isSmall ? 16 : 24))
                    .foregroundColor(statusColor)
            }
            .overlay(
                Circle()
                    .stroke(statusColor, lineWidth: isActive ? 2 : 0)
                    .scaleEffect(isActive ? 1.3 : 1.0)
                    .opacity(isActive ? 0 : 1)
                    .animation(isActive ? Animation.easeOut(duration: 1.5).repeatForever(autoreverses: false) : .default, value: isActive)
            )
            
            Text(title)
                .font(.system(size: isSmall ? 9 : 11, weight: .bold))
                .foregroundColor(isActive ? .white : .secondary)
            
            if let m = prettyModel, !isSmall {
                Text(m)
                    .font(.system(size: 10, weight: .bold, design: .monospaced))
                    .foregroundColor(.green)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Color.green.opacity(0.1))
                    .cornerRadius(4)
                    .overlay(RoundedRectangle(cornerRadius: 4).stroke(Color.green.opacity(0.3), lineWidth: 1))
            }
        }
        .frame(maxWidth: .infinity)
    }
}

struct FlowLine: View {
    let isActive: Bool
    
    var body: some View {
        Rectangle()
            .fill(isActive ? Color.green : Color.white.opacity(0.1))
            .frame(width: 2)
            .shadow(color: isActive ? .green : .clear, radius: 4)
    }
}
