import SwiftUI
import Charts

struct DashboardView: View {
    @Bindable var viewModel: DashboardViewModel
    @State private var selectedPosition: Position?
    
    var body: some View {
        ScrollView(showsIndicators: false) {
            VStack(spacing: 32) {
                // Integrated Dashboard Header
                HStack(alignment: .bottom) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("SUMMARY")
                            .font(.caption)
                            .fontWeight(.semibold)
                            .foregroundStyle(.secondary)
                        
                        Text("Dashboard")
                            .font(.title)
                            .fontWeight(.bold)
                            .foregroundStyle(.primary)
                    }
                    
                    Spacer()
                    
                    // Simple Status Pill
                    HStack(spacing: 6) {
                        Circle().fill(viewModel.isLoading ? Color.growinOrange : (viewModel.errorMessage == nil ? Color.growinGreen : Color.growinRed)).frame(width: 6, height: 6)
                        Text(viewModel.isLoading ? "SYNCING..." : (viewModel.errorMessage == nil ? "UPDATED" : "OFFLINE"))
                            .font(.caption2)
                            .fontWeight(.bold)
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background((viewModel.isLoading ? Color.growinOrange : (viewModel.errorMessage == nil ? Color.growinGreen : Color.growinRed)).opacity(0.1))
                    .clipShape(Capsule())
                }
                .padding(.horizontal)
                .padding(.top, 24)
                
                if let error = viewModel.errorMessage {
                    ErrorCard(message: error) {
                        Task { await viewModel.fetchPortfolioData() }
                    }
                }
                
                // Portfolio Summary Grid
                MetricGrid(summary: viewModel.portfolioData?.summary)
                    .padding(.horizontal)
                
                // Main Analysis Section
                HStack(spacing: 20) {
                    // Allocation Perspective
                    GlassCard(cornerRadius: 24) {
                        VStack(alignment: .leading, spacing: 16) {
                            HStack {
                                Image(systemName: "chart.pie.fill")
                                    .foregroundStyle(Color.growinAccent)
                                Text("Asset Allocation")
                                    .font(.caption)
                                    .fontWeight(.semibold)
                                    .foregroundStyle(.secondary)
                            }
                            
                            if !viewModel.allocationData.isEmpty {
                                Chart(viewModel.allocationData) { item in
                                    SectorMark(
                                        angle: .value("Value", item.value),
                                        innerRadius: .ratio(0.7),
                                        angularInset: 2
                                    )
                                    .clipShape(.rect(cornerRadius: 6))
                                    .foregroundStyle(by: .value("Name", item.label))
                                }
                                .frame(height: 180)
                                .chartLegend(position: .bottom, alignment: .center, spacing: 16)
                            } else {
                                ContentUnavailableView {
                                    Label(viewModel.isLoading ? "Loading..." : "No Data Available", systemImage: "chart.pie")
                                }
                                .scaleEffect(0.6)
                            }
                        }
                        .glassEffect(.regular.interactive())
                    }
                    
                    // Performance Snapshot
                    GlassCard(cornerRadius: 24) {
                        VStack(alignment: .leading, spacing: 16) {
                            HStack {
                                Image(systemName: "waveform.path.ecg")
                                    .foregroundStyle(Color.growinPrimary)
                                Text("Total Return")
                                    .font(.caption)
                                    .fontWeight(.semibold)
                                    .foregroundStyle(.secondary)
                            }
                            
                            if let pnl = viewModel.portfolioData?.summary?.totalPnl,
                               let pnlPercent = viewModel.portfolioData?.summary?.totalPnlPercent {
                                
                                Spacer()
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(pnl, format: .currency(code: "GBP"))
                                        .font(.system(size: 38, weight: .heavy, design: .rounded))
                                        .foregroundStyle(pnl >= 0 ? Color.growinGreen : Color.growinRed)
                                    
                                    HStack(spacing: 6) {
                                        Image(systemName: pnl >= 0 ? "arrow.up.right" : "arrow.down.right")
                                        Text(String(format: "%.2f%%", abs(pnlPercent)))
                                    }
                                    .font(.system(size: 18, weight: .bold, design: .rounded))
                                    .foregroundStyle(pnl >= 0 ? Color.growinGreen : Color.growinRed)
                                    .padding(.horizontal, 10)
                                    .padding(.vertical, 4)
                                    .background((pnl >= 0 ? Color.growinGreen : Color.growinRed).opacity(0.1))
                                    .clipShape(Capsule())
                                }
                                Spacer()
                            } else {
                                ContentUnavailableView {
                                    Label(viewModel.isLoading ? "Syncing..." : "No Performance Data", systemImage: "timer")
                                }
                                .scaleEffect(0.6)
                            }
                        }
                        .glassEffect(.regular.interactive())
                    }
                }
                .frame(height: 280)
                .padding(.horizontal)
                
                // Detailed Account Analytics
                VStack(spacing: 24) {
                    AccountSectionView(
                        title: "General Investment",
                        accountData: viewModel.investData,
                        isLoading: viewModel.isLoading,
                        accentColor: .growinPrimary,
                        onPositionTap: { position in selectedPosition = position }
                    )

                    AccountSectionView(
                        title: "ISA",
                        accountData: viewModel.isaData,
                        isLoading: viewModel.isLoading,
                        accentColor: .growinAccent,
                        onPositionTap: { position in selectedPosition = position }
                    )
                }
                .padding(.horizontal)
            }
            .padding(.bottom, 40)
        }
        .scrollIndicators(.hidden)
        .refreshable {
            await viewModel.fetchPortfolioData()
        }
        .onAppear {
            if viewModel.portfolioData == nil {
                Task { await viewModel.fetchPortfolioData() }
            }
        }
        .sheet(item: $selectedPosition) { position in
            if let ticker = position.ticker {
                NavigationStack {
                    StockChartView(viewModel: StockChartViewModel(symbol: ticker))
                        .toolbar {
                            ToolbarItem(placement: .cancellationAction) {
                                Button("Close") { selectedPosition = nil }
                            }
                        }
                }
                .frame(minWidth: 800, minHeight: 600)
            }
        }
    }
}

