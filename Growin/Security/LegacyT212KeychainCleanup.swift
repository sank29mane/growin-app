import Foundation
import Security
import os

/// One-time removal of the Trading 212 API key and secret that older builds
/// saved in the Keychain. The backend reads Trading 212 credentials only from
/// its own private config now (PR #553), so these items are dead weight.
///
/// Rules this file keeps:
/// - Exact match on service AND account. Nothing is matched by prefix, by
///   service alone or by class alone.
/// - It never reads a secret value. It only issues `SecItemDelete`.
/// - It logs a count or an OSStatus code, never an account name or a value.
/// - It never throws and never blocks launch. A failed item is retried next launch.
///
/// This is the only file allowed to spell the legacy account names. A source
/// scan in `LegacyT212KeychainCleanupTests` fails if they appear anywhere else.
nonisolated enum LegacyT212KeychainCleanup {
    /// Unprefixed accounts written before workspace scoping (58-C).
    static let flatAccounts = ["trading212ApiKey", "trading212ApiSecret"]

    /// The same items after 58-C moved them into the UK scope ("uk:<name>").
    static let scopedAccounts = flatAccounts.map { "uk:" + $0 }

    /// Every Keychain account this migration deletes, all under `KeychainStore.productionService`.
    static let legacyAccounts = flatAccounts + scopedAccounts

    /// Older builds also kept a plaintext copy in UserDefaults under the flat names
    /// until `migrateLegacyUserDefaults` moved it. Removing a key never reads its value.
    static let legacyDefaultsKeys = flatAccounts

    /// Versioned so a later cleanup can use `.v2` without re-running this one.
    static let completionKey = "legacyT212KeychainCleanup.v1.completed"

    struct Outcome: Equatable {
        var skipped: Bool
        var removed: Int
        /// OSStatus codes other than success and not-found, in account order.
        var failedStatuses: [OSStatus]
    }

    typealias Deleter = (_ service: String, _ account: String) -> OSStatus

    /// Deletes exactly one generic-password item: this service, this account.
    static func deleteExactItem(service: String, account: String) -> OSStatus {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        return SecItemDelete(query as CFDictionary)
    }

    private static let logger = Logger(subsystem: "san.Growin", category: "legacy-t212-cleanup")

    static func defaultLog(_ message: String) {
        logger.info("\(message, privacy: .public)")
    }

    /// Runs once. Completion is recorded only when no item hit an error, so a
    /// transient Keychain failure is retried on the next launch.
    @discardableResult
    static func runOnce(
        service: String = KeychainStore.productionService,
        defaults: UserDefaults = .standard,
        delete: Deleter = deleteExactItem,
        log: (String) -> Void = defaultLog
    ) -> Outcome {
        if defaults.bool(forKey: completionKey) {
            return Outcome(skipped: true, removed: 0, failedStatuses: [])
        }

        var removed = 0
        var failed: [OSStatus] = []
        for account in legacyAccounts {
            let status = delete(service, account)
            switch status {
            case errSecSuccess:
                removed += 1
            case errSecItemNotFound:
                break
            default:
                failed.append(status)
                log("legacy cleanup keychain status \(status)")
            }
        }
        for key in legacyDefaultsKeys {
            defaults.removeObject(forKey: key)
        }

        log("removed \(removed) legacy items")
        if failed.isEmpty {
            defaults.set(true, forKey: completionKey)
        }
        return Outcome(skipped: false, removed: removed, failedStatuses: failed)
    }
}
