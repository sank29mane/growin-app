import CryptoKit
import Foundation
import LocalAuthentication
import XCTest
@testable import Growin

// TEST ONLY. Nothing in this file ships in the app target. The software key
// backend below exists so the signer's checks can be exercised without a
// fingerprint. A result from it is never evidence about the Secure Enclave.

/// The committed growin-orders/1 signing vectors (TEST ONLY keys, public on purpose).
enum RelayVectors {
    static let repoRoot: URL = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()

    static let fixtures = repoRoot.appendingPathComponent("tests/backend/fixtures/relay_orders")

    struct Row {
        let name: String
        let bytes: Data
        let sha256: String
        let payload: [String: Any]
    }

    static func load() throws -> [String: Any] {
        let data = try Data(contentsOf: fixtures.appendingPathComponent("signing_vectors.json"))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    static func key(_ name: String) throws -> (scalar: Data, x963: Data, keyId: String) {
        let vectors = try load()
        let keys = try XCTUnwrap(vectors["keys"] as? [String: Any])
        let key = try XCTUnwrap(keys[name] as? [String: Any])
        let scalar = try XCTUnwrap(Data(hex: key["private_scalar_hex"] as? String ?? ""))
        let x963 = try XCTUnwrap(Data(hex: key["public_key_x963_hex"] as? String ?? ""))
        return (scalar, x963, try XCTUnwrap(key["key_id"] as? String))
    }

    static func rows() throws -> [Row] {
        let vectors = try load()
        let rows = try XCTUnwrap(vectors["rows"] as? [[String: Any]])
        return try rows.map { row in
            let bytes = try XCTUnwrap(Data(base64Encoded: row["canonical_b64"] as? String ?? ""))
            return Row(
                name: try XCTUnwrap(row["name"] as? String),
                bytes: bytes,
                sha256: try XCTUnwrap(row["canonical_sha256"] as? String),
                payload: try XCTUnwrap(row["payload"] as? [String: Any])
            )
        }
    }

    static var limitsSha256: String {
        ((try? load())?["limits_sha256"] as? String) ?? ""
    }

    static var paramsSha256: String {
        ((try? load())?["params_sha256"] as? String) ?? ""
    }
}

extension Data {
    init?(hex: String) {
        guard hex.count % 2 == 0, !hex.isEmpty else { return nil }
        var bytes: [UInt8] = []
        var index = hex.startIndex
        while index < hex.endIndex {
            let next = hex.index(index, offsetBy: 2)
            guard let byte = UInt8(hex[index..<next], radix: 16) else { return nil }
            bytes.append(byte)
            index = next
        }
        self.init(bytes)
    }

    var hexString: String { map { String(format: "%02x", $0) }.joined() }
    var sha256Hex: String { SHA256.hash(data: self).map { String(format: "%02x", $0) }.joined() }
}

/// Canonical JSON the way Python writes it: sorted keys, `,` and `:`, ASCII.
enum CanonicalJSON {
    static func data(_ value: Any) -> Data {
        Data(string(value).utf8)
    }

    static func string(_ value: Any) -> String {
        switch value {
        case let dict as [String: Any]:
            let body = dict.keys.sorted().map { "\(quote($0)):\(string(dict[$0]!))" }
            return "{" + body.joined(separator: ",") + "}"
        case let text as String:
            return quote(text)
        case let number as Int:
            return String(number)
        case is NSNull:
            return "null"
        default:
            fatalError("unsupported canonical value \(value)")
        }
    }

    private static func quote(_ text: String) -> String {
        var out = "\""
        for scalar in text.unicodeScalars {
            switch scalar {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            default: out.unicodeScalars.append(scalar)
            }
        }
        return out + "\""
    }
}

/// Software P-256 key behind the same seam as the Secure Enclave backend. Counts
/// every call so tests can prove a refusal happened before any key access, and
/// records the LAContext it was handed so tests can inspect the prompt settings.
final class SoftwareTestKeyBackend: ApprovalKeyBackend, @unchecked Sendable {
    private let lock = NSLock()
    private let fixedScalar: Data?
    private var _creates = 0
    private var _publicKeyReads = 0
    private var _signs = 0
    private var _contexts: [LAContext] = []
    private var failure: Error?

    init(scalar: Data? = nil) {
        self.fixedScalar = scalar
    }

    var creates: Int { lock.withLock { _creates } }
    var publicKeyReads: Int { lock.withLock { _publicKeyReads } }
    var signs: Int { lock.withLock { _signs } }
    var contexts: [LAContext] { lock.withLock { _contexts } }
    var keyAccessCount: Int { lock.withLock { _creates + _publicKeyReads + _signs } }

    func failSigning(with error: Error?) {
        lock.withLock { failure = error }
    }

    func resetCounters() {
        lock.withLock {
            _creates = 0
            _publicKeyReads = 0
            _signs = 0
            _contexts = []
        }
    }

    func createKeyBlob() throws -> Data {
        lock.withLock { _creates += 1 }
        return fixedScalar ?? P256.Signing.PrivateKey().rawRepresentation
    }

    func publicKeyX963(blob: Data) throws -> Data {
        lock.withLock { _publicKeyReads += 1 }
        guard let key = try? P256.Signing.PrivateKey(rawRepresentation: blob) else {
            throw ApprovalSignerError.invalidKeyBlob
        }
        return key.publicKey.x963Representation
    }

    func signDER(blob: Data, payload: Data, context: LAContext) throws -> Data {
        let error: Error? = lock.withLock {
            _signs += 1
            _contexts.append(context)
            return failure
        }
        if let error { throw error }
        let key = try P256.Signing.PrivateKey(rawRepresentation: blob)
        return try key.signature(for: payload).derRepresentation
    }
}

/// Counts how many authentication contexts (Touch ID prompts) were requested.
final class CountingContextProvider: ApprovalAuthContextProviding, @unchecked Sendable {
    private let lock = NSLock()
    private var _reasons: [String] = []
    private let inner = FreshTouchIDContextProvider()

    var reasons: [String] { lock.withLock { _reasons } }

    func makeContext(reason: String) -> LAContext {
        lock.withLock { _reasons.append(reason) }
        return inner.makeContext(reason: reason)
    }
}

/// A signer on a private keychain service so the operator's real items are never touched.
@MainActor
struct SignerFixture {
    let store: KeychainStore
    let backend: SoftwareTestKeyBackend
    let contexts: CountingContextProvider
    let signer: SecureEnclaveApprovalSigner

    init(scalar: Data?) {
        store = KeychainStore(service: "san.Growin.credentials.v1.test.\(UUID().uuidString)")
        backend = SoftwareTestKeyBackend(scalar: scalar)
        contexts = CountingContextProvider()
        signer = SecureEnclaveApprovalSigner(store: store, backend: backend, contexts: contexts)
    }

    /// The vector's primary key, so the golden bytes name this signer's key_id.
    static func primary() throws -> SignerFixture {
        SignerFixture(scalar: try RelayVectors.key("primary").scalar)
    }

    func cleanUp() {
        for workspace in Workspace.allCases {
            try? store.remove(.approvalSigningKey, scope: .workspace(workspace))
        }
        try? store.remove(.approvalSecureEnclaveKey, scope: .workspace(.india))
    }
}
