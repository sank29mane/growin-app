import SwiftUI

// MARK: - Sovereign Container

/// A fundamental layout container for the Sovereign design system.
/// Enforces tonal layering and strictly 0px corners.
public struct SovereignContainer<Content: View>: View {
    let content: Content
    
    public init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }
    
    public var body: some View {
        ZStack {
            // Base Layer: Recessed
            Rectangle()
                .fill(Color.brutalRecessed)
            
            // Content Layer with Technical Border
            content
                .background(Color.brutalCharcoal)
                .border(Color.white.opacity(0.15), width: 1)
        }
    }
}

// MARK: - Sovereign Card

/// Implements the "Asymmetric Depth" pattern for Phase 40.
/// Uses hard offsets and 1pt borders instead of shadows.
public struct SovereignCard<Content: View>: View {
    let content: Content
    
    public init(@ViewBuilder content: () -> Content) {
        self.content = content()
    }
    
    public var body: some View {
        ZStack {
            // Shadow / Depth Layer (Hard Offset)
            Rectangle()
                .fill(Color.black)
                .offset(x: 2, y: 2)
            
            // Primary Card Surface
            content
                .padding()
                .background(Color.brutalCharcoal)
                .border(Color.white.opacity(0.15), width: 1)
        }
    }
}

// MARK: - Sovereign Button Style

/// A ButtonStyle that uses hard offsets and sharp geometry.
/// Replaces "Liquid Glass" animations with technical shifts.
public struct SovereignButtonStyle: ButtonStyle {
    public func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(
                Rectangle()
                    .fill(configuration.isPressed ? Color.brutalRecessed : Color.brutalCharcoal)
            )
            .border(Color.white.opacity(0.15), width: 1)
            .offset(x: configuration.isPressed ? 2 : 0, y: configuration.isPressed ? 2 : 0)
            .foregroundStyle(Color.brutalOffWhite)
    }
}

// MARK: - Extensions

extension View {
    /// Wraps the view in a Sovereign Card with asymmetric depth.
    func sovereignCard() -> some View {
        SovereignCard { self }
    }
    
    /// Applies the Sovereign Button Style to any button.
    func sovereignButtonStyle() -> some View {
        self.buttonStyle(SovereignButtonStyle())
    }
}
