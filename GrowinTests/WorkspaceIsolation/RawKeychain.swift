import Foundation
import Security
@testable import Growin

/// Test-only raw access to a Keychain service by literal account string. It exists
/// so tests can plant legacy flat items and look at raw scoped accounts after
/// the app's own account-string API is gone. Only ever pointed at a unique test service.
struct RawKeychain {
    /// Every service this helper accepts starts with this, followed by something. It takes raw
    /// SecItem calls, so it must never be pointed at the operator's real service.
    static let testServicePrefix = "san.Growin.credentials.v1.test."

    let service: String

    /// Refuses at runtime any service that is not test-prefixed with a non-empty suffix.
    /// The inner name is `candidate` on purpose: the #573 store scan reads `init(service:` as a
    /// store construction, and callers still write `RawKeychain(service: x)`.
    init(service candidate: String) {
        precondition(
            candidate.hasPrefix(Self.testServicePrefix) && candidate.count > Self.testServicePrefix.count,
            "RawKeychain only works on a test-prefixed service")
        self.service = candidate
    }

    private func query(_ account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    func set(_ data: Data, account: String) throws {
        var item = query(account)
        item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        let status = SecItemAdd(item as CFDictionary, nil)
        guard status == errSecSuccess else { throw KeychainStoreError.status(status) }
    }

    /// Removes one raw account from the (test-only) service. Used by test cleanup.
    func remove(account: String) {
        SecItemDelete(query(account) as CFDictionary)
    }

    func data(account: String) throws -> Data? {
        var item = query(account)
        item[kSecReturnData as String] = true
        item[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(item as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else { throw KeychainStoreError.status(status) }
        return result as? Data
    }
}
