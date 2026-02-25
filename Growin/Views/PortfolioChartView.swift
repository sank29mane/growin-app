import SwiftUI
import Charts

struct PortfolioChartView: View {
    let history: [PortfolioHistoryPoint]
    let forecast: ForecastData?
    
    var body: some View {
        Chart {
            // 1. Historical Data Points
            ForEach(history) { point in
                LineMark(
                    x: .value("Date", point.date),
                    y: .value("Value", point.totalValue)
                )
                .foregroundStyle(Color.blue)
                .lineStyle(StrokeStyle(lineWidth: 2))
                
                AreaMark(
                    x: .value("Date", point.date),
                    y: .value("Value", point.totalValue)
                )
                .foregroundStyle(
                    LinearGradient(
                        colors: [.blue.opacity(0.2), .blue.opacity(0)],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                )
            }
            
            // 2. AI Forecast Overlay (Wave 3)
            if let forecast = forecast, let rawSeries = forecast.rawSeries {
                ForEach(rawSeries) { item in
                    LineMark(
                        x: .value("Date", item.date),
                        y: .value("Forecast", item.close)
                    )
                    .foregroundStyle(Color.green)
                    .lineStyle(StrokeStyle(lineWidth: 2, dash: [5, 5]))
                }
            }
        }
        .chartXAxis {
            AxisMarks(values: .stride(by: .day, count: 7)) { _ in
                AxisGridLine().foregroundStyle(.white.opacity(0.05))
                AxisValueLabel(format: .dateTime.month().day())
            }
        }
        .chartYAxis {
            AxisMarks { value in
                AxisGridLine().foregroundStyle(.white.opacity(0.05))
                if let val = value.as(Decimal.self) {
                    AxisValueLabel("£\(Int(truncating: val as NSNumber))")
                }
            }
        }
        .frame(height: 200)
    }
}
