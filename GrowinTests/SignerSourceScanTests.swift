import Foundation
import Testing

/// The signer scans decide which files may name or call an approval signer. These tests feed the
/// scan helpers planted code, so the scan logic itself is proven on CI and not only by the fact that
/// today's sources happen to pass it.
struct SignerSourceScanTests {
    private typealias Scan = SignerSourceScan

    private static func violations(_ path: String, _ text: String) -> [String] {
        Scan.namingViolations(path: path, text: text)
    }

    // MARK: Typed inferred `.shared` and other forms that never spell `Type.shared`

    @Test(arguments: [
        "let s: LocalApprovalSigner = .shared",
        "func run(signer: LocalApprovalSigner = .shared) {}",
        "var signer: LocalApprovalSigner { .shared }",
        "typealias Signer = LocalApprovalSigner",
        "extension LocalApprovalSigner { static var x: Int { 1 } }",
        "let t = LocalApprovalSigner.self",
        "let s = LocalApprovalSigner(store: .shared)",
        "let s: SecureEnclaveApprovalSigner = .shared",
        "func run(r: ApprovalSignerRouter = .shared) {}",
        "let r: ApprovalSignerRouter = .shared",
        "let s: LocalApprovalSigner =\n    .shared",
    ])
    func aFileNotOnTheAllowListCannotNameASignerType(planted: String) {
        #expect(!Self.violations("Growin/Views/New.swift", planted).isEmpty, "scan missed: \(planted)")
    }

    @Test func aFileOnTheAllowListMayNotNameATypeItWasNotGranted() {
        // SettingsView may use the router but never the software signer.
        #expect(!Self.violations("Growin/Views/SettingsView.swift", "let s: LocalApprovalSigner = .shared").isEmpty)
        // The authorizer may use the software signer but not the router.
        #expect(!Self.violations("Growin/Security/PracticeApprovalAuthorizer.swift", "let r: ApprovalSignerRouter = .shared").isEmpty)
    }

    @Test func aTypedAliasInsideAnAllowedCallerFileFailsItsPinnedMentionCount() {
        let path = "Growin/ViewModels/RelayOrderBatchViewModel.swift"
        let honest = "try await ApprovalSignerRouter.shared.signAsync(bytes, for: .india, flow: .relayOrder)"
        #expect(Self.violations(path, honest).isEmpty)
        let aliased = honest + "\nlet r: ApprovalSignerRouter = .shared"
        #expect(!Self.violations(path, aliased).isEmpty)
        let removed = "let nothing = 1"
        #expect(Self.violations(path, removed).isEmpty, "a missing mention is the allow-list check's job, not this one's")
    }

    @Test func commentsAreIgnoredAndStringsAreNotTrustedToHideCode() {
        let path = "Growin/Views/New.swift"
        #expect(Self.violations(path, "// LocalApprovalSigner.shared is not allowed here").isEmpty)
        #expect(Self.violations(path, "/// LocalApprovalSigner").isEmpty)
        #expect(Self.violations(path, "/* LocalApprovalSigner */ let x = 1").isEmpty)
        #expect(Self.violations(path, "/* outer /* LocalApprovalSigner */ still comment LocalApprovalSigner */ let x = 1").isEmpty)
        #expect(Self.violations(path, "/*\n LocalApprovalSigner\n*/\nlet x = 1").isEmpty)
        // A URL string must not be mistaken for a comment that swallows the rest of the line.
        #expect(!Self.violations(path, #"let u = "http://x", s: LocalApprovalSigner = .shared"#).isEmpty)
        // Code after a block comment on the same line still counts.
        #expect(!Self.violations(path, "/* c */ let s: LocalApprovalSigner = .shared").isEmpty)
        // A real identifier after a line comment on the previous line still counts.
        #expect(!Self.violations(path, "// note\nlet s: LocalApprovalSigner = .shared").isEmpty)
        // Names inside a string are flagged too: interpolation can hold code.
        #expect(!Self.violations(path, #"let m = "\(LocalApprovalSigner.shared)""#).isEmpty)
    }

