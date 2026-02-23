import SwiftUI

struct MeshBackground: View {
    @State private var animate = false
    
    var body: some View {
        ZStack {
            Color.growinDarkBg
            
            // Floating Mesh Blobs
            Group {
                Circle()
                    .fill(Color.meshIndigo)
                    .frame(width: 600, height: 600)
                    .offset(x: animate ? -100 : 100, y: animate ? 100 : -100)
                    .blur(radius: 100)
                
                Circle()
                    .fill(Color.meshEmerald.opacity(0.6))
                    .frame(width: 500, height: 500)
                    .offset(x: animate ? 200 : -200, y: animate ? -150 : 150)
                    .blur(radius: 120)
                
                Circle()
                    .fill(Color.growinAccent.opacity(0.3))
                    .frame(width: 400, height: 400)
                    .offset(x: animate ? -150 : 150, y: animate ? 200 : -200)
                    .blur(radius: 80)
            }
            .opacity(0.4)
            
            // Grain Texture Overlay
            Rectangle()
                .fill(.ultraThinMaterial)
                .opacity(0.2)
        }
        .onAppear {
            withAnimation(.easeInOut(duration: 15).repeatForever(autoreverses: true)) {
                animate.toggle()
            }
        }
        .ignoresSafeArea()
    }
}

struct GlassCard<Content: View>: View {
    let content: Content
    let cornerRadius: CGFloat
    @State private var isHovered = false
    
    init(cornerRadius: CGFloat = 20, @ViewBuilder content: () -> Content) {
        self.cornerRadius = cornerRadius
        self.content = content()
    }
    
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: cornerRadius)
                .fill(Color.white.opacity(0.03))
                .background(.ultraThinMaterial)
            
            content
                .padding()
        }
        .clipShape(RoundedRectangle(cornerRadius: cornerRadius))
        .overlay(
            RoundedRectangle(cornerRadius: cornerRadius)
                .stroke(
                    LinearGradient(
                        colors: [.white.opacity(0.2), .white.opacity(0.05), .white.opacity(0.2)],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    ),
                    lineWidth: 1
                )
        )
        .shadow(color: .black.opacity(0.25), radius: 15, x: 0, y: 10)
        .scaleEffect(isHovered ? 1.01 : 1.0)
        .onHover { hovering in
            withAnimation(.spring(response: 0.3, dampingFraction: 0.7)) {
                isHovered = hovering
            }
        }
    }
}

struct AppHeader: View {
    let title: String
    let subtitle: String
    var icon: String? = nil
    
    var body: some View {
        HStack(alignment: .bottom) {
            VStack(alignment: .leading, spacing: 4) {
                Text(title)
                    .font(.system(size: 34, weight: .bold, design: .rounded))
                    .foregroundStyle(.white)
                Text(subtitle)
                    .font(.system(size: 14, weight: .medium, design: .rounded))
                    .foregroundStyle(.white.opacity(0.6))
            }
            Spacer()
            
            if let icon = icon {
                GlassCard(cornerRadius: 12) {
                    Image(systemName: icon)
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(Color.growinPrimary)
                }
                .frame(width: 44, height: 44)
            }
        }
        .padding(.vertical)
    }
}

struct PremiumButton: View {
    let title: String
    var icon: String? = nil
    var color: Color = .growinPrimary
    let action: () -> Void
    
    var body: some View {
        Button(action: action) {
            HStack {
                if let icon = icon {
                    Image(systemName: icon)
                }
                Text(title)
                    .fontWeight(.bold)
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 10)
            .background(color)
            .clipShape(RoundedRectangle(cornerRadius: 12))
            .foregroundStyle(.white)
            .shadow(color: color.opacity(0.3), radius: 10, x: 0, y: 5)
        }
        .buttonStyle(.plain)
    }
}

// Reusing Existing Logic for PersonaIcon & MarkdownText with slight style tweaks
struct PersonaIcon: View {
    let name: String
    
    var body: some View {
        Image(systemName: iconName)
            .foregroundStyle(iconColor)
            .font(.system(size: 14, weight: .bold))
            .padding(8)
            .background(iconColor.opacity(0.1))
            .clipShape(Circle())
    }
    
    private var iconName: String {
        switch name {
        case "Portfolio Analyst": return "chart.pie.fill"
        case "Risk Manager": return "shield.fill"
        case "Technical Trader": return "chart.line.uptrend.xyaxis"
        case "Execution Specialist": return "bolt.fill"
        default: return "brain.head.profile"
        }
    }
    
