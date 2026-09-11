import Foundation
import Testing
@testable import Growin

struct PaperOperationsViewModelTests {
    private func makeClient() -> PaperOperationsClient {
        PaperOperationsURLProtocol.reset()
        return PaperOperationsClient(
            session: PaperOperationsClientTests.makeTestSession(),
            baseURL: URL(string: "http://127.0.0.1:8002")!
        )
    }

    @Test
    func constructingViewModelDoesNotIssueAClientCall() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let client = makeClient()
            _ = PaperOperationsViewModel(client: client, signer: StubPaperApprovalSigner(configured: false))
            #expect(PaperOperationsURLProtocol.snapshotRecord().urls.isEmpty)
        }
    }

    @Test
    func canPrepareIsFalseWhileSessionIsStopped() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: false)
            )

            #expect(viewModel.sessionState == "STOPPED")
            #expect(viewModel.canPrepare == false)
            #expect(viewModel.blockingReason?.copy == PaperOperationsCopy.stopped)
        }
    }

    @Test
    func startLocalReplayPostsConfirmationThenGetsRelianceSnapshot() async throws {
        try await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: false)
            )

            await viewModel.startLocalReplay()

            let record = PaperOperationsURLProtocol.snapshotRecord()
            #expect(record.methods == ["POST", "GET"])
            #expect(record.urls.map(\.path) == [
                "/api/market-data/sessions",
                "/api/market-data/snapshots/RELIANCE",
            ])
            let body = try #require(record.bodies.first)
            let object = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
            #expect(object["confirmation"] as? String == "START_READ_ONLY_REPLAY")
            #expect(viewModel.canPrepare == false)
            #expect(viewModel.sessionState == "RUNNING")
        }
    }

    @Test
    func malformedPayloadMapsToDurableBlockingReasonAndKeepsLastEvidence() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: false)
            )
            viewModel.lastEvidence = PaperOperationsEvidence(
                snapshotSymbol: "RELIANCE",
                source: "local-replay",
                bid: "99.02",
                ask: "101.02"
            )

            viewModel.applyMalformedSnapshotPayload(
                Data(#"{"source":"local-replay","ask":"101"}"#.utf8)
            )

            #expect(viewModel.canPrepare == false)
            #expect(viewModel.blockingReason != nil)
            #expect(viewModel.blockingReason?.kind == .malformed)
            #expect(viewModel.lastEvidence?.snapshotSymbol == "RELIANCE")
            #expect(viewModel.lastEvidence?.bid == "99.02")
        }
    }
}

final class StubPaperApprovalSigner: PaperApprovalSigning {
    var isConfigured: Bool

    init(configured: Bool) {
        isConfigured = configured
    }

    func identity() throws -> ApprovalSignerIdentity {
        ApprovalSignerIdentity(keyID: "test-key", publicKeyX963: Data())
    }

    func sign(_ payload: Data) throws -> Data {
        Data()
    }
}
