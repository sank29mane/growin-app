import SwiftUI

struct IntelligenceTraceView: View {
    let data: MarketContextData
    @State private var isExpanded: Bool = false
    @State private var showFullReasoning: Bool = false
    
    private let reasoningLimit = 500 // SOTA: Character limit for collapsed reasoning
    
    var body: some View {
        DisclosureGroup(isExpanded: $isExpanded) {
            VStack(alignment: .leading, spacing: 12) {
                // 1. SOTA 2026: Internal Reasoning (CoT)
                if let reasoning = data.reasoning {
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Text("INTERNAL LOGIC")
                                .font(.system(size: 9, weight: .black))
                                .foregroundStyle(.secondary)
                            Spacer()
                            
                            Button(action: {
                                NSPasteboard.general.clearContents()
                                NSPasteboard.general.setString(reasoning, forType: .string)
                            }) {
                                Label("COPY", systemImage: "doc.on.doc")
                                    .font(.system(size: 8, weight: .bold))
                            }
                            .buttonStyle(.plain)
                            .foregroundStyle(.blue)
                        }
                        
                        VStack(alignment: .leading, spacing: 4) {
                            let truncated = reasoning.count > reasoningLimit && !showFullReasoning
                            let textToShow = truncated ? String(reasoning.prefix(reasoningLimit)) + "..." : reasoning
                            
                            Text(textToShow)
                                .font(.system(size: 11, design: .monospaced))
                                .foregroundStyle(.white.opacity(0.8))
                                .lineSpacing(4)
                            
                            if reasoning.count > reasoningLimit {
                                Button(showFullReasoning ? "Show Less" : "Read Full Trace") {
                                    withAnimation(.spring()) {
                                        showFullReasoning.toggle()
                                    }
                                }
                                .font(.system(size: 10, weight: .bold))
                                .foregroundStyle(.blue)
                                .padding(.top, 2)
                            }
                        }
                        .padding(10)
                        .background(Color.black.opacity(0.3))
                        .cornerRadius(8)
                    }
                    .padding(.top, 4)
                    
                    Divider()
                        .background(Color.white.opacity(0.1))
                }

                // 2. Specialist Agent Steps
                VStack(alignment: .leading, spacing: 10) {
                    Text("SWARM EXECUTION")
                        .font(.system(size: 9, weight: .black))
                        .foregroundStyle(.secondary)
                        .padding(.bottom, 2)

                    if let quant = data.quant {
                        ReasoningStepRow(
                            agent: "QuantAgent",
                            status: "ANALYZED",
                            icon: "chart.line.uptrend.xyaxis",
                            color: Color.Persona.trader,
                            detail: "Signal: \(quant.signal)"
                        )
                    }
                    
                    if let forecast = data.forecast {
                        ReasoningStepRow(
                            agent: "ForecastingAgent",
                            status: "PREDICTED",
                            icon: "brain.head.profile",
                            color: .green,
                            detail: "Trend: \(forecast.trend)"
                        )
                    }
                    
                    if let research = data.research {
                        ReasoningStepRow(
                            agent: "ResearchAgent",
                            status: "SCANNED",
                            icon: "newspaper.fill",
                            color: Color.Persona.risk,
                            detail: "Sentiment: \(research.sentimentLabel)"
                        )
                    }
                    
                    if let whale = data.whale {
                        ReasoningStepRow(
                            agent: "WhaleAgent",
                            status: "DETECTED",
                            icon: "water.waves",
                            color: .indigo,
                            detail: whale.sentimentImpact
                        )
                    }
                }
            }
            .padding(.vertical, 8)
        } label: {
            HStack(spacing: 8) {
                ZStack {
                    Circle()
                        .fill(Color.blue.opacity(0.1))
                        .frame(width: 20, height: 20)
                    Image(systemName: "brain.head.profile")
                        .font(.system(size: 10))
                        .foregroundStyle(.blue)
                }
                
                Text("INTELLIGENCE TRACE")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(.white.opacity(0.7))
                
                Spacer()
                
                if let reasoning = data.reasoning {
                    Text("\(reasoning.count) chars")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Color.white.opacity(0.05))
                        .cornerRadius(4)
                }
            }
        }
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(Color.white.opacity(0.03))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.white.opacity(0.05), lineWidth: 1)
        )
    }
}

