import Foundation
@testable import Growin

/// Pass-through kept so existing `await PaperOperationsHTTPIsolation.shared.run { ... }` call sites
/// need no re-indent. It used to serialise tests around process-wide static stub state; that state is
/// gone (every test owns a `PaperOperationsStub`), so nothing is serialised any more and a test that
/// shared state again would fail under the repeated parallel runs instead of hiding behind a lock.
struct PaperOperationsHTTPIsolation {
    static let shared = PaperOperationsHTTPIsolation()

    func run<T: Sendable>(
        _ operation: @MainActor @Sendable () async throws -> T
    ) async rethrows -> T {
        try await operation()
    }
}

/// Per-test HTTP stub. Each instance has its own overrides and request record, and its own
/// URLSession. The session stamps every request with the instance id, so the shared
/// `PaperOperationsURLProtocol` class can route a request to the stub that issued it. Two tests
/// running in parallel can therefore never reset, overwrite, or record into each other.
final class PaperOperationsStub: @unchecked Sendable {
    static let idHeader = "X-Paper-Stub-ID"

    private struct State {
        var recordedURLs: [URL] = []
        var recordedMethods: [String] = []
        var recordedBodies: [Data] = []
        var overrideStartPayload: Data?
        var overrideCurrentPayload: Data?
        var overrideSnapshotStatus = 200
        var overrideSnapshotPayload: Data?
        var overridePrepareStatus = 201
        var overridePreparePayload: Data?
        var overridePrepareTransportFailure = false
        var overrideReconcileStatus = 200
        var overrideReconcilePayload: Data?
    }

    let id = UUID().uuidString
    private let lock = NSLock()
    private var state = State()

    init() { PaperOperationsStubRegistry.shared.register(self) }
    deinit { PaperOperationsStubRegistry.shared.unregister(id: id) }

    private func locked<T>(_ body: (inout State) -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body(&state)
    }

    // MARK: Session and client

