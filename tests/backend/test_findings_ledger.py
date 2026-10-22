"""The findings ledger check derives ids for GitHub review findings, reads the
latest `## Review round N` ledger, and flags any finding without a valid
disposition. Every GitHub call is faked, so this is offline."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "findings_ledger.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "findings-ledger.yml"

spec = importlib.util.spec_from_file_location("findings_ledger", SCRIPT)
fl = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fl  # dataclasses look the module up by name
spec.loader.exec_module(fl)

# The sticky-comment marker is a contract with existing comments on PRs.
MARKER = "<!-- growin-findings-ledger -->"


def test_marker_contract():
    assert fl.MARKER == MARKER


def review(rid, body="Found a bug.", state="COMMENTED", assoc="OWNER", login="sank29mane",
           at="2026-10-21T14:08:21Z", commit="f" * 40):
    return {"id": rid, "body": body, "state": state, "author_association": assoc,
            "user": {"login": login}, "submitted_at": at, "commit_id": commit}


def comment(cid, review_id=1, body="**Bug.** todo keeps plan order", assoc="OWNER", login="sank29mane",
            at="2026-10-21T14:08:21Z", path="backend/pilot_data/backfill.py", reply_to=None,
            commit="f816449178fc54b51147780beb7eb086d2806899"):
    return {"id": cid, "pull_request_review_id": review_id, "body": body, "author_association": assoc,
            "user": {"login": login}, "created_at": at, "path": path, "in_reply_to_id": reply_to,
            "original_commit_id": commit, "commit_id": "1" * 40}


def ids(findings):
    return [f.id for f in findings]


def statuses(report):
    return {r.id: r.status for r in report.rows}


# --- collecting findings ------------------------------------------------------

def test_each_review_body_and_top_level_inline_comment_is_one_finding():
    found = fl.collect_github_findings([review(1)], [comment(11), comment(12), comment(13, reply_to=11)])
    assert ids(found) == ["gh-c11", "gh-c12", "gh-r1"]
    c11 = found[0]
    assert (c11.commit, c11.path, c11.author) == ("f816449", "backend/pilot_data/backfill.py", "sank29mane")


def test_empty_review_bodies_are_not_findings_but_their_inline_comments_are():
    found = fl.collect_github_findings([review(1, body="  \n")], [comment(11)])
    assert ids(found) == ["gh-c11"]


def test_dismissed_and_pending_reviews_drop_their_inline_comments_too():
    reviews = [review(1, state="DISMISSED"), review(2, state="PENDING"), review(3)]
    comments = [comment(11, review_id=1), comment(12, review_id=2), comment(13, review_id=3)]
    assert ids(fl.collect_github_findings(reviews, comments)) == ["gh-c13", "gh-r3"]


def test_only_trusted_reviewers_count_on_a_public_repo():
    reviews = [review(1, assoc="NONE", login="drive-by"), review(2, assoc="CONTRIBUTOR", login="someone"),
               review(3, assoc="NONE", login="coderabbitai[bot]"), review(4, assoc="MEMBER")]
    comments = [comment(11, assoc="NONE", login="drive-by"), comment(12, assoc="COLLABORATOR"),
                comment(13, assoc="NONE", login="github-actions[bot]")]
    assert ids(fl.collect_github_findings(reviews, comments)) == ["gh-c12", "gh-r3", "gh-r4"]


def test_the_grok_bot_counts_even_though_it_posts_as_the_pr_author():
    # On #585 the reviewer and the PR author are the same login. Identity
    # must not be the filter.
    found = fl.collect_github_findings([review(5601040128, login="sank29mane", assoc="OWNER")], [])
    assert ids(found) == ["gh-r5601040128"]


def test_as_of_drops_later_reviews_and_comments():
    reviews = [review(1, at="2026-10-21T14:08:21Z"), review(2, at="2026-10-21T16:12:36Z")]
    comments = [comment(11, at="2026-10-21T14:08:21Z"), comment(12, review_id=2, at="2026-10-21T16:12:36Z")]
    as_of = fl._parse_time("2026-10-21T15:50:45Z")
    assert ids(fl.collect_github_findings(reviews, comments, as_of=as_of)) == ["gh-c11", "gh-r1"]


def test_unsafe_paths_and_logins_never_reach_the_report():
    found = fl.collect_github_findings([], [comment(11, path="a|b<script>.py", login="x y")])
    assert (found[0].path, found[0].author) == ("", "unknown")


def test_excerpt_strips_markup_and_breaks_mentions():
    text = "## Heading\n\n**Bug** [click](http://x) <img src=x> `code` | @sank29mane #12"
    out = fl.excerpt(text)
    for bad in ("[", "]", "(", ")", "<", ">", "`", "|", "*", "#", "@s"):
        assert bad not in out
    assert out.startswith("Bug click")
    assert "@​sank29mane" in out
    assert len(fl.excerpt("x" * 500)) == 90


def test_excerpt_skips_html_comments_like_coderabbits_header():
    text = "<!-- coderabbit-ai-generated:v1 -->\n<!-- multi\nline -->\n_Potential issue_ | Major\nrest"
    assert fl.excerpt(text) == "_Potential issue_ Major"


# --- parsing the body ---------------------------------------------------------

BODY_586_SHAPE = """## Summary
text
## Review round 1
- `codex-r1-p1` fixed `tests/backend/test_a.py::test_one`
## Review round 2
- `codex-r1-p1` fixed `tests/backend/test_a.py::test_one`
## Summary by CodeRabbit
- `opus-r9-x` residual: outside any round section, ignored
## Review round 2
- `codex-r2-p2` residual: second section with the same number
## Review round 3
### Residuals (round 3)
- `opus-r3-a` residual: an h3 stays inside the round
```
## Review round 99
```
- `grok-r3-1` declined: after a fenced block that holds a fake heading
## Deferred
- `grok-r3-2` deferred: outside the round
"""


def test_latest_round_is_the_highest_number_and_h3_and_fences_stay_inside_it():
    rounds = fl.split_rounds(BODY_586_SHAPE)
    assert [n for n, _ in rounds] == [1, 2, 2, 3]
    n, text = fl.latest_round(rounds)
    assert n == 3
    entries, _ = fl.parse_ledger(text)
    assert set(entries) == {"opus-r3-a", "grok-r3-1"}


@pytest.mark.parametrize("fenced, expected", [
    # Review probe: a ~~~ line inside a ``` block is content, not a close.
    ("```\n~~~\n```", [1, 2]),
    ("~~~\n```\n~~~", [1, 2]),
    # A closing run must be at least as long as the opening one.
    ("````\n```\n## Review round 7\n````", [1, 2]),
    # A closing line carries no info string.
    ("```\n```python\n## Review round 7\n```", [1, 2]),
    # A backtick info string cannot hold a backtick, so this opens nothing.
    ("``` a`b", [1, 2]),
    # An unclosed fence hides everything after it.
    ("```", [1]),
])
def test_fences_close_only_on_the_same_character_and_length(fenced, expected):
    body = f"## Review round 1\n{fenced}\n## Review round 2\n- `gh-c1` noted\n"
    assert [n for n, _ in fl.split_rounds(body)] == expected


def test_a_repeated_round_number_uses_the_last_section():
    body = "## Review round 2\n- `a-r2-1` residual: first\n## Review round 2\n- `a-r2-2` residual: second\n"
    n, text = fl.latest_round(fl.split_rounds(body))
    assert (n, set(fl.parse_ledger(text)[0])) == (2, {"a-r2-2"})


def test_list_and_table_lines_parse_with_or_without_backticks():
    text = """
