import SwiftUI

/// MainTabView: The root navigation for the Sovereign UI.
/// Uses a macOS-style NavigationSplitView with a brutalist sidebar and 0px corners.
public struct MainTabView: View {
    @State private var selectedTab: SovereignTab = .ledger
    
    public enum SovereignTab: String, CaseIterable, Hashable {
        case ledger = "Ledger"
        case watchlist = "Watchlist"
        case execution = "Execution"
        case strategy = "Strategy"
        case reasoning = "Reasoning"
        
        var icon: String {
            switch self {
            case .ledger: return "wallet.pass"
            case .watchlist: return "eye"
            case .execution: return "hand.raised"
            case .strategy: return "brain.head.profile"
            case .reasoning: return "terminal"
            }
        }
    }
    
    public init() {}
    
    public var body: some View {
        NavigationSplitView {
            // MARK: - Brutalist Sidebar
            VStack(alignment: .leading, spacing: 0) {
                // Header / Branding
                VStack(alignment: .leading, spacing: 4) {
                    Text("SOVEREIGN")
                        .font(SovereignTheme.Fonts.notoSerif(size: 20, weight: .bold))
                        .italic()
                        .foregroundStyle(Color.brutalChartreuse)
                    
                    Text("ALPHA_COMMAND_CENTER")
                        .font(SovereignTheme.Fonts.monacoTechnical(size: 8))
                        .foregroundStyle(Color.white.opacity(0.4))
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 30)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.brutalRecessed)
                .overlay(
                    VStack {
                        Spacer()
                        Rectangle()
                            .fill(Color.white.opacity(0.1))
                            .frame(height: 1)
                    }
                )
                
                // Navigation List
                ScrollView {
                    VStack(spacing: 0) {
                        ForEach(SovereignTab.allCases, id: \.self) { tab in
                            SidebarButton(
                                icon: tab.icon,
                                label: tab.rawValue,
                                isActive: selectedTab == tab
                            ) {
                                selectedTab = tab
                            }
                        }
                    }
                    .padding(.vertical, 8)
                }
                
                Spacer()
                
                // System Status Footer
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 6) {
                        Circle()
                            .fill(Color.brutalChartreuse)
                            .frame(width: 6, height: 6)
                        Text("LSE_STATUS: OPEN")
                            .font(SovereignTheme.Fonts.monacoTechnical(size: 10))
                    }
                    
                    Text("LAST_SYNC: 14:32:01 GMT")
                        .font(SovereignTheme.Fonts.monacoTechnical(size: 8))
                        .foregroundStyle(Color.white.opacity(0.4))
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.brutalRecessed)
                .border(Color.white.opacity(0.1), width: 1)
            }
            .frame(minWidth: 240)
            .background(Color.brutalCharcoal)
            .toolbar(.hidden) // Hide standard sidebar title
        } detail: {
            // MARK: - Detail View Content
            ZStack {
                Color.brutalCharcoal.ignoresSafeArea()
                
                switch selectedTab {
                case .ledger:
                    MasterLedgerView()
                case .watchlist:
                    WatchlistView()
                case .execution:
                    ExecutionPanelView(isPresented: .constant(true), asset: nil)
                case .strategy:
                    StrategyLabView()
                case .reasoning:
                    AgentReasoningView(events: [], isStreaming: false)
                }
            }
            .transition(.identity) // Performant, sharp transitions
        }
        .navigationSplitViewStyle(.balanced)
    }
}

// MARK: - Subcomponents

private struct SidebarButton: View {
    let icon: String
    let label: String
    let isActive: Bool
    let action: () -> Void
    
    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Image(systemName: icon)
                    .font(.system(size: 16, weight: .medium))
                    .frame(width: 20)
                
                Text(label.uppercased())
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold))
                    .kerning(1.2)
                
                Spacer()
                
                if isActive {
                    Rectangle()
                        .fill(Color.brutalChartreuse)
                        .frame(width: 2, height: 16)
                }
            }
            .padding(.horizontal, 20)
            .frame(height: 48) // High-density
            .foregroundStyle(isActive ? Color.brutalChartreuse : Color.brutalOffWhite)
            .background(isActive ? Color.white.opacity(0.05) : Color.clear)
            .border(isActive ? Color.white.opacity(0.1) : Color.clear, width: 1) // 0px corners
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityAddTraits(isActive ? [.isSelected, .isButton] : [.isButton])
    }
}

// MARK: - Preview
#Preview {
    MainTabView()
        .preferredColorScheme(.dark)
}