    func makeSession() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [PaperOperationsURLProtocol.self]
        config.httpAdditionalHeaders = [Self.idHeader: id]
        return URLSession(configuration: config)
    }

    func makeClient() -> PaperOperationsClient {
        PaperOperationsClient(session: makeSession(), baseURL: URL(string: "http://127.0.0.1:8002")!)
    }

    // MARK: Overrides

    var overrideStartPayload: Data? {
        get { locked { $0.overrideStartPayload } }
        set { locked { $0.overrideStartPayload = newValue } }
    }
    var overrideCurrentPayload: Data? {
        get { locked { $0.overrideCurrentPayload } }
        set { locked { $0.overrideCurrentPayload = newValue } }
    }
    var overrideSnapshotStatus: Int {
        get { locked { $0.overrideSnapshotStatus } }
        set { locked { $0.overrideSnapshotStatus = newValue } }
    }
    var overrideSnapshotPayload: Data? {
        get { locked { $0.overrideSnapshotPayload } }
        set { locked { $0.overrideSnapshotPayload = newValue } }
    }
    var overridePrepareStatus: Int {
        get { locked { $0.overridePrepareStatus } }
        set { locked { $0.overridePrepareStatus = newValue } }
    }
    var overridePreparePayload: Data? {
        get { locked { $0.overridePreparePayload } }
        set { locked { $0.overridePreparePayload = newValue } }
    }
    var overridePrepareTransportFailure: Bool {
        get { locked { $0.overridePrepareTransportFailure } }
        set { locked { $0.overridePrepareTransportFailure = newValue } }
    }
    var overrideReconcileStatus: Int {
        get { locked { $0.overrideReconcileStatus } }
        set { locked { $0.overrideReconcileStatus = newValue } }
    }
    var overrideReconcilePayload: Data? {
        get { locked { $0.overrideReconcilePayload } }
        set { locked { $0.overrideReconcilePayload = newValue } }
    }

    /// Clears this stub's overrides and request record. Touches no other stub.
    func reset() {
        locked { $0 = State() }
    }

    func snapshotRecord() -> (urls: [URL], methods: [String], bodies: [Data]) {
        locked { ($0.recordedURLs, $0.recordedMethods, $0.recordedBodies) }
    }

    // MARK: Responding

    enum Outcome {
        case response(status: Int, payload: Data)
        case transportFailure(URLError)
    }

    /// Records the request and chooses the canned response, atomically.
    func respond(to request: URLRequest, body: Data?) -> Outcome {
        locked { state in
            if let url = request.url { state.recordedURLs.append(url) }
            state.recordedMethods.append(request.httpMethod ?? "")
            if let body { state.recordedBodies.append(body) }

            let path = request.url?.path ?? ""
            if path.hasSuffix("/paper-preparations") {
                if state.overridePrepareTransportFailure {
                    return .transportFailure(URLError(.notConnectedToInternet))
                }
                return .response(
                    status: state.overridePrepareStatus,
                    payload: state.overridePreparePayload ?? Data(#"{"proposal_id":"p1","state":"DENIED","admission":{"decision":"DENIED","reason_code":"SPREAD_TOO_WIDE","ticker":"NSE:CASH:RELIANCE","side":"BUY"}}"#.utf8))
            } else if path.hasSuffix("/paper-reconciliations") {
                return .response(status: state.overrideReconcileStatus, payload: state.overrideReconcilePayload ?? Data(#"{}"#.utf8))
            } else if path.contains("/snapshots/") {
                return .response(
                    status: state.overrideSnapshotStatus,
                    payload: state.overrideSnapshotPayload ?? Data(#"{"instrument":{"workspace":"india","venue":"NSE","segment":"CASH","symbol":"RELIANCE","currency":"INR"},"source":"local-replay","bid":"99.02","ask":"101.02","quote_observed_at":"2026-09-11T18:37:05Z","quote_received_at":"2026-09-11T18:37:05Z","quote_sequence":3,"last_trade_price":"100","snapshot_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}"#.utf8))
            } else if request.httpMethod == "POST" {
                return .response(
                    status: 201,
                    payload: state.overrideStartPayload ?? Data(#"{"state":"RUNNING","provider":"local-replay","instruments":[{"workspace":"india","venue":"NSE","segment":"CASH","symbol":"RELIANCE","currency":"INR"}],"read_only":true}"#.utf8))
            } else if path.hasSuffix("/sessions/current"), let override = state.overrideCurrentPayload {
                return .response(status: 200, payload: override)
            } else {
                return .response(status: 200, payload: Data(#"{"state":"STOPPED","provider":null,"instruments":[],"read_only":true}"#.utf8))
            }
        }
    }
}

/// Id -> stub lookup for the URLProtocol. Weak, so a finished test's stub is released.
final class PaperOperationsStubRegistry: @unchecked Sendable {
    static let shared = PaperOperationsStubRegistry()

    private struct Entry { weak var stub: PaperOperationsStub? }
    private let lock = NSLock()
    private var entries: [String: Entry] = [:]

    func register(_ stub: PaperOperationsStub) {
        lock.lock()
        defer { lock.unlock() }
        entries[stub.id] = Entry(stub: stub)
    }

    func unregister(id: String) {
        lock.lock()
        defer { lock.unlock() }
        entries[id] = nil
    }

    func stub(id: String?) -> PaperOperationsStub? {
        guard let id else { return nil }
        lock.lock()
        defer { lock.unlock() }
        return entries[id]?.stub
    }
}

/// Stateless router. It owns no overrides or records; everything lives on the stub named by the
/// request's id header. A request with no live stub fails loudly instead of answering from shared state.
final class PaperOperationsURLProtocol: URLProtocol {
    static let allowlistPrefixes: [String] = [
        "/api/market-data/sessions",
        "/api/market-data/sessions/current",
        "/api/market-data/snapshots/",
        "/api/market-data/paper-preparations",
        "/api/market-data/paper-reconciliations",
    ]

    override class func canInit(with request: URLRequest) -> Bool { true }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let id = request.value(forHTTPHeaderField: PaperOperationsStub.idHeader)
        guard let stub = PaperOperationsStubRegistry.shared.stub(id: id) else {
            client?.urlProtocol(self, didFailWithError: URLError(.cancelled))
            return
        }
        switch stub.respond(to: request, body: Self.body(from: request)) {
        case .transportFailure(let error):
            client?.urlProtocol(self, didFailWithError: error)
        case .response(let status, let payload):
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
