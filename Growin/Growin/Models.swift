import Foundation

struct PortfolioSnapshot: Codable {
    let summary: PortfolioSummary?
    let positions: [Position]?
}

struct PortfolioSummary: Codable {
    let totalPositions: Int?
    let totalInvested: Double?
    let currentValue: Double?
    let totalPnl: Double?
    let totalPnlPercent: Double?
    let cashBalance: CashBalance?
    let accounts: [String: AccountSummary]?
    
    enum CodingKeys: String, CodingKey {
        case totalPositions = "total_positions"
        case totalInvested = "total_invested"
        case currentValue = "current_value"
        case totalPnl = "total_pnl"
        case totalPnlPercent = "total_pnl_percent"
        case cashBalance = "cash_balance"
        case accounts
    }
}

struct AccountSummary: Codable {
    let totalInvested: Double?
    let currentValue: Double?
    let totalPnl: Double?
    let cashBalance: CashBalance?
    
    enum CodingKeys: String, CodingKey {
        case totalInvested = "total_invested"
        case currentValue = "current_value"
        case totalPnl = "total_pnl"
        case cashBalance = "cash_balance"
    }
}

struct CashBalance: Codable {
    let total: Double?
    let free: Double?
}

struct Position: Codable, Identifiable {
    var id: String { 
        let t = ticker ?? UUID().uuidString
        let a = accountType ?? ""
        return "\(t)-\(a)"
    }
    let ticker: String?
    let name: String?
    let quantity: Double?
    let currentPrice: Double?
    let averagePrice: Double?
    let ppl: Double?
    let accountType: String?
    
    enum CodingKeys: String, CodingKey {
        case ticker, name, quantity
        case currentPrice = "currentPrice"
        case averagePrice = "averagePrice"
        case ppl
        case accountType = "account_type"
    }
}

// Chart API Response Models
struct ChartResponse: Codable {
    let data: [ChartDataPoint]
    let metadata: ChartMetadata
    let error: ChartErrorInfo?
}

struct ChartMetadata: Codable {
    let market: String
    let currency: String
    let symbol: String
    let ticker: String
    let provider: String
}

struct ChartErrorInfo: Codable {
    let code: String
    let message: String
    let providerErrors: [String]
    let fallbackUsed: Bool
    
    enum CodingKeys: String, CodingKey {
        case code, message
        case providerErrors = "provider_errors"
        case fallbackUsed = "fallback_used"
    }
}

struct TimeSeriesItem: Codable, Identifiable {
    var id: Double { Double(timestamp) }
    let timestamp: Int
    let open: Double
    let high: Double
    let low: Double
    let close: Double
    let volume: Double?
}

struct ChartDataPoint: Codable, Identifiable, Equatable {
    var id: String { timestamp }
    let timestamp: String
    let close: Double
    let high: Double?
    let low: Double?
    let open: Double?
    let volume: Int?
    
    var date: Date {
        // 1. Try ISO8601 with fractional seconds and internet date time (most common for modern APIs)
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: timestamp) {
            return date
        }
        
        // 2. Try standard ISO8601 (no fractional seconds)
        formatter.formatOptions = [.withInternetDateTime]
        if let date = formatter.date(from: timestamp) {
            return date
        }
        
        // 3. Manual Fallback using DateFormatter for flexible parsing
        let simpleFormatter = DateFormatter()
        simpleFormatter.locale = Locale(identifier: "en_US_POSIX")
        simpleFormatter.timeZone = TimeZone(secondsFromGMT: 0) // Assume UTC
        
        // Try common formats
        let formats = [
            "yyyy-MM-dd'T'HH:mm:ss.SSSSSSZ", // Python isoformat with microseconds & Z
            "yyyy-MM-dd'T'HH:mm:ss.SSSSSS",  // Python isoformat with microseconds (naive)
            "yyyy-MM-dd'T'HH:mm:ssZ",        // Standard ISO with Z
            "yyyy-MM-dd'T'HH:mm:ss",         // Naive ISO
            "yyyy-MM-dd HH:mm:ss",           // SQL-like
            "yyyy-MM-dd"                     // Date only
        ]
        
        for format in formats {
            simpleFormatter.dateFormat = format
            if let date = simpleFormatter.date(from: timestamp) {
                return date
            }
        }
        
        print("Warning: Failed to parse chart date: \(timestamp)")
        return Date() // Last resort fallback
    }
}

struct SystemStatusResponse: Codable {
    let system: GSystemStatus
    let agents: [String: GAgentDetailedStatus]
    let timestamp: Double
}

struct GSystemStatus: Codable {
    let uptime: Double?
    let memoryMb: Double?
    let activeThreads: Int?
    let mcp: GMCPStatus?
    let status: String?
    
    enum CodingKeys: String, CodingKey {
        case uptime
        case memoryMb = "memory_mb"
        case activeThreads = "active_threads"
        case mcp, status
    }
}

