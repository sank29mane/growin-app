import Foundation
import Testing
@testable import Growin

enum PaperOperationsSourceProbe {
    static func contents(_ relativePath: String) throws -> String {
        try SourceTree.contents(relativePath)
    }
}

struct PaperOperationsClientTests {
    /// Swift Testing builds a fresh struct per test, so each test owns its stub, session and records.
    let stub = PaperOperationsStub()

    private func makeClient() -> PaperOperationsClient {
        stub.reset()
        return stub.makeClient()
    }

    @Test
    func startReplayPostsConfirmationLiteralAndRelianceFixture() async throws {
        try await PaperOperationsMainActorRunner.shared.run {
            let client = makeClient()
            _ = try await client.startReplay()

            let record = stub.snapshotRecord()
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
        try await PaperOperationsMainActorRunner.shared.run {
            let client = makeClient()
            _ = try await client.startReplay()
            _ = try await client.refreshStatus()
            _ = try await client.loadSnapshot(symbol: "RELIANCE")
            _ = try await client.prepare(symbol: "RELIANCE", quantity: "1")
            _ = try await client.reconcile(proposalId: "p1")
            _ = try await client.stopReplay()

            let record = stub.snapshotRecord()
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
        try await PaperOperationsMainActorRunner.shared.run {
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
        try await PaperOperationsMainActorRunner.shared.run {
            let client = makeClient()
            _ = try await client.stopSession()

            let record = stub.snapshotRecord()
            #expect(record.methods == ["DELETE"])
            #expect(record.urls.map(\.path) == ["/api/market-data/sessions/current"])
            #expect(record.urls.allSatisfy { !$0.path.contains("/snapshots/") })
        }
    }

    @Test
    func currentSessionGetsSessionsCurrent() async throws {
        try await PaperOperationsMainActorRunner.shared.run {
            let client = makeClient()
            _ = try await client.currentSession()

            let record = stub.snapshotRecord()
            #expect(record.methods == ["GET"])
            #expect(record.urls.map(\.path) == ["/api/market-data/sessions/current"])
        }
    }

    @Test
    func snapshot409StaleSnapshotMapsToTypedClientError() async {
        await PaperOperationsMainActorRunner.shared.run {
            let client = makeClient()
            stub.overrideSnapshotStatus = 409
            stub.overrideSnapshotPayload = Data(
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
        try await PaperOperationsMainActorRunner.shared.run {
            let client = makeClient()
            _ = try await client.prepareIndiaPaper(symbol: "RELIANCE", quantity: "1")

            let record = stub.snapshotRecord()
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
        try await PaperOperationsMainActorRunner.shared.run {
            let client = makeClient()
            _ = try await client.reconcileIndiaPaper(proposalId: "paper-admitted-1")

            let record = stub.snapshotRecord()
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
        stub.reset()
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
        #expect(stub.snapshotRecord().urls.isEmpty)
        #expect(stub.snapshotRecord().methods.isEmpty)
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
        await PaperOperationsMainActorRunner.shared.run {
            let client = makeClient()
            stub.overridePrepareStatus = 409
            stub.overridePreparePayload = Data(
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
