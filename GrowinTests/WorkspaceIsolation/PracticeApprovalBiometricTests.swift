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
            sign: { _, _ in counter.didSign(); return Data([1, 2, 3]) }
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
}
