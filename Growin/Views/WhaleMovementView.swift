import SwiftUI

struct WhaleTrade: Codable, Identifiable {
    let id = UUID()
    let price: Double
    let size: Double
    let value_usd: Double
    let timestamp: String
    let is_whale: Bool
}

struct WhaleMovementView: View {
    let ticker: String
    let trades: [WhaleTrade]
    let sentiment: String
    let summary: String
    
    var body: some View {
        GlassCard(cornerRadius: 20) {
            VStack(alignment: .leading, spacing: 16) {
                // Header
                HStack {
                    Image(systemName: "fossil.shell.fill")
                        .foregroundStyle(Color.stitchNeonCyan)
                        .font(.title3)
                    
                    VStack(alignment: .leading, spacing: 2) {
                        Text("WHALE WATCH 2.0")
                            .premiumTypography(.overline)
                            .foregroundStyle(.secondary)
                        Text(ticker)
                            .premiumTypography(.title)
                            .foregroundStyle(.white)
                    }
                    
                    Spacer()
                    
                    SentimentPill(sentiment: sentiment)
                }
                
                Text(summary)
                    .premiumTypography(.caption)
                    .foregroundStyle(.white.opacity(0.7))
                    .lineLimit(2)
                
                Divider().background(Color.white.opacity(0.1))
                
                // Whale Chart (Simplified Representation)
                if !trades.isEmpty {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("LARGE BLOCK TRADES")
                            .premiumTypography(.overline)
                            .font(.system(size: 9))
                            .foregroundStyle(.secondary)
                        
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(alignment: .bottom, spacing: 8) {
                                ForEach(trades.suffix(10)) { trade in
                                    WhaleBar(trade: trade)
                                }
                            }
                            .frame(height: 100)
                        }
                    }
                } else {
                    ContentUnavailableView {
                        Label("No Recent Whales", systemImage: "water.waves")
                    } description: {
                        Text("Market activity is currently retail-driven.")
                    }
                    .frame(height: 100)
                    .controlSize(.small)
                }
            }
            .padding()
        }
    }
}

struct SentimentPill: View {
    let sentiment: String
    
    var body: some View {
        Text(sentiment)
            .premiumTypography(.overline)
            .padding(.horizontal, 10)
            .padding(.vertical, 4)
            .background(color.opacity(0.2))
            .cornerRadius(20)
            .overlay(
                Capsule().stroke(color.opacity(0.5), lineWidth: 1)
            )
            .foregroundStyle(color)
    }
    
    private var color: Color {
        switch sentiment.uppercased() {
        case "BULLISH": return Color.stitchNeonGreen
        case "BEARISH": return Color.growinRed
        default: return .secondary
        }
    }
}

struct WhaleBar: View {
    let trade: Swift.Double // Using value_usd for height
    let tradeObj: WhaleTrade
    
    init(trade: WhaleTrade) {
        self.tradeObj = trade
        self.trade = trade.value_usd
    }
    
    var body: some View {
        VStack(spacing: 4) {
            RoundedRectangle(cornerRadius: 4)
                .fill(LinearGradient(colors: [Color.stitchNeonCyan, Color.stitchNeonIndigo], startPoint: .top, endPoint: .bottom))
                .frame(width: 20, height: normalizedHeight)
            
            Text("$\(Int(tradeObj.value_usd / 1000))k")
                .font(.system(size: 8, weight: .bold, design: .monospaced))
                .foregroundStyle(.secondary)
        }
    }
    
    private var normalizedHeight: CGFloat {
        let maxVal = 500000.0 // Normalize relative to 500k
        return CGFloat(min(100, (trade / maxVal) * 100))
    }
}

#Preview {
    ZStack {
        Color.black.ignoresSafeArea()
        WhaleMovementView(
            ticker: "NVDA",
            trades: [
                WhaleTrade(price: 145.2, size: 1000, value_usd: 145200, timestamp: "2026-02-28", is_whale: true),
                WhaleTrade(price: 145.5, size: 2500, value_usd: 363750, timestamp: "2026-02-28", is_whale: true),
                WhaleTrade(price: 145.1, size: 1200, value_usd: 174120, timestamp: "2026-02-28", is_whale: true)
            ],
            sentiment: "BULLISH",
            summary: "Detected 3 large block trades totaling $0.68M. Activity suggests institutional accumulation."
        )
        .padding()
    }
}