struct GMCPStatus: Codable {
    let connected: Bool?
    let serversCount: Int?
    
    enum CodingKeys: String, CodingKey {
        case connected
        case serversCount = "servers_count"
    }
}

struct GAgentDetailedStatus: Codable {
    let status: String
    let model: String?
    let detail: String?
    let timestamp: String?
}

struct GSpecialistAgentsStatus {
    private let agents: [String: GAgentDetailedStatus]

    init(agents: [String: GAgentDetailedStatus]) {
        self.agents = agents
    }

    var coordinator: GAgentDetailedStatus? {
        return agents["coordinator"]
    }

    var decisionAgent: GAgentDetailedStatus? {
        return agents["decision_agent"]
    }

    subscript(key: String) -> GAgentDetailedStatus? {
        return agents[key]
    }

    var keys: Dictionary<String, GAgentDetailedStatus>.Keys {
        return agents.keys
    }
}

// MARK: - Goal Planning Models

struct GoalPlan: Codable {
    let targetReturnsPercent: Double
    let durationYears: Double
    let initialCapital: Double
    let riskProfile: String
    let feasibilityScore: Double
    let optimalWeights: [String: Double]
    let expectedAnnualReturn: Double
    let expectedVolatility: Double
    let sharpeRatio: Double
    let simulatedFinalValueAvg: Double
    let probabilityOfSuccess: Double
    let suggestedInstruments: [SuggestedInstrument]
    let simulatedGrowthPath: [GrowthPoint]?
    let rebalancingStrategy: RebalancingStrategy?
    let implementation: GoalImplementation?
    
    enum CodingKeys: String, CodingKey {
        case targetReturnsPercent = "target_returns_percent"
        case durationYears = "duration_years"
        case initialCapital = "initial_capital"
        case riskProfile = "risk_profile"
        case feasibilityScore = "feasibility_score"
        case optimalWeights = "optimal_weights"
        case expectedAnnualReturn = "expected_annual_return"
        case expectedVolatility = "expected_volatility"
        case sharpeRatio = "sharpe_ratio"
        case simulatedFinalValueAvg = "simulated_final_value_avg"
        case probabilityOfSuccess = "probability_of_success"
        case suggestedInstruments = "suggested_instruments"
        case simulatedGrowthPath = "simulated_growth_path"
        case rebalancingStrategy = "rebalancing_strategy"
        case implementation
    }
}

struct GrowthPoint: Codable, Identifiable {
    var id: Double { year }
    let year: Double
    let value: Double
    let target: Double
}

struct SuggestedInstrument: Codable, Identifiable {
    var id: String { ticker }
    let ticker: String
    let name: String
    let weight: Double
    let expectedReturn: Double
    let category: String
    
    enum CodingKeys: String, CodingKey {
        case ticker, name, weight
        case expectedReturn = "expected_return"
        case category
    }
}

struct RebalancingStrategy: Codable {
    let frequency: String
    let trigger: String?
    let method: String
    let action: String
}

struct GoalImplementation: Codable {
    let type: String
    let name: String
    let icon: String?
    let action: String
}

// MARK: - Chat Models

struct MarketContextData: Codable {
    let forecast: ForecastData?
    let quant: QuantData?
    let research: ResearchData?
    let portfolio: PortfolioData?
    let price: PriceData?
    let whale: WhaleData?
}

struct WhaleData: Codable {
    let ticker: String
    let largeTrades: [WhaleTrade]?
    let unusualVolume: Bool
    let sentimentImpact: String
    let summary: String
    
    enum CodingKeys: String, CodingKey {
        case ticker, summary
        case largeTrades = "large_trades"
        case unusualVolume = "unusual_volume"
        case sentimentImpact = "sentiment_impact"
    }
}
struct WhaleTrade: Codable, Identifiable {
    var id: Double { Double(timestamp) }
    let price: Double
    let size: Double
    let valueUsd: Double
    let timestamp: Int
    let isWhale: Bool
    
    enum CodingKeys: String, CodingKey {
        case price, size, timestamp
        case valueUsd = "value_usd"
        case isWhale = "is_whale"
    }
}

struct ForecastData: Codable {
    let ticker: String
    let forecast24h: Double
    let confidence: String
    let trend: String
    let rawSeries: [TimeSeriesItem]?

    enum CodingKeys: String, CodingKey {
        case ticker, confidence, trend
        case forecast24h = "forecast_24h"
        case rawSeries = "raw_series"
    }
}

struct QuantData: Codable {
    let ticker: String
    let signal: String
    let rsi: Double?
    let macd: [String: Double]?
    let bollingerBands: [String: Double]?
    let supportLevel: Double?
    let resistanceLevel: Double?
    
    enum CodingKeys: String, CodingKey {
        case ticker, signal, rsi, macd
        case bollingerBands = "bollinger_bands"
        case supportLevel = "support_level"
        case resistanceLevel = "resistance_level"
    }
}