    @Test func similarlyNamedTypesAreNotTheGuardedTypes() {
        let path = "Growin/Views/New.swift"
        #expect(Self.violations(path, "throw LocalApprovalSignerError.notConfigured").isEmpty)
        #expect(Self.violations(path, "throw ApprovalSignerRouterError.relayIsIndiaOnly").isEmpty)
        #expect(Self.violations(path, "let x: PracticeApprovalAuthorizer = .shared").isEmpty)
        #expect(Self.violations(path, "let x = MyLocalApprovalSigner()").isEmpty)
    }

    @Test func theCommentStripperKeepsLineStructure() {
        let stripped = Scan.stripComments("a // x\nb /* y\nz */ c\n\"//\"")
        #expect(stripped == "a \nb \n c\n\"//\"", "\(stripped.debugDescription)")
    }

    // MARK: The allow-list cannot go stale

    private static let sample: [(path: String, text: String)] = [
        ("Growin/A.swift", "let r = LocalApprovalSigner(store: x)"),
        ("Growin/B.swift", "// only a comment about LocalApprovalSigner\nlet x = 1"),
    ]

    @Test func everyAllowListedFileMustExistAndReallyNameItsTypes() {
        let allowed: [String: Set<String>] = ["Growin/A.swift": ["LocalApprovalSigner"]]
        #expect(Scan.allowListProblems(Self.sample, allowed: allowed, pinned: [:]).isEmpty)

        let missing: [String: Set<String>] = ["Growin/Gone.swift": ["LocalApprovalSigner"]]
        #expect(!Scan.allowListProblems(Self.sample, allowed: missing, pinned: [:]).isEmpty, "a deleted file must fail")

        // B only mentions the name in a comment, so it no longer legitimately names it.
        let stale: [String: Set<String>] = ["Growin/B.swift": ["LocalApprovalSigner"]]
        #expect(!Scan.allowListProblems(Self.sample, allowed: stale, pinned: [:]).isEmpty, "a comment-only mention is stale")

        let wrongType: [String: Set<String>] = ["Growin/A.swift": ["LocalApprovalSigner", "ApprovalSignerRouter"]]
        #expect(!Scan.allowListProblems(Self.sample, allowed: wrongType, pinned: [:]).isEmpty, "a type the file never names is stale")

        let orphanPin: [String: [String: Int]] = ["Growin/A.swift": ["ApprovalSignerRouter": 1]]
        #expect(!Scan.allowListProblems(Self.sample, allowed: ["Growin/A.swift": ["LocalApprovalSigner"]], pinned: orphanPin).isEmpty)
    }

    @Test func theRealAllowListAndPinsAreConsistentWithEachOther() {
        for (path, byType) in Scan.pinnedMentions {
            for type in byType.keys {
                #expect(Scan.allowedNamers[path]?.contains(type) == true, "\(path)/\(type)")
            }
        }
        for path in Scan.pinnedRouterSignCalls.keys {
            #expect(Scan.allowedNamers[path]?.contains("ApprovalSignerRouter") == true, "\(path)")
        }
        #expect(Scan.allowedNamers[Scan.routerPath] != nil)
    }

    // MARK: Router call sites

    @Test func aRouterCallSplitAcrossLinesOrSpacedOutIsStillCounted() {
        #expect(Scan.routerSignCallCount(in: "try ApprovalSignerRouter.shared.sign(a, for: .uk, flow: .paperApproval)") == 1)
        #expect(Scan.routerSignCallCount(in: "try await ApprovalSignerRouter.shared.signAsync(a)") == 1)
        #expect(Scan.routerSignCallCount(in: "try ApprovalSignerRouter.shared\n    .sign(a)") == 1)
        #expect(Scan.routerSignCallCount(in: "try await ApprovalSignerRouter.shared\n    .signAsync(\n        a\n    )") == 1)
        #expect(Scan.routerSignCallCount(in: "try ApprovalSignerRouter . shared . sign (a)") == 1)
        #expect(Scan.routerSignCallCount(in: "try ApprovalSignerRouter\n  .shared\n  .signAsync(a)") == 1)
        #expect(Scan.routerSignCallCount(in: "try ApprovalSignerRouter.shared.sign(a)\ntry await ApprovalSignerRouter.shared.signAsync(b)") == 2)
    }

