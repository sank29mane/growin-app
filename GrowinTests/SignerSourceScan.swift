import Foundation

/// Text scans over the app sources that guard the approval signers. Pure functions over strings, so
/// `SignerSourceScanTests` can feed them planted code and prove the scan itself catches each evasion.
///
/// The software signer's `sign` has no workspace gate, so the question these scans answer is: which
/// files can even name a signer type. A line such as `let s: LocalApprovalSigner = .shared` or a
/// parameter default `= .shared` never spells `LocalApprovalSigner.shared`, so matching call shapes
/// misses it. Matching the type NAME (comments stripped) cannot be dodged that way, whatever the
/// surrounding syntax is.
nonisolated enum SignerSourceScan {
    static let guardedTypes = ["LocalApprovalSigner", "SecureEnclaveApprovalSigner", "ApprovalSignerRouter"]

    static let routerPath = "Growin/Security/ApprovalSignerRouter.swift"

    /// Every file that legitimately names a guarded type, and which ones. Adding a file or a type
    /// here is the review step for a new signer caller.
    static let allowedNamers: [String: Set<String>] = [
        "Growin/Security/ApprovalSignerRouter.swift":
            ["ApprovalSignerRouter", "LocalApprovalSigner", "SecureEnclaveApprovalSigner"],
        "Growin/Security/LocalApprovalSigner.swift": ["LocalApprovalSigner"],
        "Growin/Security/SecureEnclaveApprovalSigner.swift": ["SecureEnclaveApprovalSigner"],
        "Growin/Security/PracticeApprovalAuthorizer.swift": ["LocalApprovalSigner"],
        "Growin/Models/PaperOperationsModels.swift": ["ApprovalSignerRouter"],
        "Growin/ViewModels/ChatViewModel.swift": ["ApprovalSignerRouter"],
        "Growin/ViewModels/RelayOrderBatchViewModel.swift": ["ApprovalSignerRouter"],
        "Growin/Views/SettingsView.swift": ["ApprovalSignerRouter"],
    ]

    /// Exact number of times a caller file may name a type. A typed alias (`let s: ApprovalSignerRouter
    /// = .shared`) inside an allowed file adds a mention, so it fails here even though the file is
    /// allowed to use the router. The three core definition files are not pinned: they are the doors.
    static let pinnedMentions: [String: [String: Int]] = [
        "Growin/Security/PracticeApprovalAuthorizer.swift": ["LocalApprovalSigner": 2],
        "Growin/Models/PaperOperationsModels.swift": ["ApprovalSignerRouter": 3],
        "Growin/ViewModels/ChatViewModel.swift": ["ApprovalSignerRouter": 2],
        "Growin/ViewModels/RelayOrderBatchViewModel.swift": ["ApprovalSignerRouter": 1],
        "Growin/Views/SettingsView.swift": ["ApprovalSignerRouter": 8],
    ]

    /// Exact number of `ApprovalSignerRouter.shared.sign(` / `.signAsync(` call sites per caller file.
    static let pinnedRouterSignCalls: [String: Int] = [
        "Growin/Models/PaperOperationsModels.swift": 1,
        "Growin/ViewModels/ChatViewModel.swift": 1,
        "Growin/ViewModels/RelayOrderBatchViewModel.swift": 1,
        "Growin/Views/SettingsView.swift": 2,
    ]

    // MARK: Comments

    /// Removes `//` and (nested) `/* */` comments and keeps newlines. String literals are left in
    /// place and respected, so `"http://x"` does not hide the rest of its line.
    static func stripComments(_ text: String) -> String {
        let s = Array(text.unicodeScalars)
        var out = String.UnicodeScalarView()
        func at(_ k: Int, _ ch: Unicode.Scalar) -> Bool { k < s.count && s[k] == ch }
        var i = 0
        var blockDepth = 0
        var inString = false
        var inMultiline = false
        while i < s.count {
            let c = s[i]
            if blockDepth > 0 {
                if c == "/", at(i + 1, "*") { blockDepth += 1; i += 2; continue }
                if c == "*", at(i + 1, "/") { blockDepth -= 1; i += 2; continue }
                if c == "\n" { out.append(c) }
                i += 1
                continue
            }
            if inMultiline {
                out.append(c)
                if c == "\\", i + 1 < s.count { out.append(s[i + 1]); i += 2; continue }
                if c == "\"", at(i + 1, "\""), at(i + 2, "\"") {
                    out.append(s[i + 1]); out.append(s[i + 2]); i += 3; inMultiline = false; continue
                }
                i += 1
                continue
            }
            if inString {
                out.append(c)
                if c == "\\", i + 1 < s.count { out.append(s[i + 1]); i += 2; continue }
                if c == "\"" || c == "\n" { inString = false }
                i += 1
                continue
            }
            if c == "/", at(i + 1, "/") {
                while i < s.count, s[i] != "\n" { i += 1 }
                continue
            }
            if c == "/", at(i + 1, "*") { blockDepth = 1; i += 2; continue }
            if c == "\"" {
                if at(i + 1, "\""), at(i + 2, "\"") {
                    out.append(c); out.append(s[i + 1]); out.append(s[i + 2]); i += 3; inMultiline = true; continue
                }
                inString = true
            }
            out.append(c)
            i += 1
        }
        return String(out)
    }

    static func matchCount(_ pattern: String, in text: String) -> Int {
        (try? NSRegularExpression(pattern: pattern))
            .map { $0.numberOfMatches(in: text, range: NSRange(text.startIndex..., in: text)) } ?? 0
    }

    /// Identifier occurrences of `name` in already comment-stripped code. `LocalApprovalSignerError`
    /// is a different identifier and does not count.
    static func mentions(of name: String, in code: String) -> Int {
        matchCount("(?<![A-Za-z0-9_])\(name)(?![A-Za-z0-9_])", in: code)
    }

    // MARK: Which files may name a signer type

    static func namingViolations(path: String, text: String) -> [String] {
        let code = stripComments(text)
        var out: [String] = []
        for type in guardedTypes {
            let count = mentions(of: type, in: code)
            guard count > 0 else { continue }
            guard allowedNamers[path, default: []].contains(type) else {
                out.append("\(path) names \(type) \(count)x and is not on the allow-list")
                continue
            }
            if let pinned = pinnedMentions[path]?[type], pinned != count {
                out.append("\(path) names \(type) \(count)x, pinned at \(pinned)")
            }
        }
        return out
    }

    /// The allow-list cannot go stale: every listed file exists and really names every listed type,
    /// and every pin refers to a listed pair.
    static func allowListProblems(
        _ sources: [(path: String, text: String)],
        allowed: [String: Set<String>] = SignerSourceScan.allowedNamers,
        pinned: [String: [String: Int]] = SignerSourceScan.pinnedMentions
    ) -> [String] {
        var out: [String] = []
        for (path, types) in allowed.sorted(by: { $0.key < $1.key }) {
            guard let source = sources.first(where: { $0.path == path }) else {
                out.append("allow-listed file \(path) does not exist")
                continue
            }
            let code = stripComments(source.text)
            for type in types.sorted() where mentions(of: type, in: code) == 0 {
                out.append("\(path) is allow-listed for \(type) but does not name it")
            }
        }
        for (path, byType) in pinned {
            for type in byType.keys where !(allowed[path]?.contains(type) ?? false) {
                out.append("pin for \(path)/\(type) has no allow-list entry")
            }
        }
        return out
    }

    // MARK: Router call sites

    /// `ApprovalSignerRouter.shared.sign(` and `.signAsync(`, tolerant of spaces and line breaks
    /// between every token. Comments are stripped first, so a commented call does not count.
    static func routerSignCallCount(in text: String) -> Int {
        matchCount(
            #"\bApprovalSignerRouter\s*\.\s*shared\s*\.\s*(?:signAsync|sign)\s*\("#,
            in: stripComments(text)
        )
    }

    /// `ApprovalSignerRouter.shared` that is not immediately followed by a member access: a stored
    /// copy of the router, which would be called later without being counted.
    static func routerReferenceKeptCount(in text: String) -> Int {
        matchCount(#"\bApprovalSignerRouter\s*\.\s*shared(?!\s*\.\s*\w)"#, in: stripComments(text))
    }

    /// A signing method that is referenced and not called on the spot: `let f = ApprovalSignerRouter
    /// .shared.sign` then `f(a)` twice. The per-file call counting only sees `.sign(`, so a retained
    /// reference would be invoked any number of times unseen. Only the immediate call form is allowed.
    ///
    /// Counted in every file that names a guarded type, since only those files can reach a signer or
    /// the router (see `namingViolations`). Counts:
    /// - `.sign` / `.signAsync` not followed by `(`, with spaces and line breaks tolerated. Optional and
    ///   forced calls (`.sign?(`, `.sign!(`) are still calls. An assignment target (`self.sign = sign`,
    ///   a stored closure being set) is not a reference to a method and is left alone; `==` is not.
    /// - `.sign(_:for:flow:)`: an unapplied reference spelled with argument labels only.
    static func signingMethodReferenceCount(in text: String) -> Int {
        let code = stripComments(text)
        guard guardedTypes.contains(where: { mentions(of: $0, in: code) > 0 }) else { return 0 }
        let notCalled = matchCount(
            #"\.\s*(?:signAsync|sign)\b(?!\s*[?!]?\s*\()(?!\s*=(?!=))"#,
            in: code
        )
        let labelsOnly = matchCount(
            #"\.\s*(?:signAsync|sign)\s*\(\s*(?:[A-Za-z_]\w*\s*:\s*)+\)"#,
            in: code
        )
        return notCalled + labelsOnly
    }

    /// `ApprovalSignerRouter(` or `ApprovalSignerRouter.init`, tolerant of spaces and line breaks. The
    /// router is a singleton held as `.shared`; a second instance could carry different signers. Only
    /// the router's own file may construct it, so callers pass every file but `routerPath`.
    static func routerConstructionCount(in text: String) -> Int {
        matchCount(
            #"(?<![A-Za-z0-9_])ApprovalSignerRouter\s*(?:\(|\.\s*init\b)"#,
            in: stripComments(text)
        )
    }

    /// Router sign call sites per file, for every file but the router itself, zeros omitted.
    static func routerSignCallSites(_ sources: [(path: String, text: String)]) -> [String: Int] {
        var out: [String: Int] = [:]
        for source in sources where source.path != routerPath {
            let count = routerSignCallCount(in: source.text)
            if count > 0 { out[source.path] = count }
        }
        return out
    }
}
