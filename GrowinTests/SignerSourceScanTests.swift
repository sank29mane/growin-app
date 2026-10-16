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