    private var iconColor: Color {
        switch name {
        case "Portfolio Analyst": return Color.Persona.analyst
        case "Risk Manager": return Color.Persona.risk
        case "Technical Trader": return Color.Persona.trader
        case "Execution Specialist": return Color.Persona.execution
        default: return .white
        }
    }
}

struct MarkdownText: View {
    let content: String
    
    var body: some View {
        Text(LocalizedStringKey(content))
            .font(.system(.body, design: .rounded))
            .textSelection(.enabled)
    }
}

struct ErrorCard: View {
    let message: String
    let retryAction: () -> Void
    
    var body: some View {
        GlassCard(cornerRadius: 16) {
            VStack(spacing: 16) {
                HStack(spacing: 12) {
                    Image(systemName: "exclamationmark.octagon.fill")
                        .font(.system(size: 24))
                        .foregroundStyle(Color.growinRed)
                    
                    VStack(alignment: .leading, spacing: 4) {
                        Text("SIMULATION ERROR")
                            .font(.system(size: 10, weight: .black))
                            .tracking(1)
                            .foregroundStyle(Color.growinRed)
                        Text(message)
                            .font(.system(size: 13))
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                }
                
                Button(action: retryAction) {
                    HStack {
                        Image(systemName: "arrow.clockwise")
                        Text("RETRY PROTOCOL")
                    }
                    .font(.system(size: 11, weight: .bold))
                    .padding(.horizontal, 16)
                    .padding(.vertical, 8)
                    .background(Color.growinRed.opacity(0.1))
                    .clipShape(Capsule())
                    .foregroundStyle(Color.growinRed)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal)
    }
}

// MARK: - Safety Components

struct SlideToConfirm: View {
    let title: String
    let action: () -> Void
    
    @State private var offset: CGFloat = 0
    @State private var isConfirmed = false
    private let trackWidth: CGFloat = 300
    private let handleSize: CGFloat = 56
    
    var body: some View {
        ZStack {
            // Track
            Capsule()
                .fill(Color.white.opacity(0.05))
                .frame(width: trackWidth, height: handleSize)
                .overlay(
                    Text(title)
                        .font(.system(size: 12, weight: .bold))
                        .foregroundStyle(.white.opacity(0.3))
                )
            
            // Progress Fill
            HStack {
                Capsule()
                    .fill(Color.growinPrimary)
                    .frame(width: handleSize + offset, height: handleSize)
                Spacer()
            }
            .frame(width: trackWidth)
            
            // Handle
            HStack {
                ZStack {
                    Circle()
                        .fill(.white)
                        .frame(width: handleSize - 8, height: handleSize - 8)
                        .shadow(radius: 5)
                    
                    Image(systemName: isConfirmed ? "checkmark" : "chevron.right.2")
                        .foregroundStyle(Color.growinPrimary)
                        .font(.system(size: 18, weight: .black))
                }
                .offset(x: offset + 4)
                .gesture(
                    DragGesture()
                        .onChanged { value in
                            if !isConfirmed {
                                let newOffset = value.translation.width
                                if newOffset > 0 && newOffset < trackWidth - handleSize {
                                    offset = newOffset
                                }
                            }
                        }
                        .onEnded { value in
                            if !isConfirmed {
                                if offset > trackWidth * 0.7 {
                                    withAnimation(.spring()) {
                                        offset = trackWidth - handleSize
                                        isConfirmed = true
                                    }
                                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                                        action()
                                    }
                                } else {
                                    withAnimation(.spring()) {
                                        offset = 0
                                    }
                                }
                            }
                        }
                )
                Spacer()
            }
            .frame(width: trackWidth)
        }
    }
}

// MARK: - Glass Effect System

enum Glass {
    case thin
    case regular
    case thick
    case ultraThin
    
    var material: Material {
        switch self {
        case .thin: return .thinMaterial
        case .regular: return .regularMaterial
        case .thick: return .thickMaterial
        case .ultraThin: return .ultraThinMaterial
        }
    }
    
    func interactive() -> Glass {
        return self
    }
}

struct GlassEffect: ViewModifier {
    let style: Glass
    
    func body(content: Content) -> some View {
        content
            .background(style.material)
    }
}

extension View {
    func glassEffect(_ style: Glass) -> some View {
        modifier(GlassEffect(style: style))
    }
    
    func glassEffect<S: Shape>(_ style: Glass, in shape: S) -> some View {
        self.background(shape.fill(style.material))
    }
}
