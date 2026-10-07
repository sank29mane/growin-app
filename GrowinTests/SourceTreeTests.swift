import Foundation
import Testing

/// Proves the source-scan helper survives a repo reached through a symlink. xcodebuild canonicalises
/// the project path, so `#filePath` rarely carries the symlink spelling in CI; these tests build the
/// symlinked layout explicitly instead of relying on how the compiler was invoked.
struct SourceTreeTests {
    /// <tmp>/<uuid>/real/Growin/{A.swift,Sub/B.swift,note.txt}, plus a symlink <tmp>/<uuid>/link -> real
    /// and a symlinked parent <tmp>/<uuid>/parentLink -> <tmp>/<uuid> (so parentLink/real is the repo).
    private struct Fixture {
        let base: String
        var real: String { base + "/real" }
        var link: String { base + "/link" }
        var viaParent: String { base + "/parentLink/real" }

        static func make() throws -> Fixture {
            let fm = FileManager.default
            // NSTemporaryDirectory() is under /var, itself a symlink to /private/var.
            let base = NSTemporaryDirectory() + "SourceTreeTests-\(UUID().uuidString)"
            try fm.createDirectory(atPath: base + "/real/Growin/Sub", withIntermediateDirectories: true)
            try "let a = 1\n".write(toFile: base + "/real/Growin/A.swift", atomically: true, encoding: .utf8)
            try "let b = 2\n".write(toFile: base + "/real/Growin/Sub/B.swift", atomically: true, encoding: .utf8)
            try "not swift\n".write(toFile: base + "/real/Growin/note.txt", atomically: true, encoding: .utf8)
            try fm.createSymbolicLink(atPath: base + "/link", withDestinationPath: base + "/real")
            try fm.createSymbolicLink(atPath: base + "/parentLink", withDestinationPath: base)
            return Fixture(base: base)
        }

        func remove() { try? FileManager.default.removeItem(atPath: base) }
    }

    @Test func fixtureReallyIsBehindASymlink() throws {
        let fixture = try Fixture.make()
        defer { fixture.remove() }
        #expect(SourceTree.realPath(fixture.link) == SourceTree.realPath(fixture.real))
        #expect(fixture.link != SourceTree.realPath(fixture.link))
        #expect(fixture.viaParent != SourceTree.realPath(fixture.viaParent))
        // The naive scheme this helper replaces: enumerator paths are resolved, the root is not.
        let enumerator = try #require(FileManager.default.enumerator(
            at: URL(fileURLWithPath: fixture.link + "/Growin"), includingPropertiesForKeys: nil))
        let naive = (enumerator.allObjects as! [URL])
            .filter { $0.pathExtension == "swift" }
            .map { String($0.path.dropFirst(fixture.link.count + 1)) }
        #expect(!naive.isEmpty)
        #expect(naive.allSatisfy { !$0.hasPrefix("Growin/") }, "naive prefix stripping must be wrong here: \(naive)")
    }

    @Test(arguments: ["real", "link", "viaParent"])
    func swiftSourcesGivesRepoRelativePathsWhicheverWayTheRootIsSpelled(spelling: String) throws {
        let fixture = try Fixture.make()
        defer { fixture.remove() }
        let root = ["real": fixture.real, "link": fixture.link, "viaParent": fixture.viaParent][spelling]!
        let sources = try SourceTree.swiftSources(in: root)
        #expect(sources.map(\.path).sorted() == ["Growin/A.swift", "Growin/Sub/B.swift"])
        #expect(sources.first { $0.path == "Growin/A.swift" }?.text == "let a = 1\n")
    }

    @Test func contentsReadsThroughASymlinkedRoot() throws {
        let fixture = try Fixture.make()
        defer { fixture.remove() }
        #expect(try SourceTree.contents("Growin/Sub/B.swift", in: fixture.link) == "let b = 2\n")
        #expect(try SourceTree.contents("Growin/Sub/B.swift", in: fixture.viaParent) == "let b = 2\n")
    }

    @Test func aSymlinkEscapingTheRootIsRefusedNotSilentlyRebased() throws {
        let fixture = try Fixture.make()
        defer { fixture.remove() }
        let outside = fixture.base + "/outside.swift"
        try "let leak = 0\n".write(toFile: outside, atomically: true, encoding: .utf8)
        try FileManager.default.createSymbolicLink(
            atPath: fixture.real + "/Growin/Escape.swift", withDestinationPath: outside)
        #expect(throws: SourceTree.Failure.self) {
            _ = try SourceTree.swiftSources(in: fixture.link)
        }
        #expect(throws: SourceTree.Failure.self) {
            _ = try SourceTree.contents("Growin/Escape.swift", in: fixture.link)
        }
    }

    @Test func repoRootResolvesToTheRealRepoAndSeesTheApp() throws {
        let root = SourceTree.repoRoot
        #expect(root == SourceTree.realPath(root))
        #expect(FileManager.default.fileExists(atPath: root + "/Growin.xcodeproj"))
        let sources = try SourceTree.swiftSources()
        #expect(sources.count > 10)
        #expect(sources.allSatisfy { $0.path.hasPrefix("Growin/") })
        #expect(sources.contains { $0.path == "Growin/Security/LocalApprovalSigner.swift" })
    }

    /// New scans must reuse SourceTree. A hand-rolled `#filePath` walk is the bug class this fixes.
    @Test func noTestOutsideSourceTreeLocatesSourcesFromTheCompilerPath() throws {
        let needle = "#" + "filePath"
        let allowed: Set<String> = ["GrowinTests/SourceTree.swift", "GrowinTests/SourceTreeTests.swift"]
        let offenders = try SourceTree.swiftSources(under: "GrowinTests")
            .filter { $0.text.contains(needle) && !allowed.contains($0.path) }
            .map(\.path)
        #expect(offenders.isEmpty, "use SourceTree instead of the compiler path: \(offenders)")
    }
}
