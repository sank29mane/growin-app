import Foundation
import SwiftUI

/// SovereignUtils: Shared formatting and technical helper logic for the Sovereign Desktop experience.
public enum SovereignUtils {
    
    /// Repesents an asset for execution calibration.
    public struct ExecutionAsset: Hashable, Identifiable {
        public let ticker: String
        public let currentPrice: Double
        public var id: String { ticker }
        
        public init(ticker: String, currentPrice: Double) {
            self.ticker = ticker
            self.currentPrice = currentPrice
        }
    }
    
    /// Formats currency with high-precision accounting rules.
    /// Ex: $1,482,094.62
    public static func formatCurrency(_ value: Double) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .currency
        formatter.currencySymbol = "$"
        formatter.maximumFractionDigits = 2
        formatter.minimumFractionDigits = 2
        return formatter.string(from: NSNumber(value: value)) ?? "$0.00"
    }
    
    /// Formats P/L with explicit sign tracers (+/-).
    public static func formatPL(_ value: Double) -> String {
        let prefix = value >= 0 ? "+" : ""
        return "\(prefix)\(formatCurrency(value))"
    }
    
    /// Formats percentages with explicit sign and 2 decimal precision.
    public static func formatPercentage(_ value: Double) -> String {
        let prefix = value >= 0 ? "+" : ""
        return "\(prefix)\(String(format: "%.2f", value))%"
    }
    
    /// Formats large volumes/market caps into human-readable technical chunks (T, B, M).
    public static func formatTechnicalLarge(_ value: Double) -> String {
        let thousand = 1000.0
        let million = thousand * thousand
        let billion = million * thousand
        let trillion = billion * thousand

        if value >= trillion {
            return String(format: "%.2fT", value / trillion)
        } else if value >= billion {
            return String(format: "%.2fB", value / billion)
        } else if value >= million {
            return String(format: "%.2fM", value / million)
        } else {
            return String(format: "%.0f", value)
        }
    }
}

// MARK: - View Extensions for Formatting Triggers

extension View {
    func formatCurrency(_ value: Double) -> String {
        SovereignUtils.formatCurrency(value)
    }
    
    func formatPL(_ value: Double) -> String {
        SovereignUtils.formatPL(value)
    }
    
    func formatPercentage(_ value: Double) -> String {
        SovereignUtils.formatPercentage(value)
    }
}
