#!/usr/bin/env python3
"""Findings ledger check for Growin pull requests (prototype, warn-only).

Every review finding needs a disposition in the PR body. A finding comes
from one of two places:

  GitHub reviews   Each non-empty review body is `gh-r<review id>`. Each
                   top-level inline review comment is `gh-c<comment id>`.
                   The ids are derived, so nobody has to type them in.
  The PR body      Findings that never reach GitHub (T3 reviewer verdicts)
                   use the reviewer's id, `<source>-r<round>-<n>`, for
                   example `codex-r4-p1a`. Once an id appears in any
                   `## Review round N` ledger, it stays known.

The latest `## Review round N` section (highest N, last one if repeated)
must give every known finding one ledger line:

  - `gh-c4337853615` fixed `tests/backend/test_x.py::test_y`
  - `gh-c4339411293` fixed `GrowinTests/SignerSourceScanTests.swift::testAliasFlagged`
  - `codex-r4-p1a` residual: fails closed, tracked under Residuals
  - `grok-r4-2` declined: the window size makes this unreachable
  - `opus-r3-m10` deferred: phase 62
  - `gh-c4339119151` dup `gh-c4337853615`
  - `gh-r5601040128` noted

Table rows work too: `| gh-c4337853615 | fixed | tests/...::test_y |`.

  check   Reads reviews, review comments, and the PR body (from the API or
          from files), prints a report, and with --post keeps one sticky
          PR comment current. With --warn-only it always exits 0.

Standard library only, so it runs under `python3 -I`. Review text is
untrusted (anyone can review a public PR), so only reviewers with a
trusted author association count, and every string that reaches markdown
is reduced to a safe excerpt.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MARKER = "<!-- growin-findings-ledger -->"
BOT_LOGIN = "github-actions[bot]"

DISPOSITIONS = ("fixed", "residual", "declined", "deferred", "dup", "noted")
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
TRUSTED_BOTS = frozenset({"coderabbitai[bot]"})
IGNORED_LOGINS = frozenset({BOT_LOGIN})

ID = r"(?:gh-[rc]\d+|[a-z][a-z0-9]*-r\d+-[a-z0-9]+)"
ID_RE = re.compile(rf"^{ID}$")
ROUND_RE = re.compile(r"^##[ \t]+Review round[ \t]+(\d+)\b.*$", re.IGNORECASE)
H2_RE = re.compile(r"^##[ \t]")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
DISP = "|".join(DISPOSITIONS)
LIST_LINE_RE = re.compile(
    rf"^\s*(?:[-*+]|\d+[.)])\s+`?(?P<id>{ID})`?\s*[:\-]?\s+(?P<disp>{DISP})\b\s*:?\s*(?P<detail>.*)$",
    re.IGNORECASE,
)
TABLE_LINE_RE = re.compile(
    rf"^\s*\|\s*`?(?P<id>{ID})`?\s*\|\s*(?P<disp>{DISP})\s*\|(?P<detail>.*?)\|?\s*$",
    re.IGNORECASE,
)
NODE_RE = re.compile(r"(?P<path>[\w./-]+\.(?:py|swift))::(?P<name>[\w:]+)(?:\[[^\]\s]*\])?")
SWIFT_MODS = (r"(?:@\w+(?:\([^()]*\))?"
              r"|\b(?:private|fileprivate|internal|public|open|static|class|final|override|nonisolated|mutating)\b)")
# XCTest runs `func test...()` instance methods that take no arguments.
SWIFT_XCTEST_RE = re.compile(rf"(?<![\w.])(?P<mods>(?:{SWIFT_MODS}\s+)*)func\s+(?P<name>test\w*)\s*\(\s*\)")
SWIFT_NOT_RUN = {"static", "class", "private", "fileprivate"}
# Swift Testing: `@Test` or `@Test(...)`, then attributes or modifiers, then the func.
SWIFT_TEST_ATTR_RE = re.compile(r"@Test\b")
SWIFT_DECL_RE = re.compile(rf"(?:\s|{SWIFT_MODS})*func\s+(?P<name>\w+)\s*[(<]")
SWIFT_STRING_RE = re.compile(r'(#*)("""|")')
SAFE_PATH_RE = re.compile(r"^[\w./-]{1,200}$")
SAFE_LOGIN_RE = re.compile(r"^[\w-]{1,39}(\[bot\])?$")

