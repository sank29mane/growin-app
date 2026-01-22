import SwiftUI

struct ContentView: View {
    @State private var selection: SidebarItem = .chat
    @State private var showSettings = false
    @StateObject private var backendStatus = BackendStatusViewModel.shared
    
    // Persistent State Objects (Hoisted)
    @StateObject private var chatViewModel = ChatViewModel()
    @StateObject private var goalPlannerViewModel = GoalPlannerViewModel()
    @StateObject private var portfolioViewModel = PortfolioViewModel()
    
    enum SidebarItem: String, CaseIterable, Identifiable {
        case chat = "Chat"
        case portfolio = "Portfolio"
        case charts = "Charts"
        case goalPlanner = "Goal Planner"
        case dashboard = "Dashboard"
        
        var id: String { rawValue }
        
        var icon: String {
            switch self {
            case .chat: return "bubble.left.and.bubble.right.fill"
            case .portfolio: return "chart.pie.fill"
            case .charts: return "chart.xyaxis.line"
            case .goalPlanner: return "target"
            case .dashboard: return "chart.bar.fill"
            }
        }
    }
    
    var body: some View {
        ZStack {
            NavigationSplitView {
                // Sidebar
                List(SidebarItem.allCases, selection: $selection) { item in
                    NavigationLink(value: item) {
                        Label(item.rawValue, systemImage: item.icon)
                    }
                }
                .navigationTitle("Growin")
                .listStyle(.sidebar)
            } detail: {
                // Main Content
                detailView
                    .toolbar {
                        ToolbarItem(placement: .primaryAction) {
                            settingsButton
                        }
                    }
            }
            .frame(minWidth: 900, minHeight: 600)
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("CreateChatFromChart"))) { _ in
                DispatchQueue.main.async {
                    withAnimation(.spring()) {
                        selection = .chat
                    }
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("NavigateToTab"))) { notification in
                // Prevent navigation if in strict workflow
                if goalPlannerViewModel.isLoading { return }

                if let tabString = notification.userInfo?["tab"] as? String,
                   let item = SidebarItem.allCases.first(where: { $0.rawValue.lowercased() == tabString.lowercased() }) {
                    DispatchQueue.main.async {
                        withAnimation(.spring()) {
                            selection = item
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
    private var detailView: some View {
        switch selection {
        case .chat:
            ChatView(viewModel: chatViewModel)
        case .portfolio:
            PortfolioView(viewModel: portfolioViewModel)
        case .charts:
            ChartsView()
        case .goalPlanner:
            GoalPlannerView(viewModel: goalPlannerViewModel)
        case .dashboard:
            DashboardView()
        }
    }
    
    private var settingsButton: some View {
        Button(action: { withAnimation(.spring()) { showSettings = true } }) {
            HStack(spacing: 8) {
                // Connection Light
                Circle()
                    .fill(backendStatus.isOnline ? Color.green : Color.red)
                    .frame(width: 8, height: 8)
                    .shadow(color: (backendStatus.isOnline ? Color.green : Color.red).opacity(0.5), radius: 4)
                
                Image(systemName: "gearshape.fill")
                    .imageScale(.large)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(Color.secondary.opacity(0.1))
            .cornerRadius(12)
        }
        .buttonStyle(.plain)
        .help("System Settings & Console")
    }
}

#Preview {
    ContentView()
}