    @Test func nonSigningRouterUseAndCommentedCallsAreNotCounted() {
        #expect(Scan.routerSignCallCount(in: "ApprovalSignerRouter.shared.identity(for: ws)") == 0)
        #expect(Scan.routerSignCallCount(in: "ApprovalSignerRouter.shared\n    .isConfigured(for: ws)") == 0)
        #expect(Scan.routerSignCallCount(in: "// ApprovalSignerRouter.shared.sign(a)") == 0)
        #expect(Scan.routerSignCallCount(in: "/* ApprovalSignerRouter.shared\n.signAsync(a) */") == 0)
        #expect(Scan.routerSignCallCount(in: "ApprovalSignerRouter.enrolmentDecision(workspace: .uk)") == 0)
    }

    @Test func aKeptRouterReferenceIsFlaggedWhateverTheLineBreaks() {
        #expect(Scan.routerReferenceKeptCount(in: "let r = ApprovalSignerRouter.shared") == 1)
        #expect(Scan.routerReferenceKeptCount(in: "let r = ApprovalSignerRouter.shared\nr.sign(a)") == 1)
        #expect(Scan.routerReferenceKeptCount(in: "func f(r: ApprovalSignerRouter = ApprovalSignerRouter.shared) {}") == 1)
        #expect(Scan.routerReferenceKeptCount(in: "ApprovalSignerRouter.shared.sign(a)") == 0)
        #expect(Scan.routerReferenceKeptCount(in: "ApprovalSignerRouter.shared\n    .sign(a)") == 0)
        #expect(Scan.routerReferenceKeptCount(in: "// let r = ApprovalSignerRouter.shared") == 0)
    }

    // MARK: Retained signing method references

    @Test(arguments: [
        "let f = ApprovalSignerRouter.shared.sign\ntry f(a, .uk, .paperApproval)\ntry f(b, .uk, .paperApproval)",
        "let f = ApprovalSignerRouter.shared.signAsync",
        "let f = ApprovalSignerRouter.shared\n    .sign",
        "let f = ApprovalSignerRouter.shared\n    .signAsync\nlet g = 1",
        "let f = ApprovalSignerRouter . shared . sign ",
        "let f = ApprovalSignerRouter.shared.sign(_:for:flow:)",
        "let f = ApprovalSignerRouter.shared\n    .signAsync(\n        _:\n        for:\n        flow:\n    )",
        "let f = ApprovalSignerRouter.shared.sign as Any",
        "let k = \\ApprovalSignerRouter.sign",
        "let s = LocalApprovalSigner(store: x)\nlet f = s.sign",
        "let s = SecureEnclaveApprovalSigner(x)\nlet f = s.signAsync\nlet g = 1",
        "let f = (ApprovalSignerRouter.shared).sign",
        "let ok = ApprovalSignerRouter.shared.sign == nil",
    ])
    func aRetainedSigningMethodReferenceIsFlagged(planted: String) {
        #expect(Scan.signingMethodReferenceCount(in: planted) > 0, "scan missed: \(planted)")
    }

    @Test(arguments: [
        "try ApprovalSignerRouter.shared.sign(a, for: .uk, flow: .paperApproval)",
        "try await ApprovalSignerRouter.shared.signAsync(a, for: .uk, flow: .paperApproval)",
        "try ApprovalSignerRouter.shared\n    .sign(a, for: .uk, flow: .paperApproval)",
        "try await ApprovalSignerRouter.shared . signAsync (\n    a, for: ws, flow: .paperApproval)",
        "try s.sign(payload, for: .uk)\nlet r = ApprovalSignerRouter.shared.identity(for: ws)",
        "let sig = ApprovalSignerRouter.shared.signature(for: ws)",
        "let n = ApprovalSignerRouter.shared.signedBytesCount",
        "handler.sign?(a)\nhandler.sign!(a)\nlet r = ApprovalSignerRouter.shared.identity(for: ws)",
        "// let f = ApprovalSignerRouter.shared.sign\nlet r = ApprovalSignerRouter.shared.identity(for: ws)",
        "/* let f = ApprovalSignerRouter.shared\n.signAsync */\nlet r = ApprovalSignerRouter.shared.identity(for: ws)",
        // Storing a closure into a `sign` property is an assignment, not a method reference.
        "self.sign = sign\nlet s: LocalApprovalSigner = x",
    ])
    func anImmediateCallOrAnAssignmentIsNotAMethodReference(code: String) {
        #expect(Scan.signingMethodReferenceCount(in: code) == 0, "false positive: \(code)")
    }

