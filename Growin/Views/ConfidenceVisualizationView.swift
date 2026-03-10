import SwiftUI

struct RiskAssessmentData: Codable {
    let status: String
    let confidenceScore: Double
    let riskAssessment: String
    let complianceNotes: String
    let recommendationAdjustment: String?
    let requiresHitl: Bool
    
    enum CodingKeys: String, CodingKey {
        case status
        case confidenceScore = "confidence_score"
        case riskAssessment = "risk_assessment"
        case complianceNotes = "compliance_notes"
        case recommendationAdjustment = "recommendation_adjustment"
        case requiresHitl = "requires_hitl"
    }
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
                    
                    ConfidenceIndicator(score: riskData.confidenceScore)
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
                    
                    Text(riskData.riskAssessment)
                        .premiumTypography(.body)
                        .foregroundStyle(.white.opacity(0.9))
                        .lineSpacing(4)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                
                // Compliance Notes
                if !riskData.complianceNotes.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("COMPLIANCE NOTES")
                            .premiumTypography(.overline)
                            .font(.system(size: 9))
                            .foregroundStyle(.secondary)
                        
                        Text(riskData.complianceNotes)
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
                if let adjustment = riskData.recommendationAdjustment {
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
                if riskData.requiresHitl {
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
                confidenceScore: 0.72,
                riskAssessment: "Ticker concentration exceeds 5% of portfolio. High volatility detected in tech sector.",
                complianceNotes: "ISA compliance: No prohibited fractional shares detected.",
                recommendationAdjustment: "Reduce position size by 50% to maintain diversification.",
                requiresHitl: true
            ),
            onConfirm: {},
            onReject: {}
        )
    }
}
