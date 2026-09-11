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

    @Test
    func preparePaperIntentIsNoOpWhenCanPrepareIsFalse() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let approver = StubPaperTradeApprover()
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: false),
                tradeApprover: approver
            )

            await viewModel.preparePaperIntent()

            #expect(PaperOperationsURLProtocol.snapshotRecord().urls.isEmpty)
            #expect(viewModel.pendingTradeApproval == nil)
            #expect(approver.requestCallCount == 0)
        }
    }

    @Test
    func prepare201DeniedDoesNotOpenSheetAndKeepsLastEvidence() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let approver = StubPaperTradeApprover()
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true),
                tradeApprover: approver
            )
            await viewModel.startLocalReplay()
            #expect(viewModel.canPrepare == true)
            let keptBid = viewModel.lastEvidence?.bid
            PaperOperationsURLProtocol.reset()
            PaperOperationsURLProtocol.overridePreparePayload = Data(
                #"{"proposal_id":"paper-denied-1","state":"DENIED","admission":{"decision":"DENIED","reason_code":"SPREAD_TOO_WIDE","simulator_fill_price":"100.51","simulator_drawdown_pct":"0.01","risk_quantity":"1","current_spread_pct":"0.09"}}"#.utf8
            )

            await viewModel.preparePaperIntent()

            let record = PaperOperationsURLProtocol.snapshotRecord()
            #expect(record.urls.map(\.path) == ["/api/market-data/paper-preparations"])
            #expect(record.urls.allSatisfy { !$0.absoluteString.contains("/api/ai/trade/approve") })
            #expect(viewModel.pendingTradeApproval == nil)
            #expect(approver.requestCallCount == 0)
            #expect(viewModel.blockingReason?.kind == .admissionDenied)
            #expect(viewModel.blockingReason?.copy == PaperOperationsCopy.admissionDenied(reasonCode: "SPREAD_TOO_WIDE"))
            #expect(viewModel.canPrepare == false)
            #expect(viewModel.lastEvidence?.bid == keptBid)
        }
    }

    @Test
    func prepare201AdmittedOpensSheetWithExecutionTicker() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let approver = StubPaperTradeApprover()
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true),
                tradeApprover: approver
            )
            await viewModel.startLocalReplay()
            PaperOperationsURLProtocol.reset()
            PaperOperationsURLProtocol.overridePreparePayload = Data(
                #"{"proposal_id":"paper-admitted-1","state":"PENDING","admission":{"decision":"ADMITTED","reason_code":"ADMITTED","simulator_fill_price":"100.51","simulator_drawdown_pct":"0.01","risk_quantity":"1","current_spread_pct":"0.01"},"regime":{"regime_id":1,"model_version":"gmm-v1","observed_at":"2026-09-11T18:37:05Z","source_snapshot_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}"#.utf8
            )

            await viewModel.preparePaperIntent()

            let record = PaperOperationsURLProtocol.snapshotRecord()
            #expect(record.urls.map(\.path) == ["/api/market-data/paper-preparations"])
            #expect(record.urls.allSatisfy { !$0.absoluteString.contains("/api/ai/trade/approve") })
            #expect(approver.requestCallCount == 1)
            #expect(approver.lastProposal?.proposalId == "paper-admitted-1")
            #expect(approver.lastProposal?.ticker == "NSE:CASH:RELIANCE")
            #expect(approver.lastProposal?.action == "BUY")
            #expect(approver.lastProposal?.quantity == Decimal(string: "1"))
            #expect(viewModel.pendingTradeApproval != nil)
            #expect(viewModel.simulatorFillPrice == "100.51")
            #expect(viewModel.swarmRiskQuantity == "1")
            #expect(viewModel.regimeId == "1")
            #expect(viewModel.modelVersion == "gmm-v1")
            #expect(viewModel.blockingReason?.kind != .admissionDenied)
        }
    }

    @Test
    func prepare409DeniedKeepsPrepareDisabledWithFailClosedCopy() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true),
                tradeApprover: StubPaperTradeApprover()
            )
            await viewModel.startLocalReplay()
            #expect(viewModel.canPrepare == true)
            PaperOperationsURLProtocol.reset()
            PaperOperationsURLProtocol.overridePrepareStatus = 409
            PaperOperationsURLProtocol.overridePreparePayload = Data(
                #"{"detail":{"code":"PAPER_PREPARATION_DENIED","message":"workspace is not india"}}"#.utf8
            )

            await viewModel.preparePaperIntent()

            #expect(viewModel.pendingTradeApproval == nil)
            #expect(viewModel.canPrepare == false)
            #expect(viewModel.blockingReason?.kind == .rejectedAfterPrepare)
            #expect(viewModel.blockingReason?.copy == PaperOperationsCopy.rejectedAfterPrepare(reasonCode: "PAPER_PREPARATION_DENIED"))
        }
    }

    @Test
    func prepareTransportFailureUsesPrepareFailedCopyAndDoesNotOpenSheet() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true),
                tradeApprover: StubPaperTradeApprover()
            )
            await viewModel.startLocalReplay()
            PaperOperationsURLProtocol.reset()
            PaperOperationsURLProtocol.overridePrepareTransportFailure = true

            await viewModel.preparePaperIntent()

            #expect(viewModel.pendingTradeApproval == nil)
            #expect(viewModel.prepareFailedMessage == PaperOperationsCopy.prepareFailed)
        }
    }

    @Test
    func approvalCompletionResponseDecodesOptionalExecutionDetailsWithSnakeCase() throws {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let withAck = try decoder.decode(
            ApprovalCompletionResponse.self,
            from: Data(#"{"message":"Paper trade acknowledged by local-paper.","execution_details":{"proposal_id":"paper-admitted-1","broker":"local-paper","broker_order_id":"bo-1","status":"ACKNOWLEDGED","raw":{},"idempotent_replay":false}}"#.utf8)
        )
        #expect(withAck.message == "Paper trade acknowledged by local-paper.")
        #expect(withAck.executionDetails?.proposalId == "paper-admitted-1")
        #expect(withAck.executionDetails?.broker == "local-paper")
        #expect(withAck.executionDetails?.brokerOrderId == "bo-1")
        #expect(withAck.executionDetails?.status == "ACKNOWLEDGED")

        let withoutAck = try decoder.decode(
            ApprovalCompletionResponse.self,
            from: Data(#"{"message":"ok"}"#.utf8)
        )
        #expect(withoutAck.executionDetails == nil)
    }

    @Test
    func signedCompleteStoresOrderAckAndAcknowledgeDoesNotCallCompleteAgain() async throws {
        try await PaperOperationsHTTPIsolation.shared.run {
            let approver = StubPaperTradeApprover()
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true),
                tradeApprover: approver
            )
            let review = try await admittedReview(viewModel: viewModel)
            PaperOperationsURLProtocol.reset()

            try await viewModel.completeTradeApproval(review)

            #expect(approver.completeCallCount == 1)
            #expect(viewModel.lifecycleStep == .signed)
            #expect(viewModel.lastOrderAck?.proposalId == "paper-admitted-1")
            #expect(viewModel.lastOrderAck?.brokerOrderId == "bo-1")
            #expect(viewModel.pendingTradeApproval == nil)
            #expect(viewModel.canPrepare == false)
            #expect(viewModel.unreconciledIntent == true)

            viewModel.acknowledgeLocalFill()

            #expect(approver.completeCallCount == 1)
            #expect(viewModel.lifecycleStep == .acknowledged)
            #expect(PaperOperationsURLProtocol.snapshotRecord().urls.isEmpty)
            #expect(PaperOperationsURLProtocol.snapshotRecord().urls.allSatisfy {
                !$0.absoluteString.contains("/api/ai/trade/approval/complete")
            })
        }
    }

    @Test
    func acknowledgeWithoutStoredAckUsesFailedCopyAndDoesNotAdvance() async throws {
        try await PaperOperationsHTTPIsolation.shared.run {
            let approver = StubPaperTradeApprover()
            approver.completeResponse = ApprovalCompletionResponse(
                message: "Paper trade acknowledged by local-paper.",
                executionDetails: nil
            )
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true),
                tradeApprover: approver
            )
            let review = try await admittedReview(viewModel: viewModel)

            try await viewModel.completeTradeApproval(review)

            #expect(viewModel.lifecycleStep == .signed)
            #expect(viewModel.lastOrderAck == nil)
            #expect(viewModel.lifecycleStep != .acknowledged)

            viewModel.acknowledgeLocalFill()

            #expect(approver.completeCallCount == 1)
            #expect(viewModel.acknowledgeFailedMessage == PaperOperationsCopy.acknowledgeFailed)
            #expect(viewModel.lifecycleStep != .acknowledged)
        }
    }

    @Test
    func reconcilePaperOutcomeIsNoOpBeforeAcknowledgement() async throws {
        try await PaperOperationsHTTPIsolation.shared.run {
            let approver = StubPaperTradeApprover()
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true),
                tradeApprover: approver
            )
            let review = try await admittedReview(viewModel: viewModel)
            try await viewModel.completeTradeApproval(review)
            PaperOperationsURLProtocol.reset()

            await viewModel.reconcilePaperOutcome()

            #expect(PaperOperationsURLProtocol.snapshotRecord().urls.isEmpty)
            #expect(viewModel.lifecycleStep == .signed)
            #expect(viewModel.canPrepare == false)
        }
    }

    @Test
    func reconcileAfterAcknowledgePostsLoopbackConfirmationAndUnblocksPrepare() async throws {
        try await PaperOperationsHTTPIsolation.shared.run {
            let approver = StubPaperTradeApprover()
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true),
                tradeApprover: approver
            )
            let review = try await admittedReview(viewModel: viewModel)
            try await viewModel.completeTradeApproval(review)
            viewModel.acknowledgeLocalFill()
            #expect(viewModel.canPrepare == false)
            #expect(viewModel.blockingReason?.kind == .unreconciled)
            PaperOperationsURLProtocol.reset()

            await viewModel.reconcilePaperOutcome()

            let record = PaperOperationsURLProtocol.snapshotRecord()
            #expect(record.urls.map(\.path) == ["/api/market-data/paper-reconciliations"])
            #expect(record.methods == ["POST"])
            let body = try #require(record.bodies.first)
            let object = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
            #expect(Set(object.keys) == Set(["confirmation", "proposal_id"]))
            #expect(object["confirmation"] as? String == "RECONCILE_INDIA_PAPER")
            #expect(object["proposal_id"] as? String == "paper-admitted-1")
            #expect(object["broker"] == nil)
            #expect(viewModel.lifecycleStep == .reconciled)
            #expect(viewModel.unreconciledIntent == false)
            #expect(viewModel.canPrepare == true)
        }
    }

    @Test
    func reconcileFailureKeepsUnreconciledCopyAndDoesNotInventAFill() async throws {
        try await PaperOperationsHTTPIsolation.shared.run {
            let approver = StubPaperTradeApprover()
            let viewModel = PaperOperationsViewModel(
                client: makeClient(),
                signer: StubPaperApprovalSigner(configured: true),
                tradeApprover: approver
            )
            let review = try await admittedReview(viewModel: viewModel)
            try await viewModel.completeTradeApproval(review)
            viewModel.acknowledgeLocalFill()
            PaperOperationsURLProtocol.reset()
            PaperOperationsURLProtocol.overrideReconcileStatus = 500
            PaperOperationsURLProtocol.overrideReconcilePayload = Data(#"{"detail":"loopback failed"}"#.utf8)

            await viewModel.reconcilePaperOutcome()

            #expect(viewModel.lifecycleStep == .acknowledged)
            #expect(viewModel.unreconciledIntent == true)
            #expect(viewModel.canPrepare == false)
            #expect(viewModel.reconcileFailedMessage == PaperOperationsCopy.reconcileFailed)
            #expect(viewModel.lastOrderAck?.brokerOrderId == "bo-1")
        }
    }

    @MainActor
    private func admittedReview(viewModel: PaperOperationsViewModel) async throws -> TradeApprovalReview {
        await viewModel.startLocalReplay()
        PaperOperationsURLProtocol.reset()
        PaperOperationsURLProtocol.overridePreparePayload = Data(
            #"{"proposal_id":"paper-admitted-1","state":"PENDING","admission":{"decision":"ADMITTED","reason_code":"ADMITTED","simulator_fill_price":"100.51","simulator_drawdown_pct":"0.01","risk_quantity":"1","current_spread_pct":"0.01"}}"#.utf8
        )
        await viewModel.preparePaperIntent()
        return try #require(viewModel.pendingTradeApproval)
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

final class StubPaperTradeApprover: PaperTradeApproving {
    private(set) var requestCallCount = 0
    private(set) var completeCallCount = 0
    private(set) var lastProposal: TradeProposalData?
    var completeResponse = ApprovalCompletionResponse(
        message: "Paper trade acknowledged by local-paper.",
        executionDetails: PaperExecutionAck(
            proposalId: "paper-admitted-1",
            broker: "local-paper",
            brokerOrderId: "bo-1",
            status: "ACKNOWLEDGED"
        )
    )

    func requestTradeApproval(proposal: TradeProposalData) async throws -> TradeApprovalReview {
        requestCallCount += 1
        lastProposal = proposal
        return TradeApprovalReview.testingPlaceholder(proposal: proposal)
    }

    func completeTradeApproval(_ review: TradeApprovalReview, signature: Data) async throws -> ApprovalCompletionResponse {
        completeCallCount += 1
        return completeResponse
    }
}
