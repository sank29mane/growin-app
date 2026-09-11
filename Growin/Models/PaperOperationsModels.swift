import Foundation

enum PaperOperationsModels {
    enum DecodeError: Error, Equatable {
        case malformedSnapshot
        case malformedSession
        case malformedPrepare
    }

    static func decodeSession(_ data: Data) throws -> PaperSessionStatus {
        do {
            return try makeDecoder().decode(PaperSessionStatus.self, from: data)
        } catch {
            throw DecodeError.malformedSession
        }
    }

    static func decodeSnapshot(_ data: Data) throws -> PaperMarketSnapshot {
        do {
            return try makeDecoder().decode(PaperMarketSnapshot.self, from: data)
        } catch {
            throw DecodeError.malformedSnapshot
        }
    }

    static func decodePrepareResponse(_ data: Data) throws -> PaperPrepareResponse {
        do {
            return try makeDecoder().decode(PaperPrepareResponse.self, from: data)
        } catch {
            throw DecodeError.malformedPrepare
        }
    }

    static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            if let date = fractionalISO8601.date(from: raw) {
                return date
            }
            if let date = internetISO8601.date(from: raw) {
                return date
            }
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Unreadable ISO-8601 date: \(raw)"
            )
        }
        return decoder
    }

    static func makeEncoder() -> JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }
}

private let fractionalISO8601: ISO8601DateFormatter = {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter
}()

private let internetISO8601: ISO8601DateFormatter = {
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime]
    return formatter
}()

struct IndiaInstrumentDTO: Codable, Equatable, Hashable, Sendable {
    var workspace: String
    var venue: String
    var segment: String
    var symbol: String
    var currency: String

    static let reliance = IndiaInstrumentDTO(
        workspace: "india",
        venue: "NSE",
        segment: "CASH",
        symbol: "RELIANCE",
        currency: "INR"
    )
}

struct PaperSessionStatus: Codable, Equatable, Sendable {
    var state: String
    var provider: String?
    var instruments: [IndiaInstrumentDTO]
    var readOnly: Bool

    static let stopped = PaperSessionStatus(
        state: "STOPPED",
        provider: nil,
        instruments: [],
        readOnly: true
    )
}

struct PaperMarketSnapshot: Decodable, Equatable, Sendable {
    var instrument: IndiaInstrumentDTO
    var source: String
    var bid: String
    var ask: String
    var quoteObservedAt: Date
    var quoteReceivedAt: Date?
    var quoteSequence: Int?
    var lastTradePrice: String?
    var snapshotId: String
}

struct PaperAdmission: Decodable, Equatable, Sendable {
    var decision: String
    var reasonCode: String

    var isAdmitted: Bool { decision == "ADMITTED" }
}

struct PaperPrepareResponse: Decodable, Equatable, Sendable {
    var proposalId: String
    var state: String
    var admission: PaperAdmission
}

struct ReplaySessionStartRequest: Encodable, Equatable {
    var provider: String
    var confirmation: String
    var instruments: [IndiaInstrumentDTO]
    var events: [ReplayEventDTO]
}

struct ReplayEventDTO: Encodable, Equatable {
    var source: String
    var observedAt: String
    var receivedAt: String
    var instrument: IndiaInstrumentDTO
    var kind: String
    var bid: String?
    var ask: String?
    var sequence: Int
    var price: String?
    var quantity: String?
}

struct PaperPrepareRequest: Encodable, Equatable {
    var confirmation: String
    var symbol: String
    var quantity: String
}

struct PaperReconcileRequest: Encodable, Equatable {
    var confirmation: String
    var proposalId: String
}

struct PaperOperationsEvidence: Equatable, Sendable {
    var snapshotSymbol: String
    var source: String
    var bid: String
    var ask: String
    var quoteObservedAt: String? = nil
    var snapshotId: String? = nil
    var regimeId: String? = nil
    var modelVersion: String? = nil
    var regimeObservedAt: String? = nil
    var sourceSnapshotId: String? = nil
    var simulatorFillPrice: String? = nil
    var simulatorDrawdownPct: String? = nil
    var simulatorDecision: String? = nil
    var swarmRiskQuantity: String? = nil
    var swarmSpreadPct: String? = nil
    var swarmReasonCode: String? = nil
    var rejectionReasons: [String] = []
}

enum PaperOperationsCopy {
    static let stopped = "Replay is stopped. Start Local Replay, then inspect evidence before preparing."
    static let emptyHeading = "Local replay is stopped"
    static let emptyBody = "Start a local India/NSE replay to inspect regime, simulator, risk, and admission evidence. Nothing is prepared until that evidence is visible and unblocked."
    static let startFailed = "Local replay could not start. Confirm the backend is reachable on loopback, then try Start Local Replay again."
    static let stopFailed = "Local replay could not stop. Try Stop Local Replay again. Do not assume the session is gone."
    static let statusFailed = "Session status could not be read. Try Refresh Session Status. Prepare stays disabled."
    static let startLocalReplay = "Start Local Replay"
    static let stopLocalReplay = "Stop Local Replay"
    static let refreshSessionStatus = "Refresh Session Status"
    static let loadSnapshotEvidence = "Load Snapshot Evidence"
    static let stopDialogTitle = "Stop local replay?"
    static let stopDialogBody = "Freshness evidence will go stale until you start again. Unreconciled paper intents stay visible."
    static let stopDialogConfirm = "Stop Local Replay"
    static let stopDialogDismiss = "Keep Replay Running"
    static let pickerEmpty = "No replay instruments"
    static let pickerPrompt = "Select a replay instrument"
    static let quantityLabel = "Quantity"
    static let heading = "PAPER OPERATIONS"
    static let chipStopped = "STOPPED"
    static let chipLive = "LIVE"