- `gh-c1` fixed `tests/backend/test_a.py::test_one`
* gh-c2: residual: fails closed
1. Codex-R4-P1A Declined: unreachable at this window size
| gh-r3 | noted | summary only |
| `gh-c4` | dup | `gh-c1` |
- not a ledger line gh-c5 fixed
"""
    entries, conflicts = fl.parse_ledger(text)
    assert {k: v.disposition for k, v in entries.items()} == {
        "gh-c1": "fixed", "gh-c2": "residual", "codex-r4-p1a": "declined", "gh-r3": "noted", "gh-c4": "dup"}
    assert entries["gh-c2"].detail == "fails closed"
    assert conflicts == []


def test_two_dispositions_for_one_id_are_reported_and_the_first_wins():
    entries, conflicts = fl.parse_ledger("- `gh-c1` fixed `t.py::t`\n- `gh-c1` declined: no\n")
    assert entries["gh-c1"].disposition == "fixed"
    assert len(conflicts) == 1 and "gh-c1" in conflicts[0]


# --- evaluation ---------------------------------------------------------------

def finding(fid):
    return fl.Finding(id=fid, author="sank29mane", created_at="2026-10-21T14:08:21Z")


def judge(body, *fids, tests=None):
    return fl.evaluate([finding(f) for f in fids], body, tests)


def test_motivating_case_a_github_finding_never_listed_across_four_rounds_is_missing():
    # #585: the Grok review's liveness bug (gh-c4337853615) never appeared in
    # rounds 1-4, which only carried T3 reviewer findings.
    body = "\n".join(
        f"## Review round {n}\n- `codex-r{n}-p1` fixed `tests/backend/test_a.py::test_{n}`\n" for n in (1, 2, 3))
    body += "## Review round 4\n" + "".join(
        f"- `codex-r{n}-p1` fixed `tests/backend/test_a.py::test_{n}`\n" for n in (1, 2, 3))
    report = judge(body, "gh-c4337853615")
    assert statuses(report) == {"gh-c4337853615": "missing", "codex-r1-p1": "ok",
                                "codex-r2-p1": "ok", "codex-r3-p1": "ok"}
    assert [r.id for r in report.problems] == ["gh-c4337853615"]


def test_no_round_section_means_every_finding_is_missing():
    report = judge("## Summary\nNo rounds yet.\n", "gh-c1")
    assert statuses(report) == {"gh-c1": "missing"}
    assert "no `## Review round N`" in report.rows[0].why


def test_no_findings_is_clean():
    assert judge("## Summary\n").problems == []


def test_a_finding_listed_earlier_but_dropped_from_the_latest_round_is_missing():
    body = "## Review round 1\n- `opus-r1-m10` residual: tracked\n## Review round 2\n- `gh-c1` noted\n"
    report = judge(body)
    row = {r.id: r for r in report.rows}["opus-r1-m10"]
    assert row.status == "missing" and "dropped from round 2" in row.why


@pytest.mark.parametrize("line, why", [
    ("- `gh-c1` fixed with a careful change", "test node id"),
    ("- `gh-c1` residual", "needs a reason"),
    ("- `gh-c1` declined:", "needs a reason"),
    ("- `gh-c1` deferred: -", "needs a reason"),
    ("- `gh-c1` noted", "only for review summaries"),
    ("- `gh-c1` dup", "needs the id"),
    ("- `gh-c1` dup `gh-c999`", "not a known finding"),
])
def test_invalid_dispositions(line, why):
    report = judge(f"## Review round 1\n{line}\n", "gh-c1")
    row = report.rows[0]
    assert row.status == "invalid" and why in row.why


def test_valid_dispositions_without_a_test_index():
    body = """## Review round 2
