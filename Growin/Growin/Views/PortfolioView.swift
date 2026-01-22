import SwiftUI
import Charts

struct PortfolioView: View {
    @ObservedObject var viewModel: PortfolioViewModel
    
    var body: some View {
        ZStack {
            GradientBackground()
            
            VStack(spacing: 0) {
                // Header
                HStack {
                    AppHeader(
                        title: "Portfolio Intelligence",
                        subtitle: viewModel.lastUpdated != nil ? "As of \(viewModel.lastUpdated!.formatted(date: .omitted, time: .shortened))" : "Real-time portfolio pulse"
                    )
                    
                    Spacer()
                    
                    // Account Switcher
                    Picker("", selection: $viewModel.t212AccountType) {
                        Text("INVEST").tag("invest")
                        Text("ISA").tag("isa")
                    }
                    .pickerStyle(.segmented)
                    .frame(width: 150)
                    .onChange(of: viewModel.t212AccountType) { _, newValue in
                        Task {
                            await viewModel.switchAccount(newType: newValue)
                        }
                    }
                    .disabled(viewModel.isSwitchingAccount)
                    
                    Spacer().frame(width: 20)
                    
                    if viewModel.isLoading || viewModel.isSwitchingAccount {
                        ProgressView().tint(.white)
                    } else {
                        Button(action: { Task { await viewModel.fetchPortfolio() } }) {
                            Image(systemName: "arrow.clockwise")
                                .font(.system(size: 14, weight: .bold))
                                .foregroundStyle(.white)
                                .padding(8)
                                .background(Color.white.opacity(0.1))
                                .clipShape(Circle())
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding()
                
                ScrollView {
                    VStack(spacing: 24) {
                        if let snapshot = viewModel.snapshot {
                            // Summary Cards
                            MetricGrid(summary: snapshot.summary)
                                .transition(.opacity.combined(with: .move(edge: .top)))
                            
                            // Performance Chart
                            if !viewModel.portfolioHistory.isEmpty {
                                GlassCard {
                                    VStack(alignment: .leading, spacing: 12) {
                                        HStack {
                                            Text("Performance")
                                                .font(.system(size: 12, weight: .bold))
                                                .foregroundStyle(.secondary)
                                            
                                            Spacer()
                                            
                                            // Time range picker
                                            Picker("", selection: $viewModel.selectedTimeRange) {
                                                Text("1D").tag(TimeRange.day)
                                                Text("1W").tag(TimeRange.week)
                                                Text("1M").tag(TimeRange.month)
                                                Text("3M").tag(TimeRange.threeMonths)
                                                Text("1Y").tag(TimeRange.year)
                                                Text("ALL").tag(TimeRange.all)
                                            }
                                            .pickerStyle(.segmented)
                                            .frame(width: 200)
                                        }
                                        
                                        PerformanceLineChart(history: viewModel.portfolioHistory, timeRange: viewModel.selectedTimeRange)
                                            .frame(height: 200)
                                    }
                                }
                                .transition(.opacity.combined(with: .scale(scale: 0.95)))
                            }
                            
                            // Allocation Donut
                            if let positions = snapshot.positions, !positions.isEmpty {
                                GlassCard {
                                    VStack(alignment: .leading, spacing: 12) {
                                        Text("Market Value Distribution")
                                            .font(.system(size: 12, weight: .bold))
                                            .foregroundStyle(.secondary)
                                        
                                        AllocationDonutChart(positions: positions)
                                            .frame(height: 180)
                                    }
                                }
                                .transition(.opacity.combined(with: .scale(scale: 0.95)))
                            }
                            
                            // Top Positions
                            VStack(alignment: .leading, spacing: 16) {
                                Text("Top Holdings")
                                    .font(.system(size: 14, weight: .bold))
                                    .foregroundStyle(.white.opacity(0.7))
                                    .padding(.horizontal)
                                
                                LazyVStack(spacing: 12) {
                                    ForEach(snapshot.positions ?? []) { position in
                                        PositionDeepCard(position: position)
                                            .onTapGesture {
                                                viewModel.selectedPosition = position
                                            }
                                    }
                                }
                            }
                            .transition(.opacity.combined(with: .move(edge: .bottom)))
                        } else if !viewModel.isLoading {
                            ContentUnavailableView {
                                Label("No Connection", systemImage: "wifi.exclamationmark")
                            } description: {
                                if let error = viewModel.errorMsg {
                                    Text(error)
                                        .foregroundStyle(.secondary)
                                } else {
                                    Text("Unable to fetch your portfolio intelligence from the server.")
                                }
                            } actions: {
                                Button(action: {
                                    Task {
                                        await viewModel.fetchPortfolio()
                                        await viewModel.fetchHistory()
                                    }
                                }) {
                                    Label("Retry", systemImage: "arrow.clockwise")
                                }
                                .buttonStyle(.borderedProminent)
                            }
                        }
                    }
                    .padding()
                }
                .refreshable {
                    await viewModel.fetchPortfolio()
                    await viewModel.fetchHistory()
                }
            }
        }
        .sheet(item: $viewModel.selectedPosition) { position in
            if let ticker = position.ticker {
                NavigationStack {
                    StockChartView(viewModel: StockChartViewModel(symbol: ticker))
                        .toolbar {
                            ToolbarItem(placement: .cancellationAction) {
                                Button("Close") {
                                    viewModel.selectedPosition = nil
                                }
                            }
                        }
                }
                .frame(minWidth: 800, minHeight: 600)
            }
        }
        .onAppear {
            Task {
                await viewModel.onAppear()
            }
        }
        .onChange(of: viewModel.selectedTimeRange) { _, _ in
            Task { await viewModel.fetchHistory() }
        }
    }
}

struct MetricGrid: View {
    let summary: PortfolioSummary?
    
    var body: some View {
        VStack(spacing: 12) {
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], spacing: 12) {
                MiniMetricCard(title: "Total Amount", value: String(format: "£%.2f", (summary?.currentValue ?? 0) + (summary?.cashBalance?.free ?? 0)), icon: "chart.bar.fill", color: .blue)
                MiniMetricCard(title: "Portfolio Value", value: String(format: "£%.2f", summary?.currentValue ?? 0), icon: "briefcase.fill", color: .green)
                MiniMetricCard(title: "Total P&L", value: String(format: "£%.2f", summary?.totalPnl ?? 0), icon: "arrow.up.right.circle.fill", color: (summary?.totalPnl ?? 0) >= 0 ? .green : .red)
                MiniMetricCard(title: "Return", value: String(format: "%.2f%%", summary?.totalPnlPercent ?? 0), icon: "percent", color: .purple)
                MiniMetricCard(title: "Available Cash", value: String(format: "£%.2f", summary?.cashBalance?.free ?? 0), icon: "sterlingsign.circle.fill", color: .orange)
            }
            
            if let accounts = summary?.accounts, accounts.count > 1 {
                HStack(spacing: 8) {
                    ForEach(Array(accounts.keys).sorted(), id: \.self) { key in
                        if let acc = accounts[key] {
                            HStack {
                                Text(key.uppercased())
                                    .font(.system(size: 8, weight: .black))
                                Text(String(format: "£%.0f", acc.currentValue ?? 0))
                                    .font(.system(size: 10, weight: .bold))
                            }
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(Color.white.opacity(0.1))
                            .cornerRadius(6)
                        }
                    }
                }
                .padding(.top, 4)
            }
        }
    }
}

struct MiniMetricCard: View {
    let title: String
    let value: String
    let icon: String
    let color: Color
    
