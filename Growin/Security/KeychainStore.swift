import Foundation
import Security
import SwiftUI

enum KeychainStoreError: LocalizedError {
    case unexpectedData
    case status(OSStatus)

    var errorDescription: String? {
        switch self {
        case .unexpectedData:
            return "The credential could not be decoded."
        case .status(let status):
            return SecCopyErrorMessageString(status, nil) as String? ?? "Keychain error \(status)."
        }
    }
}

final class KeychainStore: @unchecked Sendable {
    static let shared = KeychainStore()

    private let service = "san.Growin.credentials.v1"

    private init() {}

    func data(for account: String) throws -> Data? {
        var query = baseQuery(account: account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound {
            return nil
        }
        guard status == errSecSuccess else {
            throw KeychainStoreError.status(status)
        }
        guard let data = item as? Data else {
            throw KeychainStoreError.unexpectedData
        }
        return data
    }

    func string(for account: String) throws -> String? {
        guard let data = try data(for: account) else {
            return nil
        }
        guard let value = String(data: data, encoding: .utf8) else {
            throw KeychainStoreError.unexpectedData
        }
        return value
    }

    func set(_ data: Data, for account: String) throws {
        if data.isEmpty {
            try remove(account)
            return
        }

        let query = baseQuery(account: account)
        let attributes: [String: Any] = [kSecValueData as String: data]
        let updateStatus = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)

        if updateStatus == errSecItemNotFound {
            var item = query
            item[kSecValueData as String] = data
            item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
            let addStatus = SecItemAdd(item as CFDictionary, nil)
            guard addStatus == errSecSuccess else {
                throw KeychainStoreError.status(addStatus)
            }
            return
        }

        guard updateStatus == errSecSuccess else {
            throw KeychainStoreError.status(updateStatus)
        }
    }

    func set(_ value: String, for account: String) throws {
        try set(Data(value.utf8), for: account)
    }

    func remove(_ account: String) throws {
        let status = SecItemDelete(baseQuery(account: account) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainStoreError.status(status)
        }
    }

    @discardableResult
    func migrateLegacyUserDefaults(_ defaults: UserDefaults = .standard) -> [String] {
        let accounts = [
            "openaiApiKey", "geminiApiKey", "finnhubApiKey", "trading212ApiKey",
            "trading212ApiSecret", "trading212IsaApiKey", "trading212IsaApiSecret",
            "alpacaApiKey", "alpacaSecretKey", "newsApiKey", "tavilyApiKey",
            "t212InvestKey", "t212InvestSecret", "t212IsaKey", "t212IsaSecret",
        ]
        var failures: [String] = []

        for account in accounts {
            guard let legacy = defaults.string(forKey: account), !legacy.isEmpty else {
                continue
            }
            do {
                if try string(for: account) == nil {
                    try set(legacy, for: account)
                }
                guard try string(for: account) != nil else {
                    throw KeychainStoreError.unexpectedData
                }
                defaults.removeObject(forKey: account)
            } catch {
                failures.append(account)
            }
        }
        return failures
    }

    private func baseQuery(account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }
}

@propertyWrapper
struct KeychainStorage: DynamicProperty {
    @State private var value: String
    private let account: String

    init(wrappedValue defaultValue: String, _ account: String) {
        self.account = account
        let stored = (try? KeychainStore.shared.string(for: account)) ?? nil
        _value = State(initialValue: stored ?? defaultValue)
    }

    var wrappedValue: String {
        get { value }
        nonmutating set {
            value = newValue
            try? KeychainStore.shared.set(newValue, for: account)
        }
    }

    var projectedValue: Binding<String> {
        Binding(
            get: { value },
            set: { wrappedValue = $0 }
        )
    }
}
