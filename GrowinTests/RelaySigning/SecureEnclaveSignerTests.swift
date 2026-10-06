import CryptoKit
import Foundation
import LocalAuthentication
import XCTest
@testable import Growin

/// 63-03 Task 1: the CryptoKit Secure Enclave signer, the strict payload
/// inspector, and the Swift-made signature vector the Python verifiers read.
@MainActor
final class SecureEnclaveSignerTests: XCTestCase {
    private var fixture: SignerFixture!

    override func setUpWithError() throws {
        fixture = try SignerFixture.primary()
    }

    override func tearDown() async throws {
        fixture?.cleanUp()
    }

    private static let vectorFile = RelayVectors.fixtures.appendingPathComponent("swift_signature.json")

    // MARK: Swift signs the golden bytes (tracer)

    func testSwiftDEROverGoldenBytesVerifiesAgainstVectorKey() throws {
        let primary = try RelayVectors.key("primary")
        let identity = try fixture.signer.createIdentityIfNeeded(for: .india)
        XCTAssertEqual(identity.keyID, primary.keyId)
        XCTAssertEqual(identity.publicKeyX963, primary.x963)

        let publicKey = try P256.Signing.PublicKey(x963Representation: primary.x963)
        var written: [[String: Any]] = []
        for row in try RelayVectors.rows() {
            let der = try fixture.signer.sign(row.bytes, flow: .relayOrder, for: .india)
            let signature = try P256.Signing.ECDSASignature(derRepresentation: der)
            XCTAssertTrue(publicKey.isValidSignature(signature, for: row.bytes), row.name)
            // DER canonical form: the VM verifier re-encodes and compares.
            XCTAssertEqual(signature.derRepresentation, der, row.name)
            var flipped = row.bytes
            flipped[flipped.startIndex] ^= 0x01
            XCTAssertFalse(publicKey.isValidSignature(signature, for: flipped), row.name)
            written.append([
                "name": row.name,
                "canonical_sha256": row.sha256,
                "signature_der_b64": der.base64EncodedString(),
            ])
        }

        // Regenerate the committed vector only on request. ECDSA is randomised, so
        // the file changes every time; the committed copy is checked in the next test.
        if ProcessInfo.processInfo.environment["GROWIN_WRITE_VECTORS"] == "1" {
            let document: [String: Any] = [
                "schema": "growin-orders/1 swift signature vector",
                "test_only": true,
                "warning": "TEST ONLY. Signed by Swift (CryptoKit) with the public TEST ONLY primary key from signing_vectors.json. Never pin or enrol this key.",
                "signer": "primary",
                "key_id": primary.keyId,
                "public_key_x963_hex": primary.x963.hexString,
                "rows": written,
            ]
            var data = try JSONSerialization.data(
                withJSONObject: document,
                options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
            )
            data.append(0x0A)
            try data.write(to: Self.vectorFile, options: .atomic)
        }
    }

    func testCommittedSwiftSignatureFileVerifiesOverGoldenBytes() throws {
        let data = try Data(contentsOf: Self.vectorFile)
        let document = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(document["test_only"] as? Bool, true)
        let primary = try RelayVectors.key("primary")
        XCTAssertEqual(document["key_id"] as? String, primary.keyId)
        let publicKey = try P256.Signing.PublicKey(x963Representation: primary.x963)
        let golden = try RelayVectors.rows()
        let rows = try XCTUnwrap(document["rows"] as? [[String: Any]])
        XCTAssertEqual(rows.count, golden.count)
        for row in rows {
            let name = try XCTUnwrap(row["name"] as? String)
            let match = try XCTUnwrap(golden.first { $0.name == name }, name)
            XCTAssertEqual(row["canonical_sha256"] as? String, match.sha256, name)
            let der = try XCTUnwrap(Data(base64Encoded: row["signature_der_b64"] as? String ?? ""))
            let signature = try P256.Signing.ECDSASignature(derRepresentation: der)
            XCTAssertTrue(publicKey.isValidSignature(signature, for: match.bytes), name)
            var flipped = match.bytes
            flipped[flipped.startIndex + 5] ^= 0x01
            XCTAssertFalse(publicKey.isValidSignature(signature, for: flipped), name)
        }
    }

