"""The operator dashboard orders PRs as stacks, derives next actions, and
keeps the operator's notes. Every GitHub call is faked, so this is offline."""

from __future__ import annotations

import importlib.util
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "pr_dashboard.py"
SAFETY = REPO_ROOT / ".github" / "safety-paths.txt"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr-dashboard.yml"
SWIFT = REPO_ROOT / ".github" / "workflows" / "swift-ci.yml"

spec = importlib.util.spec_from_file_location("pr_dashboard", SCRIPT)
dash = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dash)

REPO = "owner/repo"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


def pr(number, base="main", head=None, draft=False, ci="pass", guard="pass", labeled=False,
       required=False, mergeable=True, gate="", same_repo=True, title=None):
    return {
        "number": number, "title": title or f"PR {number}",
        "url": f"https://github.com/{REPO}/pull/{number}", "draft": draft,
        "base_ref": base, "default_branch": "main", "head_ref": head or f"feat/{number}",
        "head_sha": "a" * 40, "same_repo": same_repo, "mergeable": mergeable,
        "mergeable_state": "clean", "labeled": labeled, "gate": gate,
        "ci": ci, "guard": guard, "safety_required": required,
    }


# --- ordering ---------------------------------------------------------------

def test_order_stacks_puts_main_first_and_children_under_parents():
    prs = [
        pr(559, base="feat/557"),
        pr(557, base="feat/553"),
        pr(556, base="feat/554"),
        pr(555),
        pr(554),
        pr(553),
        pr(560, base="feat/gone"),     # base branch has no open PR
        pr(561, base="feat/553"),
    ]
    got = [(p["number"], depth, parent) for p, depth, parent in dash.order_stacks(prs)]
    assert got == [
        (553, 0, None), (557, 1, 553), (559, 2, 557), (561, 1, 553),
        (554, 0, None), (556, 1, 554),
        (555, 0, None),
        (560, 0, None),
    ]


def test_order_stacks_ignores_fork_branches_with_the_same_name_and_survives_cycles():
    prs = [pr(10, head="feat/x", same_repo=False), pr(11, base="feat/x")]
    got = [(p["number"], parent) for p, _, parent in dash.order_stacks(prs)]
    assert got == [(10, None), (11, None)]
    cycle = [pr(1, base="feat/2", head="feat/1"), pr(2, base="feat/1", head="feat/2")]
    got = sorted(p["number"] for p, _, _ in dash.order_stacks(cycle))
    assert got == [1, 2]


# --- next action --------------------------------------------------------------

@pytest.mark.parametrize("kwargs,parent,expected", [
    ({}, None, ("merge", "Merge")),
    ({"labeled": True, "required": True}, None, ("merge", "Merge")),
    ({"required": True}, None, ("label", "Needs safety-reviewed label")),
    ({"required": True, "guard": "fail"}, None, ("label", "Needs safety-reviewed label")),
    ({"ci": "fail", "required": True}, None, ("ci", "CI failing")),
    ({"mergeable": False, "ci": "fail"}, None, ("conflicts", "Conflicts: rebase")),
    ({"ci": "pending"}, None, ("wait", "Waiting on CI")),
    ({"ci": "missing"}, None, ("wait", "Waiting on CI")),
    ({"guard": "fail"}, None, ("guard", "Safety Guard failing")),
    ({"guard": "pending"}, None, ("wait", "Waiting on Safety Guard")),
    ({"mergeable": None}, None, ("wait", "Waiting on GitHub mergeability check")),
    ({"draft": True}, None, ("draft", "Draft")),
    ({"draft": True, "gate": "Touch ID UAT by operator"}, None,
     ("draft", "Draft: Touch ID UAT by operator")),
    ({"base": "feat/1", "ci": "missing", "guard": "missing"}, 1, ("stacked", "Waiting on base #1")),
    ({"base": "feat/1", "draft": True}, 1, ("stacked", "Waiting on base #1")),
    ({"base": "feat/1", "draft": True, "gate": "UAT"}, 1, ("draft", "Draft: UAT")),
    ({"base": "feat/gone"}, None, ("retarget", "Base has no open PR: retarget to main")),
])
def test_next_action(kwargs, parent, expected):
    assert dash.next_action(pr(5, **kwargs), parent) == expected


