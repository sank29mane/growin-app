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
    /// The shared store or the type as a value, member call or not: a test never needs either. An
    /// alias such as `let s = <store>.shared` never spells the dotted call, so the bare name is
    /// matched, tolerant of spaces and line breaks. Every pattern here is read on reduced code
    /// (`SwiftReader`), so string needles and comments are not findings and interpolations are.
    /// `tests/backend/test_swift_scan_bypasses.py` is the primary guard and holds the same rules;
    /// this is its in-Xcode mirror.
    private static let storeValuePattern = #"(?<![A-Za-z0-9_])KeychainStore\s*\.\s*(?:shared|self|init)(?![A-Za-z0-9_])"#
    private static let storeExtensionPattern = #"\bextension\s+KeychainStore(?![A-Za-z0-9_])"#
    private static let storeTypealiasPattern = #"\btypealias\s+\w+\s*(?:<[^>]*>)?\s*=\s*KeychainStore(?![A-Za-z0-9_])"#
    private static let storeArgumentPattern = #"(?<![A-Za-z0-9_])store\s*:\s*\.shared(?![A-Za-z0-9_])"#
    /// The service constant of the shared store, assembled so this file never spells its name (the
    /// scan in LegacyT212KeychainCleanupTests bans that name anywhere in GrowinTests). Tests never
    /// need it: a store built on it is the shared store.
    private static let serviceConstantPattern = #"(?<![A-Za-z0-9_])"# + "production" + "Service" + #"(?![A-Za-z0-9_])"#
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

    // MARK: Reading Swift (mirrors `reduce_swift` in tests/backend/test_swift_scan_bypasses.py)

    /// One string literal: where its `""` sits in the reduced code (UTF-16 offsets), its kind
    /// (plain, multiline or raw) and its exact source text, delimiters included.
    struct SwiftLiteral {
        let start: Int
        let end: Int
        let kind: String
        let source: String
    }

    struct ReducedSwift {
        let bare: String
        let literals: [SwiftLiteral]
        let errors: [String]
    }

    /// Reads Swift source in one pass, the way the compiler splits code from text. Comments go
    /// (block comments nest). Each string literal becomes `""`, then ` (expression) ` for every
    /// interpolation in it, so the code inside `\(...)`, and inside `\#(...)` of a raw string, stays
    /// visible. In a raw string with n `#`, only a backslash followed by n `#` escapes or
    /// interpolates. Backticks are dropped from code. Anything that does not close is recorded in
    /// `errors`, and the scans refuse the file instead of trusting the rest.
    final class SwiftReader {
        private static let newline: UInt16 = 10
        private static let quote: UInt16 = 34
        private static let hash: UInt16 = 35
        private static let openParen: UInt16 = 40
        private static let closeParen: UInt16 = 41
        private static let star: UInt16 = 42
        private static let slash: UInt16 = 47
        private static let backslash: UInt16 = 92
        private static let backtick: UInt16 = 96

        private let text: [UInt16]
        private var out: [UInt16] = []
        private var literals: [SwiftLiteral] = []
        private var errors: [String] = []

        init(_ source: String) {
            text = Array(source.utf16)
        }

        func read() -> ReducedSwift {
            _ = code(from: 0, untilClose: false)
            return ReducedSwift(bare: String(decoding: out, as: UTF16.self), literals: literals, errors: errors)
        }

        private func starts(_ pattern: [UInt16], at index: Int) -> Bool {
            guard index >= 0, index + pattern.count <= text.count else { return false }
            for offset in 0..<pattern.count where text[index + offset] != pattern[offset] {
                return false
            }
            return true
        }

        private func emit(_ piece: String) {
            out.append(contentsOf: piece.utf16)
        }

        private func line(_ index: Int) -> Int {
            text[..<min(index, text.count)].filter { $0 == Self.newline }.count + 1
        }

        private func blockComment(from start: Int) -> Int {
            var i = start
            var depth = 0
            while i < text.count {
                if starts([Self.slash, Self.star], at: i) {
                    depth += 1
                    i += 2
                } else if starts([Self.star, Self.slash], at: i) {
                    depth -= 1
                    i += 2
                    if depth == 0 {
                        emit(" ")
                        return i
                    }
                } else {
                    if text[i] == Self.newline { out.append(Self.newline) }
                    i += 1
                }
            }
            errors.append("unterminated block comment at line \(line(start))")
            return i
        }

        private func code(from start: Int, untilClose: Bool) -> Int {
            var i = start
            var depth = 0
            while i < text.count {
                let unit = text[i]
                if starts([Self.slash, Self.slash], at: i) {
                    while i < text.count, text[i] != Self.newline { i += 1 }
                    emit(" ")
                    continue
                }
                if starts([Self.slash, Self.star], at: i) {
                    i = blockComment(from: i)
                    continue
                }
                if unit == Self.hash {
                    var after = i
                    while after < text.count, text[after] == Self.hash { after += 1 }
                    if after < text.count, text[after] == Self.quote {
                        let multiline = starts([Self.quote, Self.quote, Self.quote], at: after)
                        i = literal(
                            start: i, body: after + (multiline ? 3 : 1), hashes: after - i,
                            multiline: multiline, kind: "raw")
                        continue
                    }
                }
                if starts([Self.quote, Self.quote, Self.quote], at: i) {
                    i = literal(start: i, body: i + 3, hashes: 0, multiline: true, kind: "multiline")
                    continue
                }
                if unit == Self.quote {
                    i = literal(start: i, body: i + 1, hashes: 0, multiline: false, kind: "plain")
                    continue
                }
                if unit == Self.backtick {
                    i += 1
                    continue
                }
                if untilClose {
                    if unit == Self.openParen {
                        depth += 1
                    } else if unit == Self.closeParen {
                        if depth == 0 { return i + 1 }
                        depth -= 1
                    }
                }
                out.append(unit)
                i += 1
            }
            if untilClose { errors.append("unterminated interpolation at line \(line(start))") }
            return i
        }

        private func literal(start: Int, body: Int, hashes: Int, multiline: Bool, kind: String) -> Int {
            let begin = out.count
            emit("\"\"")
            let marks = [UInt16](repeating: Self.hash, count: hashes)
            let close = (multiline ? [Self.quote, Self.quote, Self.quote] : [Self.quote]) + marks
            let escape = [Self.backslash] + marks
            var i = body
            var newlines = 0
            var closed = false
            while i < text.count {
                if starts(escape, at: i) {
                    let after = i + escape.count
                    if after < text.count, text[after] == Self.openParen {
                        emit(" (")
                        i = code(from: after + 1, untilClose: true)
                        emit(") ")
                        continue
                    }
                    if after < text.count, text[after] == Self.newline {
                        if !multiline { break }
                        newlines += 1
                    }
                    i = after + 1
                    continue
                }
                if starts(close, at: i) {
                    i += close.count
                    closed = true
                    break
                }
                if text[i] == Self.newline {
                    if !multiline { break }
                    newlines += 1
                }
                i += 1
            }
            if !closed { errors.append("unterminated \(kind) string literal at line \(line(start))") }
            emit(String(repeating: "\n", count: newlines))
            let end = min(i, text.count)
            literals.append(SwiftLiteral(
                start: begin, end: out.count, kind: kind,
                source: String(decoding: text[start..<end], as: UTF16.self)))
            return i
        }
    }

    /// A pattern that does not compile is itself a failure, so a typo cannot switch a rule off.
    private static func compiled(_ pattern: String) -> NSRegularExpression? {
        do {
            return try NSRegularExpression(pattern: pattern)
        } catch {
            Issue.record("scan pattern does not compile: \(pattern)")
            return nil
        }
    }

    private static func matches(_ pattern: String, in text: String) -> [NSTextCheckingResult] {
        guard let regex = compiled(pattern) else { return [] }
        return regex.matches(in: text, range: NSRange(location: 0, length: (text as NSString).length))
    }

    /// The match of `pattern` that starts exactly at `location`, searching no further than `end`.
    private static func anchoredMatch(_ pattern: String, in text: String, at location: Int, upTo end: Int? = nil) -> NSRange? {
        guard let regex = compiled(pattern) else { return nil }
        let length = (text as NSString).length
        let stop = end ?? length
        guard location >= 0, location <= stop, stop <= length else { return nil }
        let range = NSRange(location: location, length: stop - location)
        return regex.firstMatch(in: text, options: .anchored, range: range)?.range
    }

    /// A newline does not end a declaration when the line ends in one of these, or the next line
    /// starts with one of the second set. Inside `(`, `[` or a generic `<` it never does.
    private static let continuesAfter: String =
        #"(?:[,:=\&.<|]|->|@\w+|(?<![A-Za-z0-9_])(?:func|var|let|static|class|final|private|fileprivate|internal|public|mutating"#
        + #"|nonmutating|override|throws|rethrows|async|inout|some|any|where|borrowing|consuming|sending))\s*$"#
    private static let continuesBefore: String =
        #"\s*(?:[(.\&<=|?:+*%^~\-]|(?:where|throws|rethrows|async)(?![A-Za-z0-9_]))"#

    /// Reduced code with each multi-line declaration joined onto one line, so the one-line
    /// declaration patterns see `func cleanUp(scope store: Optional<` and the type on the next line
    /// as one declaration. Mirrors `logical_lines` in the Python guard.
    static func logicalLines(_ bare: String) -> String {
        let units = Array(bare.utf16)
        let total = units.count
        guard let after = compiled(continuesAfter), let before = compiled(continuesBefore) else { return bare }
        func isIdentifier(_ unit: UInt16) -> Bool {
            switch unit {
            case 48...57, 65...90, 95, 97...122: return true  // 0-9 A-Z _ a-z
            default: return unit >= 128
            }
        }
        var out: [UInt16] = []
        var current: [UInt16] = []
        var stack: [UInt16] = []
        for (i, unit) in units.enumerated() {
            if unit == 10 {
                let tail = String(decoding: current.suffix(48), as: UTF16.self)
                let tailRange = NSRange(location: 0, length: (tail as NSString).length)
                let insideBrackets = stack.last.map { $0 == 40 || $0 == 91 || $0 == 60 } ?? false
                let endsOpen = after.firstMatch(in: tail, range: tailRange) != nil
                let nextRange = NSRange(location: i + 1, length: total - i - 1)
                let nextOpens = before.firstMatch(in: bare, options: .anchored, range: nextRange) != nil
                if insideBrackets || endsOpen || nextOpens {
                    current.append(32)
                } else {
                    out.append(contentsOf: current)
                    out.append(10)
                    current = []
                }
                continue
            }
            let previous: UInt16? = i > 0 ? units[i - 1] : nil
            switch unit {
            case 40, 91:  // ( [
                stack.append(unit)
            case 123:  // {
                while stack.last == 60 { stack.removeLast() }
                stack.append(unit)
            case 60:  // <, a generic only when written right after a name
                if let previous, isIdentifier(previous) { stack.append(unit) }
            case 62:  // >, but not the one in ->
                if stack.last == 60, previous != 45 { stack.removeLast() }
            case 41, 93, 125:  // ) ] }
                while stack.last == 60 { stack.removeLast() }
                let opener: UInt16 = unit == 41 ? 40 : (unit == 93 ? 91 : 123)
                if stack.last == opener { stack.removeLast() }
            case 59, 61:  // ; =
                while stack.last == 60 { stack.removeLast() }
            default:
                break
            }
            current.append(unit)
        }
        out.append(contentsOf: current)
        return String(decoding: out, as: UTF16.self)
    }

    /// `.shared` written without a type in front of it takes its type from context, so a scan cannot
    /// tell a Keychain store from anything else: a helper call, an assignment, a getter, a return.
    /// The Keychain scope enum case is the one allowed use, in the three shapes below.
    private static let scopeContexts: String =
        #"(?<![A-Za-z0-9_])scope\s*:\s*\.shared(?![A-Za-z0-9_])"#
        + #"|\.\s*policy\s*[=!]=\s*\.shared(?![A-Za-z0-9_])"#
        + #"|\.\s*policy\s*\.\s*allows\s*\(\s*\.shared(?![A-Za-z0-9_])"#
    private static let keywordsBeforeImplicitMember: Set<String> = [
        "return", "in", "else", "try", "await", "throw", "yield", "case",
    ]
    /// Declarations that could make the exempt shapes above mean the shared store: a parameter or
    /// property labelled scope whose type is the store, a generic bound to the store, and a custom
    /// equality, pattern match, `allows` or `policy` taking or holding one. Read on logical lines.
    private static let scopeShapedDeclarations = [
        #"(?<![A-Za-z0-9_])scope(?:\s+\w+)?\s*:\s*[^,)\n]*?(?<![A-Za-z0-9_])KeychainStore(?![A-Za-z0-9_])"#,
        #"<\s*\w+\s*:\s*KeychainStore(?![A-Za-z0-9_])|\bwhere\b[^{\n]*\b\w+\s*(?::|==)\s*KeychainStore(?![A-Za-z0-9_])"#,
        #"\b(?:func|var|let)\s*(?:==|!=|~=|allows\b|policy\b)[^{\n]*?(?<![A-Za-z0-9_])KeychainStore(?![A-Za-z0-9_])"#,
    ]

    private static func implicitSharedUses(in bare: String) -> [String] {
        let code = bare.replacingOccurrences(of: scopeContexts, with: " ", options: .regularExpression)
        let ns = code as NSString
        var found: [String] = []
        for hit in matches(#"\.\s*shared(?![A-Za-z0-9_])"#, in: code) {
            var before = Substring(ns.substring(to: hit.range.location))
            while let last = before.last, last.isWhitespace { before.removeLast() }
            // `store: .shared` is left to the argument check.
            if before.range(of: #"(?<![A-Za-z0-9_])store\s*:$"#, options: .regularExpression) != nil { continue }
            if let last = before.last, last.isLetter || last.isNumber || last == "_" || last == ")" || last == "]" {
                // `Type.shared` is a member access. A keyword in front still takes an implicit member.
                let word = String(
                    before.reversed().prefix(while: { $0.isLetter || $0.isNumber || $0 == "_" }).reversed())
                if !keywordsBeforeImplicitMember.contains(word) { continue }
            }
            found.append(String(before.suffix(24)))
        }
        return found
    }

    /// Every store a test builds takes one plain literal under the test prefix. The only
    /// interpolation allowed is the `UUID().uuidString` every real call site uses, after the whole
    /// prefix, so the value can never be the shared store's service.
    private static let testServiceLiteral =
        #"^"san\.Growin\.credentials\.v1\.test\.[A-Za-z0-9._-]*(?:\\\(UUID\(\)\.uuidString\)[A-Za-z0-9._-]*)?"\z"#
    /// The store's initializer, and any `.init(service:` (implicit, `type(of:)`, a metatype).
    private static let storeConstruction =
        #"(?<![A-Za-z0-9_])KeychainStore\s*(?:\.\s*init\s*)?\(|(?<![A-Za-z0-9_])init\s*\((?=\s*service\s*:)"#
    /// The one identifier form allowed: `let name = "<test literal>"` and, as the very next
    /// statement, `let x = <store>(service: name)` or `return (<store>(service: name), ...)`.
    private static let serviceBindingGap =
        #"\s*;?\s*(?:(?:let|var)\s+[A-Za-z_]\w*\s*(?::\s*KeychainStore\s*)?=\s*|return\s*\(?\s*)\z"#

    private static func isTestServiceLiteral(_ literal: SwiftLiteral) -> Bool {
        literal.kind == "plain" && literal.source.range(of: testServiceLiteral, options: .regularExpression) != nil
    }

    private static func boundToTestLiteral(_ reduced: ReducedSwift, name: String, before callStart: Int) -> Bool {
        guard let literal = reduced.literals.filter({ $0.end <= callStart }).max(by: { $0.end < $1.end }),
              isTestServiceLiteral(literal),
              anchoredMatch(serviceBindingGap, in: reduced.bare, at: literal.end, upTo: callStart) != nil,
              let binding = compiled(
                #"(?<![A-Za-z0-9_])let\s+"# + NSRegularExpression.escapedPattern(for: name) + #"\s*(?::\s*String\s*)?=\s*\z"#)
        else { return false }
        return binding.firstMatch(in: reduced.bare, range: NSRange(location: 0, length: literal.start)) != nil
    }

    /// Each store a test builds whose service is not a plain test literal, or a `let` bound to one.
    private static func serviceArgumentProblems(_ reduced: ReducedSwift) -> [String] {
        let bare = reduced.bare
        let ns = bare as NSString
        var byStart: [Int: SwiftLiteral] = [:]
        for literal in reduced.literals { byStart[literal.start] = literal }
        var found: [String] = []
        for call in matches(storeConstruction, in: bare) {
            let lineNumber = ns.substring(to: call.range.location).filter { $0 == "\n" }.count + 1
            guard let label = anchoredMatch(#"\s*service\s*:\s*"#, in: bare, at: NSMaxRange(call.range)) else {
                found.append("builds a Keychain store without a service: argument (line \(lineNumber))")
                continue
            }
            let argument = NSMaxRange(label)
            if let literal = byStart[argument] {
                if isTestServiceLiteral(literal), anchoredMatch(#"\s*\)"#, in: bare, at: literal.end) != nil { continue }
            } else if let name = anchoredMatch(#"[A-Za-z_][A-Za-z0-9_]*(?=\s*\))"#, in: bare, at: argument),
                      boundToTestLiteral(reduced, name: ns.substring(with: name), before: call.range.location) {
                continue
            }
            found.append("builds a Keychain store on a service that is not one plain test literal (line \(lineNumber))")
        }
        return found
    }

    /// Ways to start or open an app from a test without the UI-test helper. Read on reduced code,
    /// so `ProcessInfo`, `Font.system(size:)`, comments and string text are not findings.
    private static let launcherPattern: String =
        #"(?<![A-Za-z0-9_])(?:Process|NSTask|NSWorkspace|NSRunningApplication|NSAppleScript|NSUserAppleScriptTask"#
        + #"|NSUserUnixTask|OSAScript|popen|posix_spawn\w*|exec[lv]p?e?|LSOpen\w*|NSClassFromString|objc_getClass"#
        + #"|objc_lookUpClass|dlopen|dlsym)(?![A-Za-z0-9_])"#
        + #"|(?<![A-Za-z0-9_.])system\s*\("#
        + #"|(?<![A-Za-z0-9_])(?:Darwin|Glibc|Foundation)\s*\.\s*system(?![A-Za-z0-9_])"#
    /// The open tool is a string, so it is looked for in the source text.
    private static let openTool = "/usr/bin/" + "open"

    private static func unreadable(_ reduced: ReducedSwift) -> [String] {
        reduced.errors.map { "cannot be read to the end, so it is refused: \($0)" }
    }

    private static func launcherProblems(in text: String, reduced: ReducedSwift? = nil) -> [String] {
        let reduced = reduced ?? SwiftReader(text).read()
        var found = unreadable(reduced)
        let ns = reduced.bare as NSString
        for hit in matches(launcherPattern, in: reduced.bare) {
            found.append("starts a process or app: \(ns.substring(with: hit.range))")
        }
        if text.contains(openTool) { found.append("names the open tool") }
        return found
    }

    /// One test file: the shared store or the type as a value, an extension or typealias of the
    /// type, `store: .shared`, any implicit `.shared`, the declarations that could stretch the scope
    /// exemption, the service constant's name, and the service of every store the file builds.
    private static func storeReach(in text: String) -> [String] {
        let reduced = SwiftReader(text).read()
        let bare = reduced.bare
        var found = unreadable(reduced)
        if !matches(storeValuePattern, in: bare).isEmpty {
            found.append("reaches the shared Keychain store or takes the type as a value")
        }
        if !matches(storeExtensionPattern, in: bare).isEmpty { found.append("extends the Keychain store") }
        if !matches(storeTypealiasPattern, in: bare).isEmpty { found.append("aliases the Keychain store type") }
        if !matches(storeArgumentPattern, in: bare).isEmpty { found.append("passes the shared store as store:") }
        if !matches(serviceConstantPattern, in: bare).isEmpty { found.append("names the shared store's service constant") }
        for use in implicitSharedUses(in: bare) {
            found.append("implicit shared member, type from context (\(use))")
        }
        let logical = logicalLines(bare)
        let logicalText = logical as NSString
        for pattern in scopeShapedDeclarations {
            for hit in matches(pattern, in: logical) {
                found.append("declaration that can widen the scope exemption (\(logicalText.substring(with: hit.range).prefix(40)))")
            }
        }
        found += serviceArgumentProblems(reduced)
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
            for problem in storeReach(in: text) {
                findings.append("\(path): \(problem)")
            }
            // GrowinTests is app-hosted: a second Growin started from a unit test would run without
            // the test-host signals and reach the real Keychain at launch.
            for problem in launcherProblems(in: text) {
                findings.append("\(path): \(problem)")
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
            "let unique = \"san.Growin.credentials." + "v1.test.x\"\nlet own = KeychainStore(service: unique)",
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
            ("production service", "let s = " + store + "(service: " + store + "." + "production" + "Service)"),
            ("yield", "var s: KeychainStore { _read { yield .shared } }"),
            ("switch case", "switch store {\ncase .shared: break\ndefault: break\n}"),
            ("bare allows call", "allows(.shared)"),
            // Codex r2 P2 on #573: the scope exemption is only as good as the type of what it is passed to.
            ("scope-labelled helper", "func cleanUp(scope store: KeychainStore) {}\ncleanUp(scope: .shared)"),
            ("scope label only", "func cleanUp(scope: KeychainStore) {}"),
            ("scope optional", "func cleanUp(scope store: KeychainStore?) {}"),
            ("scope unwrapped", "func cleanUp(scope store: KeychainStore!) {}"),
            ("scope Optional generic", "func cleanUp(scope store: Optional<KeychainStore>) {}"),
            ("scope inout", "func cleanUp(scope store: inout KeychainStore) {}"),
            ("scope underscore name", "func cleanUp(scope _: KeychainStore) {}"),
            ("scope initializer", "init(scope store: KeychainStore) {}"),
            ("scope property", "struct W { let scope: KeychainStore }"),
            ("scope generic", "func cleanUp<S: KeychainStore>(scope store: S) {}"),
            ("scope where clause", "func cleanUp<S>(scope store: S) where S == KeychainStore {}"),
            ("custom equality", "static func == (l: KeychainStore, r: Foo) -> Bool { true }"),
            ("custom pattern match", "func ~= (l: KeychainStore, r: Foo) -> Bool { true }"),
            ("custom allows", "func allows(_ s: KeychainStore) -> Bool { true }"),
            ("allows closure", "let allows: (KeychainStore) -> Bool = { _ in true }"),
            ("policy holding a store", "var policy: KeychainStore"),
            // Codex r2 P2: an interpolation is executable code.
            ("interpolated helper call", "print(\"cleanup: \\(Self.cleanUp(.shared))\")"),
            ("nested parentheses", "print(\"x \\(a(b(c(.shared))))\")"),
            ("nested string inside", "print(\"x \\(a(\"y\", .shared))\")"),
            ("two interpolations", "print(\"\\(a) and \\(cleanUp(.shared))\")"),
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
            "#expect(!CredentialName.x.policy.allows(.shared))",
            "#expect(a.policy != .shared)",
            "func a(scope: KeychainScope) {}",
            "func b(scope store: KeychainScope) {}",
            "func c(_ store: KeychainStore, scope: KeychainScope) {}",
            "static func == (l: Foo, r: Foo) -> Bool { true }",
            "print(\"router: \\(ApprovalSignerRouter.shared.x)\")",
            "let t = \"plain .shared text\"",
            "ApprovalSignerRouter.shared.sign(x)",
            "make().shared",
            "// cleanUp(.shared)",
            "let n = \"cleanUp(.shared)\"",
            "let unique = \"san.Growin.credentials." + "v1.test.x\"\nlet own = KeychainStore(service: unique)\ncleanUp(own)",
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
        // Other ways to start the app without the application type: the helper may not use them either.
        for problem in launcherProblems(in: helper.text) {
            findings.append("\(helper.path): \(problem)")
        }
        for (path, text) in sources where path != uiHelperPath {
            let reduced = SwiftReader(text).read()
            for problem in launcherProblems(in: text, reduced: reduced) {
                findings.append("\(path): \(problem)")
            }
            if text.contains(appType) { findings.append("\(path): names the application type outside the helper") }
            if text.contains(environment) { findings.append("\(path): touches the launch environment outside the helper") }
            let launches = !matches(#"\.\s*(?:launch|activate)\s*\("#, in: reduced.bare).isEmpty
            if launches && !reduced.bare.contains("GrowinLaunch.makeApp()") {
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

    // MARK: Review round 3 on #573 (mirrors the planted samples in the Python guard)

    @Test func aTestMayNotCallAnyMemberOfTheSharedStore() {
        let samples: [String] = [
            "try KeychainStore.shared.deleteAll()",
            "try KeychainStore.`shared`.deleteAll()",
            "try `KeychainStore`.shared.deleteAll()",
            "try KeychainStore\n    .shared.deleteAll()",
            "try KeychainStore.\n    shared.deleteAll()",
            "try KeychainStore . shared . deleteAll()",
            "try Growin.KeychainStore.shared.deleteAll()",
            "print(\"\\(try? KeychainStore.shared.string(for: .x, scope: .shared))\")",
            "print(#\"\\#(try? KeychainStore.shared.deleteAll())\"#)",
        ]
        for sample in samples {
            #expect(Self.storeReach(in: sample).contains { $0.contains("shared Keychain store") },
                    "\(sample) went undetected")
        }
    }

    @Test func everyStoreATestBuildsTakesAPlainTestLiteral() {
        let prod = Self.quotedProductionService
        let prefix = "san.Growin.credentials." + "v1.test."
        let bad: [(label: String, text: String)] = [
            ("production literal (operator probe)", "let s = KeychainStore(service: \(prod))"),
            ("explicit init", "let s = KeychainStore.init(service: \(prod))"),
            ("implicit init", "let s: KeychainStore = .init(service: \(prod))"),
            ("metatype init", "let s = type(of: other).init(service: \(prod))"),
            ("backticked", "let s = `KeychainStore`(`service`: \(prod))"),
            ("multi-line arguments", "let s = KeychainStore(\n    service:\n        \(prod)\n)"),
            ("inside an interpolation", "print(\"\\(KeychainStore(service: \(prod)))\")"),
            ("concatenated after the prefix", "let s = KeychainStore(service: \"\(prefix)\" + x)"),
            ("whole value interpolated", "let s = KeychainStore(service: \"\\(name)\")"),
            ("interpolation before the prefix", "let s = KeychainStore(service: \"\\(p)\(prefix)x\")"),
            ("other interpolation after the prefix", "let s = KeychainStore(service: \"\(prefix)\\(name)\")"),
            ("prefix without its dot", "let s = KeychainStore(service: \"san.Growin.credentials.v1.testx\")"),
            ("raw string", "let s = KeychainStore(service: #\"\(prefix)x\"#)"),
            ("multi-line string", "let s = KeychainStore(service: \"\"\"\n    \(prefix)x\n    \"\"\")"),
            ("literal with a member call", "let s = KeychainStore(service: \"\(prefix)x\".lowercased())"),
            ("member access", "let s = KeychainStore(service: Self.name)"),
            ("closure argument", "let make = { KeychainStore(service: $0) }"),
            ("no label", "let s = KeychainStore(\"\(prefix)x\")"),
            ("unbound identifier", "let s = KeychainStore(service: name)"),
            ("identifier bound to the production literal", "let name = \(prod)\nlet s = KeychainStore(service: name)"),
            ("identifier bound two statements back",
             "let name = \"\(prefix)x\"\nprint(1)\nlet s = KeychainStore(service: name)"),
            ("identifier bound with var", "var name = \"\(prefix)x\"\nlet s = KeychainStore(service: name)"),
        ]
        for sample in bad {
            #expect(!Self.productionReach(in: [("T.swift", sample.text)]).isEmpty, "\(sample.label) went undetected")
        }
        let good: [String] = [
            "let s = KeychainStore(service: \"\(prefix)\\(UUID().uuidString)\")",
            "ukStore = KeychainStore(service: \"\(prefix)uk.\\(UUID().uuidString)\")",
            "let s = KeychainStore(\n    service: \"\(prefix)\\(UUID().uuidString)\"\n)",
            "let service = \"\(prefix)\\(UUID().uuidString)\"\nlet store = KeychainStore(service: service)",
            "let service = \"\(prefix)x\"\nreturn (KeychainStore(service: service), service)",
            "let n = \"KeychainStore(service: other)\"",
            "let raw = RawKeychain(service: service)",
        ]
        for text in good {
            #expect(Self.productionReach(in: [("T.swift", text)]) == [], "\(text)")
        }
    }

    @Test func theStoreRuleSeesTheRealConstructionSites() throws {
        let sources = try SourceTree.swiftSources(under: "GrowinTests")
        var sites = 0
        for source in sources {
            let reduced = SwiftReader(source.text).read()
            sites += Self.matches(Self.storeConstruction, in: reduced.bare).count
            #expect(Self.serviceArgumentProblems(reduced) == [], "\(source.path)")
        }
        #expect(sites >= 5, "positive control: the store rule saw \(sites) construction sites")
    }

    private static func launcherSamples() -> [(label: String, text: String)] {
        let tool = Self.openTool
        return [
            ("process run (operator probe)",
             "try Process.run(URL(fileURLWithPath: \"\(tool)\"), arguments: [\"-b\", \"san.Growin\"])"),
            ("process with a space", "let p = Process ()"),
            ("process qualified", "let p = Foundation.Process()"),
            ("process as a type", "var p: Process?"),
            ("process in an interpolation", "print(\"\\(Process())\")"),
            ("process in a raw interpolation", "print(#\"\\#(Process())\"#)"),
            ("task", "let t = NSTask()"),
            ("popen", "let f = popen(\"open -b san.Growin\", \"r\")"),
            ("spawn", "posix_spawn(&pid, path, nil, nil, args, nil)"),
            ("spawnp", "posix_spawnp(&pid, path, nil, nil, args, nil)"),
            ("execv", "execv(path, args)"),
            ("execve", "execve(path, args, env)"),
            ("execlp", "execlp(name, arg, nil)"),
            ("system", "system(\"open -b san.Growin\")"),
            ("Darwin.system", "Darwin.system(\"x\")"),
            ("Launch Services", "LSOpenCFURLRef(url, nil)"),
            ("running application", "NSRunningApplication.runningApplications(withBundleIdentifier: id)"),
            ("workspace open", "NSWorkspace.shared.open(url)"),
            ("workspace open application", "NSWorkspace.shared.openApplication(at: url, configuration: c)"),
            ("AppleScript", "NSAppleScript(source: \"activate\")"),
            ("class by name", "let c = NSClassFromString(\"NS\" + \"Task\")"),
            ("open tool in a string", "let tool = \"\(tool)\""),
        ]
    }

    private static let launcherLegitimate: [String] = [
        "let info = ProcessInfo.processInfo",
        "@Test func thisTestProcessIsSkipped() {}",
        "let f = Font.system(size: 12)",
        "let s = config.system(x)",
        "let n = \"Process() NSWorkspace system(\"\n// NSWorkspace.shared.open(url)",
    ]

    @Test func theLauncherScanCatchesEveryWayToStartAnApp() {
        let helper: (path: String, text: String) = (Self.uiHelperPath, Self.helperSource(lines: Self.goodMakeAppBody))
        for sample in Self.launcherSamples() {
            #expect(!Self.uiLaunchViolations(in: [helper, ("GrowinUITests/F.swift", sample.text)]).isEmpty,
                    "\(sample.label) went undetected in a UI test")
            #expect(!Self.productionReach(in: [("GrowinTests/F.swift", sample.text)]).isEmpty,
                    "\(sample.label) went undetected in a unit test")
        }
        for text in Self.launcherLegitimate {
            #expect(Self.uiLaunchViolations(in: [helper, ("GrowinUITests/F.swift", text)]) == [], "\(text)")
            #expect(Self.launcherProblems(in: text) == [], "\(text)")
        }
    }

    @Test func rawStringInterpolationsAreCodeAndAnUnreadableFileIsRefused() {
        let raw: [String] = [
            "print(#\"x \\#(Self.cleanUp(.shared)) y\"#)",
            "print(##\"x \\##(cleanUp(.shared))\"##)",
            "let m = #\"\"\"\n    \\#(cleanUp(.shared))\n    \"\"\"#",
            "let m = #\"\\#(KeychainStore.shared)\"#",
            "// #\"\"\"\nlet s = KeychainStore.shared\n// \"\"\"#",
        ]
        for sample in raw {
            #expect(!Self.productionReach(in: [("T.swift", sample)]).isEmpty, "\(sample) went undetected")
        }
        // In a raw string `\(` is text and quotes inside do not end it.
        #expect(SwiftReader("let r = #\"a \\(b) \"c\" d\"#\n").read().bare == "let r = \"\"\n")
        #expect(SwiftReader("let r = ##\"\\#(x) \\##(y)\"##\n").read().bare == "let r = \"\" (y) \n")

        // Fail closed: a file the reader cannot finish is refused, whatever it would hide.
        let helper: (path: String, text: String) = (Self.uiHelperPath, Self.helperSource(lines: Self.goodMakeAppBody))
        let unreadable: [String] = [
            "let r = #\"\"\"\nlet s = 1",
            "let r = ##\"abc\"#\nlet s = 1",
            "let r = #\"abc\nlet s = 1",
            "let r = \"abc\nlet s = 1",
            "let r = \"\\(f(\n",
            "/* let s = 1\n",
        ]
        for sample in unreadable {
            #expect(!SwiftReader(sample).read().errors.isEmpty, "\(sample)")
            #expect(Self.productionReach(in: [("T.swift", sample)]).contains { $0.contains("cannot be read") }, "\(sample)")
            #expect(!Self.uiLaunchViolations(in: [helper, ("GrowinUITests/F.swift", sample)]).isEmpty, "\(sample)")
        }
    }

    @Test func declarationsSplitOverLinesAreCaught() {
        let bad: [String] = [
            "func cleanUp(scope store: Optional<\n    KeychainStore\n>) {}",
            "func cleanUp(\n    scope store:\n        KeychainStore?\n) {}",
            "var scope:\n    KeychainStore?",
            "var scope: Optional<\n    KeychainStore> = nil",
            "func allows(\n    _ s: KeychainStore\n) -> Bool { true }",
            "func allows\n(_ s: KeychainStore) -> Bool { true }",
            "static func ==\n(l: KeychainStore, r: Foo) -> Bool { true }",
            "static func\n== (l: KeychainStore, r: Foo) -> Bool { true }",
            "func cleanUp<S>(scope store: S)\n    where S == KeychainStore {}",
            "var policy:\n    KeychainStore",
            "let allows: (\n    KeychainStore\n) -> Bool = { _ in true }",
        ]
        for sample in bad {
            #expect(!Self.productionReach(in: [("T.swift", sample)]).isEmpty, "\(sample) went undetected")
        }
        let fine: [String] = [
            "func c(\n    _ store: KeychainStore,\n    scope: KeychainScope\n) {}",
            "let scope: KeychainScope\nlet store: KeychainStore",
            "var scope: Optional<\n    KeychainScope> = nil\nfunc f(_ s: KeychainStore) {}",
            "func allows(\n    _ s: KeychainScope\n) -> Bool { true }\nlet store: KeychainStore",
            "if i<n { f() }\nlet store: KeychainStore",
        ]
        for text in fine {
            #expect(Self.productionReach(in: [("T.swift", text)]) == [], "\(text)")
        }
        #expect(Self.logicalLines("f(\n  a,\n  b\n)\nlet x = 1\n") == "f(   a,   b )\nlet x = 1\n")
        #expect(Self.logicalLines("var s: Optional<\n  T> = nil\nlet y = 2\n") == "var s: Optional<   T> = nil\nlet y = 2\n")
        #expect(Self.logicalLines("x {\n  a\n  b\n}\n") == "x {\n  a\n  b\n}\n")
    }
}