    // MARK: Inspector

    func testInspectorParsesRelayOrderFromGoldenBytes() throws {
        for row in try RelayVectors.rows() {
            guard case .relayOrder(let payload) = try SignedPayloadInspector.inspect(row.bytes) else {
                return XCTFail("\(row.name) did not parse as a relay order")
            }
            let intent = try XCTUnwrap(row.payload["intent"] as? [String: Any])
            XCTAssertEqual(payload.intent.stockCode, intent["stock_code"] as? String)
            XCTAssertEqual(payload.intent.quantity, intent["quantity"] as? Int)
            XCTAssertEqual(payload.intent.limitPrice, intent["limit_price"] as? String)
            XCTAssertEqual(payload.intent.side, intent["side"] as? String)
            XCTAssertEqual(payload.intent.reason, intent["reason"] as? String)
            XCTAssertEqual(payload.intent.batchId, intent["batch_id"] as? String)
            XCTAssertEqual(payload.challengeId, row.payload["challenge_id"] as? String)
            XCTAssertEqual(payload.keyId, row.payload["key_id"] as? String)
        }
    }

    func testInspectorParsesPaperDispatchAndControlClear() throws {
        let key = try RelayVectors.key("primary").keyId
        let paper = CanonicalJSON.data([
            "version": 1, "purpose": "growin.execution.dispatch", "challenge_id": "c-1",
            "proposal_id": "p-1", "client_order_id": "co-1", "intent_hash": "ih",
            "workspace": "india", "account": "paper", "broker": "local-paper", "mode": "PAPER",
            "ticker": "RELIANCE", "side": "BUY", "quantity": "10", "order_type": "LIMIT",
            "limit_price": "2500.50", "replaces_proposal_id": "", "evidence_hash": "eh",
            "nonce": "n", "issued_at": 1, "expires_at": 2, "key_id": key,
        ] as [String: Any])
        guard case .paperDispatch(let dispatch) = try SignedPayloadInspector.inspect(paper) else {
            return XCTFail("paper bytes did not parse as paper dispatch")
        }
        XCTAssertEqual(dispatch.ticker, "RELIANCE")
        XCTAssertEqual(dispatch.limitPrice, "2500.50")
        XCTAssertEqual(dispatch.mode, "PAPER")

        let clear = CanonicalJSON.data([
            "version": 1, "purpose": "growin.execution.control.clear", "challenge_id": "c-2",
            "workspace": "india", "control_version": 3, "nonce": "n", "issued_at": 1,
            "expires_at": 2, "key_id": key,
        ] as [String: Any])
        guard case .controlClear(let control) = try SignedPayloadInspector.inspect(clear) else {
            return XCTFail("clear bytes did not parse as control clear")
        }
        XCTAssertEqual(control.controlVersion, 3)
        XCTAssertEqual(control.workspace, "india")
    }

    private func goldenText() throws -> String {
        String(decoding: try XCTUnwrap(RelayVectors.rows().first).bytes, as: UTF8.self)
    }