def test_operator_gate_line_is_found_and_escaped():
    body = "Intro\n\n**Operator gate:** press Touch ID @someone <script>\nOperator gate: second"
    assert dash.operator_gate(body) == "press Touch ID @someone <script>"
    assert dash.operator_gate("no gate here") == ""
    assert dash.operator_gate(None) == ""
    _, text = dash.next_action(pr(5, draft=True, gate=dash.operator_gate(body)), None)
    assert "@someone" not in text and "<script>" not in text
    assert text == "Draft: press Touch ID &#64;someone &lt;script&gt;"


# --- untrusted text -------------------------------------------------------------

def test_md_text_neutralises_markdown_html_and_mentions():
    hostile = "fix | [x](http://evil) `code` <img src=x> @team #12 ![i](y)\n**b** ~s~ _u_"
    out = dash.md_text(hostile, 200)
    assert "|" not in out.replace("\\|", "")
    assert "<" not in out and ">" not in out and "@" not in out and "`" not in out
    assert "\\[x\\](http://evil)" in out and "\\#12" in out and "\n" not in out
    assert dash.md_text("x" * 100, 10) == "x" * 9 + "…"


def test_trigger_text_is_restricted():
    assert dash.clean_trigger("pull_request_target (synchronize #552)") == \
        "pull_request_target (synchronize #552)"
    assert dash.clean_trigger("workflow_run (`evil` <b> @x)") == "workflow_run (evil b x)"
    assert dash.clean_trigger("") == "unknown"


# --- runs -------------------------------------------------------------------

RUNS = [
    {"id": 1, "path": ".github/workflows/safety-guard.yml", "event": "pull_request_target",
     "status": "completed", "conclusion": "failure", "created_at": "2026-10-07T08:58:47Z"},
    {"id": 3, "path": ".github/workflows/safety-guard.yml", "event": "pull_request_target",
     "status": "completed", "conclusion": "success", "created_at": "2026-10-07T18:41:22Z"},
    {"id": 2, "path": ".github/workflows/ci.yml", "event": "pull_request",
     "status": "in_progress", "conclusion": None, "created_at": "2026-10-07T08:58:49Z"},
    # A PR-controlled workflow pretending to be Safety Guard
    {"id": 9, "path": ".github/workflows/safety-guard.yml", "event": "pull_request",
     "status": "completed", "conclusion": "success", "created_at": "2026-10-07T23:00:00Z"},
]


def test_latest_run_uses_the_default_branch_workflow_and_newest_attempt():
    guard = dash.latest_run(RUNS, *dash.GUARD_RUN)
    assert guard["id"] == 3
    assert dash.run_state(guard) == "pass"
    assert dash.run_state(dash.latest_run(RUNS, *dash.CI_RUN)) == "pending"
    assert dash.run_state(dash.latest_run(RUNS[:1], *dash.GUARD_RUN)) == "fail"
    assert dash.run_state(dash.latest_run([], *dash.CI_RUN)) == "missing"
    assert dash.latest_run(None, *dash.CI_RUN) is None


# --- notes and marker -------------------------------------------------------------

def test_notes_are_preserved_verbatim():
    notes = (f"{dash.NOTES_START}\nMerge #553 first.\n| odd | table |\n<!-- inner -->\n"
             f"@me `code`\n{dash.NOTES_END}")
    body = f"{dash.MARKER}\nold table\n\n{notes}\n\nfooter"
    assert dash.extract_notes(body) == notes
    rendered = dash.render([], [], dash.extract_notes(body), "schedule", NOW)
    assert notes in rendered
    assert dash.extract_notes(rendered) == notes


@pytest.mark.parametrize("body", [None, "", "no markers", f"{dash.NOTES_START} only start",
                                  f"{dash.NOTES_END} before {dash.NOTES_START}"])
def test_missing_notes_get_an_empty_placeholder_block(body):
    notes = dash.extract_notes(body)
    assert notes.startswith(dash.NOTES_START) and notes.endswith(dash.NOTES_END)
    assert dash.NOTES_PLACEHOLDER in notes


def issue(number, login="github-actions[bot]", body=None, **extra):
    return {"number": number, "state": "open", "user": {"login": login},
            "body": f"{dash.MARKER}\nx" if body is None else body, **extra}


