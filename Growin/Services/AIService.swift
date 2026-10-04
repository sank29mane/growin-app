import Foundation

@MainActor
class AIService {
    let baseURL = AppConfig.shared.baseURL
    private let session: URLSession

    /// The session default is an injection seam for tests, not an identity default.
    init(session: URLSession = .shared) {
        self.session = session
    }
    
    func streamStrategyEvents(ticker: String?) async throws -> AsyncThrowingStream<AgentEvent, Error> {
        var components = URLComponents(string: "\(baseURL)/api/ai/strategy/stream")!
        components.queryItems = [
            URLQueryItem(name: "session_id", value: UUID().uuidString),
            URLQueryItem(name: "ticker", value: ticker)
        ]
        
        let url = components.url!
        var request = URLRequest(url: url)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        
        let (bytes, response) = try await session.bytes(for: request)
        
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            throw NSError(domain: "AIService", code: 0, userInfo: [NSLocalizedDescriptionKey: "Failed to connect to strategy stream"])
        }
        
        return AsyncThrowingStream { continuation in
            let task = Task {
                var currentEvent: String?
                
                do {
                    for try await line in bytes.lines {
                        if line.isEmpty {
                            currentEvent = nil
                            continue
                        }
                        
                        let parsed = SSEParser.parseLine(line)
                        if let event = parsed.event {
                            currentEvent = event
                        } else if let data = parsed.data {
                            if let eventName = currentEvent, let eventData = data.data(using: .utf8) {
                                let decoder = JSONDecoder()
                                decoder.keyDecodingStrategy = .useDefaultKeys
                                
                                switch eventName {
                                case "status_update", "reasoning_step":
                                    if let agentEvent = try? decoder.decode(AgentEvent.self, from: eventData) {
                                        continuation.yield(agentEvent)
                                    }
                                case "final_result":
                                    // Handle final result separately or yield as a special AgentEvent
                                    if let agentEvent = try? decoder.decode(AgentEvent.self, from: eventData) {
                                        continuation.yield(agentEvent)
                                    }
                                case "error":
                                    continuation.finish(throwing: NSError(domain: "AIService", code: 1, userInfo: [NSLocalizedDescriptionKey: data]))
                                default:
                                    break
                                }
                            }
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }
    
    func fetchStrategy(id: String) async throws -> AIStrategy {
        let url = URL(string: "\(baseURL)/api/ai/strategy/\(id)")!
        let (data, _) = try await session.data(from: url)
        
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .useDefaultKeys
        return try decoder.decode(AIStrategy.self, from: data)
    }
    
    func challengeStrategy(id: String, challenge: String) async throws -> (newSessionId: String, message: String) {
        var components = URLComponents(string: "\(baseURL)/api/ai/strategy/\(id)/challenge")!
        components.queryItems = [URLQueryItem(name: "challenge", value: challenge)]
        
        var request = URLRequest(url: components.url!)
        request.httpMethod = "POST"
        
        let (data, _) = try await session.data(for: request)
        let response = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        
        return (
            response?["new_session_id"] as? String ?? "",
            response?["message"] as? String ?? ""
        )
    }

    // MARK: - Workspace-bound approval API (58-09)

    func enrollApprovalKey(identity: ApprovalSignerIdentity, token: String, workspace: Workspace) async throws -> String {
        let result: ApprovalEnrollmentResponse = try await postJSON(
            endpoint: "/api/ai/trade/approval/enroll",
            body: [
                "public_key_x963_b64": identity.publicKeyX963.base64EncodedString(),
                "enrollment_token": token,
                "workspace": workspace.rawValue,
            ]
        )
        guard result.keyId == identity.keyID else {
            throw TradeApprovalReviewError.signerMismatch
        }
        return result.keyId
    }

    func approvalStatus(workspace: Workspace) async throws -> ApprovalStatusResponse {
        guard var components = URLComponents(string: baseURL + "/api/ai/trade/approval/status") else {
            throw URLError(.badURL)
        }
        components.queryItems = [URLQueryItem(name: "workspace", value: workspace.rawValue)]
        guard let url = components.url else {
            throw URLError(.badURL)
        }
        let (data, response) = try await session.data(from: url)
        guard let http = response as? HTTPURLResponse else {
            throw NSError(domain: "AIService.SignedApproval", code: 0,
                          userInfo: [NSLocalizedDescriptionKey: "Approval status is unavailable."])
        }
        if http.statusCode == 409 {
            throw TradeApprovalReviewError.workspaceMismatch
        }
        guard (200...299).contains(http.statusCode) else {
            throw NSError(domain: "AIService.SignedApproval", code: 0,
                          userInfo: [NSLocalizedDescriptionKey: "Approval status is unavailable."])
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let status = try decoder.decode(ApprovalStatusResponse.self, from: data)
        // A null workspace means the backend has no ledger open; anything else
        // must be the workspace we asked about.
        if let responded = status.workspace, responded != workspace.rawValue {
            throw TradeApprovalReviewError.workspaceMismatch
        }
        return status
    }

    func createPaperApprovalCheck(workspace: Workspace) async throws -> TradeProposalData {
        try await createUATProposal(endpoint: "/api/ai/trade/approval/uat-proposal", workspace: workspace)
    }

    func createPaperRequoteCheck(workspace: Workspace) async throws -> TradeProposalData {
        try await createUATProposal(endpoint: "/api/ai/trade/requote/uat-proposal", workspace: workspace)
    }

    func verifyPaperRequoteCheck(_ review: TradeApprovalReview, signature: Data, workspace: Workspace) async throws -> String {
        guard review.payload.workspace == workspace.rawValue else {
            throw TradeApprovalReviewError.workspaceMismatch
        }
        let result: ApprovalCompletionResponse = try await postJSON(
            endpoint: "/api/ai/trade/requote/uat/verify",
            body: [
                "proposal_id": review.payload.proposalId,
                "challenge_id": review.payload.challengeId,
                "signature_der_b64": signature.base64EncodedString(),
                "workspace": workspace.rawValue,
            ]
        )
        return result.message
    }

    func requestTradeApproval(proposal: TradeProposalData, workspace: Workspace) async throws -> TradeApprovalReview {
        let challenge: ApprovalChallengeResponse = try await postJSON(
            endpoint: "/api/ai/trade/approval/challenge",
            body: ["proposal_id": proposal.proposalId, "workspace": workspace.rawValue]
        )
        return try TradeApprovalReview(challenge: challenge, expectedProposal: proposal, expectedWorkspace: workspace)
    }

    func completeTradeApproval(_ review: TradeApprovalReview, signature: Data, workspace: Workspace) async throws -> ApprovalCompletionResponse {
        guard review.payload.workspace == workspace.rawValue else {
            throw TradeApprovalReviewError.workspaceMismatch
        }
        return try await postJSON(
            endpoint: "/api/ai/trade/approval/complete",
            body: [
                "proposal_id": review.payload.proposalId,
                "challenge_id": review.payload.challengeId,
                "signature_der_b64": signature.base64EncodedString(),
                "workspace": workspace.rawValue,
            ]
        )
    }

    func approveTrade(id: String, workspace: Workspace) async throws -> String {
        let url = URL(string: "\(baseURL)/api/ai/trade/approve")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body = ["proposal_id": id, "decision": "APPROVED", "workspace": workspace.rawValue]
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, response) = try await session.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            let errorMsg = String(data: data, encoding: .utf8) ?? "Unknown Error"
            throw NSError(domain: "AIService", code: 2, userInfo: [NSLocalizedDescriptionKey: "Approval failed: \(errorMsg)"])
        }
        let result = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        return result?["message"] as? String ?? "Trade execution started."
    }

    func rejectTrade(id: String, notes: String? = nil, workspace: Workspace) async throws -> String {
        let url = URL(string: "\(baseURL)/api/ai/trade/reject")!
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        var body: [String: Any] = ["proposal_id": id, "decision": "REJECTED", "workspace": workspace.rawValue]
        if let notes = notes {
            body["notes"] = notes
        }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (data, response) = try await session.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 else {
            let errorMsg = String(data: data, encoding: .utf8) ?? "Unknown Error"
            throw NSError(domain: "AIService", code: 3, userInfo: [NSLocalizedDescriptionKey: "Rejection failed: \(errorMsg)"])
        }
        let result = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        return result?["message"] as? String ?? "Trade proposal rejected."
    }

    private func createUATProposal(endpoint: String, workspace: Workspace) async throws -> TradeProposalData {
        guard let url = URL(string: baseURL + endpoint) else {
            throw URLError(.badURL)
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(["workspace": workspace.rawValue])

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, (200...299).contains(http.statusCode) else {
            let detail = (try? JSONDecoder().decode(ApprovalAPIError.self, from: data).detail)
                ?? "The paper approval check could not be created."
            throw NSError(
                domain: "AIService.SignedApproval",
                code: (response as? HTTPURLResponse)?.statusCode ?? 0,
                userInfo: [NSLocalizedDescriptionKey: detail]
            )
        }
        // TradeProposalData supplies explicit snake_case CodingKeys. Do not use
        // convertFromSnakeCase here: it transforms the input before that mapping
        // and causes a false missing `proposal_id` decoding error.
        return try JSONDecoder().decode(TradeProposalData.self, from: data)
    }

    private func postJSON<Response: Decodable>(
        endpoint: String,
        body: [String: String]
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
            let detail = (try? JSONDecoder().decode(ApprovalAPIError.self, from: data).detail)
                ?? "The signed approval request failed."
            throw NSError(
                domain: "AIService.SignedApproval",
                code: (response as? HTTPURLResponse)?.statusCode ?? 0,
                userInfo: [NSLocalizedDescriptionKey: detail]
            )
        }
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return try decoder.decode(Response.self, from: data)
    }
}

private struct ApprovalEnrollmentResponse: Decodable {
    let keyId: String
}

struct ApprovalStatusResponse: Decodable {
    let mode: String
    let enrolled: Bool
    let keyId: String?
    let workspace: String?
}

struct PaperExecutionAck: Decodable, Equatable, Sendable {
    var proposalId: String
    var broker: String
    var brokerOrderId: String
    var status: String
}

struct ApprovalCompletionResponse: Decodable, Equatable, Sendable {
    var message: String
    var executionDetails: PaperExecutionAck?
}

private struct ApprovalAPIError: Decodable {
    let detail: String
}
