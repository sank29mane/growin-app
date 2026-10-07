import Foundation
import Testing
@testable import Growin

/// GrowinTests is app-hosted: the test runner launches the real Growin.app, so
/// `GrowinApp.init` runs inside every test run. Xcode previews and UI tests launch the
/// app too. These tests prove such launches skip the migrations instead of touching the
/// developer's real Keychain, and they scan the test sources for any path back to it.
/// None of them runs a real migration step.
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

    // MARK: Skip signals

    @Test func thisTestProcessIsSkipped() {
        #expect(LaunchMigrations.shouldSkip())
    }

    @Test func eachTestHostSignalAloneIsEnough() {
        for key in LaunchMigrations.testHostEnvironmentKeys {
            #expect(LaunchMigrations.shouldSkip(environment: [key: "x"], xctestCaseClassLoaded: false), "\(key)")
        }
        #expect(LaunchMigrations.shouldSkip(environment: [:], xctestCaseClassLoaded: true))
    }

    @Test func anXcodePreviewAloneIsEnough() {
        #expect(LaunchMigrations.shouldSkip(
            environment: [LaunchMigrations.previewEnvironmentKey: "1"], xctestCaseClassLoaded: false))
        #expect(LaunchMigrations.previewEnvironmentKey == "XCODE_RUNNING_FOR_PREVIEWS")
        for other in ["0", "", "true"] {
            #expect(!LaunchMigrations.shouldSkip(
                environment: [LaunchMigrations.previewEnvironmentKey: other], xctestCaseClassLoaded: false), "\(other)")
        }
    }

    @Test func theExplicitSuppressionKeyAloneIsEnough() {
        #expect(LaunchMigrations.shouldSkip(
            environment: [LaunchMigrations.skipEnvironmentKey: "1"], xctestCaseClassLoaded: false))
        #expect(LaunchMigrations.skipEnvironmentKey == "GROWIN_SKIP_LAUNCH_MIGRATIONS")
        for other in ["0", "", "true"] {
            #expect(!LaunchMigrations.shouldSkip(
                environment: [LaunchMigrations.skipEnvironmentKey: other], xctestCaseClassLoaded: false), "\(other)")
        }
    }

    @Test func aNormalLaunchIsNotSkipped() {
        let normal = ["HOME": "/Users/x", "PATH": "/usr/bin", "__CFBundleIdentifier": "san.Growin"]
        #expect(!LaunchMigrations.shouldSkip(environment: normal, xctestCaseClassLoaded: false))
        #expect(!LaunchMigrations.shouldSkip(environment: [:], xctestCaseClassLoaded: false))
    }

    // MARK: Entry point

    @Test func aSkippedLaunchRunsNoMigrationStep() {
        let counter = Counter()
        LaunchMigrations.runAtLaunch(skip: true, steps: Self.countingSteps(counter))
        #expect(counter.order.isEmpty)
    }

    @Test func theRealSkipDecisionSkipsEveryStepInsideTheTestRunner() {
        // The same decision the app makes at launch, with counting steps instead of real ones.
        let counter = Counter()
        LaunchMigrations.runAtLaunch(skip: LaunchMigrations.shouldSkip(), steps: Self.countingSteps(counter))
        #expect(counter.order.isEmpty)
    }

    @Test func aNormalLaunchRunsAllStepsInOrder() {
        let counter = Counter()
        LaunchMigrations.runAtLaunch(skip: false, steps: Self.countingSteps(counter))
        #expect(counter.order == ["cleanup", "defaults", "flat"])
    }

    // MARK: Source guards on the app

    // Needles are assembled so this file does not trip its own scans.
    private static let atLaunch = "run" + "AtLaunch("
    private static let sharedStore = "KeychainStore" + ".shared."
    private static let productionSteps = "." + "production" + "()"
    private static let quotedProductionService = "\"" + "san.Growin.credentials." + "v1" + "\""
    private static let cleanupCall = "LegacyT212KeychainCleanup." + "run" + "Once("

    /// Direct calls to a launch migration.
    private static let migrationCalls = [
        "migrateLegacy" + "UserDefaults(",
        "migrateFlat" + "ItemsToScoped(",
        cleanupCall,
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
        #expect(String(initBody.prefix(900)).contains("LaunchMigrations." + Self.atLaunch + ")"))

        let sources = try SourceTree.swiftSources(under: "Growin")
        #expect(sources.contains { $0.path == "Growin/GrowinApp.swift" }, "positive control")
        #expect(Self.unguardedCallers(in: sources) == [])
    }

    @Test func theEntryPointChecksTheGuardBeforeAnyStep() throws {
        let text = try SourceTree.contents("Growin/Security/LaunchMigrations.swift")
        let body = try #require(text.components(separatedBy: "static func " + Self.atLaunch + "skip:").dropFirst().first)
        let guardAt = try #require(body.range(of: "guard !skip else { return }"))
        for step in ["steps.legacyT212Cleanup()", "steps.legacyUserDefaults()", "steps.flatItemsToScoped()"] {
            let stepAt = try #require(body.range(of: step), "\(step) missing")
            #expect(guardAt.lowerBound < stepAt.lowerBound, "\(step) runs before the guard")
        }
    }

    @Test func theInjectableOverloadHasNoDefaults() throws {
        let text = try SourceTree.contents("Growin/Security/LaunchMigrations.swift")
        let signature = try #require(text.components(separatedBy: "static func " + Self.atLaunch + "skip:").dropFirst().first)
        let declaration = String(signature.prefix(while: { $0 != "{" }))
        #expect(!declaration.contains("="), "the injectable overload must not default skip or steps: \(declaration)")
    }

    // MARK: Source guards on the tests

    /// The text inside the parentheses of each call that starts with `needle`.
    private static func callArguments(of needle: String, in text: String) -> [String] {
        var results: [String] = []
        var rest = Substring(text)
        while let found = rest.range(of: needle) {
            var depth = 1
            var args = ""
            var index = found.upperBound
            while index < rest.endIndex, depth > 0 {
                let character = rest[index]
                if character == "(" { depth += 1 }
                if character == ")" { depth -= 1 }
                if depth > 0 { args.append(character) }
                index = rest.index(after: index)
            }
            results.append(args)
            rest = rest[found.upperBound...]
        }
        return results
    }

    /// Every way a test source could get back to the operator's real Keychain through a launch migration.
    private static func productionReach(in sources: [(path: String, text: String)]) -> [String] {
        var findings: [String] = []
        for (path, text) in sources {
            if text.contains(quotedProductionService) {
                findings.append("\(path): names the production service literal")
            }
            if text.contains(productionSteps) {
                findings.append("\(path): uses the production steps")
            }
            var rest = Substring(text)
            while let found = rest.range(of: sharedStore) {
                let before = rest[..<found.lowerBound].last
                if before != "\"" { findings.append("\(path): calls the shared Keychain store") }
                rest = rest[found.upperBound...]
            }
            for args in callArguments(of: atLaunch, in: text) where !args.contains("steps:") {
                findings.append("\(path): calls the launch entry point without injected steps")
            }
        }
        return findings
    }

    @Test func noTestSourceCanReachTheRealKeychainThroughALaunchMigration() throws {
        let sources = try SourceTree.swiftSources(under: "GrowinTests")
        #expect(sources.contains { $0.path.hasSuffix("LaunchMigrationsTests.swift") }, "positive control")
        #expect(Self.productionReach(in: sources) == [])
    }

    @Test func theTestSourceScanCatchesEveryPlantedProductionCall() {
        let at = Self.atLaunch
        let samples: [(label: String, text: String)] = [
            ("shared store call", "func f() { _ = " + Self.sharedStore + "migrateLegacyUserDefaults() }"),
            ("flat migration call", "let x = " + Self.sharedStore + "migrateFlatItemsToScoped()"),
            ("production steps", "LaunchMigrations." + at + "skip: false, steps: " + Self.productionSteps + ")"),
            ("zero-argument entry point", "LaunchMigrations." + at + ")"),
            ("entry point without steps", "LaunchMigrations." + at + "skip: false)"),
            ("production service literal", Self.cleanupCall + "service: " + Self.quotedProductionService + ")"),
        ]
        for sample in samples {
            let findings = Self.productionReach(in: [("T.swift", sample.text)])
            #expect(!findings.isEmpty, "\(sample.label) went undetected")
        }
    }

    @Test func theTestSourceScanLeavesLegitimateTextAlone() {
        let at = Self.atLaunch
        let fine: [String] = [
            "line.contains(\"" + Self.sharedStore + "\")",
            "LaunchMigrations." + at + "skip: false, steps: counting)",
            "LaunchMigrations." + at + "skip: true,\n    steps: Self.countingSteps(counter))",
            "let service = \"san.Growin.credentials." + "v1.test.\\(UUID().uuidString)\"",
        ]
        for text in fine {
            #expect(Self.productionReach(in: [("T.swift", text)]) == [], "\(text)")
        }
    }

    // MARK: Source guard on GrowinUITests

    private static let uiHelperPath = "GrowinUITests/LaunchSupport.swift"

    /// UI tests launch the app in its own process, so each one must go through the helper
    /// that sets the suppression key.
    private static func uiLaunchViolations(in sources: [(path: String, text: String)]) -> [String] {
        let creator = "XCUI" + "Application("
        let helperAssignment =
            "launchEnvironment[\"\(LaunchMigrations.skipEnvironmentKey)\"] = \"1\""
        var findings: [String] = []
        guard let helper = sources.first(where: { $0.path == uiHelperPath }) else {
            return ["\(uiHelperPath): helper missing"]
        }
        if !helper.text.contains(helperAssignment) {
            findings.append("\(helper.path): does not set the suppression key")
        }
        for (path, text) in sources where path != uiHelperPath {
            if text.contains(creator) { findings.append("\(path): creates an application outside the helper") }
            if text.contains(".launch()") && !text.contains("GrowinLaunch.makeApp()") {
                findings.append("\(path): launches an app that did not come from the helper")
            }
        }
        return findings
    }

    @Test func everyUILaunchSetsTheSuppressionKey() throws {
        let sources = try SourceTree.swiftSources(under: "GrowinUITests")
        #expect(sources.contains { $0.path != Self.uiHelperPath && $0.text.contains(".launch()") },
                "positive control: at least one UI test launches the app")
        #expect(Self.uiLaunchViolations(in: sources) == [])
    }

    @Test func theUILaunchScanCatchesPlantedViolations() {
        let creator = "XCUI" + "Application("
        let goodHelper = "launchEnvironment[\"GROWIN_SKIP_LAUNCH_MIGRATIONS\"] = \"1\""
        let helper: (path: String, text: String) = (Self.uiHelperPath, goodHelper + " " + creator + ")")
        let goodTest: (path: String, text: String) = ("GrowinUITests/A.swift", "let app = GrowinLaunch.makeApp(); app.launch()")

        #expect(Self.uiLaunchViolations(in: [helper, goodTest]) == [])
        // A test creating its own application.
        #expect(!Self.uiLaunchViolations(in: [helper, ("GrowinUITests/B.swift", "let app = " + creator + "); app.launch()")]).isEmpty)
        // A launch that did not come from the helper.
        #expect(!Self.uiLaunchViolations(in: [helper, ("GrowinUITests/C.swift", "app.launch()")]).isEmpty)
        // A helper that forgot the key, or sets a different one.
        #expect(!Self.uiLaunchViolations(in: [(Self.uiHelperPath, creator + ")"), goodTest]).isEmpty)
        #expect(!Self.uiLaunchViolations(in: [(Self.uiHelperPath, "launchEnvironment[\"OTHER\"] = \"1\""), goodTest]).isEmpty)
        // No helper at all.
        #expect(!Self.uiLaunchViolations(in: [goodTest]).isEmpty)
    }
}