    @Test func aFileThatNamesNoGuardedTypeIsNotScannedForMethodReferences() {
        // Unrelated code, such as `FloatingPoint.sign`, cannot reach a signer without naming one.
        #expect(Scan.signingMethodReferenceCount(in: "let s = value.sign") == 0)
        #expect(Scan.signingMethodReferenceCount(in: "let s = value.sign\nlet r = ApprovalSignerRouter.shared.x") > 0)
    }

    // MARK: A second router

    @Test(arguments: [
        "let r = ApprovalSignerRouter(india: a, uk: b)",
        "let r = ApprovalSignerRouter ( india: a, uk: b )",
        "let r = ApprovalSignerRouter\n    (india: a, uk: b)",
        "let r = ApprovalSignerRouter(\n    india: a,\n    uk: b\n)",
        "let r = ApprovalSignerRouter.init(india: a, uk: b)",
        "let r = ApprovalSignerRouter . init (india: a, uk: b)",
        "let r = ApprovalSignerRouter\n    .init(india: a, uk: b)",
        "let make = ApprovalSignerRouter.init",
        "static let shared = ApprovalSignerRouter(india: a, uk: b)",
        "func f() -> ApprovalSignerRouter { ApprovalSignerRouter(india: a, uk: b) }",
        // Inferred construction: the type is named only by annotation or parameter default.
        "let r: ApprovalSignerRouter = .init(india: .shared, uk: .shared)",
        "let r: ApprovalSignerRouter = .init(\n    india: a,\n    uk: b\n)",
        "let r: ApprovalSignerRouter =\n    .init(india: a, uk: b)",
        "let r: ApprovalSignerRouter\n    = .init(india: a, uk: b)",
        "let r: ApprovalSignerRouter? = .init(india: a, uk: b)",
        "let r: ApprovalSignerRouter = . init (a)",
        "func run(r: ApprovalSignerRouter = .init(india: a, uk: b)) {}",
        "func run(r: ApprovalSignerRouter = .init()) {}",
        // Inferred from somewhere else: only the argument list gives it away.
        "var r: ApprovalSignerRouter { .init(india: a, uk: b) }",
        "func f() -> ApprovalSignerRouter { .init(india: a, uk: b) }",
        "let r = make(.init(india: a, uk: b))",
        "let r = make(.init(\n    india: a,\n    uk: b))",
        "let r = make(. init ( india : a, uk: b))",
        // Backticked spellings of `init` and of the `india` label.
        "let r: ApprovalSignerRouter = .init(`india`: a, uk: b)",
        "let r = make(.init(`india`: a, uk: b))",
        "let r = make(.`init`(india: a, uk: b))",
        "let r = make(.`init`(`india`: a, uk: b))",
        "let r: ApprovalSignerRouter = .`init`(india: a, uk: b)",
        "let r = ApprovalSignerRouter.`init`(india: a, uk: b)",
        "let r = ApprovalSignerRouter . `init` (india: a, uk: b)",
    ])
    func constructingASecondRouterIsFlagged(planted: String) {
        #expect(Scan.routerConstructionCount(in: planted) > 0, "scan missed: \(planted)")
    }