struct AccountSectionView: View {
    let title: String
    let accountData: AccountData?
    let isLoading: Bool
    let accentColor: Color
    let onPositionTap: (Position) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text(title)
                    .font(.caption)
                    .fontWeight(.semibold)
                    .foregroundStyle(.secondary)
                
                Spacer()
                
                if isLoading {
                    ProgressView().scaleEffect(0.5)
                }
            }
            .padding(.horizontal, 4)

            if let data = accountData {
                HStack(spacing: 20) {
                    // Metrics Column
                    VStack(spacing: 12) {
                        MetricRow(title: "Portfolio Value", value: String(format: "£%.2f", data.summary.currentValue ?? 0), icon: "banknote.fill", color: accentColor)
                        MetricRow(title: "Net Return", value: {
                            if let pnl = data.summary.totalPnl, let invested = data.summary.totalInvested, invested > 0 {
                                return String(format: "%+%.1f%%", (pnl / invested) * 100)
                            }
                            return "0.0%"
                        }(), icon: "percent", color: (data.summary.totalPnl ?? 0) >= 0 ? .growinGreen : .growinRed)
                        MetricRow(title: "Available Cash", value: String(format: "£%.2f", data.summary.cashBalance?.free ?? 0), icon: "pouch.fill", color: .growinOrange)
                    }
                    .frame(width: 200)

                    // Holdings Visualization
                    GlassCard {
                        VStack(alignment: .leading, spacing: 12) {
                            Text("Top Holdings")
                                .font(.system(size: 13, weight: .bold, design: .rounded))
                                .foregroundStyle(.secondary)
                            
                            ScrollView(.horizontal, showsIndicators: false) {
                                HStack(spacing: 12) {
                                    ForEach(data.positions.prefix(5)) { position in
                                        PositionMiniCard(position: position) {
                                            onPositionTap(position)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            } else if !isLoading {
                GlassCard {
                    ContentUnavailableView("No Active Positions", systemImage: "tray.fill", description: Text("You have no open positions in this account."))
                        .scaleEffect(0.7)
                }
            }
        }
    }
}

struct MetricRow: View {
    let title: String
    let value: String
    let icon: String
    let color: Color
    
    var body: some View {
        GlassCard {
            HStack(spacing: 12) {
                Image(systemName: icon)
                    .font(.system(size: 14))
                    .foregroundStyle(color)
                    .frame(width: 28, height: 28)
                    .background(color.opacity(0.1))
                    .clipShape(Circle())
                
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(.secondary)
                    Text(value)
                        .font(.system(size: 15, weight: .heavy, design: .rounded))
                        .foregroundStyle(.white)
                }
                Spacer()
            }
            .padding(.vertical, -4)
        }
    }
}

struct PositionMiniCard: View {
    let position: Position
    let action: () -> Void
    
    var body: some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 8) {
                Text(position.ticker ?? "???")
                    .font(.system(size: 14, weight: .black, design: .rounded))
                    .foregroundStyle(.white)
                
                Text(String(format: "%+.1f%%", (position.ppl ?? 0) / ((position.currentPrice ?? 1) * (position.quantity ?? 1)) * 100))
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle((position.ppl ?? 0) >= 0 ? Color.growinGreen : Color.growinRed)
            }
            .padding(12)
            .frame(width: 100, height: 70)
            .background(.white.opacity(0.05))
            .glassEffect(.thin.interactive(), in: .rect(cornerRadius: 12))
        }
        .buttonStyle(.plain)
    }
}