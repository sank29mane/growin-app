import CryptoKit
import Foundation
import Testing
@testable import Growin

private final class AuthCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var authCount = 0
    private var signCount = 0
    var auth: Int { lock.lock(); defer { lock.unlock() }; return authCount }
    var signed: Int { lock.lock(); defer { lock.unlock() }; return signCount }
    func didAuth() { lock.lock(); authCount += 1; lock.unlock() }
    func didSign() { lock.lock(); signCount += 1; lock.unlock() }
}

private struct StubAuthenticator: BiometricAuthenticating {
    let outcome: PracticeApprovalAuthError?
    let counter: AuthCounter
    func authenticate(reason: String) async throws {
        counter.didAuth()
        if let outcome { throw outcome }
    }
}

/// The biometric gate on every practice signature. No test here touches the real
/// Keychain, LocalAuthentication or a network: the authenticator and signer are stubs.
@MainActor
@Suite(.serialized)
struct PracticeApprovalBiometricTests {
    private static func authorizer(
        _ outcome: PracticeApprovalAuthError?, counter: AuthCounter, keyId: String = "key-1"
    ) -> PracticeApprovalAuthorizer {
        PracticeApprovalAuthorizer.makeForTesting(
            authenticator: StubAuthenticator(outcome: outcome, counter: counter),
            identity: { _ in ApprovalSignerIdentity(keyID: keyId, publicKeyX963: Data()) },
            sign: { _, _, _ in counter.didSign(); return Data([1, 2, 3]) }
        )
    }

    @Test func successfulBiometricProducesExactlyOneSignature() async throws {
        let counter = AuthCounter()
        let review = try PracticeApprovalTests.review()
        let signature = try await Self.authorizer(nil, counter: counter).signature(for: review, workspace: .uk)
        #expect(signature == Data([1, 2, 3]))
        #expect(counter.auth == 1)
        #expect(counter.signed == 1)
    }

    @Test func everyApprovalNeedsItsOwnBiometric() async throws {
        let counter = AuthCounter()
        let review = try PracticeApprovalTests.review()
        let authorizer = Self.authorizer(nil, counter: counter)
        _ = try await authorizer.signature(for: review, workspace: .uk)
        _ = try await authorizer.signature(for: review, workspace: .uk)
        #expect(counter.auth == 2)
        #expect(counter.signed == 2)
    }