- `gh-c1` fixed `tests/backend/test_a.py::test_one[case-1]`
- `gh-c2` residual: fails closed
- `gh-c3` declined: unreachable
- `gh-c4` deferred: phase 62
- `gh-r5` noted
- `gh-c6` dup `gh-c1`
- `codex-r2-p1` fixed `tests/backend/test_a.py::TestX::test_two`
"""
    report = judge(body, "gh-c1", "gh-c2", "gh-c3", "gh-c4", "gh-r5", "gh-c6")
    assert report.problems == []
    assert len(report.rows) == 7


def test_a_dup_inherits_its_targets_problem():
    body = "## Review round 1\n- `gh-c2` dup `gh-c1`\n- `gh-c3` dup `gh-c2`\n"
    report = judge(body, "gh-c1", "gh-c2", "gh-c3")
    assert statuses(report) == {"gh-c1": "missing", "gh-c2": "missing", "gh-c3": "missing"}
    assert "dup of `gh-c2`" in {r.id: r for r in report.rows}["gh-c3"].why


def test_a_dup_loop_is_invalid():
    body = "## Review round 1\n- `gh-c1` dup `gh-c2`\n- `gh-c2` dup `gh-c1`\n"
    assert set(statuses(judge(body, "gh-c1", "gh-c2")).values()) == {"invalid"}


def test_an_unknown_github_id_in_the_ledger_is_a_warning():
    report = judge("## Review round 1\n- `gh-c42` residual: typo\n")
    assert any("gh-c42" in w for w in report.warnings)


# --- test lookup --------------------------------------------------------------

TEST_SOURCE = '''
import pytest

pytestmark = [pytest.mark.finding("mod-r1-1")]

@pytest.mark.finding("gh-c1")
def test_marked():
    pass

def test_plain():
    pass

@pytest.mark.finding("gh-c3")
class TestGroup:
    @pytest.mark.finding("gh-c4")
    async def test_inner(self):
        pass

class TestOuter:
    pytestmark = pytest.mark.finding("gh-c6")

    class TestInner:
        @pytest.mark.finding("gh-c5")
        def test_deep(self):
            def test_local():
                pass
'''

SWIFT_SOURCE = """
import XCTest
import Testing

