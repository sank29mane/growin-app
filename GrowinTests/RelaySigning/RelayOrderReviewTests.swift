import CryptoKit
import Foundation
import LocalAuthentication
import XCTest
@testable import Growin

// MARK: - Fakes

/// URLProtocol stub for the relay approval routes. Records every request and
/// answers from a handler. Separate from the paper tests' stub on purpose.
final class RelayBatchURLProtocol: URLProtocol {
    struct Recorded {
        let path: String
        let body: [String: String]
    }

    private static let lock = NSLock()
    nonisolated(unsafe) private static var recorded: [Recorded] = []
    nonisolated(unsafe) private static var handler: (@Sendable (Recorded) -> (Int, Data))?

    static func install(_ handler: @escaping @Sendable (Recorded) -> (Int, Data)) {
        lock.lock()
        recorded = []
        self.handler = handler
        lock.unlock()
    }

    static func requests() -> [Recorded] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }

    static func makeSession() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [RelayBatchURLProtocol.self]
        return URLSession(configuration: config)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        var raw = request.httpBody
        if raw == nil, let stream = request.httpBodyStream {
            stream.open()
            var data = Data()
            let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: 1024)
            while stream.hasBytesAvailable {
                let read = stream.read(buffer, maxLength: 1024)
                if read <= 0 { break }
                data.append(buffer, count: read)
            }
            buffer.deallocate()
            stream.close()
            raw = data
        }
        let body = (raw.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: String]) ?? [:]
        let record = Recorded(path: request.url?.path ?? "", body: body)
        Self.lock.lock()
        Self.recorded.append(record)
        let handler = Self.handler
        Self.lock.unlock()
        let (status, payload) = handler?(record) ?? (500, Data())
        let response = HTTPURLResponse(
            url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: payload)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

/// Stands in for the Mac backend plus the VM: mints relay bytes per proposal and
/// verifies every signature it receives against the key it minted for.
final class FakeRelayServer: @unchecked Sendable {
    struct Behaviour {
        var challengeFailure: (proposal: String, status: Int, body: String)?
        var completeFailure: (proposal: String, status: Int, body: String)?
        var batchIdOverride: String?
        var keyIdOverride: String?
        var summaryClaim: [String: Any]?
        var expiresIn = 60
    }

    private let lock = NSLock()
    private let keyId: String
    private let publicKey: P256.Signing.PublicKey
    private let now: Int
    private var served: [String: Data] = [:]
    private var counter = 0
    private(set) var verifiedSignatures = 0
    private var behaviour = Behaviour()

    init(keyId: String, publicKey: P256.Signing.PublicKey, now: Int) {
        self.keyId = keyId
        self.publicKey = publicKey
        self.now = now
    }

    func configure(_ change: (inout Behaviour) -> Void) {
        lock.withLock { change(&behaviour) }
    }

    var verified: Int { lock.withLock { verifiedSignatures } }

    func relayBytes(
        proposalId: String, challengeId: String, batchId: String?, keyId: String,
        side: String = "sell", quantity: Int = 5, stock: String = "ABC", limit: String = "2500.5",
        reason: String = "halve", mode: String = "SHADOW", issuedAt: Int, expiresAt: Int
    ) -> Data {
        let limits = RelayVectors.limitsSha256
        let intent: [String: Any] = [
            "intent_id": "intent-\(proposalId)", "proposal_id": proposalId, "workspace": "india",
            "broker": "icici-breeze", "mode": mode, "exchange": "NSE", "product": "cash",
            "order_type": "limit", "validity": "day", "side": side, "stock_code": stock,
            "isin": "INE111B01023", "quantity": quantity, "limit_price": limit, "reason": reason,
            "batch_id": batchId.map { $0 as Any } ?? NSNull(), "limits_sha256": limits,
            "params_sha256": RelayVectors.paramsSha256, "key_id": keyId,
        ]
        return CanonicalJSON.data([
            "version": 1, "purpose": "growin.relay.order", "challenge_id": challengeId,
            "nonce": String(repeating: "n", count: 43), "issued_at": issuedAt, "expires_at": expiresAt,
            "key_id": keyId, "limits_sha256": limits, "intent": intent,
        ] as [String: Any])
    }

