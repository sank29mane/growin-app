import SwiftUI

/// AlphaLedgerView: A brutalist technical ledger for asset monitoring.
/// Enforces "Authority through Absence" with no dividers and tonal depth.
struct AlphaLedgerView: View {
    let assets: [AlphaAsset] = [
        AlphaAsset(ticker: "3GLD.L", name: "Leveraged Gold", alpha: 0.1245, status: .active),
        AlphaAsset(ticker: "QQQ", name: "Nasdaq 100", alpha: 0.0832, status: .active),
        AlphaAsset(ticker: "BTC", name: "Bitcoin", alpha: -0.0210, status: .neutral),
        AlphaAsset(ticker: "NVDA", name: "Nvidia Corp", alpha: 0.1567, status: .active),
        AlphaAsset(ticker: "USD", name: "Cash Reserve", alpha: 0.0000, status: .standby)
    ]
    
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Header Row
            HStack {
                Text("ASSET")
                    .sovereignTechnical(size: 10)
                    .opacity(0.4)
                Spacer()
                Text("ALPHA")
                    .sovereignTechnical(size: 10)
                    .opacity(0.4)
                
                // Extra spacer for status indicator alignment
                Color.clear.frame(width: 20, height: 1)
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 12)
            
            // Ledger Rows with Tonal Background
            VStack(spacing: 0) {
                ForEach(assets) { asset in
                    LedgerRow(asset: asset)
                }
            }
            .background(Color.brutalRecessed)
            .border(Color.white.opacity(0.05), width: 1)
        }
    }
}

struct LedgerRow: View {
    let asset: AlphaAsset
    @State private var isHovered = false
    
    var body: some View {
        HStack(alignment: .center, spacing: 0) {
            // Radical Asymmetry: Ticker with serif font
            VStack(alignment: .leading, spacing: 2) {
                Text(asset.ticker)
                    .font(SovereignTheme.Fonts.notoSerif(size: 16))
                    .foregroundStyle(Color.brutalOffWhite)
                Text(asset.name)
                    .sovereignTechnical(size: 10)
                    .opacity(0.5)
            }
            
            Spacer()
            
            // Technical Alpha Value in Space Grotesk
            Text(String(format: "%+.4f", asset.alpha))
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 14))
                .foregroundStyle(asset.alpha > 0.1 ? Color.brutalChartreuse : Color.brutalOffWhite)
            
            // Status Indicator (Brutal Accent)
            Rectangle()
                .fill(asset.alpha > 0.1 ? Color.brutalChartreuse : Color.white.opacity(0.15))
                .frame(width: 4, height: 32)
                .padding(.leading, 16)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        // Tonal shifts: Recessed for base, Charcoal for rows
        .background(isHovered ? Color.white.opacity(0.03) : Color.brutalCharcoal)
        .onHover { hovering in
            withAnimation(.easeInOut(duration: 0.15)) {
                isHovered = hovering
            }
        }
    }
}

struct AlphaAsset: Identifiable {
    let id = UUID()
    let ticker: String
    let name: String
    let alpha: Double
    let status: AlphaStatus
}

enum AlphaStatus {
    case active, neutral, standby
}
