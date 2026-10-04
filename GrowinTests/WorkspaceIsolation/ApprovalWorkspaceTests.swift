import CryptoKit
import Foundation
import Testing
@testable import Growin

/// URLProtocol stub dedicated to the workspace approval tests. Records every
/// request and answers from `responder`.
final class ApprovalWorkspaceURLProtocol: URLProtocol {
    struct Recorded {
        let url: URL
        let method: String
        let body: Data?
    }

    private static let lock = NSLock()
    nonisolated(unsafe) private static var recorded: [Recorded] = []
    nonisolated(unsafe) private static var responder: (@Sendable (Recorded) -> (Int, Data))?

    static func install(_ responder: @escaping @Sendable (Recorded) -> (Int, Data)) {
        lock.lock()
        recorded = []
        self.responder = responder
        lock.unlock()
    }

    static func requests() -> [Recorded] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }

    static func makeSession() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [ApprovalWorkspaceURLProtocol.self]
        return URLSession(configuration: config)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let record = Recorded(
            url: request.url!,
            method: request.httpMethod ?? "",
            body: Self.body(from: request)
        )
        Self.lock.lock()
        Self.recorded.append(record)
        let responder = Self.responder
        Self.lock.unlock()

        let (status, payload) = responder?(record) ?? (500, Data())
        let response = HTTPURLResponse(
            url: record.url,
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: payload)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private static func body(from request: URLRequest) -> Data? {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: 1024)
        defer { buffer.deallocate() }
        while stream.hasBytesAvailable {
            let read = stream.read(buffer, maxLength: 1024)
            if read <= 0 { break }
            data.append(buffer, count: read)
        }
        return data.isEmpty ? nil : data
    }
}

@MainActor
@Suite(.serialized)
struct ApprovalWorkspaceTests {
    // MARK: Fixtures

    private static func makeSigner() -> (LocalApprovalSigner, KeychainStore, String) {
        let service = "san.Growin.credentials.v1.test.\(UUID().uuidString)"
        let store = KeychainStore(service: service)
        return (LocalApprovalSigner(store: store), store, service)
    }

    private static func cleanUp(_ store: KeychainStore) {
        for workspace in Workspace.allCases {
            try? store.remove(.approvalSigningKey, scope: .workspace(workspace))
        }
        try? store.removeLegacyFlatItem(.approvalSigningKey)
    }

    private static let proposal = TradeProposalData(
        proposalId: "p-1",
        ticker: "RELIANCE",
        action: "BUY",
        quantity: 10,
        reasoning: nil,
        status: nil
    )

    nonisolated private static func payloadJSON(workspace: String, keyId: String, expires: Int) -> Data {
        let json = """
        {"version":1,"purpose":"growin.execution.dispatch","challenge_id":"c-1","proposal_id":"p-1",\
        "client_order_id":"co-1","intent_hash":"ih","workspace":"\(workspace)","account":"paper",\
        "broker":"local-paper","mode":"PAPER","ticker":"RELIANCE","side":"BUY","quantity":"10",\
        "nonce":"n","issued_at":1,"expires_at":\(expires),"key_id":"\(keyId)"}
        """
        return Data(json.utf8)
    }

    nonisolated private static func challengeJSON(workspace: String, keyId: String = "key-1") -> Data {
        let expires = Int(Date().timeIntervalSince1970) + 600
        // issued_at and expires_at must match the payload bytes exactly.
        let payload = payloadJSON(workspace: workspace, keyId: keyId, expires: expires)
        let payloadB64 = payload.base64EncodedString()
        let json = """
        {"challenge_id":"c-1","proposal_id":"p-1","key_id":"\(keyId)","intent_hash":"ih",\
        "signed_payload_b64":"\(payloadB64)","issued_at":1,"expires_at":\(expires)}
        """
        return Data(json.utf8)
    }

    private static func makeChallenge(workspace: String) throws -> ApprovalChallengeResponse {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(ApprovalChallengeResponse.self, from: challengeJSON(workspace: workspace))
    }

    private static func body(_ record: ApprovalWorkspaceURLProtocol.Recorded) throws -> [String: String] {
        let data = try #require(record.body)
        return try JSONDecoder().decode([String: String].self, from: data)
    }