final class SignerSourceScanTests: XCTestCase {
    func testAliasFlagged() throws {
        XCTAssertTrue(true)
    }
}

@Test("generic router alias") func genericRouterAliasIsCaught() {
}
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(TEST_SOURCE)
    (tmp_path / "outside.py").write_text("def test_secret():\n    pass\n")
    (tmp_path / "GrowinTests").mkdir()
    (tmp_path / "GrowinTests" / "SignerSourceScanTests.swift").write_text(SWIFT_SOURCE)
    (tmp_path / "outside.swift").write_text("func testSecret() {}\n")
    (tmp_path / "GrowinTests" / "Notes.m").write_text("- (void)testObjc {}\n")
    return tmp_path


def test_the_index_finds_functions_methods_and_markers_without_importing(repo):
    idx = fl.TestIndex(repo)
    assert idx.lookup("tests/test_x.py", "test_marked") == {"mod-r1-1", "gh-c1"}
    assert idx.lookup("tests/test_x.py", "test_plain") == {"mod-r1-1"}
    assert idx.lookup("tests/test_x.py", "TestGroup::test_inner") == {"mod-r1-1", "gh-c3", "gh-c4"}
    assert idx.lookup("tests/test_x.py", "test_gone") is None
    assert idx.lookup("tests/nope.py", "test_marked") is None


def test_nested_test_classes_are_indexed_by_their_full_node_path(repo):
    idx = fl.TestIndex(repo)
    assert idx.lookup("tests/test_x.py", "TestOuter::TestInner::test_deep") == {"mod-r1-1", "gh-c5", "gh-c6"}
    assert idx.lookup("tests/test_x.py", "TestInner::test_deep") is None
    # A function defined inside a test is not a test.
    assert idx.lookup("tests/test_x.py", "TestOuter::TestInner::test_deep::test_local") is None


def test_swift_tests_are_found_by_a_text_scan_and_carry_no_markers(repo):
    idx = fl.TestIndex(repo)
    path = "GrowinTests/SignerSourceScanTests.swift"
    assert idx.lookup(path, "testAliasFlagged") == set()
    assert idx.lookup(path, "SignerSourceScanTests::testAliasFlagged") == set()
    assert idx.lookup(path, "genericRouterAliasIsCaught") == set()
    assert idx.lookup(path, "testGone") is None
    assert fl.TestIndex(repo / "GrowinTests").lookup("../outside.swift", "testSecret") is None


def test_a_swift_fix_can_be_marked_fixed(repo):
    body = """## Review round 1
- `gh-c1` fixed `GrowinTests/SignerSourceScanTests.swift::testAliasFlagged`
- `gh-c2` fixed `GrowinTests/SignerSourceScanTests.swift::testGone`
- `gh-c3` fixed `GrowinTests/Notes.m::testObjc`
"""
    report = judge(body, "gh-c1", "gh-c2", "gh-c3", tests=fl.TestIndex(repo))
    assert statuses(report) == {"gh-c1": "ok", "gh-c2": "test-missing", "gh-c3": "invalid"}
    assert {r.id: r for r in report.rows}["gh-c1"].notes == []


