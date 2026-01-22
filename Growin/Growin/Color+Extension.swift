import SwiftUI

extension Color {
    // Brand Colors
    static let growinPrimary = Color(red: 0.1, green: 0.2, blue: 0.5) // Deep navy
    static let growinBackground = Color(red: 0.05, green: 0.05, blue: 0.08) // Near black
    
    // Persona Colors
    struct Persona {
        static let analyst = Color(red: 0.5, green: 0.4, blue: 0.9) // Indigo/Amethyst
        static let risk = Color(red: 0.9, green: 0.5, blue: 0.3) // Amber/Flame
        static let trader = Color(red: 0.2, green: 0.8, blue: 0.5) // Emerald/Seafoam
        static let execution = Color(red: 0.3, green: 0.6, blue: 1.0) // Azure/Sky
    }
    
    // UI Utilities
    static let glassBorder = Color.white.opacity(0.15)
    static let glassBackground = Color.black.opacity(0.3)
}