    @Test(arguments: [PracticeApprovalAuthError.cancelled, .unavailable, .failed])
    func aFailedCancelledOrUnavailableBiometricProducesNoSignature(outcome: PracticeApprovalAuthError) async throws {
        let counter = AuthCounter()
        let review = try PracticeApprovalTests.review()
        do {
            _ = try await Self.authorizer(outcome, counter: counter).signature(for: review, workspace: .uk)
            Issue.record("Expected the approval to fail closed")
        } catch let error as PracticeApprovalAuthError {
            #expect(error == outcome)
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
        #expect(counter.auth == 1)
        #expect(counter.signed == 0)
    }

    @Test func aKeyMismatchOrWrongWorkspaceNeverReachesTheBiometricPromptOrSigner() async throws {
        let counter = AuthCounter()
        let review = try PracticeApprovalTests.review()
        do {
            _ = try await Self.authorizer(nil, counter: counter, keyId: "other-key")
                .signature(for: review, workspace: .uk)
            Issue.record("Expected signerMismatch")
        } catch TradeApprovalReviewError.signerMismatch {
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
        do {
            _ = try await Self.authorizer(nil, counter: counter).signature(for: review, workspace: .india)
            Issue.record("Expected invalidEnvelope")
        } catch TradeApprovalReviewError.invalidEnvelope {
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
        #expect(counter.auth == 0)
        #expect(counter.signed == 0)
    }

    // MARK: Every signing entry point refuses PRACTICE without Touch ID

    private static func realSigner() throws -> (LocalApprovalSigner, KeychainStore) {
        let store = KeychainStore(service: "san.Growin.credentials.v1.test.\(UUID().uuidString)")
        let signer = LocalApprovalSigner(store: store)
        _ = try signer.createIdentityIfNeeded(for: .uk)
        _ = try signer.createIdentityIfNeeded(for: .india)
        return (signer, store)
    }

    /// The review fixture is frozen with key id "key-1"; present the real public key under it.
    nonisolated private static func fixtureIdentity(_ signer: LocalApprovalSigner, _ workspace: Workspace) throws -> ApprovalSignerIdentity {
        ApprovalSignerIdentity(keyID: "key-1", publicKeyX963: try signer.identity(for: workspace).publicKeyX963)
    }

    private static func cleanUp(_ store: KeychainStore) {
        for workspace in Workspace.allCases {
            try? store.remove(.approvalSigningKey, scope: .workspace(workspace))
        }
    }

    private static func expectRefused(_ body: () throws -> Data, _ label: String) {
        do {
            _ = try body()
            Issue.record("\(label) signed a PRACTICE payload without authorization")
        } catch LocalApprovalSignerError.practiceRequiresAuthorization {
        } catch {
            Issue.record("\(label) threw the wrong error: \(error)")
        }
    }

    @Test func theRawSignerRefusesPracticePayloadsInEveryShape() throws {
        let (signer, store) = try Self.realSigner()
        defer { Self.cleanUp(store) }
        let review = try PracticeApprovalTests.review()
        let shapes: [(String, Data)] = [
            ("signedBytes", review.signedBytes),
            ("lowercase mode", Data(#"{"mode":"practice","x":1}"#.utf8)),
            ("padded mode", Data(#"{"mode":" PRACTICE "}"#.utf8)),
            ("non-JSON bytes", Data("PRACTICE order bytes".utf8)),
        ]
        for workspace in Workspace.allCases {
            for (label, bytes) in shapes {
                Self.expectRefused({ try signer.sign(bytes, for: workspace) }, "\(label)/\(workspace)")
            }
        }
    }

    @Test func theRawSignerStillSignsPaperPayloads() throws {
        let (signer, store) = try Self.realSigner()
        defer { Self.cleanUp(store) }
        let signature = try signer.sign(Data(#"{"mode":"PAPER"}"#.utf8), for: .uk)
        #expect(!signature.isEmpty)
    }

    // MARK: India paper entry point
    //
    // India signs through the router's Secure Enclave signer, not the software signer, so the
    // software signer's own check (`practiceRequiresAuthorization`) is no longer the first line of
    // defence there. The Secure Enclave signer refuses earlier and by structure: it parses the exact
    // bytes (strict inspector), then checks purpose, workspace and mode, all before the stored key is
    // read, the key identity is derived, or a Touch ID context is built. That refusal is the stronger
    // guard (one typed error per cause, no text matching, and a practice-looking byte string that is
    // not a valid paper payload never gets near a key), so these tests pin it instead of forcing the
    // software signer's error onto India. The software signer's own refusal stays pinned by
    // `theRawSignerRefusesPracticePayloadsInEveryShape`.

    /// A well-formed India paper-dispatch envelope whose only defect is the mode. Nothing but the mode
    /// check can stop it: the key id is real and the purpose and workspace are right.
    private static func indiaDispatchBytes(keyId: String, mode: String) -> Data {
        CanonicalJSON.data([
            "version": 1, "purpose": "growin.execution.dispatch", "challenge_id": "c-1",
            "proposal_id": "p-1", "client_order_id": "co-1", "intent_hash": "ih",
            "workspace": "india", "account": "paper", "broker": "local-paper", "mode": mode,
            "ticker": "RELIANCE", "side": "BUY", "quantity": "10", "order_type": "LIMIT",
            "limit_price": "2500.50", "nonce": "n", "issued_at": 1, "expires_at": 2, "key_id": keyId,
        ] as [String: Any])
    }

    private static func practiceShapes(keyId: String) throws -> [(String, Data)] {
        let review = try PracticeApprovalTests.review()
        return [
            ("UK practice review bytes", review.signedBytes),
            ("lowercase mode, not an envelope", Data(#"{"mode":"practice","x":1}"#.utf8)),
            ("padded mode, not an envelope", Data(#"{"mode":" PRACTICE "}"#.utf8)),
            ("non-JSON bytes", Data("PRACTICE order bytes".utf8)),
            ("India envelope, PRACTICE", indiaDispatchBytes(keyId: keyId, mode: "PRACTICE")),
            ("India envelope, lowercase practice", indiaDispatchBytes(keyId: keyId, mode: "practice")),
            ("India envelope, padded PRACTICE", indiaDispatchBytes(keyId: keyId, mode: " PRACTICE ")),
        ]
    }

    /// The typed refusals that happen before the signer looks at a key. A refusal that comes later
    /// (`notConfigured`, `keyMismatch`, `underlying`) would mean the key was read first.
    private static func isRefusalBeforeAnyKeyAccess(_ error: Error) -> Bool {
        if error is SignedPayloadInspectionError { return true }
        switch error as? ApprovalSignerError {
        case .purposeNotAllowed?, .workspaceMismatch?, .modeNotAllowed?, .indiaOnly?: return true
        default: return false
        }
    }

    @Test func theIndiaPaperAdapterEntryPointRefusesEveryPracticeShapeBeforeAnyKeyAccess() throws {
        // A key id no India key can have, so a refusal after the key read would show as
        // `notConfigured` (no key enrolled) or `keyMismatch` (an operator key), never as these.
        for (label, bytes) in try Self.practiceShapes(keyId: String(repeating: "a", count: 64)) {
            do {
                _ = try LocalPaperApprovalSigner().sign(bytes)
                Issue.record("\(label) produced a signature at the India paper entry point")
            } catch {
                #expect(Self.isRefusalBeforeAnyKeyAccess(error), "\(label) was refused too late or untyped: \(error)")
            }
        }
    }

    /// Same refusals, on the router the adapter calls, but with a counting key backend so "before any
    /// key access" is measured rather than inferred from the error type. Sync and async paths.
    @Test func everyPracticeShapeIsRefusedBeforeTheKeyIsReadOrTouchIDIsAsked() async throws {
        let fixture = SignerFixture(scalar: nil)
        defer { fixture.cleanUp() }
        let router = ApprovalSignerRouter(india: fixture.signer, uk: LocalApprovalSigner(store: fixture.store))
        let identity = try router.createIdentityIfNeeded(for: .india)
        let shapes = try Self.practiceShapes(keyId: identity.keyID)
        fixture.backend.resetCounters()

        var signatures = 0
        for (label, bytes) in shapes {
            do {
                _ = try router.sign(bytes, for: .india, flow: .paperApproval)
                signatures += 1
                Issue.record("\(label) was signed (sync)")
            } catch {
                #expect(Self.isRefusalBeforeAnyKeyAccess(error), "\(label) sync: \(error)")
            }
            do {
                _ = try await router.signAsync(bytes, for: .india, flow: .paperApproval)
                signatures += 1
                Issue.record("\(label) was signed (async)")
            } catch {
                #expect(Self.isRefusalBeforeAnyKeyAccess(error), "\(label) async: \(error)")
            }
        }
        #expect(signatures == 0)
        #expect(fixture.backend.keyAccessCount == 0, "a practice payload reached the key")
        #expect(fixture.backend.signs == 0)
        #expect(fixture.contexts.reasons.isEmpty, "a practice payload built a Touch ID prompt")

        // Control: the same fixture does sign a PAPER envelope, so the zero counts above are not an
        // artefact of a signer that refuses everything.
        let paper = Self.indiaDispatchBytes(keyId: identity.keyID, mode: "PAPER")
        let der = try router.sign(paper, for: .india, flow: .paperApproval)
        #expect(!der.isEmpty)
        #expect(fixture.backend.signs == 1)
        #expect(fixture.contexts.reasons.count == 1)
    }

    /// The practice authorizer is the one place the software key signs outside the router, so it
    /// must never accept India, even for a review whose own workspace field says india (the plain
    /// review initializer is not validated, so such a value can be built in-process).
    @Test func theAuthorizerRefusesIndiaBeforeAnyIdentityBiometricOrSignature() async throws {
        let counter = AuthCounter()
        let identityCalls = AuthCounter()
        let expires = Int(Date().timeIntervalSince1970) + 600
        let json = PracticeApprovalTests.payload(workspace: "india", expires: expires)
        let challenge = try PracticeApprovalTests.challenge(json, expires: expires)
        let bytes = try #require(Data(base64Encoded: challenge.signedPayloadB64))
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let payload = try decoder.decode(SignedTradeApprovalPayload.self, from: bytes)
        #expect(payload.workspace == "india")
        #expect(payload.mode == PracticeApprovalPolicy.mode)
        let indiaReview = TradeApprovalReview(challenge: challenge, payload: payload, signedBytes: bytes)

        let authorizer = PracticeApprovalAuthorizer.makeForTesting(
            authenticator: StubAuthenticator(outcome: nil, counter: counter),
            identity: { _ in
                identityCalls.didAuth()
                return ApprovalSignerIdentity(keyID: "key-1", publicKeyX963: Data())
            },
            sign: { _, _, _ in counter.didSign(); return Data([1]) }
        )
        do {
            _ = try await authorizer.signature(for: indiaReview, workspace: .india)
            Issue.record("The authorizer signed for India")
        } catch TradeApprovalReviewError.invalidEnvelope {
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
        #expect(identityCalls.auth == 0, "the software identity was read for India")
        #expect(counter.auth == 0)
        #expect(counter.signed == 0)
    }

    @Test func chatRefusesAPracticeReviewBeforeAnySigning() async throws {
        let review = try PracticeApprovalTests.review()
        let chat = ChatViewModel()
        do {
            try await chat.completeTradeApproval(review)
            Issue.record("Chat accepted a PRACTICE review")
        } catch TradeApprovalReviewError.invalidEnvelope {
        } catch {
            Issue.record("Unexpected error: \(error)")
        }
    }

    @Test func theAuthorizedPathSignsOnlyAfterBiometricAndTheSignatureVerifies() async throws {
        let (signer, store) = try Self.realSigner()
        defer { Self.cleanUp(store) }
        let identity = try signer.identity(for: .uk)
        let counter = AuthCounter()
        let authorizer = PracticeApprovalAuthorizer.makeForTesting(
            authenticator: StubAuthenticator(outcome: nil, counter: counter),
            identity: { try Self.fixtureIdentity(signer, $0) },
            sign: { try signer.signAuthorizedPractice($0, for: $1, authorization: $2) }
        )
        let review = try PracticeApprovalTests.review()
        let signature = try await authorizer.signature(for: review, workspace: .uk)
        let key = try P256.Signing.PublicKey(x963Representation: identity.publicKeyX963)
        #expect(key.isValidSignature(try P256.Signing.ECDSASignature(derRepresentation: signature), for: review.signedBytes))
        #expect(counter.auth == 1)

        let denied = PracticeApprovalAuthorizer.makeForTesting(
            authenticator: StubAuthenticator(outcome: .cancelled, counter: counter),
            identity: { try Self.fixtureIdentity(signer, $0) },
            sign: { try signer.signAuthorizedPractice($0, for: $1, authorization: $2) }
        )
        do {
            _ = try await denied.signature(for: review, workspace: .uk)
            Issue.record("Cancelled biometric still signed")
        } catch let error as PracticeApprovalAuthError {
            #expect(error == .cancelled)
        }
    }

    // MARK: Source scan

    /// Every .swift file under Growin/, as (repo-relative path, contents). Symlink-safe via SourceTree.
    private static func appSources() throws -> [(path: String, text: String)] {
        try SourceTree.swiftSources()
    }

    /// Topology after #558: the raw software signer is reached only inside `ApprovalSignerRouter`
    /// (plus the practice authorizer, pinned by the tests below), and every approval signature in the
    /// app goes through `ApprovalSignerRouter.shared`. A new router caller fails the exact-set check
    /// and must be reviewed for PRACTICE handling. The router itself is PAPER-only for both keys.
    @Test func theRawSignerIsReachedOnlyThroughTheRouterAndEveryRouterCallerHandlesPractice() throws {
        let sources = try Self.appSources()
        #expect(sources.count > 10, "source probe found too few files")
        let routerPath = "Growin/Security/ApprovalSignerRouter.swift"

        // No file calls the software signer's `sign(` through `.shared`; the router holds its own
        // reference and the authorizer uses the token-gated method.
        let rawCallers = Set(sources.filter { $0.text.contains("LocalApprovalSigner.shared.sign(") }.map(\.path))
        #expect(rawCallers.isEmpty, "raw software signing outside the router: \(rawCallers.sorted())")
        // Nor does anything name the Secure Enclave signer's shared instance: the router takes it by
        // inferred `.shared`, so a direct caller would bypass the flow, purpose and key checks.
        let seCallers = sources.filter { $0.text.contains("SecureEnclaveApprovalSigner.shared") }.map(\.path)
        #expect(seCallers.isEmpty, "Secure Enclave signer reached without the router: \(seCallers)")
        // One router, built once, held only as `.shared` and always called through.
        for (path, text) in sources where path != routerPath {
            #expect(!text.contains("ApprovalSignerRouter("), "\(path) builds a second router")
            #expect(Self.matchCount(#"\bApprovalSignerRouter\.shared(?!\s*\.\s*\w)"#, in: text) == 0,
                    "\(path) keeps a reference to ApprovalSignerRouter.shared instead of calling through it")
        }

        let routerCallers = Set(sources.filter {
            $0.path != routerPath
                && ($0.text.contains("ApprovalSignerRouter.shared.sign(")
                    || $0.text.contains("ApprovalSignerRouter.shared.signAsync("))
        }.map(\.path))
        // A new caller fails here and must be reviewed for PRACTICE handling.
        #expect(routerCallers == [
            "Growin/Models/PaperOperationsModels.swift",
            "Growin/ViewModels/ChatViewModel.swift",
            "Growin/ViewModels/RelayOrderBatchViewModel.swift",
            "Growin/Views/SettingsView.swift",
        ])
        for (path, text) in sources where routerCallers.contains(path) {
            #expect(!text.contains("expectedPractice"), "\(path) builds a PRACTICE review")
            #expect(!text.contains("PracticeApprovalAuthorizer"), "\(path) mixes the authorizer with the router")
        }

        // Chat rejects PRACTICE before it reads an identity or signs through the router.
        let chat = try #require(sources.first { $0.path == "Growin/ViewModels/ChatViewModel.swift" }?.text)
        let guardAt = try #require(chat.range(of: "PracticeApprovalPolicy.mode"))
        let routerAt = try #require(chat.range(of: "ApprovalSignerRouter.shared"))
        #expect(guardAt.lowerBound < routerAt.lowerBound, "Chat must reject PRACTICE before it uses the router")

        // The router's own gates: UK software key is PAPER-only, India's Secure Enclave signer is
        // PAPER-only for paper dispatch. Behaviour is covered in RelayOrderReviewTests and above.
        let router = try #require(sources.first { $0.path == routerPath }?.text)
        #expect(router.contains(#"guard case .string("PAPER")? = object["mode"]"#))
        #expect(router.contains("softwareModeNotPaper"))
        let enclave = try #require(sources.first { $0.path == "Growin/Security/SecureEnclaveApprovalSigner.swift" }?.text)
        #expect(enclave.contains(#"paper.mode != "PAPER""#))
        // Practice Approvals signs through the authorizer, never the router.
        let practiceView = try #require(sources.first { $0.path == "Growin/Views/Trading/PracticeApprovalsView.swift" }?.text)
        #expect(!practiceView.contains("ApprovalSignerRouter"))
    }

    @Test func onlyTheAuthorizerCanMintAPracticeSigningToken() throws {
        let sources = try Self.appSources()
        #expect(sources.count > 10, "source probe found too few files")
        let authorizerPath = "Growin/Security/PracticeApprovalAuthorizer.swift"
        let signerPath = "Growin/Security/LocalApprovalSigner.swift"
        for (path, text) in sources {
            if text.contains("PracticeSigningAuthorization(") {
                #expect(path == authorizerPath, "\(path) mints a practice token")
            }
            if text.contains("signAuthorizedPractice(") {
                #expect([authorizerPath, signerPath].contains(path), "\(path) calls the practice signer directly")
            }
        }
        let practiceView = try #require(sources.first { $0.path == "Growin/Views/Trading/PracticeApprovalsView.swift" }?.text)
        #expect(practiceView.contains("PracticeApprovalAuthorizer.shared.signature("))
        #expect(!practiceView.contains("LocalApprovalSigner"))
    }

    // MARK: Single construction site
    //
    // The compiler is the primary enforcement: `PracticeApprovalAuthorizer.init` is `private`,
    // so only its own file can build one, and the only test injection point
    // (`makeForTesting`) is `#if DEBUG`. This scan is the secondary check for the type file
    // itself, where `private` does not help: it must contain exactly one Release-reachable
    // construction, the `shared` definition.

    private static let authorizerPath = "Growin/Security/PracticeApprovalAuthorizer.swift"

    private static func stripComments(_ text: String) -> String {
        text.replacingOccurrences(of: #"/\*[\s\S]*?\*/"#, with: "", options: .regularExpression)
            .replacingOccurrences(of: #"//[^\n]*"#, with: "", options: .regularExpression)
    }

    private static func matchCount(_ pattern: String, in text: String) -> Int {
        (try? NSRegularExpression(pattern: pattern))
            .map { $0.numberOfMatches(in: text, range: NSRange(text.startIndex..., in: text)) } ?? 0
    }

    /// Every spelling of "build an authorizer" that does not name the type outright:
    /// `Self(`, `Self.init`, `Type.init`, a bare `.init`, and `Type(`.
    private static func constructionCount(in text: String) -> Int {
        [#"\bSelf\s*\("#, #"\.init\b"#, #"\bPracticeApprovalAuthorizer\s*\("#]
            .reduce(0) { $0 + matchCount($1, in: text) }
    }

    /// Splits the source on `#if DEBUG` / `#endif`. Any other conditional compilation
    /// directive is reported, because `#if !DEBUG` or `#else` would make a region Release-reachable.
    private static func splitCompilationConditions(
        _ text: String
    ) -> (release: String, debug: String, problems: [String]) {
        var release: [Substring] = []
        var debug: [Substring] = []
        var problems: [String] = []
        var inDebug = false
        for line in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed == "#if DEBUG" {
                if inDebug { problems.append("nested #if DEBUG") }
                inDebug = true
            } else if trimmed == "#endif" {
                if !inDebug { problems.append("#endif without #if DEBUG") }
                inDebug = false
            } else if trimmed.hasPrefix("#if") || trimmed.hasPrefix("#else") || trimmed.hasPrefix("#elseif") {
                problems.append("unsupported directive `\(trimmed)`")
            } else if inDebug {
                debug.append(line)
            } else {
                release.append(line)
            }
        }
        if inDebug { problems.append("unterminated #if DEBUG") }
        return (release.joined(separator: "\n"), debug.joined(separator: "\n"), problems)
    }

    /// Violations for one app source file. Empty means the file may not build an authorizer
    /// outside the allowed single site.
    private static func authorizerViolations(path: String, text rawText: String) -> [String] {
        let text = stripComments(rawText)
        var out: [String] = []
        if path != authorizerPath {
            if matchCount(#"\bPracticeApprovalAuthorizer\b(?!\.shared\b)"#, in: text) > 0 {
                out.append("\(path) names PracticeApprovalAuthorizer other than as `.shared` (construction, typealias or typed `.init`)")
            }
            if text.contains("makeForTesting") { out.append("\(path) reaches the test factory") }
            return out
        }
        let parts = splitCompilationConditions(text)
        out += parts.problems.map { "\(path): \($0)" }
        let releaseSites = constructionCount(in: parts.release)
        if releaseSites != 1 { out.append("\(path) has \(releaseSites) Release-reachable construction sites, expected 1") }
        if !parts.release.contains("static let shared = PracticeApprovalAuthorizer(") {
            out.append("\(path): the single construction is not the `shared` definition")
        }
        let debugSites = constructionCount(in: parts.debug)
        if debugSites > 1 || (debugSites == 1 && !parts.debug.contains("static func makeForTesting(")) {
            out.append("\(path): DEBUG-only region builds more than the test factory")
        }
        if matchCount(#"typealias\s+\w+(<[^>]*>)?\s*=\s*(\w+\.)*(PracticeApprovalAuthorizer|Self)\b"#, in: text) > 0 {
            out.append("\(path) aliases the authorizer type")
        }
        return out
    }

    @Test func appCodeHasExactlyOneAuthorizerConstructionSite() throws {
        let sources = try Self.appSources()
        #expect(sources.count > 10, "source probe found too few files")
        #expect(sources.contains { $0.path == Self.authorizerPath })
        for (path, text) in sources {
            #expect(Self.authorizerViolations(path: path, text: text).isEmpty,
                    "\(Self.authorizerViolations(path: path, text: text))")
        }
        let users = sources.filter { $0.text.contains("PracticeApprovalAuthorizer.shared") }.map(\.path)
        #expect(users.contains("Growin/Views/Trading/PracticeApprovalsView.swift"))
    }

    @Test func theConstructionScanFlagsEveryEvasionForm() {
        let body = "    static let shared = PracticeApprovalAuthorizer(authenticator: a)\n"
        func typeFile(_ extra: String) -> String { "struct PracticeApprovalAuthorizer {\n\(body)\(extra)}\n" }
        let evasions: [(String, String, String)] = [
            ("typealias elsewhere", "Growin/Views/X.swift",
             "typealias A = PracticeApprovalAuthorizer\nlet x = A(authenticator: a)"),
            ("typed .init elsewhere", "Growin/Views/X.swift",
             "let a: PracticeApprovalAuthorizer = .init(authenticator: a)"),
            ("direct construction elsewhere", "Growin/Views/X.swift", "let a = PracticeApprovalAuthorizer(authenticator: a)"),
            ("test factory elsewhere", "Growin/Views/X.swift", "let a = Foo.makeForTesting(authenticator: a)"),
            ("extension Self(", Self.authorizerPath,
             typeFile("}\nextension PracticeApprovalAuthorizer {\n    static let other = Self(authenticator: a)\n")),
            ("second Self.init", Self.authorizerPath, typeFile("    static let other = Self.init(authenticator: a)\n")),
            ("bare .init", Self.authorizerPath, typeFile("    static let other: PracticeApprovalAuthorizer = .init(authenticator: a)\n")),
            ("typealias of Self", Self.authorizerPath, typeFile("    typealias A = Self\n")),
            ("#if !DEBUG", Self.authorizerPath, typeFile("    #if !DEBUG\n    static let other = Self(authenticator: a)\n    #endif\n")),
            ("#else of DEBUG", Self.authorizerPath,
             typeFile("    #if DEBUG\n    #else\n    static let other = Self(authenticator: a)\n    #endif\n")),
        ]
        for (label, path, text) in evasions {
            #expect(!Self.authorizerViolations(path: path, text: text).isEmpty, "scan missed: \(label)")
        }
        #expect(Self.authorizerViolations(path: Self.authorizerPath, text: typeFile("")).isEmpty)
        #expect(Self.authorizerViolations(
            path: Self.authorizerPath,
            text: typeFile("    #if DEBUG\n    static func makeForTesting() -> Self { Self(authenticator: a) }\n    #endif\n")
        ).isEmpty)
    }

    // MARK: Raw signer instances
    //
    // `theRawSignerIsReachedOnlyThroughTheRouterAndEveryRouterCallerHandlesPractice` matches the literal `LocalApprovalSigner.shared.sign(`.
    // A second instance (`LocalApprovalSigner(store:)`, `.init`, a typealias, an extension factory,
    // or a stored copy of `.shared`) would sign without ever being counted by that scan.
    // The initializer is internal, so the compiler does not stop it.

    private static let signerPath = "Growin/Security/LocalApprovalSigner.swift"

    /// Violations for one app source file. Empty means the file builds no signer instance,
    /// except the single `static let shared` site inside the signer's own file.
    private static func signerInstanceViolations(path: String, text rawText: String) -> [String] {
        let text = rawText
            .split(separator: "\n", omittingEmptySubsequences: false)
            .filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix("//") }
            .joined(separator: "\n")
        var out: [String] = []
        // Named construction, `Type.init`, and inferred `.init(store:` where the type comes from a
        // property, return or parameter annotation (`var s: LocalApprovalSigner { .init(store: x) }`).
        // The initializer's only label is `store`, and `\s` spans newlines for multiline calls.
        var constructions = matchCount(#"\bLocalApprovalSigner\s*\("#, in: text)
            + matchCount(#"\bLocalApprovalSigner\s*\.\s*init\b"#, in: text)
            + matchCount(#"\.\s*init\s*\(\s*store\s*:"#, in: text)
        if path == signerPath {
            constructions += matchCount(#"\bSelf\s*\("#, in: text) + matchCount(#"\bSelf\s*\.\s*init\b"#, in: text)
        }
        if path == signerPath {
            if constructions != 1 { out.append("\(path) builds \(constructions) signer instances, expected exactly the shared one") }
        } else if constructions > 0 {
            out.append("\(path) builds a LocalApprovalSigner instance")
        }
        if path != signerPath, matchCount(#"\bextension\s+LocalApprovalSigner\b"#, in: text) > 0 {
            out.append("\(path) extends LocalApprovalSigner")
        }
        // `let t = LocalApprovalSigner.self; t.init(store:)` builds an instance with no spelling above.
        if path != signerPath, matchCount(#"\bLocalApprovalSigner\s*\.\s*self\b"#, in: text) > 0 {
            out.append("\(path) takes the LocalApprovalSigner metatype")
        }
        if matchCount(#"typealias\s+\w+\s*=\s*(\w+\.)*LocalApprovalSigner\b"#, in: text) > 0 {
            out.append("\(path) aliases the signer type")
        }
        if matchCount(#"\bLocalApprovalSigner\.shared(?!\s*\.\s*\w)"#, in: text) > 0 {
            out.append("\(path) keeps a reference to LocalApprovalSigner.shared instead of calling through it")
        }
        return out
    }

    @Test func appCodeNeverBuildsASecondRawSignerInstance() throws {
        let sources = try Self.appSources()
        #expect(sources.count > 10, "source probe found too few files")
        #expect(sources.contains { $0.path == Self.signerPath })
        for (path, text) in sources {
            #expect(Self.signerInstanceViolations(path: path, text: text).isEmpty,
                    "\(Self.signerInstanceViolations(path: path, text: text))")
        }
    }

    @Test func theSignerInstanceScanFlagsEveryEvasionForm() {
        let signerFile = "final class LocalApprovalSigner {\n    static let shared = LocalApprovalSigner(store: .shared)\n}\n"
        let evasions: [(String, String, String)] = [
            ("instance with store", "Growin/Views/X.swift", "let s = LocalApprovalSigner(store: .shared)"),
            ("instance with spaced paren", "Growin/Views/X.swift", "let s = LocalApprovalSigner (store: store)"),
            ("explicit .init", "Growin/Views/X.swift", "let s = LocalApprovalSigner.init(store: store)"),
            ("typed bare .init", "Growin/Views/X.swift", "let s: LocalApprovalSigner = .init(store: store)"),
            ("typealias", "Growin/Views/X.swift", "typealias Signer = LocalApprovalSigner\nlet s = Signer(store: store)"),
            ("extension factory", "Growin/Views/X.swift", "extension LocalApprovalSigner { static func make() -> Self { Self(store: .shared) } }"),
            ("stored copy of shared", "Growin/Views/X.swift", "let s = LocalApprovalSigner.shared\n_ = try s.sign(bytes, for: .uk)"),
            ("second instance in signer file", Self.signerPath,
             signerFile + "let other = LocalApprovalSigner(store: .shared)\n"),
            ("typed computed property .init", "Growin/Views/X.swift",
             "var signer: LocalApprovalSigner { .init(store: .shared) }"),
            ("typed stored property .init", "Growin/Views/X.swift",
             "let signer: LocalApprovalSigner = .init(store: .shared)"),
            ("function return .init", "Growin/Views/X.swift",
             "func makeSigner() -> LocalApprovalSigner {\n    return .init(store: .shared)\n}"),
            ("parameter default .init", "Growin/Views/X.swift",
             "func run(signer: LocalApprovalSigner = .init(store: .shared)) {}"),
            ("multiline .init", "Growin/Views/X.swift",
             "var signer: LocalApprovalSigner {\n    .init(\n        store: .shared\n    )\n}"),
            ("multiline named construction", "Growin/Views/X.swift",
             "let s = LocalApprovalSigner(\n    store: .shared\n)"),
            ("metatype then init", "Growin/Views/X.swift",
             "let t = LocalApprovalSigner.self\nlet s = t.init(store: .shared)"),
            ("metatype, spaced", "Growin/Views/X.swift", "let t = LocalApprovalSigner . self"),
            ("Self( second site in signer file", Self.signerPath,
             signerFile + "extension LocalApprovalSigner { static func other() -> Self { Self(store: .shared) } }\n"),
            ("no shared instance in signer file", Self.signerPath, "final class LocalApprovalSigner {}\n"),
        ]
        for (label, path, text) in evasions {
            #expect(!Self.signerInstanceViolations(path: path, text: text).isEmpty, "scan missed: \(label)")
        }
        #expect(Self.signerInstanceViolations(path: Self.signerPath, text: signerFile).isEmpty)
        #expect(Self.signerInstanceViolations(
            path: "Growin/Views/X.swift",
            text: "// LocalApprovalSigner(store: x) is not allowed here\nlet id = try LocalApprovalSigner.shared.identity(for: .uk)\nthrow LocalApprovalSignerError.notConfigured"
        ).isEmpty)
    }
}
