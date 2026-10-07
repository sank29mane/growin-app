import Foundation
import LocalAuthentication

enum PracticeApprovalAuthError: LocalizedError, Equatable {
    case unavailable
    case cancelled
    case failed

    var errorDescription: String? {
        switch self {
        case .unavailable:
            return "Touch ID is not available on this Mac, so practice orders cannot be approved here."
        case .cancelled:
            return "Touch ID was cancelled. The practice order was not approved."
        case .failed:
            return "Touch ID did not recognise you. The practice order was not approved."
        }
    }
}

/// One biometric check per call. A conforming type must never fall back to a passcode.
protocol BiometricAuthenticating: Sendable {
    func authenticate(reason: String) async throws
}

/// Touch ID only (`.deviceOwnerAuthenticationWithBiometrics`). A fresh LAContext per
/// call with no reuse window, so every signature needs its own touch. No passcode
/// fallback: the fallback button is hidden and the policy refuses passcode anyway.
struct TouchIDAuthenticator: BiometricAuthenticating {
    func authenticate(reason: String) async throws {
        let context = LAContext()
        context.touchIDAuthenticationAllowableReuseDuration = 0
        context.localizedFallbackTitle = ""
        var policyError: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &policyError) else {
            throw PracticeApprovalAuthError.unavailable
        }
        do {
            let ok = try await context.evaluatePolicy(
                .deviceOwnerAuthenticationWithBiometrics,
                localizedReason: reason
            )
            guard ok else { throw PracticeApprovalAuthError.failed }
        } catch let error as PracticeApprovalAuthError {
            throw error
        } catch let error as LAError {
            switch error.code {
            case .userCancel, .appCancel, .systemCancel:
                throw PracticeApprovalAuthError.cancelled
            case .biometryNotAvailable, .biometryNotEnrolled, .biometryLockout, .passcodeNotSet:
                throw PracticeApprovalAuthError.unavailable
            default:
                throw PracticeApprovalAuthError.failed
            }
        } catch {
            throw PracticeApprovalAuthError.failed
        }
    }
}

/// Proof that a biometric check just succeeded. `LocalApprovalSigner.signAuthorizedPractice`
/// requires one, and the initializer is `fileprivate`, so only `PracticeApprovalAuthorizer`
/// (after `authenticate` returns) can create it.
struct PracticeSigningAuthorization: Sendable {
    fileprivate init() {}
}

/// Produces a practice approval signature only after a biometric check. The review
/// is validated and the key identity matched BEFORE the prompt, so the operator is
/// never asked to touch for an envelope that would be refused. The signing closure
/// runs only after authentication succeeds; any authenticator error fails closed.
struct PracticeApprovalAuthorizer: Sendable {
    typealias IdentityProvider = @Sendable (Workspace) throws -> ApprovalSignerIdentity
    typealias SignProvider = @Sendable (Data, Workspace, PracticeSigningAuthorization) throws -> Data

    let authenticator: any BiometricAuthenticating
    let identity: IdentityProvider
    let sign: SignProvider

    static let shared = PracticeApprovalAuthorizer(
        authenticator: TouchIDAuthenticator(),
        identity: { try LocalApprovalSigner.shared.identity(for: $0) },
        sign: {
            try LocalApprovalSigner.shared.signAuthorizedPractice($0, for: $1, authorization: $2)
        }
    )

    func signature(for review: TradeApprovalReview, workspace: Workspace) async throws -> Data {
        guard review.payload.workspace == workspace.rawValue,
              review.payload.mode == PracticeApprovalPolicy.mode else {
            throw TradeApprovalReviewError.invalidEnvelope
        }
        let signerIdentity = try identity(workspace)
        guard signerIdentity.keyID == review.payload.keyId else {
            throw TradeApprovalReviewError.signerMismatch
        }
        try await authenticator.authenticate(
            reason: "Approve practice \(review.payload.side) \(review.payload.quantity) \(review.payload.ticker)"
        )
        return try sign(review.signedBytes, workspace, PracticeSigningAuthorization())
    }
}
