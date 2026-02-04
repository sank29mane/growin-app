import SwiftUI
import Charts
import Foundation

struct InteractiveChartView: View {
    let history: [TimeSeriesItem]
    let forecast: [TimeSeriesItem]
    let ticker: String
    let showTTMIndicator: Bool = true

    @State private var selectedDate: Date? = nil
    @State private var selectedPrice: Double? = nil

    // Separate data processing for TTM forecasts
    var historicalDataPoints: [(Date, Double)] {
        history.map {
            (Date(timeIntervalSince1970: Double($0.timestamp) / 1000.0), $0.close)
        }
    }

    var ttmForecastDataPoints: [(Date, Double)] {
        forecast.map {
            (Date(timeIntervalSince1970: Double($0.timestamp) / 1000.0), $0.close)
        }
    }

    // TTM Model metadata
    var hasTTMData: Bool {
        !forecast.isEmpty
    }

    var ttmForecastRange: (start: Date, end: Date)? {
        guard let first = forecast.first, let last = forecast.last else { return nil }
        return (
            Date(timeIntervalSince1970: Double(first.timestamp) / 1000.0),
            Date(timeIntervalSince1970: Double(last.timestamp) / 1000.0)
        )
    }

    var combinedData: [(Date, Double, String)] {
        var result: [(Date, Double, String)] = []

        let historyPoints = history.map {
            (Date(timeIntervalSince1970: Double($0.timestamp) / 1000.0), $0.close, "Historical")
        }
        result.append(contentsOf: historyPoints)

        let forecastPoints = forecast.map {
            (Date(timeIntervalSince1970: Double($0.timestamp) / 1000.0), $0.close, "TTM Forecast")
        }
        result.append(contentsOf: forecastPoints)

        return result
    }

    private var headerView: some View {
        HStack {
            VStack(alignment: .leading) {
                Text("\(ticker) Price Analysis")
                    .font(.headline)
                    .foregroundStyle(.white)

                if let price = selectedPrice, let date = selectedDate {
                    Text("\(price, specifier: "%.2f") at \(date, format: .dateTime.hour().minute().day())")
                        .font(.caption)
                        .foregroundStyle(.blue)
                } else {
                    HStack(spacing: 4) {
                        Text("Historical + TTM-R2 Forecast")
                            .font(.caption)
                            .foregroundStyle(.gray)
                        if showTTMIndicator {
                            Image(systemName: "brain")
                                .font(.caption2)
                                .foregroundStyle(.green.opacity(0.8))
                        }
                    }
                }
            }

            Spacer()

            // Enhanced Legend with TTM indicator
            HStack(spacing: 12) {
                LegendItem(label: "History", color: .blue, dashed: false)
                LegendItem(label: "TTM-R2 Forecast", color: .green, dashed: true)
                if showTTMIndicator && hasTTMData {
                    HStack(spacing: 4) {
                        Image(systemName: "brain.fill")
                            .font(.caption)
                            .foregroundStyle(.green)
                        Text("AI")
                            .font(.caption2)
                            .fontWeight(.bold)
                            .foregroundStyle(.green)
                    }
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(.green.opacity(0.1))
                    .cornerRadius(8)
                }
            }
        }
        .padding(.horizontal, 4)
    }

