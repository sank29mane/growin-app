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
        PracticeApprovalAuthorizer(
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

    @Test func theIndiaPaperAdapterEntryPointRefusesPractice() throws {
        let review = try PracticeApprovalTests.review()
        Self.expectRefused({ try LocalPaperApprovalSigner().sign(review.signedBytes) }, "LocalPaperApprovalSigner")
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
        let authorizer = PracticeApprovalAuthorizer(
            authenticator: StubAuthenticator(outcome: nil, counter: counter),
            identity: { try Self.fixtureIdentity(signer, $0) },
            sign: { try signer.signAuthorizedPractice($0, for: $1, authorization: $2) }
        )
        let review = try PracticeApprovalTests.review()
        let signature = try await authorizer.signature(for: review, workspace: .uk)
        let key = try P256.Signing.PublicKey(x963Representation: identity.publicKeyX963)
        #expect(key.isValidSignature(try P256.Signing.ECDSASignature(derRepresentation: signature), for: review.signedBytes))
        #expect(counter.auth == 1)

        let denied = PracticeApprovalAuthorizer(
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

    /// Resolves symlinks with realpath(3) so the repo root and every enumerated file agree on one
    /// spelling (a worktree under a symlinked directory, or /var vs /private/var).
    private static func realPath(_ path: String) -> String {
        guard let resolved = realpath(path, nil) else { return path }
        defer { free(resolved) }
        return String(cString: resolved)
    }

    private static func appSources() throws -> [(path: String, text: String)] {
        let repoRoot = realPath(
            URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().path)
        let enumerator = try #require(FileManager.default.enumerator(
            at: URL(fileURLWithPath: realPath(repoRoot + "/Growin")), includingPropertiesForKeys: nil))
        var sources: [(String, String)] = []
        for case let url as URL in enumerator where url.pathExtension == "swift" {
            let resolved = realPath(url.path)
            #expect(resolved.hasPrefix(repoRoot + "/"), "\(resolved) resolves outside the repo")
            sources.append((String(resolved.dropFirst(repoRoot.count + 1)),
                            try String(contentsOfFile: resolved, encoding: .utf8)))
        }
        return sources
    }

    @Test func noOtherCallerOfTheRawSignerHandlesPractice() throws {
        let sources = try Self.appSources()
        #expect(sources.count > 10, "source probe found too few files")
        let rawCallers = Set(sources.filter { $0.text.contains("LocalApprovalSigner.shared.sign(") }.map(\.path))
        // A new raw caller fails here and must be reviewed for PRACTICE handling.
        #expect(rawCallers == [
            "Growin/Models/PaperOperationsModels.swift",
            "Growin/ViewModels/ChatViewModel.swift",
            "Growin/Views/SettingsView.swift",
        ])
        for (path, text) in sources where rawCallers.contains(path) {
            #expect(!text.contains("expectedPractice"), "\(path) builds a PRACTICE review")
            #expect(!text.contains("PracticeApprovalAuthorizer"), "\(path) mixes the authorizer with the raw signer")
        }
        let chat = try #require(sources.first { $0.path == "Growin/ViewModels/ChatViewModel.swift" }?.text)
        let guardAt = try #require(chat.range(of: "PracticeApprovalPolicy.mode"))
        let signAt = try #require(chat.range(of: "LocalApprovalSigner.shared.sign("))
        #expect(guardAt.lowerBound < signAt.lowerBound, "Chat must reject PRACTICE before it signs")
    }

    @Test func onlyTheAuthorizerCanMintAPracticeSigningToken() throws {
        let sources = try Self.appSources()
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

    @Test func appCodeOnlyUsesTheSharedAuthorizerAndNeverBuildsAnother() throws {
        let sources = try Self.appSources()
        #expect(sources.count > 10, "source probe found too few files")
        let authorizerPath = "Growin/Security/PracticeApprovalAuthorizer.swift"
        for (path, text) in sources {
            if path == authorizerPath {
                // Exactly one construction, and it is the shared instance.
                #expect(text.components(separatedBy: "PracticeApprovalAuthorizer(").count - 1 == 1,
                        "\(path) constructs more than the shared authorizer")
                #expect(text.contains("static let shared = PracticeApprovalAuthorizer("))
                continue
            }
            #expect(!text.contains("PracticeApprovalAuthorizer("), "\(path) constructs an authorizer")
            #expect(!text.contains("PracticeApprovalAuthorizer.init"), "\(path) constructs an authorizer")
        }
        let users = sources.filter { $0.text.contains("PracticeApprovalAuthorizer.shared") }.map(\.path)
        #expect(users.contains("Growin/Views/Trading/PracticeApprovalsView.swift"))
    }
}
