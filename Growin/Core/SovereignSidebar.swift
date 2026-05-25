import SwiftUI

/// SovereignSidebar: Persistent sidebar navigation for the macOS Sovereign experience.
/// Implements high-density icons and technical mono labels.
public struct SovereignSidebar: View {
    @Binding var selectedTab: SidebarTab
    
    public var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            // Branding: The "Sovereign" Header
            VStack(alignment: .leading, spacing: 4) {
                Text("GROWIN")
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 14, weight: .bold))
                    .foregroundStyle(Color.brutalChartreuse)
                Text("ALPHA v1.30")
                    .font(SovereignTheme.Fonts.monacoTechnical(size: 10))
                    .foregroundStyle(Color.brutalOffWhite.opacity(0.4))
            }
            .padding(.bottom, 24)
            
            // Navigation Links
            Group {
                SidebarLink(icon: "message.fill", title: "CHAT", tab: .chat, selection: $selectedTab)
                SidebarLink(icon: "terminal", title: "CONSOLE", tab: .console, selection: $selectedTab)
                SidebarLink(icon: "piechart", title: "PORTFOLIO", tab: .portfolio, selection: $selectedTab)
                SidebarLink(icon: "chart.xyaxis.line", title: "WATCHLIST", tab: .watchlist, selection: $selectedTab)
                SidebarLink(icon: "cpu", title: "STRATEGY", tab: .strategy, selection: $selectedTab)
                SidebarLink(icon: "chart.bar", title: "ANALYTICS", tab: .analytics, selection: $selectedTab)
            }
            
            Spacer()
            
            // Footnote: Archival Meta
            VStack(alignment: .leading, spacing: 4) {
                Text("MARKET: LONDON")
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 10))
                    .foregroundStyle(Color.brutalChartreuse.opacity(0.8))
                Text("AGENT: JULES-01")
                    .font(SovereignTheme.Fonts.monacoTechnical(size: 8))
                    .foregroundStyle(Color.brutalOffWhite.opacity(0.3))
            }
        }
        .padding(20)
        .frame(minWidth: 180, maxWidth: 240)
        .background(Color.brutalRecessed)
        .border(SovereignTheme.Colors.technicalBorder, width: 1) // Sharp edges
    }
}

private struct SidebarLink: View {
    let icon: String
    let title: String
    let tab: SidebarTab
    @Binding var selection: SidebarTab
    
    var body: some View {
        Button {
            selection = tab
        } label: {
            HStack(spacing: 12) {
                Image(systemName: icon)
                    .font(.system(size: 14))
                    .foregroundStyle(selection == tab ? Color.brutalChartreuse : Color.brutalOffWhite.opacity(0.6))
                    .frame(width: 20)
                
                Text(title)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: selection == tab ? .bold : .regular))
                    .foregroundStyle(selection == tab ? Color.brutalOffWhite : Color.brutalOffWhite.opacity(0.6))
                
                Spacer()
            }
            .padding(.vertical, 8)
            .padding(.horizontal, 12)
            .background(selection == tab ? Color.white.opacity(0.05) : Color.clear)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(title) Tab")
        .accessibilityAddTraits(selection == tab ? [.isSelected, .isButton] : [.isButton])
        .accessibilityHint("Switches to the \(title) section")
    }
}

public enum SidebarTab: String, CaseIterable, Identifiable {
    case chat, console, portfolio, watchlist, strategy, analytics
    public var id: String { self.rawValue }
}

#Preview {
    ZStack {
        Color.black.ignoresSafeArea()
        SovereignSidebar(selectedTab: .constant(.portfolio))
    }
}