struct ReasoningStepRow: View, Equatable {
    let agent: String
    let status: String
    let icon: String
    let color: Color
    let detail: String
    
    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 12))
                .foregroundStyle(color)
                .frame(width: 24, height: 24)
                .background(color.opacity(0.1))
                .clipShape(Circle())
            
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text(agent)
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(.white)
                    Text(status)
                        .font(.system(size: 8, weight: .black))
                        .padding(.horizontal, 4)
                        .padding(.vertical, 1)
                        .background(color.opacity(0.2))
                        .foregroundStyle(color)
                        .cornerRadius(3)
                }
                
                Text(detail)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
            }
            
            Spacer()
        }
    }
}

struct RichDataView: View {
    let data: MarketContextData
    
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // 1. Top Level Metrics (Price & Sentiment)
            HStack(spacing: 8) {
                if let price = data.price {
                    PriceCard(price: price)
                }
                
                if let research = data.research {
                    SentimentCard(research: research)
                }
            }
            
            // 2. Interactive Chart
            if let history = data.price?.historySeries, 
               let forecast = data.forecast?.rawSeries,
               let ticker = data.price?.ticker {
                InteractiveChartView(history: history, forecast: forecast, ticker: ticker)
            }
            
            // 3. Forecast Summary
            if let forecast = data.forecast {
                ForecastCard(forecast: forecast)
            }
            
            // 4. Technicals
            if let quant = data.quant {
                TechnicalCard(quant: quant)
            }
            
            // 5. Whale Activity
            if let whale = data.whale {
                WhaleCard(whale: whale)
            }
            
            // 6. Portfolio (if relevant)
            if let portfolio = data.portfolio {
                PortfolioSnapshotCard(portfolio: portfolio)
            }
        }
        .padding(.top, 8)
    }
}

struct WhaleCard: View {
    let whale: WhaleData
    
    var impactColor: Color {
        switch whale.sentimentImpact {
        case "BULLISH": return .green
        case "BEARISH": return .red
        default: return .indigo
        }
    }
    
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Label("WHALE ALERT", systemImage: "water.waves")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(.indigo)
                Spacer()
                if whale.unusualVolume {
                    Text("UNUSUAL VOLUME")
                        .font(.system(size: 8, weight: .bold))
                        .padding(.horizontal, 4)
                        .padding(.vertical, 2)
                        .background(.red.opacity(0.2))
                        .foregroundStyle(.red)
                        .cornerRadius(4)
                }
            }
            
            Text(whale.summary)
                .font(.system(size: 13))
                .foregroundStyle(.white.opacity(0.9))
                .lineLimit(3)
            
            if let trades = whale.largeTrades, !trades.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(trades.prefix(5)) { trade in
                            VStack(alignment: .leading, spacing: 2) {
                                // Decimal conversion for formatting
                                let valueK = Double(truncating: trade.valueUsd as NSNumber) / 1000.0
                                Text("$\(String(format: "%.0f", valueK))k")
                                    .font(.system(size: 12, weight: .bold))
                                    .foregroundStyle(.white)
                                
                                let size = Double(truncating: trade.size as NSNumber)
                                Text("\(String(format: "%.0f", size)) shares")
                                    .font(.system(size: 9))
                                    .foregroundStyle(.secondary)
                            }
                            .padding(.horizontal, 8)
                            .padding(.vertical, 6)
                            .background(Color.white.opacity(0.05))
                            .cornerRadius(8)
                        }
                    }
                }
            }
        }
        .padding(12)
        .background(Color.indigo.opacity(0.1))
        .cornerRadius(12)
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.indigo.opacity(0.3), lineWidth: 1))
    }
}

struct PriceCard: View {
    let price: PriceData
    
    var body: some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text(price.ticker)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                
                let priceValue = Double(truncating: (price.currentPrice ?? Decimal(0)) as NSNumber)
                Text(verbatim: "\(price.currency ?? "£") \(String(format: "%.2f", priceValue))")
                    .font(.system(size: 16, weight: .bold))
                    .foregroundStyle(.white)
            }
            Spacer()
        }
        .padding(10)
        .background(Color.white.opacity(0.05))
        .cornerRadius(12)
    }
}

struct SentimentCard: View {
    let research: ResearchData
    
