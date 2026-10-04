import Foundation
import Security
@testable import Growin

/// Test-only raw access to a Keychain service by literal account string. It exists
/// so tests can plant legacy flat items and look at raw scoped accounts after
/// the app's own account-string API is gone. Only ever pointed at a unique test service.
struct RawKeychain {
    let service: String

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
