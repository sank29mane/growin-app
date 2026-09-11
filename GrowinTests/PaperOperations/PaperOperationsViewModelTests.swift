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

    @Test
    func stopLocalReplayDeletesCurrentAndDoesNotGetSnapshot() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true)
            )
            viewModel.unreconciledIntent = true
            await viewModel.startLocalReplay()
            PaperOperationsURLProtocol.reset()

            await viewModel.stopLocalReplay()

            let record = PaperOperationsURLProtocol.snapshotRecord()
            #expect(record.methods == ["DELETE"])
            #expect(record.urls.map(\.path) == ["/api/market-data/sessions/current"])
            #expect(record.urls.allSatisfy { !$0.path.contains("/snapshots/") })
            #expect(viewModel.unreconciledIntent == true)
            #expect(viewModel.canPrepare == false)
        }
    }

    @Test
    func refreshSessionStatusDoesNotRecordSnapshotURLAndLeavesStaleBlocking() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true)
            )
            await viewModel.startLocalReplay()
            viewModel.lastEvidence = PaperOperationsEvidence(
                snapshotSymbol: "RELIANCE",
                source: "local-replay",
                bid: "99.02",
                ask: "101.02"
            )
            viewModel.blockingReason = .staleSnapshot
            PaperOperationsURLProtocol.reset()
            PaperOperationsURLProtocol.overrideCurrentPayload = Data(
                #"{"state":"RUNNING","provider":"local-replay","instruments":[{"workspace":"india","venue":"NSE","segment":"CASH","symbol":"RELIANCE","currency":"INR"}],"read_only":true}"#.utf8
            )

            await viewModel.refreshSessionStatus()

            let record = PaperOperationsURLProtocol.snapshotRecord()
            #expect(record.methods == ["GET"])
            #expect(record.urls.map(\.path) == ["/api/market-data/sessions/current"])
            #expect(record.urls.allSatisfy { !$0.path.contains("/snapshots/") })
            #expect(viewModel.blockingReason?.kind == .staleSnapshot)
            #expect(viewModel.canPrepare == false)
            #expect(viewModel.lastEvidence?.snapshotSymbol == "RELIANCE")
        }
    }

    @Test
    func loadSnapshotEvidenceOnStaleSnapshotSetsDurableStaleAndKeepsLastEvidence() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true)
            )
            await viewModel.startLocalReplay()
            viewModel.lastEvidence = PaperOperationsEvidence(
                snapshotSymbol: "RELIANCE",
                source: "local-replay",
                bid: "99.02",
                ask: "101.02"
            )
            PaperOperationsURLProtocol.reset()
            PaperOperationsURLProtocol.overrideSnapshotStatus = 409
            PaperOperationsURLProtocol.overrideSnapshotPayload = Data(
                #"{"detail":{"code":"STALE_SNAPSHOT","message":"top-of-book snapshot is stale"}}"#.utf8
            )

            await viewModel.loadSnapshotEvidence()

            let record = PaperOperationsURLProtocol.snapshotRecord()
            #expect(record.urls.map(\.path) == ["/api/market-data/snapshots/RELIANCE"])
            #expect(viewModel.blockingReason?.kind == .staleSnapshot)
            #expect(viewModel.blockingReason?.copy == PaperOperationsCopy.staleSnapshot)
            #expect(viewModel.canPrepare == false)
            #expect(viewModel.lastEvidence?.bid == "99.02")
        }
    }

    @Test
    func zeroInstrumentsClearsSelectedSymbol() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true)
            )
            await viewModel.startLocalReplay()
            #expect(viewModel.selectedInstrumentSymbol == "RELIANCE")
            PaperOperationsURLProtocol.reset()
            PaperOperationsURLProtocol.overrideCurrentPayload = Data(
                #"{"state":"RUNNING","provider":"local-replay","instruments":[],"read_only":true}"#.utf8
            )

            await viewModel.refreshSessionStatus()

            #expect(viewModel.selectedInstrumentSymbol == nil)
            #expect(viewModel.session.instruments.isEmpty)
            #expect(viewModel.canPrepare == false)
        }
    }

    @Test
    func manyInstrumentsDoNotImplicitlySelectOnStart() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true)
            )
            PaperOperationsURLProtocol.overrideStartPayload = Data(
                #"{"state":"RUNNING","provider":"local-replay","instruments":[{"workspace":"india","venue":"NSE","segment":"CASH","symbol":"RELIANCE","currency":"INR"},{"workspace":"india","venue":"NSE","segment":"CASH","symbol":"INFY","currency":"INR"}],"read_only":true}"#.utf8
            )

            await viewModel.startLocalReplay()

            #expect(viewModel.session.instruments.count == 2)
            #expect(viewModel.selectedInstrumentSymbol == nil)
            #expect(viewModel.canPrepare == false)
        }
    }

    @Test
    func refreshAutoSelectsWhenExactlyOneInstrumentRemains() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true)
            )
            PaperOperationsURLProtocol.overrideStartPayload = Data(
                #"{"state":"RUNNING","provider":"local-replay","instruments":[{"workspace":"india","venue":"NSE","segment":"CASH","symbol":"RELIANCE","currency":"INR"},{"workspace":"india","venue":"NSE","segment":"CASH","symbol":"INFY","currency":"INR"}],"read_only":true}"#.utf8
            )
            await viewModel.startLocalReplay()
            PaperOperationsURLProtocol.reset()
            PaperOperationsURLProtocol.overrideCurrentPayload = Data(
                #"{"state":"RUNNING","provider":"local-replay","instruments":[{"workspace":"india","venue":"NSE","segment":"CASH","symbol":"RELIANCE","currency":"INR"}],"read_only":true}"#.utf8
            )

            await viewModel.refreshSessionStatus()

            #expect(viewModel.selectedInstrumentSymbol == "RELIANCE")
        }
    }

    @Test
    func pickerSelectionRejectsSymbolsOutsideSessionInstruments() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true)
            )
            await viewModel.startLocalReplay()

            viewModel.selectedInstrumentSymbol = "NOT_IN_SESSION"

            #expect(viewModel.selectedInstrumentSymbol == "RELIANCE")
            #expect(viewModel.session.instruments.contains { $0.symbol == viewModel.selectedInstrumentSymbol })
        }
    }

    @Test
    func runningWithoutSnapshotKeepsCanPrepareFalseAndMissingCopy() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true)
            )
            PaperOperationsURLProtocol.overrideStartPayload = Data(
                #"{"state":"RUNNING","provider":"local-replay","instruments":[{"workspace":"india","venue":"NSE","segment":"CASH","symbol":"RELIANCE","currency":"INR"},{"workspace":"india","venue":"NSE","segment":"CASH","symbol":"INFY","currency":"INR"}],"read_only":true}"#.utf8
            )

            await viewModel.startLocalReplay()
            viewModel.selectedInstrumentSymbol = "RELIANCE"

            #expect(viewModel.sessionState == "RUNNING")
            #expect(viewModel.snapshot == nil)
            #expect(viewModel.regimeId == nil)
            #expect(viewModel.canPrepare == false)
            #expect(viewModel.blockingReason?.kind == .missingSnapshot)
            #expect(viewModel.blockingReason?.copy == PaperOperationsCopy.missingSnapshot)
            #expect(viewModel.disabledPrepareAccessibilityHint == PaperOperationsCopy.missingSnapshot)
        }
    }

    @Test
    func signerNotConfiguredBlocksPrepareWithoutCreatingIdentity() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let signer = StubPaperApprovalSigner(configured: false)
            let viewModel = PaperOperationsViewModel(client: makeClient(), signer: signer)

            await viewModel.startLocalReplay()

            #expect(viewModel.sessionState == "RUNNING")
            #expect(viewModel.snapshot != nil)
            #expect(signer.identityCallCount == 0)
            #expect(signer.signCallCount == 0)
            #expect(viewModel.canPrepare == false)
            #expect(viewModel.blockingReason?.kind == .signerMissing)
            #expect(viewModel.blockingReason?.copy == PaperOperationsCopy.signerMissing)
            #expect(viewModel.disabledPrepareAccessibilityHint == PaperOperationsCopy.signerMissing)
        }
    }

    @Test
    func unreconciledIntentBlocksPrepareWithUnreconciledCopy() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true)
            )
            await viewModel.startLocalReplay()
            #expect(viewModel.canPrepare == true)

            viewModel.unreconciledIntent = true

            #expect(viewModel.canPrepare == false)
            #expect(viewModel.blockingReason?.kind == .unreconciled)
            #expect(viewModel.blockingReason?.copy == PaperOperationsCopy.unreconciled)
            #expect(viewModel.disabledPrepareAccessibilityHint == PaperOperationsCopy.unreconciled)
        }
    }

    @Test
    func emptyObjectSnapshotPayloadIsMalformedAndKeepsLastSnapshotFields() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true)
            )
            await viewModel.startLocalReplay()
            #expect(viewModel.snapshot?.bid == "99.02")
            #expect(viewModel.lastEvidence?.ask == "101.02")
            PaperOperationsURLProtocol.reset()
            PaperOperationsURLProtocol.overrideSnapshotPayload = Data(#"{}"#.utf8)

            await viewModel.loadSnapshotEvidence()

            #expect(viewModel.blockingReason?.kind == .malformed)
            #expect(viewModel.blockingReason?.copy == PaperOperationsCopy.malformed)
            #expect(viewModel.canPrepare == false)
            #expect(viewModel.snapshot?.bid == "99.02")
            #expect(viewModel.snapshot?.ask == "101.02")
            #expect(viewModel.lastEvidence?.bid == "99.02")
            #expect(viewModel.lastEvidence?.ask == "101.02")
        }
    }

    @Test
    func canPrepareIsTrueWithoutRegimeSimulatorOrSwarmAndSlotSaysEvidenceComplete() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true)
            )

            await viewModel.startLocalReplay()

            #expect(viewModel.snapshot?.source == "local-replay")
            #expect(viewModel.regimeId == nil)
            #expect(viewModel.modelVersion == nil)
            #expect(viewModel.simulatorFillPrice == nil)
            #expect(viewModel.simulatorDecision == nil)
            #expect(viewModel.swarmRiskQuantity == nil)
            #expect(viewModel.swarmReasonCode == nil)
            #expect(viewModel.canPrepare == true)
            #expect(viewModel.blockingReason == nil)
            #expect(viewModel.blockingSlotCopy == PaperOperationsCopy.evidenceComplete)
            #expect(viewModel.disabledPrepareAccessibilityHint.isEmpty)
        }
    }

    @Test
    func quantityLongerThan32KeepsCanPrepareFalse() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true)
            )
            await viewModel.startLocalReplay()
            #expect(viewModel.canPrepare == true)

            viewModel.quantity = String(repeating: "1", count: 33)

            #expect(viewModel.canPrepare == false)
        }
    }

    @Test
    func concurrentInFlightIgnoresSecondSessionAction() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true)
            )
            viewModel.inFlightAction = .refresh
            await viewModel.startLocalReplay()
            await viewModel.stopLocalReplay()
            await viewModel.refreshSessionStatus()
            await viewModel.loadSnapshotEvidence()

            #expect(PaperOperationsURLProtocol.snapshotRecord().urls.isEmpty)
        }
    }

    @Test
    func loadSnapshotEvidenceWhenStoppedDoesNotFetch() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true)
            )

            await viewModel.loadSnapshotEvidence()

            #expect(PaperOperationsURLProtocol.snapshotRecord().urls.isEmpty)
        }
    }
}

final class StubPaperApprovalSigner: PaperApprovalSigning {
    var isConfigured: Bool
    private(set) var identityCallCount = 0
    private(set) var signCallCount = 0

    init(configured: Bool) {
        isConfigured = configured
    }

    func identity() throws -> ApprovalSignerIdentity {
        identityCallCount += 1
        return ApprovalSignerIdentity(keyID: "test-key", publicKeyX963: Data())
    }

    func sign(_ payload: Data) throws -> Data {
        signCallCount += 1
        return Data()
    }
}
