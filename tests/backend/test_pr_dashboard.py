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
# Marker strings are part of the contract with the operator, so the tests
# spell them out instead of reading them from the script.
NOTES_MARKER = "<!-- operator-notes -->"
LEGACY_START = "<!-- operator-notes:start -->"
LEGACY_END = "<!-- operator-notes:end -->"
LEGACY_PLACEHOLDER = "_Operator notes go here. Anything between these two markers survives every update._"


def test_notes_marker_contract():
    assert (dash.NOTES_MARKER, dash.LEGACY_START, dash.LEGACY_END, dash.LEGACY_PLACEHOLDER) == \
        (NOTES_MARKER, LEGACY_START, LEGACY_END, LEGACY_PLACEHOLDER)


def pr(number, base="main", head=None, draft=False, ci="pass", guard="pass", labeled=False,
       required=False, mergeable=True, gate="", same_repo=True, title=None, state="clean"):
    return {
        "number": number, "title": title or f"PR {number}",
        "url": f"https://github.com/{REPO}/pull/{number}", "draft": draft,
        "base_ref": base, "default_branch": "main", "head_ref": head or f"feat/{number}",
        "head_sha": "a" * 40, "same_repo": same_repo, "mergeable": mergeable,
        "mergeable_state": state, "labeled": labeled, "gate": gate,
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
    # C2: mergeable=True is not enough. Only clean or has_hooks may say Merge.
    ({"state": "has_hooks"}, None, ("merge", "Merge")),
    ({"state": "blocked"}, None, ("blocked", "Blocked (blocked)")),
    ({"state": "behind"}, None, ("blocked", "Blocked (behind)")),
    # Round 2: unstable means an advisory check failed. Say so; still not Merge.
    ({"state": "unstable"}, None, ("advisory", "Advisory checks failing")),
    ({"state": "unknown"}, None, ("blocked", "Blocked (unknown)")),
    ({"state": "draft"}, None, ("blocked", "Blocked (draft)")),
    ({"state": "weird<b>"}, None, ("blocked", "Blocked (unknown)")),
    ({"state": "dirty"}, None, ("conflicts", "Conflicts: rebase")),
    # C3: anything but success is unresolved, never Merge.
    ({"ci": "unresolved"}, None, ("unresolved", "CI unresolved: re-run")),
    ({"guard": "unresolved"}, None, ("unresolved", "Safety Guard unresolved: re-run")),
    # C5: unknown safety classification is never Merge, even with the label.
    ({"required": None}, None, ("unknown", "Safety status unknown")),
    ({"required": None, "labeled": True}, None, ("unknown", "Safety status unknown")),
])
def test_next_action(kwargs, parent, expected):
    assert dash.next_action(pr(5, **kwargs), parent) == expected


@pytest.mark.parametrize("conclusion,state", [
    ("success", "pass"), ("failure", "fail"), ("timed_out", "fail"), ("startup_failure", "fail"),
    ("skipped", "unresolved"), ("neutral", "unresolved"), ("cancelled", "unresolved"),
    ("action_required", "unresolved"), ("stale", "unresolved"), (None, "unresolved"),
])
def test_only_success_is_a_pass(conclusion, state):
    assert dash.run_state({"status": "completed", "conclusion": conclusion}) == state
    if state != "pass":
        key, _ = dash.next_action(pr(5, ci=state), None)
        assert key != "merge"


def test_merge_cell_shows_a_blocking_state():
    assert dash._merge_cell(pr(5)) == "✅ yes"
    assert dash._merge_cell(pr(5, state="blocked")) == "⚠️ blocked"
    assert dash._merge_cell(pr(5, state="unstable")) == "⚠️ advisory checks failing"
    assert dash._merge_cell(pr(5, state="dirty")) == "❌ conflicts"
    assert dash._merge_cell(pr(5, mergeable=None, state="unknown")) == "⏳ computing"


