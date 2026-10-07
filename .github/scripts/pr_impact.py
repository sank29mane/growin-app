#!/usr/bin/env python3
"""PR impact report for Growin.

  collect  Runs inside the PR's own CI job (unprivileged). Reduces the pytest
           JUnit XML files and a coverage.py JSON report to one small
           metrics.json.
  report   Runs in the privileged workflow_run job, from the default branch.
           Treats both metrics files as untrusted data, validates their shape,
           compares the PR against main, and renders the PR comment. With
           --repo and --pr it also reads the PR's file list and merge state
           from the GitHub API (data only, never PR code).

Standard library only, so it runs under `python3 -I` on any runner.
`report` is advisory and exits 0 even when budgets are exceeded or metrics
fail validation. Configuration errors exit 2.

Every GitHub API call goes through gh_api(), so tests can swap in a fake.
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import re
import sys
import subprocess
import time
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

SCHEMA = 1
MARKER = "<!-- growin-pr-impact -->"
MAX_METRICS_BYTES = 5_000_000
MAX_FILES = 20_000
MAX_BENCH_TESTS = 500
PATH_RE = re.compile(r"^[A-Za-z0-9_./-]{1,240}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_./:\[\]-]{1,200}$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
CI_CONCLUSIONS = {
    "success", "failure", "cancelled", "timed_out", "skipped",
    "neutral", "action_required", "startup_failure", "stale",
}
EXCLUDED_PREFIXES = (
    "backend/.venv/",
    "backend/venv_forecast/",
    "backend/growin_core_src/",
)
EPS = 1e-9

METRIC_IDS = {
    "tests.total", "tests.failed", "tests.skipped", "tests.duration_s",
    "bench.duration_s", "cov.total_pct", "cov.safety_pct",
}
RULE_KEYS = {"max", "min", "max_delta", "min_delta", "max_delta_pct"}
UNITS = {"count", "seconds", "percent"}
ICONS = {"ok": "✅", "warn": "⚠️", "na": "➖"}


class MetricsError(ValueError):
    """A metrics or budget file is missing, malformed, or out of range."""


# --------------------------------------------------------------------------
# collect
# --------------------------------------------------------------------------

def _suites(path: Path) -> list[ET.Element]:
    root = ET.parse(path).getroot()
    return root.findall("testsuite") if root.tag == "testsuites" else [root]


def read_junit(path: Path, per_test: bool = False) -> dict:
    total = failed = skipped = 0
    duration = 0.0
    cases: dict[str, float] = {}
    for suite in _suites(path):
        total += int(suite.get("tests", 0))
        failed += int(suite.get("failures", 0)) + int(suite.get("errors", 0))
        skipped += int(suite.get("skipped", 0))
        duration += float(suite.get("time", 0))
        if per_test:
            for case in suite.iter("testcase"):
                module = (case.get("classname") or "").rsplit(".", 1)[-1]
                cases[f"{module}::{case.get('name', '')}"] = round(float(case.get("time", 0)), 3)
    out = {"total": total, "failed": failed, "skipped": skipped, "duration_s": round(duration, 2)}
    if per_test:
        out["tests"] = cases
    return out


def read_coverage(path: Path, root: Path) -> dict:
    data = json.loads(path.read_text())
    files: dict[str, list[int]] = {}
    for name, entry in data.get("files", {}).items():
        p = Path(name.replace("\\", "/"))
        if p.is_absolute():
            try:
                p = p.relative_to(root)
            except ValueError:
                continue
        rel = p.as_posix().removeprefix("./")
        if not rel.startswith("backend/") or rel.startswith(EXCLUDED_PREFIXES):
            continue
        summary = entry.get("summary", {})
        statements = int(summary.get("num_statements", 0))
        if statements > 0:
            files[rel] = [int(summary.get("covered_lines", 0)), statements]
    return {"files": files}


def cmd_collect(args: argparse.Namespace) -> int:
    metrics: dict = {"schema": SCHEMA}
    if args.junit and Path(args.junit).is_file():
        tests = read_junit(Path(args.junit))
        metrics["tests"] = tests
    if args.bench_junit and Path(args.bench_junit).is_file():
        bench = read_junit(Path(args.bench_junit), per_test=True)
        metrics["benchmarks"] = {"duration_s": bench["duration_s"], "tests": bench["tests"]}
    if args.coverage and Path(args.coverage).is_file():
        metrics["coverage"] = read_coverage(Path(args.coverage), Path(args.root).resolve())
    kept = ", ".join(k for k in metrics if k != "schema")
    if not kept:
        # No artifact is better than an all-empty one: the report then says
        # CI stopped early instead of showing a table of blanks.
        print("No test or coverage reports found. Nothing to collect.")
        return 0
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(metrics, sort_keys=True))
    print(f"Wrote {args.out}: {kept}")
    return 0


# --------------------------------------------------------------------------
# validate (untrusted input)
# --------------------------------------------------------------------------

def _num(obj: object, key: str, integer: bool = False) -> float:
    if not isinstance(obj, dict) or key not in obj:
        raise MetricsError(f"missing '{key}'")
    v = obj[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise MetricsError(f"'{key}' is not a number")
    if integer and not isinstance(v, int):
        raise MetricsError(f"'{key}' is not an integer")
    if not math.isfinite(v) or v < 0:
        raise MetricsError(f"'{key}' is out of range")
    return v


def load_metrics(path: Path) -> dict:
    try:
        return _load_metrics(path)
    except (OverflowError, RecursionError):
        raise MetricsError("metrics exceed numeric or nesting limits") from None


def _load_metrics(path: Path) -> dict:
    try:
        if path.stat().st_size > MAX_METRICS_BYTES:
            raise MetricsError("metrics file is too large")
        raw = json.loads(path.read_text())
    except OSError:
        raise MetricsError("metrics file is unreadable") from None
    except ValueError:
        raise MetricsError("metrics file is not valid JSON") from None
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        raise MetricsError("unsupported metrics schema")

    out: dict = {"tests": None, "benchmarks": None, "coverage": None}
    t = raw.get("tests")
    if t is not None:
        out["tests"] = {
            "total": _num(t, "total", True),
            "failed": _num(t, "failed", True),
            "skipped": _num(t, "skipped", True),
            "duration_s": _num(t, "duration_s"),
        }
    b = raw.get("benchmarks")
    if b is not None:
        cases = b.get("tests") if isinstance(b, dict) else None
        if not isinstance(cases, dict) or len(cases) > MAX_BENCH_TESTS:
            raise MetricsError("benchmark tests are malformed")
        checked = {}
        for name, secs in cases.items():
            if not NAME_RE.match(name):
                raise MetricsError("benchmark test name is not allowed")
            checked[name] = _num(cases, name)
        out["benchmarks"] = {"duration_s": _num(b, "duration_s"), "tests": checked}
    c = raw.get("coverage")
    if c is not None:
        files = c.get("files") if isinstance(c, dict) else None
        if not isinstance(files, dict) or len(files) > MAX_FILES:
            raise MetricsError("coverage files are malformed")
        checked_files = {}
        for name, pair in files.items():
            if not PATH_RE.match(name):
                raise MetricsError("coverage path is not allowed")
            if not (isinstance(pair, list) and len(pair) == 2):
                raise MetricsError("coverage entry is malformed")
            covered = _num({"v": pair[0]}, "v", True)
            total = _num({"v": pair[1]}, "v", True)
            if total == 0 or covered > total:
                raise MetricsError("coverage entry is out of range")
            checked_files[name] = (int(covered), int(total))
        out["coverage"] = checked_files
    return out


def load_budget(path: Path) -> list[dict]:
    try:
        rows = json.loads(path.read_text())["rows"]
    except (OSError, ValueError, KeyError, TypeError):
        raise MetricsError("budget file is unreadable") from None
    for row in rows:
        if row.get("id") not in METRIC_IDS or row.get("unit") not in UNITS:
            raise MetricsError(f"budget row has an unknown id or unit: {row.get('id')}")
        rule = row.get("rule")
        if not isinstance(rule, dict) or not rule or set(rule) - RULE_KEYS:
            raise MetricsError(f"budget row {row['id']} has an invalid rule")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in rule.values()):
            raise MetricsError(f"budget row {row['id']} has a non-numeric rule")
        if not isinstance(row.get("warn"), bool) or not row.get("label") or not row.get("area"):
            raise MetricsError(f"budget row {row['id']} is incomplete")
    return rows


def load_patterns(path: Path) -> list[str]:
    patterns = [
        line.strip() for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not patterns:
        raise MetricsError("no safety patterns loaded")
    return patterns


# --------------------------------------------------------------------------
# safety-path matcher
#
# A port of safety-guard.sh's `[[ "$path" == $pattern ]]`. Inside [[ ]] bash
# matches as if extglob were on, `*` and `?` also cross `/`, and a leading
# `.` needs no special match. Supported: * ? [...] [!...] [^...] [:class:]
# ranges, backslash escapes, and ?(..) *(..) +(..) @(..) !(..) with `|`.
# The matcher tracks the set of reachable end positions, so it never
# backtracks exponentially.
# --------------------------------------------------------------------------

MAX_MATCH_LEN = 1024
_ASCII_PUNCT = set("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_POSIX_CLASSES = {
    "alnum": lambda c: c.isascii() and c.isalnum(),
    "alpha": lambda c: c.isascii() and c.isalpha(),
    "ascii": lambda c: ord(c) < 128,
    "blank": lambda c: c in " \t",
    "cntrl": lambda c: ord(c) < 32 or ord(c) == 127,
    "digit": lambda c: c in "0123456789",
    "graph": lambda c: 33 <= ord(c) <= 126,
    "lower": lambda c: "a" <= c <= "z",
    "print": lambda c: 32 <= ord(c) <= 126,
    "punct": lambda c: c in _ASCII_PUNCT,
    "space": lambda c: c in " \t\n\r\f\v",
    "upper": lambda c: "A" <= c <= "Z",
    "word": lambda c: c == "_" or (c.isascii() and c.isalnum()),
    "xdigit": lambda c: c in "0123456789abcdefABCDEF",
}


def _parse_bracket(p: str, i: int) -> tuple[tuple | None, int]:
    """Parse `[...]` at p[i]. Returns (None, i) when it is a literal `[`."""
    j = i + 1
    neg = j < len(p) and p[j] in "!^"
    if neg:
        j += 1
    items: list[tuple] = []
    first = True
    while j < len(p):
        c = p[j]
        if c == "]" and not first:
            return ("cls", neg, tuple(items)), j + 1
        first = False
        if c == "[" and p.startswith(":", j + 1):
            end = p.find(":]", j + 2)
            if end != -1:
                items.append(("class", p[j + 2:end]))
                j = end + 2
                continue
        if c == "\\" and j + 1 < len(p):
            j += 1
            c = p[j]
        if j + 2 < len(p) and p[j + 1] == "-" and p[j + 2] != "]":
            hi_at = j + 2
            if p[hi_at] == "\\" and hi_at + 1 < len(p):
                hi_at += 1
            items.append(("range", c, p[hi_at]))
            j = hi_at + 1
            continue
        items.append(("char", c))
        j += 1
    return None, i


def _parse_ext(p: str, i: int) -> tuple[tuple | None, int]:
    """Parse the pattern list after `op(`. Returns (None, i) if unterminated."""
    alts = []
    j = i
    while True:
        seq, j = _parse_seq(p, j, in_ext=True)
        alts.append(seq)
        if j >= len(p):
            return None, i
        if p[j] == ")":
            return tuple(alts), j + 1
        j += 1  # skip `|`


def _parse_seq(p: str, i: int, in_ext: bool) -> tuple[tuple, int]:
    nodes: list[tuple] = []
    while i < len(p):
        c = p[i]
        if c == "\\" and i + 1 < len(p):
            nodes.append(("lit", p[i + 1]))
            i += 2
            continue
        if c == "[":
            cls, j = _parse_bracket(p, i)
            if cls is not None:
                nodes.append(cls)
                i = j
                continue
        if in_ext and c in "|)":
            return tuple(nodes), i
        if c in "?*+@!" and p.startswith("(", i + 1):
            alts, j = _parse_ext(p, i + 2)
            if alts is not None:
                nodes.append(("ext", c, alts))
                i = j
                continue
        if c == "*":
            if not nodes or nodes[-1] != ("star",):
                nodes.append(("star",))
        elif c == "?":
            nodes.append(("any",))
        else:
            nodes.append(("lit", c))
        i += 1
    return tuple(nodes), i


@functools.lru_cache(maxsize=512)
def _compile_glob(pattern: str) -> tuple:
    return _parse_seq(pattern, 0, in_ext=False)[0]


def _class_hit(node: tuple, ch: str) -> bool:
    hit = False
    for item in node[2]:
        kind = item[0]
        if kind == "char":
            hit = ch == item[1]
        elif kind == "range":
            hit = item[1] <= ch <= item[2]
        else:
            test = _POSIX_CLASSES.get(item[1])
            hit = bool(test and test(ch))
        if hit:
            break
    return hit != node[1]


def _seq_ends(seq: tuple, s: str, start: int) -> set[int]:
    cur = {start}
    for node in seq:
        nxt: set[int] = set()
        for pos in cur:
            nxt |= _node_ends(node, s, pos)
        if not nxt:
            return nxt
        cur = nxt
    return cur


def _node_ends(node: tuple, s: str, pos: int) -> set[int]:
    n = len(s)
    kind = node[0]
    if kind == "lit":
        return {pos + 1} if pos < n and s[pos] == node[1] else set()
    if kind == "any":
        return {pos + 1} if pos < n else set()
    if kind == "star":
        return set(range(pos, n + 1))
    if kind == "cls":
        return {pos + 1} if pos < n and _class_hit(node, s[pos]) else set()

    op, alts = node[1], node[2]

    def once(at: int) -> set[int]:
        out: set[int] = set()
        for alt in alts:
            out |= _seq_ends(alt, s, at)
        return out

    def closure(seed: set[int]) -> set[int]:
        seen = set(seed)
        frontier = set(seed)
        while frontier:
            fresh: set[int] = set()
            for at in frontier:
                fresh |= once(at)
            frontier = fresh - seen
            seen |= frontier
        return seen

    if op == "@":
        return once(pos)
    if op == "?":
        return once(pos) | {pos}
    if op == "*":
        return closure({pos})
    if op == "+":
        return closure(once(pos))
    hits = once(pos)  # "!": any span that no alternative matches in full
    return {end for end in range(pos, n + 1) if end not in hits}


def glob_match(path: str, pattern: str) -> bool:
    """True when bash `[[ $path == $pattern ]]` would be true."""
    if len(path) > MAX_MATCH_LEN or len(pattern) > MAX_MATCH_LEN:
        return False
    return len(path) in _seq_ends(_compile_glob(pattern), path, 0)


def matches_safety(path: str | None, patterns: list[str]) -> bool:
    """safety-guard.sh `matches`: an empty path never matches."""
    return bool(path) and any(glob_match(path, p) for p in patterns)


def is_test_path(path: str | None) -> bool:
    """safety-guard.sh `is_test`."""
    return bool(path) and path.startswith(("tests/", "GrowinTests/", "GrowinUITests/"))


# --------------------------------------------------------------------------
# change footprint and merge readiness (GitHub API data, never PR code)
# --------------------------------------------------------------------------

AREAS = (
    ("backend/", "Backend app"),
    ("tests/", "Backend tests"),
    ("GrowinTests/", "Swift tests"),
    ("GrowinUITests/", "Swift tests"),
    ("Growin/", "Swift app"),
    ("Growin.xcodeproj/", "Swift app"),
    (".github/", "CI"),
    ("gateway/", "Gateway"),
)
AREA_ORDER = ("Backend app", "Backend tests", "Swift app", "Swift tests",
              "CI", "Gateway", "Docs and other")
MAX_PR_FILES = 3000          # the PR files API stops here
MAX_LISTED_SAFETY = 40
REVIEW_LABEL = "safety-reviewed"
DISPLAY_PATH_RE = re.compile(r"^[A-Za-z0-9_./ +@(),=~-]{1,240}$")
REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
FILE_STATUSES = {"added", "removed", "modified", "renamed", "copied", "changed", "unchanged"}
MERGE_STATES = {
    "clean": "clean",
    "dirty": "conflicts",
    "blocked": "blocked until required checks and label pass",
    "behind": "behind base",
    "unstable": "non-required checks failing",
    "has_hooks": "clean (hooks pending)",
    "draft": "draft",
}


def gh_api(path: str, method: str = "GET", payload: dict | None = None) -> object:
    """The only place this script and pr_dashboard.py talk to GitHub."""
    cmd = ["gh", "api", "-X", method, "-H", "Accept: application/vnd.github+json", path]
    if payload is not None:
        cmd += ["--input", "-"]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=60,
                            input=json.dumps(payload) if payload is not None else None)
    return json.loads(result.stdout) if result.stdout.strip() else None


def api_pages(api, path: str, limit: int) -> list:
    """Follow ?page=N until a short page or `limit` items."""
    sep = "&" if "?" in path else "?"
    items: list = []
    page = 1
    while len(items) < limit:
        chunk = api(f"{path}{sep}per_page=100&page={page}")
        if not isinstance(chunk, list):
            raise ValueError("expected a list from the GitHub API")
        items += chunk
        if len(chunk) < 100:
            break
        page += 1
    return items[:limit]


def display_path(path: str) -> str:
    """Render an attacker-controlled file name safely inside a code span."""
    return f"`{path}`" if DISPLAY_PATH_RE.match(path) and "`" not in path else "(unprintable path)"


def display_ref(ref: str) -> str:
    return f"`{ref}`" if REF_RE.match(ref) else "(unusual branch name)"


def area_of(path: str) -> str:
    for prefix, name in AREAS:
        if path.startswith(prefix):
            return name
    return "Docs and other"


def _int(v: object) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def footprint(files: list, patterns: list[str]) -> dict:
    """Group PR files by area and list what Safety Guard would flag."""
    areas: dict[str, list[int]] = {}
    safety: list[dict] = []
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("filename"), str):
            continue
        path = f["filename"]
        prev = f.get("previous_filename") if isinstance(f.get("previous_filename"), str) else ""
        status = f.get("status") if isinstance(f.get("status"), str) else "modified"
        row = areas.setdefault(area_of(path), [0, 0, 0])
        row[0] += 1
        row[1] += _int(f.get("additions"))
        row[2] += _int(f.get("deletions"))
        if matches_safety(path, patterns) or matches_safety(prev, patterns):
            safety.append({"kind": "path", "path": path, "previous": prev, "status": status})
        if status == "removed" and is_test_path(path):
            safety.append({"kind": "deleted test", "path": path, "previous": "", "status": status})
        if status == "renamed" and is_test_path(prev) and not is_test_path(path):
            safety.append({"kind": "test moved out", "path": path, "previous": prev, "status": status})
    ordered = {name: areas[name] for name in AREA_ORDER if name in areas}
    return {"areas": ordered, "safety": safety}


def find_parent(base_ref: str, repo: str, open_prs: list) -> int | None:
    """The open PR whose head branch (in this repo) is `base_ref`."""
    for pr in open_prs:
        try:
            head = pr["head"]
            if head["ref"] == base_ref and head["repo"]["full_name"] == repo:
                number = pr["number"]
                if isinstance(number, int) and not isinstance(number, bool):
                    return number
        except (KeyError, TypeError):
            continue
    return None


def build_context(pull: dict, files: list, parent: int | None, patterns: list[str]) -> dict:
    labels = [lb.get("name") for lb in pull.get("labels") or [] if isinstance(lb, dict)]
    fp = footprint(files, patterns)
    mergeable = pull.get("mergeable")
    return {
        "draft": pull.get("draft") is True,
        "base_ref": str(pull["base"]["ref"]),
        "default_branch": str(pull["base"]["repo"].get("default_branch") or "main"),
        "parent": parent,
        "mergeable": mergeable if isinstance(mergeable, bool) else None,
        "mergeable_state": str(pull.get("mergeable_state") or "unknown"),
        "labeled": REVIEW_LABEL in labels,
        "footprint": fp,
        "safety_required": bool(fp["safety"]),
        "files_truncated": len(files) >= MAX_PR_FILES,
    }


def fetch_pull(api, repo: str, pr: int, sleep=time.sleep) -> dict:
    """GET one PR. GitHub computes `mergeable` lazily, so ask once more."""
    pull = api(f"repos/{repo}/pulls/{int(pr)}")
    if pull.get("mergeable") is None and pull.get("state") == "open":
        sleep(3)
        pull = api(f"repos/{repo}/pulls/{int(pr)}")
    return pull


def fetch_pr_context(repo: str, pr: int, patterns: list[str], api=None,
                     sleep=time.sleep) -> dict:
    api = api or gh_api
    if not REPO_RE.match(repo):
        raise ValueError("repo is not owner/name")
    pull = fetch_pull(api, repo, pr, sleep)
    files = api_pages(api, f"repos/{repo}/pulls/{int(pr)}/files", MAX_PR_FILES)
    base_ref = pull["base"]["ref"]
    parent = None
    if base_ref != (pull["base"]["repo"].get("default_branch") or "main"):
        owner = repo.split("/")[0]
        heads = api(f"repos/{repo}/pulls?state=open&head={quote(owner + ':' + base_ref, safe='')}&per_page=100")
        parent = find_parent(base_ref, repo, heads if isinstance(heads, list) else [])
    return build_context(pull, files, parent, patterns)


def readiness_line(ctx: dict) -> str:
    parts = ["Draft" if ctx["draft"] else "Ready for review"]
    base = f"base {display_ref(ctx['base_ref'])}"
    if ctx["base_ref"] != ctx["default_branch"]:
        base += f" (stacked on #{ctx['parent']})" if ctx["parent"] else " (no open parent PR found)"
    parts.append(base)
    if ctx["mergeable"] is False:
        parts.append("mergeable: no, conflicts")
    elif ctx["mergeable"] is None:
        parts.append("mergeable: still computing")
    else:
        parts.append(f"mergeable: {MERGE_STATES.get(ctx['mergeable_state'], 'yes')}")
    if not ctx["safety_required"]:
        label = "not required" + (" (present)" if ctx["labeled"] else "")
    else:
        label = "required, present" if ctx["labeled"] else "required, missing"
    parts.append(f"`{REVIEW_LABEL}`: {label}")
    return "**Merge readiness:** " + " · ".join(parts)


def render_footprint(ctx: dict | None) -> str:
    if ctx is None:
        return "### Change footprint\n\nUnavailable: the GitHub API lookup failed.\n"
    fp = ctx["footprint"]
    out = ["### Change footprint", ""]
    if not fp["areas"]:
        out += ["No changed files reported.", ""]
    else:
        out += ["| Area | Files | Added | Removed |", "| :-- | --: | --: | --: |"]
        tot = [0, 0, 0]
        for name, (n, add, rm) in fp["areas"].items():
            out.append(f"| {name} | {n:,} | +{add:,} | -{rm:,} |")
            tot = [tot[0] + n, tot[1] + add, tot[2] + rm]
        out += [f"| **Total** | **{tot[0]:,}** | **+{tot[1]:,}** | **-{tot[2]:,}** |", ""]
    if ctx["files_truncated"]:
        out += [f"The file list stops at {MAX_PR_FILES:,} files, so totals are a floor.", ""]
    hits = fp["safety"]
    if not hits:
        out += ["No safety paths touched.", ""]
        return "\n".join(out)
    state = "present" if ctx["labeled"] else "missing"
    out += [f"**Safety paths ({len(hits)}).** These need the `{REVIEW_LABEL}` label "
            f"from a human before Safety Guard passes. Label: {state}.", ""]
    for hit in hits[:MAX_LISTED_SAFETY]:
        path = display_path(hit["path"])
        if hit["kind"] == "path":
            if hit["previous"]:
                note = f"renamed from {display_path(hit['previous'])}"
            else:
                note = hit["status"] if hit["status"] in FILE_STATUSES else "changed"
            out.append(f"- {path} ({note})")
        elif hit["kind"] == "deleted test":
            out.append(f"- {path} (deleted test)")
        else:
            out.append(f"- {path} (test moved out of {display_path(hit['previous'])})")
    if len(hits) > MAX_LISTED_SAFETY:
        out.append(f"- and {len(hits) - MAX_LISTED_SAFETY} more")
    out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# headline verdict and one-line summary
# --------------------------------------------------------------------------

CI_RED = {
    "failure": "CI failed",
    "cancelled": "CI cancelled",
    "timed_out": "CI timed out",
    "startup_failure": "CI failed to start",
    "action_required": "CI is waiting for approval",
    "stale": "CI result is stale",
}


def headline(ci: str, hv: dict[str, float], rows: list[dict], states: list[str],
             ctx: dict | None = None, notice: str | None = None,
             has_base: bool = True) -> str:
    """One line: 🟢 Healthy, 🟡 Healthy, needs attention: ..., or 🔴 ...

    Red: CI did not succeed, or tests failed.
    Yellow: anything a reviewer should look at before merging (budgets over,
    metrics missing, conflicts, a missing safety label).
    Green: none of the above. Never green without test metrics.
    """
    red: list[str] = []
    if ci in CI_RED:
        red.append(CI_RED[ci])
    failed = int(hv.get("tests.failed", 0))
    if failed:
        red.append(f"{failed:,} failing test{'s' if failed != 1 else ''}")
    if red:
        return "🔴 " + "; ".join(red)
    attention: list[str] = []
    if ci not in ("success", "neutral", "skipped"):
        attention.append("CI result unknown")
    if notice:
        attention.append(notice)
    elif "tests.total" not in hv:
        attention.append("test metrics missing")
    elif "cov.total_pct" not in hv:
        attention.append("coverage missing")
    over = [r["label"] for r, s in zip(rows, states) if s == "warn" and r["id"] != "tests.failed"]
    if over:
        attention.append(f"over budget: {', '.join(over)}")
    if ctx is not None:
        if ctx["mergeable"] is False:
            attention.append("merge conflicts")
        if ctx["safety_required"] and not ctx["labeled"]:
            attention.append(f"needs `{REVIEW_LABEL}` label")
    if attention:
        return "🟡 Healthy, needs attention: " + "; ".join(attention)
    return "🟢 Healthy" if has_base else "🟢 Healthy (no main baseline to compare)"


def summary_line(hv: dict[str, float], bv: dict[str, float] | None) -> str:
    bv = bv or {}

    def part(label: str, key: str, unit: str, pct_delta: bool = False,
             no_delta: bool = False) -> str:
        h = hv.get(key)
        if h is None:
            return f"{label} n/a"
        text = f"{label} {fmt_value(unit, h)}"
        b = bv.get(key)
        if b is not None and not no_delta:
            d = h - b
            if pct_delta:
                text += f" ({d / b * 100:+.1f}%)" if b > 0 else ""
            elif unit == "percent":
                text += f" ({d:+.2f} pp)"
            else:
                text += f" ({int(d):+,})"
        return text

    return " · ".join([
        part("Tests", "tests.total", "count"),
        part("Failed", "tests.failed", "count", no_delta=True),
        part("Coverage", "cov.total_pct", "percent"),
        part("Safety-path coverage", "cov.safety_pct", "percent"),
        part("Suite time", "tests.duration_s", "seconds", pct_delta=True),
    ])


# --------------------------------------------------------------------------
# compare
# --------------------------------------------------------------------------

def _pct(files: dict[str, tuple[int, int]]) -> float | None:
    total = sum(t for _, t in files.values())
    return round(sum(c for c, _ in files.values()) / total * 100, 2) if total else None


def derive(m: dict, patterns: list[str]) -> dict[str, float]:
    """Flatten validated metrics into the ids budget rows refer to."""
    vals: dict[str, float] = {}
    if m["tests"]:
        vals["tests.total"] = m["tests"]["total"]
        vals["tests.failed"] = m["tests"]["failed"]
        vals["tests.skipped"] = m["tests"]["skipped"]
        vals["tests.duration_s"] = round(m["tests"]["duration_s"], 1)
    if m["benchmarks"]:
        vals["bench.duration_s"] = round(m["benchmarks"]["duration_s"], 2)
    if m["coverage"]:
        files = m["coverage"]
        overall = _pct(files)
        if overall is not None:
            vals["cov.total_pct"] = overall
        # Same glob semantics as safety-guard.sh: `*` also crosses `/`.
        safe = {p: v for p, v in files.items() if matches_safety(p, patterns)}
        safety = _pct(safe)
        if safety is not None:
            vals["cov.safety_pct"] = safety
    return vals


def evaluate(row: dict, head: dict[str, float], base: dict[str, float] | None) -> str:
    """Return ok, warn (advisory breach), or na (not evaluated)."""
    h = head.get(row["id"])
    if h is None:
        return "na"
    b = (base or {}).get(row["id"])
    rule = row["rule"]
    delta_rules = {"max_delta", "min_delta", "max_delta_pct"} & rule.keys()
    if delta_rules and (b is None or ("max_delta_pct" in rule and b == 0)):
        return "na"
    breached = False
    if "max" in rule and h > rule["max"] + EPS:
        breached = True
    if "min" in rule and h < rule["min"] - EPS:
        breached = True
    if b is not None:
        d = h - b
        if "max_delta" in rule and d > rule["max_delta"] + EPS:
            breached = True
        if "min_delta" in rule and d < rule["min_delta"] - EPS:
            breached = True
        if "max_delta_pct" in rule and b > 0 and d / b * 100 > rule["max_delta_pct"] + EPS:
            breached = True
    if not breached:
        return "ok"
    return "warn"


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def fmt_seconds(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(round(s)), 60)
    return f"{m}m {sec:02d}s"


def fmt_value(unit: str, v: float | None) -> str:
    if v is None:
        return "n/a"
    if unit == "count":
        return f"{int(v):,}"
    if unit == "seconds":
        return fmt_seconds(v)
    return f"{v:.2f}%"


def fmt_delta(unit: str, d: float) -> str:
    if unit == "count":
        return f"{int(d):+,}"
    if unit == "seconds":
        return f"{d:+.1f}s"
    return f"{d:+.2f} pp"


def fmt_impact(unit: str, h: float | None, b: float | None) -> str:
    if h is None or b is None:
        return "n/a"
    d = h - b
    if abs(d) < EPS:
        return "0.00 pp" if unit == "percent" else "0 (0.0%)" if b > 0 else "0"
    text = fmt_delta(unit, d)
    if unit != "percent" and b > 0:
        text += f" ({d / b * 100:+.1f}%)"
    return text


def fmt_budget(unit: str, rule: dict) -> str:
    parts = []
    if "max" in rule:
        parts.append(f"≤ {fmt_value(unit, rule['max'])}")
    if "min" in rule:
        parts.append(f"≥ {fmt_value(unit, rule['min'])}")
    if "max_delta" in rule:
        parts.append("≤ main" if rule["max_delta"] == 0
                     else f"≤ {fmt_delta(unit, rule['max_delta'])} vs main")
    if "min_delta" in rule:
        parts.append("≥ main" if rule["min_delta"] == 0
                     else f"≥ {fmt_delta(unit, rule['min_delta'])} vs main")
    if "max_delta_pct" in rule:
        parts.append(f"≤ {rule['max_delta_pct']:+g}% vs main")
    return ", ".join(parts)


def _short(sha: str | None) -> str | None:
    return sha[:7] if sha and SHA_RE.match(sha) else None


def _footer(head_sha: str | None, base_sha: str | None, ci: str,
            approximate: bool = False, created_at: str | None = None) -> str:
    base = f"`{_short(base_sha)}`" if _short(base_sha) else "none yet"
    if approximate and _short(base_sha):
        age = "age unknown"
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            hours = max(0, (datetime.now(timezone.utc) - created).total_seconds() / 3600)
            age = f"{hours:.1f} hours old"
        except (AttributeError, ValueError, TypeError):
            pass
        base = f"approximate baseline ({base}, {age})"
    head = f"`{_short(head_sha)}`" if _short(head_sha) else "unknown"
    conclusion = ci if ci in CI_CONCLUSIONS else "unknown"
    return f"Baseline: {base} · PR result: {head} · Source CI: {conclusion}"


def _pr_sections(ctx: dict | None, want_ctx: bool) -> tuple[list[str], list[str]]:
    """(readiness lines for the top, footprint lines for below the table)."""
    if not want_ctx:
        return [], []
    top = [readiness_line(ctx), ""] if ctx else []
    return top, [render_footprint(ctx)]


def render_notice(reason: str, head_sha: str | None, ci: str,
                  ctx: dict | None = None, want_ctx: bool = False) -> str:
    top, bottom = _pr_sections(ctx, want_ctx)
    out = [MARKER, "## PR impact (advisory)", "",
           f"**{headline(ci, {}, [], [], ctx, notice='metrics not evaluated')}**", ""]
    out += top
    out += [f"Metrics not evaluated for this commit. {reason}", ""]
    out += bottom
    out += [_footer(head_sha, None, ci), ""]
    return "\n".join(out)


def _details(head: dict, base: dict | None) -> str:
    sections: list[str] = []
    hc = head["coverage"]
    bc = base["coverage"] if base else None
    if hc and bc:
        moves = []
        for path in hc.keys() & bc.keys():
            hp = hc[path][0] / hc[path][1] * 100
            bp = bc[path][0] / bc[path][1] * 100
            if abs(hp - bp) >= 0.5:
                moves.append((abs(hp - bp), path, bp, hp))
        moves.sort(reverse=True)
        new = hc.keys() - bc.keys()
        gone = bc.keys() - hc.keys()
        lines = ["**Coverage movers** (files with a change of 0.5 pp or more)", ""]
        if moves:
            lines += ["| File | Main | This PR | Change |", "| :-- | --: | --: | --: |"]
            for _, path, bp, hp in moves[:8]:
                lines.append(f"| `{path}` | {bp:.1f}% | {hp:.1f}% | {hp - bp:+.1f} pp |")
        else:
            lines.append("None.")
        if new:
            cov = sum(hc[p][0] for p in new)
            tot = sum(hc[p][1] for p in new)
            lines.append(f"\nNew files: {len(new)} ({cov}/{tot} statements covered).")
        if gone:
            lines.append(f"\nRemoved files: {len(gone)}.")
        sections.append("\n".join(lines))
    hb = head["benchmarks"]
    if hb and hb["tests"]:
        bb = base["benchmarks"]["tests"] if base and base["benchmarks"] else {}
        lines = ["**Benchmark timings**", "", "| Test | Main | This PR |", "| :-- | --: | --: |"]
        for name in sorted(hb["tests"]):
            prior = fmt_seconds(bb[name]) if name in bb else "n/a"
            lines.append(f"| `{name}` | {prior} | {fmt_seconds(hb['tests'][name])} |")
        sections.append("\n".join(lines))
    if not sections:
        return ""
    return "<details>\n<summary>Coverage movers and benchmark timings</summary>\n\n" \
        + "\n\n".join(sections) + "\n\n</details>\n"


def render_report(
    rows: list[dict],
    head: dict,
    base: dict | None,
    patterns: list[str],
    head_sha: str | None,
    base_sha: str | None,
    ci: str,
    approximate: bool = False,
    created_at: str | None = None,
    ctx: dict | None = None,
    want_ctx: bool = False,
) -> tuple[str, int]:
    hv = derive(head, patterns)
    bv = derive(base, patterns) if base else None
    states = [evaluate(r, hv, bv) for r in rows]
    warns, missing = states.count("warn"), states.count("na")
    verdict = f"Advisory only. {warns} budget(s) exceeded; {missing} metric(s) not evaluated."
    top, bottom = _pr_sections(ctx, want_ctx)
    out = [MARKER, "## PR impact (advisory)", "",
           f"**{headline(ci, hv, rows, states, ctx, has_base=bv is not None)}**", "",
           summary_line(hv, bv), ""]
    out += top
    out += [verdict, ""]
    out += ["| Area | Metric | Main baseline | This PR | Impact | Budget | |",
            "| :-- | :-- | --: | --: | --: | :-- | :-: |"]
    for row, state in zip(rows, states):
        h, b = hv.get(row["id"]), (bv or {}).get(row["id"])
        out.append(
            f"| {row['area']} | {row['label']} | {fmt_value(row['unit'], b)} "
            f"| {fmt_value(row['unit'], h)} | {fmt_impact(row['unit'], h, b)} "
            f"| {fmt_budget(row['unit'], row['rule'])} | {ICONS[state]}{' not evaluated' if state == 'na' else ''} |"
        )
    out.append("")
    if bv is None:
        out += ["No main baseline yet, so budgets that compare against main were skipped.", ""]
    if "na" in states:
        out += ["➖ not evaluated means a metric or usable baseline is missing.", ""]
    out += bottom
    out += [_footer(head_sha, base_sha if bv else None, ci, approximate, created_at), ""]
    details = _details(head, base)
    if details:
        out += [details]
    return "\n".join(out), 0


def cmd_report(args: argparse.Namespace) -> int:
    ci = args.ci_conclusion
    out_path = Path(args.out)
    try:
        rows = load_budget(Path(args.budget))
        patterns = load_patterns(Path(args.safety_paths))
    except MetricsError as e:
        print(f"::error::{e}")
        return 2

    want_ctx = bool(args.repo and args.pr)
    ctx = None
    if want_ctx:
        try:
            ctx = fetch_pr_context(args.repo, args.pr, patterns)
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError, AttributeError):
            print("::warning::PR file list or merge state lookup failed. Footprint omitted.")

    if not args.head or not Path(args.head).is_file():
        out_path.write_text(render_notice(
            "CI finished without producing metrics, usually because it stopped before the tests ran.",
            args.head_sha, ci, ctx, want_ctx))
        return 0
    try:
        head = load_metrics(Path(args.head))
    except MetricsError as e:
        print(f"::error::PR metrics failed validation: {e}")
        out_path.write_text(render_notice("The metrics artifact failed validation.", args.head_sha, ci,
                                         ctx, want_ctx))
        return 0

    base = None
    if args.base and Path(args.base).is_file():
        try:
            base = load_metrics(Path(args.base))
        except MetricsError as e:
            print(f"::warning::Ignoring main baseline: {e}")

    body, code = render_report(rows, head, base, patterns, args.head_sha, args.base_sha, ci,
                               args.approximate_baseline, args.base_created_at, ctx, want_ctx)
    out_path.write_text(body)
    print(body)
    return code


def _gh_json(*args: str) -> object:
    result = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def cmd_baseline(args: argparse.Namespace) -> int:
    """Prefer the merge-base main artifact, then a successful main fallback."""
    merge_base = None
    runs = []
    fields = "databaseId,headSha,createdAt,event,headBranch,conclusion"
    common = ["run", "list", "--repo", args.repo, "--workflow", "ci.yml",
              "--branch", "main", "--event", "push", "--status", "success",
              "--limit", "50", "--json", fields]
    try:
        comparison = _gh_json("api", f"repos/{args.repo}/compare/main...{args.head_sha}")
        merge_base = comparison["merge_base_commit"]["sha"]
        runs = _gh_json(*common, "--commit", merge_base)
    except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
        print("::notice::Merge-base baseline lookup unavailable.", file=sys.stderr)
    try:
        runs += _gh_json(*common)
    except (subprocess.SubprocessError, OSError, ValueError, TypeError):
        print("::notice::Latest main baseline lookup unavailable.", file=sys.stderr)
    destination = Path(args.out_dir)
    destination.mkdir(parents=True, exist_ok=True)
    artifact = destination / "metrics.json"
    seen = set()
    for run in runs:
        run_id = str(run["databaseId"])
        if run_id in seen:
            continue
        seen.add(run_id)
        # Do not reuse a partially downloaded or invalid previous candidate.
        artifact.unlink(missing_ok=True)
        try:
            subprocess.run(["gh", "run", "download", run_id, "--repo", args.repo,
                            "-n", "pr-impact-metrics", "-D", str(destination)],
                           check=True, capture_output=True, text=True)
            load_metrics(artifact)
        except (subprocess.SubprocessError, OSError, MetricsError):
            continue
        print(f"sha={run['headSha']}")
        print(f"created_at={run['createdAt']}")
        print(f"approximate={'false' if run['headSha'] == merge_base else 'true'}")
        return 0
    artifact.unlink(missing_ok=True)
    print("::notice::No usable main baseline artifact.", file=sys.stderr)
    print("sha=\ncreated_at=\napproximate=false")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect")
    c.add_argument("--junit")
    c.add_argument("--bench-junit")
    c.add_argument("--coverage")
    c.add_argument("--root", default=".")
    c.add_argument("--out", required=True)
    c.set_defaults(fn=cmd_collect)

    r = sub.add_parser("report")
    r.add_argument("--head")
    r.add_argument("--base")
    r.add_argument("--budget", required=True)
    r.add_argument("--safety-paths", required=True)
    r.add_argument("--head-sha")
    r.add_argument("--base-sha")
    r.add_argument("--base-created-at")
    r.add_argument("--approximate-baseline", action="store_true")
    r.add_argument("--ci-conclusion", default="unknown")
    r.add_argument("--repo", help="owner/name; with --pr adds footprint and merge readiness")
    r.add_argument("--pr", type=int)
    r.add_argument("--out", required=True)
    r.set_defaults(fn=cmd_report)

    b = sub.add_parser("baseline")
    b.add_argument("--repo", required=True)
    b.add_argument("--head-sha", required=True)
    b.add_argument("--out-dir", required=True)
    b.set_defaults(fn=cmd_baseline)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
