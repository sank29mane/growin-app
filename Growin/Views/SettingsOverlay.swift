import SwiftUI

struct SettingsOverlay: View {
    @Binding var isPresented: Bool
    @State private var selectedTab = 0
    
    var body: some View {
        ZStack {
            // Background Blur
            Color.black.opacity(0.4)
                .edgesIgnoringSafeArea(.all)
                .onTapGesture {withAnimation(.spring()) { isPresented = false }}
            
            // Pop-out Window
            VStack(spacing: 0) {
                // Header
                headerView
                
                // Content Switcher
                VStack(spacing: 0) {
                    // Navigation
                    HStack(spacing: 0) {
                        tabItem(title: "AI Model", icon: "cpu", index: 0)
                        tabItem(title: "Accounts", icon: "person.crop.circle", index: 1)
                        tabItem(title: "Activity Log", icon: "list.bullet.rectangle", index: 2)
                    }
                    .padding(.horizontal)
                    .background(Color.white.opacity(0.05))
                    
                    Divider().background(Color.secondary.opacity(0.2))
                    
                    // Main View
                    Group {
                        if selectedTab == 2 {
                            // Activity Log has its own ScrollView inside IntelligentConsoleView
                            IntelligentConsoleView()
                        } else {
                            ScrollView {
                                VStack(spacing: 24) {
                                    if selectedTab == 0 {
                                        AIConfigSection()
                                        HFModelHubSection()
                                        AgentPersonasSection()
                                    } else if selectedTab == 1 {
                                        TradingConfigSection()
                                        AccountStatusSection()
                                    }
                                }
                                .padding(24)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .frame(width: 800, height: 850)
            .background(.ultraThinMaterial)
            .cornerRadius(32)
            .overlay(
                RoundedRectangle(cornerRadius: 32)
                    .stroke(LinearGradient(colors: [.white.opacity(0.2), .clear], startPoint: .topLeading, endPoint: .bottomTrailing), lineWidth: 1)
            )
            .shadow(color: .black.opacity(0.5), radius: 40, x: 0, y: 20)
            .scaleEffect(isPresented ? 1 : 0.9)
            .opacity(isPresented ? 1 : 0)
        }
    }
    
    private var headerView: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                Text("Preferences")
                    .font(.title2)
                    .fontWeight(.semibold)
            }
            Spacer()
            Button(action: { withAnimation(.spring()) { isPresented = false } }) {
                Image(systemName: "xmark")
                    .font(.system(size: 14, weight: .bold))
                    .foregroundColor(.primary)
                    .padding(10)
                    .background(Circle().fill(Color.secondary.opacity(0.1)))
            }
            .buttonStyle(.plain)
        }
        .padding(24)
    }
    
    private func tabItem(title: String, icon: String, index: Int) -> some View {
        Button(action: { withAnimation(.interactiveSpring()) { selectedTab = index } }) {
            VStack(spacing: 12) {
                HStack(spacing: 6) {
                    Image(systemName: icon)
                        .font(.subheadline)
                    Text(title)
                        .font(.subheadline)
                        .fontWeight(.medium)
                }
                .foregroundColor(selectedTab == index ? .primary : .secondary)
                .frame(maxWidth: .infinity)
                .padding(.top, 16)
                
                // Indicator
                Rectangle()
                    .fill(selectedTab == index ? Color.green : Color.clear)
                    .frame(height: 2)
                    .padding(.horizontal, 20)
            }
        }
        .buttonStyle(.plain)
    }
}

#Preview {
    SettingsOverlay(isPresented: .constant(true))
}