def test_operator_gate_line_is_found_and_escaped():
    body = "Intro\n\n**Operator gate:** press Touch ID @someone <script>\nOperator gate: second"
    assert dash.operator_gate(body) == "press Touch ID @someone <script>"
    assert dash.operator_gate("no gate here") == ""
    assert dash.operator_gate("> **Operator gate:** quoted") == ""
    assert dash.operator_gate("  _Operator gate:_ UAT") == "UAT"
    assert dash.operator_gate(None) == ""
    _, text = dash.next_action(pr(5, draft=True, gate=dash.operator_gate(body)), None)
    assert "@someone" not in text and "<script>" not in text
    assert text == "Draft: press Touch ID &#64;someone &lt;script&gt;"


def test_operator_gate_only_matches_at_line_start():
    # O5: a mid-line mention is prose, not a gate.
    assert dash.operator_gate("This PR has no operator gate: nothing to do here") == ""
    assert dash.operator_gate("text\noperator gate: real one") == "real one"


# --- untrusted text -------------------------------------------------------------

def test_md_text_neutralises_markdown_html_and_mentions():
    hostile = "fix | [x](http://evil) `code` <img src=x> @team #12 ![i](y)\n**b** ~s~ _u_"
    out = dash.md_text(hostile, 200)
    assert "|" not in out.replace("\\|", "")
    assert "<" not in out and ">" not in out and "@" not in out and "`" not in out
    assert "\\[x\\](http\u200b://evil)" in out and "\\#12" in out and "\n" not in out
    assert dash.md_text("x" * 100, 10) == "x" * 9 + "…"


@pytest.mark.parametrize("title", [
    "see https://evil.example/x", "HTTP://EVIL.example", "go to www.evil.example now",
    "ftp://files.example",
])
def test_md_text_breaks_bare_autolinks(title):
    # O4: GitHub autolinks bare URLs and www. hosts in issue bodies.
    out = dash.md_text(title, 200)
    assert not re.search(r"(?i)(https?|ftp)://|www\.", out)
    assert "​" in out


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

NOTE_URL = "https://github.com/owner/repo/issues/42#issuecomment-900"


def note(body_text, login="sank29mane", assoc="OWNER", cid=900, updated="2026-10-08T10:00:00Z"):
    return {"id": cid, "body": body_text, "user": {"login": login}, "author_association": assoc,
            "updated_at": updated, "html_url": f"https://github.com/owner/repo/issues/42#issuecomment-{cid}"}


def test_notes_section_is_a_read_only_copy_with_a_link():
    text = "Merge #553 first.\n\n| odd | table |\n@me `code`"
    section = dash.notes_section(note(dash.notes_seed(text)), REPO)
    assert f"_Read-only copy of [the notes comment]({NOTE_URL}). Edit that comment" in section
    assert "> Merge #553 first.\n>\n> | odd | table |\n> @me `code`" in section
    assert NOTES_MARKER not in section and dash.NOTES_HEADER not in section


def test_notes_section_rejects_odd_links_and_strips_the_dashboard_marker():
    bad = dict(note(f"{NOTES_MARKER}\n{dash.MARKER} hi"), html_url="https://evil.example/x")
    section = dash.notes_section(bad, REPO)
    assert "evil" not in section and "the notes comment below" in section
    assert dash.MARKER not in section and "> hi" in section
    assert "created on the first update" in dash.notes_section(None, REPO)


def test_find_notes_comment_trusts_only_the_bot_and_repo_members():
    comments = [
        note(f"{NOTES_MARKER}\nbot seed", login="github-actions[bot]", assoc="NONE", cid=1,
             updated="2026-10-01T00:00:00Z"),
        note(f"{NOTES_MARKER}\nowner edit", cid=2, updated="2026-10-05T00:00:00Z"),
        note(f"{NOTES_MARKER}\nstranger", login="drive-by", assoc="NONE", cid=3,
             updated="2026-10-09T00:00:00Z"),
        note("no marker", cid=4, updated="2026-10-10T00:00:00Z"),
    ]
    assert dash.find_notes_comment(comments)["id"] == 2
    assert dash.find_notes_comment(comments[2:]) is None
    assert dash.find_notes_comment([]) is None


