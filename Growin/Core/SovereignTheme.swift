import SwiftUI

/// SovereignTheme: The "Brutal Editorial" foundation for Phase 40.
/// Enforces "Authority through Absence" with 0px corners and tonal layering.
public enum SovereignTheme {
    
    // MARK: - Palette (Authority through Absence)
    // Hex values strictly follow the Sovereign Alpha Command Center research.
    
    public enum Colors {
        public static let brutalCharcoal = Color(hex: "121212")
        public static let brutalRecessed = Color(hex: "0A0A0A")
        public static let brutalOffWhite = Color(hex: "F5F5F7")
        public static let brutalChartreuse = Color(hex: "DFFF00") // The "Acid" accent
        public static let technicalBorder = Color.white.opacity(0.15)
    }
    
    // MARK: - Typography (Technical & Wealth)
    
    public enum Fonts {
        /// Noto Serif: Used for Wealth & Authority (headers, display text).
        public static func notoSerif(size: CGFloat, weight: Font.Weight = .regular) -> Font {
            return .custom("NotoSerif-Regular", size: size) // Adjusting for specific weight if available in assets
        }
        
        /// Space Grotesk: Used for Technical Data (numeric values, metrics).
        public static func spaceGrotesk(size: CGFloat, weight: Font.Weight = .regular) -> Font {
            return .custom("SpaceGrotesk-Regular", size: size)
        }
        
        /// Monaco: Used for archival agent trace (technical ledger).
        public static func monacoTechnical(size: CGFloat) -> Font {
            return .custom("Monaco", size: size)
        }
    }
}

// MARK: - Extensions

extension Color {
    public static let brutalCharcoal = SovereignTheme.Colors.brutalCharcoal
    public static let brutalRecessed = SovereignTheme.Colors.brutalRecessed
    public static let brutalOffWhite = SovereignTheme.Colors.brutalOffWhite
    public static let brutalChartreuse = SovereignTheme.Colors.brutalChartreuse
}

extension View {
    /// Applies the Sovereign Wealth Header style (Noto Serif)
    func sovereignHeader(size: CGFloat = 32) -> some View {
        self.font(SovereignTheme.Fonts.notoSerif(size: size))
            .foregroundStyle(Color.brutalOffWhite)
    }
    
    /// Applies the Sovereign Technical Data style (Space Grotesk)
    func sovereignTechnical(size: CGFloat = 14) -> some View {
        self.font(SovereignTheme.Fonts.spaceGrotesk(size: size))
            .foregroundStyle(Color.brutalOffWhite)
    }
    
    /// Applies the "Acid" accent color (Brutal Chartreuse)
    func acidAccent() -> some View {
        self.foregroundStyle(Color.brutalChartreuse)
    }
}
