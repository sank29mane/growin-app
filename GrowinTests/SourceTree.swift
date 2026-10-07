import Foundation

/// Symlink-safe access to the app sources for the source-scan tests.
///
/// `FileManager.enumerator(at:)` returns fully resolved paths, while a root built from `#filePath`
/// keeps whatever spelling the compiler was given. When the repo sits behind a symlink (`~/.cache`
/// worktrees, `/tmp` -> `/private/tmp`, `/var` -> `/private/var`) the two spellings differ and any
/// `dropFirst(root.count + 1)` or `hasPrefix(root)` comparison silently yields garbage paths.
/// Every scan must go through this type so both sides use one `realpath(3)` spelling.
enum SourceTree {
    enum Failure: Error, CustomStringConvertible {
        case unreadable(String)
        case missingDirectory(String)
        case noSwiftFiles(String)
        case escapesRoot(path: String, root: String)

        var description: String {
            switch self {
            case .unreadable(let path): return "cannot read \(path)"
            case .missingDirectory(let path): return "scan root \(path) is not a directory"
            case .noSwiftFiles(let path): return "scan root \(path) holds no .swift files, so a scan over it would pass vacuously"
            case .escapesRoot(let path, let root): return "\(path) resolves outside \(root)"
            }
        }
    }

    /// Resolves every symlink with realpath(3). Falls back to the input if the path does not exist.
    static func realPath(_ path: String) -> String {
        guard let resolved = realpath(path, nil) else { return path }
        defer { free(resolved) }
        return String(cString: resolved)
    }

    /// The repo root, resolved. Anchored on this file (GrowinTests/SourceTree.swift), so the depth
    /// of the calling test file never matters.
    static var repoRoot: String {
        realPath(
            URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent()
                .deletingLastPathComponent()
                .path)
    }

    /// Repo-relative path of `path` under `root`, both resolved. Throws if it is not inside `root`.
    static func relativePath(of path: String, under root: String) throws -> String {
        let resolvedRoot = realPath(root)
        let resolved = realPath(path)
        guard resolved.hasPrefix(resolvedRoot + "/") else {
            throw Failure.escapesRoot(path: resolved, root: resolvedRoot)
        }
        return String(resolved.dropFirst(resolvedRoot.count + 1))
    }

    /// Every `.swift` file under `<root>/<directory>`, as (root-relative path, contents).
    static func swiftSources(
        under directory: String = "Growin",
        in root: String = SourceTree.repoRoot
    ) throws -> [(path: String, text: String)] {
        let resolvedRoot = realPath(root)
        let start = realPath(resolvedRoot + "/" + directory)
        // An enumerator over a missing directory yields nothing, and every "no file contains X"
        // scan would then pass. Refuse a missing or empty root instead of returning [].
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: start, isDirectory: &isDirectory), isDirectory.boolValue else {
            throw Failure.missingDirectory(start)
        }
        guard let enumerator = FileManager.default.enumerator(
            at: URL(fileURLWithPath: start), includingPropertiesForKeys: nil
        ) else {
            throw Failure.unreadable(start)
        }
        var sources: [(String, String)] = []
        for case let url as URL in enumerator where url.pathExtension == "swift" {
            let relative = try relativePath(of: url.path, under: resolvedRoot)
            let text = try String(contentsOfFile: resolvedRoot + "/" + relative, encoding: .utf8)
            sources.append((relative, text))
        }
        guard !sources.isEmpty else { throw Failure.noSwiftFiles(start) }
        return sources
    }

    /// Contents of one repo-relative file.
    static func contents(_ relativePath: String, in root: String = SourceTree.repoRoot) throws -> String {
        let resolvedRoot = realPath(root)
        let full = realPath(resolvedRoot + "/" + relativePath)
        _ = try Self.relativePath(of: full, under: resolvedRoot)
        return try String(contentsOfFile: full, encoding: .utf8)
    }
}