MISSING, INVALID, TEST_MISSING, OK = "missing", "invalid", "test-missing", "ok"
PROBLEMS = (MISSING, INVALID, TEST_MISSING)


@dataclass(frozen=True)
class Finding:
    id: str
    author: str
    created_at: str
    commit: str = ""
    path: str = ""
    excerpt: str = ""


@dataclass(frozen=True)
class Entry:
    id: str
    disposition: str
    detail: str


@dataclass
class Row:
    id: str
    status: str
    why: str
    finding: Finding | None = None
    entry: Entry | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Report:
    rows: list[Row]
    latest_round: int | None
    warnings: list[str]

    @property
    def problems(self) -> list[Row]:
        return [r for r in self.rows if r.status in PROBLEMS]


# --- collecting findings ------------------------------------------------------

def excerpt(text: str, limit: int = 90) -> str:
    """First meaningful line of untrusted text, safe to drop into a table cell.

    Skips headings, strips markdown and HTML punctuation (underscores stay,
    they are harmless inside words) and breaks @-mentions, so a review
    comment cannot inject markup, links, or pings into the sticky comment."""
    text = re.sub(r"<!--.*?(-->|$)", "", text or "", flags=re.S)
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        line = re.sub(r"[`*~<>\[\]()|#!\\]", "", line)
        line = line.replace("@", "@\u200b")
        line = " ".join(line.split())
        if line:
            return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return ""


def _trusted(item: dict, trusted_associations, trusted_bots, ignored) -> bool:
    login = (item.get("user") or {}).get("login", "")
    if login in ignored:
        return False
    return item.get("author_association") in trusted_associations or login in trusted_bots


def _before(stamp: str | None, as_of: datetime | None) -> bool:
    return as_of is None or (stamp is not None and _parse_time(stamp) <= as_of)


def collect_github_findings(reviews, comments, *, as_of=None,
                            trusted_associations=TRUSTED_ASSOCIATIONS,
                            trusted_bots=TRUSTED_BOTS, ignored=IGNORED_LOGINS) -> list[Finding]:
    """Derive one finding per non-empty review body and per top-level inline comment.

    Replies are discussion, not findings. Dismissed and pending reviews are
    skipped, and so are their inline comments."""
    out: list[Finding] = []
    dropped_reviews = set()
    for r in reviews:
        if r.get("state") in ("DISMISSED", "PENDING"):
            dropped_reviews.add(r.get("id"))
            continue
        if not (r.get("body") or "").strip():
            continue
        if not _trusted(r, trusted_associations, trusted_bots, ignored):
            continue
        if not _before(r.get("submitted_at"), as_of):
            continue
        out.append(Finding(
            id=f"gh-r{r['id']}", author=_safe_login(r), created_at=r.get("submitted_at", ""),
            commit=(r.get("commit_id") or "")[:7], excerpt=excerpt(r.get("body", "")),
        ))
    for c in comments:
        if c.get("in_reply_to_id") or c.get("pull_request_review_id") in dropped_reviews:
            continue
        if not _trusted(c, trusted_associations, trusted_bots, ignored):
            continue
        if not _before(c.get("created_at"), as_of):
            continue
        path = c.get("path") or ""
        out.append(Finding(
            id=f"gh-c{c['id']}", author=_safe_login(c), created_at=c.get("created_at", ""),
            commit=(c.get("original_commit_id") or c.get("commit_id") or "")[:7],
            path=path if SAFE_PATH_RE.match(path) else "", excerpt=excerpt(c.get("body", "")),
        ))
    out.sort(key=lambda f: (f.created_at, f.id))
    return out


def _safe_login(item: dict) -> str:
    login = (item.get("user") or {}).get("login", "")
    return login if SAFE_LOGIN_RE.match(login) else "unknown"


# --- parsing the PR body ------------------------------------------------------

