import SwiftUI

/// MasterLedgerView: High-density macOS Portfolio Dashboard.
/// Replaces the mobile-first version with a multi-column desktop layout.
/// Features a persistent sidebar, wide account overview, and deep asset analytics.
struct MasterLedgerView: View {
    @State private var totalBalance: Double = 1482094.62
    @State private var dayPL: Double = 42901.00
    @State private var dayPLPercentage: Double = 1.24
    
    // Mock assets for demonstration
    let assets = [
        PortfolioAsset(ticker: "MSFT", name: "Microsoft Corp.", price: 412.32, change: 1.24, holdings: 412.00, value: 169875.84, sector: "Technology", signal: "BULLISH"),
        PortfolioAsset(ticker: "AAPL", name: "Apple Inc.", price: 189.43, change: -0.45, holdings: 890.00, value: 168592.70, sector: "Technology", signal: "NEUTRAL"),
        PortfolioAsset(ticker: "VUSA", name: "Vanguard S&P 500", price: 92.11, change: 0.88, holdings: 4200.00, value: 386862.00, sector: "ETF", signal: "ACCUMULATE"),
        PortfolioAsset(ticker: "3GLD.L", name: "WisdomTree Gold", price: 218.45, change: 2.15, holdings: 1540.00, value: 336413.00, sector: "Commodities", signal: "HEDGE")
    ]
    
    var body: some View {
        // Main Stage: Portfolio Ledger
        VStack(alignment: .leading, spacing: 0) {

                // Header Panel: Account Overview
                AccountOverviewBanner(
                    totalBalance: totalBalance,
                    dayPL: dayPL,
                    dayPLPercentage: dayPLPercentage
                )
                
                // Asset Ledger stage
                ScrollView(showsIndicators: false) {
                    VStack(alignment: .leading, spacing: 0) {
                        // Table header: Technical columns
                        HStack(spacing: 0) {
                            Text("INSTRUMENT")
                                .frame(width: 240, alignment: .leading)
                            Text("SECTOR")
                                .frame(width: 140, alignment: .leading)
                            Text("PRICE")
                                .frame(width: 120, alignment: .trailing)
                            Text("24H CHG")
                                .frame(width: 100, alignment: .trailing)
                            Text("HOLDINGS")
                                .frame(width: 140, alignment: .trailing)
                            Text("POSITION VALUE")
                                .frame(width: 180, alignment: .trailing)
                            Text("ALPHA SIGNAL")
                                .frame(width: 160, alignment: .trailing)
                            Spacer()
                        }
                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                        .foregroundStyle(Color.brutalOffWhite.opacity(0.4))
                        .padding(.horizontal, 32)
                        .padding(.vertical, 16)
                        .background(Color.black.opacity(0.2))
                        
                        // Ledger List (Density-First)
                        VStack(spacing: 0) {
                            ForEach(assets) { asset in
                                DesktopLedgerRow(asset: asset)
                                    .background(asset.ticker == "VUSA" ? Color.brutalRecessed : Color.clear)
                                    .border(SovereignTheme.Colors.technicalBorder.opacity(0.3), width: 0.5)
                            }
                        }
                        
                        // Analytics Trace Footer
                        VStack(alignment: .leading, spacing: 16) {
                            Text("PORTFOLIO CORRELATION ANALYSIS (V1.2)")
                                .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                            
                            HStack(alignment: .top, spacing: 40) {
                                Text("SYSTEM: AGENT JULES-01")
                                    .font(SovereignTheme.Fonts.monacoTechnical(size: 11))
                                    .foregroundStyle(Color.brutalChartreuse)
                                
                                Text("LATENCY: 4.2ms")
                                    .font(SovereignTheme.Fonts.monacoTechnical(size: 11))
                                
                                Text("Aggregated market positions indicate a Concentration Risk within Tech-Sector equities. Hedge adjustments suggested for late-quarter stability.")
                                    .font(SovereignTheme.Fonts.monacoTechnical(size: 11))
                                    .foregroundStyle(Color.brutalOffWhite.opacity(0.6))
                                    .frame(maxWidth: 600, alignment: .leading)
                            }
                        }
                        .padding(32)
                        .background(Color.brutalRecessed)
                        .border(SovereignTheme.Colors.technicalBorder, width: 1)
                        .padding(32)
                    }
                }
            }
            .background(Color.brutalCharcoal)
    }
}