@pytest.mark.parametrize("body,expected", [
    (f"x\n{LEGACY_START}\nKeep me.\n{LEGACY_END}\n", "Keep me."),
    (f"{LEGACY_START}\n{LEGACY_PLACEHOLDER}\n{LEGACY_END}", None),
    (f"{LEGACY_START} only start", None),
    (f"{LEGACY_END} before {LEGACY_START}", None),
    (None, None), ("", None),
])
def test_legacy_notes_block_is_read_for_migration(body, expected):
    assert dash.legacy_notes(body) == expected


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
    body = dash.render(rows, merged, dash.notes_section(None, REPO), "pull_request_target (labeled #553)",
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
    body = dash.render([], [], dash.notes_section(None, REPO), "schedule", NOW)
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
    def __init__(self, issues=(), comments=(), label=True, mergeable_first=True):
        self.calls = []
        self.pulls = {
            553: pull_json(553, labels=("safety-reviewed",)),
            557: pull_json(557, base="feat/553", body="Operator gate: wait for UAT", draft=True),
        }
        self.issues = list(issues)
        self.comments = list(comments)
        self.label = label
        self.mergeable_first = mergeable_first
        self.seen = set()

    def __call__(self, path, method="GET", payload=None):
        self.calls.append((method, path, payload))
        if method == "POST" and path == "repos/owner/repo/issues":
            return {"number": 77}
        m = re.fullmatch(r"repos/owner/repo/issues/(\d+)/comments(\?.*)?", path)
        if method == "POST" and m:
            return {"id": 901, "body": payload["body"], "user": {"login": "github-actions[bot]"},
                    "author_association": "NONE", "updated_at": "2026-10-08T12:00:00Z",
                    "html_url": f"https://github.com/owner/repo/issues/{m.group(1)}#issuecomment-901"}
        if method != "GET":
            return {}
        if m:
            return self.comments
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


def run(gh, **kw):
    return dash.update(REPO, "schedule", patterns(), api=gh, now=NOW, sleep=lambda s: None, **kw)


def test_update_creates_the_issue_and_its_notes_comment():
    gh = FakeGitHub()
    body = run(gh)
    (m1, p1, issue_payload), (m2, p2, comment_payload), (m3, p3, patch) = gh.writes()
    assert (m1, p1) == ("POST", "repos/owner/repo/issues")
    assert issue_payload["title"] == "Operator dashboard" and issue_payload["labels"] == ["dashboard"]
    assert (m2, p2) == ("POST", "repos/owner/repo/issues/77/comments")
    assert comment_payload["body"] == dash.notes_seed(None)
    assert (m3, p3) == ("PATCH", "repos/owner/repo/issues/77") and patch["body"] == body
    assert "[the notes comment](https://github.com/owner/repo/issues/77#issuecomment-901)" in body
    assert f"> {dash.NOTES_PLACEHOLDER}" in body and dash.MARKER in body
    assert "| [#553]" in body and "| **Merge** |" in body
    assert "└─ [#557]" in body and "Draft: wait for UAT" in body


def test_update_creates_without_label_when_the_label_does_not_exist():
    gh = FakeGitHub(label=False)
    run(gh)
    assert "labels" not in gh.writes()[0][2]


def test_update_migrates_the_old_notes_block_into_a_comment_once():
    legacy = f"{LEGACY_START}\nKeep me. #553 after UAT.\n{LEGACY_END}"
    existing = issue(42, body=f"{dash.MARKER}\nstale table\n{legacy}\n")
    gh = FakeGitHub(issues=[issue(41, login="someone"), existing])
    run(gh)
    (m1, p1, seed), (m2, p2, patch) = gh.writes()
    assert (m1, p1) == ("POST", "repos/owner/repo/issues/42/comments")
    assert seed["body"] == dash.notes_seed("Keep me. #553 after UAT.")
    assert (m2, p2) == ("PATCH", "repos/owner/repo/issues/42")
    assert "> Keep me. #553 after UAT." in patch["body"]
    assert LEGACY_START not in patch["body"] and "stale table" not in patch["body"]


def test_update_copies_the_notes_comment_and_never_edits_it():
    # C6: the operator edits the comment; the bot only rewrites the issue body,
    # so a note saved mid-run cannot be overwritten.
    existing = issue(42, body=f"{dash.MARKER}\nold body\n")
    comments = [note(dash.notes_seed("v2: merge 557 after UAT"), cid=900),
                note(f"{NOTES_MARKER}\nspoof", login="drive-by", assoc="NONE", cid=950,
                     updated="2026-10-09T00:00:00Z")]
    gh = FakeGitHub(issues=[existing], comments=comments)
    run(gh)
    writes = gh.writes()
    assert [(m, p) for m, p, _ in writes] == [("PATCH", "repos/owner/repo/issues/42")]
    body = writes[0][2]["body"]
    assert "> v2: merge 557 after UAT" in body and "spoof" not in body
    assert f"[the notes comment]({NOTE_URL})" in body
    assert not any("/comments/" in p for _, p, _ in gh.calls)


def test_update_ignores_a_stranger_marker_and_creates_its_own_comment():
    existing = issue(42, body=f"{dash.MARKER}\nold body\n")
    gh = FakeGitHub(issues=[existing],
                    comments=[note(f"{NOTES_MARKER}\nspoof", login="x", assoc="CONTRIBUTOR")])
    body = run(gh)
    assert [(m, p) for m, p, _ in gh.writes()] == [
        ("POST", "repos/owner/repo/issues/42/comments"), ("PATCH", "repos/owner/repo/issues/42")]
    assert "spoof" not in body


def test_dry_run_previews_the_migration_without_writing():
    legacy = f"{LEGACY_START}\nKeep me.\n{LEGACY_END}"
    gh = FakeGitHub(issues=[issue(42, body=f"{dash.MARKER}\n{legacy}")])
    body = run(gh, dry_run=True)
    assert gh.writes() == [] and "> Keep me." in body


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
                                 "actions": "read"}
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