SWIFT_TRAPS = r'''
import XCTest
import Testing

final class FooTests: XCTestCase {
    // func testRemoved() { }
    /* func testAlsoGone() {} */
    /* outer /* inner */ func testNestedGone() {} */
    let s = "func testInString("
    let r = #"func testInRaw() " still raw"#
    let q = #"a lone " quote"#; func testAfterRaw() {}
    let m = """
        func testInMultiline() {}
        """
    let url = "http://x"; func testAfterURL() {}
    /* note */ func testAfterBlock() throws {}
    private func helper(x: Int) {}
    private static func testService() -> String { "svc" }
    func testTakesArgs(x: Int) {}
    @MainActor func testOnMain() async throws {}
}

@Suite struct Cases {
    @Test(arguments: ["a)", "b"])
    func parameterised(x: String) {}
    @Test("display (name)") @MainActor
    static func attributed() async {}
    @Test(.timeLimit(.minutes(1)), arguments: [1])
    func timed(x: Int) {}
    func notATest() {}
}
'''


def test_swift_comments_strings_and_helpers_are_not_tests(tmp_path):
    (tmp_path / "GrowinTests").mkdir()
    (tmp_path / "GrowinTests" / "FooTests.swift").write_text(SWIFT_TRAPS)
    idx = fl.TestIndex(tmp_path)
    path = "GrowinTests/FooTests.swift"
    # The review's probe: commented-out, quoted and helper functions are not tests.
    for name in ("testRemoved", "testAlsoGone", "testNestedGone", "testInString", "testInRaw",
                 "testInMultiline", "helper", "testService", "testTakesArgs", "notATest"):
        assert idx.lookup(path, name) is None, name
    # A string or comment does not hide the code after it.
    for name in ("testAfterURL", "testAfterBlock", "testAfterRaw", "testOnMain",
                 "parameterised", "attributed", "timed"):
        assert idx.lookup(path, name) == set(), name


def test_a_commented_out_swift_test_is_test_missing(repo):
    path = repo / "GrowinTests" / "SignerSourceScanTests.swift"
    path.write_text(path.read_text().replace("    func testAliasFlagged()", "    // func testAliasFlagged()"))
    body = """## Review round 1
- `gh-c1` fixed `GrowinTests/SignerSourceScanTests.swift::testAliasFlagged`
"""
    assert statuses(judge(body, "gh-c1", tests=fl.TestIndex(repo))) == {"gh-c1": "test-missing"}


def test_the_index_refuses_paths_outside_the_root(repo):
    idx = fl.TestIndex(repo / "tests")
    assert idx.lookup("../outside.py", "test_secret") is None
    assert idx.lookup(str(repo / "outside.py"), "test_secret") is None


def test_fixed_needs_the_named_test_to_exist_at_this_head(repo):
    body = """## Review round 1
- `gh-c1` fixed `tests/test_x.py::test_marked`
- `gh-c2` fixed `tests/test_x.py::test_plain`
- `gh-c3` fixed `tests/test_x.py::test_marked` and `tests/test_x.py::test_deleted`
"""
    report = judge(body, "gh-c1", "gh-c2", "gh-c3", tests=fl.TestIndex(repo))
    assert statuses(report) == {"gh-c1": "ok", "gh-c2": "ok", "gh-c3": "test-missing"}
    rows = {r.id: r for r in report.rows}
    assert rows["gh-c1"].notes == []
    assert rows["gh-c2"].notes and "no `finding(\"gh-c2\")` marker" in rows["gh-c2"].notes[0]


# --- rendering ----------------------------------------------------------------

def test_the_report_starts_with_the_marker_and_gives_a_paste_ready_skeleton():
    report = fl.evaluate(
        fl.collect_github_findings([], [comment(11, body="**Bug** @someone <b>x</b>")]),
        "## Review round 2\n")
    text = fl.render(report, head_sha="1ddd426692a1")
    assert text.startswith(MARKER + "\n")
    assert "Checked round 2 at `1ddd426`: 0 of 1 findings" in text
    assert "- `gh-c11` fixed|residual|declined|deferred|dup|noted ..." in text
    assert "<b>" not in text and "@someone" not in text


