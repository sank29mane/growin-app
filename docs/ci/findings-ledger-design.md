# Findings ledger and review-integrity checks

Status: exploration. A warn-only prototype of idea 1 ships with this note.
Nothing here blocks a merge.

## Problem

Two process failures, both seen on recent PRs:

1. **A finding never enters the fix loop.** On #585 a GitHub review posted at
   head `f816449` reported a liveness bug. Windows that keep failing are not
   terminal, so they get re-queued first, and two always-failing
   `upstream_shape` codes trip `stopped_consecutive_failures` on every resume.
   Later symbols never get fetched. Four fix rounds followed. Each
   `## Review round N` section listed the findings from the reviewers that
   reported back to the agent (Codex, Opus, a second Grok pass), and none of
   them listed this one. The same reviewer reported it again at `1ddd426` as
   "not fixed". The fix briefs carried reviewer verdicts but not GitHub
   review comments, and nothing checked that every finding got an answer.
2. **A finding is fixed, then quietly regresses.** A later round changes
   the code again, and no test is keyed to the finding, so nobody notices.

The threat model is honest but fallible agents and humans. The goal is to
stop findings getting lost. It is not to stop a hostile author gaming the
ledger. A determined author can always write `declined: no`. The check only
makes the decision visible.

## What the data shows

Pulled from the GitHub API for #585, #586 and #589:

| PR | GitHub reviews | Top-level inline comments | Derived findings | Dispositioned in the body |
|---|---|---|---|---|
| #585 | 2 (both via the operator's login) | 6 | 8 | 0 |
| #586 | 2 (CodeRabbit, operator's login) | 8 | 10 | 0 |
| #589 | 1 (operator's login) | 2 | 3 | 0 |

Things that shape the design:

- The automated reviewer posts **as the PR author's login**, with
  `author_association: OWNER`. You cannot tell reviewer from author by
  login, so the check must not filter on identity.
- Round sections are free prose, and labels are local to a round: "Codex
  P1", "Opus P3d", "Grok P3-1", "(Grok: 2 P1, 1 P2)". Numbering restarts
  every round. There is no id that survives into the next round.
- #586 has **two** `## Review round 2` headings (one after the CodeRabbit
  summary block). The parser takes the highest N, and the last section if
  N repeats.
- Some findings are real duplicates across sources. #586's GitHub comment
  on `strict: True` with the raw schema is the same issue as round 1 item 1
  from Codex. A `dup` disposition costs one line.
- On #585 one body edit held another PR's body for about 78 seconds before
  it was corrected. Any check driven by `edited` events will sometimes see
  a transient wrong body. The sticky comment self-heals on the next event.
  An enforcing check needs to tolerate this (see "Path to enforcement").

## Idea 1: findings ledger check (prototype in this PR)

### Ids

Hybrid, decided by where the finding lives:

- **GitHub findings get derived ids.** `gh-r<review id>` for a non-empty
  review body, `gh-c<comment id>` for a top-level inline comment. Replies
  are discussion, not findings. GitHub ids are stable and unique, and
  nobody has to type them right. The sticky comment prints a paste-ready
  line for each one. Hashing (review id + comment id) adds nothing: the
  comment id alone is already unique.
- **Findings that never reach GitHub use reviewer-assigned ids** in the
  form `<source>-r<round>-<n>`, for example `codex-r4-p1a`. There is
  nothing to derive these from, so the convention goes into the reviewer
  brief ("prefix each finding with an id"), and the fix brief copies them
  through.

Reviewer-supplied aliases on GitHub comments (a reviewer typing its own
id into the comment) were considered and dropped. Two ids for one finding
is a second source of truth.

### Ledger format

One line per finding, inside the latest `## Review round N` section. List
or table rows both work, and the backticks are optional:

```
- `gh-c4337853615` fixed `tests/backend/test_pilot_data_backfill.py::test_repeating_failures_do_not_block_later_windows`
- `gh-c4339119151` dup `gh-c4337853615`
- `codex-r4-p1a` residual: fails closed, see Residuals
- `grok-r4-2` declined: the window size makes this unreachable
- `opus-r3-m10` deferred: phase 62
- `gh-r5601040128` noted
```

