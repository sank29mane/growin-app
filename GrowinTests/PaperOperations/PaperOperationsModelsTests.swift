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
}
