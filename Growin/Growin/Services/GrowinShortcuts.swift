import AppIntents
import SwiftUI

struct AskGrowinIntent: AppIntent {
    static var title: LocalizedStringResource = "Ask Growin"
    static var description = IntentDescription("Open Growin and ask the AI a financial question.")
    static var openAppWhenRun: Bool = true
    
    @Parameter(title: "Query", description: "What do you want to ask Growin?")
    var query: String?
    
    @MainActor
    func perform() async throws -> some IntentResult {
        if let query = query {
            NotificationCenter.default.post(name: NSNotification.Name("AskGrowinQuery"), object: nil, userInfo: ["query": query])
        }
        return .result()
    }
}

struct ShowPortfolioIntent: AppIntent {
    static var title: LocalizedStringResource = "Show Portfolio"
    static var description = IntentDescription("Show your current Growin portfolio status.")
    static var openAppWhenRun: Bool = true
    
    @MainActor
    func perform() async throws -> some IntentResult {
        NotificationCenter.default.post(name: NSNotification.Name("NavigateToTab"), object: nil, userInfo: ["tab": "portfolio"])
        return .result()
    }
}

struct GrowinShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: AskGrowinIntent(),
            phrases: [
                "Ask \(.applicationName) for financial advice",
                "Chat with \(.applicationName)"
            ],
            shortTitle: "Ask Growin",
            systemImageName: "bubble.left.and.bubble.right.fill"
        )
        
        AppShortcut(
            intent: ShowPortfolioIntent(),
            phrases: [
                "Show my \(.applicationName) portfolio",
                "How is my \(.applicationName) doing today?"
            ],
            shortTitle: "Show Portfolio",
            systemImageName: "chart.pie.fill"
        )
    }
}
