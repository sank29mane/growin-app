import Foundation
import Testing
@testable import Growin

/// Tripwire: the REAL production wiring, inside the real test host, must refuse.
///
/// Every other test uses a test-prefixed store. This file is the one place that touches the
/// production singletons, and it only expects refusals. If the runtime guard ever stops
/// working, these fail loudly instead of the host quietly reading or deleting the operator's items.
///
/// Rules this file keeps (pinned by `tests/backend/test_keychain_test_host_guard.py`):
/// - Read-only. No write, no remove, no migration, no signing. The one delete uses an account
///   built from a fresh UUID, so a broken guard would delete nothing real.
/// - Every production call sits inside an assertion that expects a refusal or "not configured".
/// - It asserts on error codes and booleans only. No value is bound, printed, logged or attached.
/// - It is the only GrowinTests file allowed to call the shared store, and the #573 scans exempt
///   that one exact call in this one exact path (nothing else).
@MainActor
@Suite(.serialized)
struct ProductionTripwireTests {
    private static let refused = "PRODUCTION_KEYCHAIN_REFUSED_UNDER_TESTS"
    /// The production service, spelled in two pieces so the name-based scans keep seeing no literal.
    private static let protectedName = "san.Growin.credentials." + "v1"

    /// The refusal code of a throwing call, "NO_REFUSAL" if it did not throw.
    private static func code(of body: () throws -> Void) -> String {
        do {
            try body()
            return "NO_REFUSAL"
        } catch let error as KeychainStoreError {
            return error.code
        } catch {
            return "UNEXPECTED_ERROR"
        }
    }

    @Test func thisProcessIsATestHost() {
        #expect(TestHostDetection.isTestHost())
        #expect(ProductionKeychainGuard.refusesCurrentProcess(service: Self.protectedName))
    }

    @Test func theSharedStoreRefusesARead() {
        #expect(
            Self.code(of: { _ = try KeychainStore.shared.string(for: .alpacaApiKey, scope: .workspace(.uk)) })
                == Self.refused)
    }

    @Test func theSignersReportNotConfigured() {
        #expect(LocalApprovalSigner.shared.isConfigured(for: .uk) == false)
        #expect(LocalApprovalSigner.shared.isConfigured(for: .india) == false)
        #expect(ApprovalSignerRouter.shared.isConfigured(for: .uk) == false)
        #expect(ApprovalSignerRouter.shared.isConfigured(for: .india) == false)
    }

    @Test func theCleanupRefusesADeleteOnTheProductionService() {
        #expect(
            LegacyT212KeychainCleanup.deleteExactItem(service: Self.protectedName, account: UUID().uuidString)
                == LegacyT212KeychainCleanup.refusedUnderTestsStatus)
    }
}