    /// A typealias lets `R(india:uk:)` build a router with the name nowhere near the call. The
    /// declaration is a violation on its own. A backticked right-hand side and a generic alias are
    /// the same kind of declaration, and constructions through the alias name are counted too.
    @Test(arguments: [
        // The Codex round 2 probe: SettingsView's configuration check rewritten through an alias.
        "typealias R = ApprovalSignerRouter\nreturn R(india: .shared, uk: .shared).isConfigured(for: workspace)",
        "typealias R=ApprovalSignerRouter",
        "typealias   R   =\n    ApprovalSignerRouter",
        "typealias R\n    = ApprovalSignerRouter",
        "private typealias R = ApprovalSignerRouter",
        "typealias R = ApprovalSignerRouter?",
        "typealias R = ApprovalSignerRouter!",
        "typealias R = Growin.ApprovalSignerRouter",
        "typealias R = Growin . ApprovalSignerRouter",
        "typealias R = Optional<ApprovalSignerRouter>",
        "typealias R = [ApprovalSignerRouter]",
        "typealias R = (ApprovalSignerRouter)",
        "typealias `R` = ApprovalSignerRouter",
        "typealias R<T> = ApprovalSignerRouter",
        "associatedtype R = ApprovalSignerRouter",
        // Backticked right-hand side. `R(india:)` is not `.init(india:)`, so the india-label rule
        // cannot see it, and the declaration itself has to recognise the backticks.
        "typealias R = `ApprovalSignerRouter`",
        "typealias R =\n    `ApprovalSignerRouter`",
        "typealias   R   = `ApprovalSignerRouter`",
        "private typealias R = `ApprovalSignerRouter`",
        "typealias R = `ApprovalSignerRouter`\nlet r = R(india: .shared, uk: .shared)",
        "typealias R = `ApprovalSignerRouter`\nlet r = R.init(a, b)",
        "typealias R = `ApprovalSignerRouter`\nlet make = R.init",
        "typealias R = `ApprovalSignerRouter`\nlet r = `R`(india: a, uk: b)",
        "typealias R = Growin.`ApprovalSignerRouter`",
        "typealias R = Growin . `ApprovalSignerRouter`\nlet r = R.init(india: .shared, uk: .shared)",
        "typealias R = Growin.`ApprovalSignerRouter`\nlet r = R.init(a, b)",
        "typealias R = Optional<`ApprovalSignerRouter`>",
        "associatedtype R = `ApprovalSignerRouter`",
        "associatedtype R: Sendable = ApprovalSignerRouter",
        "associatedtype R: Sendable & AnyObject = Growin.`ApprovalSignerRouter`",
        "typealias R = `ApprovalSignerRouter`?\nlet r: R? = .init(a, b)",
        // Generic alias. `Box<Int>(india:)` does not spell the router, and `Box<Int>.init(a, b)` /
        // `Box<Int> = .init(a, b)` do not use the india label, so only alias resolution sees them.
        "typealias Box<T> = ApprovalSignerRouter\nlet r = Box<Int>(india: .shared, uk: .shared)",
        "typealias Box<T> = ApprovalSignerRouter\nlet r = Box<Int>.init(a, b)",
        "typealias Box<T> = ApprovalSignerRouter\nlet r: Box<Int> = .init(a, b)",
        "typealias Box<T> = ApprovalSignerRouter\nlet r: Box<Int> =\n    .init(india: .shared, uk: .shared)",
        "typealias Box<T> = ApprovalSignerRouter\nlet r: Box<Int>? = .init(a, b)",
        "typealias Box < T > = ApprovalSignerRouter\nlet r = Box< Int >(india: a, uk: b)",
        "typealias Box<T: Sendable> = ApprovalSignerRouter\nlet r = Box<Int>(india: a, uk: b)",
        "typealias Box<T> = `ApprovalSignerRouter`\nlet r = Box<Int>(india: a, uk: b)",
        "typealias Box<T> = Growin.`ApprovalSignerRouter`\nlet r = Box<Int>.init(a, b)",
        "typealias Box<T> = Growin.`ApprovalSignerRouter`\nlet r: Box<Int> = .init(india: a, uk: b)",
    ])
    func aTypealiasOfTheRouterIsFlagged(planted: String) {
        #expect(Scan.routerConstructionCount(in: planted) > 0, "scan missed: \(planted)")
    }

    /// The declaration is already a hit. The use has to add another, or a backticked / generic call
    /// would still pass when the declaration rule is the only thing that fired.
    @Test func constructionsThroughAResolvedAliasAddAHit() {
        let backticked = "typealias R = `ApprovalSignerRouter`"
        let backtickedCount = Scan.routerConstructionCount(in: backticked)
        #expect(backtickedCount > 0)
        #expect(Scan.routerConstructionCount(in: backticked + "\nlet r = R(india: a, uk: b)") > backtickedCount)
        #expect(Scan.routerConstructionCount(in: backticked + "\nlet r = R.init(a, b)") > backtickedCount)
        #expect(Scan.routerConstructionCount(in: backticked + "\nlet d = Date.init(timeIntervalSince1970: 0)") == backtickedCount)

        let generic = "typealias Box<T> = ApprovalSignerRouter"
        let genericCount = Scan.routerConstructionCount(in: generic)
        #expect(genericCount > 0)
        #expect(Scan.routerConstructionCount(in: generic + "\nlet r = Box<Int>(india: a, uk: b)") > genericCount)
        #expect(Scan.routerConstructionCount(in: generic + "\nlet r = Box<Int>.init(a, b)") > genericCount)
        #expect(Scan.routerConstructionCount(in: generic + "\nlet r: Box<Int> = .init(a, b)") > genericCount)
        #expect(Scan.routerConstructionCount(in: generic + "\nlet d = Date.init(timeIntervalSince1970: 0)") == genericCount)
    }

