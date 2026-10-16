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
    ])
    func constructingASecondRouterIsFlagged(planted: String) {
        #expect(Scan.routerConstructionCount(in: planted) > 0, "scan missed: \(planted)")
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
