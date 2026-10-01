import Foundation
import Testing
@testable import Growin

enum PaperOperationsSourceProbe {
    static func contents(_ relativePath: String) throws -> String {
        let testsFile = URL(fileURLWithPath: #filePath)
        let repoRoot = testsFile
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        return try String(contentsOf: repoRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }
}

actor PaperOperationsHTTPIsolation {
    static let shared = PaperOperationsHTTPIsolation()
    private var occupied = false

    func run<T: Sendable>(
        _ operation: @MainActor @Sendable () async throws -> T
    ) async rethrows -> T {
        while occupied {
            await Task.yield()
        }
        occupied = true
        defer { occupied = false }
        return try await operation()
    }
}

final class PaperOperationsURLProtocol: URLProtocol {
    private static let lock = NSLock()
    nonisolated(unsafe) static var recordedURLs: [URL] = []
    nonisolated(unsafe) static var recordedMethods: [String] = []
    nonisolated(unsafe) static var recordedBodies: [Data] = []
    nonisolated(unsafe) static var overrideStartPayload: Data?
    nonisolated(unsafe) static var overrideCurrentPayload: Data?
    nonisolated(unsafe) static var overrideSnapshotStatus = 200
    nonisolated(unsafe) static var overrideSnapshotPayload: Data?
    nonisolated(unsafe) static var overridePrepareStatus = 201
    nonisolated(unsafe) static var overridePreparePayload: Data?
    nonisolated(unsafe) static var overridePrepareTransportFailure = false
    nonisolated(unsafe) static var overrideReconcileStatus = 200
    nonisolated(unsafe) static var overrideReconcilePayload: Data?

    static let allowlistPrefixes: [String] = [
        "/api/market-data/sessions",
        "/api/market-data/sessions/current",
        "/api/market-data/snapshots/",
        "/api/market-data/paper-preparations",
        "/api/market-data/paper-reconciliations",
    ]

    static func reset() {
        lock.lock()
        recordedURLs = []
        recordedMethods = []
        recordedBodies = []
        overrideStartPayload = nil
        overrideCurrentPayload = nil
        overrideSnapshotStatus = 200
        overrideSnapshotPayload = nil
        overridePrepareStatus = 201
        overridePreparePayload = nil
        overridePrepareTransportFailure = false
        overrideReconcileStatus = 200
        overrideReconcilePayload = nil
        lock.unlock()
    }

    static func snapshotRecord() -> (urls: [URL], methods: [String], bodies: [Data]) {
        lock.lock()
        defer { lock.unlock() }
        return (recordedURLs, recordedMethods, recordedBodies)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.lock.lock()
        if let url = request.url {
            Self.recordedURLs.append(url)
        }
        Self.recordedMethods.append(request.httpMethod ?? "")
        if let body = Self.body(from: request) {
            Self.recordedBodies.append(body)
        }
        Self.lock.unlock()

        let path = request.url?.path ?? ""
        let payload: Data
        let status: Int
        if path.hasSuffix("/paper-preparations") {
            if Self.overridePrepareTransportFailure {
                client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
                return
            }
            status = Self.overridePrepareStatus
            payload = Self.overridePreparePayload ?? Data(#"{"proposal_id":"p1","state":"DENIED","admission":{"decision":"DENIED","reason_code":"SPREAD_TOO_WIDE","ticker":"NSE:CASH:RELIANCE","side":"BUY"}}"#.utf8)
        } else if path.hasSuffix("/paper-reconciliations") {
            status = Self.overrideReconcileStatus
            payload = Self.overrideReconcilePayload ?? Data(#"{}"#.utf8)
        } else if path.contains("/snapshots/") {
            status = Self.overrideSnapshotStatus
            payload = Self.overrideSnapshotPayload ?? Data(#"{"instrument":{"workspace":"india","venue":"NSE","segment":"CASH","symbol":"RELIANCE","currency":"INR"},"source":"local-replay","bid":"99.02","ask":"101.02","quote_observed_at":"2026-09-11T18:37:05Z","quote_received_at":"2026-09-11T18:37:05Z","quote_sequence":3,"last_trade_price":"100","snapshot_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}"#.utf8)
        } else if request.httpMethod == "POST" {
            status = 201
            payload = Self.overrideStartPayload ?? Data(#"{"state":"RUNNING","provider":"local-replay","instruments":[{"workspace":"india","venue":"NSE","segment":"CASH","symbol":"RELIANCE","currency":"INR"}],"read_only":true}"#.utf8)
        } else if path.hasSuffix("/sessions/current"), let override = Self.overrideCurrentPayload {
            status = 200
            payload = override
        } else {
            status = 200
            payload = Data(#"{"state":"STOPPED","provider":null,"instruments":[],"read_only":true}"#.utf8)
        }

        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: status,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: payload)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private static func body(from request: URLRequest) -> Data? {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        let bufferSize = 1024
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufferSize)
        defer { buffer.deallocate() }
        while stream.hasBytesAvailable {
            let read = stream.read(buffer, maxLength: bufferSize)
            if read <= 0 { break }
            data.append(buffer, count: read)
        }
        return data.isEmpty ? nil : data
    }
}

struct PaperOperationsClientTests {
    static func makeTestSession() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [PaperOperationsURLProtocol.self]
        return URLSession(configuration: config)
    }

    private func makeClient() -> PaperOperationsClient {
        PaperOperationsURLProtocol.reset()
        return PaperOperationsClient(
            session: Self.makeTestSession(),
            baseURL: URL(string: "http://127.0.0.1:8002")!
        )
    }

    @Test
    func startReplayPostsConfirmationLiteralAndRelianceFixture() async throws {
        try await PaperOperationsHTTPIsolation.shared.run {
            let client = makeClient()
            _ = try await client.startReplay()

            let record = PaperOperationsURLProtocol.snapshotRecord()
            #expect(record.methods.contains("POST"))
            let startURL = try #require(record.urls.first { $0.path == "/api/market-data/sessions" })
            #expect(startURL.path == "/api/market-data/sessions")

            let body = try #require(record.bodies.first)
            let object = try JSONSerialization.jsonObject(with: body) as? [String: Any]
            #expect(object?["confirmation"] as? String == "START_READ_ONLY_REPLAY")
            #expect(object?["provider"] as? String == "local-replay")
            let instruments = try #require(object?["instruments"] as? [[String: Any]])
            #expect(instruments.first?["symbol"] as? String == "RELIANCE")
            #expect(instruments.first?["workspace"] as? String == "india")
            #expect(instruments.first?["venue"] as? String == "NSE")
            #expect(instruments.first?["segment"] as? String == "CASH")
            #expect(instruments.first?["currency"] as? String == "INR")
        }
    }

    @Test
    func recordedURLsStayInsideMarketDataAllowlist() async throws {
        try await PaperOperationsHTTPIsolation.shared.run {
            let client = makeClient()
            _ = try await client.startReplay()
            _ = try await client.refreshStatus()
            _ = try await client.loadSnapshot(symbol: "RELIANCE")
            _ = try await client.prepare(symbol: "RELIANCE", quantity: "1")
            _ = try await client.reconcile(proposalId: "p1")
            _ = try await client.stopReplay()

            let record = PaperOperationsURLProtocol.snapshotRecord()
            #expect(!record.urls.isEmpty)
            for url in record.urls {
                let allowed = PaperOperationsURLProtocol.allowlistPrefixes.contains { prefix in
                    url.path == prefix || url.path.hasPrefix(prefix)
                }
                #expect(allowed, "recorded URL escaped allowlist: \(url.absoluteString)")
                #expect(!url.path.contains("breeze"))
                #expect(!url.path.contains("trading212"))
                #expect(!url.path.contains("mcp"))
                #expect(!url.absoluteString.contains("/api/ai/trade/approve"))
                #expect(!url.absoluteString.contains("/api/system/status"))
            }
        }
    }

    @Test
    func sessionAndPrepareBodiesOmitBrokerModeURLAndAPIKey() async throws {
        try await PaperOperationsHTTPIsolation.shared.run {
            let client = makeClient()
            let sessionBody = try client.encodeSessionStartBody()
            let prepareBody = try client.encodePrepareBody(symbol: "RELIANCE", quantity: "1")

            for body in [sessionBody, prepareBody] {
                let object = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
                #expect(object["broker"] == nil)
                #expect(object["mode"] == nil)
                #expect(object["url"] == nil)
                #expect(object["api_key"] == nil)
            }
        }
    }

    @Test
    func stopSessionDeletesCurrentAndDoesNotGetSnapshots() async throws {
        try await PaperOperationsHTTPIsolation.shared.run {
            let client = makeClient()
            _ = try await client.stopSession()

            let record = PaperOperationsURLProtocol.snapshotRecord()
            #expect(record.methods == ["DELETE"])
            #expect(record.urls.map(\.path) == ["/api/market-data/sessions/current"])
            #expect(record.urls.allSatisfy { !$0.path.contains("/snapshots/") })
        }
    }

    @Test
    func currentSessionGetsSessionsCurrent() async throws {
        try await PaperOperationsHTTPIsolation.shared.run {
            let client = makeClient()
            _ = try await client.currentSession()

            let record = PaperOperationsURLProtocol.snapshotRecord()
            #expect(record.methods == ["GET"])
            #expect(record.urls.map(\.path) == ["/api/market-data/sessions/current"])
        }
    }

    @Test
    func snapshot409StaleSnapshotMapsToTypedClientError() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let client = makeClient()
            PaperOperationsURLProtocol.overrideSnapshotStatus = 409
            PaperOperationsURLProtocol.overrideSnapshotPayload = Data(
                #"{"detail":{"code":"STALE_SNAPSHOT","message":"top-of-book snapshot is stale"}}"#.utf8
            )

            do {
                _ = try await client.snapshot(symbol: "RELIANCE")
                Issue.record("expected staleSnapshot error")
            } catch PaperOperationsClientError.staleSnapshot {
                // expected
            } catch {
                Issue.record("unexpected error \(error)")
            }
        }
    }

    @Test
    func prepareIndiaPaperJSONHasOnlyConfirmationSymbolAndQuantity() async throws {
        try await PaperOperationsHTTPIsolation.shared.run {
            let client = makeClient()
            _ = try await client.prepareIndiaPaper(symbol: "RELIANCE", quantity: "1")

            let record = PaperOperationsURLProtocol.snapshotRecord()
            #expect(record.urls.map(\.path) == ["/api/market-data/paper-preparations"])
            #expect(record.methods == ["POST"])
            let body = try #require(record.bodies.first)
            let object = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
            #expect(Set(object.keys) == Set(["confirmation", "symbol", "quantity"]))
            #expect(object["confirmation"] as? String == "PREPARE_INDIA_PAPER")
            #expect(object["symbol"] as? String == "RELIANCE")
            #expect(object["quantity"] as? String == "1")
            #expect(object["workspace"] == nil)
        }
    }

    @Test
    func reconcileIndiaPaperJSONHasOnlyConfirmationAndProposalId() async throws {
        try await PaperOperationsHTTPIsolation.shared.run {
            let client = makeClient()
            _ = try await client.reconcileIndiaPaper(proposalId: "paper-admitted-1")

            let record = PaperOperationsURLProtocol.snapshotRecord()
            #expect(record.urls.map(\.path) == ["/api/market-data/paper-reconciliations"])
            #expect(record.methods == ["POST"])
            let body = try #require(record.bodies.first)
            let object = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
            #expect(Set(object.keys) == Set(["confirmation", "proposal_id"]))
            #expect(object["confirmation"] as? String == "RECONCILE_INDIA_PAPER")
            #expect(object["proposal_id"] as? String == "paper-admitted-1")
            #expect(object["broker"] == nil)
            #expect(object["mode"] == nil)
            #expect(object["url"] == nil)
            #expect(object["api_key"] == nil)
        }
    }

    @Test
    func forbiddenPathsThrowBeforeTheSessionFires() throws {
        PaperOperationsURLProtocol.reset()
        let forbidden = [
            "/api/system/status",
            "/mcp/trading212/anything",
            "/api/ai/trade/approve",
        ]
        for path in forbidden {
            do {
                _ = try PaperOperationsClient.makeAllowlistedURL(
                    baseURL: "http://127.0.0.1:8002",
                    path: path
                )
                Issue.record("expected disallowedPath for \(path)")
            } catch PaperOperationsClientError.disallowedPath(let refused) {
                #expect(refused == path)
            } catch {
                Issue.record("unexpected error \(error) for \(path)")
            }
        }
        #expect(PaperOperationsURLProtocol.snapshotRecord().urls.isEmpty)
        #expect(PaperOperationsURLProtocol.snapshotRecord().methods.isEmpty)
    }

    @Test
    func makeAllowlistedURLBuildsLoopbackMarketDataURL() throws {
        let url = try PaperOperationsClient.makeAllowlistedURL(
            baseURL: "http://127.0.0.1:8002/",
            path: "/api/market-data/sessions"
        )
        #expect(url.scheme == "http")
        #expect(url.host == "127.0.0.1")
        #expect(url.port == 8002)
        #expect(url.path == "/api/market-data/sessions")
    }

    @Test
    func paperOperationsClientSourceDoesNotReferenceBackendStatus() throws {
        let source = try PaperOperationsSourceProbe.contents("Growin/Services/PaperOperationsClient.swift")
        #expect(!source.contains("BackendStatusViewModel"))
        #expect(!source.contains("MarketClient"))
        #expect(source.contains("validatePath"))
        #expect(source.contains("makeAllowlistedURL"))
    }

    @Test
    func prepare409PaperPreparationDeniedMapsToTypedClientError() async {
        await PaperOperationsHTTPIsolation.shared.run {
            let client = makeClient()
            PaperOperationsURLProtocol.overridePrepareStatus = 409
            PaperOperationsURLProtocol.overridePreparePayload = Data(
                #"{"detail":{"code":"PAPER_PREPARATION_DENIED","message":"workspace is not india"}}"#.utf8
            )

            do {
                _ = try await client.prepareIndiaPaper(symbol: "RELIANCE", quantity: "1")
                Issue.record("expected paperPreparationDenied error")
            } catch PaperOperationsClientError.paperPreparationDenied {
                // expected
            } catch {
                Issue.record("unexpected error \(error)")
            }
        }
    }
}
