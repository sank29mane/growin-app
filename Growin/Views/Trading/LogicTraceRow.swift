import SwiftUI

/// A high-density archival ledger row for agent logic traces.
/// Enforces Monaco font and archival styling.
public struct LogicTraceRow: View {
    public enum LogLevel: String {
        case debug = "DEBUG"
        case info = "INFO"
        case exec = "EXEC"
        case warn = "WARN"
        case error = "ERROR"
        
        var color: Color {
            switch self {
            case .debug: return .gray
            case .info: return .white.opacity(0.8)
            case .exec: return .brutalChartreuse
            case .warn: return .yellow
            case .error: return .red
            }
        }
    }
    
    let timestamp: String
    let level: LogLevel
    let message: String
    
    public init(timestamp: String? = nil, level: LogLevel = .info, message: String) {
        if let timestamp = timestamp {
            self.timestamp = timestamp
        } else {
            let formatter = DateFormatter()
            formatter.dateFormat = "HH:mm:ss.SSS"
            self.timestamp = formatter.string(from: Date())
        }
        self.level = level
        self.message = message
    }
    
    public var body: some View {
        HStack(spacing: 8) {
            // Technical Timestamp
            Text("[\(timestamp)]")
                .font(SovereignTheme.Fonts.monacoTechnical(size: 10))
                .foregroundStyle(Color.white.opacity(0.4))
                .frame(width: 80, alignment: .leading)
            
            // Log Level
            Text(level.rawValue)
                .font(SovereignTheme.Fonts.monacoTechnical(size: 10))
                .foregroundStyle(level.color)
                .frame(width: 45, alignment: .leading)
            
            // Trace Message
            Text(message)
                .font(SovereignTheme.Fonts.monacoTechnical(size: 11))
                .foregroundStyle(Color.brutalOffWhite)
                .lineLimit(1)
            
            Spacer()
        }
        .padding(.horizontal, 8)
        .frame(height: 22) // High-density (18-24px requirement)
        .background(Color(hex: "1C1B1B")) // Row tonal layering (1C1B1B from plan)
        .overlay(
            VStack {
                Spacer()
                // 1px tonal line for divider
                Rectangle()
                    .fill(Color.white.opacity(0.08))
                    .frame(height: 1)
            }
        )
    }
}
