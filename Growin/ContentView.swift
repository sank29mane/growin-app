import SwiftUI

struct ContentView: View {
    @State private var selection: SidebarItem? = .alphaCommand
    @State private var showSettings = false
    @State private var showChat = true
    @State private var backendStatus = BackendStatusViewModel.shared
    
    // Persistent State Objects (Hoisted)
    @State private var strategyViewModel = AIStrategyViewModel()
    @State private var portfolioViewModel = PortfolioViewModel()
    @State private var dashboardViewModel = DashboardViewModel()
    @State private var goalPlannerViewModel = GoalPlannerViewModel()
    @State private var chatViewModel = ChatViewModel()
    
    enum SidebarItem: String, CaseIterable, Identifiable {
        case alphaCommand = "Alpha Command"
        case reasoning = "Agent Reasoning"
        case calibration = "Strategy Lab"
        case portfolio = "Portfolio"
        case charts = "Charts"
        case goalPlanner = "Goal Planner"
        case chat = "AI Assistant"
        
        var id: String { rawValue }
        
        var icon: String {
            switch self {
            case .alphaCommand: return "terminal.fill"
            case .reasoning: return "brain.head.profile"
            case .calibration: return "slider.horizontal.3"
            case .portfolio: return "chart.pie.fill"
            case .charts: return "chart.xyaxis.line"
            case .goalPlanner: return "target"
            case .chat: return "bubble.left.and.bubble.right.fill"
            }
        }
        
        var color: Color {
            switch self {
            case .alphaCommand: return .brutalChartreuse
            case .reasoning: return .brutalOffWhite
            case .calibration: return .brutalChartreuse
            case .portfolio: return .Persona.analyst
            case .charts: return .Persona.trader
            case .goalPlanner: return .Persona.risk
            case .chat: return .cyan
            }
        }
    }
    
    var body: some View {
        ZStack {
            // Global Background: Brutal Recessed
            Color.brutalRecessed.ignoresSafeArea()
            
            NavigationSplitView {
                // Sidebar: Sovereign Style
                List(selection: $selection) {
                    Section("Sovereign Alpha") {
                        SidebarRow(item: .alphaCommand, selection: selection)
                        SidebarRow(item: .reasoning, selection: selection)
                        SidebarRow(item: .calibration, selection: selection)
                    }
                    
                    Section("Intelligence & Accounts") {
                        SidebarRow(item: .portfolio, selection: selection)
                        SidebarRow(item: .charts, selection: selection)
                        SidebarRow(item: .goalPlanner, selection: selection)
                    }
                    
                    Section("Communication") {
                        SidebarRow(item: .chat, selection: selection)
                    }
                }
                .navigationTitle("GROWIN ALPHA")
                .listStyle(.sidebar)
                .background(Color.brutalRecessed)
            } detail: {
                // Main Content: No Mesh, Just Sovereign Container
                ZStack {
                    if let selection = selection {
                        detailViewFor(item: selection)
                            .transition(.opacity.combined(with: .move(edge: .bottom)))
                            .id(selection.id)
                    } else {
                        Text("SELECT AN OPERATION")
                            .font(SovereignTheme.Fonts.monacoTechnical(size: 14))
                            .foregroundStyle(Color.brutalOffWhite.opacity(0.3))
                    }
                }
                .toolbar {
                    ToolbarItem(placement: .primaryAction) {
                        HStack(spacing: 16) {
                            chatToggleButton
                            settingsButton
                        }
                    }
                }
                .inspector(isPresented: $showChat) {
                    ChatView(viewModel: chatViewModel)
                        .inspectorColumnWidth(min: 300, ideal: 350, max: 450)
                }
            }
            .frame(minWidth: 1200, minHeight: 750)
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("CreateChatFromChart"))) { _ in
                DispatchQueue.main.async {
                    withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) {
                        showChat = true
                    }
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("NavigateToTab"))) { notification in
                if let tab = notification.userInfo?["tab"] as? String {
                    DispatchQueue.main.async {
                        withAnimation(.spring()) {
                            switch tab {
                            case "portfolio": selection = .portfolio
                            case "chat": 
                                selection = .chat
                                showChat = true
                            default: break
                            }
                        }
                    }
                }
            }
            
            // Global Settings Overlay
            if showSettings {
                SettingsOverlay(isPresented: $showSettings)
                    .transition(.opacity.combined(with: .scale(scale: 0.95)))
                    .zIndex(100)
            }
        }
    }
    
    @ViewBuilder
    private func detailViewFor(item: SidebarItem) -> some View {
        switch item {
        case .alphaCommand:
            AlphaCommandDashboard()
        case .reasoning:
            AgentReasoningView(
                events: strategyViewModel.streamingEvents,
                isStreaming: strategyViewModel.isStreaming
            )
        case .calibration:
            StrategyLabView()
        case .portfolio:
            PortfolioView(viewModel: portfolioViewModel)
        case .charts:
            ChartsView()
        case .goalPlanner:
            GoalPlannerView(viewModel: goalPlannerViewModel)
        case .chat:
            ChatView(viewModel: chatViewModel)
        }
    }
    
    private var chatToggleButton: some View {
        Button(action: { withAnimation(.spring()) { showChat.toggle() } }) {
            Image(systemName: "sidebar.right")
                .foregroundStyle(showChat ? Color.brutalChartreuse : Color.brutalOffWhite)
        }
        .help("Toggle AI Assistant")
    }
    
    private var settingsButton: some View {
        Button(action: { withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) { showSettings = true } }) {
            HStack(spacing: 8) {
                Circle()
                    .fill(backendStatus.isOnline ? Color.brutalChartreuse : Color.red)
                    .frame(width: 8, height: 8)
                
                Text("SYSTEM")
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                
                Image(systemName: "terminal")
                    .imageScale(.small)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(Color.brutalCharcoal)
            .border(Color.white.opacity(0.1), width: 1)
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("System Settings, backend \(backendStatus.isOnline ? "online" : "offline")")
        .accessibilityAddTraits(.isButton)
        .help("System Settings & Console")
    }
}

struct SidebarRow: View {
    let item: ContentView.SidebarItem
    let selection: ContentView.SidebarItem?
    
    var isSelected: Bool { selection == item }
    
    var body: some View {
        NavigationLink(value: item) {
            Label {
                Text(item.rawValue.uppercased())
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: isSelected ? .bold : .regular))
                    .foregroundStyle(isSelected ? Color.brutalChartreuse : Color.brutalOffWhite)
            } icon: {
                Image(systemName: item.icon)
                    .foregroundStyle(isSelected ? Color.brutalChartreuse : item.color.opacity(0.6))
                    .font(.system(size: 12))
            }
        }
        .listRowBackground(isSelected ? Color.white.opacity(0.05) : Color.clear)
    }
}

#Preview {
    ContentView()
}
