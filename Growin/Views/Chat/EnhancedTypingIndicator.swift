import SwiftUI
import Combine

struct EnhancedTypingIndicator: View {
    var statusText: String = "Analyzing your request..."
    @State private var dotIndex = 0
    @State private var pulseScale: CGFloat = 1.0
    
    private let timer = Timer.publish(every: 0.4, on: .main, in: .common).autoconnect()
    
    var body: some View {
        HStack(spacing: 14) {
            ZStack {
                Circle()
                    .fill(Color.cyan.opacity(0.2))
                    .frame(width: 36, height: 36)
                    .scaleEffect(pulseScale)
                
                Image(systemName: "brain.head.profile")
                    .font(.system(size: 18))
                    .foregroundStyle(Color.cyan)
            }
            
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 4) {
                    ForEach(0..<3) { i in
                        Circle()
                            .fill(Color.cyan)
                            .frame(width: 7, height: 7)
                            .scaleEffect(dotIndex == i ? 1.3 : 1.0)
                            .opacity(dotIndex == i ? 1.0 : 0.4)
                    }
                }
                
                Text(statusText)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 12))
                    .foregroundStyle(Color.brutalOffWhite.opacity(0.6))
            }
            
            Spacer()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(
            RoundedRectangle(cornerRadius: 16)
                .fill(.ultraThinMaterial)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 16)
                .stroke(Color.white.opacity(0.05), lineWidth: 1)
        )
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(statusText)
        .onReceive(timer) { _ in
            withAnimation(.easeInOut(duration: 0.3)) {
                dotIndex = (dotIndex + 1) % 3
                pulseScale = pulseScale == 1.0 ? 1.1 : 1.0
            }
        }
    }
}

#Preview {
    ZStack {
        Color.black.ignoresSafeArea()
        EnhancedTypingIndicator()
            .padding()
    }
}