    private var chartView: some View {
        Chart {
            // Historical Line
            ForEach(history) { item in
                LineMark(
                    x: .value("Time", Date(timeIntervalSince1970: Double(item.timestamp) / 1000.0)),
                    y: .value("Price", item.close),
                    series: .value("Type", "History")
                )
                .foregroundStyle(.blue)
                .interpolationMethod(.catmullRom)
            }

            // TTM-R2 Forecast Line with enhanced styling
            ForEach(forecast) { item in
                LineMark(
                    x: .value("Time", Date(timeIntervalSince1970: Double(item.timestamp) / 1000.0)),
                    y: .value("Price", item.close),
                    series: .value("Type", "TTM Forecast")
                )
                .foregroundStyle(.green)
                .lineStyle(StrokeStyle(lineWidth: 3, dash: [8, 4]))
                .interpolationMethod(.catmullRom)
            }

            // Transition marker between historical and TTM data
            if let lastHistoricalDate = history.last?.timestamp,
               let firstForecastDate = forecast.first?.timestamp,
               lastHistoricalDate < firstForecastDate {
                RuleMark(x: .value("Transition", Date(timeIntervalSince1970: Double(lastHistoricalDate) / 1000.0)))
                    .foregroundStyle(.orange.opacity(0.5))
                    .lineStyle(StrokeStyle(lineWidth: 1, dash: [2, 2]))
                    .annotation(position: .top, alignment: .leading) {
                        Text("TTM-R2")
                            .font(.caption2)
                            .foregroundStyle(.orange)
                            .padding(.horizontal, 4)
                            .padding(.vertical, 2)
                            .background(.black.opacity(0.7))
                            .cornerRadius(3)
                    }
            }

            // Selection Rule with enhanced TTM information
            if let selectedDate = selectedDate {
                RuleMark(x: .value("Selected", selectedDate))
                    .foregroundStyle(.white.opacity(0.3))
                    .offset(yStart: -10)
                    .annotation(position: .top) {
                        if let price = selectedPrice {
                            let isTTMData = ttmForecastDataPoints.contains { abs($0.0.timeIntervalSince(selectedDate)) < 3600 } // Within 1 hour
                            VStack(spacing: 2) {
                                Text("\(price, specifier: "%.2f")")
                                    .font(.caption)
                                    .fontWeight(.bold)
                                Text(isTTMData ? "TTM-R2 Forecast" : "Historical")
                                    .font(.caption2)
                                    .foregroundStyle(isTTMData ? .green : .blue)
                            }
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(.ultraThinMaterial)
                            .cornerRadius(6)
                            .shadow(radius: 2)
                        }
                    }
            }
        }
        .chartXAxis {
            AxisMarks(values: .stride(by: .day)) { value in
                AxisGridLine()
                AxisValueLabel(format: .dateTime.day().month())
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading) { value in
                AxisGridLine()
                AxisValueLabel()
            }
        }
        .chartYScale(domain: .automatic(includesZero: false))
        .frame(height: 200)
        .padding(.top, 8)
        .chartOverlay { proxy in
            GeometryReader { geometry in
                Rectangle().fill(.clear).contentShape(Rectangle())
                    .gesture(
                        DragGesture()
                            .onChanged { value in
                                guard let plotFrame = proxy.plotFrame else { return }
                                let origin = geometry[plotFrame].origin
                                let location = CGPoint(
                                    x: value.location.x - origin.x,
                                    y: value.location.y - origin.y
                                )

                                if let date: Date = proxy.value(atX: location.x) {
                                    selectedDate = date
                                    resolvePrice(at: date)
                                }
                            }
                            .onEnded { _ in
                                selectedDate = nil
                                selectedPrice = nil
                            }
                    )
                    .onHover { isHovering in
                        // Could add hover effects here in the future
                    }
            }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            headerView

            chartView
        }
        .padding(16)
        .background {
            RoundedRectangle(cornerRadius: 16)
                .fill(.ultraThinMaterial)
                .overlay {
                    RoundedRectangle(cornerRadius: 16)
                        .stroke(.white.opacity(0.1), lineWidth: 1)
                }
        }
    }
    
    private func resolvePrice(at date: Date) {
        let all = combinedData.sorted { $0.0 < $1.0 }
        
        // Find closest point
        if let closest = all.min(by: { abs($0.0.timeIntervalSince(date)) < abs($1.0.timeIntervalSince(date)) }) {
            selectedPrice = closest.1
        }
    }
}

struct LegendItem: View {
    let label: String
    let color: Color
    let dashed: Bool
    
    var body: some View {
        HStack(spacing: 4) {
            if dashed {
                Capsule()
                    .stroke(color, style: StrokeStyle(lineWidth: 2, dash: [2, 2]))
                    .frame(width: 12, height: 4)
            } else {
                Capsule()
                    .fill(color)
                    .frame(width: 12, height: 4)
            }
            Text(label)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(.gray)
        }
    }
}