    /// The router's own file is allowed to construct the router. The same text in any other file is not.
    @Test func theRouterFileItselfIsNotScannedForConstruction() {
        let planted = "typealias R = `ApprovalSignerRouter`\nstatic let shared = ApprovalSignerRouter(india: a, uk: b)\nlet r = R(india: a, uk: b)"
        let sources = [
            (Scan.routerPath, planted),
            ("Growin/Views/Other.swift", "let x = 1"),
        ]
        let outside = sources.filter { $0.0 != Scan.routerPath }
        #expect(outside.allSatisfy { Scan.routerConstructionCount(in: $0.1) == 0 })
        #expect(Scan.routerConstructionCount(in: planted) > 0)
    }

    /// Inside `extension ApprovalSignerRouter`, `Self(...)` and `Self.init(...)` build a router without
    /// the name or an `.init(` with the right label next to it. The extension itself is the violation.
    @Test(arguments: [
        // The Codex round 2 P3 probe.
        "extension ApprovalSignerRouter { static func make() -> Self { Self(india: .shared, uk: .shared) } }",
        "extension ApprovalSignerRouter {\n    static func make() -> Self { Self.init(india: a, uk: b) }\n}",
        "extension ApprovalSignerRouter {\n    static func make() -> Self { Self . init (india: a, uk: b) }\n}",
        "public extension ApprovalSignerRouter {}",
        "extension Growin.ApprovalSignerRouter {}",
        "extension\n    ApprovalSignerRouter: Foo {}",
        "extension ApprovalSignerRouter: Sendable where Self: Sendable {}",
        "extension P where Self == ApprovalSignerRouter { static var x: Self { Self(india: a, uk: b) } }",
    ])
    func anExtensionOfTheRouterIsFlagged(planted: String) {
        #expect(Scan.routerConstructionCount(in: planted) > 0, "scan missed: \(planted)")
    }

    @Test func aliasAndExtensionRulesLeaveUnrelatedCodeAlone() {
        #expect(Scan.routerConstructionCount(in: "typealias Handler = () -> Void") == 0)
        #expect(Scan.routerConstructionCount(in: "typealias Router2 = ApprovalSignerRouterConfig") == 0)
        #expect(Scan.routerConstructionCount(in: "typealias R = MyApprovalSignerRouter") == 0)
        #expect(Scan.routerConstructionCount(in: "typealias R = ApprovalSignerRouterError") == 0)
        #expect(Scan.routerConstructionCount(in: "typealias R = `UnrelatedRouter`") == 0)
        #expect(Scan.routerConstructionCount(in: "associatedtype R: Sendable = Handler") == 0)
        #expect(Scan.routerConstructionCount(in: "typealias R = `ApprovalSignerRouterConfig`") == 0)
        #expect(Scan.routerConstructionCount(in: "typealias R = `ApprovalSignerRouterError`") == 0)
        #expect(Scan.routerConstructionCount(in: "typealias R = Growin.`UnrelatedRouter`") == 0)
        #expect(Scan.routerConstructionCount(in: "typealias Box<T> = Handler\nlet r = Box<Int>(value)") == 0)
        #expect(Scan.routerConstructionCount(in: "typealias Box<T> = Handler\nlet r = Box<Int>.init(value)") == 0)
        #expect(Scan.routerConstructionCount(in: "typealias Box<T> = Handler\nlet r: Box<Int> = .init(value)") == 0)
        #expect(Scan.routerConstructionCount(in: "typealias Box<T> = `Handler`\nlet r = Box<Int>(india: a, uk: b)") == 0)
        #expect(Scan.routerConstructionCount(in: "typealias Box<T> = Foo<ApprovalSignerRouter>") == 0)
        #expect(Scan.routerConstructionCount(in: "// typealias R = `ApprovalSignerRouter`") == 0)
        #expect(Scan.routerConstructionCount(in: "/* typealias Box<T> = ApprovalSignerRouter */ let x = 1") == 0)
        // An alias line followed by an unrelated mention must not be read as one declaration.
        #expect(Scan.routerConstructionCount(in: "typealias Handler = () -> Void\nfunc run(r: ApprovalSignerRouter) {}") == 0)
        #expect(Scan.routerConstructionCount(in: "typealias R = Foo\nlet r: ApprovalSignerRouter = .shared") == 0)
        #expect(Scan.routerConstructionCount(in: "associatedtype Item = Int") == 0)
        #expect(Scan.routerConstructionCount(in: "extension Foo { static func make() -> Self { Self(a: 1) } }") == 0)
        #expect(Scan.routerConstructionCount(in: "extension Foo { static func make() -> Self { Self.init(a: 1) } }") == 0)
        #expect(Scan.routerConstructionCount(in: "extension Foo: Bar {\n    func x(r: ApprovalSignerRouter) {}\n}") == 0)
        #expect(Scan.routerConstructionCount(in: "extension Foo { var r: ApprovalSignerRouter { .shared } }") == 0)
        #expect(Scan.routerConstructionCount(in: "extension ApprovalSignerRouterError: Equatable {}") == 0)
        #expect(Scan.routerConstructionCount(in: "extension MyApprovalSignerRouter {}") == 0)
        #expect(Scan.routerConstructionCount(in: "// typealias R = ApprovalSignerRouter") == 0)
        #expect(Scan.routerConstructionCount(in: "// extension ApprovalSignerRouter {}") == 0)
        #expect(Scan.routerConstructionCount(in: "/* typealias R = ApprovalSignerRouter */ let x = 1") == 0)
        #expect(Scan.routerConstructionCount(in: "let s = Foo(.init(`indiaTime`: 1))") == 0)
    }

