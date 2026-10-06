import Foundation
import Testing
@testable import Growin

/// A URLProtocol stub private to the practice approval tests. It records every
/// request and answers from `responder`. No test here contacts a network.
final class PracticeURLProtocol: URLProtocol {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var recorded: [URL] = []
    nonisolated(unsafe) private static var responder: (@Sendable (URL) -> (Int, Data))?

    static func install(_ responder: @escaping @Sendable (URL) -> (Int, Data)) {
        lock.lock()
        recorded = []
        self.responder = responder
        lock.unlock()
    }

    static func requests() -> [URL] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }

    static func makeSession() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [PracticeURLProtocol.self]
        return URLSession(configuration: config)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let url = request.url!
        Self.lock.lock()
        Self.recorded.append(url)
        let responder = Self.responder
        Self.lock.unlock()
        let (status, payload) = responder?(url) ?? (500, Data())
        let response = HTTPURLResponse(
            url: url, statusCode: status, httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: payload)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

@MainActor
@Suite(.serialized)
struct PracticeApprovalTests {
    // MARK: Fixtures

    private static let proposal = PracticeProposal(
        proposalId: "p-1",
        ticker: "VODl_EQ",
        action: "BUY",
        quantity: "2",
        orderType: "LIMIT",
        limitPrice: "71.3",
        timeValidity: "DAY",
        mode: "PRACTICE",
        broker: "t212_practice",
        status: "PENDING",
        reasoning: nil,
        notional: "1.426"
    )

    /// A signed-payload dictionary for the practice proposal above. Keys are the
    /// backend's snake_case names; a nil value leaves the key out.
    private static func payload(
        mode: String = "PRACTICE",
        broker: String = "t212_practice",
        workspace: String = "uk",
        orderType: String? = "LIMIT",
        limitPrice: String? = "71.3",
        ticker: String = "VODl_EQ",
        side: String = "BUY",
        quantity: String = "2",
        expires: Int
    ) -> [String: Any] {
        var json: [String: Any] = [
            "version": 1,
            "purpose": "growin.execution.dispatch",
            "challenge_id": "c-1",
            "proposal_id": "p-1",
            "client_order_id": "co-1",
            "intent_hash": "ih",
            "workspace": workspace,
            "account": "20260001",
            "broker": broker,
            "mode": mode,
            "ticker": ticker,
            "side": side,
            "quantity": quantity,
            "nonce": "n",
            "issued_at": 1,
            "expires_at": expires,
            "key_id": "key-1",
        ]
        if let orderType { json["order_type"] = orderType }
        if let limitPrice { json["limit_price"] = limitPrice }
        return json
    }

    private static func challenge(_ json: [String: Any], expires: Int) throws -> ApprovalChallengeResponse {
        let bytes = try JSONSerialization.data(withJSONObject: json, options: [.sortedKeys])
        return ApprovalChallengeResponse(
            challengeId: "c-1",
            proposalId: "p-1",
            keyId: "key-1",
            intentHash: "ih",
            signedPayloadB64: bytes.base64EncodedString(),
            issuedAt: 1,
            expiresAt: expires
        )
    }

    private static func review(
        _ proposal: PracticeProposal? = nil,
        workspace: Workspace = .uk,
        mode: String = "PRACTICE",
        broker: String = "t212_practice",
        payloadWorkspace: String = "uk",
        orderType: String? = "LIMIT",
        limitPrice: String? = "71.3",
        ticker: String = "VODl_EQ",
        side: String = "BUY",
        quantity: String = "2"
    ) throws -> TradeApprovalReview {
        let expires = Int(Date().timeIntervalSince1970) + 600
        let json = payload(
            mode: mode, broker: broker, workspace: payloadWorkspace, orderType: orderType,
            limitPrice: limitPrice, ticker: ticker, side: side, quantity: quantity, expires: expires
        )
        return try TradeApprovalReview(
            challenge: try challenge(json, expires: expires),
            expectedPractice: proposal ?? Self.proposal,
            expectedWorkspace: workspace
        )
    }

    private static func rejects(_ build: () throws -> TradeApprovalReview) -> Bool {
        do {
            _ = try build()
            return false
        } catch {
            return true
        }
    }

    // MARK: Decoding a matching PRACTICE payload

    @Test func aPracticePayloadMatchingTheProposalDecodesIntoAReview() throws {
        let review = try Self.review()
        #expect(review.payload.mode == "PRACTICE")
        #expect(review.payload.broker == "t212_practice")
        #expect(review.payload.orderType == "LIMIT")
        #expect(Decimal(string: review.payload.limitPrice ?? "") == Decimal(string: "71.3"))
        #expect(review.payload.workspace == "uk")
        #expect(!review.signedBytes.isEmpty)
    }

