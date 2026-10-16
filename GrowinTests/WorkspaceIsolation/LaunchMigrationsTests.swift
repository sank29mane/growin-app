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
    /// Every way to hold the shared store or the type as a value, with or without a member call.
    /// An alias such as `let s = <store>.shared` never spells the dotted call, so the bare name
    /// is matched, tolerant of spaces and line breaks, and not when a quote or identifier precedes
    /// it (a string needle, or another type). `tests/backend/test_swift_scan_bypasses.py` is the
    /// primary guard and holds the fuller rule set with the same rules; this is its in-Xcode mirror.
    private static let storeValuePattern = #"(?<![A-Za-z0-9_"])KeychainStore\s*\.\s*(?:shared|self|init)\b"#
    private static let storeExtensionPattern = #"extension\s+KeychainStore\b"#
    private static let storeTypealiasPattern = #"typealias\s+\w+\s*=\s*KeychainStore\b"#
    /// The same names, spelled out for the planted samples.
    private static let storeValueNeedles = [
        "KeychainStore" + ".shared",
        "KeychainStore" + ".self",
        "KeychainStore" + ".init",
    ]
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

    /// `.shared` written without a type in front of it takes its type from context, so a scan cannot
    /// tell a Keychain store from anything else: a helper call, an assignment, a getter, a return.
    /// The Keychain scope enum case is the one allowed use, in the four shapes below. Comment lines
    /// and text inside a string literal are skipped (an odd number of quotes earlier on the line).
    private static let scopeContexts = [
        #"scope\s*:\s*\.shared\b"#,
        #"[=!]=\s*\.shared\b"#,
        #"allows\(\s*\.shared\b"#,
        #"case\s+\.shared\b"#,
    ]
    private static let keywordsBeforeImplicitMember: Set<String> = ["return", "in", "else", "try", "await", "throw"]

    private static func implicitSharedUses(in text: String) -> [String] {
        var found: [String] = []
        for rawLine in text.components(separatedBy: "\n") {
            if rawLine.trimmingCharacters(in: .whitespaces).hasPrefix("//") { continue }
            var line = rawLine.replacingOccurrences(of: "`", with: "")
            for context in scopeContexts {
                line = line.replacingOccurrences(of: context, with: " ", options: .regularExpression)
            }
            var searchStart = line.startIndex
            while let hit = line.range(
                of: #"\.\s*shared\b"#, options: .regularExpression, range: searchStart..<line.endIndex)
            {
                searchStart = hit.upperBound
                let prefix = String(line[..<hit.lowerBound])
                let quotes = prefix.replacingOccurrences(of: "\\\"", with: "").filter { $0 == "\"" }.count
                if quotes % 2 == 1 { continue }
                let before = prefix.trimmingCharacters(in: .whitespaces)
                if let last = before.last, last.isLetter || last.isNumber || last == "_" || last == ")" || last == "]" {
                    // `Type.shared` is a member access. A keyword in front still takes an implicit member.
                    let word = String(
                        before.reversed().prefix(while: { $0.isLetter || $0.isNumber || $0 == "_" }).reversed())
                    if !keywordsBeforeImplicitMember.contains(word) { continue }
                }
                found.append(String(before.suffix(24)))
            }
        }
        return found
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
            // Backticks are dropped first, so a backticked member is read as the plain name.
            let plain = text.replacingOccurrences(of: "`", with: "")
            if plain.range(of: storeValuePattern, options: .regularExpression) != nil {
                findings.append("\(path): reaches the shared Keychain store")
            }
            if text.range(of: storeExtensionPattern, options: .regularExpression) != nil {
                findings.append("\(path): extends the Keychain store")
            }
            if text.range(of: storeTypealiasPattern, options: .regularExpression) != nil {
                findings.append("\(path): aliases the Keychain store type")
            }
            for use in implicitSharedUses(in: text) {
                findings.append("\(path): implicit shared member, type from context (\(use))")
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
            ("alias of the shared store", "let store = " + Self.storeValueNeedles[0] + "\nstore.migrateFlatItemsToScoped()"),
            ("type taken as a value", "let kind = " + Self.storeValueNeedles[1]),
            ("initializer taken as a value", "let make = " + Self.storeValueNeedles[2]),
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
            "let own = KeychainStore(service: unique)",
            "#expect(throws: KeychainStoreError.self) {}",
            "line.contains(\"" + Self.storeValueNeedles[1] + "\")",
        ]
        for text in fine {
            #expect(Self.productionReach(in: [("T.swift", text)]) == [], "\(text)")
        }
    }

    @Test func theTestSourceScanCatchesImplicitSharedAndAliasForms() {
        let store = "Keychain" + "Store"
        // Codex r1 and Opus verify on #573: `.shared` whose type comes from context, in the shapes
        // the real `cleanUp(_ store: KeychainStore)` helpers accept, plus the spelling variants.
        let samples: [(label: String, text: String)] = [
            ("helper call", "Self.cleanUp(.shared)"),
            ("bare helper call", "cleanUp(.shared)"),
            ("function taking a store", "func migrate(_ s: KeychainStore) {}\nmigrate(.shared)"),
            ("assignment in setUp", "var store: KeychainStore!\nfunc setUp() { store = .shared }"),
            ("other label", "run(on: .shared)"),
            ("implicitly unwrapped default", "var s: KeychainStore! = .shared"),
            ("getter", "var s: KeychainStore { get { .shared } }"),
            ("closure initialiser", "let s: KeychainStore = { .shared }()"),
            ("return", "func f() -> KeychainStore { return .shared }"),
            ("closure with in", "let f: (Int) -> KeychainStore = { _ in .shared }"),
            ("nil coalescing", "let s: KeychainStore = maybe ?? .shared"),
            ("argument on its own line", "cleanUp(\n    .shared\n)"),
            ("name, space, member", "let s = " + store + " .shared"),
            ("dot on the next line", "let s = " + store + "\n    .shared"),
            ("backticked member", "let s = " + store + ".`shared`"),
            ("extension", "extension " + store + " { static var alias: " + store + " { shared } }"),
            ("typealias", "typealias Store = " + store),
        ]
        for sample in samples {
            let findings = Self.productionReach(in: [("T.swift", sample.text)])
            #expect(!findings.isEmpty, "\(sample.label) went undetected")
        }
    }

    @Test func theTestSourceScanLeavesLegitimateSharedUsesAlone() {
        let fine: [String] = [
            "let s = URLSession.shared",
            "store.set(v, for: .x, scope: .shared)",
            "#expect(name.policy == .shared)",
            "#expect(!policy.allows(.shared))",
            "case .shared: return true",
            "ApprovalSignerRouter.shared.sign(x)",
            "make().shared",
            "// cleanUp(.shared)",
            "let n = \"cleanUp(.shared)\"",
            "let own = KeychainStore(service: unique)\ncleanUp(own)",
            "extension KeychainStoreError { var x: Int { 1 } }",
        ]
        for text in fine {
            #expect(Self.productionReach(in: [("T.swift", text)]) == [], "\(text)")
        }
    }

    // MARK: Source guard on GrowinUITests

    private static let uiHelperPath = "GrowinUITests/LaunchSupport.swift"

    /// The helper's source with whole-line `//` comments removed.
    private static func codeLines(_ text: String) -> String {
        text.components(separatedBy: "\n")
            .filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix("//") }
            .joined(separator: "\n")
    }

    /// The whole body of `makeApp` must be: create the app, set the suppression key on it, return
    /// it. A substring check would pass an early `return .init(bundleIdentifier:)` placed before an
    /// honest tail. Outside `makeApp` the helper may not name the application type or call an
    /// initializer. Mirrors `helper_offences` in tests/backend/test_swift_scan_bypasses.py.
    private static func helperShapeProblems(_ helperText: String, key: String) -> [String] {
        let appType = "XCUI" + "Application"
        let code = codeLines(helperText)
        let parts = code.components(separatedBy: "func makeApp() -> " + appType + " {")
        guard parts.count == 2 else { return ["makeApp must be declared exactly once"] }
        var depth = 1
        var closed = false
        var body = ""
        var rest = ""
        for character in parts[1] {
            if closed {
                rest.append(character)
                continue
            }
            if character == "{" { depth += 1 }
            if character == "}" {
                depth -= 1
                if depth == 0 {
                    closed = true
                    continue
                }
            }
            body.append(character)
        }
        var problems: [String] = []
        let escapedKey = NSRegularExpression.escapedPattern(for: key)
        let shape = #"^\s*let (\w+) = "# + appType + #"\(\)\s*\1\.launchEnvironment\[""# + escapedKey
            + #""\] = "1"\s*return \1\s*$"#
        if body.range(of: shape, options: .regularExpression) == nil {
            problems.append("makeApp body is not exactly create, set the suppression key, return")
        }
        let outside = parts[0] + rest
        if outside.contains(appType) { problems.append("names the application type outside makeApp") }
        if outside.range(of: #"\.\s*init\b"#, options: .regularExpression) != nil {
            problems.append("calls an initializer outside makeApp")
        }
        return problems
    }

    /// A helper source with the given makeApp body lines.
    private static func helperSource(lines: [String]) -> String {
        let appType = "XCUI" + "Application"
        let head = "enum GrowinLaunch {\n    static func makeApp() -> " + appType + " {\n"
        let body = lines.map { "        " + $0 }.joined(separator: "\n")
        return head + body + "\n    }\n}\n"
    }

    private static let goodMakeAppBody: [String] = [
        "let app = " + "XCUI" + "Application()",
        "app.launchEnvironment[\"GROWIN_SKIP_LAUNCH_MIGRATIONS\"] = \"1\"",
        "return app",
    ]

    /// UI tests launch the app in its own process, so each one must go through the helper
    /// that sets the suppression key.
    private static func uiLaunchViolations(in sources: [(path: String, text: String)]) -> [String] {
        // The bare type name, not `XCUIApplication(`: `.init()`, `(bundleIdentifier:)`, a typealias
        // and `.self` all name it. Only the helper may. Same for the launch environment, which a
        // test could clear to drop the suppression key.
        let appType = "XCUI" + "Application"
        let environment = "launch" + "Environment"
        let helperAssignment =
            "launchEnvironment[\"\(LaunchMigrations.skipEnvironmentKey)\"] = \"1\""
        var findings: [String] = []
        guard let helper = sources.first(where: { $0.path == uiHelperPath }) else {
            return ["\(uiHelperPath): helper missing"]
        }
        if !helper.text.contains(helperAssignment) {
            findings.append("\(helper.path): does not set the suppression key")
        }
        for problem in helperShapeProblems(helper.text, key: LaunchMigrations.skipEnvironmentKey) {
            findings.append("\(helper.path): \(problem)")
        }
        // Other ways to start the app without the application type.
        let launchers = ["NS" + "Workspace", "NS" + "Task", "posix" + "_spawn", "Pro" + "cess("]
        for (path, text) in sources where path != uiHelperPath {
            if launchers.contains(where: { text.contains($0) }) {
                findings.append("\(path): starts a process without the helper")
            }
            if text.contains(appType) { findings.append("\(path): names the application type outside the helper") }
            if text.contains(environment) { findings.append("\(path): touches the launch environment outside the helper") }
            let launches = text.contains(".launch()") || text.contains(".activate()")
            if launches && !text.contains("GrowinLaunch.makeApp()") {
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

    @Test func theHelperBodyIsMatchedWholeNotAsASubstring() {
        let appType = "XCUI" + "Application"
        let goodTest: (path: String, text: String) = ("GrowinUITests/A.swift", "let app = GrowinLaunch.makeApp(); app.launch()")
        let good = Self.goodMakeAppBody
        #expect(Self.uiLaunchViolations(in: [(Self.uiHelperPath, Self.helperSource(lines: good)), goodTest]) == [])

        // Codex r1 P2 on #573: an unsafe early return in front of the honest tail.
        var earlyInit = good
        earlyInit.insert("if flag { return .init(bundleIdentifier: \"x\") }", at: 0)
        var earlyNamed = good
        earlyNamed.insert("if flag { return " + appType + "() }", at: 0)
        var differentVariable = good
        differentVariable[2] = "return other"
        var inferredReturn = good
        inferredReturn[2] = "return .init()"
        var extraStatement = good
        extraStatement.insert("app.launchArguments = []", at: 2)
        var secondReturn = good
        secondReturn.append("return " + appType + "()")
        var keyOnOtherVariable = good
        keyOnOtherVariable[1] = good[1].replacingOccurrences(of: "app.", with: "other.")
        var keyBeforeApp = good
        keyBeforeApp.swapAt(0, 1)
        let variants: [(label: String, lines: [String])] = [
            ("early inferred init", earlyInit),
            ("early named creation", earlyNamed),
            ("returns a different variable", differentVariable),
            ("returns an inferred init", inferredReturn),
            ("extra statement", extraStatement),
            ("second return", secondReturn),
            ("key set on another variable", keyOnOtherVariable),
            ("key set before the app exists", keyBeforeApp),
        ]
        for variant in variants {
            let source = Self.helperSource(lines: variant.lines)
            #expect(!Self.uiLaunchViolations(in: [(Self.uiHelperPath, source), goodTest]).isEmpty,
                    "\(variant.label) went undetected")
        }
        // A second creator or an inferred initializer beside makeApp.
        let beside = Self.helperSource(lines: good)
            + "extension GrowinLaunch { static func raw() -> " + appType + " { .init() } }\n"
        #expect(!Self.uiLaunchViolations(in: [(Self.uiHelperPath, beside), goodTest]).isEmpty)
        // A comment that names the type is not code.
        let commented = "/// " + appType + "() is named here only in a comment.\n" + Self.helperSource(lines: good)
        #expect(Self.uiLaunchViolations(in: [(Self.uiHelperPath, commented), goodTest]) == [])
    }

    @Test func theUILaunchScanCatchesPlantedViolations() {
        let creator = "XCUI" + "Application("
        let helper: (path: String, text: String) = (Self.uiHelperPath, Self.helperSource(lines: Self.goodMakeAppBody))
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

        // Other ways to name or start an app outside the helper (#566 P3).
        let appType = "XCUI" + "Application"
        let bypasses: [(label: String, text: String)] = [
            ("init call", "let app = " + appType + ".init()"),
            ("bundle identifier", "let app = " + appType + "(bundleIdentifier: \"x\")"),
            ("url initializer", "let app = " + appType + "(url: u)"),
            ("typealias", "typealias App = " + appType),
            ("type value", "let t = " + appType + ".self"),
            ("activate without the helper", "other.activate()"),
            ("reset launch environment", "let app = GrowinLaunch.makeApp(); app.launchEnvironment = [:]"),
        ]
        for bypass in bypasses {
            let found = Self.uiLaunchViolations(in: [helper, ("GrowinUITests/D.swift", bypass.text)])
            #expect(!found.isEmpty, "\(bypass.label) went undetected")
        }
        // Other launchers.
        let launchers: [(label: String, text: String)] = [
            ("workspace", "NS" + "Workspace.shared.open(url)"),
            ("task", "let t = NS" + "Task()"),
            ("process", "let p = Pro" + "cess()"),
            ("spawn", "posix" + "_spawn(&pid, path, nil, nil, args, nil)"),
        ]
        for launcher in launchers {
            let found = Self.uiLaunchViolations(in: [helper, ("GrowinUITests/F.swift", launcher.text)])
            #expect(!found.isEmpty, "\(launcher.label) went undetected")
        }
        // The app that came from the helper may be activated.
        let activated = "let app = GrowinLaunch.makeApp(); app.activate()"
        #expect(Self.uiLaunchViolations(in: [helper, ("GrowinUITests/E.swift", activated)]) == [])
    }
}
