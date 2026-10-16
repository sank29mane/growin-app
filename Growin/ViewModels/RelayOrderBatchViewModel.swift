import Foundation
import LocalAuthentication
import Observation

/// Why a batch stopped. `code` is the reason code to show: the VM's O6 code when
/// the server gave one (`capital_cap`, `kill_switch`, ...), otherwise a local one.
nonisolated struct RelayBatchFailure: LocalizedError, Equatable, Sendable {
    enum Stage: String, Equatable, Sendable {
        case challenge, review, signing, complete
    }

    let stage: Stage
    let status: Int
    let code: String
    let message: String

    var errorDescription: String? { "\(message) (\(code))" }
}

nonisolated struct RelayCompletionResponse: Decodable, Equatable, Sendable {
    let message: String?
}

/// The existing Mac approval routes, used for one proposal at a time.
@MainActor
protocol RelayApprovalServing {
    func requestChallenge(proposalId: String) async throws -> RelayApprovalChallenge
    func complete(proposalId: String, challengeId: String, signature: Data) async throws -> RelayCompletionResponse
}

/// POST /api/ai/trade/approval/challenge and /complete for the India workspace.
/// 63-05 makes the challenge route return the VM-minted bytes for SHADOW intents;
/// this client only needs the challenge id, proposal id and those bytes.
@MainActor
final class RelayApprovalClient: RelayApprovalServing {
    private let session: URLSession
    private let baseURL: String

    init(session: URLSession = .shared, baseURL: String = AppConfig.shared.baseURL) {
        self.session = session
        self.baseURL = baseURL
    }

    func requestChallenge(proposalId: String) async throws -> RelayApprovalChallenge {
        try await post(
            endpoint: "/api/ai/trade/approval/challenge",
            body: ["proposal_id": proposalId, "workspace": Workspace.india.rawValue],
            stage: .challenge
        )
    }

    func complete(proposalId: String, challengeId: String, signature: Data) async throws -> RelayCompletionResponse {
        try await post(
            endpoint: "/api/ai/trade/approval/complete",
            body: [
                "proposal_id": proposalId,
                "challenge_id": challengeId,
                "signature_der_b64": signature.base64EncodedString(),
                "workspace": Workspace.india.rawValue,
            ],
            stage: .complete
        )
    }

    private func post<Response: Decodable>(
        endpoint: String,
        body: [String: String],
        stage: RelayBatchFailure.Stage
    ) async throws -> Response {
        guard let url = URL(string: baseURL + endpoint) else {
            throw URLError(.badURL)
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(body)
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            throw Self.failure(stage: stage, status: status, body: data)
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(Response.self, from: data)
    }

    /// Finds a reason code in the common error shapes without trusting any of
    /// them: `{reason|code}`, `{detail: {reason|code}}`, `{error: {reason|code}}`,
    /// or `{detail: "text"}`.
    nonisolated static func failure(stage: RelayBatchFailure.Stage, status: Int, body: Data) -> RelayBatchFailure {
        let object = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
        func code(in dict: [String: Any]?) -> String? {
            guard let dict else { return nil }
            return (dict["reason"] as? String) ?? (dict["code"] as? String)
        }
        func message(in dict: [String: Any]?) -> String? {
            guard let dict else { return nil }
            return (dict["message"] as? String) ?? (dict["detail"] as? String)
        }
        let detail = object?["detail"] as? [String: Any]
        let error = object?["error"] as? [String: Any]
        let found = code(in: detail) ?? code(in: error) ?? code(in: object)
        let text = message(in: detail) ?? message(in: error) ?? message(in: object)
        return RelayBatchFailure(
            stage: stage,
            status: status,
            code: Self.sanitisedCode(found) ?? "http_\(status)",
            message: text.map { String($0.prefix(200)) } ?? "The server refused the request."
        )
    }

    nonisolated private static func sanitisedCode(_ raw: String?) -> String? {
        guard let raw, !raw.isEmpty, raw.count <= 64,
              raw.range(of: "^[A-Za-z0-9_.-]+\\z", options: .regularExpression) != nil else {
            return nil
        }
        return raw
    }
}

/// Walks a halve or flatten batch one order at a time (D-06). Per proposal: one
/// challenge, one review from the signed bytes, one Touch ID, one complete. The
/// first failure stops the batch. Nothing retries.
@Observable @MainActor
final class RelayOrderBatchViewModel {
    enum ItemState: Equatable {
        case queued
        case reviewing(RelayOrderReview)
        case signing(RelayOrderReview)
        case completed(RelayOrderReview, message: String?)
        case failed(RelayBatchFailure)
        case notAttempted
    }

    struct Item: Identifiable, Equatable {
        let proposalId: String
        var state: ItemState
        var id: String { proposalId }
    }

    typealias SignBytes = @MainActor (Data) async throws -> Data

    let batchId: String?
    private(set) var items: [Item]
    private(set) var failure: RelayBatchFailure?
    private(set) var isBusy = false