    @Test func theBackendProposalListingDecodesAndCarriesNoAccountId() throws {
        let json = """
        {"proposals":[{"proposal_id":"p-1","ticker":"VODl_EQ","action":"BUY","quantity":"2",\
        "order_type":"LIMIT","limit_price":"71.3","time_validity":"DAY","mode":"PRACTICE",\
        "broker":"t212_practice","status":"PENDING","notional":"1.426"}]}
        """
        let list = try JSONDecoder().decode(PracticeProposalList.self, from: Data(json.utf8))
        #expect(list.proposals == [Self.proposal.withoutReasoning()])
        #expect(!json.contains("20260001"))
    }

    // MARK: Refusals

    @Test(arguments: ["LIVE", "live", "PAPER", "FUTURE", "", "practice", "PRACTICE "])
    func liveAndAnyOtherModeAreRejected(mode: String) {
        #expect(Self.rejects { try Self.review(mode: mode) })
    }

    @Test func aProposalThatIsNotPracticeIsRejectedEvenWithAPracticePayload() {
        for mode in ["PAPER", "LIVE", "FUTURE"] {
            let other = PracticeProposal(
                proposalId: "p-1", ticker: "VODl_EQ", action: "BUY", quantity: "2", orderType: "LIMIT",
                limitPrice: "71.3", timeValidity: "DAY", mode: mode, broker: "t212_practice",
                status: "PENDING", reasoning: nil, notional: nil
            )
            #expect(Self.rejects { try Self.review(other) })
        }
    }

    @Test func aPayloadWhoseLimitPriceDiffersFromTheProposalIsRejected() {
        #expect(Self.rejects { try Self.review(limitPrice: "71.4") })
        #expect(Self.rejects { try Self.review(limitPrice: "7.13") })
        #expect(Self.rejects { try Self.review(limitPrice: nil) })
        #expect(Self.rejects { try Self.review(limitPrice: "not-a-price") })
    }

    @Test(arguments: ["MARKET", "STOP", "STOP_LIMIT", "limit", ""])
    func aPayloadWhoseOrderTypeDiffersFromTheProposalIsRejected(orderType: String) {
        #expect(Self.rejects { try Self.review(orderType: orderType) })
        #expect(Self.rejects { try Self.review(orderType: nil) })
    }

    @Test func aProposalThatIsNotLimitDayIsRejected() {
        let gtc = PracticeProposal(
            proposalId: "p-1", ticker: "VODl_EQ", action: "BUY", quantity: "2", orderType: "LIMIT",
            limitPrice: "71.3", timeValidity: "GOOD_TILL_CANCEL", mode: "PRACTICE", broker: "t212_practice",
            status: "PENDING", reasoning: nil, notional: nil
        )
        let market = PracticeProposal(
            proposalId: "p-1", ticker: "VODl_EQ", action: "BUY", quantity: "2", orderType: "MARKET",
            limitPrice: "71.3", timeValidity: "DAY", mode: "PRACTICE", broker: "t212_practice",
            status: "PENDING", reasoning: nil, notional: nil
        )
        #expect(Self.rejects { try Self.review(gtc) })
        #expect(Self.rejects { try Self.review(market, orderType: "MARKET") })
    }

    @Test func aWrongBrokerWorkspaceTickerSideOrQuantityIsRejected() {
        #expect(Self.rejects { try Self.review(broker: "trading212") })
        #expect(Self.rejects { try Self.review(broker: "paper") })
        #expect(Self.rejects { try Self.review(payloadWorkspace: "india") })
        #expect(Self.rejects { try Self.review(workspace: .india) })
        #expect(Self.rejects { try Self.review(ticker: "LLOYl_EQ") })
        #expect(Self.rejects { try Self.review(side: "SELL") })
        #expect(Self.rejects { try Self.review(quantity: "3") })
    }