| Disposition | Rule |
|---|---|
| `fixed` | Must name at least one test node id: `path.py::name` (nested classes join with `::`) or `path.swift::testName`. With `--repo-root`, each named test must exist at the head. Python files are parsed with `ast`. Swift files get a text scan after comments and string literals are blanked: a function counts if it carries `@Test`, or is an XCTest-style `func test...()` instance method with no arguments that is not `private` or `static`. A commented-out test reads as `test-missing`. The scan does not check that the method sits in an `XCTestCase` subclass. Nothing is imported, built or run. A missing `@pytest.mark.finding("<id>")` marker on a Python test is a note, not a problem (idea 3). Swift tests carry no markers. |
| `residual`, `declined`, `deferred` | Need a reason. |
| `dup <id>` | Takes the status of the target. Loops are invalid, and so is an unknown target. |
| `noted` | Only for review summaries (`gh-r…`), which often hold no action. |

"Known findings" means every derived GitHub finding plus every id that
appears in any round's ledger. An id listed in round 2 and missing from
round 4 is flagged as "dropped". That catches the bookkeeping half of the
regression problem.

Only reviews from `OWNER`, `MEMBER` or `COLLABORATOR`, plus named bots
(`coderabbitai[bot]`), count. Anyone can review a public PR, and a drive-by
reviewer should not be able to add required work. Dismissed reviews drop
out, along with their inline comments.

Every top-level inline comment from a trusted author is a finding,
including the PR author's own notes to reviewers. That is deliberate. The
automated reviewer posts as the PR author, so association cannot tell the
two apart. Filtering on severity tags (`**Bug`, `**Nit`) would tie the
check to one reviewer's style, and an untagged real finding would then
pass silently, which is the #585 failure again. An author note costs one
`declined` line.

### Dry run on #585

I replayed the check at each point in #585's history, using the body as
it was at each push (from the edit history) and only the reviews posted
up to that time:

| When | Head | Body | Findings | Flagged |
|---|---|---|---|---|
| PR opened | `f816449` | round 1, "no findings yet" | 0 | none |
| First GitHub review | `f816449` | round 1 | 5 | all 5, including the liveness bug `gh-c4337853615` |
| Round 2 pushed | `99e804c` | rounds 1-2 | 5 | all 5 |
| Round 3 pushed | `f5f5c95` | rounds 1-3 | 5 | all 5 |
| Round 4 pushed | `1ddd426` | rounds 1-4 | 5 | all 5 |
| Second GitHub review | `1ddd426` | rounds 1-4 | 8 | all 8, including the re-report `gh-c4339119151` |

So from the first review onward, every push would have carried a warning
naming the liveness bug, with a paste-ready ledger line for it. Two of the
five (the IST midnight and `RelayUnavailable` suggestions) were in fact
fixed in round 1 under Codex and Opus labels. They would have needed
`dup` lines, which is the cost of not sharing ids across sources.

A hypothetical round 5 that claims the liveness bug as `fixed` by a test
that does not exist at `1ddd426` comes back `test-missing`. The `dup`
re-report inherits that status. That is the guard against "fixed" with
nothing behind it.

#586 and #589 are both merged with 0 of 10 and 0 of 3 GitHub findings
dispositioned. Some were addressed under other labels and some were nits.
The check would have asked for 13 extra lines across the two PRs.

### Assessment

- **Value: high.** It closes the exact gap behind #585. It is mechanical
  and cheap, and the sticky comment doubles as the input for the next fix
  brief.
- **False positives: low to medium, and cheap to clear.** Every review
  summary and every nit needs a line. Cross-source duplicates need `dup`.
  None of these is wrong. Each costs one line. The real FP risks are parse
  failures (someone writes the ledger in a format the regex misses) and a
  transient wrong body during edits.
- **CI minutes: about 20 to 30 s per run** on `ubuntu-latest`, with a
  sparse checkout and no dependency install (stdlib only). Concurrency
  cancels superseded runs. A PR with five pushes, five body edits and three
  reviews is roughly 15 runs, so about 5 to 8 minutes. The repo is public,
  so standard runners cost nothing, and it never competes for the macOS
  runners.
