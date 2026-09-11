import Foundation

enum PaperOperationsClientError: Error, LocalizedError, Equatable {
    case disallowedPath(String)
    case badURL
    case invalidResponse
    case staleSnapshot
    case paperPreparationDenied
    case httpStatus(Int, String)

    var errorDescription: String? {
        switch self {
        case .disallowedPath(let path):
            return "Paper Operations refused a non-allowlisted path: \(path)"
        case .badURL:
            return "Paper Operations could not build a loopback URL."
        case .invalidResponse:
            return "Paper Operations received a non-HTTP response."
        case .staleSnapshot:
            return "STALE_SNAPSHOT"
        case .paperPreparationDenied:
            return "PAPER_PREPARATION_DENIED"
        case .httpStatus(let status, let detail):
            return "HTTP \(status): \(detail)"
        }
    }
}

struct PaperOperationsClient {
    static let allowlistPrefixes: [String] = [
        "/api/market-data/sessions",
        "/api/market-data/sessions/current",
        "/api/market-data/snapshots/",
        "/api/market-data/paper-preparations",
        "/api/market-data/paper-reconciliations",
    ]

    private let session: URLSession
    private let baseURLString: String

    init(
        session: URLSession? = nil,
        baseURL: String = AppConfig.shared.baseURL
    ) {
        self.session = session ?? URLSession(configuration: .ephemeral)
        if baseURL.hasSuffix("/") {
            self.baseURLString = String(baseURL.dropLast())
        } else {
            self.baseURLString = baseURL
        }
    }

    init(session: URLSession, baseURL: URL) {
        self.init(session: session, baseURL: baseURL.absoluteString)
    }

    func startSession() async throws -> PaperSessionStatus {
        let body = try encodeSessionStartBody()
        let data = try await perform(method: "POST", path: "/api/market-data/sessions", body: body)
        return try PaperOperationsModels.decodeSession(data)
    }

    func startReplay() async throws -> PaperSessionStatus {
        try await startSession()
    }

    func currentSession() async throws -> PaperSessionStatus {
        let data = try await perform(method: "GET", path: "/api/market-data/sessions/current")
        return try PaperOperationsModels.decodeSession(data)
    }

    func refreshStatus() async throws -> PaperSessionStatus {
        try await currentSession()
    }

    func snapshot(symbol: String) async throws -> PaperMarketSnapshot {
        let encoded = symbol.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? symbol
        let data = try await perform(method: "GET", path: "/api/market-data/snapshots/\(encoded)")
        return try PaperOperationsModels.decodeSnapshot(data)
    }

    func loadSnapshot(symbol: String) async throws -> PaperMarketSnapshot {
        try await snapshot(symbol: symbol)
    }

    func stopSession() async throws -> PaperSessionStatus {
        let data = try await perform(method: "DELETE", path: "/api/market-data/sessions/current")
        return try PaperOperationsModels.decodeSession(data)
    }

    func stopReplay() async throws -> PaperSessionStatus {
        try await stopSession()
    }

    func prepare(symbol: String, quantity: String) async throws -> PaperPrepareResponse {
        try await prepareIndiaPaper(symbol: symbol, quantity: quantity)
    }

    func prepareIndiaPaper(symbol: String, quantity: String) async throws -> PaperPrepareResponse {
        let body = try encodePrepareBody(symbol: symbol, quantity: quantity)
        let data = try await perform(method: "POST", path: "/api/market-data/paper-preparations", body: body)
        return try PaperOperationsModels.decodePrepareResponse(data)
    }

    func reconcile(proposalId: String) async throws -> Data {
        try await reconcileIndiaPaper(proposalId: proposalId)
    }

    func reconcileIndiaPaper(proposalId: String) async throws -> Data {
        let body = try encodeReconcileBody(proposalId: proposalId)
        return try await perform(method: "POST", path: "/api/market-data/paper-reconciliations", body: body)
    }

    func encodeSessionStartBody(now: Date = Date()) throws -> Data {
        let request = ReplaySessionStartRequest.relianceFixture(now: now)
        let data = try PaperOperationsModels.makeEncoder().encode(request)
        try Self.rejectForbiddenKeys(in: data)
        return data
    }

