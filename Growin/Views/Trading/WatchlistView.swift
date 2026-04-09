import SwiftUI

/// WatchlistView: Professional macOS Asset Watchlist & Analysis Terminal.
/// Optimized for horizontal split-view layouts with active charting integration.
struct WatchlistView: View {
    @State private var selectedAssetId: UUID?
    @State private var watchlistedAssets = [
        WatchlistAsset(ticker: "AAPL", name: "Apple Inc.", price: 189.43, change: -0.45, sparkline: [190, 188, 189, 187, 189, 188, 189.43], market: "NASDAQ"),
        WatchlistAsset(ticker: "TSLA", name: "Tesla Inc.", price: 168.32, change: 1.12, sparkline: [165, 167, 166, 168, 167, 169, 168.32], market: "NASDAQ"),
        WatchlistAsset(ticker: "LSE:VUSA", name: "Vanguard S&P 500", price: 92.11, change: 0.88, sparkline: [91, 91.5, 91.2, 91.8, 92, 91.9, 92.11], market: "LSE"),
        WatchlistAsset(ticker: "LSE:IUSA", name: "iShares S&P 500", price: 38.45, change: 0.75, sparkline: [38, 38.2, 38.1, 38.3, 38.4, 38.3, 38.45], market: "LSE"),
        WatchlistAsset(ticker: "3GLD.L", name: "WisdomTree Gold", price: 218.45, change: 2.15, sparkline: [214, 215, 216, 217, 218, 217, 218.45], market: "LSE")
    ]
    
    @State private var showingExecutionPanel = false
    @State private var currentExecutionAsset: SovereignUtils.ExecutionAsset? = nil
    
    var body: some View {
        // Primary Split: Ledger & Analysis
        HStack(spacing: 0) {
                // Column 1: Asset Ledger (Left)
                VStack(alignment: .leading, spacing: 0) {
                    HStack {
                        Text("ASSET WATCHLIST")
                            .font(SovereignTheme.Fonts.spaceGrotesk(size: 14, weight: .bold))
                            .foregroundStyle(Color.brutalOffWhite)
                        Spacer()
                        Text("5 TRACE ACTIVE")
                            .font(SovereignTheme.Fonts.spaceGrotesk(size: 10))
                            .foregroundStyle(Color.brutalChartreuse)
                    }
                    .padding(24)
                    .background(Color.black.opacity(0.1))
                    
                    ScrollView(showsIndicators: false) {
                        VStack(spacing: 0) {
                            ForEach(watchlistedAssets) { asset in
                                WatchlistRow(asset: asset, isSelected: selectedAssetId == asset.id)
                                    .contentShape(Rectangle())
                                    .onTapGesture {
                                        selectedAssetId = asset.id
                                    }
                                    .background(selectedAssetId == asset.id ? Color.white.opacity(0.05) : Color.clear)
                                    .border(SovereignTheme.Colors.technicalBorder.opacity(0.2), width: 0.5)
                            }
                        }
                    }
                }
                .frame(width: 420)
                .border(SovereignTheme.Colors.technicalBorder, width: 1)
                
                // Column 2: Analysis & Charting (Right)
                ZStack {
                    Color.brutalRecessed.ignoresSafeArea()
                    
                    if let selectedAsset = watchlistedAssets.first(where: { $0.id == selectedAssetId }) {
                        AnalysisDetailView(asset: selectedAsset) {
                            currentExecutionAsset = SovereignUtils.ExecutionAsset(ticker: selectedAsset.ticker, currentPrice: selectedAsset.price)
                            showingExecutionPanel = true
                        }
                    } else {
                        // Empty State: Technical Trace Placeholder
                        VStack(spacing: 20) {
                            Image(systemName: "chart.bar.xaxis")
                                .font(.system(size: 48))
                                .foregroundStyle(Color.brutalOffWhite.opacity(0.1))
                            Text("SELECT INSTRUMENT FOR TRACE")
                                .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold))
                                .foregroundStyle(Color.brutalOffWhite.opacity(0.3))
                        }
                    }
                    
                    // Overlay: Execution Panel
                    if showingExecutionPanel {
                        Color.black.opacity(0.6)
                            .ignoresSafeArea()
                            .onTapGesture { showingExecutionPanel = false }
                        
                        HStack(spacing: 0) {
                            Spacer()
                            ExecutionPanelView(isPresented: $showingExecutionPanel, asset: currentExecutionAsset)
                                .transition(.move(edge: .trailing))
                        }
                    }
                }
                .animation(.spring(response: 0.4, dampingFraction: 0.8), value: showingExecutionPanel)
            }
    }
}

