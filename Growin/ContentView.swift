import SwiftUI

struct ContentView: View {
    @State private var selection: SidebarItem? = .chat
    @State private var showSettings = false
    @State private var backendStatus = BackendStatusViewModel.shared
    
    // Persistent State Objects (Hoisted)
    @State private var chatViewModel = ChatViewModel()
    @State private var goalPlannerViewModel = GoalPlannerViewModel()
    @State private var portfolioViewModel = PortfolioViewModel()
    @State private var dashboardViewModel = DashboardViewModel()
    
    enum SidebarItem: String, CaseIterable, Identifiable {
        case chat = "Intelligence"
        case dashboard = "Dashboard"
        case portfolio = "Portfolio"
        case charts = "Charts"
        case goalPlanner = "Goal Planner"
        
        var id: String { rawValue }
        
        var icon: String {
            switch self {
            case .chat: return "sparkles"
            case .dashboard: return "chart.bar.fill"
            case .portfolio: return "chart.pie.fill"
            case .charts: return "chart.xyaxis.line"
            case .goalPlanner: return "target"
            }
        }
        
        var color: Color {
            switch self {
            case .chat: return .growinAccent
            case .dashboard: return .growinPrimary
            case .portfolio: return .Persona.analyst
            case .charts: return .Persona.trader
            case .goalPlanner: return .Persona.risk
            }
        }
    }
    
    var body: some View {
        ZStack {
            NavigationSplitView {
                // Sidebar
                List(selection: $selection) {
                    Section("Intelligence") {
                        SidebarRow(item: .chat, selection: selection)
                    }
                    
                    Section("Accounts") {
                        SidebarRow(item: .dashboard, selection: selection)
                        SidebarRow(item: .portfolio, selection: selection)
                    }
                    
                    Section("Analysis") {
                        SidebarRow(item: .charts, selection: selection)
                        SidebarRow(item: .goalPlanner, selection: selection)
                    }
                }
                .navigationTitle("Growin")
                .listStyle(.sidebar)
            } detail: {
                // Main Content with Mesh Background
                ZStack {
                    MeshBackground()
                    
                    if let selection = selection {
                        detailViewFor(item: selection)
                            .transition(.opacity.combined(with: .move(edge: .bottom)))
                            .id(selection.id)
                    } else {
                        Text("Select an item")
                            .foregroundStyle(.secondary)
                    }
                }
                .toolbar {
                    ToolbarItem(placement: .primaryAction) {
                        settingsButton
                    }
                }
            }
            .frame(minWidth: 1000, minHeight: 700)
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("CreateChatFromChart"))) { _ in
                DispatchQueue.main.async {
                    withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) {
                        selection = .chat
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
        case .chat:
            ChatView(viewModel: chatViewModel)
        case .portfolio:
            PortfolioView(viewModel: portfolioViewModel)
        case .charts:
            ChartsView()
        case .goalPlanner:
            GoalPlannerView(viewModel: goalPlannerViewModel)
        case .dashboard:
            DashboardView(viewModel: dashboardViewModel)
        }
    }
    
    private var settingsButton: some View {
        Button(action: { withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) { showSettings = true } }) {
            HStack(spacing: 8) {
                Circle()
                    .fill(backendStatus.isOnline ? Color.growinGreen : Color.growinRed)
                    .frame(width: 8, height: 8)
                    .shadow(color: (backendStatus.isOnline ? Color.growinGreen : Color.growinRed).opacity(0.5), radius: 4)
                
                Image(systemName: "gearshape.fill")
                    .imageScale(.large)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(.thinMaterial)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .overlay(
                RoundedRectangle(cornerRadius: 12)
                    .stroke(Color.white.opacity(0.1), lineWidth: 0.5)
            )
        }
        .buttonStyle(.plain)
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
                Text(item.rawValue)
                    .font(.system(.body, design: .rounded))
                    .fontWeight(isSelected ? .bold : .medium)
            } icon: {
                Image(systemName: item.icon)
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(item.color)
                    .font(.system(size: 14, weight: .semibold))
            }
        }
    }
}

#Preview {
    ContentView()
}