- **Maintenance: low.** About 560 lines of stdlib Python and 50 offline
  tests. The fragile parts are the ledger regex and GitHub's event
  payloads.
- **Fails open or closed:** today it always fails open (`--warn-only`,
  `continue-on-error`). In enforcing mode, an API read error exits 2, which
  fails closed. A parse miss shows up as "missing", which also fails
  closed. So the enforcing version never silently passes a PR it could not
  read.

## Idea 2: automated break-proofs

**The plain version:** for each test node a PR adds or changes, run it
against the merge base (test files overlaid from the head) and require it
to fail.

The motivating data argues against the plain version. #586 round 2's own
break-proof table lists tests that are **green on the reverted code on
purpose**: "already rejected by `parse_constant`" and "fail-closed by
accident, these tests lock that in". Lock-in and characterization tests are
legitimate. #585 round 4 item 6 changed parametrize ids for `pytest -n auto`
determinism, which makes "new" node ids that pass on the base. A blanket
"must fail on base" rule flags all of these.

**The better version is keyed to the ledger:** for each `fixed` disposition,
run the named tests at the head where the finding was raised, and require
red there. For GitHub findings that commit is known exactly
(`original_commit_id`, shown in the "At" column of the sticky comment). For
reviewer-id findings, the round section would need a `Reviewed head: <sha>`
line, or the check falls back to the merge base.

- **Value: high.** It proves the named test detects the bug, which is what
  the hand-run break-proof tables in PR bodies do today. And it runs every
  time, not once.
- **False positives:** low in the keyed version, high in the plain one. A
  test that cannot import on the old head counts as red, as asked. It is
  weak evidence, though (it may fail only because a new helper is
  missing), so report it as `red (import)`, separate from `red (assert)`.
- **CI minutes:** one extra job. It needs a second checkout at the old
  sha, `uv sync` (cached, about 30 to 60 s), and only the named node ids.
  Collection dominates. For a typical PR that is 2 to 4 minutes on
  `ubuntu-latest`. Tests that need macOS-only dependencies (MLX) would need
  the macOS runner or an explicit skip list.
- **Maintenance: medium.** Overlaying head test files onto an old tree
  breaks when a test imports a new testkit. Conftest drift between heads
  and parametrize-id drift both need care. Rebased PRs lose the old sha
  from the branch. GitHub usually still serves a force-pushed sha by id,
  but nothing guarantees it.
- **Fails open or closed:** a missing old sha should be "unverified", not
  pass. An enforcing version should fail closed on "unverified" only for
  findings on safety paths.

## Idea 3: finding-keyed regression tests

Keep reviewer repro probes as tests marked
`@pytest.mark.finding("grok-r4-1")`. The ledger check then confirms that a
`fixed` test carries the matching marker. The prototype already reports a
missing marker as a note.

- **Value: medium on its own, high as glue.** A failing test names the
  finding it guards, so a later regression points straight back to the
  review that found it. The pinned pytest (7.4.4) cannot select on marker
  arguments with `-m` (8.0 added that, for keyword arguments only), so
  `pytest --collect-only` plus a small plugin or the ast index would list
  every finding-keyed test.
- **False positives: none** while it stays a note. If enforced, the cost
  is one decorator per fixed finding.
- **CI minutes: zero** (it is part of the ledger run).
- **Maintenance: low.** Register the marker in `backend/pyproject.toml`
  (`markers = ["finding(id): ..."]`) so `--strict-markers` stays possible.
  This PR does not touch that file.
- **Fails open or closed:** the same as idea 1.

## Idea 4: nightly diff-scoped mutation testing

`mutmut` (or `cosmic-ray`) on safety-path files changed in the last N
merges.

