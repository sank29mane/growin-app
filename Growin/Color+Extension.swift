import SwiftUI

extension Color {
    // MARK: - Brand Core (Stitch Design System)
    static let growinPrimary = Color(hex: "6366F1") // Stitch Neon Indigo
    static let growinSecondary = Color(hex: "06B6D4") // Stitch Neon Cyan
    static let growinAccent = Color(hex: "A855F7") // Stitch Neon Purple
    
    // Stitch Neon Palette
    static let stitchNeonIndigo = Color(hex: "6366F1")
    static let stitchNeonCyan = Color(hex: "06B6D4")
    static let stitchNeonPurple = Color(hex: "A855F7")
    static let stitchNeonGreen = Color(hex: "10B981")
    static let stitchNeonYellow = Color(hex: "F59E0B")
    
    // MARK: - Backgrounds (Deep Charcoal)
    static let growinDarkBg = Color(hex: "0A0A0B") // Stitch Deep Charcoal
    static let growinSurface = Color(hex: "141416") // Stitch Surface
    
    // MARK: - Mesh Components (Stitch Palette)
    static let meshIndigo = Color(hex: "1E1B4B")
    static let meshEmerald = Color(hex: "064E3B")
    static let meshSlate = Color(hex: "09090B")
    
    // MARK: - Gradients
    static let primaryGradient = LinearGradient(
        colors: [Color(hex: "6366F1"), Color(hex: "A855F7")],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )
    
    // MARK: - Semantic (High Contrast)
    static let growinRed = Color(hex: "F43F5E") // Rose 500
    static let growinGreen = Color(hex: "10B981") // Emerald 500
    static let growinOrange = Color(hex: "F59E0B") // Amber 500
    
    // MARK: - Persona Colors
    struct Persona {
        static let analyst = Color(hex: "818CF8") // Indigo 400
        static let risk = Color(hex: "FB7185") // Rose 400
        static let trader = Color(hex: "2DD4BF") // Teal 400
        static let execution = Color(hex: "60A5FA") // Blue 400
    }
    
    // MARK: - UI Utilities (Premium Contrast)
    static let glassBorder = Color.white.opacity(0.12)
    static let glassShine = Color.white.opacity(0.06)
    static let textPrimary = Color.white
    static let textSecondary = Color(hex: "A1A1AA") // Zinc 400
    static let textTertiary = Color(hex: "71717A") // Zinc 500
    
    // Helper to init from Hex
    init(hex: String) {
        let hex = hex.trimmingCharacters(in: CharacterSet.alphanumerics.inverted)
        var int: UInt64 = 0
        Scanner(string: hex).scanHexInt64(&int)
        let a, r, g, b: UInt64
        switch hex.count {
        case 3: // RGB (12-bit)
            (a, r, g, b) = (255, (int >> 8) * 17, (int >> 4 & 0xF) * 17, (int & 0xF) * 17)
        case 6: // RGB (24-bit)
            (a, r, g, b) = (255, int >> 16, int >> 8 & 0xFF, int & 0xFF)
        case 8: // ARGB (32-bit)
            (a, r, g, b) = (int >> 24, int >> 16 & 0xFF, int >> 8 & 0xFF, int & 0xFF)
        default:
            (a, r, g, b) = (1, 1, 1, 0)
        }
        
        self.init(
            .sRGB,
            red: Double(r) / 255,
            green: Double(g) / 255,
            blue:  Double(b) / 255,
            opacity: Double(a) / 255
        )
    }
}