AVAIL = "A.swift:3:9: error: 'glassEffect(_:in:)' is only available in macOS 27.0 or newer"
AVAIL2 = "/w/C.swift:9:2: error: 'Bar' is only available in macOS 26.4 or newer"
UNAVAILABLE = "C.swift:9:2: error: 'Foo' is unavailable in macOS"
SDK_MISSING = 'xcodebuild: error: SDK "macosx27.0" cannot be located.'
# A real syntax error in a file whose path contains SDK-gap words
TRICK_PATH = "/w/is unavailable in macOS/is only available in macOS 27.0 or newer.swift:4:1: error: expected '}' in struct"
SCOPE = "B.swift:1:8: error: cannot find 'Foo' in scope"
SYNTAX = "D.swift:4:1: error: expected '}' in struct"
# Follow-up (#561 final check): colons are legal in macOS paths, so the LAST
# ":N:N: error: " is the real diagnostic, not the first one.
COLON_PATH = "/w/x:1:2: error: a requires a newer version of Xcode.swift:4:1: error: expected '}' in struct"
# A version phrase followed by more text is not the whole message.
NEWER_TAIL = "E.swift:1:1: error: 'Foo' requires a newer version of Xcode; also cannot find 'Bar' in scope"
NEWER_TOOL_TAIL = "xcodebuild: error: 'Foo' requires a newer version of Xcode; also cannot find 'Bar' in scope"
FORMAT_TAIL = "xcodebuild: error: The project 'G' cannot be opened because it is in a future Xcode project file format; also cannot find 'Bar' in scope"
NEWER = "E.swift:1:1: error: 'Foo' requires a newer version of Xcode"
# Real xcodebuild says "Unable to read project 'X.xcodeproj'." for a future
# project format, so the old format phrase never fired and is gone (fail closed).
FORMAT = "xcodebuild: error: The project 'G' cannot be opened because it is in a future Xcode project file format."
# Fix round 1: a line carrying two diagnostic delimiters is never an SDK gap,
# because the greedy extraction would otherwise keep only the quoted tail.
QUOTED = '@available(*, unavailable, message: "blocked :1:2: error: The project G cannot be opened because it is in a future Xcode project file format.")'
QUOTED_BARE = 'E.swift:1:1: error: @available(*, unavailable, message: "blocked :1:2: error: The project G cannot be opened because it is in a future Xcode project file format.'
QUOTED_NEWER = 'E.swift:1:1: error: @available(*, unavailable, message: "blocked :1:2: error: a requires a newer version of Xcode'
ANCHOR = '/w/x:1:2: error: SDK "y.swift:4:1: error: expected \'}\' in struct" cannot be located.'