    private let client: any RelayApprovalServing
    private let signBytes: SignBytes
    private let clock: @MainActor () -> Date

    init(
        batchId: String?,
        proposalIds: [String],
        client: any RelayApprovalServing,
        signBytes: @escaping SignBytes = { bytes in
            try await ApprovalSignerRouter.shared.signAsync(bytes, for: .india, flow: .relayOrder)
        },
        clock: @escaping @MainActor () -> Date = { Date() }
    ) {
        self.batchId = batchId
        self.items = proposalIds.map { Item(proposalId: $0, state: .queued) }
        self.client = client
        self.signBytes = signBytes
        self.clock = clock
    }

    /// Index of the order the operator should act on next, if any.
    var currentIndex: Int? {
        guard failure == nil else { return nil }
        return items.firstIndex { item in
            switch item.state {
            case .queued, .reviewing, .signing: return true
            default: return false
            }
        }
    }

    var isFinished: Bool {
        failure != nil || items.allSatisfy { if case .completed = $0.state { return true } else { return false } }
    }

    var completedCount: Int {
        items.filter { if case .completed = $0.state { return true } else { return false } }.count
    }

    /// Asks for the next challenge and builds its review from the signed bytes.
    func prepareNext() async {
        guard !isBusy, failure == nil, let index = currentIndex, items[index].state == .queued else { return }
        isBusy = true
        defer { isBusy = false }
        let proposalId = items[index].proposalId
        do {
            let challenge = try await client.requestChallenge(proposalId: proposalId)
            let review = try RelayOrderReview(challenge: challenge, expectedProposalId: proposalId, now: clock())
            if let batchId, review.batchId != batchId {
                throw RelayOrderReviewError.batchMismatch
            }
            items[index].state = .reviewing(review)
        } catch {
            stop(at: index, with: Self.failure(from: error, stage: .review))
        }
    }

    /// One Touch ID, then the signature goes to `complete`. Called per order.
    func approveCurrent() async {
        guard !isBusy, failure == nil,
              let index = currentIndex,
              case .reviewing(let review) = items[index].state else { return }
        guard review.payload.expiresAt > Int(clock().timeIntervalSince1970) else {
            stop(at: index, with: Self.failure(from: RelayOrderReviewError.expired, stage: .review))
            return
        }
        isBusy = true
        defer { isBusy = false }
        items[index].state = .signing(review)
        let signature: Data
        do {
            signature = try await signBytes(review.signedBytes)
        } catch {
            stop(at: index, with: Self.failure(from: error, stage: .signing))
            return
        }
        do {
            let result = try await client.complete(
                proposalId: review.proposalId,
                challengeId: review.challengeId,
                signature: signature
            )
            items[index].state = .completed(review, message: result.message)
        } catch {
            stop(at: index, with: Self.failure(from: error, stage: .complete))
        }
    }

    private func stop(at index: Int, with failure: RelayBatchFailure) {
        self.failure = failure
        items[index].state = .failed(failure)
        for later in items.indices where later > index {
            if items[later].state == .queued { items[later].state = .notAttempted }
        }
    }

    static func failure(from error: Error, stage: RelayBatchFailure.Stage) -> RelayBatchFailure {
        if let failure = error as? RelayBatchFailure { return failure }
        if let review = error as? RelayOrderReviewError {
            let code: String
            switch review {
            case .invalidEnvelope: code = "invalid_envelope"
            case .notARelayOrder: code = "not_relay_order"
            case .challengeMismatch: code = "challenge_mismatch"
            case .proposalMismatch: code = "proposal_mismatch"
            case .batchMismatch: code = "batch_mismatch"
            case .expired: code = "challenge_expired"
            }
            return RelayBatchFailure(stage: stage, status: 0, code: code, message: review.localizedDescription)
        }
        if error is SignedPayloadInspectionError {
            return RelayBatchFailure(stage: stage, status: 0, code: "bytes_refused", message: error.localizedDescription)
        }
        if let signer = error as? ApprovalSignerError {
            if case .underlying(let inner) = signer, (inner as? LAError)?.code == .userCancel {
                return RelayBatchFailure(
                    stage: stage, status: 0, code: "touch_id_cancelled",
                    message: "Touch ID was cancelled. Nothing was sent."
                )
            }
            let code: String
            switch signer {
            case .keyMismatch: code = "key_mismatch"
            case .purposeNotAllowed: code = "purpose_not_allowed"
            case .workspaceMismatch: code = "workspace_mismatch"
            case .modeNotAllowed: code = "mode_not_allowed"
            case .notConfigured: code = "not_enrolled"
            default: code = "signing_failed"
            }
            return RelayBatchFailure(stage: stage, status: 0, code: code, message: signer.localizedDescription)
        }
        return RelayBatchFailure(stage: stage, status: 0, code: "request_failed", message: error.localizedDescription)
    }
}
