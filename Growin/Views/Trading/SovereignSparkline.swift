import SwiftUI
import Charts

/// SovereignSparkline: A brutalist, density-first sparkline component.
/// Uses Swift Charts for high-performance 120Hz rendering.
struct SovereignSparkline: View {
    let data: [Double]
    let color: Color
    
    var body: some View {
        Chart {
            ForEach(Array(data.enumerated()), id: \.offset) { index, value in
                LineMark(
                    x: .value("Time", index),
                    y: .value("Price", value)
                )
                .foregroundStyle(color)
                .lineStyle(StrokeStyle(lineWidth: 1.5))
                
                AreaMark(
                    x: .value("Time", index),
                    y: .value("Price", value)
                )
                .foregroundStyle(
                    LinearGradient(
                        colors: [color.opacity(0.15), .clear],
                        startPoint: .top,
                        endPoint: .bottom
                    )
                )
            }
        }
        .chartXAxis(.hidden)
        .chartYAxis(.hidden)
        .chartLegend(.hidden)
        .frame(height: 32) // Compact height for high-density rows
    }
}

#Preview {
    ZStack {
        Color.black.ignoresSafeArea()
        SovereignSparkline(
            data: [10, 12, 11, 14, 13, 16, 15, 18],
            color: Color.brutalChartreuse
        )
        .padding()
    }
}
