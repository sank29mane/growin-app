import SwiftUI
import UserNotifications

class NotificationManager: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NotificationManager()
    
    private override init() {
        super.init()
        UNUserNotificationCenter.current().delegate = self
        registerCategories()
    }
    
    private func registerCategories() {
        let viewAction = UNNotificationAction(identifier: "VIEW_ACTION", title: "Analyze", options: [.foreground])
        let ignoreAction = UNNotificationAction(identifier: "IGNORE_ACTION", title: "Ignore", options: [.destructive])
        let category = UNNotificationCategory(identifier: "TRADE_CATEGORY", actions: [viewAction, ignoreAction], intentIdentifiers: [], options: [])
        UNUserNotificationCenter.current().setNotificationCategories([category])
    }
    
    func requestAuthorization() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .badge, .sound]) { granted, error in
            if granted {
                print("✅ Notification permission granted")
            } else if let error = error {
                print("❌ Notification permission error: \(error.localizedDescription)")
            }
        }
    }
    
    func scheduleTradeSuggestion(ticker: String, action: String, confidence: Double) {
        let content = UNMutableNotificationContent()
        content.title = "New Trade Suggestion: \(ticker)"
        content.body = "Growth Analyst suggests a \(action.uppercased()) with \(Int(confidence * 100))% confidence."
        content.sound = .default
        content.userInfo = ["type": "trade_suggestion", "ticker": ticker, "action": action]
        
        content.categoryIdentifier = "TRADE_CATEGORY"
        
        let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 1, repeats: false)
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: trigger)
        
        UNUserNotificationCenter.current().add(request)
    }
    
    func scheduleMorningReport(summary: String) {
        let content = UNMutableNotificationContent()
        content.title = "Good Morning! Your Growth Outlook"
        content.body = summary
        content.sound = .default
        
        // Schedule for 9:00 AM every day
        var dateComponents = DateComponents()
        dateComponents.hour = 9
        dateComponents.minute = 0
        
        let trigger = UNCalendarNotificationTrigger(dateMatching: dateComponents, repeats: true)
        let request = UNNotificationRequest(identifier: "MORNING_REPORT", content: content, trigger: trigger)
        
        UNUserNotificationCenter.current().add(request)
    }
    
    // MARK: - UNUserNotificationCenterDelegate
    
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        // Show notification even when app is in foreground
        completionHandler([.banner, .sound, .list])
    }
    
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler completionHandler: @escaping () -> Void) {
        let userInfo = response.notification.request.content.userInfo
        
        if response.actionIdentifier == "VIEW_ACTION" || response.actionIdentifier == UNNotificationDefaultActionIdentifier {
            // Deep link to the ticker analysis
            if let ticker = userInfo["ticker"] as? String {
                NotificationCenter.default.post(name: NSNotification.Name("DeepLinkTicker"), object: nil, userInfo: ["ticker": ticker])
            }
        }
        
        completionHandler()
    }
}