    func respond(_ request: RelayBatchURLProtocol.Recorded, batchId: String?) -> (Int, Data) {
        let settings = lock.withLock { behaviour }
        let proposal = request.body["proposal_id"] ?? ""
        if request.path.hasSuffix("/challenge") {
            if let failure = settings.challengeFailure, failure.proposal == proposal {
                return (failure.status, Data(failure.body.utf8))
            }
            let id: Int = lock.withLock { counter += 1; return counter }
            let challengeId = String(format: "00000000-0000-4000-8000-%012d", id)
            let bytes = relayBytes(
                proposalId: proposal, challengeId: challengeId,
                batchId: settings.batchIdOverride ?? batchId,
                keyId: settings.keyIdOverride ?? keyId,
                issuedAt: now, expiresAt: now + settings.expiresIn
            )
            lock.withLock { served[challengeId] = bytes }
            var body: [String: Any] = [
                "challenge_id": challengeId, "proposal_id": proposal,
                "signed_payload_b64": bytes.base64EncodedString(), "key_id": keyId,
            ]
            if let claim = settings.summaryClaim { body["summary"] = claim }
            return (200, (try? JSONSerialization.data(withJSONObject: body)) ?? Data())
        }
        if let failure = settings.completeFailure, failure.proposal == proposal {
            return (failure.status, Data(failure.body.utf8))
        }
        let challengeId = request.body["challenge_id"] ?? ""
        let signature = Data(base64Encoded: request.body["signature_der_b64"] ?? "") ?? Data()
        let bytes = lock.withLock { served[challengeId] }
        guard let bytes,
              let parsed = try? P256.Signing.ECDSASignature(derRepresentation: signature),
              publicKey.isValidSignature(parsed, for: bytes) else {
            return (403, Data(#"{"detail":{"reason":"signature_invalid"}}"#.utf8))
        }
        lock.withLock { verifiedSignatures += 1 }
        return (200, Data(#"{"message":"relay order recorded, not forwarded"}"#.utf8))
    }
}

// MARK: - Tests

/// 63-03 Task 2: router, review from the signed bytes, enrolment decision and
/// the one-Touch-ID-per-order batch walk.
@MainActor
final class RelayOrderReviewTests: XCTestCase {
    private var fixture: SignerFixture!
    private var ukStore: KeychainStore!
    private var uk: LocalApprovalSigner!
    private var router: ApprovalSignerRouter!

    override func setUpWithError() throws {
        fixture = try SignerFixture.primary()
        ukStore = KeychainStore(service: "san.Growin.credentials.v1.test.uk.\(UUID().uuidString)")
        uk = LocalApprovalSigner(store: ukStore)
        router = ApprovalSignerRouter(india: fixture.signer, uk: uk)
    }

    override func tearDown() async throws {
        fixture?.cleanUp()
        if let ukStore {
            for workspace in Workspace.allCases {
                try? ukStore.remove(.approvalSigningKey, scope: .workspace(workspace))
            }
        }
    }

    private let now = Date(timeIntervalSince1970: 1_791_450_000)
    private var nowEpoch: Int { Int(now.timeIntervalSince1970) }

    private func server() throws -> FakeRelayServer {
        let primary = try RelayVectors.key("primary")
        return FakeRelayServer(
            keyId: primary.keyId,
            publicKey: try P256.Signing.PublicKey(x963Representation: primary.x963),
            now: nowEpoch
        )
    }

    private func relayChallenge(
        _ server: FakeRelayServer, proposal: String = "proposal-0001", challengeId: String = "00000000-0000-4000-8000-000000000001",
        batchId: String? = "batch-halve-0001", keyId: String? = nil, mode: String = "SHADOW", expiresIn: Int = 60,
        summary: [String: Any]? = nil
    ) throws -> RelayApprovalChallenge {
        let primary = try RelayVectors.key("primary")
        let bytes = server.relayBytes(
            proposalId: proposal, challengeId: challengeId, batchId: batchId, keyId: keyId ?? primary.keyId,
            mode: mode, issuedAt: nowEpoch, expiresAt: nowEpoch + expiresIn
        )
        var body: [String: Any] = [
            "challenge_id": challengeId, "proposal_id": proposal,
            "signed_payload_b64": bytes.base64EncodedString(),
        ]
        if let summary { body["summary"] = summary }
        let data = try JSONSerialization.data(withJSONObject: body)
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(RelayApprovalChallenge.self, from: data)
    }

    // MARK: Router

    /// Break-proof 5: routing India to the software signer makes these fail.
    func testRouterSendsIndiaToTheSecureEnclaveAndUKToTheSoftwareSigner() throws {
        XCTAssertEqual(router.route(for: .india), .secureEnclave)
        XCTAssertEqual(router.route(for: .uk), .localSoftware)

        // India end to end: the Secure Enclave backend is the one that creates, holds and signs.
        let identity = try router.createIdentityIfNeeded(for: .india)
        XCTAssertEqual(fixture.backend.creates, 1)
        XCTAssertNotNil(try fixture.store.data(for: .approvalSecureEnclaveKey, scope: .workspace(.india)))
        XCTAssertNil(try fixture.store.data(for: .approvalSigningKey, scope: .workspace(.india)), "India got a software key")
        XCTAssertNil(try ukStore.data(for: .approvalSigningKey, scope: .workspace(.india)), "India got a software key")
        XCTAssertEqual(try router.identity(for: .india), identity)
        XCTAssertTrue(router.isConfigured(for: .india))

        let bytes = try XCTUnwrap(RelayVectors.rows().first).bytes
        let der = try router.sign(bytes, for: .india, flow: .relayOrder)
        XCTAssertEqual(fixture.backend.signs, 1)
        XCTAssertEqual(fixture.contexts.reasons.count, 1)
        let publicKey = try P256.Signing.PublicKey(x963Representation: identity.publicKeyX963)
        XCTAssertTrue(publicKey.isValidSignature(try P256.Signing.ECDSASignature(derRepresentation: der), for: bytes))
    }

    func testRouterKeepsTheSoftwareSignerForUKAndNeverTouchesTheSecureEnclaveForIt() throws {
        let identity = try router.createIdentityIfNeeded(for: .uk)
        XCTAssertEqual(fixture.backend.keyAccessCount, 0, "UK must not touch the Secure Enclave backend")
        XCTAssertNotNil(try ukStore.data(for: .approvalSigningKey, scope: .workspace(.uk)))
        XCTAssertNil(try ukStore.data(for: .approvalSecureEnclaveKey, scope: .workspace(.india)))

        let payload = ukPaperBytes(keyId: identity.keyID)
        let der = try router.sign(payload, for: .uk, flow: .paperApproval)
        let publicKey = try P256.Signing.PublicKey(x963Representation: identity.publicKeyX963)
        XCTAssertTrue(publicKey.isValidSignature(try P256.Signing.ECDSASignature(derRepresentation: der), for: payload))
        XCTAssertEqual(fixture.backend.keyAccessCount, 0)
        XCTAssertTrue(fixture.contexts.reasons.isEmpty, "UK flows behave as before: no new prompt")

        XCTAssertThrowsError(try router.sign(payload, for: .uk, flow: .relayOrder)) { error in
            XCTAssertEqual(error as? ApprovalSignerRouterError, .relayIsIndiaOnly)
        }
        XCTAssertFalse(router.canAdoptLegacyKey(into: .india), "India never adopts the flat software key")
    }

    // MARK: Software signer is gated by the bytes, not the flow label (P2, #558)

    private func ukPaperBytes(keyId: String, workspace: String = "uk") -> Data {
        CanonicalJSON.data([
            "version": 1, "purpose": "growin.execution.dispatch", "challenge_id": "c-1",
            "proposal_id": "p-1", "client_order_id": "co-1", "intent_hash": "ih",
            "workspace": workspace, "account": "paper", "broker": "local-paper", "mode": "PAPER",
            "ticker": "VOD.L", "side": "BUY", "quantity": "10", "order_type": "LIMIT",
            "limit_price": "70.50", "replaces_proposal_id": "", "evidence_hash": "eh",
            "nonce": "n", "issued_at": 1, "expires_at": 2, "key_id": keyId,
        ] as [String: Any])
    }

    /// Break-proof: removing the payload check in requireSoftwareSignable makes the
    /// India relay, mismatch and unparseable cases sign with the UK key.
    func testSoftwareSignerRefusesIndiaRelayBytesLabelledAsPaperApprovalSyncAndAsync() async throws {
        _ = try router.createIdentityIfNeeded(for: .uk)
        let indiaRelay = try XCTUnwrap(RelayVectors.rows().first).bytes
        XCTAssertThrowsError(try router.sign(indiaRelay, for: .uk, flow: .paperApproval)) { error in
            XCTAssertEqual(error as? ApprovalSignerRouterError, .relayIsIndiaOnly)
        }
        do {
            _ = try await router.signAsync(indiaRelay, for: .uk, flow: .paperApproval)
            XCTFail("India relay bytes were signed with the UK software key")
        } catch {
            XCTAssertEqual(error as? ApprovalSignerRouterError, .relayIsIndiaOnly)
        }
        XCTAssertThrowsError(try router.sign(indiaRelay, for: .uk, flow: .controlClear)) { error in
            XCTAssertEqual(error as? ApprovalSignerRouterError, .softwareFlowNotAllowed)
        }
        XCTAssertEqual(fixture.backend.keyAccessCount, 0)
    }

    func testSoftwareSignerRefusesPurposeAndWorkspaceMismatchSyncAndAsync() async throws {
        let identity = try router.createIdentityIfNeeded(for: .uk)
        let wrongWorkspace = ukPaperBytes(keyId: identity.keyID, workspace: "india")
        let wrongPurpose = CanonicalJSON.data([
            "version": 1, "purpose": "growin.execution.control.clear", "challenge_id": "c-2",
            "workspace": "uk", "control_version": 3, "nonce": "n", "issued_at": 1,
            "expires_at": 2, "key_id": identity.keyID,
        ] as [String: Any])
        for bytes in [wrongWorkspace, wrongPurpose] {
            XCTAssertThrowsError(try router.sign(bytes, for: .uk, flow: .paperApproval))
            do {
                _ = try await router.signAsync(bytes, for: .uk, flow: .paperApproval)
                XCTFail("mismatched bytes were signed")
            } catch {}
        }
        XCTAssertThrowsError(try router.sign(wrongWorkspace, for: .uk, flow: .paperApproval)) { error in
            guard case .workspaceMismatch? = error as? ApprovalSignerError else { return XCTFail("\(error)") }
        }
        XCTAssertThrowsError(try router.sign(wrongPurpose, for: .uk, flow: .paperApproval)) { error in
            guard case .purposeNotAllowed? = error as? ApprovalSignerError else { return XCTFail("\(error)") }
        }
    }

    func testSoftwareSignerRefusesUnparseableBytesSyncAndAsync() async throws {
        _ = try router.createIdentityIfNeeded(for: .uk)
        for bytes in [Data("uk-paper-approval-bytes".utf8), Data(), Data(#"{"purpose":"growin.execution.dispatch"}"#.utf8)] {
            XCTAssertThrowsError(try router.sign(bytes, for: .uk, flow: .paperApproval)) { error in
                XCTAssertTrue(error is SignedPayloadInspectionError, "\(error)")
            }
            do {
                _ = try await router.signAsync(bytes, for: .uk, flow: .paperApproval)
                XCTFail("unparseable bytes were signed")
            } catch {
                XCTAssertTrue(error is SignedPayloadInspectionError, "\(error)")
            }
        }
    }

    func testSoftwareSignerStillSignsALegitimatePaperApprovalAsync() async throws {
        let identity = try router.createIdentityIfNeeded(for: .uk)
        let payload = ukPaperBytes(keyId: identity.keyID)
        let der = try await router.signAsync(payload, for: .uk, flow: .paperApproval)
        let publicKey = try P256.Signing.PublicKey(x963Representation: identity.publicKeyX963)
        XCTAssertTrue(publicKey.isValidSignature(try P256.Signing.ECDSASignature(derRepresentation: der), for: payload))
    }

    func testOnlyTheRouterReachesTheSoftwareSignerInAppSources() throws {
        let root = RelayVectors.repoRoot.appendingPathComponent("Growin")
        let enumerator = try XCTUnwrap(FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil))
        var offenders: [String] = []
        var scanned = 0
        for case let url as URL in enumerator where url.pathExtension == "swift" {
            scanned += 1
            let text = try String(contentsOf: url, encoding: .utf8)
            let name = url.lastPathComponent
            if text.contains("LocalApprovalSigner.shared"), name != "ApprovalSignerRouter.swift", name != "LocalApprovalSigner.swift" {
                offenders.append(name)
            }
        }
        XCTAssertGreaterThan(scanned, 10)
        XCTAssertEqual(offenders, [], "these files sign without the router")
        let routerSource = try PaperOperationsSourceProbe.contents("Growin/Security/ApprovalSignerRouter.swift")
        XCTAssertTrue(routerSource.contains("case .india: return .secureEnclave"))
        XCTAssertTrue(routerSource.contains("case .uk: return .localSoftware"))
    }

    // MARK: Enrolment decision (P-22)

    func testIndiaLedgerKeyMismatchAsksForAFreshLedgerInsteadOfAdopting() {
        let local = String(repeating: "a", count: 64)
        let old = String(repeating: "b", count: 64)
        XCTAssertEqual(ApprovalSignerRouter.enrolmentDecision(workspace: .india, localKeyID: local, backendEnrolled: false, backendKeyID: nil), .enrol)
        XCTAssertEqual(ApprovalSignerRouter.enrolmentDecision(workspace: .india, localKeyID: local, backendEnrolled: true, backendKeyID: local), .alreadyEnrolled)
        XCTAssertEqual(ApprovalSignerRouter.enrolmentDecision(workspace: .india, localKeyID: local, backendEnrolled: true, backendKeyID: old), .freshLedgerRequired)
        XCTAssertEqual(ApprovalSignerRouter.enrolmentDecision(workspace: .india, localKeyID: local, backendEnrolled: true, backendKeyID: nil), .freshLedgerRequired)
        XCTAssertEqual(ApprovalSignerRouter.enrolmentDecision(workspace: .uk, localKeyID: local, backendEnrolled: true, backendKeyID: old), .signerMismatch)
        let message = ApprovalSignerRouterError.indiaLedgerKeyMismatch.localizedDescription
        XCTAssertTrue(message.contains("fresh ledger"))
        XCTAssertTrue(message.contains("nothing was changed"))
    }

    func testSettingsRoutesEnrolmentThroughTheDecisionAndTheRouter() throws {
        let settings = try PaperOperationsSourceProbe.contents("Growin/Views/SettingsView.swift")
        XCTAssertTrue(settings.contains("ApprovalSignerRouter.enrolmentDecision("))
        XCTAssertTrue(settings.contains("ApprovalSignerRouterError.indiaLedgerKeyMismatch"))
        XCTAssertFalse(settings.contains("LocalApprovalSigner.shared"))
        let chat = try PaperOperationsSourceProbe.contents("Growin/ViewModels/ChatViewModel.swift")
        XCTAssertFalse(chat.contains("LocalApprovalSigner.shared"))
        XCTAssertTrue(chat.contains("ApprovalSignerRouter.shared"))
        let paper = try PaperOperationsSourceProbe.contents("Growin/Models/PaperOperationsModels.swift")
        XCTAssertFalse(paper.contains("LocalApprovalSigner.shared"))
        XCTAssertTrue(paper.contains("ApprovalSignerRouter.shared"))
    }

    // MARK: Review

    func testReviewShowsEveryFieldParsedFromTheSignedBytes() throws {
        let server = try server()
        let challenge = try relayChallenge(server)
        let review = try RelayOrderReview(challenge: challenge, expectedProposalId: "proposal-0001", now: now)
        XCTAssertEqual(review.side, "sell")
        XCTAssertEqual(review.quantity, 5)
        XCTAssertEqual(review.stockCode, "ABC")
        XCTAssertEqual(review.isin, "INE111B01023")
        XCTAssertEqual(review.limitPrice, "2500.5")
        XCTAssertEqual(review.notional, Decimal(string: "12502.5"))
        XCTAssertEqual(review.notionalText, "12502.50")
        XCTAssertEqual(review.reason, "halve")
        XCTAssertEqual(review.batchId, "batch-halve-0001")
        XCTAssertFalse(review.hasSummaryDisagreement)
        XCTAssertEqual(review.signedBytes, Data(base64Encoded: challenge.signedPayloadB64))
    }

    /// Break-proof 4: showing the backend summary instead of the bytes makes this fail.
    func testABackendSummaryThatDisagreesWithTheBytesIsIgnoredAndFlagged() throws {
        let server = try server()
        let lie: [String: Any] = [
            "side": "buy", "quantity": 1, "stock_code": "EVIL", "isin": "INE000A01012",
            "limit_price": "1.00", "reason": "entry", "batch_id": "batch-other-0001",
        ]
        let challenge = try relayChallenge(server, summary: lie)
        let review = try RelayOrderReview(challenge: challenge, expectedProposalId: "proposal-0001", now: now)
        // What the operator sees is what the bytes say.
        XCTAssertEqual(review.side, "sell")
        XCTAssertEqual(review.quantity, 5)
        XCTAssertEqual(review.stockCode, "ABC")
        XCTAssertEqual(review.isin, "INE111B01023")
        XCTAssertEqual(review.limitPrice, "2500.5")
        XCTAssertEqual(review.reason, "halve")
        XCTAssertEqual(review.batchId, "batch-halve-0001")
        XCTAssertEqual(
            Set(review.summaryDisagreements),
            ["side", "quantity", "stock_code", "isin", "limit_price", "reason", "batch_id"]
        )
        XCTAssertTrue(review.hasSummaryDisagreement)

        // A summary that agrees (even formatted differently) is not flagged.
        let agree: [String: Any] = ["side": "SELL", "quantity": 5, "stock_code": "ABC", "limit_price": "2500.50"]
        let ok = try RelayOrderReview(
            challenge: try relayChallenge(server, summary: agree), expectedProposalId: "proposal-0001", now: now
        )
        XCTAssertFalse(ok.hasSummaryDisagreement)
    }

    func testReviewRefusesBytesThatAreNotTheRequestedRelayOrder() throws {
        let server = try server()
        func review(_ challenge: RelayApprovalChallenge, proposal: String = "proposal-0001", at date: Date? = nil) throws -> RelayOrderReview {
            try RelayOrderReview(challenge: challenge, expectedProposalId: proposal, now: date ?? now)
        }
        // A different proposal than the one requested.
        XCTAssertThrowsError(try review(try relayChallenge(server), proposal: "proposal-0002")) {
            XCTAssertEqual($0 as? RelayOrderReviewError, .proposalMismatch)
        }
        // Server names a different challenge than the bytes carry.
        let good = try relayChallenge(server)
        let wrongId = RelayApprovalChallenge(
            challengeId: "00000000-0000-4000-8000-0000000000ff", proposalId: good.proposalId,
            signedPayloadB64: good.signedPayloadB64, keyId: nil, summary: nil
        )
        XCTAssertThrowsError(try review(wrongId)) { XCTAssertEqual($0 as? RelayOrderReviewError, .challengeMismatch) }
        // Already expired.
        XCTAssertThrowsError(try review(good, at: now.addingTimeInterval(61))) {
            XCTAssertEqual($0 as? RelayOrderReviewError, .expired)
        }
        // Not base64.
        let garbage = RelayApprovalChallenge(challengeId: "x", proposalId: "proposal-0001", signedPayloadB64: "%%%", keyId: nil, summary: nil)
        XCTAssertThrowsError(try review(garbage)) { XCTAssertEqual($0 as? RelayOrderReviewError, .invalidEnvelope) }
        // Paper bytes are not a relay order.
        let paper = CanonicalJSON.data([
            "version": 1, "purpose": "growin.execution.dispatch", "challenge_id": "c-1", "proposal_id": "proposal-0001",
            "workspace": "india", "mode": "PAPER", "ticker": "RELIANCE", "side": "BUY", "quantity": "10",
            "key_id": String(repeating: "a", count: 64), "expires_at": nowEpoch + 60,
        ] as [String: Any])
        let paperChallenge = RelayApprovalChallenge(
            challengeId: "c-1", proposalId: "proposal-0001", signedPayloadB64: paper.base64EncodedString(), keyId: nil, summary: nil
        )
        XCTAssertThrowsError(try review(paperChallenge)) { XCTAssertEqual($0 as? RelayOrderReviewError, .notARelayOrder) }
        // A LIVE intent never reaches a review.
        XCTAssertThrowsError(try review(try relayChallenge(server, mode: "LIVE"))) {
            XCTAssertEqual($0 as? SignedPayloadInspectionError, .liveModeRefused)
        }
    }

    func testErrorBodiesYieldASanitisedReasonCode() {
        func failure(_ json: String, status: Int = 409) -> RelayBatchFailure {
            RelayApprovalClient.failure(stage: .challenge, status: status, body: Data(json.utf8))
        }
        XCTAssertEqual(failure(#"{"detail":{"reason":"capital_cap","message":"over"}}"#).code, "capital_cap")
        XCTAssertEqual(failure(#"{"error":{"code":"kill_switch"}}"#, status: 423).code, "kill_switch")
        XCTAssertEqual(failure(#"{"reason":"stop_open"}"#, status: 423).code, "stop_open")
        XCTAssertEqual(failure(#"{"detail":"nope"}"#, status: 500).code, "http_500")
        XCTAssertEqual(failure(#"{"detail":{"reason":"has spaces and <b>"}}"#).code, "http_409")
        XCTAssertEqual(failure("not json", status: 502).code, "http_502")
        XCTAssertEqual(failure(#"{"detail":{"reason":"collar","message":"limit too far"}}"#).message, "limit too far")
    }

    // MARK: Batch

    private struct BatchHarness {
        let model: RelayOrderBatchViewModel
        let server: FakeRelayServer
        let clock: ClockBox
    }

    @MainActor final class ClockBox {
        var date: Date
        init(_ date: Date) { self.date = date }
    }

    private func makeBatch(
        proposals: [String] = ["p-0001", "p-0002", "p-0003"],
        batchId: String = "batch-halve-0001",
        configure: (FakeRelayServer) -> Void = { _ in }
    ) throws -> BatchHarness {
        let server = try server()
        configure(server)
        RelayBatchURLProtocol.install { server.respond($0, batchId: batchId) }
        let clock = ClockBox(now)
        let router = self.router!
        let client = RelayApprovalClient(session: RelayBatchURLProtocol.makeSession(), baseURL: "http://relay.test")
        let model = RelayOrderBatchViewModel(
            batchId: batchId,
            proposalIds: proposals,
            client: client,
            signBytes: { bytes in try await router.signAsync(bytes, for: .india, flow: .relayOrder) },
            clock: { clock.date }
        )
        return BatchHarness(model: model, server: server, clock: clock)
    }

    private func paths() -> [String] {
        RelayBatchURLProtocol.requests().map { "\($0.path.split(separator: "/").last ?? "")" + ":" + ($0.body["proposal_id"] ?? "") }
    }

    func testBatchWalksEveryProposalInOrderWithOneTouchIDEach() async throws {
        _ = try router.createIdentityIfNeeded(for: .india)
        let harness = try makeBatch()
        let model = harness.model

        for _ in 0..<3 {
            await model.prepareNext()
            await model.approveCurrent()
        }

        XCTAssertEqual(paths(), [
            "challenge:p-0001", "complete:p-0001",
            "challenge:p-0002", "complete:p-0002",
            "challenge:p-0003", "complete:p-0003",
        ])
        XCTAssertEqual(fixture.backend.signs, 3, "one signature per order")
        XCTAssertEqual(fixture.contexts.reasons.count, 3, "one Touch ID prompt per order")
        XCTAssertEqual(Set(fixture.backend.contexts.map { ObjectIdentifier($0) }).count, 3, "a fresh context each time")
        XCTAssertEqual(harness.server.verified, 3, "the server verified every signature over its own bytes")
        XCTAssertNil(model.failure)
        XCTAssertTrue(model.isFinished)
        XCTAssertEqual(model.completedCount, 3)
        for item in model.items {
            guard case .completed(let review, _) = item.state else { return XCTFail("\(item.proposalId) not completed") }
            XCTAssertEqual(review.batchId, "batch-halve-0001")
        }
    }

    func testBatchStopsAtTheFirstServerRefusalAndNeverRetries() async throws {
        _ = try router.createIdentityIfNeeded(for: .india)
        let harness = try makeBatch { server in
            server.configure {
                $0.completeFailure = ("p-0002", 423, #"{"detail":{"reason":"kill_switch","message":"orders are blocked"}}"#)
            }
        }
        let model = harness.model
        for _ in 0..<2 {
            await model.prepareNext()
            await model.approveCurrent()
        }
        let failure = try XCTUnwrap(model.failure)
        XCTAssertEqual(failure.code, "kill_switch")
        XCTAssertEqual(failure.status, 423)
        XCTAssertEqual(failure.stage, .complete)
        XCTAssertEqual(model.items[0].state.isCompleted, true)
        XCTAssertEqual(model.items[1].state, .failed(failure))
        XCTAssertEqual(model.items[2].state, .notAttempted)

        let requestsBefore = RelayBatchURLProtocol.requests().count
        let signsBefore = fixture.backend.signs
        // Pressing the buttons again must not retry or move on.
        await model.prepareNext()
        await model.approveCurrent()
        await model.prepareNext()
        XCTAssertEqual(RelayBatchURLProtocol.requests().count, requestsBefore, "no auto-retry")
        XCTAssertEqual(fixture.backend.signs, signsBefore)
        XCTAssertEqual(fixture.backend.signs, 2)
        XCTAssertTrue(model.isFinished)
    }

    func testChallengeRefusalStopsBeforeAnyTouchIDForThatOrder() async throws {
        _ = try router.createIdentityIfNeeded(for: .india)
        let harness = try makeBatch { server in
            server.configure { $0.challengeFailure = ("p-0002", 409, #"{"detail":{"reason":"capital_cap"}}"#) }
        }
        let model = harness.model
        await model.prepareNext()
        await model.approveCurrent()
        await model.prepareNext()  // p-0002: refused at mint
        await model.approveCurrent()  // nothing to approve

        let failure = try XCTUnwrap(model.failure)
        XCTAssertEqual(failure.code, "capital_cap")
        XCTAssertEqual(failure.stage, .challenge)
        XCTAssertEqual(fixture.backend.signs, 1, "no Touch ID for a doomed order")
        XCTAssertEqual(model.items[2].state, .notAttempted)
        XCTAssertEqual(paths(), ["challenge:p-0001", "complete:p-0001", "challenge:p-0002"])
    }

    func testBatchStopsWhenTheBytesCarryADifferentBatch() async throws {
        _ = try router.createIdentityIfNeeded(for: .india)
        let harness = try makeBatch { $0.configure { $0.batchIdOverride = "batch-other-0001" } }
        await harness.model.prepareNext()
        let failure = try XCTUnwrap(harness.model.failure)
        XCTAssertEqual(failure.code, "batch_mismatch")
        XCTAssertEqual(fixture.backend.signs, 0)
        XCTAssertEqual(paths(), ["challenge:p-0001"])
    }

    func testBatchStopsWhenTheChallengeExpiredBeforeTheTouch() async throws {
        _ = try router.createIdentityIfNeeded(for: .india)
        let harness = try makeBatch()
        await harness.model.prepareNext()
        harness.clock.date = now.addingTimeInterval(61)
        await harness.model.approveCurrent()
        XCTAssertEqual(harness.model.failure?.code, "challenge_expired")
        XCTAssertEqual(fixture.backend.signs, 0)
        XCTAssertEqual(fixture.contexts.reasons.count, 0, "no prompt for an expired challenge")
        XCTAssertEqual(paths(), ["challenge:p-0001"])
    }

    func testCancellingTouchIDStopsTheBatchAndSendsNothing() async throws {
        _ = try router.createIdentityIfNeeded(for: .india)
        let harness = try makeBatch()
        fixture.backend.failSigning(with: ApprovalSignerError.underlying(LAError(.userCancel)))
        await harness.model.prepareNext()
        await harness.model.approveCurrent()
        XCTAssertEqual(harness.model.failure?.code, "touch_id_cancelled")
        XCTAssertEqual(harness.model.failure?.stage, .signing)
        XCTAssertEqual(paths(), ["challenge:p-0001"], "nothing is sent after a cancelled Touch ID")
        XCTAssertEqual(harness.server.verified, 0)
    }

    func testBytesForAForeignKeyAreRefusedByTheSignerAndNeverCompleted() async throws {
        _ = try router.createIdentityIfNeeded(for: .india)
        let other = try RelayVectors.key("other")
        let harness = try makeBatch { $0.configure { $0.keyIdOverride = other.keyId } }
        await harness.model.prepareNext()
        await harness.model.approveCurrent()
        XCTAssertEqual(harness.model.failure?.code, "key_mismatch")
        XCTAssertEqual(fixture.backend.signs, 0)
        XCTAssertTrue(fixture.contexts.reasons.isEmpty, "no Touch ID for bytes that name another key")
        XCTAssertEqual(paths(), ["challenge:p-0001"])
    }

    func testABackendSummaryLieIsFlaggedInTheBatchAndTheBytesStillDrivethePrompt() async throws {
        _ = try router.createIdentityIfNeeded(for: .india)
        let harness = try makeBatch(proposals: ["p-0001"]) {
            $0.configure { $0.summaryClaim = ["side": "buy", "stock_code": "EVIL"] }
        }
        await harness.model.prepareNext()
        guard case .reviewing(let review) = harness.model.items[0].state else { return XCTFail("expected a review") }
        XCTAssertEqual(Set(review.summaryDisagreements), ["side", "stock_code"])
        await harness.model.approveCurrent()
        let reason = try XCTUnwrap(fixture.contexts.reasons.first)
        XCTAssertTrue(reason.contains("SELL"))
        XCTAssertTrue(reason.contains("ABC"))
        XCTAssertFalse(reason.contains("EVIL"))
    }
}

private extension RelayOrderBatchViewModel.ItemState {
    var isCompleted: Bool {
        if case .completed = self { return true }
        return false
    }
}