/// DesktopLedgerRow: A single high-density asset row optimized for macOS horizontal space.
private struct DesktopLedgerRow: View {
    let asset: PortfolioAsset
    
    var body: some View {
        HStack(spacing: 0) {
            // Ticker & Full Name (Serif/Mono Blend)
            VStack(alignment: .leading, spacing: 2) {
                Text(asset.ticker)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 15, weight: .bold))
                Text(asset.name)
                    .font(SovereignTheme.Fonts.notoSerif(size: 11))
                    .foregroundStyle(Color.brutalOffWhite.opacity(0.4))
            }
            .frame(width: 240, alignment: .leading)
            
            // Sector (Mono)
            Text(asset.sector.uppercased())
                .font(SovereignTheme.Fonts.monacoTechnical(size: 10))
                .foregroundStyle(Color.brutalOffWhite.opacity(0.6))
                .frame(width: 140, alignment: .leading)
            
            // Price (Space Grotesk Mono Digit)
            Text(String(format: "$%.2f", asset.price))
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 14))
                .monospacedDigit()
                .frame(width: 120, alignment: .trailing)
            
            // 24H Change
            Text(String(format: "%@%.2f%%", asset.change >= 0 ? "+" : "", asset.change))
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 14))
                .foregroundStyle(asset.change >= 0 ? Color.brutalChartreuse : Color.red)
                .frame(width: 100, alignment: .trailing)
            
            // Holdings
            Text(String(format: "%.2f SHRS", asset.holdings))
                .font(SovereignTheme.Fonts.monacoTechnical(size: 11))
                .foregroundStyle(Color.brutalOffWhite.opacity(0.5))
                .frame(width: 140, alignment: .trailing)
            
            // Position Value
            Text(String(format: "$%.2f", asset.value))
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 14, weight: .bold))
                .frame(width: 180, alignment: .trailing)
            
            // Alpha Signal
            Text(asset.signal)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 11, weight: .bold))
                .foregroundStyle(Color.white)
                .padding(.horizontal, 10)
                .padding(.vertical, 4)
                .background(signalColor(asset.signal).opacity(0.15))
                .border(signalColor(asset.signal).opacity(0.6), width: 1)
                .frame(width: 160, alignment: .trailing)
            
            Spacer()
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(asset.ticker), \(asset.name), Sector: \(asset.sector). Price: $\(String(format: "%.2f", asset.price)), Change: \(asset.change >= 0 ? "+" : "")\(String(format: "%.2f", asset.change)) percent. Holdings: \(String(format: "%.2f", asset.holdings)) shares, Value: $\(String(format: "%.2f", asset.value)). Signal: \(asset.signal).")
        .padding(.horizontal, 32)
        .padding(.vertical, 14)
    }
    
    private func signalColor(_ signal: String) -> Color {
        switch signal {
        case "BULLISH": return Color.brutalChartreuse
        case "ACCUMULATE": return Color.green
        case "HEDGE": return Color.orange
        case "NEUTRAL": return Color.gray
        default: return Color.brutalOffWhite
        }
    }
}

/// Expanded model for Desktop Portfolio
struct PortfolioAsset: Identifiable {
    let id = UUID()
    let ticker: String
    let name: String
    let price: Double
    let change: Double
    let holdings: Double
    let value: Double
    let sector: String
    let signal: String
}

#Preview {
    MasterLedgerView()
        .frame(width: 1400, height: 900)
}