def split_rounds(body: str) -> list[tuple[int, str]]:
    """`## Review round N` sections in body order. A section runs to the next
    level-2 heading. Headings inside fenced code blocks do not count.

    Fences follow CommonMark: a fence closes only on a bare run of the same
    character at least as long as the one that opened it, so a `~~~` line
    inside a ``` block is content. Indentation is not limited to three
    spaces, because ledger fences often sit inside list items."""
    sections: list[tuple[int, list[str]]] = []
    current: list[str] | None = None
    fence: tuple[str, int] | None = None
    for line in (body or "").splitlines():
        m = FENCE_RE.match(line)
        if m:
            run, rest = m.group(1), m.group(2)
            if fence is None:
                if not (run[0] == "`" and "`" in rest):  # a backtick info string cannot hold backticks
                    fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not rest.strip():
                fence = None
        if fence is None and H2_RE.match(line):
            m = ROUND_RE.match(line)
            if m:
                current = []
                sections.append((int(m.group(1)), current))
                continue
            current = None
        if current is not None:
            current.append(line)
    return [(n, "\n".join(lines)) for n, lines in sections]


def latest_round(rounds: list[tuple[int, str]]) -> tuple[int, str] | None:
    """Highest round number; the last section wins if a number repeats."""
    best = None
    for n, text in rounds:
        if best is None or n >= best[0]:
            best = (n, text)
    return best


def parse_ledger(text: str) -> tuple[dict[str, Entry], list[str]]:
    entries: dict[str, Entry] = {}
    conflicts: list[str] = []
    for line in text.splitlines():
        m = LIST_LINE_RE.match(line) or TABLE_LINE_RE.match(line)
        if not m:
            continue
        entry = Entry(m.group("id").lower(), m.group("disp").lower(), m.group("detail").strip())
        prior = entries.get(entry.id)
        if prior and prior.disposition != entry.disposition:
            conflicts.append(f"`{entry.id}` has two dispositions: {prior.disposition} and {entry.disposition}")
        entries.setdefault(entry.id, entry)
    return entries, conflicts


def mentioned_ids(rounds: list[tuple[int, str]]) -> dict[str, int]:
    """Every id with a ledger line in any round, mapped to the last round that listed it."""
    seen: dict[str, int] = {}
    for n, text in rounds:
        for fid in parse_ledger(text)[0]:
            seen[fid] = n
    return seen


# --- test lookup --------------------------------------------------------------

class TestIndex:
    """Answers "does this test exist, and which finding markers does it carry".

    Python files are parsed with ast. Swift files get a text scan, after
    comments and string literals are blanked, for `@Test func` and XCTest
    `func test...()` methods. Swift tests carry no markers. It never imports,
    builds or runs PR code."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self._cache: dict[str, dict[str, set[str]] | None] = {}

    def lookup(self, path: str, name: str) -> set[str] | None:
        """Finding ids marked on the test, or None if the test is not found.
        For Swift, only the last `::` segment (the function name) counts."""
        tests = self._load(path)
        if tests is None:
            return None
        if path.endswith(".swift"):
            name = name.rsplit("::", 1)[-1]
        return tests.get(name)

    def _load(self, path: str):
        if path in self._cache:
            return self._cache[path]
        result = None
        target = (self.root / path).resolve()
        if (not Path(path).is_absolute() and ".." not in Path(path).parts
                and target.is_relative_to(self.root) and target.is_file()):
            try:
                text = target.read_text(encoding="utf-8")
                if target.suffix == ".swift":
                    result = _index_swift(text)
                elif target.suffix == ".py":
                    result = _index_module(ast.parse(text))
            except (SyntaxError, UnicodeDecodeError, OSError):
                result = None
        self._cache[path] = result
        return result


def _swift_code(text: str) -> str:
    """Swift source with comments removed and string literals emptied, so a
    commented-out or quoted `func testX(` does not count. Newlines are kept.
    Block comments nest, as in SignerSourceScan.stripComments. Strings cover
    "...", multi-line and raw (#"..."#) literals. A `\\(...)` interpolation
    holding its own string is not tracked, which only matters if a test's
    declaration sits inside one."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if text.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif text.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    if text[i] == "\n":
                        out.append("\n")
                    i += 1
            out.append(" ")
        elif m := SWIFT_STRING_RE.match(text, i):
            hashes, quote = m.groups()
            close, i = quote + hashes, m.end()
            while i < n and not text.startswith(close, i):
                if text[i] == "\\" and not hashes:
                    i += 2
                    continue
                if text[i] == "\n":
                    if quote == '"':
                        break  # an unterminated one-line string ends at the line
                    out.append("\n")
                i += 1
            if text.startswith(close, i):
                i += len(close)
            out.append('""')
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _index_swift(text: str) -> dict[str, set[str]]:
    code = _swift_code(text)
    names = {m["name"] for m in SWIFT_XCTEST_RE.finditer(code) if not SWIFT_NOT_RUN & set(m["mods"].split())}
    for m in SWIFT_TEST_ATTR_RE.finditer(code):
        i = m.end()
        while i < len(code) and code[i].isspace():
            i += 1
        if i < len(code) and code[i] == "(":
            depth = 0
            while i < len(code):
                depth += {"(": 1, ")": -1}.get(code[i], 0)
                i += 1
                if depth == 0:
                    break
        if d := SWIFT_DECL_RE.match(code, i):
            names.add(d["name"])
    return {name: set() for name in names}


def _marker_ids(decorators) -> set[str]:
    ids = set()
    for d in decorators:
        if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "finding"
                and isinstance(d.func.value, ast.Attribute) and d.func.value.attr == "mark"):
            ids.update(a.value.lower() for a in d.args if isinstance(a, ast.Constant) and isinstance(a.value, str))
    return ids