    /// Builds a review that bypasses the initializer's checks, to feed the
    /// client-side workspace guards.
    private static func review(workspace: String) throws -> TradeApprovalReview {
        let challenge = try makeChallenge(workspace: workspace)
        let bytes = try #require(Data(base64Encoded: challenge.signedPayloadB64))
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let payload = try decoder.decode(SignedTradeApprovalPayload.self, from: bytes)
        return TradeApprovalReview(challenge: challenge, payload: payload, signedBytes: bytes)
    }

    // MARK: Signer

    @Test func workspaceKeysDifferAndSignaturesVerifyOnlyWithTheirOwnKey() throws {
        let (signer, store, _) = Self.makeSigner()
        defer { Self.cleanUp(store) }

        let uk = try signer.createIdentityIfNeeded(for: .uk)
        let india = try signer.createIdentityIfNeeded(for: .india)
        #expect(uk.keyID != india.keyID)
        #expect(try signer.identity(for: .uk) == uk)

        let payload = Data("frozen review bytes".utf8)
        let signature = try signer.sign(payload, for: .uk)
        let der = try P256.Signing.ECDSASignature(derRepresentation: signature)
        let ukKey = try P256.Signing.PublicKey(x963Representation: uk.publicKeyX963)
        let indiaKey = try P256.Signing.PublicKey(x963Representation: india.publicKeyX963)
        #expect(ukKey.isValidSignature(der, for: payload))
        #expect(!indiaKey.isValidSignature(der, for: payload))
    }

