#!/usr/bin/env python3
"""Operator dashboard for Growin pull requests.

Keeps one issue titled "Operator dashboard" up to date with every open PR,
ordered as stacks, plus what to do next on each one.

  update   Reads PRs, workflow runs, and PR file lists from the GitHub API,
           renders the dashboard, and creates or edits the issue. With
           --dry-run it prints the body and writes nothing.

Runs from the default branch only (see pr-dashboard.yml). It never checks
out or executes PR code. PR titles, bodies, branch names, and file names are
attacker-controlled on a public repo, so all of them are escaped or
validated before they reach markdown.

Standard library only, so it runs under `python3 -I`. All GitHub calls go
through pr_impact.gh_api (one injectable function), so tests stay offline.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote


def _load_impact():
    """pr_impact.py sits next to this file. `python3 -I` drops the script
    directory from sys.path, so load it by path."""
    path = Path(__file__).resolve().with_name("pr_impact.py")
    spec = importlib.util.spec_from_file_location("pr_impact", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


impact = _load_impact()

MARKER = "<!-- growin-pr-dashboard -->"
NOTES_START = "<!-- operator-notes:start -->"
NOTES_END = "<!-- operator-notes:end -->"
NOTES_PLACEHOLDER = "_Operator notes go here. Anything between these two markers survives every update._"
TITLE = "Operator dashboard"
BOT = "github-actions[bot]"
LABEL = "dashboard"
CI_RUN = (".github/workflows/ci.yml", "pull_request")                    # job: Run SOTA Test Suite
GUARD_RUN = (".github/workflows/safety-guard.yml", "pull_request_target")  # job: Safety Guard
MAX_OPEN = 100
MAX_MERGED = 20
MAX_BODY = 60_000
MERGED_DAYS = 7
PASS = {"success", "neutral", "skipped"}
ICONS = {"pass": "✅", "fail": "❌", "pending": "⏳", "missing": "—"}
GATE_RE = re.compile(r"operator gate:\s*(.+)", re.IGNORECASE)
TRIGGER_RE = re.compile(r"[^A-Za-z0-9 #()._:/-]")


# --------------------------------------------------------------------------
# untrusted text
# --------------------------------------------------------------------------

def md_text(value: object, limit: int = 60) -> str:
    """Escape PR-controlled text for a markdown table cell on one line.

    Removes control characters, neutralises HTML, links, emphasis, code
    spans, table pipes, and @mentions, then truncates.
    """
    text = str(value) if value is not None else ""
    text = "".join(" " if ord(c) < 32 or ord(c) == 127 else c for c in text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    out = []
    for c in text:
        if c == "&":
            out.append("&amp;")
        elif c == "<":
            out.append("&lt;")
        elif c == ">":
            out.append("&gt;")
        elif c == "@":
            out.append("&#64;")
        elif c == "`":
            out.append("'")
        elif c in "|[]*_~\\!#":
            out.append("\\" + c)
        else:
            out.append(c)
    return "".join(out)


def clean_trigger(value: str) -> str:
    return TRIGGER_RE.sub("", value or "")[:80].strip() or "unknown"


def operator_gate(body: object) -> str:
    """Text after the first `Operator gate:` in the PR body, or ''."""
    if not isinstance(body, str):
        return ""
    for line in body.splitlines():
        m = GATE_RE.search(line)
        if m:
            return m.group(1).strip().strip("*_` ").strip()
    return ""


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

def latest_run(runs: object, path: str, event: str) -> dict | None:
    """Newest workflow run from `path` triggered by `event`.

    Filtering on the workflow file and event, not on the check name, means a
    PR cannot spoof Safety Guard with a same-named job in its own workflow:
    only the default-branch file runs on pull_request_target.
    """
    if not isinstance(runs, list):
        return None
    hits = [r for r in runs if isinstance(r, dict)
            and r.get("path") == path and r.get("event") == event]
    if not hits:
        return None
    return max(hits, key=lambda r: (str(r.get("created_at") or ""), r.get("id") or 0))


def run_state(run: dict | None) -> str:
    if run is None:
        return "missing"
    if run.get("status") != "completed":
        return "pending"
    return "pass" if run.get("conclusion") in PASS else "fail"


def normalise(pull: dict, repo: str) -> dict:
    head = pull.get("head") or {}
    head_repo = (head.get("repo") or {}).get("full_name")
    labels = [lb.get("name") for lb in pull.get("labels") or [] if isinstance(lb, dict)]
    mergeable = pull.get("mergeable")
    return {
        "number": int(pull["number"]),
        "title": pull.get("title") or "",
        "url": f"https://github.com/{repo}/pull/{int(pull['number'])}",
        "draft": pull.get("draft") is True,
        "base_ref": str(pull["base"]["ref"]),
        "default_branch": str(pull["base"]["repo"].get("default_branch") or "main"),
        "head_ref": str(head.get("ref") or ""),
        "head_sha": str(head.get("sha") or ""),
        "same_repo": head_repo == repo,
        "mergeable": mergeable if isinstance(mergeable, bool) else None,
        "mergeable_state": str(pull.get("mergeable_state") or "unknown"),
        "labeled": impact.REVIEW_LABEL in labels,
        "gate": operator_gate(pull.get("body")),
        "ci": "missing",
        "guard": "missing",
        "safety_required": None,
    }


def collect(repo: str, api, patterns: list[str], sleep=time.sleep) -> list[dict]:
    listed = impact.api_pages(api, f"repos/{repo}/pulls?state=open&sort=created&direction=asc", MAX_OPEN)
    pulls = {}
    for item in listed:
        pulls[item["number"]] = api(f"repos/{repo}/pulls/{int(item['number'])}")
    unknown = [n for n, p in pulls.items() if p.get("mergeable") is None]
    if unknown:
        sleep(3)  # GitHub computes mergeability on first read
        for n in unknown:
            pulls[n] = api(f"repos/{repo}/pulls/{int(n)}")
    prs = []
    for pull in pulls.values():
        pr = normalise(pull, repo)
        if re.fullmatch(r"[0-9a-f]{40}", pr["head_sha"]):
            try:
                runs = api(f"repos/{repo}/actions/runs?head_sha={pr['head_sha']}&per_page=100")
                runs = runs.get("workflow_runs") if isinstance(runs, dict) else None
                pr["ci"] = run_state(latest_run(runs, *CI_RUN))
                pr["guard"] = run_state(latest_run(runs, *GUARD_RUN))
            except (subprocess.SubprocessError, OSError, ValueError):
                pr["ci"] = pr["guard"] = "missing"
        try:
            files = impact.api_pages(api, f"repos/{repo}/pulls/{pr['number']}/files", impact.MAX_PR_FILES)
            pr["safety_required"] = bool(impact.footprint(files, patterns)["safety"])
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, TypeError):
            pr["safety_required"] = None
        prs.append(pr)
    return prs


def recently_merged(closed: list, now: datetime, days: int = MERGED_DAYS) -> list[dict]:
    cutoff = now - timedelta(days=days)
    out = []
    for p in closed:
        merged_at = p.get("merged_at") if isinstance(p, dict) else None
        if not merged_at:
            continue
        try:
            when = datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if when >= cutoff:
            out.append({"number": int(p["number"]), "title": p.get("title") or "",
                        "url": p.get("html_url") or "", "merged_at": when})
    out.sort(key=lambda p: p["merged_at"], reverse=True)
    return out[:MAX_MERGED]


# --------------------------------------------------------------------------
# ordering and next action
# --------------------------------------------------------------------------

def order_stacks(prs: list[dict]) -> list[tuple[dict, int, int | None]]:
    """(pr, depth, parent number). PRs on the default branch first, each
    followed by the PRs stacked on it, depth-first, numbers ascending."""
    by_head = {}
    for pr in sorted(prs, key=lambda p: p["number"]):
        if pr["same_repo"]:
            by_head.setdefault(pr["head_ref"], pr)
    parent_of: dict[int, int | None] = {}
    children: dict[int, list[dict]] = {}
    for pr in prs:
        parent = None
        if pr["base_ref"] != pr["default_branch"]:
            cand = by_head.get(pr["base_ref"])
            if cand is not None and cand["number"] != pr["number"]:
                parent = cand
        parent_of[pr["number"]] = parent["number"] if parent else None
        if parent:
            children.setdefault(parent["number"], []).append(pr)
    roots = sorted((p for p in prs if parent_of[p["number"]] is None),
                   key=lambda p: (p["base_ref"] != p["default_branch"], p["number"]))
    out: list[tuple[dict, int, int | None]] = []
    seen: set[int] = set()

    def walk(pr: dict, depth: int) -> None:
        if pr["number"] in seen:
            return
        seen.add(pr["number"])
        out.append((pr, depth, parent_of[pr["number"]]))
        for child in sorted(children.get(pr["number"], []), key=lambda p: p["number"]):
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)
    for pr in sorted(prs, key=lambda p: p["number"]):  # cycles: show them flat
        if pr["number"] not in seen:
            seen.add(pr["number"])
            out.append((pr, 0, parent_of[pr["number"]]))
    return out


def next_action(pr: dict, parent: int | None) -> tuple[str, str]:
    """(category, text). The first blocker wins."""
    stacked = pr["base_ref"] != pr["default_branch"]
    if pr["draft"] and pr["gate"]:
        return "draft", f"Draft: {md_text(pr['gate'], 70)}"
    if stacked:
        if parent:
            return "stacked", f"Waiting on base #{parent}"
        return "retarget", "Base has no open PR: retarget to main"
    if pr["draft"]:
        return "draft", "Draft"
    if pr["mergeable"] is False:
        return "conflicts", "Conflicts: rebase"
    if pr["ci"] == "fail":
        return "ci", "CI failing"
    if pr["safety_required"] and not pr["labeled"]:
        return "label", "Needs safety-reviewed label"
    if pr["ci"] in ("pending", "missing"):
        return "wait", "Waiting on CI"
    if pr["guard"] == "fail":
        return "guard", "Safety Guard failing"
    if pr["guard"] in ("pending", "missing"):
        return "wait", "Waiting on Safety Guard"
    if pr["mergeable"] is None:
        return "wait", "Waiting on GitHub mergeability check"
    return "merge", "Merge"


# --------------------------------------------------------------------------
# issue body
# --------------------------------------------------------------------------

def extract_notes(body: object) -> str:
    """The operator-notes block, markers included, or a fresh empty one."""
    if isinstance(body, str):
        start = body.find(NOTES_START)
        end = body.find(NOTES_END, start + len(NOTES_START)) if start != -1 else -1
        if start != -1 and end != -1:
            return body[start:end + len(NOTES_END)]
    return f"{NOTES_START}\n{NOTES_PLACEHOLDER}\n{NOTES_END}"


def find_issue(issues: list) -> dict | None:
    """The oldest open issue the bot created that carries the marker."""
    hits = [i for i in issues if isinstance(i, dict)
            and "pull_request" not in i
            and i.get("state", "open") == "open"
            and (i.get("user") or {}).get("login") == BOT
            and MARKER in (i.get("body") or "")]
    return min(hits, key=lambda i: i["number"]) if hits else None


def _label_cell(pr: dict) -> str:
    if pr["labeled"]:
        return "yes"
    if pr["safety_required"] is None:
        return "?"
    return "no" if pr["safety_required"] else "not required"


def _merge_cell(pr: dict) -> str:
    if pr["mergeable"] is False:
        return "❌ conflicts"
    if pr["mergeable"] is None:
        return "⏳ computing"
    return "✅ yes"


def render(rows: list[tuple[dict, int, int | None]], merged: list[dict], notes: str,
           trigger: str, now: datetime, run_url: str | None = None) -> str:
    counts: dict[str, int] = {}
    lines = []
    for pr, depth, parent in rows:
        key, action = next_action(pr, parent)
        counts[key] = counts.get(key, 0) + 1
        indent = "&nbsp;&nbsp;&nbsp;" * max(0, depth - 1) + ("└─ " if depth else "")
        cell = f"{indent}[#{pr['number']}]({pr['url']}) {md_text(pr['title'], 60)}"
        stacked = pr["base_ref"] != pr["default_branch"]
        ci = "—" if stacked and pr["ci"] == "missing" else ICONS[pr["ci"]]
        guard = "—" if stacked and pr["guard"] == "missing" else ICONS[pr["guard"]]
        bold = f"**{action}**" if key == "merge" else action
        lines.append(f"| {cell} | {'Draft' if pr['draft'] else 'Ready'} | {ci} | {guard} "
                     f"| {_label_cell(pr)} | {_merge_cell(pr)} | {bold} |")

    out = [MARKER, "## Open pull requests", ""]
    if rows:
        tally = [f"{len(rows)} open"]
        for key, word in (("merge", "ready to merge"), ("label", "need the safety label"),
                          ("ci", "CI failing"), ("conflicts", "with conflicts"),
                          ("stacked", "waiting on a base PR"), ("draft", "draft")):
            if counts.get(key):
                tally.append(f"{counts[key]} {word}")
        out += [" · ".join(tally), "",
                "| PR | State | CI | Safety Guard | Label | Mergeable | Next action |",
                "| :-- | :-- | :-: | :-: | :-- | :-- | :-- |", *lines, "",
                "CI is `Run SOTA Test Suite` and Safety Guard is the guard run on the current "
                "head. Stacked PRs show — because those checks only run against main.", ""]
    else:
        out += ["No open pull requests.", ""]

    out += [f"## Recently merged ({MERGED_DAYS} days)", ""]
    if merged:
        for p in merged:
            out.append(f"- [#{p['number']}]({p['url']}) {md_text(p['title'], 80)} "
                       f"· {p['merged_at']:%Y-%m-%d}")
    else:
        out.append("None.")
    out += ["", "## Operator notes", "", notes, "", "---"]
    footer = f"Updated {now:%Y-%m-%d %H:%M} UTC from {clean_trigger(trigger)}"
    if run_url:
        footer += f" · [run]({run_url})"
    out.append(f"<sub>{footer}</sub>")
    body = "\n".join(out) + "\n"
    if len(body) > MAX_BODY:  # keep the notes and footer; trim the table text
        body = body[:MAX_BODY - len(notes) - 200] + "\n\n_Truncated._\n\n" + notes + "\n"
    return body


# --------------------------------------------------------------------------
# command
# --------------------------------------------------------------------------

def label_exists(repo: str, api) -> bool:
    try:
        api(f"repos/{repo}/labels/{LABEL}")
        return True
    except (subprocess.SubprocessError, OSError, ValueError):
        return False


def update(repo: str, trigger: str, patterns: list[str], api=None,
           now: datetime | None = None, dry_run: bool = False, run_url: str | None = None,
           sleep=time.sleep) -> str:
    api = api or impact.gh_api
    if not impact.REPO_RE.match(repo):
        raise ValueError("repo is not owner/name")
    now = now or datetime.now(timezone.utc)
    rows = order_stacks(collect(repo, api, patterns, sleep))
    closed = api(f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=100")
    merged = recently_merged(closed if isinstance(closed, list) else [], now)
    # Read the issue last, right before writing, so a note the operator saved
    # while PRs were being read is not lost.
    issues = impact.api_pages(api, f"repos/{repo}/issues?state=open&creator={quote(BOT)}", 500)
    issue = find_issue(issues)
    body = render(rows, merged, extract_notes(issue.get("body") if issue else None),
                  trigger, now, run_url)
    if dry_run:
        return body
    if issue:
        api(f"repos/{repo}/issues/{int(issue['number'])}", "PATCH", {"body": body})
        print(f"Updated issue #{issue['number']}.")
    else:
        payload = {"title": TITLE, "body": body}
        if label_exists(repo, api):
            payload["labels"] = [LABEL]
        created = api(f"repos/{repo}/issues", "POST", payload)
        print(f"Created issue #{(created or {}).get('number')}.")
    return body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update")
    u.add_argument("--repo", required=True)
    u.add_argument("--trigger", default="manual")
    u.add_argument("--safety-paths", required=True)
    u.add_argument("--run-id", default="")
    u.add_argument("--out", help="also write the body here (for the step summary)")
    u.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        patterns = impact.load_patterns(Path(args.safety_paths))
    except (impact.MetricsError, OSError) as e:
        print(f"::error::{e}")
        return 2
    run_url = None
    if args.run_id.isdigit() and impact.REPO_RE.match(args.repo):
        run_url = f"https://github.com/{args.repo}/actions/runs/{args.run_id}"
    body = update(args.repo, args.trigger, patterns, dry_run=args.dry_run, run_url=run_url)
    if args.out:
        Path(args.out).write_text(body)
    if args.dry_run:
        print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