def _pytestmark_ids(body) -> set[str]:
    ids = set()
    for node in body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
            ids |= _marker_ids(values)
    return ids


def _index_module(tree: ast.Module) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    _index_body(tree.body, "", _pytestmark_ids(tree.body), out)
    return out


def _index_body(body, prefix: str, inherited: set[str], out: dict[str, set[str]]) -> None:
    """Functions keyed by their pytest node path; nested classes join with `::`."""
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[prefix + node.name] = inherited | _marker_ids(node.decorator_list)
        elif isinstance(node, ast.ClassDef):
            ids = inherited | _marker_ids(node.decorator_list) | _pytestmark_ids(node.body)
            _index_body(node.body, f"{prefix}{node.name}::", ids, out)


# --- evaluation ---------------------------------------------------------------

def evaluate(findings: list[Finding], body: str, tests: TestIndex | None = None) -> Report:
    rounds = split_rounds(body)
    latest = latest_round(rounds)
    entries, conflicts = parse_ledger(latest[1]) if latest else ({}, [])
    earlier = mentioned_ids(rounds)
    by_id = {f.id: f for f in findings}
    warnings = list(conflicts)

    known = list(by_id)
    known += [fid for fid in earlier if fid not in by_id]
    known += [fid for fid in entries if fid not in by_id and fid not in earlier]
    for fid in entries:
        if fid.startswith("gh-") and fid not in by_id:
            warnings.append(f"`{fid}` is in the ledger but is not a GitHub finding on this PR "
                            "(typo, untrusted author, or a dismissed review)")

    rows: list[Row] = []
    for fid in known:
        row = _judge(fid, entries, earlier, latest, tests, by_id)
        row.finding = by_id.get(fid)
        rows.append(row)
    return Report(rows=rows, latest_round=latest[0] if latest else None, warnings=warnings)


