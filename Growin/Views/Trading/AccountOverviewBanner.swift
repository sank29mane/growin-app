import SwiftUI

/// AccountOverviewBanner: High-precision portfolio stats for the macOS Sovereign Dashboard.
/// Features expanded horizontal metrics and technical mono labels.
struct AccountOverviewBanner: View {
    let totalBalance: Double
    let dayPL: Double
    let dayPLPercentage: Double
    let leverage: Double = 1.22
    let buyingPower: Double = 42809.12
    
    var body: some View {
        HStack(alignment: .top, spacing: 48) {
            // Main Stat (Noto Serif Wealth Header)
            VStack(alignment: .leading, spacing: 4) {
                Text("TOTAL LIQUID EQUITY")
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                    .foregroundStyle(Color.brutalOffWhite.opacity(0.4))
                
                Text(self.formatCurrency(totalBalance))
                    .font(SovereignTheme.Fonts.notoSerif(size: 42, weight: .regular))
                    .foregroundStyle(Color.brutalOffWhite)
            }
            
            // Separation: Vertical Dotted Line Placeholder (Using Border/Opacity)
            Rectangle()
                .fill(SovereignTheme.Colors.technicalBorder)
                .frame(width: 1, height: 60)
            
            HStack(spacing: 32) {
                MetricColumn(title: "DAY P/L", value: self.formatPL(dayPL), color: dayPL >= 0 ? Color.brutalChartreuse : Color.red)
                MetricColumn(title: "24H CHANGE", value: self.formatPercentage(dayPLPercentage), color: dayPLPercentage >= 0 ? Color.brutalChartreuse : Color.red)
                MetricColumn(title: "BUYING POWER", value: self.formatCurrency(buyingPower), color: Color.brutalOffWhite)
                MetricColumn(title: "LEVERAGE", value: String(format: "%.2fx", leverage), color: Color.brutalOffWhite.opacity(0.6))
            }

            Spacer()
            
            // Status Icon: Terminal Execution
            Image(systemName: "terminal")
                .font(.system(size: 14))
                .foregroundStyle(Color.brutalChartreuse)
                .padding(12)
                .background(Color.white.opacity(0.05))
                .border(SovereignTheme.Colors.technicalBorder, width: 1)
        }
        .padding(32)
        .background(Color.brutalRecessed)
        .border(SovereignTheme.Colors.technicalBorder.opacity(0.5), width: 1)
    }
}

private struct MetricColumn: View {
    let title: String
    let value: String
    let color: Color
    
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 9, weight: .bold))
                .foregroundStyle(Color.brutalOffWhite.opacity(0.4))
            
            Text(value)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 14))
                .monospacedDigit()
                .foregroundStyle(color)
        }
    }
}

#Preview {
    ZStack {
        Color.black.ignoresSafeArea()
        AccountOverviewBanner(
            totalBalance: 1482094.62,
            dayPL: 42901.00,
            dayPLPercentage: 1.24
        )
        .padding()
    }
}