struct ResearchData: Codable {
    let ticker: String
    let sentimentScore: Double
    let sentimentLabel: String
    let topHeadlines: [String]
    
    enum CodingKeys: String, CodingKey {
        case ticker
        case sentimentScore = "sentiment_score"
        case sentimentLabel = "sentiment_label"
        case topHeadlines = "top_headlines"
    }
}

struct PriceData: Codable {
    let ticker: String
    let currentPrice: Double?
    let currency: String?
    let historySeries: [TimeSeriesItem]?
    
    enum CodingKeys: String, CodingKey {
        case ticker
        case currentPrice = "current_price"
        case currency
        case historySeries = "history_series"
    }
}

struct PortfolioData: Codable {
    let totalValue: Double?
    let totalPnL: Double?
    let pnlPercent: Double?
    let cashBalance: CashBalance?
    let snapshot: PortfolioSnapshot?
    
    enum CodingKeys: String, CodingKey {
        case totalValue = "total_value"
        case totalPnL = "total_pnl"
        case pnlPercent = "pnl_percent"
        case cashBalance = "cash_balance"
        case snapshot
    }
}

struct ChatMessageModel: Codable, Identifiable {
    var id: String { messageId }
    let messageId: String
    let role: String
    let content: String
    let timestamp: String
    let toolCalls: [ToolCall]?
    let toolCallId: String?
    let agentName: String?
    let modelName: String?
    let data: MarketContextData?

    enum CodingKeys: String, CodingKey {
        case messageId = "message_id"
        case role, content, timestamp
        case toolCalls = "tool_calls"
        case toolCallId = "tool_call_id"
        case agentName = "agent_name"
        case modelName = "model_name"
        case data
    }
    
    var isUser: Bool { role == "user" }
    var displayName: String {
        isUser ? "You" : (agentName ?? "AI Assistant")
    }
}

struct ToolCall: Codable {
    let id: String
    let type: String
    let function: ToolFunction
}

struct AnyCodable: Codable {
    let value: Any

    init(_ value: Any) {
        self.value = value
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let intValue = try? container.decode(Int.self) {
            value = intValue
        } else if let doubleValue = try? container.decode(Double.self) {
            value = doubleValue
        } else if let boolValue = try? container.decode(Bool.self) {
            value = boolValue
        } else if let stringValue = try? container.decode(String.self) {
            value = stringValue
        } else if let arrayValue = try? container.decode([AnyCodable].self) {
            value = arrayValue.map { $0.value }
        } else if let dictValue = try? container.decode([String: AnyCodable].self) {
            value = dictValue.mapValues { $0.value }
        } else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "AnyCodable value cannot be decoded")
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch value {
        case let intValue as Int:
            try container.encode(intValue)
        case let doubleValue as Double:
            try container.encode(doubleValue)
        case let boolValue as Bool:
            try container.encode(boolValue)
        case let stringValue as String:
            try container.encode(stringValue)
        case let arrayValue as [Any]:
            try container.encode(arrayValue.map { AnyCodable($0) })
        case let dictValue as [String: Any]:
            try container.encode(dictValue.mapValues { AnyCodable($0) })
        default:
            throw EncodingError.invalidValue(value, EncodingError.Context(codingPath: container.codingPath, debugDescription: "AnyCodable value cannot be encoded"))
        }
    }
}

struct ToolFunction: Codable {
    let name: String
    let arguments: [String: AnyCodable]
}

struct ChatRequest: Codable {
    let messages: [ChatMessageModel]
    let model: String
    let stream: Bool
    let tools: [ToolDefinition]?
}

struct ToolDefinition: Codable {
    let type: String
    let function: ToolFunctionDefinition
}

struct ToolFunctionDefinition: Codable {
    let name: String
    let description: String
    let parameters: [String: AnyCodable]
}

struct GrowinChatMessage: Codable {
    let message: String
    let conversationId: String?
    let modelName: String?
    let coordinatorModel: String?
    let apiKeys: [String: String]?
    let accountType: String?
    
    enum CodingKeys: String, CodingKey {
        case message
        case conversationId = "conversation_id"
        case modelName = "model_name"
        case coordinatorModel = "coordinator_model"
        case apiKeys = "api_keys"
        case accountType = "account_type"
    }
}

struct ChatResponse: Codable {
    let response: String
    let conversationId: String
    let agent: String
    let timestamp: String
    let toolCalls: [ToolCall]?
    let data: MarketContextData?
    
    enum CodingKeys: String, CodingKey {
        case response
        case conversationId = "conversation_id"
        case agent, timestamp
        case toolCalls = "tool_calls"
        case data
    }
}

struct ChatChoice: Codable {
    let index: Int
    let message: ChatMessageModel
    let finish_reason: String?
}

struct Usage: Codable {
    let prompt_tokens: Int
    let completion_tokens: Int
    let total_tokens: Int
}