def _judge(fid, entries, earlier, latest, tests, by_id, seen=None) -> Row:
    entry = entries.get(fid)
    if entry is None:
        if latest is None:
            return Row(fid, MISSING, "no `## Review round N` section in the PR body")
        if fid in earlier:
            return Row(fid, MISSING, f"listed in round {earlier[fid]} but dropped from round {latest[0]}")
        return Row(fid, MISSING, f"no disposition in round {latest[0]}")

    disp, detail = entry.disposition, entry.detail
    reason = re.sub(r"^[\s:\-]+", "", detail)
    if disp == "fixed":
        nodes = list(NODE_RE.finditer(detail))
        if not nodes:
            return Row(fid, INVALID, "`fixed` needs a test node id "
                       "(`path.py::test_name` or `path.swift::testName`)", entry=entry)
        notes = []
        for m in nodes:
            node = f"{m.group('path')}::{m.group('name')}"
            if tests is None:
                continue
            markers = tests.lookup(m.group("path"), m.group("name"))
            if markers is None:
                return Row(fid, TEST_MISSING, f"`{_code(node)}` not found at this head", entry=entry)
            if fid not in markers and m.group("path").endswith(".py"):
                notes.append(f"`{_code(node)}` has no `finding(\"{fid}\")` marker")
        return Row(fid, OK, "fixed", entry=entry, notes=notes)
    if disp in ("residual", "declined", "deferred"):
        if len(reason) < 3:
            return Row(fid, INVALID, f"`{disp}` needs a reason", entry=entry)
        return Row(fid, OK, disp, entry=entry)
    if disp == "noted":
        if not fid.startswith("gh-r"):
            return Row(fid, INVALID, "`noted` is only for review summaries (`gh-r…`)", entry=entry)
        return Row(fid, OK, "noted", entry=entry)
    # dup: resolve to the target's status.
    m = re.match(ID, reason.strip("`").lower())
    target = m.group(0) if m else ""
    seen = (seen or set()) | {fid}
    if not target or not ID_RE.match(target):
        return Row(fid, INVALID, "`dup` needs the id it duplicates", entry=entry)
    if target in seen:
        return Row(fid, INVALID, f"`dup` chain loops back to `{target}`", entry=entry)
    if target not in entries and target not in by_id and target not in earlier:
        return Row(fid, INVALID, f"`dup` target `{target}` is not a known finding", entry=entry)
    inner = _judge(target, entries, earlier, latest, tests, by_id, seen)
    if inner.status != OK:
        return Row(fid, inner.status, f"dup of `{target}`, which is {inner.status}: {inner.why}", entry=entry)
    return Row(fid, OK, f"dup of `{target}` ({inner.why})", entry=entry, notes=inner.notes)


def _code(text: str) -> str:
    return text.replace("`", "")


# --- rendering ----------------------------------------------------------------

def render(report: Report, *, pr: int | None = None, head_sha: str = "", as_of: str = "") -> str:
    problems = report.problems
    total = len(report.rows)
    head = f" at `{head_sha[:7]}`" if head_sha else ""
    when = f" as of {as_of}" if as_of else ""
    lines = [MARKER, "## Findings ledger (advisory)", ""]
    if report.latest_round is None:
        lines.append(f"No `## Review round N` section in the PR body{head}{when}.")
    else:
        lines.append(f"Checked round {report.latest_round}{head}{when}: "
                     f"{total - len(problems)} of {total} findings have a valid disposition.")
    lines.append("")
    if not total:
        lines.append("No review findings yet. Nothing to check.")
    if problems:
        lines += ["| Finding | Status | Why | Raised by | At | Where | First line |",
                  "|---|---|---|---|---|---|---|"]
        for r in problems:
            f = r.finding
            lines.append("| `{}` | {} | {} | {} | {} | {} | {} |".format(
                r.id, r.status, r.why.replace("|", "/"),
                f.author if f else "PR body", f"`{f.commit}`" if f and f.commit else "",
                f"`{f.path}`" if f and f.path else "", f.excerpt if f else ""))
        missing = [r.id for r in problems if r.status == MISSING]
        if missing:
            lines += ["", f"Paste into `## Review round {report.latest_round or 'N'}` and fill in:", "", "```"]
            lines += [f"- `{fid}` fixed|residual|declined|deferred|dup|noted ..." for fid in missing]
            lines.append("```")
    notes = [(r.id, n) for r in report.rows for n in r.notes]
    if notes or report.warnings:
        lines += ["", "<details><summary>Notes</summary>", ""]
        lines += [f"- `{fid}`: {n}" for fid, n in notes]
        lines += [f"- {w}" for w in report.warnings]
        lines += ["", "</details>"]
    lines += ["", "_Warn-only prototype. It never blocks a merge. Format: `docs/ci/findings-ledger-design.md`._"]
    return "\n".join(lines) + "\n"


