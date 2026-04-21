import SwiftUI

/// SuggestionItem: Data model for the suggestion chips in the welcome screen.
struct SuggestionItem: Identifiable {
    let id = UUID()
    let icon: String
    let title: String
    let prompt: String
}

/// WelcomeView: The high-fidelity empty state for the Growin AI Chat.
/// Follows SOTA patterns from ChatGPT and Claude with a focus on macOS Tahoe aesthetics.
struct WelcomeView: View {
    let onSuggestionTap: (String) -> Void
    
    private let suggestions = [
        SuggestionItem(icon: "📊", title: "Portfolio Overview", prompt: "Give me an overview of my portfolio performance"),
        SuggestionItem(icon: "🎯", title: "Tomorrow's Plays", prompt: "What trading opportunities do you see for tomorrow?"),
        SuggestionItem(icon: "📈", title: "ISA Account", prompt: "Analyze my ISA account performance"),
        SuggestionItem(icon: "💰", title: "Invest Account", prompt: "How is my Invest account doing?"),
        SuggestionItem(icon: "⚠️", title: "Risk Check", prompt: "Evaluate my portfolio risk exposure"),
        SuggestionItem(icon: "📉", title: "Market Outlook", prompt: "What's the market outlook for this week?")
    ]
    
    var body: some View {
        VStack(spacing: 40) {
            // SOTA Header Section
            VStack(spacing: 16) {
                ZStack {
                    Circle()
                        .fill(Color.cyan.opacity(0.15))
                        .frame(width: 80, height: 80)
                        .blur(radius: 20)
                    
                    Image(systemName: "brain.head.profile")
                        .font(.system(size: 48, weight: .light))
                        .foregroundStyle(
                            LinearGradient(
                                colors: [.brutalChartreuse, .cyan],
                                startPoint: .topLeading,
                                endPoint: .bottomTrailing
                            )
                        )
                        .shadow(color: Color.cyan.opacity(0.3), radius: 10)
                }
                
                VStack(spacing: 8) {
                    Text("Growin Intelligence")
                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 28, weight: .bold))
                        .foregroundStyle(Color.brutalOffWhite)
                    
                    Text("Your SOTA macOS 2026 Trading Companion")
                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                        .foregroundStyle(Color.brutalOffWhite.opacity(0.6))
                }
            }
            
            // Suggestion Chips Grid
            VStack(alignment: .leading, spacing: 16) {
                Text("Suggested Actions")
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold))
                    .foregroundStyle(Color.brutalOffWhite.opacity(0.4))
                    .kerning(1.2)
                    .padding(.leading, 4)
                
                LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible(), spacing: 12)], spacing: 12) {
                    ForEach(suggestions) { item in
                        SuggestionChip(item: item) {
                            onSuggestionTap(item.prompt)
                        }
                    }
                }
            }
            .frame(maxWidth: 600)
            .padding(.horizontal, 24)
        }
    }
}

/// SuggestionChip: A Tahoe-style "Liquid Glass" button for quick actions.
struct SuggestionChip: View {
    let item: SuggestionItem
    let action: () -> Void
    
    @State private var isHovered = false
    
    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Text(item.icon)
                    .font(.system(size: 18))
                
                Text(item.title)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 14, weight: .medium))
                    .foregroundStyle(Color.brutalOffWhite.opacity(0.9))
                
                Spacer()
                
                Image(systemName: "arrow.up.right")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(Color.brutalOffWhite.opacity(0.3))
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
            .background(
                RoundedRectangle(cornerRadius: 12)
                    .fill(isHovered ? Color.white.opacity(0.08) : Color.white.opacity(0.04))
                    .background(.ultraThinMaterial)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 12)
                    .stroke(
                        isHovered ? Color.cyan.opacity(0.4) : Color.white.opacity(0.1),
                        lineWidth: 1
                    )
            )
            .scaleEffect(isHovered ? 1.015 : 1.0)
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(item.title). \(item.prompt)")
        .accessibilityAddTraits(.isButton)
        .onHover { isHovered = $0 }
        .animation(.spring(response: 0.2, dampingFraction: 0.7), value: isHovered)
    }
}

#Preview {
    ZStack {
        Color.black.ignoresSafeArea()
        WelcomeView(onSuggestionTap: { _ in })
    }
    .frame(width: 800, height: 800)
}
