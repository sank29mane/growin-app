import SwiftUI

struct RiskAssessmentData: Codable {
    let status: String
    let confidence_score: Double
    let risk_assessment: String
    let compliance_notes: String
    let recommendation_adjustment: String?
    let requires_hitl: Bool
}

struct ConfidenceVisualizationView: View {
    let riskData: RiskAssessmentData
    let onConfirm: () -> Void
    let onReject: () -> Void
    
    @State private var animatePulse = false
    
    var body: some View {
        GlassCard(cornerRadius: 24) {
            VStack(spacing: 20) {
                // Header with Confidence Level
                HStack {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("RISK GOVERNANCE")
                            .premiumTypography(.overline)
                            .foregroundStyle(statusColor)
                        
                        Text(riskData.status)
                            .premiumTypography(.title)
                            .foregroundStyle(.white)
                    }
                    
                    Spacer()
                    
                    ConfidenceIndicator(score: riskData.confidence_score)
                }
                
                Divider().background(statusColor.opacity(0.2))
                
                // Risk Assessment Text
                VStack(alignment: .leading, spacing: 12) {
                    Label {
                        Text("Risk Audit")
                            .premiumTypography(.overline)
                            .foregroundStyle(.secondary)
                    } icon: {
                        Image(systemName: "shield.lefthalf.filled")
                            .foregroundStyle(statusColor)
                    }
                    
                    Text(riskData.risk_assessment)
                        .premiumTypography(.body)
                        .foregroundStyle(.white.opacity(0.9))
                        .lineSpacing(4)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                
                // Compliance Notes
                if !riskData.compliance_notes.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("COMPLIANCE NOTES")
                            .premiumTypography(.overline)
                            .font(.system(size: 9))
                            .foregroundStyle(.secondary)
                        
                        Text(riskData.compliance_notes)
                            .premiumTypography(.caption)
                            .foregroundStyle(statusColor.opacity(0.8))
                            .padding(10)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(statusColor.opacity(0.05))
                            .cornerRadius(12)
                            .overlay(
                                RoundedRectangle(cornerRadius: 12)
                                    .stroke(statusColor.opacity(0.2), lineWidth: 0.5)
                            )
                    }
                }
                
                // Recommendation Adjustment
                if let adjustment = riskData.recommendation_adjustment {
                    HStack(spacing: 12) {
                        Image(systemName: "arrow.triangle.2.circlepath")
                            .foregroundStyle(Color.stitchNeonCyan)
                        
                        VStack(alignment: .leading, spacing: 2) {
                            Text("SUGGESTED ADJUSTMENT")
                                .premiumTypography(.overline)
                                .font(.system(size: 8))
                            Text(adjustment)
                                .premiumTypography(.caption)
                                .foregroundStyle(.white)
                        }
                    }
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.stitchNeonCyan.opacity(0.1))
                    .cornerRadius(12)
                }
                
                // HITL Action Gate
                if riskData.requires_hitl {
                    VStack(spacing: 16) {
                        HStack {
                            Image(systemName: "hand.tap.fill")
                                .symbolEffect(.bounce, value: animatePulse)
                            Text("Manual Confirmation Required")
                                .premiumTypography(.overline)
                        }
                        .foregroundStyle(Color.stitchNeonIndigo)
                        .onAppear { animatePulse = true }
                        
                        SlideToConfirm(title: "SLIDE TO EXECUTE TRADE", action: onConfirm)
                        
                        Button("Cancel Order", action: onReject)
                            .premiumTypography(.caption)
                            .foregroundStyle(.secondary)
                            .buttonStyle(.plain)
                    }
                    .padding(.top, 8)
                } else {
                    PremiumButton(title: "Acknowledge", icon: "checkmark.circle.fill", action: onConfirm)
                }
            }
        }
        .padding(.horizontal)
        .transition(.asymmetric(insertion: .move(edge: .bottom).combined(with: .opacity), removal: .opacity))
    }
    
    private var statusColor: Color {
        switch riskData.status {
        case "APPROVED": return Color.stitchNeonGreen
        case "FLAGGED": return Color.stitchNeonYellow
        case "BLOCKED": return Color.growinRed
        default: return .secondary
        }
    }
}

#Preview {
    ZStack {
        Color.black.ignoresSafeArea()
        ConfidenceVisualizationView(
            riskData: RiskAssessmentData(
                status: "FLAGGED",
                confidence_score: 0.72,
                risk_assessment: "Ticker concentration exceeds 5% of portfolio. High volatility detected in tech sector.",
                compliance_notes: "ISA compliance: No prohibited fractional shares detected.",
                recommendation_adjustment: "Reduce position size by 50% to maintain diversification.",
                requires_hitl: true
            ),
            onConfirm: {},
            onReject: {}
        )
    }
}
