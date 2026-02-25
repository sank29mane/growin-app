//  GrowinApp.swift
//  Growin
//  Created by Sanket Mane on 22/06/2025.

import SwiftUI

@main
struct GrowinApp: App {
    @State private var isBackendRunning = false
    @State private var portfolioObserver = PortfolioSummaryObserver.shared
    
    init() {
        // Initialize Notification Manager
        NotificationManager.shared.requestAuthorization()
        
        // Register Services Provider
        NSApplication.shared.servicesProvider = GrowinServices()
    }
    
    var body: some Scene {
        WindowGroup {
            ContentView()
                .preferredColorScheme(.dark)
                .onAppear {
                    checkBackendConnection()
                }
                .onReceive(NotificationCenter.default.publisher(for: NSNotification.Name("DeepLinkTicker"))) { notification in
                    // Logic to handle deep link - usually switch tab and search
                    print("Deep linking to ticker: \(notification.userInfo?["ticker"] ?? "")")
                }
        }
        .windowStyle(.automatic)
        .windowToolbarStyle(.unified(showsTitle: true))
        .commands {
            CommandGroup(replacing: .newItem) { }
            
            CommandMenu("Backend") {
                Button("Check Connection") {
                    checkBackendConnection()
                }
                .keyboardShortcut("r", modifiers: .command)
                
                Button("Launch Terminal") {
                    openTerminalWithBackend()
                }
                .keyboardShortcut("t", modifiers: [.command, .shift])
                
                Divider()
                
                Button("Trigger Test Alert") {
                    NotificationManager.shared.scheduleTradeSuggestion(ticker: "AAPL", action: "buy", confidence: 0.85)
                }
            }
        }
        
        Settings {
            SettingsView()
                .frame(width: 500, height: 600)
        }
        
        // Native macOS Menu Bar Extra
        MenuBarExtra(portfolioObserver.menuBarLabel, systemImage: portfolioObserver.menuBarIcon) {
            Group {
                if let summary = portfolioObserver.lastSummary {
                    let totalVal = summary.currentValue ?? 0
                    Text("Total Value: £\(totalVal.formatted(.number.precision(.fractionLength(2))))")
                    
                    let pnl = summary.totalPnl ?? 0
                    Text("Day's P&L: \(pnl >= 0 ? "+" : "")£\(pnl.formatted(.number.precision(.fractionLength(2))))")
                        .foregroundColor(pnl >= 0 ? .green : .red)
                    
                    Divider()
                }
                
                Button("Open Portfolio") {
                    NSApp.activate(ignoringOtherApps: true)
                    DispatchQueue.main.async {
                        NotificationCenter.default.post(name: NSNotification.Name("NavigateToTab"), object: nil, userInfo: ["tab": "portfolio"])
                    }
                }
                .keyboardShortcut("P")
                
                Button("Quick Ask") {
                    NSApp.activate(ignoringOtherApps: true)
                    DispatchQueue.main.async {
                        NotificationCenter.default.post(name: NSNotification.Name("NavigateToTab"), object: nil, userInfo: ["tab": "chat"])
                    }
                }
                .keyboardShortcut("A")
                
                Divider()
                
                Button("Quit Growin") {
                    NSApplication.shared.terminate(nil)
                }
                .keyboardShortcut("q")
            }
        }
    }
    
    private func checkBackendConnection() {
        Task {
            let url = URL(string: "\(AppConfig.shared.baseURL)/health")!
            do {
                let (_, response) = try await URLSession.shared.data(from: url)
                if let httpResponse = response as? HTTPURLResponse {
                    isBackendRunning = httpResponse.statusCode == 200
                }
            } catch {
                isBackendRunning = false
            }
        }
    }
    
    private func openTerminalWithBackend() {
        let script = """
        tell application "Terminal"
            activate
            do script "cd '\(FileManager.default.currentDirectoryPath)' && ./start_backend.sh"
        end tell
        """
        if let appleScript = NSAppleScript(source: script) {
            var error: NSDictionary?
            appleScript.executeAndReturnError(&error)
        }
    }
}