    func encodePrepareBody(symbol: String, quantity: String) throws -> Data {
        let request = PaperPrepareRequest(
            confirmation: "PREPARE_INDIA_PAPER",
            symbol: symbol,
            quantity: quantity
        )
        let data = try PaperOperationsModels.makeEncoder().encode(request)
        try Self.rejectForbiddenKeys(in: data)
        return data
    }

    func encodeReconcileBody(proposalId: String) throws -> Data {
        let request = PaperReconcileRequest(
            confirmation: "RECONCILE_INDIA_PAPER",
            proposalId: proposalId
        )
        let data = try PaperOperationsModels.makeEncoder().encode(request)
        try Self.rejectForbiddenKeys(in: data)
        return data
    }

    private func perform(method: String, path: String, body: Data? = nil) async throws -> Data {
        let url = try Self.makeAllowlistedURL(baseURL: baseURLString, path: path)
        var request = URLRequest(url: url)
        request.httpMethod = method
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw PaperOperationsClientError.invalidResponse
        }
        guard (200...299).contains(http.statusCode) else {
            throw Self.mapHTTPError(status: http.statusCode, data: data)
        }
        return data
    }

    static func mapHTTPError(status: Int, data: Data) -> PaperOperationsClientError {
        if status == 409, let code = errorCode(from: data) {
            if code == "STALE_SNAPSHOT" {
                return .staleSnapshot
            }
            if code == "PAPER_PREPARATION_DENIED" {
                return .paperPreparationDenied
            }
        }
        let detail = String(data: data, encoding: .utf8) ?? "Unknown Error"
        return .httpStatus(status, detail)
    }

    private static func errorCode(from data: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        if let detail = object["detail"] as? [String: Any], let code = detail["code"] as? String {
            return code
        }
        return object["code"] as? String
    }

    static func makeAllowlistedURL(baseURL: String, path: String) throws -> URL {
        try validatePath(path)
        let trimmed = baseURL.hasSuffix("/") ? String(baseURL.dropLast()) : baseURL
        guard let url = URL(string: trimmed + path) else {
            throw PaperOperationsClientError.badURL
        }
        return url
    }

    static func validatePath(_ path: String) throws {
        let allowed = allowlistPrefixes.contains { prefix in
            path == prefix || path.hasPrefix(prefix)
        }
        guard allowed else {
            throw PaperOperationsClientError.disallowedPath(path)
        }
    }

    private static func rejectForbiddenKeys(in data: Data) throws {
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return
        }
        for key in ["broker", "mode", "url", "api_key"] {
            if object[key] != nil {
                throw PaperOperationsClientError.disallowedPath(key)
            }
        }
    }
}

extension ReplaySessionStartRequest {
    static func relianceFixture(now: Date) -> ReplaySessionStartRequest {
        let instrument = IndiaInstrumentDTO.reliance
        let stamp = Self.isoStamp(now)
        let commonSource = "local-replay"
        let quotes: [(String, String, Int)] = [
            ("99", "101", 1),
            ("99.01", "101.01", 2),
            ("99.02", "101.02", 3),
        ]
        var events: [ReplayEventDTO] = quotes.map { bid, ask, sequence in
            ReplayEventDTO(
                source: commonSource,
                observedAt: stamp,
                receivedAt: stamp,
                instrument: instrument,
                kind: "quote",
                bid: bid,
                ask: ask,
                sequence: sequence,
                price: nil,
                quantity: nil
            )
        }
        events.append(
            ReplayEventDTO(
                source: commonSource,
                observedAt: stamp,
                receivedAt: stamp,
                instrument: instrument,
                kind: "trade",
                bid: nil,
                ask: nil,
                sequence: 4,
                price: "100",
                quantity: "1"
            )
        )
        return ReplaySessionStartRequest(
            provider: "local-replay",
            confirmation: "START_READ_ONLY_REPLAY",
            instruments: [instrument],
            events: events
        )
    }

    private static func isoStamp(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }
}