    func testInspectorRefusesMalformedDocuments() throws {
        let golden = try goldenText()
        let key = try RelayVectors.key("primary").keyId
        let cases: [(String, String, SignedPayloadInspectionError)] = [
            ("unknown purpose", golden.replacingOccurrences(of: "growin.relay.order", with: "growin.relay.other"), .unknownPurpose),
            ("wrong version", golden.replacingOccurrences(of: "\"version\":1", with: "\"version\":2"), .unsupportedVersion),
            ("duplicate top key", golden.replacingOccurrences(of: "{\"challenge_id\"", with: "{\"version\":1,\"challenge_id\""), .duplicateKey),
            ("duplicate intent key", golden.replacingOccurrences(of: "\"intent_id\"", with: "\"side\":\"buy\",\"intent_id\""), .duplicateKey),
            ("trailing bytes", golden + "{}", .trailingBytes),
            ("trailing newline", golden + "\n", .trailingBytes),
            ("float quantity", golden.replacingOccurrences(of: "\"quantity\":10", with: "\"quantity\":10.5"), .floatNotAllowed),
            ("exponent number", golden.replacingOccurrences(of: "\"issued_at\":1791450000", with: "\"issued_at\":1e9"), .floatNotAllowed),
            ("live mode", golden.replacingOccurrences(of: "\"mode\":\"SHADOW\"", with: "\"mode\":\"LIVE\""), .liveModeRefused),
            ("non-shadow mode", golden.replacingOccurrences(of: "\"mode\":\"SHADOW\"", with: "\"mode\":\"PAPER\""), .invalidField("mode")),
            ("leading whitespace", " " + golden, .malformed),
            ("not an object", "[1]", .malformed),
            ("empty object", "{}", .missingField("purpose")),
            ("intent key_id differs", golden.replacingOccurrences(
                of: "\"key_id\":\"\(key)\",\"limit_price\"", with: "\"key_id\":\"\(String(repeating: "a", count: 64))\",\"limit_price\""
            ), .invalidField("intent.key_id")),
        ]
        for (name, text, expected) in cases {
            XCTAssertNotEqual(text, golden, "\(name): the mutation did not change the document")
            XCTAssertThrowsError(try SignedPayloadInspector.inspect(Data(text.utf8)), name) { error in
                XCTAssertEqual(error as? SignedPayloadInspectionError, expected, name)
            }
        }
        XCTAssertThrowsError(try SignedPayloadInspector.inspect(Data()))
        var nonASCII = Data(golden.utf8)
        nonASCII[nonASCII.startIndex + 20] = 0xC3
        XCTAssertThrowsError(try SignedPayloadInspector.inspect(nonASCII))
    }

    func testRefusalHappensBeforeAnyKeyAccess() throws {
        _ = try fixture.signer.createIdentityIfNeeded(for: .india)
        let golden = try goldenText()
        let bad: [Data] = [
            Data(golden.replacingOccurrences(of: "growin.relay.order", with: "growin.relay.other").utf8),
            Data(golden.replacingOccurrences(of: "\"version\":1", with: "\"version\":2").utf8),
            Data(golden.replacingOccurrences(of: "{\"challenge_id\"", with: "{\"version\":1,\"challenge_id\"").utf8),
            Data((golden + "x").utf8),
            Data(golden.replacingOccurrences(of: "\"mode\":\"SHADOW\"", with: "\"mode\":\"LIVE\"").utf8),
        ]
        fixture.backend.resetCounters()
        for bytes in bad {
            XCTAssertThrowsError(try fixture.signer.sign(bytes, flow: .relayOrder, for: .india))
        }
        XCTAssertEqual(fixture.backend.keyAccessCount, 0, "a refused document must not touch the key")
        XCTAssertTrue(fixture.contexts.reasons.isEmpty, "a refused document must not build a Touch ID prompt")
    }

    // MARK: Real Secure Enclave (no signing, so no prompt)