@pytest.mark.parametrize("older,status,log,code,degraded", [
    # Pure availability errors on an older SDK: warning and summary, not a red X
    ("true", 65, AVAIL, 0, True),
    ("true", 65, f"{AVAIL}\nnote: in expansion\n{AVAIL2}\n{AVAIL}", 0, True),
    ("true", 65, SDK_MISSING, 0, True),
    # Round 2: only the message after "error: " is classified, so a path
    # cannot disguise a syntax error, and @available(macOS, unavailable)
    # (permanent, no SDK fixes it) stays red.
    ("true", 65, TRICK_PATH, 1, False),
    ("true", 65, f"{AVAIL}\n{TRICK_PATH}", 1, False),
    ("true", 65, UNAVAILABLE, 1, False),
    ("true", 65, f"{AVAIL}\n{UNAVAILABLE}", 1, False),
    # Follow-up: version phrases degrade only as the complete message
    ("true", 65, NEWER, 0, True),
    ("true", 65, FORMAT, 1, False),
    ("true", 65, QUOTED, 1, False),
    ("true", 65, QUOTED_BARE, 1, False),
    ("true", 65, QUOTED_NEWER, 1, False),
    ("true", 65, ANCHOR, 1, False),
    ("true", 65, f"{AVAIL}\n{ANCHOR}", 1, False),
    ("true", 65, COLON_PATH, 1, False),
    ("true", 65, f"{AVAIL}\n{COLON_PATH}", 1, False),
    ("true", 65, NEWER_TAIL, 1, False),
    ("true", 65, NEWER_TOOL_TAIL, 1, False),
    ("true", 65, FORMAT_TAIL, 1, False),
    # C4: one real error mixed in keeps it red
    ("true", 65, f"{AVAIL}\n{SCOPE}", 1, False),
    ("true", 65, f"{AVAIL}\n{SYNTAX}", 1, False),
    # Errors that look like an SDK gap but are not availability errors stay red
    ("true", 65, SCOPE, 1, False),
    ("true", 65, "B.swift:1:8: error: no such module 'FoundationModels'", 1, False),
    ("true", 65, "A.swift:3:9: error: value of type 'some View' has no member 'glassEffect'", 1, False),
    # Exit 65 with no error line at all is red too
    ("true", 65, "** BUILD FAILED **", 1, False),
    # Availability errors on a new enough SDK are real failures
    ("false", 65, AVAIL, 1, False),
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
        for line in log.splitlines():
            if "error: " in line:
                assert line.split("error: ", 1)[1] in text
    elif code:
        assert "::error::Swift build failed" in r.stdout