Honest cost, measured here: `backend/execution/ledger.py` is 4,310 lines,
and its direct test file runs 12 tests in 7.4 s (23 s wall clock including
`uv` start-up). A 4,000-line module typically yields 1,500 to 2,500
mutants. Even at 7 s per mutant against only that one test file, that is
3 to 5 hours for one file, and the file has other covering tests. The
safety-path Python touched by 64 merges since 2026-10-01 adds up to about
31,000 lines, which is days of CPU per night. File scope is not viable.

What is viable is **scope by changed function**: diff the last N merges,
map hunks to function names, and run mutmut on those names only. A typical
safety PR changes 100 to 300 lines, which comes to 100 to 300 mutants. At
about 10 s each, that is 15 to 50 minutes nightly on one `ubuntu-latest`
runner. That fits the free tier, but MLX-dependent code needs macOS.

- **Value: medium.** It finds weak tests nobody asked about, but the
  break-proof tables in PR bodies already cover the guards that matter.
  Ideas 2 and 3 make that discipline automatic for each finding.
- **False positives: high.** Equivalent mutants, logging and message text,
  and timeouts all need triage.
- **Maintenance: high.** It needs a survivor baseline, a triage process and
  flake handling.
- **Fails open:** nightly and informational only. It should never gate.

## Recommendation

Build **1, then 2 keyed to the ledger, with 3 riding along as a note.
Skip 4** for now.

I expected 1 then 2 and, after looking at the data, I agree, with one
change. Idea 2 should not be "every changed test must fail on the base".
It should be "every test named in a `fixed` disposition must fail at the
head where the finding was raised". That only works once idea 1 exists,
because the ledger is what names the test and the finding. The plain
version of idea 2 would have flagged legitimate lock-in tests in #586's
own break-proof table. Idea 1 alone would have caught the motivating case
on its first run.

## Path to enforcement

1. Run warn-only on real PRs for about two weeks. Track how often a ledger
   line misparses, and how many lines a typical PR needs.
2. Move the script to `.github/scripts/` and run it the way Safety Guard
   does: from the default branch, with no PR code executed, so a PR cannot
   rewrite the check that judges it. The `fixed` test lookup then needs
   the PR's test files read through the API as data
   (`repos/.../contents/<path>?ref=<head>`), not checked out.
3. Make it a required check for every PR, failing on `missing` and
   `invalid` only. `test-missing` stays a warning until idea 2 runs the
   named tests, because a test that exists can still prove nothing.
4. Debounce `edited` events, or re-read the body once after a short delay
   before failing, to absorb transient wrong bodies.

## Decisions (operator, 2026-10-22)

The operator took the defaults below. Each one holds only while it pays
for itself on real PRs; the two-week warn-only run is where that gets
checked.

1. **Review summaries need a line.** `gh-r` ids stay in the ledger and
   `noted` closes them. Summaries hold bugs too (the #585 second review's
   status table).
2. **CodeRabbit counts.** It skips draft PRs, so on this repo it mostly
   reviews a PR once it is ready. A noisy comment costs one `declined`
   line.
3. **Enforcement covers every PR**, but blocks only on `missing` and
   `invalid` (step 3 above). The #585 miss was a process failure, not a
   path one.
4. **Reviewer briefs use `<source>-r<round>-<n>` ids, and every fix brief
   carries the sticky comment verbatim.** This needs no CI and starts now.
   It is what would have put the #585 liveness bug in front of the fixer
   on round 1.
5. **A `fixed` line names its tests by node id.** The `finding` marker
   stays optional: Swift tests cannot carry it, and pytest 7.4.4 cannot
   select on its arguments.

Order of work: brief templates now; this PR merges warn-only; two weeks of
data; then the enforcement steps above; then idea 2. Idea 4 stays skipped.

## Deferred

- Re-report detection: flag a `dup` that points at a finding already
  marked `fixed` when the duplicate was posted on a later head. That is the
  "it regressed" signal. It needs body edit history from GraphQL.
- A `Reviewed head: <sha>` line convention per round, so idea 2 knows
  where reviewer-id findings were raised.
- Feeding open-finding counts into the operator dashboard issue.
- Reading reviewer verdicts straight from T3 threads instead of relying on
  the round section. Out of scope for a GitHub check.
- Registering the `finding` marker in `backend/pyproject.toml`.
