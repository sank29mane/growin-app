import SwiftUI
import UserNotifications

/// Service for handling passive income and risk-related push notifications.
/// Specifically focuses on "Gauge & Abort" triggers requiring manual HITL approval.
class PushNotificationService: NSObject, UNUserNotificationCenterDelegate {
    static let shared = PushNotificationService()
    
    private override init() {
        super.init()
        registerCategories()
    }
    
    private func registerCategories() {
        // HITL (Human-in-the-Loop) Action Categories
        let approveAction = UNNotificationAction(identifier: "APPROVE_ACTION", title: "Approve Trade", options: [.foreground])
        let abortAction = UNNotificationAction(identifier: "ABORT_ACTION", title: "Abort / Panic Exit", options: [.destructive, .foreground])
        
        let riskCategory = UNNotificationCategory(
            identifier: "RISK_HITL_CATEGORY",
            actions: [approveAction, abortAction],
            intentIdentifiers: [],
            options: [.customDismissAction]
        )
        
        UNUserNotificationCenter.current().setNotificationCategories([riskCategory])
    }
    
    /// Schedules a notification for a "Gauge & Abort" situation.
    /// Triggered when drawdown or confidence thresholds are breached.
    func scheduleRiskAlert(ticker: String, reason: String, confidence: Double, actionRequired: String) {
        let content = UNMutableNotificationContent()
        content.title = "⚠️ Risk Alert: \(ticker)"
        content.body = "Agent recommends \(actionRequired). \(reason) (Confidence: \(Int(confidence * 100))%)"
        content.sound = .default
        content.categoryIdentifier = "RISK_HITL_CATEGORY"
        content.userInfo = [
            "ticker": ticker,
            "action": actionRequired,
            "confidence": confidence,
            "type": "risk_hitl"
        ]
        
        let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 1, repeats: false)
        let request = UNNotificationRequest(identifier: "RISK_\(ticker)_\(UUID().uuidString)", content: content, trigger: trigger)
        
        UNUserNotificationCenter.current().add(request) { error in
            if let error = error {
                print("❌ Failed to schedule risk alert: \(error.localizedDescription)")
            } else {
                print("✅ Risk alert scheduled for \(ticker)")
            }
        }
    }
    
    // MARK: - UNUserNotificationCenterDelegate implementation
    
    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification, withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound, .list])
    }
    
    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler completionHandler: @escaping () -> Void) {
        let userInfo = response.notification.request.content.userInfo
        let ticker = userInfo["ticker"] as? String ?? "Unknown"
        
        switch response.actionIdentifier {
        case "APPROVE_ACTION":
            print("✅ User approved trade for \(ticker)")
            // Future: Trigger backend/agent to execute
            NotificationCenter.default.post(name: NSNotification.Name("HITLApproval"), object: nil, userInfo: userInfo)
            
        case "ABORT_ACTION":
            print("🛑 User aborted/panic exited for \(ticker)")
            // Future: Trigger backend/agent to exit
            NotificationCenter.default.post(name: NSNotification.Name("HITLAbort"), object: nil, userInfo: userInfo)
            
        default:
            // Default tap behavior
            if let type = userInfo["type"] as? String, type == "risk_hitl" {
                NotificationCenter.default.post(name: NSNotification.Name("NavigateToDashboard"), object: nil, userInfo: userInfo)
            }
        }
        
        completionHandler()
    }
}
