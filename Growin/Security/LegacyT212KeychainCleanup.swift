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
/// - It never shows a Keychain prompt (see `exactQuery`).
/// - It logs a count or an OSStatus code, never an account name or a value.
/// - It never throws and never blocks launch. A failed item is retried on the
///   next launch, up to `maxFailedLaunches` times.
///
/// This is the only file allowed to spell the legacy account names. A source
/// scan in `LegacyT212KeychainCleanupTests` fails if they appear anywhere else.
nonisolated enum LegacyT212KeychainCleanup {
    /// Unprefixed accounts written before workspace scoping (58-C).
    static let flatAccounts = ["trading212ApiKey", "trading212ApiSecret"]

    /// The same items after 58-C moved them into the UK scope ("uk:<name>").
    static let scopedAccounts = flatAccounts.map { "uk:" + $0 }

    /// Every Keychain account this migration deletes, all under the app's production Keychain service.
    static let legacyAccounts = flatAccounts + scopedAccounts

    /// Older builds also kept a plaintext copy in UserDefaults under the flat names
    /// until `migrateLegacyUserDefaults` moved it. Removing a key never reads its value.
    static let legacyDefaultsKeys = flatAccounts

    /// Versioned so a later cleanup can use `.v2` without re-running this one.
    static let completionKey = "legacyT212KeychainCleanup.v1.completed"

    /// Launches that ended with at least one Keychain error.
    static let failedLaunchesKey = "legacyT212KeychainCleanup.v1.failedLaunches"

    /// After this many failed launches the migration stops retrying.
    static let maxFailedLaunches = 3

    /// `SecItemDelete` on the file keychain removes one match per call, and the same
    /// service and account can exist once per keychain on the search list. Delete in a
    /// loop until not-found, but never more than this many calls per account.
    static let maxDeletesPerAccount = 16

    /// Reported when the per-account bound is hit: matches may remain, so it is an error.
    static let tooManyMatchesStatus: OSStatus = errSecDuplicateItem

    static let giveUpMessage =
        "legacy Trading 212 cleanup stopped after \(maxFailedLaunches) failed launches; remove the old Trading 212 items in Keychain Access"

    struct Outcome: Equatable {
        var skipped: Bool
        var removed: Int
        /// OSStatus codes other than success and not-found, in account order.
        var failedStatuses: [OSStatus]
        /// True on the launch that hit `maxFailedLaunches`.
        var gaveUp = false
    }

    typealias Deleter = (_ service: String, _ account: String) -> OSStatus

    /// The one delete query. Class, service and account pin a single item; the
    /// authentication-UI key makes a Keychain that would prompt fail with
    /// errSecInteractionNotAllowed instead, because this runs synchronously in
    /// App.init and a prompt there would block launch.
    ///
    /// `kSecUseAuthenticationUIFail` is deprecated (macOS 11) in favour of an LAContext
    /// with `interactionNotAllowed`. The old items live in the file keychain, and the
    /// SDK headers document the LAContext route for the data-protection keychain, so
    /// the key stays. Not verified against an item saved by a differently signed build.
    static func exactQuery(service: String, account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecUseAuthenticationUI as String: kSecUseAuthenticationUIFail,
        ]
    }

    /// Deletes at most one generic-password item: this service, this account.
    static func deleteExactItem(service: String, account: String) -> OSStatus {
        SecItemDelete(exactQuery(service: service, account: account) as CFDictionary)
    }

    private static let logger = Logger(subsystem: "san.Growin", category: "legacy-t212-cleanup")

    static func defaultLog(_ message: String) {
        logger.info("\(message, privacy: .public)")
    }

    /// Deletes every match of one exact pair. Stops at not-found (done), at any
    /// other error (reported), or at the bound (reported as an error).
    private static func deleteAllMatches(
        service: String, account: String, delete: Deleter
    ) -> (removed: Int, error: OSStatus?) {
        var removed = 0
        for _ in 0..<maxDeletesPerAccount {
            let status = delete(service, account)
            switch status {
            case errSecSuccess:
                removed += 1
            case errSecItemNotFound:
                return (removed, nil)
            default:
                return (removed, status)
            }
        }
        return (removed, tooManyMatchesStatus)
    }

    /// Runs once. Completion is recorded only when no item hit an error, so a
    /// transient Keychain failure is retried on the next launch, up to
    /// `maxFailedLaunches` times. `service` has no default on purpose: the only
    /// production caller is `LaunchMigrations`, and every test passes its own.
    @discardableResult
    static func runOnce(
        service: String,
        defaults: UserDefaults = .standard,
        delete: Deleter = deleteExactItem,
        log: (String) -> Void = defaultLog
    ) -> Outcome {
        if defaults.bool(forKey: completionKey)
            || defaults.integer(forKey: failedLaunchesKey) >= maxFailedLaunches {
            return Outcome(skipped: true, removed: 0, failedStatuses: [])
        }

        var removed = 0
        var failed: [OSStatus] = []
        for account in legacyAccounts {
            let result = deleteAllMatches(service: service, account: account, delete: delete)
            removed += result.removed
            if let status = result.error {
                failed.append(status)
                log("legacy cleanup keychain status \(status)")
            }
        }
        for key in legacyDefaultsKeys {
            defaults.removeObject(forKey: key)
        }

        log("removed \(removed) legacy items")
        var gaveUp = false
        if failed.isEmpty {
            defaults.set(true, forKey: completionKey)
        } else {
            let failedLaunches = defaults.integer(forKey: failedLaunchesKey) + 1
            defaults.set(failedLaunches, forKey: failedLaunchesKey)
            if failedLaunches >= maxFailedLaunches {
                gaveUp = true
                log(giveUpMessage)
            }
        }
        return Outcome(skipped: false, removed: removed, failedStatuses: failed, gaveUp: gaveUp)
    }
}
