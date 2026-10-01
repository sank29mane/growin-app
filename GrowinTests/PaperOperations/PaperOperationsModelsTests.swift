import Foundation
import Testing
@testable import Growin

struct PaperOperationsModelsTests {
    @Test
    func stoppedSessionJSONDecodesToEmptyReadOnlySession() throws {
        let data = """
        {"state":"STOPPED","provider":null,"instruments":[],"read_only":true}
        """.data(using: .utf8)!

        let session = try PaperOperationsModels.decodeSession(data)

        #expect(session.state == "STOPPED")
        #expect(session.provider == nil)
        #expect(session.instruments.isEmpty)
        #expect(session.readOnly == true)
    }

    @Test
    func http201DeniedAdmissionDoesNotDecodeAsAdmitted() throws {
        let data = """
        {
          "proposal_id": "paper-denied-1",
          "state": "DENIED",
          "admission": {
            "decision": "DENIED",
            "reason_code": "SPREAD_TOO_WIDE",
            "simulator_fill_price": "100.51",
            "simulator_drawdown_pct": "0.01",
            "risk_quantity": "1",
            "current_spread_pct": "0.09",
            "ticker": "NSE:CASH:RELIANCE",
            "side": "BUY"
          }
        }
        """.data(using: .utf8)!

        let prepare = try PaperOperationsModels.decodePrepareResponse(data)

        #expect(prepare.admission.decision == "DENIED")
        #expect(prepare.admission.isAdmitted == false)
        #expect(prepare.admission.reasonCode == "SPREAD_TOO_WIDE")
    }

    @Test
    func missingRequiredSnapshotFieldsAreMalformedNotAFakeBid() throws {
        let data = """
        {
          "instrument": {
            "workspace": "india",
            "venue": "NSE",
            "segment": "CASH",
            "symbol": "RELIANCE",
            "currency": "INR"
          },
          "source": "local-replay",
          "ask": "101",
          "quote_observed_at": "2026-09-11T18:37:05.246289Z",
          "quote_received_at": "2026-09-11T18:37:05.246289Z",
          "quote_sequence": 3,
          "snapshot_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        }
        """.data(using: .utf8)!

        #expect(throws: PaperOperationsModels.DecodeError.malformedSnapshot) {
            try PaperOperationsModels.decodeSnapshot(data)
        }
    }

    @Test
    func emptyObjectSnapshotDataIsMalformedNotAFakeQuote() throws {
        #expect(throws: PaperOperationsModels.DecodeError.malformedSnapshot) {
            try PaperOperationsModels.decodeSnapshot(Data(#"{}"#.utf8))
        }
    }

    @Test
    func paperOperationsCopyMatchesUISpecEvidenceCompleteAndSignerMissing() {
        #expect(
            PaperOperationsCopy.evidenceComplete
                == "Evidence is complete. Prepare stays a separate explicit action."
        )
        #expect(
            PaperOperationsCopy.signerMissing
                == "Local paper approval is not configured. Open System Settings, choose Set up local paper approvals, then return here."
        )
        #expect(
            PaperOperationsCopy.stopped
                == "Replay is stopped. Start Local Replay, then inspect evidence before preparing."
        )
        #expect(
            PaperOperationsCopy.missingSnapshot
                == "Snapshot evidence is missing. Load Snapshot Evidence before preparing."
        )
        #expect(
            PaperOperationsCopy.staleSnapshot
                == "Snapshot evidence is stale. Refresh Session Status, then Load Snapshot Evidence."
        )
        #expect(
            PaperOperationsCopy.malformed
                == "The server returned unreadable evidence. Do not prepare. Refresh Session Status. If this repeats, Stop Local Replay and start again."
        )
        #expect(
            PaperOperationsCopy.unreconciled
                == "This paper intent is unreconciled. Reconcile Paper Outcome before starting another prepare."
        )
        #expect(
            PaperOperationsCopy.admissionDenied(reasonCode: "SPREAD_TOO_WIDE")
                == "Paper intent was denied: SPREAD_TOO_WIDE. Inspect the evidence. Prepare stays disabled until a fresh admitted snapshot exists."
        )
        #expect(
            PaperOperationsCopy.rejectedAfterPrepare(reasonCode: "SPREAD_TOO_WIDE")
                == "Preparation was rejected: SPREAD_TOO_WIDE. Last evidence stays visible. Prepare stays disabled."
        )
        #expect(
            PaperOperationsCopy.startFailed
                == "Local replay could not start. Confirm the backend is reachable on loopback, then try Start Local Replay again."
        )
        #expect(
            PaperOperationsCopy.stopFailed
                == "Local replay could not stop. Try Stop Local Replay again. Do not assume the session is gone."
        )
        #expect(
            PaperOperationsCopy.statusFailed
                == "Session status could not be read. Try Refresh Session Status. Prepare stays disabled."
        )
        #expect(
            PaperOperationsCopy.snapshotFailed(symbol: "RELIANCE")
                == "Snapshot could not be loaded for RELIANCE. Select a subscribed instrument, then Load Snapshot Evidence."
        )
        #expect(
            PaperOperationsCopy.prepareFailed
                == "Paper intent was not prepared. No broker was contacted. Fix the blocking reason, then try Prepare Paper Intent again."
        )
        #expect(
            PaperOperationsCopy.acknowledgeFailed
                == "Local fill was not acknowledged. The signed intent is unchanged. Try Acknowledge Local Fill again."
        )
        #expect(
            PaperOperationsCopy.reconcileFailed
                == "Paper outcome was not reconciled. Try Reconcile Paper Outcome again before preparing another intent."
        )
    }

    @Test
    func truncatedHashUsesSixteenCharactersPlusEllipsis() {
        let full = String(repeating: "b", count: 64)
        #expect(PaperOperationsCopy.truncatedHash(full) == String(repeating: "b", count: 16) + "…")
        #expect(PaperOperationsCopy.truncatedHash("abc") == "abc")
        #expect(PaperOperationsCopy.truncatedHash(String(repeating: "c", count: 16)) == String(repeating: "c", count: 16))
    }
}