def test_find_issue_needs_marker_bot_author_and_not_a_pr():
    issues = [
        issue(5, login="attacker"),                       # marker, wrong author
        issue(6, body="no marker"),
        issue(7, pull_request={"url": "x"}),              # PRs come back from the issues API too
        issue(9),
        issue(8),
    ]
    assert dash.find_issue(issues)["number"] == 8
    assert dash.find_issue(issues[:3]) is None
    assert dash.find_issue([]) is None


# --- merged list and render ----------------------------------------------------------

def test_recently_merged_window_and_order():
    closed = [
        {"number": 1, "title": "old", "merged_at": (NOW - timedelta(days=8)).isoformat(), "html_url": "u1"},
        {"number": 2, "title": "closed unmerged", "merged_at": None, "html_url": "u2"},
        {"number": 3, "title": "a", "merged_at": "2026-10-06T10:00:00Z", "html_url": "u3"},
        {"number": 4, "title": "b", "merged_at": "2026-10-07T10:00:00Z", "html_url": "u4"},
        {"number": 5, "title": "bad", "merged_at": "not a date", "html_url": "u5"},
    ]
    assert [p["number"] for p in dash.recently_merged(closed, NOW)] == [4, 3]


def test_render_table_and_footer():
    prs = [pr(553, required=True), pr(557, base="feat/553", ci="missing", guard="missing"),
           pr(560, draft=True, gate="needs Touch ID"), pr(561)]
    rows = dash.order_stacks(prs)
    merged = [{"number": 552, "title": "VM | guards", "url": "https://x/552",
               "merged_at": NOW - timedelta(hours=3)}]
    body = dash.render(rows, merged, dash.extract_notes(None), "pull_request_target (labeled #553)",
                       NOW, "https://github.com/owner/repo/actions/runs/1")
    assert body.startswith(dash.MARKER)
    assert "4 open · 1 ready to merge · 1 need the safety label · 1 waiting on a base PR · 1 draft" in body
    lines = [line for line in body.splitlines() if line.startswith("| ")][2:]
    assert lines[0].startswith("| [#553](https://github.com/owner/repo/pull/553) PR 553 | Ready | ✅ | ✅ | no |")
    assert lines[0].endswith("| Needs safety-reviewed label |")
    assert lines[1].startswith("| └─ [#557]") and "| ➖ | ➖ | not required |" in lines[1]
    assert lines[1].endswith("| Waiting on base #553 |")
    assert lines[2].startswith("| [#560]") and lines[2].endswith("| Draft: needs Touch ID |")
    assert lines[3].endswith("| **Merge** |")
    assert "- [#552](https://x/552) VM \\| guards · 2026-10-08" in body
    assert body.rstrip().endswith(
        "<sub>Updated 2026-10-08 12:00 UTC from pull_request_target (labeled #553)"
        " · [run](https://github.com/owner/repo/actions/runs/1)</sub>")


def test_render_empty_state():
    body = dash.render([], [], dash.extract_notes(None), "schedule", NOW)
    assert "No open pull requests." in body and "None." in body


# --- update end to end (fake API) ----------------------------------------------------

def pull_json(number, base="main", head=None, mergeable=True, draft=False, labels=(), body=""):
    return {
        "number": number, "title": f"PR {number}", "draft": draft, "state": "open",
        "mergeable": mergeable, "mergeable_state": "clean", "body": body,
        "labels": [{"name": n} for n in labels],
        "base": {"ref": base, "repo": {"default_branch": "main"}},
        "head": {"ref": head or f"feat/{number}", "sha": f"{number:040d}"[-40:].replace("0", "a"),
                 "repo": {"full_name": REPO}},
    }


