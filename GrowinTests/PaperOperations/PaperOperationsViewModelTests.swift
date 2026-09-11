import Foundation
import Testing
@testable import Growin

struct PaperOperationsViewModelTests {
    private func makeClient() -> PaperOperationsClient {
        PaperOperationsURLProtocol.reset()
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [PaperOperationsURLProtocol.self]
        let session = URLSession(configuration: config)
        return PaperOperationsClient(
            session: session,
            baseURL: URL(string: "http://127.0.0.1:8002")!
        )
    }

    @Test @MainActor
    func constructingViewModelDoesNotIssueAClientCall() {
        let client = makeClient()
        _ = PaperOperationsViewModel(client: client)

        #expect(PaperOperationsURLProtocol.recordedURLs.isEmpty)
    }

    @Test @MainActor
    func canPrepareIsFalseWhileSessionIsStopped() {
        let viewModel = PaperOperationsViewModel(client: makeClient())

        #expect(viewModel.sessionState == "STOPPED")
        #expect(viewModel.canPrepare == false)
        #expect(viewModel.blockingReason?.copy == PaperOperationsCopy.stopped)
    }

    @Test @MainActor
    func malformedPayloadMapsToDurableBlockingReasonAndKeepsLastEvidence() async throws {
        let client = makeClient()
        let viewModel = PaperOperationsViewModel(client: client)
        viewModel.lastEvidence = PaperOperationsEvidence(
            snapshotSymbol: "RELIANCE",
            source: "local-replay",
            bid: "99.02",
            ask: "101.02"
        )

        PaperOperationsURLProtocol.reset()
        await viewModel.applyMalformedSnapshotPayload(
            Data(#"{"source":"local-replay","ask":"101"}"#.utf8)
        )

        #expect(viewModel.canPrepare == false)
        #expect(viewModel.blockingReason != nil)
        #expect(viewModel.blockingReason?.kind == .malformed)
        #expect(viewModel.lastEvidence?.snapshotSymbol == "RELIANCE")
        #expect(viewModel.lastEvidence?.bid == "99.02")
    }
}
