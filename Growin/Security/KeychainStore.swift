import Foundation
import Security
import SwiftUI

enum KeychainStoreError: LocalizedError {
    case unexpectedData
    case status(OSStatus)
    case scopeNotAllowed(CredentialName)

    var errorDescription: String? {
        switch self {
        case .unexpectedData:
            return "The credential could not be decoded."
        case .status(let status):
            return SecCopyErrorMessageString(status, nil) as String? ?? "Keychain error \(status)."
        case .scopeNotAllowed(let name):
            return "The credential \(name.rawValue) is not available in that scope."
        }
    }
}

final class KeychainStore: @unchecked Sendable {
    static let shared = KeychainStore(service: "san.Growin.credentials.v1")

    private let service: String

    /// Tests pass a unique service so the operator's real items are never touched.
    init(service: String) {
        self.service = service
    }

    // MARK: Scoped API

    func data(for name: CredentialName, scope: KeychainScope) throws -> Data? {
        try data(for: try scopedAccount(name, scope))
    }

    func string(for name: CredentialName, scope: KeychainScope) throws -> String? {
        try string(for: try scopedAccount(name, scope))
    }

    func set(_ data: Data, for name: CredentialName, scope: KeychainScope) throws {
        try set(data, for: try scopedAccount(name, scope))
    }

    func set(_ value: String, for name: CredentialName, scope: KeychainScope) throws {
        try set(Data(value.utf8), for: try scopedAccount(name, scope))
    }

    func remove(_ name: CredentialName, scope: KeychainScope) throws {
        try remove(try scopedAccount(name, scope))
    }

    /// The account is "<prefix>:<raw value>" once the policy check passes.
    private func scopedAccount(_ name: CredentialName, _ scope: KeychainScope) throws -> String {
        guard name.policy.allows(scope) else {
            throw KeychainStoreError.scopeNotAllowed(name)
        }
        return "\(scope.accountPrefix):\(name.rawValue)"
    }

    // MARK: Legacy flat items (unprefixed account, same service)

    func legacyFlatData(for name: CredentialName) throws -> Data? {
        try data(for: name.rawValue)
    }

    func removeLegacyFlatItem(_ name: CredentialName) throws {
        try remove(name.rawValue)
    }

    // MARK: Account-string API (LocalApprovalSigner only; removed by 58-09)

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

    // MARK: Migration

    /// Moves shared and fixed-scope secrets out of UserDefaults into their scoped
    /// account. Lossless: write only if absent, read back, then remove the
    /// UserDefaults key. Failures stay in UserDefaults and retry next launch.
    /// The approval key was never in UserDefaults and is skipped.
    @discardableResult
    func migrateLegacyUserDefaults(_ defaults: UserDefaults = .standard) -> [CredentialName] {
        var failures: [CredentialName] = []

        for name in CredentialName.allCases {
            guard let scope = name.fixedScope else { continue }
            guard let legacy = defaults.string(forKey: name.rawValue), !legacy.isEmpty else {
                continue
            }
            do {
                if try string(for: name, scope: scope) == nil {
                    try set(legacy, for: name, scope: scope)
                }
                guard try string(for: name, scope: scope) != nil else {
                    throw KeychainStoreError.unexpectedData
                }
                defaults.removeObject(forKey: name.rawValue)
            } catch {
                failures.append(name)
            }
        }
        return failures
    }

    /// Copies each flat (unprefixed) shared or fixed-scope item to its scoped
    /// account, reads it back, compares bytes, then deletes the flat item.
    /// Idempotent. A failed or conflicting item is left untouched and returned.
    /// The flat approval key is not moved here; 58-09 adopts it after a key-ID check.
    @discardableResult
    func migrateFlatItemsToScoped() -> [CredentialName] {
        var failures: [CredentialName] = []

        for name in CredentialName.allCases {
            guard let scope = name.fixedScope else { continue }
            do {
                guard let flat = try legacyFlatData(for: name) else { continue }
                if let existing = try data(for: name, scope: scope) {
                    // A scoped value already exists. Drop the flat item only when it
                    // is byte-identical; a differing value is never overwritten.
                    guard existing == flat else {
                        failures.append(name)
                        continue
                    }
                } else {
                    try set(flat, for: name, scope: scope)
                    guard try data(for: name, scope: scope) == flat else {
                        throw KeychainStoreError.unexpectedData
                    }
                }
                try removeLegacyFlatItem(name)
            } catch {
                failures.append(name)
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
    private let name: CredentialName
    private let scope: KeychainScope

    init(wrappedValue defaultValue: String, _ name: CredentialName, scope: KeychainScope) {
        self.name = name
        self.scope = scope
        let stored = (try? KeychainStore.shared.string(for: name, scope: scope)) ?? nil
        _value = State(initialValue: stored ?? defaultValue)
    }

    var wrappedValue: String {
        get { value }
        nonmutating set {
            value = newValue
            try? KeychainStore.shared.set(newValue, for: name, scope: scope)
        }
    }

    var projectedValue: Binding<String> {
        Binding(
            get: { value },
            set: { wrappedValue = $0 }
        )
    }
}
