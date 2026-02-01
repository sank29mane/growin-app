import SwiftUI
import Charts

struct StockChartView: View {
    @StateObject var viewModel: StockChartViewModel
    @State private var selectedPoint: ChartDataPoint?
    @State private var chartColor: Color = .green
    
    let timeframes = ["1Day", "1Week", "1Month", "3Month", "1Year", "Max"]
    
    var body: some View {
        ScrollView {
            VStack(spacing: 20) {
                // Provider notification banner
                if viewModel.showProviderNotification {
                    HStack {
                        Image(systemName: "exclamationmark.triangle")
                            .foregroundColor(.orange)
                        Text(viewModel.providerNotificationMessage)
                            .font(.caption)
                            .foregroundColor(.primary)
                        Spacer()
                        Button(action: {
                            viewModel.showProviderNotification = false
                        }) {
                            Image(systemName: "xmark")
                                .foregroundColor(.secondary)
                        }
                    }
                    .padding(.horizontal)
                    .padding(.vertical, 8)
                    .background(Color.orange.opacity(0.1))
                    .cornerRadius(8)
                    .transition(.slide)
                }

                headerView
                chartContainerView
                timeframePickerView
                
                // Advanced Features - REAL ANALYSIS
                analysisView
            }
            .padding(.vertical)
        }
        .background(
            RoundedRectangle(cornerRadius: 24)
                .fill(Color.secondary.opacity(0.05))
                .overlay(
                    RoundedRectangle(cornerRadius: 24)
                        .stroke(Color.secondary.opacity(0.1), lineWidth: 1)
                )
        )
        .onAppear {
            updateChartColor()
        }
        .onChange(of: viewModel.chartData) { old, new in
            updateChartColor()
        }
    }
    
    @ViewBuilder
    private var headerView: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Chart Title and Description
            VStack(alignment: .leading, spacing: 4) {
                Text(viewModel.chartTitle.isEmpty ? viewModel.symbol.uppercased() : viewModel.chartTitle)
                    .font(.system(size: 18, weight: .bold))
                    .foregroundColor(.white)

                if !viewModel.chartDescription.isEmpty {
                    Text(viewModel.chartDescription)
                        .font(.system(size: 12))
                        .foregroundColor(.white.opacity(0.7))
                }
            }