    static func snapshotFailed(symbol: String) -> String {
        "Snapshot could not be loaded for \(symbol). Select a subscribed instrument, then Load Snapshot Evidence."
    }
    static let prepareFailed = "Paper intent was not prepared. No broker was contacted. Fix the blocking reason, then try Prepare Paper Intent again."
    static let acknowledgeFailed = "Local fill was not acknowledged. The signed intent is unchanged. Try Acknowledge Local Fill again."
    static let reconcileFailed = "Paper outcome was not reconciled. Try Reconcile Paper Outcome again before preparing another intent."
    static let missingField = "Missing"
    static let rejectionReason = "Rejection reason"
    static let rejectionReasons = "Rejection reasons"
    static let cardRegime = "Regime"
    static let cardSimulator = "Simulator"
    static let cardSwarm = "Swarm/Risk"
    static let cardSnapshot = "Snapshot Freshness"
    static let subtitle = "LOCAL INDIA/NSE REPLAY // PAPER ONLY"
    static let modeStrip = "PAPER ONLY · LOCAL REPLAY · NO BROKER"
    static let evidenceComplete = "Evidence is complete. Prepare stays a separate explicit action."
    static let missingSnapshot = "Snapshot evidence is missing. Load Snapshot Evidence before preparing."
    static let staleSnapshot = "Snapshot evidence is stale. Refresh Session Status, then Load Snapshot Evidence."
    static let malformed = "The server returned unreadable evidence. Do not prepare. Refresh Session Status. If this repeats, Stop Local Replay and start again."
    static let signerMissing = "Local paper approval is not configured. Open System Settings, choose Set up local paper approvals, then return here."
    static let unreconciled = "This paper intent is unreconciled. Reconcile Paper Outcome before starting another prepare."

    static func admissionDenied(reasonCode: String) -> String {
        "Paper intent was denied: \(reasonCode). Inspect the evidence. Prepare stays disabled until a fresh admitted snapshot exists."
    }

    static func rejectedAfterPrepare(reasonCode: String) -> String {
        "Preparation was rejected: \(reasonCode). Last evidence stays visible. Prepare stays disabled."
    }

    static func truncatedHash(_ value: String) -> String {
        let prefix = String(value.prefix(16))
        if value.count <= 16 {
            return prefix
        }
        return prefix + "…"
    }
}

enum BlockingReason: Equatable, Sendable {
    enum Kind: Equatable, Sendable {
        case stopped
        case missingSnapshot
        case staleSnapshot
        case malformed
        case signerMissing
        case unreconciled
        case admissionDenied
        case rejectedAfterPrepare
    }

    case stopped
    case missingSnapshot
    case staleSnapshot
    case malformed
    case signerMissing
    case unreconciled
    case admissionDenied(reasonCode: String)
    case rejectedAfterPrepare(reasonCode: String)

    var kind: Kind {
        switch self {
        case .stopped: return .stopped
        case .missingSnapshot: return .missingSnapshot
        case .staleSnapshot: return .staleSnapshot
        case .malformed: return .malformed
        case .signerMissing: return .signerMissing
        case .unreconciled: return .unreconciled
        case .admissionDenied: return .admissionDenied
        case .rejectedAfterPrepare: return .rejectedAfterPrepare
        }
    }

    var copy: String {
        switch self {
        case .stopped:
            return PaperOperationsCopy.stopped
        case .missingSnapshot:
            return PaperOperationsCopy.missingSnapshot
        case .staleSnapshot:
            return PaperOperationsCopy.staleSnapshot
        case .malformed:
            return PaperOperationsCopy.malformed
        case .signerMissing:
            return PaperOperationsCopy.signerMissing
        case .unreconciled:
            return PaperOperationsCopy.unreconciled
        case .admissionDenied(let reasonCode):
            return PaperOperationsCopy.admissionDenied(reasonCode: reasonCode)
        case .rejectedAfterPrepare(let reasonCode):
            return PaperOperationsCopy.rejectedAfterPrepare(reasonCode: reasonCode)
        }
    }
}

protocol PaperApprovalSigning: AnyObject {
    var isConfigured: Bool { get }
    func identity() throws -> ApprovalSignerIdentity
    func sign(_ payload: Data) throws -> Data
}

final class LocalPaperApprovalSigner: PaperApprovalSigning {
    var isConfigured: Bool {
        LocalApprovalSigner.shared.isConfigured
    }

    func identity() throws -> ApprovalSignerIdentity {
        try LocalApprovalSigner.shared.identity()
    }

    func sign(_ payload: Data) throws -> Data {
        try LocalApprovalSigner.shared.sign(payload)
    }
}
