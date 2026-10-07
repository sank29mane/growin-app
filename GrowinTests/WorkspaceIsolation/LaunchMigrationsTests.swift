import Foundation
import Testing
@testable import Growin

/// GrowinTests is app-hosted: the test runner launches the real Growin.app, so
/// `GrowinApp.init` runs inside every test run. These tests prove that launch
/// skips the migrations instead of touching the developer's real Keychain.
/// None of them calls a real migration step.
struct LaunchMigrationsTests {
    private final class Counter: @unchecked Sendable {
        var order: [String] = []
    }

    private static func countingSteps(_ counter: Counter) -> LaunchMigrations.Steps {
        LaunchMigrations.Steps(
            legacyUserDefaults: { counter.order.append("defaults") },
            flatItemsToScoped: { counter.order.append("flat") },
            legacyT212Cleanup: { counter.order.append("cleanup") })
    }

    @Test func thisTestProcessIsRecognisedAsATestHost() {
        #expect(LaunchMigrations.isTestHosted())
    }

    @Test func eachTestHostSignalAloneIsEnough() {
        for key in LaunchMigrations.testHostEnvironmentKeys {
            #expect(LaunchMigrations.isTestHosted(environment: [key: "x"], xctestCaseClassLoaded: false), "\(key)")
        }
        #expect(LaunchMigrations.isTestHosted(environment: [:], xctestCaseClassLoaded: true))
    }

    @Test func aNormalLaunchIsNotATestHost() {
        let normal = ["HOME": "/Users/x", "PATH": "/usr/bin", "__CFBundleIdentifier": "san.Growin"]
        #expect(!LaunchMigrations.isTestHosted(environment: normal, xctestCaseClassLoaded: false))
        #expect(!LaunchMigrations.isTestHosted(environment: [:], xctestCaseClassLoaded: false))
    }

    @Test func aTestHostedLaunchRunsNoMigrationStep() {
        let counter = Counter()
        LaunchMigrations.runAtLaunch(testHosted: true, steps: Self.countingSteps(counter))
        #expect(counter.order.isEmpty)
    }

    @Test func theDefaultGuardSkipsEveryStepInsideTheTestRunner() {
        // No testHosted argument: this is the exact path GrowinApp.init takes.
        let counter = Counter()
        LaunchMigrations.runAtLaunch(steps: Self.countingSteps(counter))
        #expect(counter.order.isEmpty)
    }

    @Test func aNormalLaunchRunsAllStepsInOrder() {
        let counter = Counter()
        LaunchMigrations.runAtLaunch(testHosted: false, steps: Self.countingSteps(counter))
        #expect(counter.order == ["cleanup", "defaults", "flat"])
    }

    // MARK: Source guards

    /// Direct calls to a launch migration. Needles are assembled so this file does not trip its own scan.
    private static let migrationCalls = [
        "migrateLegacy" + "UserDefaults(",
        "migrateFlat" + "ItemsToScoped(",
        "LegacyT212KeychainCleanup." + "run" + "Once(",
    ]

    /// Files in Growin/ allowed to mention a migration call: the guarded entry point and the definitions.
    private static let allowedCallers: Set<String> = [
        "Growin/Security/LaunchMigrations.swift",
        "Growin/Security/KeychainStore.swift",
        "Growin/Security/LegacyT212KeychainCleanup.swift",
    ]

    private static func unguardedCallers(in sources: [(path: String, text: String)]) -> [String] {
        sources
            .filter { !allowedCallers.contains($0.path) }
            .filter { source in migrationCalls.contains { source.text.contains($0) } }
            .map(\.path)
    }

    @Test func appLaunchReachesMigrationsOnlyThroughTheGuardedEntryPoint() throws {
        let app = try SourceTree.contents("Growin/GrowinApp.swift")
        let initBody = try #require(app.components(separatedBy: "init() {").dropFirst().first)
        #expect(String(initBody.prefix(900)).contains("LaunchMigrations.runAtLaunch()"))

        let sources = try SourceTree.swiftSources(under: "Growin")
        #expect(sources.contains { $0.path == "Growin/GrowinApp.swift" }, "positive control")
        #expect(Self.unguardedCallers(in: sources) == [])
    }

    @Test func theEntryPointChecksTheGuardBeforeAnyStep() throws {
        let text = try SourceTree.contents("Growin/Security/LaunchMigrations.swift")
        let body = try #require(text.components(separatedBy: "static func runAtLaunch(").dropFirst().first)
        let guardAt = try #require(body.range(of: "guard !testHosted else { return }"))
        for step in ["steps.legacyT212Cleanup()", "steps.legacyUserDefaults()", "steps.flatItemsToScoped()"] {
            let stepAt = try #require(body.range(of: step), "\(step) missing")
            #expect(guardAt.lowerBound < stepAt.lowerBound, "\(step) runs before the guard")
        }
    }

    @Test func theCallerScanCatchesPlantedBypasses() {
        let direct = "KeychainStore.shared." + "migrateLegacy" + "UserDefaults()"
        let flat = "_ = store." + "migrateFlat" + "ItemsToScoped()"
        let cleanup = "LegacyT212KeychainCleanup." + "runOnce(service: s)"
        let planted: [(path: String, text: String)] = [
            ("Growin/GrowinApp.swift", direct),
            ("Growin/Views/X.swift", flat),
            ("Growin/Views/Y.swift", cleanup),
            ("Growin/Views/Z.swift", "let fine = 1"),
            ("Growin/Security/LaunchMigrations.swift", direct),
        ]
        #expect(Self.unguardedCallers(in: planted) == ["Growin/GrowinApp.swift", "Growin/Views/X.swift", "Growin/Views/Y.swift"])
    }
}