    @Test func identityForMissingWorkspaceThrowsNotConfigured() throws {
        let (signer, store, _) = Self.makeSigner()
        defer { Self.cleanUp(store) }

        _ = try signer.createIdentityIfNeeded(for: .uk)
        #expect(signer.isConfigured(for: .uk))
        #expect(!signer.isConfigured(for: .india))
        do {
            _ = try signer.identity(for: .india)
            Issue.record("Expected notConfigured")
        } catch LocalApprovalSignerError.notConfigured {
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
        do {
            _ = try signer.sign(Data("x".utf8), for: .india)
            Issue.record("Expected notConfigured")
        } catch LocalApprovalSignerError.notConfigured {
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
    }

    // MARK: Review envelope

    @Test func reviewRejectsPayloadForAnotherWorkspace() throws {
        let challenge = try Self.makeChallenge(workspace: "india")
        do {
            _ = try TradeApprovalReview(
                challenge: challenge,
                expectedProposal: Self.proposal,
                expectedWorkspace: .uk
            )
            Issue.record("Expected invalidEnvelope")
        } catch TradeApprovalReviewError.invalidEnvelope {
        } catch {
            Issue.record("Unexpected error: \(error)")
        }

        let review = try TradeApprovalReview(
            challenge: challenge,
            expectedProposal: Self.proposal,
            expectedWorkspace: .india
        )
        #expect(review.payload.workspace == "india")
    }

    // MARK: Wire

    @Test func challengeAndCompleteNameTheWorkspace() async throws {
        ApprovalWorkspaceURLProtocol.install { record in
            if record.url.path.hasSuffix("/approval/challenge") {
                return (200, Self.challengeJSON(workspace: "india"))
            }
            return (200, Data(#"{"message":"done"}"#.utf8))
        }
        let service = AIService(session: ApprovalWorkspaceURLProtocol.makeSession())

        let review = try await service.requestTradeApproval(proposal: Self.proposal, workspace: .india)
        _ = try await service.completeTradeApproval(review, signature: Data([1, 2, 3]), workspace: .india)

        let requests = ApprovalWorkspaceURLProtocol.requests()
        #expect(requests.count == 2)
        #expect(requests[0].url.path == "/api/ai/trade/approval/challenge")
        #expect(try Self.body(requests[0])["workspace"] == "india")
        #expect(try Self.body(requests[0])["proposal_id"] == "p-1")
        #expect(requests[1].url.path == "/api/ai/trade/approval/complete")
        #expect(try Self.body(requests[1])["workspace"] == "india")
    }

    @Test func completeAndVerifyRefuseAReviewForAnotherWorkspaceWithoutSending() async throws {
        ApprovalWorkspaceURLProtocol.install { _ in (200, Data(#"{"message":"done"}"#.utf8)) }
        let service = AIService(session: ApprovalWorkspaceURLProtocol.makeSession())
        let ukReview = try Self.review(workspace: "uk")

        do {
            _ = try await service.completeTradeApproval(ukReview, signature: Data([1]), workspace: .india)
            Issue.record("Expected workspaceMismatch")
        } catch TradeApprovalReviewError.workspaceMismatch {
        }
        do {
            _ = try await service.verifyPaperRequoteCheck(ukReview, signature: Data([1]), workspace: .india)
            Issue.record("Expected workspaceMismatch")
        } catch TradeApprovalReviewError.workspaceMismatch {
        }
        #expect(ApprovalWorkspaceURLProtocol.requests().isEmpty)
    }

    @Test func statusSendsWorkspaceQueryAndRejectsAnotherWorkspaceResponse() async throws {
        ApprovalWorkspaceURLProtocol.install { _ in
            (200, Data(#"{"mode":"paper","enrolled":false,"key_id":null,"workspace":"india"}"#.utf8))
        }
        let service = AIService(session: ApprovalWorkspaceURLProtocol.makeSession())

        do {
            _ = try await service.approvalStatus(workspace: .uk)
            Issue.record("Expected workspaceMismatch")
        } catch TradeApprovalReviewError.workspaceMismatch {
        }
        let first = try #require(ApprovalWorkspaceURLProtocol.requests().first)
        #expect(first.url.query == "workspace=uk")

        let status = try await service.approvalStatus(workspace: .india)
        #expect(status.workspace == "india")
    }

    @Test func statusMaps409ToWorkspaceMismatchAndAcceptsNullWorkspace() async throws {
        ApprovalWorkspaceURLProtocol.install { _ in
            (409, Data(#"{"detail":"Workspace does not match the open execution ledger"}"#.utf8))
        }
        let service = AIService(session: ApprovalWorkspaceURLProtocol.makeSession())
        do {
            _ = try await service.approvalStatus(workspace: .uk)
            Issue.record("Expected workspaceMismatch")
        } catch TradeApprovalReviewError.workspaceMismatch {
        }

        ApprovalWorkspaceURLProtocol.install { _ in
            (200, Data(#"{"mode":"paper","enrolled":false,"key_id":null,"workspace":null}"#.utf8))
        }
        let status = try await service.approvalStatus(workspace: .uk)
        #expect(status.workspace == nil)
    }

    @Test func enrollAndUatRoutesNameTheWorkspace() async throws {
        let (signer, store, _) = Self.makeSigner()
        defer { Self.cleanUp(store) }
        let identity = try signer.createIdentityIfNeeded(for: .india)

        ApprovalWorkspaceURLProtocol.install { record in
            if record.url.path.hasSuffix("/enroll") {
                return (200, Data(#"{"key_id":"\#(identity.keyID)"}"#.utf8))
            }
            if record.url.path.hasSuffix("/uat-proposal") {
                return (200, Data(#"{"proposal_id":"p-1","ticker":"RELIANCE","action":"BUY","quantity":10}"#.utf8))
            }
            return (200, Data(#"{"message":"verified"}"#.utf8))
        }
        let service = AIService(session: ApprovalWorkspaceURLProtocol.makeSession())

        _ = try await service.enrollApprovalKey(identity: identity, token: "tok", workspace: .india)
        _ = try await service.createPaperApprovalCheck(workspace: .india)
        _ = try await service.createPaperRequoteCheck(workspace: .india)
        _ = try await service.verifyPaperRequoteCheck(try Self.review(workspace: "india"), signature: Data([1]), workspace: .india)

        let requests = ApprovalWorkspaceURLProtocol.requests()
        #expect(requests.map(\.url.path) == [
            "/api/ai/trade/approval/enroll",
            "/api/ai/trade/approval/uat-proposal",
            "/api/ai/trade/requote/uat-proposal",
            "/api/ai/trade/requote/uat/verify",
        ])
        for request in requests {
            #expect(try Self.body(request)["workspace"] == "india", "\(request.url.path)")
        }
    }

    @Test func approveAndRejectNameTheWorkspace() async throws {
        ApprovalWorkspaceURLProtocol.install { _ in (200, Data(#"{"message":"ok"}"#.utf8)) }
        let service = AIService(session: ApprovalWorkspaceURLProtocol.makeSession())

        _ = try await service.approveTrade(id: "p-1", workspace: .uk)
        _ = try await service.rejectTrade(id: "p-1", notes: "n", workspace: .uk)

        let requests = ApprovalWorkspaceURLProtocol.requests()
        #expect(requests.count == 2)
        for request in requests {
            let raw = try #require(request.body)
            let object = try #require(JSONSerialization.jsonObject(with: raw) as? [String: String])
            #expect(object["workspace"] == "uk")
            #expect(object["proposal_id"] == "p-1")
        }
    }

    // MARK: India fixing

    @Test func paperOperationsAdapterRequestsAsIndia() async throws {
        ApprovalWorkspaceURLProtocol.install { _ in (200, Self.challengeJSON(workspace: "india")) }
        let service = AIService(session: ApprovalWorkspaceURLProtocol.makeSession())
        let approver = AIServicePaperTradeApprover(service: service)

        let review = try await approver.requestTradeApproval(proposal: Self.proposal)
        #expect(review.payload.workspace == "india")
        let first = try #require(ApprovalWorkspaceURLProtocol.requests().first)
        #expect(try Self.body(first)["workspace"] == "india")
    }

    @Test func paperOperationsSignerAdapterUsesIndiaOnly() throws {
        let source = try PaperOperationsSourceProbe.contents("Growin/Models/PaperOperationsModels.swift")
        let start = try #require(source.range(of: "final class LocalPaperApprovalSigner"))
        let tail = source[start.lowerBound...]
        let end = try #require(tail.range(of: "\n}\n"))
        let adapters = String(tail[tail.startIndex..<end.upperBound])
        #expect(adapters.contains("isConfigured(for: .india)"))
        #expect(adapters.contains("identity(for: .india)"))
        #expect(adapters.contains("sign(payload, for: .india)"))
        #expect(!adapters.contains(".uk"))
    }

    // MARK: Task 2: selection, duplicate refusal, legacy adoption

    @Test func workspaceSelectionStartsEmptyAndRoundTrips() throws {
        let suite = "san.Growin.tests.selection.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }

        #expect(WorkspaceSelection.current(defaults) == nil)
        WorkspaceSelection.set(.india, defaults)
        #expect(WorkspaceSelection.current(defaults) == .india)
        defaults.set("mars", forKey: WorkspaceSelection.defaultsKey)
        #expect(WorkspaceSelection.current(defaults) == nil)
        defaults.set("", forKey: WorkspaceSelection.defaultsKey)
        #expect(WorkspaceSelection.current(defaults) == nil)
        WorkspaceSelection.set(.uk, defaults)
        WorkspaceSelection.set(nil, defaults)
        #expect(WorkspaceSelection.current(defaults) == nil)
    }

    @Test func identicalKeyInBothWorkspacesIsRefused() throws {
        let (signer, store, _) = Self.makeSigner()
        defer { Self.cleanUp(store) }

        let raw = P256.Signing.PrivateKey().rawRepresentation
        try store.set(raw, for: .approvalSigningKey, scope: .workspace(.uk))
        try store.set(raw, for: .approvalSigningKey, scope: .workspace(.india))

        for workspace in Workspace.allCases {
            do {
                _ = try signer.identity(for: workspace)
                Issue.record("Expected duplicateKeyAcrossWorkspaces for \(workspace)")
            } catch LocalApprovalSignerError.duplicateKeyAcrossWorkspaces {
            } catch {
                Issue.record("Unexpected error: \(error)")
            }
            do {
                _ = try signer.createIdentityIfNeeded(for: workspace)
                Issue.record("Expected duplicateKeyAcrossWorkspaces for \(workspace)")
            } catch LocalApprovalSignerError.duplicateKeyAcrossWorkspaces {
            } catch {
                Issue.record("Unexpected error: \(error)")
            }
            do {
                _ = try signer.sign(Data("x".utf8), for: workspace)
                Issue.record("Expected duplicateKeyAcrossWorkspaces for \(workspace)")
            } catch LocalApprovalSignerError.duplicateKeyAcrossWorkspaces {
            } catch {
                Issue.record("Unexpected error: \(error)")
            }
        }
    }

    private static let legacyAccount = "approvalSoftwareP256PrivateKey.v1"

    @Test func legacyAdoptionIsUkOnlyAndNeedsAMatchingKeyID() throws {
        let (signer, store, service) = Self.makeSigner()
        defer { Self.cleanUp(store) }
        let raw = RawKeychain(service: service)

        let legacyKey = P256.Signing.PrivateKey()
        try raw.set(legacyKey.rawRepresentation, account: Self.legacyAccount)
        let legacyKeyID = SHA256.hash(data: legacyKey.publicKey.x963Representation)
            .map { String(format: "%02x", $0) }.joined()
        #expect(signer.hasLegacyFlatKey())

        // India never adopts the flat key.
        do {
            _ = try signer.adoptLegacyKey(into: .india, expectedKeyID: legacyKeyID)
            Issue.record("Expected legacyAdoptionNotAllowed")
        } catch LocalApprovalSignerError.legacyAdoptionNotAllowed {
        } catch {
            Issue.record("Unexpected error: \(error)")
        }

        // A wrong key ID leaves the flat item in place.
        do {
            _ = try signer.adoptLegacyKey(into: .uk, expectedKeyID: "not-the-key")
            Issue.record("Expected legacyKeyMismatch")
        } catch LocalApprovalSignerError.legacyKeyMismatch {
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
        #expect(try raw.data(account: Self.legacyAccount) == legacyKey.rawRepresentation)
        #expect(!signer.isConfigured(for: .uk))

        // The right key ID moves it, and the flat item is gone.
        let adopted = try signer.adoptLegacyKey(into: .uk, expectedKeyID: legacyKeyID)
        #expect(adopted.keyID == legacyKeyID)
        #expect(try signer.identity(for: .uk) == adopted)
        #expect(try raw.data(account: Self.legacyAccount) == nil)
        #expect(!signer.hasLegacyFlatKey())

        // A second call has nothing to adopt.
        do {
            _ = try signer.adoptLegacyKey(into: .uk, expectedKeyID: legacyKeyID)
            Issue.record("Expected noLegacyKey")
        } catch LocalApprovalSignerError.noLegacyKey {
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
        // India is untouched and still has no key.
        #expect(!signer.isConfigured(for: .india))
    }

    @Test func legacyAdoptionRefusesWhenAUkKeyAlreadyExists() throws {
        let (signer, store, service) = Self.makeSigner()
        defer { Self.cleanUp(store) }
        let raw = RawKeychain(service: service)

        _ = try signer.createIdentityIfNeeded(for: .uk)
        let legacyKey = P256.Signing.PrivateKey()
        try raw.set(legacyKey.rawRepresentation, account: Self.legacyAccount)
        let legacyKeyID = SHA256.hash(data: legacyKey.publicKey.x963Representation)
            .map { String(format: "%02x", $0) }.joined()

        do {
            _ = try signer.adoptLegacyKey(into: .uk, expectedKeyID: legacyKeyID)
            Issue.record("Expected workspaceKeyExists")
        } catch LocalApprovalSignerError.workspaceKeyExists {
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
        #expect(try raw.data(account: Self.legacyAccount) == legacyKey.rawRepresentation)
    }

    @Test func settingsHasNoHardCodedUkAndNoNoArgumentSignerUse() throws {
        let settings = try PaperOperationsSourceProbe.contents("Growin/Views/SettingsView.swift")
        #expect(!settings.contains("workspaces/uk/"))
        #expect(settings.contains("WorkspaceSelection.defaultsKey"))
        let chat = try PaperOperationsSourceProbe.contents("Growin/ViewModels/ChatViewModel.swift")
        for source in [settings, chat] {
            #expect(!source.contains("LocalApprovalSigner.shared.identity()"))
            #expect(!source.contains("LocalApprovalSigner.shared.isConfigured\n"))
            #expect(!source.contains("LocalApprovalSigner.shared.sign(review.signedBytes)"))
        }
    }
}