def test_annotations_escape_newlines():
    row = fl.Row("gh-c1", "invalid", "a\nb%")
    assert fl.annotations(fl.Report([row], 1, [])) == [
        "::warning title=Findings ledger::gh-c1 invalid: a%0Ab%25"]


# --- CLI and GitHub I/O -------------------------------------------------------

@pytest.fixture
def files(tmp_path):
    import json
    (tmp_path / "reviews.json").write_text(json.dumps([review(1)]))
    (tmp_path / "comments.json").write_text(json.dumps([comment(11)]))
    (tmp_path / "body.md").write_text("## Review round 1\n- `gh-r1` noted\n")
    return tmp_path


def cli(files, *extra):
    return fl.main(["check", "--repo", "o/r", "--pr", "7", "--reviews-file", str(files / "reviews.json"),
                    "--comments-file", str(files / "comments.json"), "--body-file", str(files / "body.md"),
                    *extra])


def test_cli_exits_1_on_problems_and_0_with_warn_only(files, capsys):
    assert cli(files) == 1
    assert cli(files, "--warn-only") == 0
    assert "gh-c11" in capsys.readouterr().out


def test_cli_exits_0_when_every_finding_is_dispositioned(files):
    (files / "body.md").write_text("## Review round 1\n- `gh-r1` noted\n- `gh-c11` residual: fine\n")
    assert cli(files) == 0


def test_an_api_failure_fails_open_only_in_warn_only_mode(monkeypatch, capsys):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "gh")
    monkeypatch.setattr(fl, "gh_api", boom)
    args = ["check", "--repo", "o/r", "--pr", "7"]
    assert fl.main(args + ["--warn-only"]) == 0
    assert fl.main(args) == 2
    assert "could not read PR #7" in capsys.readouterr().out


class FakeApi:
    def __init__(self, head="a" * 40, comments=()):
        self.head, self.comments, self.calls = head, list(comments), []

    def __call__(self, path, method="GET", body_file=None, paginate=False):
        self.calls.append((method, path))
        if path.endswith("/pulls/7"):
            return {"head": {"sha": self.head}}
        if path.endswith("/issues/7/comments") and method == "GET":
            return self.comments
        return {}


def bot_comment(cid, body=MARKER + "\nold"):
    return {"id": cid, "user": {"login": "github-actions[bot]"}, "body": body}


def test_sticky_comment_is_edited_in_place(tmp_path):
    api = FakeApi(comments=[bot_comment(5), {"id": 6, "user": {"login": "x"}, "body": MARKER}, bot_comment(8)])
    fl.post_sticky("o/r", 7, "a" * 40, str(tmp_path / "c.md"), api=api)
    assert ("PATCH", "repos/o/r/issues/comments/8") in api.calls
    assert not any(m == "POST" for m, _ in api.calls)


def test_sticky_comment_is_created_once_when_absent(tmp_path):
    api = FakeApi(comments=[bot_comment(5, body="<!-- growin-pr-impact -->")])
    fl.post_sticky("o/r", 7, "a" * 40, str(tmp_path / "c.md"), api=api)
    assert ("POST", "repos/o/r/issues/7/comments") in api.calls


def test_a_stale_head_posts_nothing(tmp_path):
    api = FakeApi(head="b" * 40, comments=[bot_comment(5)])
    out = fl.post_sticky("o/r", 7, "a" * 40, str(tmp_path / "c.md"), api=api)
    assert "stale" in out
    assert [m for m, _ in api.calls] == ["GET"]


# --- the workflow stays advisory ----------------------------------------------

def test_the_workflow_is_warn_only_and_never_uses_pull_request_target():
    text = WORKFLOW.read_text()
    assert "continue-on-error: true" in text
    assert "--warn-only" in text
    assert "pull_request_target" not in text
    assert "persist-credentials: false" in text
    assert "            GrowinTests\n" in text
    for event in ("pull_request:", "pull_request_review:", "pull_request_review_comment:"):
        assert event in text
    assert "types: [ submitted, edited, dismissed ]" in text
