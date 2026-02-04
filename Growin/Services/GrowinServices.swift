import AppKit
import SwiftUI

class GrowinServices: NSObject {
    @objc func analyzeTicker(_ pboard: NSPasteboard, userData: String, error: AutoreleasingUnsafeMutablePointer<NSString>) {
        guard let ticker = pboard.string(forType: .string) else { return }
        
        // Clean the ticker string
        let cleanTicker = ticker.trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: "$", with: "")
        
        DispatchQueue.main.async {
            // Activate app
            NSApp.activate(ignoringOtherApps: true)
            
            // Post notification for ChatView to handle
            NotificationCenter.default.post(
                name: NSNotification.Name("CreateChatFromTickerSearch"),
                object: nil,
                userInfo: ["ticker": cleanTicker]
            )
            
            // Switch to chat tab
            NotificationCenter.default.post(
                name: NSNotification.Name("NavigateToTab"),
                object: nil,
                userInfo: ["tab": "chat"]
            )
        }
    }
}

// In GrowinApp or a bootstrapping class, we would register this:
// NSApplication.shared.servicesProvider = GrowinServices()
// NSUpdateDynamicServices()