    var body: some View {
        GlassCard(cornerRadius: 12) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Image(systemName: icon)
                        .foregroundStyle(color)
                        .font(.system(size: 12))
                    Text(title)
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(.secondary)
                }
                
                Text(value)
                    .font(.system(size: 16, weight: .black))
                    .foregroundStyle(.white)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

struct AllocationDonutChart: View {
    let positions: [Position]
    
    var body: some View {
        Chart {
            ForEach(topFiveAllocation) { item in
                SectorMark(
                    angle: .value("Value", item.value),
                    innerRadius: .ratio(0.65),
                    angularInset: 2
                )
                .cornerRadius(4)
                .foregroundStyle(by: .value("Ticker", item.label))
            }
        }
        .chartLegend(position: .trailing)
    }
    
    private var topFiveAllocation: [AllocationItem] {
        let items = positions.map { pos in
            AllocationItem(label: pos.ticker ?? "???", value: (pos.currentPrice ?? 0) * (pos.quantity ?? 0))
        }.sorted { $0.value > $1.value }
        
        var result = Array(items.prefix(5))
        if items.count > 5 {
            let othersValue = items.dropFirst(5).reduce(0) { $0 + $1.value }
            result.append(AllocationItem(label: "Others", value: othersValue))
        }
        return result
    }
}

struct PositionDeepCard: View {
    let position: Position
    