class FakeGitHub:
    def __init__(self, issues=(), label=True, mergeable_first=True):
        self.calls = []
        self.pulls = {
            553: pull_json(553, labels=("safety-reviewed",)),
            557: pull_json(557, base="feat/553", body="Operator gate: wait for UAT", draft=True),
        }
        self.issues = list(issues)
        self.label = label
        self.mergeable_first = mergeable_first
        self.seen = set()

    def __call__(self, path, method="GET", payload=None):
        self.calls.append((method, path, payload))
        if method != "GET":
            return {"number": 77}
        m = re.fullmatch(r"repos/owner/repo/pulls/(\d+)", path)
        if m:
            n = int(m.group(1))
            pull = dict(self.pulls[n])
            if not self.mergeable_first and n not in self.seen:
                self.seen.add(n)
                pull["mergeable"] = None
            return pull
        if path.startswith("repos/owner/repo/pulls?state=open"):
            return list(self.pulls.values())
        if re.match(r"repos/owner/repo/pulls/\d+/files", path):
            return [{"filename": "backend/execution/x.py", "status": "modified", "additions": 1, "deletions": 0}]
        if path.startswith("repos/owner/repo/actions/runs?head_sha="):
            return {"workflow_runs": [RUNS[1], dict(RUNS[2], status="completed", conclusion="success")]}
        if path.startswith("repos/owner/repo/pulls?state=closed"):
            return [{"number": 552, "title": "merged", "merged_at": "2026-10-08T01:00:00Z",
                     "html_url": "https://github.com/owner/repo/pull/552"}]
        if path.startswith("repos/owner/repo/issues?state=open&creator=github-actions%5Bbot%5D"):
            return self.issues
        if path == "repos/owner/repo/labels/dashboard" and self.label:
            return {"name": "dashboard"}
        raise dash.subprocess.CalledProcessError(1, ["gh", "api", path])

    def writes(self):
        return [(m, p, b) for m, p, b in self.calls if m != "GET"]


def patterns():
    return dash.impact.load_patterns(SAFETY)


def test_update_creates_the_issue_with_label_when_missing():
    gh = FakeGitHub()
    body = dash.update(REPO, "schedule", patterns(), api=gh, now=NOW, sleep=lambda s: None)
    (method, path, payload), = gh.writes()
    assert (method, path) == ("POST", "repos/owner/repo/issues")
    assert payload["title"] == "Operator dashboard" and payload["labels"] == ["dashboard"]
    assert payload["body"] == body and dash.MARKER in body and dash.NOTES_PLACEHOLDER in body
    assert "| [#553]" in body and "| **Merge** |" in body
    assert "└─ [#557]" in body and "Draft: wait for UAT" in body


def test_update_creates_without_label_when_the_label_does_not_exist():
    gh = FakeGitHub(label=False)
    dash.update(REPO, "schedule", patterns(), api=gh, now=NOW, sleep=lambda s: None)
    (_, _, payload), = gh.writes()
    assert "labels" not in payload


def test_update_patches_the_existing_issue_and_keeps_notes():
    notes = f"{dash.NOTES_START}\nKeep me. #553 after UAT.\n{dash.NOTES_END}"
    existing = issue(42, body=f"{dash.MARKER}\nstale table\n{notes}\n")
    gh = FakeGitHub(issues=[issue(41, login="someone"), existing])
    dash.update(REPO, "schedule", patterns(), api=gh, now=NOW, sleep=lambda s: None)
    (method, path, payload), = gh.writes()
    assert (method, path) == ("PATCH", "repos/owner/repo/issues/42")
    assert notes in payload["body"] and "stale table" not in payload["body"]


def test_update_rereads_unknown_mergeability_once_and_dry_run_writes_nothing():
    slept = []
    gh = FakeGitHub(mergeable_first=False)
    body = dash.update(REPO, "workflow_dispatch", patterns(), api=gh, now=NOW, dry_run=True,
                       sleep=slept.append)
    assert slept == [3]
    assert gh.writes() == []
    assert "computing" not in body
    assert all(m == "GET" for m, _, _ in gh.calls)


def test_update_rejects_a_bad_repo():
    with pytest.raises(ValueError):
        dash.update("owner/repo; rm", "x", [], api=FakeGitHub(), now=NOW)


# --- workflow wiring --------------------------------------------------------------

def _actions(text):
    return re.findall(r"uses: (.+)", text)