/// AnalysisDetailView: A high-density analysis panel for a selected asset on macOS.
private struct AnalysisDetailView: View {
    let asset: WatchlistAsset
    let onExecute: () -> Void
    
    var body: some View {
        VStack(alignment: .leading, spacing: 40) {
            // Header: Instrument Info
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(asset.ticker)
                        .font(SovereignTheme.Fonts.notoSerif(size: 32, weight: .regular))
                    Text(asset.name)
                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 14))
                        .foregroundStyle(Color.brutalOffWhite.opacity(0.5))
                }
                
                Spacer()
                
                VStack(alignment: .trailing, spacing: 8) {
                    Text(String(format: "$%.2f", asset.price))
                        .font(SovereignTheme.Fonts.notoSerif(size: 32, weight: .regular))
                    Text(String(format: "%@%.2f%%", asset.change >= 0 ? "+" : "", asset.change))
                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                        .foregroundStyle(asset.change >= 0 ? Color.brutalChartreuse : Color.red)
                }
                
                Button(action: onExecute) {
                    Text("EXECUTE TRACE")
                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                        .padding(.horizontal, 16)
                        .padding(.vertical, 8)
                        .background(Color.brutalChartreuse)
                        .foregroundStyle(Color.black)
                        .border(Color.black, width: 0.5)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Execute Trace for \(asset.ticker)")
                .accessibilityAddTraits(.isButton)

                .padding(.leading, 24)
            }
            
            // Expanded Chart Placeholder (Professional Terminal Aesthetic)
            VStack {
                Spacer()
                SovereignSparkline(data: asset.sparkline, color: Color.brutalChartreuse)
                    .frame(height: 240)
                    .padding()
                Spacer()
            }
            .background(Color.black.opacity(0.3))
            .border(SovereignTheme.Colors.technicalBorder, width: 1)
            
            // Technical Metadata Grid
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], spacing: 24) {
                InfoBlock(title: "MARKET CAP", value: "2.84T")
                InfoBlock(title: "VOLUME", value: "48.2M")
                InfoBlock(title: "P/E RATIO", value: "24.2")
                InfoBlock(title: "BETA", value: "1.08")
                InfoBlock(title: "OPEN", value: "188.42")
                InfoBlock(title: "RANGE", value: "186.2 - 191.1")
            }
        }
        .padding(48)
    }
}

private struct InfoBlock: View {
    let title: String
    let value: String
    
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                .foregroundStyle(Color.brutalOffWhite.opacity(0.4))
            Text(value)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                .foregroundStyle(Color.brutalOffWhite)
        }
    }
}

private struct WatchlistRow: View {
    let asset: WatchlistAsset
    let isSelected: Bool
    
    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 2) {
                Text(asset.ticker)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 14, weight: .bold))
                    .foregroundStyle(isSelected ? Color.brutalChartreuse : Color.brutalOffWhite)
                Text(asset.market)
                    .font(SovereignTheme.Fonts.notoSerif(size: 10))
                    .foregroundStyle(Color.brutalOffWhite.opacity(0.4))
            }
            .frame(width: 100, alignment: .leading)
            
            Spacer()
            
            SovereignSparkline(data: asset.sparkline, color: asset.change >= 0 ? Color.brutalChartreuse : Color.red)
                .frame(width: 80, height: 20)
            
            Spacer()
            
            VStack(alignment: .trailing, spacing: 2) {
                Text(String(format: "$%.2f", asset.price))
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 14, weight: .bold))
                    .monospacedDigit()
                Text(String(format: "%@%.2f%%", asset.change >= 0 ? "+" : "", asset.change))
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 12))
                    .foregroundStyle(asset.change >= 0 ? Color.brutalChartreuse : Color.red)
            }
            .frame(width: 80, alignment: .trailing)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 16)
    }
}

struct WatchlistAsset: Identifiable {
    let id = UUID()
    let ticker: String
    let name: String
    let price: Double
    let change: Double
    let sparkline: [Double]
    let market: String
}

#Preview {
    WatchlistView()
        .frame(width: 1200, height: 800)
}