def annotations(report: Report) -> list[str]:
    return [f"::warning title=Findings ledger::{r.id} {r.status}: {_annotation_text(r.why)}"
            for r in report.problems]


def _annotation_text(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


# --- GitHub I/O ---------------------------------------------------------------

def gh_api(path: str, *, method: str = "GET", body_file: str | None = None, paginate: bool = False):
    cmd = ["gh", "api", path]
    if method != "GET":
        cmd += ["-X", method]
    if body_file:
        cmd += ["-F", f"body=@{body_file}"]
    if paginate:
        cmd += ["--paginate", "--jq", ".[]"]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    if paginate:
        return [json.loads(line) for line in out.splitlines() if line.strip()]
    return json.loads(out) if out.strip() else None


def post_sticky(repo: str, pr: int, head_sha: str, comment_file: str, api=gh_api) -> str:
    current = api(f"repos/{repo}/pulls/{pr}")["head"]["sha"]
    if head_sha and current != head_sha:
        return f"PR #{pr} moved to {current[:7]}; skipped the stale report"
    existing = [c["id"] for c in api(f"repos/{repo}/issues/{pr}/comments", paginate=True)
                if (c.get("user") or {}).get("login") == BOT_LOGIN and (c.get("body") or "").startswith(MARKER)]
    if existing:
        api(f"repos/{repo}/issues/comments/{existing[-1]}", method="PATCH", body_file=comment_file)
        return f"updated comment {existing[-1]}"
    api(f"repos/{repo}/issues/{pr}/comments", method="POST", body_file=comment_file)
    return "posted a new comment"


def _parse_time(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _load_json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--repo", required=True)
    c.add_argument("--pr", type=int, required=True)
    c.add_argument("--head-sha", default="")
    c.add_argument("--reviews-file", help="JSON list from pulls/N/reviews (default: the API)")
    c.add_argument("--comments-file", help="JSON list from pulls/N/comments (default: the API)")
    c.add_argument("--body-file", help="PR body as markdown (default: the API)")
    c.add_argument("--as-of", help="ignore reviews and comments after this ISO time (dry runs)")
    c.add_argument("--repo-root", help="check that `fixed` tests exist here (ast only, no imports)")
    c.add_argument("--out", help="write the markdown report here")
    c.add_argument("--post", action="store_true", help="create or update the sticky PR comment")
    c.add_argument("--annotate", action="store_true", help="print ::warning annotations")
    c.add_argument("--warn-only", action="store_true", help="always exit 0")
    a = p.parse_args(argv)

    try:
        reviews = _load_json(a.reviews_file) if a.reviews_file else \
            gh_api(f"repos/{a.repo}/pulls/{a.pr}/reviews", paginate=True)
        comments = _load_json(a.comments_file) if a.comments_file else \
            gh_api(f"repos/{a.repo}/pulls/{a.pr}/comments", paginate=True)
        if a.body_file:
            body = Path(a.body_file).read_text(encoding="utf-8")
        else:
            pr = gh_api(f"repos/{a.repo}/pulls/{a.pr}")
            body = pr.get("body") or ""
            a.head_sha = a.head_sha or pr["head"]["sha"]
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        print(f"::warning title=Findings ledger::could not read PR #{a.pr}: {type(exc).__name__}")
        return 0 if a.warn_only else 2

    as_of = _parse_time(a.as_of) if a.as_of else None
    findings = collect_github_findings(reviews, comments, as_of=as_of)
    report = evaluate(findings, body, TestIndex(Path(a.repo_root)) if a.repo_root else None)
    text = render(report, pr=a.pr, head_sha=a.head_sha, as_of=a.as_of or "")
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    print(text)
    if a.annotate:
        print("\n".join(annotations(report)))
    if a.post:
        if not a.out:
            print("::warning title=Findings ledger::--post needs --out")
        else:
            try:
                print(post_sticky(a.repo, a.pr, a.head_sha, a.out))
            except (subprocess.CalledProcessError, KeyError, TypeError) as exc:
                print(f"::warning title=Findings ledger::could not post the comment: {type(exc).__name__}")
    if a.warn_only:
        return 0
    return 1 if report.problems else 0


if __name__ == "__main__":
    sys.exit(main())