    @Test func anExpiredChallengeIsRejected() throws {
        let expired = Int(Date().timeIntervalSince1970) - 5
        let json = Self.payload(expires: expired)
        let challenge = try Self.challenge(json, expires: expired)
        #expect(Self.rejects {
            try TradeApprovalReview(challenge: challenge, expectedPractice: Self.proposal, expectedWorkspace: .uk)
        })
    }

    @Test func thePaperReviewStillRefusesAPracticePayload() throws {
        let expires = Int(Date().timeIntervalSince1970) + 600
        let json = Self.payload(expires: expires)
        let challenge = try Self.challenge(json, expires: expires)
        let paper = TradeProposalData(
            proposalId: "p-1", ticker: "VODl_EQ", action: "BUY", quantity: 2, reasoning: nil, status: nil
        )
        #expect(Self.rejects {
            try TradeApprovalReview(challenge: challenge, expectedProposal: paper, expectedWorkspace: .uk)
        })
    }

    // MARK: Status mode and the enrolment token

    @Test func statusModePracticeAllowsEnrolmentAndOtherModesDoNot() {
        #expect(ApprovalEnrollment.allowsEnrolment(mode: "practice"))
        #expect(ApprovalEnrollment.allowsEnrolment(mode: "paper"))
        for mode in ["disabled", "live", "", "PRACTICE", "unknown"] {
            #expect(!ApprovalEnrollment.allowsEnrolment(mode: mode))
        }
    }

    @Test func thePracticeTokenSitsBesideThePracticeLedgerAndThePaperPathIsUnchanged() {
        let home = URL(fileURLWithPath: "/Users/someone")
        let base = "/Users/someone/Library/Application Support/Growin/workspaces"
        #expect(ApprovalEnrollment.tokenURL(workspace: .uk, mode: "practice", home: home)?.path
                == "\(base)/uk-t212-practice/execution.sqlite3.enrollment-token")
        #expect(ApprovalEnrollment.tokenURL(workspace: .uk, mode: "paper", home: home)?.path
                == "\(base)/uk/execution.sqlite3.enrollment-token")
        #expect(ApprovalEnrollment.tokenURL(workspace: .india, mode: "paper", home: home)?.path
                == "\(base)/india/execution.sqlite3.enrollment-token")
    }

    @Test func thereIsNoTokenPathForPracticeInIndiaOrForAnyOtherMode() {
        let home = URL(fileURLWithPath: "/Users/someone")
        #expect(ApprovalEnrollment.tokenURL(workspace: .india, mode: "practice", home: home) == nil)
        #expect(ApprovalEnrollment.tokenURL(workspace: .uk, mode: "disabled", home: home) == nil)
        #expect(ApprovalEnrollment.tokenURL(workspace: .uk, mode: "live", home: home) == nil)
    }

    // MARK: Copy and client

    @Test func theBannerNamesPracticeTheDemoAndVirtualFunds() {
        #expect(PracticeApprovalCopy.banner == "PRACTICE · Trading 212 demo · virtual funds")
        #expect(PracticeApprovalPolicy.broker == "t212_practice")
        #expect(PracticeApprovalPolicy.orderType == "LIMIT")
        #expect(PracticeApprovalPolicy.timeValidity == "DAY")
    }

    @Test func practiceProposalsAreReadFromTheLoopbackListingOnly() async throws {
        let body = """
        {"proposals":[{"proposal_id":"p-1","ticker":"VODl_EQ","action":"BUY","quantity":"2",\
        "order_type":"LIMIT","limit_price":"71.3","time_validity":"DAY","mode":"PRACTICE",\
        "broker":"t212_practice","status":"PENDING"}]}
        """
        PracticeURLProtocol.install { _ in (200, Data(body.utf8)) }
        let service = AIService(session: PracticeURLProtocol.makeSession())
        let proposals = try await service.practiceProposals()
        #expect(proposals.map(\.proposalId) == ["p-1"])
        let urls = PracticeURLProtocol.requests()
        #expect(urls.count == 1)
        #expect(urls.first?.path == "/api/t212-practice/proposals")
        #expect(urls.first?.host != "demo.trading212.com")
    }

    @Test func aPracticeApprovalRequestForTheIndiaWorkspaceSendsNothing() async {
        PracticeURLProtocol.install { _ in (200, Data("{}".utf8)) }
        let service = AIService(session: PracticeURLProtocol.makeSession())
        do {
            _ = try await service.requestPracticeApproval(proposal: Self.proposal, workspace: .india)
            Issue.record("Expected a workspace mismatch")
        } catch TradeApprovalReviewError.workspaceMismatch {
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
        #expect(PracticeURLProtocol.requests().isEmpty)
    }
}

private extension PracticeProposal {
    func withoutReasoning() -> PracticeProposal {
        PracticeProposal(
            proposalId: proposalId, ticker: ticker, action: action, quantity: quantity,
            orderType: orderType, limitPrice: limitPrice, timeValidity: timeValidity, mode: mode,
            broker: broker, status: status, reasoning: nil, notional: notional
        )
    }
}