    var color: Color {
        switch research.sentimentLabel {
        case "BULLISH": return .green
        case "BEARISH": return .red
        default: return .gray
        }
    }
    
    var body: some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text("SENTIMENT")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                HStack(spacing: 4) {
                    Image(systemName: research.sentimentLabel == "BULLISH" ? "arrow.up.circle.fill" : (research.sentimentLabel == "BEARISH" ? "arrow.down.circle.fill" : "minus.circle.fill"))
                        .foregroundStyle(color)
                    Text(research.sentimentLabel)
                        .font(.system(size: 14, weight: .bold))
                        .foregroundStyle(color)
                }
            }
            Spacer()
        }
        .padding(10)
        .background(Color.white.opacity(0.05))
        .cornerRadius(12)
    }
}

struct ForecastCard: View {
    let forecast: ForecastData
    
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("AI FORECAST (24H)")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(.secondary)
                Spacer()
                Text(forecast.confidence)
                    .font(.caption2.bold())
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Color.blue.opacity(0.2))
                    .cornerRadius(4)
            }
            
            HStack(alignment: .lastTextBaseline) {
                let forecastValue = Double(truncating: forecast.forecast24h as NSNumber)
                Text(String(format: "£%.2f", forecastValue))
                    .font(.system(size: 20, weight: .bold))
                    .foregroundStyle(.white)
            }
        }
        .padding(12)
        .background(Color.blue.opacity(0.1))
        .cornerRadius(12)
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.blue.opacity(0.3), lineWidth: 1))
    }
}

struct TechnicalCard: View {
    let quant: QuantData
    
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("TECHNICALS")
                .font(.system(size: 10, weight: .bold))
                .foregroundStyle(.secondary)
            
            HStack(spacing: 16) {
                MetricItem(label: "Signal", value: quant.signal)
                
                if let rsi = quant.rsi {
                    let rsiVal = Double(truncating: rsi as NSNumber)
                    MetricItem(label: "RSI", value: String(format: "%.0f", rsiVal))
                }
                
                if let support = quant.supportLevel {
                    let supportVal = Double(truncating: support as NSNumber)
                    MetricItem(label: "Support", value: String(format: "%.1f", supportVal))
                }
            }
        }
        .padding(12)
        .background(Color.white.opacity(0.05))
        .cornerRadius(12)
    }
}

struct PortfolioSnapshotCard: View {
    let portfolio: PortfolioData
    
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("📊 PORTFOLIO SNAPSHOT")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(.secondary)
                Spacer()
            }
            
            // Main metrics row
            HStack(spacing: 16) {
                // Total Value
                VStack(alignment: .leading, spacing: 4) {
                    Text("Total Value")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    
                    let totalValue = Double(truncating: (portfolio.totalValue ?? Decimal(0)) as NSNumber)
                    Text(String(format: "£%.2f", totalValue))
                        .font(.system(size: 18, weight: .bold))
                        .foregroundStyle(.white)
                }
                
                Spacer()
                
                // P&L
                VStack(alignment: .center, spacing: 4) {
                    Text("P&L")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Text(String(format: "%+.2f%%", (portfolio.pnlPercent ?? 0.0) * 100))
                        .font(.system(size: 16, weight: .bold))
                        .foregroundStyle((portfolio.pnlPercent ?? 0.0) >= 0 ? .green : .red)
                }
                
                Spacer()
                
                // Cash Balance
                if let cashDict = portfolio.cashBalance, let cash = cashDict["total"] {
                    VStack(alignment: .trailing, spacing: 4) {
                        Text("Cash")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        
                        let cashVal = Double(truncating: cash as NSNumber)
                        Text(String(format: "£%.2f", cashVal))
                            .font(.system(size: 16, weight: .bold))
                            .foregroundStyle(.cyan)
                    }
                }
            }
        }
        .padding(14)
        .background(
            LinearGradient(
                colors: [Color.blue.opacity(0.15), Color.purple.opacity(0.1)],
                startPoint: .topLeading,
                endPoint: .bottomTrailing
            )
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .stroke(Color.blue.opacity(0.3), lineWidth: 1)
        )
        .cornerRadius(14)
    }
}

struct MetricItem: View {
    let label: String
    let value: String
    
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.subheadline.bold())
                .foregroundStyle(.white)
        }
    }
}