    func testSecureEnclaveKeyReloadsFromItsBlobWithTheSamePublicKey() throws {
        try XCTSkipUnless(SecureEnclave.isAvailable, "No Secure Enclave on this machine; nothing was checked.")
        let backend = SecureEnclaveKeyBackend()
        let blob: Data
        do {
            blob = try backend.createKeyBlob()
        } catch ApprovalSignerError.deviceLocked {
            // Environmental, not a pass: the key class is "when unlocked" and this Mac is locked.
            throw XCTSkip("The Mac is locked, so the Secure Enclave refused to create a key (-25308). This check did NOT run; run it at an unlocked Mac.")
        } catch {
            return XCTFail("Secure Enclave key creation with [.privateKeyUsage, .biometryCurrentSet] failed: \(error)")
        }
        let first = try backend.publicKeyX963(blob: blob)
        let second = try backend.publicKeyX963(blob: blob)
        XCTAssertEqual(first, second)
        XCTAssertEqual(first.count, 65)
        XCTAssertEqual(first.first, 0x04)
        let identity = ApprovalSignerIdentity(publicKeyX963: first)
        XCTAssertEqual(identity.keyID.count, 64)
        XCTAssertEqual(identity.keyID, first.sha256Hex)
        // A blob that was not made by this Secure Enclave must not load.
        XCTAssertThrowsError(try backend.publicKeyX963(blob: Data(repeating: 7, count: 64)))
    }

    // MARK: Operator UAT (needs a fingerprint; skipped unless asked for)

    /// Signs with the app's REAL India Secure Enclave key. Touch ID is pressed once.
    /// Run: GROWIN_SE_UAT=1 xcodebuild test ... -only-testing:GrowinTests/SecureEnclaveSignerTests/testOperatorSecureEnclaveUAT
    func testOperatorSecureEnclaveUAT() throws {
        let environment = ProcessInfo.processInfo.environment
        guard environment["GROWIN_SE_UAT"] == "1" else {
            throw XCTSkip("Operator UAT: set GROWIN_SE_UAT=1 and press Touch ID once. Nothing was signed.")
        }
        try XCTSkipUnless(SecureEnclave.isAvailable, "No Secure Enclave on this machine.")

        // Real keychain, real Secure Enclave, real prompt. No injected backend here.
        let signer = SecureEnclaveApprovalSigner.shared
        let existed = signer.isConfigured(for: .india)
        let identity = try signer.createIdentityIfNeeded(for: .india)
        print("SE UAT: India key \(existed ? "found" : "created"), key_id prefix \(identity.keyID.prefix(8))")

        // The golden bytes name the TEST ONLY key, which the signer correctly refuses.
        // Swap in the real key_id (same length, so the bytes stay canonical).
        let row = try XCTUnwrap(RelayVectors.rows().first)
        let testKeyId = try RelayVectors.key("primary").keyId
        let text = String(decoding: row.bytes, as: UTF8.self)
            .replacingOccurrences(of: testKeyId, with: identity.keyID)
        let bytes = Data(text.utf8)
        XCTAssertNotEqual(bytes, row.bytes)

        let der = try signer.sign(bytes, flow: .relayOrder, for: .india)

        let publicKey = try P256.Signing.PublicKey(x963Representation: identity.publicKeyX963)
        let signature = try P256.Signing.ECDSASignature(derRepresentation: der)
        XCTAssertTrue(publicKey.isValidSignature(signature, for: bytes), "the Secure Enclave signature must verify locally")

        let home = FileManager.default.homeDirectoryForCurrentUser
        let directory = home.appendingPathComponent(".config/growin/uat", isDirectory: true)
        try FileManager.default.createDirectory(
            at: directory, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let document: [String: Any] = [
            "schema": "growin-orders/1 secure enclave UAT",
            "golden_row": row.name,
            "key_id": identity.keyID,
            "public_key_x963_hex": identity.publicKeyX963.hexString,
            "signed_bytes_b64": bytes.base64EncodedString(),
            "signed_bytes_sha256": bytes.sha256Hex,
            "signature_der_b64": der.base64EncodedString(),
            "key_existed_before_uat": existed,
        ]
        let data = try JSONSerialization.data(withJSONObject: document, options: [.prettyPrinted, .sortedKeys])
        let file = directory.appendingPathComponent("63-se-signature.json")
        try data.write(to: file, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        print("SE UAT: wrote \(file.path)")
    }
}