    var body: some View {
        GlassCard(cornerRadius: 14) {
            HStack(spacing: 16) {
                VStack(spacing: 4) {
                    Circle()
                        .fill(Color.blue.opacity(0.1))
                        .frame(width: 36, height: 36)
                        .overlay(
                            Text(position.ticker?.prefix(1) ?? "?")
                                .font(.system(size: 14, weight: .bold))
                                .foregroundStyle(.blue)
                        )
                    
                    if let acc = position.accountType {
                        Text(acc.uppercased())
                            .font(.system(size: 6, weight: .black))
                            .padding(.horizontal, 4)
                            .padding(.vertical, 2)
                            .background(acc == "isa" ? Color.purple.opacity(0.2) : Color.blue.opacity(0.2))
                            .foregroundStyle(acc == "isa" ? .purple : .blue)
                            .cornerRadius(3)
                    }
                }
                
                VStack(alignment: .leading, spacing: 4) {
                    Text(position.name ?? position.ticker ?? "UNKNOWN")
                        .font(.system(size: 16, weight: .black))
                        .foregroundStyle(.white)
                    
                    if position.name != nil {
                        Text(position.ticker ?? "")
                            .font(.system(size: 10, weight: .bold))
                            .foregroundStyle(.white.opacity(0.4))
                    }
                    
                    Text("\(String(format: "%.2f", position.quantity ?? 0)) shares")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(.white.opacity(0.5))
                }
                
                Spacer()
                
                VStack(alignment: .trailing, spacing: 4) {
                    let value = (position.currentPrice ?? 0) * (position.quantity ?? 0)
                    Text(String(format: "£%.2f", value))
                        .font(.system(size: 16, weight: .black))
                        .foregroundStyle(.white)
                    
                    let pnl = position.ppl ?? 0
                    Text(String(format: "%@£%.2f", pnl >= 0 ? "+" : "", pnl))
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(pnl >= 0 ? .green : .red)
                }
            }
        }
    }
}

// MARK: - Supporting Types

enum TimeRange {
    case day, week, month, threeMonths, year, all
    
    var days: Int {
        switch self {
        case .day: return 1
        case .week: return 7
        case .month: return 30
        case .threeMonths: return 90
        case .year: return 365
        case .all: return 3650 // 10 years
        }
    }
}

struct PortfolioHistoryPoint: Codable, Identifiable {
    var id: String { timestamp }
    let timestamp: String
    let totalValue: Double
    let totalPnl: Double
    let cashBalance: Double
    
    enum CodingKeys: String, CodingKey {
        case timestamp
        case totalValue = "total_value"
        case totalPnl = "total_pnl"
        case cashBalance = "cash_balance"
    }
    
    var date: Date {
        // 1. Try ISO8601 with fractional seconds (e.g., 2024-03-21T10:00:00.000Z)
        let isoFormatter = ISO8601DateFormatter()
        isoFormatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = isoFormatter.date(from: timestamp) {
            return date
        }
        
        // 2. Try standard ISO8601 (e.g., 2024-03-21T10:00:00Z)
        let standardISO = ISO8601DateFormatter()
        if let date = standardISO.date(from: timestamp) {
            return date
        }
        
        // 3. Try fallback DateFormatter for simple formats (e.g., 2024-03-21)
        let fallbackFormatter = DateFormatter()
        fallbackFormatter.dateFormat = "yyyy-MM-dd"
        if let date = fallbackFormatter.date(from: timestamp) {
            return date
        }
        
        // 4. Try parsing as a Unix timestamp if it's a number
        if let interval = Double(timestamp) {
            return Date(timeIntervalSince1970: interval > 10000000000 ? interval / 1000 : interval)
        }
        
        return Date()
    }
}

struct PerformanceLineChart: View {
    let history: [PortfolioHistoryPoint]
    let timeRange: TimeRange
    
    var body: some View {
        if history.isEmpty {
            ContentUnavailableView("No Data", systemImage: "chart.line.uptrend.xyaxis")
                .scaleEffect(0.5)
        } else {
            // Compute Y-axis bounds with padding to avoid flat line scaling issues
            let values = history.map { $0.totalValue }
            let rawMin = values.min() ?? 0
            let rawMax = values.max() ?? 0
            
            // Add 10% padding on each side, handle case where min == max
            let range = rawMax - rawMin
            let padding = range > 0 ? range * 0.1 : max(rawMin * 0.1, 10)
            let minValue = rawMin - padding
            let maxValue = rawMax + padding
            
            // Also compute X-axis bounds from data
            let dates = history.map { $0.date }
            let minDate = dates.min() ?? Date()
            let maxDate = dates.max() ?? Date()
            
            Chart(history) { point in
                LineMark(
                    x: .value("Date", point.date),
                    y: .value("Value", point.totalValue)
                )
                .foregroundStyle(.blue)
                .lineStyle(StrokeStyle(lineWidth: 2))
                .interpolationMethod(.catmullRom)
                
                AreaMark(
                    x: .value("Date", point.date),
                    y: .value("Value", point.totalValue)
                )
                .foregroundStyle(
                    LinearGradient(
                        colors: [.blue.opacity(0.6), .blue.opacity(0.1)],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                )
                .interpolationMethod(.catmullRom)
            }
            .chartYAxis {
                AxisMarks(position: .leading) { value in
                    AxisGridLine()
                    AxisValueLabel()
                }
            }
            .chartXAxis {
                AxisMarks(values: .automatic(desiredCount: 5)) { value in
                    AxisGridLine()
                    AxisValueLabel(format: .dateTime.month().day())
                }
            }
            // Apply explicit axis scales to ensure proper chart rendering
            .chartYScale(domain: minValue...maxValue)
            .chartXScale(domain: minDate...maxDate)
        }
    }
}

