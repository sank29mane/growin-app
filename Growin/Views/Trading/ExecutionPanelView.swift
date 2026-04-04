import SwiftUI

/// ExecutionPanelView: The high-precision trade entry & risk calibration panel.
/// Optimized for macOS Native performance on M4 Pro.
struct ExecutionPanelView: View {
    @Binding var isPresented: Bool
    let asset: SovereignUtils.ExecutionAsset?
    
    @State private var quantity: String = ""
    @State private var limitPrice: String = ""
    @State private var orderType: OrderType = .market
    @State private var isConfirmed: Bool = false
    @State private var showStrategyOverlay: Bool = false
    
    enum OrderType: String, CaseIterable {
        case market = "MARKET"
        case limit = "LIMIT"
    }
    
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Header: Trace Identification
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("EXECUTION PROTOCOL")
                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                        .foregroundStyle(Color.brutalChartreuse)
                    
                    if let asset = asset {
                        Text(asset.ticker)
                            .font(SovereignTheme.Fonts.notoSerif(size: 24, weight: .regular))
                    } else {
                        Text("SELECT ASSET")
                            .font(SovereignTheme.Fonts.notoSerif(size: 24, weight: .regular))
                            .opacity(0.3)
                    }
                }
                
                Spacer()
                
                Button(action: { isPresented = false }) {
                    Image(systemName: "xmark")
                        .font(.system(size: 12, weight: .bold))
                        .foregroundStyle(Color.brutalOffWhite)
                        .padding(8)
                        .background(Color.white.opacity(0.1))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Close execution panel")
                .accessibilityAddTraits(.isButton)
            }
            .padding(24)
            .background(Color.black.opacity(0.2))
            
            ScrollView {
                VStack(alignment: .leading, spacing: 32) {
                    // Section 1: Order Type
                    VStack(alignment: .leading, spacing: 12) {
                        Label("ORDER TYPE", systemImage: "bolt.fill")
                            .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                            .foregroundStyle(Color.brutalOffWhite.opacity(0.5))
                        
                        HStack(spacing: 0) {
                            ForEach(OrderType.allCases, id: \.self) { type in
                                Button(action: { 
                                    withAnimation(.interactiveSpring()) {
                                        orderType = type 
                                    }
                                }) {
                                    Text(type.rawValue)
                                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold))
                                        .frame(maxWidth: .infinity)
                                        .padding(.vertical, 12)
                                        .background(orderType == type ? Color.brutalChartreuse : Color.clear)
                                        .foregroundStyle(orderType == type ? Color.black : Color.brutalOffWhite)
                                        .border(SovereignTheme.Colors.technicalBorder, width: 0.5)
                                }
                                .buttonStyle(.plain)
                                .accessibilityLabel("\(type.rawValue) Order")
                                .accessibilityAddTraits(orderType == type ? [.isButton, .isSelected] : .isButton)
                                .accessibilityHint("Selects \(type.rawValue) order type")
                            }
                        }
                    }
                    
                    // Section 2: Inputs
                    VStack(alignment: .leading, spacing: 20) {
                        // Quantity
                        VStack(alignment: .leading, spacing: 8) {
                            Text("QUANTITY")
                                .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                                .foregroundStyle(Color.brutalOffWhite.opacity(0.4))
                            
                            TextField("0.00", text: $quantity)
                                .textFieldStyle(.plain)
                                .font(SovereignTheme.Fonts.monacoTechnical(size: 24))
                                .foregroundStyle(Color.brutalChartreuse)
                                .padding(16)
                                .background(Color.black.opacity(0.3))
                                .border(SovereignTheme.Colors.technicalBorder, width: 1)
                        }
                        
                        if orderType == .limit {
                            // Limit Price
                            VStack(alignment: .leading, spacing: 8) {
                                Text("LIMIT PRICE")
                                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                                    .foregroundStyle(Color.brutalOffWhite.opacity(0.4))
                                
                                TextField("0.00", text: $limitPrice)
                                    .textFieldStyle(.plain)
                                    .font(SovereignTheme.Fonts.monacoTechnical(size: 24))
                                    .foregroundStyle(Color.brutalOffWhite)
                                    .padding(16)
                                    .background(Color.black.opacity(0.3))
                                    .border(SovereignTheme.Colors.technicalBorder, width: 1)
                            }
                        }
                    }
                    
                    // Section 3: Risk Calibration
                    VStack(alignment: .leading, spacing: 16) {
                        HStack {
                            Text("AI RISK CALIBRATION")
                                .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                                .foregroundStyle(Color.brutalChartreuse)
                            Spacer()
                            Button(action: { showStrategyOverlay.toggle() }) {
                                Image(systemName: "info.circle")
                                    .font(.system(size: 12))
                                    .foregroundStyle(Color.brutalChartreuse)
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("Strategy parameters info")
                            .accessibilityAddTraits(.isButton)
                        }
                        
                        VStack(spacing: 12) {
                            RiskMetricRow(title: "PORTFOLIO IMPACT", value: "+1.2% EXP")
                            RiskMetricRow(title: "MARGIN USAGE", value: "0.00% (CASH)")
                            RiskMetricRow(title: "MAX LOSS AT 2σ", value: "-$42.10")
                        }
                        .padding(16)
                        .background(Color.brutalOffWhite.opacity(0.02))
                        .border(Color.white.opacity(0.05), width: 1)
                    }
                    
                    Spacer(minLength: 40)
                    
                    // Final Action: Slide-to-Confirm
                    VStack(spacing: 16) {
                        if isConfirmed {
                            VStack(spacing: 8) {
                                ProgressView()
                                    .progressViewStyle(.circular)
                                    .controlSize(.small)
                                Text("EXECUTING ARCHIVAL TRACE...")
                                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                                    .foregroundStyle(Color.brutalChartreuse)
                            }
                            .frame(height: 50)
                            .frame(maxWidth: .infinity)
                            .background(Color.brutalChartreuse.opacity(0.1))
                            .border(Color.brutalChartreuse, width: 1)
                        } else {
                            SovereignSlideToConfirm(isConfirmed: $isConfirmed) {
                                // Trigger actual backend execution
                                print("Committing order...")
                            }
                        }
                        
                        Text("VERIFIED BY SOVEREIGN AI • TRACE ID: \(UUID().uuidString.prefix(8))")
                            .font(SovereignTheme.Fonts.spaceGrotesk(size: 8))
                            .foregroundStyle(Color.brutalOffWhite.opacity(0.3))
                    }
                }
                .padding(24)
            }
        }
        .frame(width: 400)
        .background(Color.brutalRecessed)
        .border(SovereignTheme.Colors.technicalBorder, width: 1)
        .overlay {
            if showStrategyOverlay {
                StrategyOverlayView(isPresented: $showStrategyOverlay)
                    .transition(.move(edge: .trailing).combined(with: .opacity))
            }
        }
    }
}