    @Test func routerUseThatIsNotConstructionIsNotFlagged() {
        #expect(Scan.routerConstructionCount(in: "try ApprovalSignerRouter.shared.sign(a, for: .uk, flow: .paperApproval)") == 0)
        #expect(Scan.routerConstructionCount(in: "ApprovalSignerRouter.enrolmentDecision(workspace: .uk)") == 0)
        #expect(Scan.routerConstructionCount(in: "throw ApprovalSignerRouterError.relayIsIndiaOnly(workspace: .uk)") == 0)
        #expect(Scan.routerConstructionCount(in: "let x = MyApprovalSignerRouter(a)") == 0)
        #expect(Scan.routerConstructionCount(in: "func run(r: ApprovalSignerRouter) {}") == 0)
        #expect(Scan.routerConstructionCount(in: "// let r = ApprovalSignerRouter(india: a, uk: b)") == 0)
        #expect(Scan.routerConstructionCount(in: "/* ApprovalSignerRouter\n.init() */ let x = 1") == 0)
        #expect(Scan.routerConstructionCount(in: "// let r: ApprovalSignerRouter = .init(india: a, uk: b)") == 0)
        #expect(Scan.routerConstructionCount(in: "let r: ApprovalSignerRouter = .shared") == 0)
        #expect(Scan.routerConstructionCount(in: "func run(r: ApprovalSignerRouter = .shared) {}") == 0)
        #expect(Scan.routerConstructionCount(in: "let d = Date.init(timeIntervalSince1970: 0)\nlet r: ApprovalSignerRouter = .shared") == 0)
        #expect(Scan.routerConstructionCount(in: "let s = Foo(.init(indiaTime: 1))") == 0)
    }

    @Test func callSiteCountsArePerFileSoASecondCallInAListedFileChangesThem() {
        let one = "try await ApprovalSignerRouter.shared.signAsync(a)"
        let two = one + "\n" + one
        let base: [(path: String, text: String)] = [
            ("Growin/ViewModels/ChatViewModel.swift", one),
            (Scan.routerPath, "ApprovalSignerRouter.shared.sign(x)"),  // the router itself is excluded
            ("Growin/Views/Other.swift", "let x = 1"),
        ]
        #expect(Scan.routerSignCallSites(base) == ["Growin/ViewModels/ChatViewModel.swift": 1])
        let doubled: [(path: String, text: String)] = [("Growin/ViewModels/ChatViewModel.swift", two)]
        #expect(Scan.routerSignCallSites(doubled) == ["Growin/ViewModels/ChatViewModel.swift": 2])
        #expect(Scan.routerSignCallSites(doubled) != ["Growin/ViewModels/ChatViewModel.swift": 1])
    }
}