def test_dashboard_workflow_security():
    import yaml
    text = WORKFLOW.read_text()
    wf = yaml.safe_load(text)
    on = wf[True]  # PyYAML reads the `on` key as True
    assert set(on) == {"schedule", "workflow_dispatch", "pull_request_target", "workflow_run"}
    assert on["schedule"] == [{"cron": "*/30 * * * *"}]
    assert set(on["pull_request_target"]["types"]) == {
        "opened", "reopened", "synchronize", "closed", "labeled", "unlabeled",
        "ready_for_review", "converted_to_draft", "edited"}
    assert on["workflow_run"]["workflows"] == ["Growin Backend CI", "Safety Guard"]
    assert wf["permissions"] == {"contents": "read", "pull-requests": "read", "issues": "write",
                                 "checks": "read", "actions": "read"}
    assert wf["concurrency"] == {"group": "pr-dashboard", "cancel-in-progress": False}
    steps = wf["jobs"]["dashboard"]["steps"]
    checkout = steps[0]["with"]
    assert checkout["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkout["persist-credentials"] is False
    assert set(checkout["sparse-checkout"].split()) == {
        ".github/scripts/pr_dashboard.py", ".github/scripts/pr_impact.py", ".github/safety-paths.txt"}
    assert "head.sha" not in text and "head.ref" not in text and "head_branch" not in text
    # Event data only reaches the shell through env, never inline ${{ }} in run:
    for step in steps:
        assert "${{" not in step.get("run", "")
    for action in _actions(text):
        assert re.fullmatch(r"[\w/-]+@[0-9a-f]{40} # v[\w.]+", action)


def test_swift_ci_is_advisory_and_pinned():
    import yaml
    text = SWIFT.read_text()
    wf = yaml.safe_load(text)
    on = wf[True]
    assert set(on) == {"pull_request", "push", "workflow_dispatch"}
    for key in ("pull_request", "push"):
        assert on[key]["branches"] == ["main"]
        assert set(on[key]["paths"]) == {"Growin/**", "GrowinTests/**", "Growin.xcodeproj/**",
                                         ".github/workflows/swift-ci.yml"}
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["unit-tests"]
    assert job["runs-on"] == "macos-latest"
    assert "secrets." not in text and "TEST_RUNNER_GROWIN_SE_UAT=" not in text
    assert "-only-testing:GrowinTests" in text and "-skip-testing:GrowinUITests" in text
    assert "CODE_SIGNING_ALLOWED=NO" in text
    assert job["env"]["MIN_SDK"] == "26.5"
    for action in _actions(text):
        assert re.fullmatch(r"[\w/-]+@[0-9a-f]{40} # v[\w.]+", action)
    upload = next(s for s in job["steps"] if "upload-artifact" in s.get("uses", ""))
    assert upload["if"] == "failure()"


@pytest.mark.parametrize("older,status,log,code,degraded", [
    # SDK gap on an older SDK: warning and summary, not a red X
    ("true", 65, "A.swift:3:9: error: value of type 'some View' has no member 'glassEffect'", 0, True),
    ("true", 65, "B.swift:1:8: error: no such module 'FoundationModels'", 0, True),
    # The same error on a new enough SDK is a real failure
    ("false", 65, "A.swift:3:9: error: value of type 'some View' has no member 'glassEffect'", 1, False),
    # A failure that is not an SDK gap stays red even on an older SDK
    ("true", 65, "error: linker command failed with exit code 1", 1, False),
    ("false", 0, "", 0, False),
])
def test_swift_build_step_degrades_only_on_an_sdk_gap(tmp_path, older, status, log, code, degraded):
    import os
    import subprocess
    import yaml

    step = next(s for s in yaml.safe_load(SWIFT.read_text())["jobs"]["unit-tests"]["steps"]
                if s.get("id") == "build")
    fake = tmp_path / "bin" / "xcodebuild"
    fake.parent.mkdir()
    fake.write_text('#!/bin/bash\nprintf "%s\\n" "$FAKE_LOG"\nexit "$FAKE_STATUS"\n')
    fake.chmod(0o755)
    out, summary = tmp_path / "out", tmp_path / "summary"
    env = dict(os.environ, PATH=f"{fake.parent}:{os.environ['PATH']}", RUNNER_TEMP=str(tmp_path),
               GITHUB_OUTPUT=str(out), GITHUB_STEP_SUMMARY=str(summary), MIN_SDK="26.5",
               OLDER_SDK=older, SDK="26.2", XCODE="Xcode_26.2.app",
               FAKE_LOG=log, FAKE_STATUS=str(status))
    r = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True)
    assert r.returncode == code, r.stdout + r.stderr
    outputs = out.read_text()
    assert ("built=true" in outputs) is (status == 0)
    assert ("degraded=true" in outputs) is degraded
    if degraded:
        assert "::warning::Swift CI skipped" in r.stdout
        text = summary.read_text()
        assert "## Swift CI: degraded (advisory)" in text and "macOS `26.5` SDK or newer" in text
        assert log.split("error: ", 1)[1] in text
    elif code:
        assert "::error::Swift build failed" in r.stdout