private struct RiskMetricRow: View {
    let title: String
    let value: String
    
    var body: some View {
        HStack {
            Text(title)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 10))
                .foregroundStyle(Color.brutalOffWhite.opacity(0.4))
            Spacer()
            Text(value)
                .font(SovereignTheme.Fonts.monacoTechnical(size: 10))
                .foregroundStyle(Color.brutalOffWhite)
        }
    }
}

private struct StrategyOverlayView: View {
    @Binding var isPresented: Bool
    
    var body: some View {
        ZStack {
            Color.black.opacity(0.8)
                .onTapGesture { isPresented = false }
            
            VStack(alignment: .leading, spacing: 20) {
                Text("STRATEGY PARAMETERS")
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold))
                    .foregroundStyle(Color.brutalChartreuse)
                
                VStack(alignment: .leading, spacing: 12) {
                    Text("The Sovereign AI has calibrated this entry based on current LSE Leveraged ETF liquidity and M4 Pro local inference results.")
                        .font(SovereignTheme.Fonts.spaceGrotesk(size: 10))
                        .foregroundStyle(Color.brutalOffWhite.opacity(0.7))
                        .lineSpacing(4)
                    
                    Divider().background(Color.white.opacity(0.1))
                    
                    MetricItem(title: "Model Confidence", value: "0.942")
                    MetricItem(title: "Alpha Decay Rate", value: "0.002/hr")
                    MetricItem(title: "Execution Priority", value: "High (Tier 1)")
                }
                
                Spacer()
                
                Button("CLOSE TRACE") {
                    isPresented = false
                }
                .buttonStyle(.plain)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity)
                .background(Color.brutalOffWhite.opacity(0.1))
                .border(Color.white.opacity(0.2), width: 1)
                .accessibilityAddTraits(.isButton)
            }
            .padding(24)
            .background(Color.brutalCharcoal)
            .border(Color.brutalChartreuse.opacity(0.5), width: 1)
            .frame(width: 280, height: 350)
        }
    }
    
    private struct MetricItem: View {
        let title: String
        let value: String
        var body: some View {
            HStack {
                Text(title).font(SovereignTheme.Fonts.spaceGrotesk(size: 10))
                Spacer()
                Text(value).font(SovereignTheme.Fonts.monacoTechnical(size: 10)).acidAccent()
            }
        }
    }
}

#Preview {
    ZStack {
        Color.gray.ignoresSafeArea()
        ExecutionPanelView(
            isPresented: Binding.constant(true),
            asset: SovereignUtils.ExecutionAsset(ticker: "AAPL", currentPrice: 189.43)
        )
    }
}