            // Price and Change Info
            HStack(alignment: .bottom) {
                VStack(alignment: .leading, spacing: 4) {
                    if let selected = selectedPoint {
                        Text(selected.close, format: .currency(code: viewModel.currency))
                            .font(.system(size: 32, weight: .bold, design: .rounded))
                        Text(selected.date, style: .date)
                            .font(.caption)
                            .foregroundColor(.secondary)
                        Text("\(viewModel.market) • \(viewModel.provider)")
                            .font(.caption2)
                            .foregroundColor(.secondary.opacity(0.7))
                    } else if let last = viewModel.chartData.last {
                        Text(last.close, format: .currency(code: viewModel.currency))
                            .font(.system(size: 32, weight: .bold, design: .rounded))

                        if let first = viewModel.chartData.first {
                            let change = last.close - first.close
                            let percent = (change / first.close) * 100

                            HStack(spacing: 4) {
                                Image(systemName: change >= 0 ? "arrow.up.right" : "arrow.down.right")
                                Text("\(change >= 0 ? "+" : "")\(change, specifier: "%.2f") (\(percent, specifier: "%.2f")%)")
                            }
                            .font(.subheadline.bold())
                            .foregroundColor(change >= 0 ? .green : .red)
                        }
                    } else {
                        Text("\(viewModel.currency == "GBP" ? "£" : "$")0.00")
                            .font(.system(size: 32, weight: .bold, design: .rounded))
                            .redacted(reason: .placeholder)
                    }
                }

                Spacer()

                // Action Buttons
                HStack(spacing: 8) {
                    // Share Link
                    ShareLink(item: "Check out this \(viewModel.symbol) analysis on Growin! Current Price: \(viewModel.currency) \(String(format: "%.2f", viewModel.chartData.last?.close ?? 0))") {
                        Image(systemName: "square.and.arrow.up")
                            .font(.system(size: 12))
                            .padding(8)
                            .background(Color.secondary.opacity(0.1))
                            .cornerRadius(8)
                    }
                    .buttonStyle(.plain)

                    // New Chat Button
                    Button(action: {
                        // Create new conversation with chart context
                        createNewChatFromChart()
                    }) {
                        HStack(spacing: 6) {
                            Image(systemName: "bubble.left.and.bubble.right")
                            Text("New Chat")
                        }
                        .font(.system(size: 12, weight: .semibold))
                        .padding(.horizontal, 12)
                        .padding(.vertical, 6)
                        .background(Color.blue.opacity(0.2))
                        .foregroundColor(.blue)
                        .cornerRadius(8)
                    }
                }
            }
        }
        .padding()
    }
    
    @ViewBuilder
    private var chartContainerView: some View {
        ZStack {
            if viewModel.isLoading {
                ProgressView()
            } else if viewModel.chartData.isEmpty {
                VStack {
                    Image(systemName: "chart.line.uptrend.xyaxis")
                        .font(.system(size: 40))
                        .foregroundColor(.secondary)
                    Text("No Data Available")
                        .font(.headline)
                    Text("Could not fetch historical data for \(viewModel.symbol)")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            } else {
                chartView
            }
        }
        .frame(height: 250)
    }
    
    @ViewBuilder
    private var timeframePickerView: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 12) {
                ForEach(timeframes, id: \.self) { tf in
                    Button(action: {
                        withAnimation(.spring()) {
                            viewModel.updateTimeframe(tf)
                        }
                    }) {
                        Text(tfShort(tf))
                            .font(.system(size: 14, weight: .semibold))
                            .padding(.horizontal, 16)
                            .padding(.vertical, 8)
                            .background(viewModel.selectedTimeframe == tf ? Color.accentColor : Color.secondary.opacity(0.1))
                            .foregroundColor(viewModel.selectedTimeframe == tf ? .white : .primary)
                            .clipShape(Capsule())
                    }
                }
            }
            .padding(.horizontal)
        }
    }
    
    private var chartView: some View {
        Chart {
            ForEach(viewModel.chartData) { point in
                AreaMark(
                    x: .value("Date", point.date),
                    yStart: .value("Baseline", minValue),
                    yEnd: .value("Price", point.close)
                )
                .foregroundStyle(
                    LinearGradient(
                        colors: [chartColor.opacity(0.6), chartColor.opacity(0.1)], // Increased opacity
                        startPoint: .top,
                        endPoint: .bottom
                    )
                )
                .interpolationMethod(.catmullRom)
                
                LineMark(
                    x: .value("Date", point.date),
                    y: .value("Price", point.close)
                )
                .foregroundStyle(chartColor)
                .interpolationMethod(.catmullRom)
                .lineStyle(StrokeStyle(lineWidth: 3, lineCap: .round, lineJoin: .round))
            }
            
            if let selected = selectedPoint {
                RuleMark(x: .value("Selected Date", selected.date))
                    .foregroundStyle(Color.secondary.opacity(0.5))
                    .lineStyle(StrokeStyle(lineWidth: 1, dash: [5, 5]))
                
                PointMark(
                    x: .value("Selected Date", selected.date),
                    y: .value("Selected Price", selected.close)
                )
                .foregroundStyle(chartColor)
                .symbolSize(100)
            }
        }
        .chartXAxis(.hidden)
        .chartYAxis(.hidden)
        .chartYScale(domain: minValue...maxValue)
        .drawingGroup() // Offload rendering to Metal for performance
        .chartOverlay { proxy in
            GeometryReader { geometry in
                Rectangle().fill(.clear).contentShape(Rectangle())
                    .gesture(
                        DragGesture(minimumDistance: 0)
                            .onChanged { value in
                                if let plotFrame = proxy.plotFrame {
                                    let x = value.location.x - geometry[plotFrame].origin.x
                                    if let date: Date = proxy.value(atX: x) {
                                        // Find closest point
                                        if let closest = viewModel.chartData.min(by: {
                                            abs($0.date.timeIntervalSince(date)) < abs($1.date.timeIntervalSince(date))
                                        }) {
                                            selectedPoint = closest
                                        }
                                    }
                                }
                            }
                            .onEnded { _ in
                                selectedPoint = nil
                            }
                    )
            }
        }
    }
    
    private var minValue: Double {
        let values = viewModel.chartData.map { $0.close }
        let min = values.min() ?? 0
        return min * 0.98
    }
    
    private var maxValue: Double {
        let values = viewModel.chartData.map { $0.close }
        let max = values.max() ?? 100
        return max * 1.02
    }
    
    private func updateChartColor() {
        guard let first = viewModel.chartData.first, let last = viewModel.chartData.last else { return }
        chartColor = last.close >= first.close ? .green : .red
    }
    
    private func tfShort(_ tf: String) -> String {
        switch tf {
        case "1Day": return "1D"
        case "1Week": return "1W"
        case "1Month": return "1M"
        case "3Month": return "3M"
        case "1Year": return "1Y"
        case "Max": return "ALL"
        default: return tf
        }
    }

    @ViewBuilder
    private var analysisView: some View {
        HStack(spacing: 16) {
            GlassCard {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Text("AI ANALYSIS")
                            .font(.system(size: 10, weight: .black, design: .monospaced))
                            .foregroundColor(.blue)
                        
                        Spacer()
                        
                        if let updated = viewModel.lastUpdated {
                            Text(updated, style: .relative)
                                .font(.system(size: 8))
                                .foregroundColor(.secondary)
                        }
                    }
                    
                    Text(viewModel.aiAnalysis)
                        .font(.system(size: 11))
                        .foregroundColor(.white.opacity(0.8))
                }
            }
            
            GlassCard {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        Text("ALGO SIGNALS")
                            .font(.system(size: 10, weight: .black, design: .monospaced))
                            .foregroundColor(.green)
                        
                        Spacer()
                        
                        if let updated = viewModel.lastUpdated {
                            Text(updated, style: .relative)
                                .font(.system(size: 8))
                                .foregroundColor(.secondary)
                        }
                    }
                    
                    Text(viewModel.algoSignals)
                        .font(.system(size: 11))
                        .foregroundColor(.white.opacity(0.8))
                }
            }
        }
        .padding(.horizontal)
    }

    private func createNewChatFromChart() {
        // This would navigate to chat view with chart context
        // For now, we'll implement a notification-based approach
        let chartContext: [String: Any] = [
            "symbol": viewModel.symbol,
            "timeframe": viewModel.selectedTimeframe,
            "currentPrice": viewModel.chartData.last?.close ?? 0,
            "chartTitle": viewModel.chartTitle
        ]

        NotificationCenter.default.post(
            name: NSNotification.Name("CreateChatFromChart"),
            object: nil,
            userInfo: chartContext
        )
    }
}
