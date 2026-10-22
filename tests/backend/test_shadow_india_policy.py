"""Phase 64-03 Task 1: the pause and abort policy table (D-07, D-08, P-06, T-64-16).

State comes only from evidence records on disk. The restart rows reopen the store, once in a real child process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from shadow_india import policy
from shadow_india.evidence import EvidenceAborted, EvidenceStore
from shadow_india.policy import (
    ABORT,
    PAUSE,
    POLICY,
    PolicyError,
    RunAborted,
    RunPaused,
    raise_event,
    resume,
    rule_for,
    run_state,
    state_from_records,
)

BACKEND = str(Path(__file__).resolve().parents[2] / "backend")
HASHES = {"strategy_sha256": "a" * 64}
ISIN = "INE002A01018"
OTHER_ISIN = "INE009A01021"
CLEAR_ISIN = "INE999A01010"  # an instrument no test pauses; entry steps need some valid ISIN
ALL_STEPS = (policy.STEP_ADMIT, policy.STEP_MINT, policy.STEP_BOOK, policy.STEP_EXPIRE, policy.STEP_READ_ONLY)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 22, 4, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


def make_store(tmp_path: Path, clock: Clock | None = None) -> EvidenceStore:
    return EvidenceStore.create(
        tmp_path, run_id="run1", run_class="plumbing", session_id="s-1", hashes=HASHES, clock=clock or Clock()
    )


def reopen(tmp_path: Path) -> EvidenceStore:
    return EvidenceStore.open(tmp_path, run_id="run1", session_id="s-1")


def blocked_steps(store: EvidenceStore, *, isin: str = CLEAR_ISIN) -> set[str]:
    state = run_state(store)
    return {step for step in ALL_STEPS if state.blocks(step, isin=isin)}


# --- the table ---------------------------------------------------------------------------------------

D07_ABORT = [
    "broker_mutation_attempted", "bypass_detected", "secret_leaked", "audit_corrupt", "state_corrupt",
    "unexplained_account_position", "evidence_corrupt", "authorize_attempted", "ledger_evidence_disagree",
]
P06_ABORT = [
    "vm_audit_broken", "vm_state_unreadable", "vm_state_unwritable", "vm_config_invalid", "vm_clock_inconsistent",
    "vm_order_code", "vm_unknown_code", "code_status_mismatch", "vm_challenge_or_success", "vm_423_no_audit_entry",
    "vm_audit_duplicate_entries", "vm_audit_codes_unexpected", "vm_audit_chain_break",
]
D07_PAUSE_ISIN = [
    "missing_bar", "unresolved_corporate_action", "ambiguous_exit_fill", "identity_ambiguity", "band_ambiguity",
]
D07_PAUSE_ALL = [
    "stale_data", "freshness_check_failed", "session_expired", "quota_failure", "egress_failure", "contract_mismatch",
    "hash_mismatch",
]
P06_PAUSE_ALL = [
    "kill_label_unreadable", "untyped_response", "relay_unauthorized", "relay_no_session", "relay_session_expired",
    "relay_egress_mismatch", "relay_budget_unavailable", "relay_budget_exhausted", "relay_contract_mismatch",
    "relay_forbidden_client",
]
ENTRIES_PAUSE = ["divergence_breach", "drawdown_halt"]
RUN_END = ["drawdown_flatten_15"]


@pytest.mark.parametrize("event", D07_ABORT + P06_ABORT)
def test_abort_events_are_aborts(event):
    assert rule_for(event) == policy.Rule(ABORT, policy.SCOPE_ALL)


@pytest.mark.parametrize("event", D07_PAUSE_ISIN)
def test_instrument_scoped_pauses(event):
    assert rule_for(event) == policy.Rule(PAUSE, policy.SCOPE_ISIN)


@pytest.mark.parametrize("event", D07_PAUSE_ALL + P06_PAUSE_ALL)
def test_pauses_that_stop_all_processing(event):
    assert rule_for(event) == policy.Rule(PAUSE, policy.SCOPE_ALL)


@pytest.mark.parametrize("event", ENTRIES_PAUSE)
def test_pauses_that_stop_new_entries(event):
    assert rule_for(event) == policy.Rule(PAUSE, policy.SCOPE_ENTRIES)


@pytest.mark.parametrize("event", RUN_END)
def test_the_minus_15_flatten_is_a_terminal_run_end_for_entries(event):
    assert rule_for(event) == policy.Rule(policy.RUN_ENDED, policy.SCOPE_ENTRIES)


def test_the_table_is_exactly_the_events_above_and_every_row_is_a_pause_an_abort_or_a_run_end():
    listed = set(
        D07_ABORT + P06_ABORT + D07_PAUSE_ISIN + D07_PAUSE_ALL + P06_PAUSE_ALL + ENTRIES_PAUSE + RUN_END
    )
    assert set(POLICY) == listed
    assert {rule.action for rule in POLICY.values()} == {PAUSE, ABORT, policy.RUN_ENDED}


def test_an_unclassified_event_raises_and_writes_nothing(tmp_path):
    store = make_store(tmp_path)
    for event in ("never_heard_of_it", "", "KILL_SWITCH"):
        with pytest.raises(PolicyError) as caught:
            rule_for(event)
        assert caught.value.code == "unknown_event"
        with pytest.raises(PolicyError):
            raise_event(store, event, "reason")
    assert store.records() == []


# --- raising an event ----------------------------------------------------------------------------------


def test_raise_event_records_the_scoped_pause_or_the_abort(tmp_path):
    store = make_store(tmp_path)
    paused = raise_event(store, "stale_data", "bars older than one session")
    assert (paused["kind"], paused["event"], paused["scope"]) == ("pause", "stale_data", "all")
    isin_pause = raise_event(store, "band_ambiguity", "two bands on file", isin=ISIN)
    assert isin_pause["scope"] == f"isin:{ISIN}"
    aborted = raise_event(store, "audit_corrupt", "chain break")
    assert (aborted["kind"], aborted["event"]) == ("abort", "audit_corrupt") and "scope" not in aborted


@pytest.mark.parametrize(
    "isin", [None, "", "short", "ine002a01018", ISIN + "\n", ISIN + "\r\n", "\n" + ISIN, ISIN + "\n\n"]
)
def test_an_instrument_pause_needs_a_real_isin(tmp_path, isin):
    store = make_store(tmp_path)
    with pytest.raises(PolicyError) as caught:
        raise_event(store, "missing_bar", "no bar", isin=isin)
    assert caught.value.code == "isin_required" and store.records() == []


# --- entry steps need the instrument (fails closed) -------------------------------------------------------


@pytest.mark.parametrize("event", D07_PAUSE_ISIN)
@pytest.mark.parametrize("step", [policy.STEP_ADMIT, policy.STEP_MINT])
def test_an_instrument_pause_is_enforced_whatever_the_caller_passes(tmp_path, event, step):
    store = make_store(tmp_path)
    raise_event(store, event, "data problem", isin=ISIN)
    state = run_state(store)
    for bad in (None, "", "ine002a01018", "INE002A0101", "RELIANCE", ISIN + " ", 5, ISIN + "\n", ISIN + "\r\n", "\n" + ISIN):
        with pytest.raises(PolicyError) as caught:
            state.blocks(step, isin=bad)  # type: ignore[arg-type]
        assert caught.value.code == "isin_required"
        with pytest.raises(PolicyError) as caught:
            state.require(step, isin=bad)  # type: ignore[arg-type]
        assert caught.value.code == "isin_required"
    assert state.blocks(step, isin=ISIN)
    with pytest.raises(RunPaused):
        state.require_entry(step, ISIN)
    assert not state.blocks(step, isin=OTHER_ISIN)
    state.require_entry(step, OTHER_ISIN)


def test_the_instrument_is_not_needed_for_steps_that_are_not_entries(tmp_path):
    store = make_store(tmp_path)
    raise_event(store, "missing_bar", "no bar", isin=ISIN)
    state = run_state(store)
    for step in (policy.STEP_BOOK, policy.STEP_EXPIRE, policy.STEP_READ_ONLY):
        assert not state.blocks(step)
        state.require(step)


def test_require_entry_refuses_a_missing_instrument_and_a_non_entry_step(tmp_path):
    state = run_state(make_store(tmp_path))
    with pytest.raises(PolicyError) as no_isin:
        state.require_entry(policy.STEP_ADMIT, None)  # type: ignore[arg-type]
    assert no_isin.value.code == "isin_required"
    with pytest.raises(PolicyError) as wrong_step:
        state.require_entry(policy.STEP_BOOK, ISIN)
    assert wrong_step.value.code == "not_an_entry_step"
    state.require_entry(policy.STEP_ADMIT, ISIN)


def test_an_abort_still_refuses_entry_steps_and_a_missing_instrument_is_refused_too(tmp_path):
    store = make_store(tmp_path)
    raise_event(store, "audit_corrupt", "chain break")
    state = run_state(store)
    with pytest.raises(RunAborted):
        state.require_entry(policy.STEP_ADMIT, ISIN)
    with pytest.raises(PolicyError):
        state.require(policy.STEP_ADMIT)


# --- what each scope blocks ---------------------------------------------------------------------------


def test_scopes_block_exactly_their_steps(tmp_path):
    store = make_store(tmp_path)
    assert blocked_steps(store) == set()
    raise_event(store, "divergence_breach", "gap")
    assert blocked_steps(store) == {"admit", "mint"}  # bookings and expiries keep running
    raise_event(store, "missing_bar", "no bar", isin=ISIN)
    assert blocked_steps(store, isin=ISIN) == {"admit", "mint"}
    assert blocked_steps(store, isin=OTHER_ISIN) == {"admit", "mint"}  # still the entries pause
    raise_event(store, "stale_data", "old bars")
    assert blocked_steps(store) == {"admit", "mint", "book", "expire"}  # read-only checks still run


def test_an_instrument_pause_does_not_block_another_instrument(tmp_path):
    store = make_store(tmp_path)
    raise_event(store, "missing_bar", "no bar", isin=ISIN)
    assert blocked_steps(store, isin=ISIN) == {"admit", "mint"}
    assert blocked_steps(store, isin=OTHER_ISIN) == set()
    assert blocked_steps(store) == set()
    with pytest.raises(RunPaused) as caught:
        run_state(store).require(policy.STEP_ADMIT, isin=ISIN)
    assert caught.value.code == "run_paused"
    run_state(store).require(policy.STEP_ADMIT, isin=OTHER_ISIN)


@pytest.mark.parametrize("event", D07_PAUSE_ALL + P06_PAUSE_ALL)
def test_a_pause_that_stops_processing_blocks_booking_and_expiry_too(tmp_path, event):
    store = make_store(tmp_path)
    raise_event(store, event, "operator review needed")
    state = run_state(store)
    for step in (policy.STEP_ADMIT, policy.STEP_MINT, policy.STEP_BOOK, policy.STEP_EXPIRE):
        with pytest.raises(RunPaused):
            state.require(step, isin=CLEAR_ISIN)
    state.require(policy.STEP_READ_ONLY)


def test_an_unknown_step_is_refused(tmp_path):
    with pytest.raises(PolicyError) as caught:
        run_state(make_store(tmp_path)).blocks("send_order")
    assert caught.value.code == "unknown_step"


# --- an abort is terminal -------------------------------------------------------------------------------


def test_an_abort_blocks_every_step_and_resume_refuses_it(tmp_path):
    store = make_store(tmp_path)
    raise_event(store, "audit_corrupt", "chain break")
    state = run_state(store)
    assert state.aborted and state.abort_events == ("audit_corrupt",)
    for step in ALL_STEPS:
        with pytest.raises(RunAborted) as caught:
            state.require(step, isin=CLEAR_ISIN)
        assert caught.value.code == "run_aborted"
    before = store.records()
    for reason in ("operator reviewed", "x"):
        with pytest.raises(PolicyError) as refused:
            resume(store, reason)
        assert refused.value.code == "resume_refused_abort"
    assert store.records() == before


def test_an_abort_after_a_pause_cannot_be_cleared_by_clearing_the_pause_first(tmp_path):
    store = make_store(tmp_path)
    paused = raise_event(store, "stale_data", "old bars")
    raise_event(store, "bypass_detected", "a guard was skipped")  # an abort lands while the pause is open
    with pytest.raises(PolicyError) as caught:
        resume(store, "reviewed", pause_seqs=[paused["seq"]])
    assert caught.value.code == "resume_refused_abort"
    assert run_state(store).aborted and blocked_steps(store) == set(ALL_STEPS)


def test_a_pause_cleared_earlier_does_not_undo_a_later_abort(tmp_path):
    store = make_store(tmp_path)
    raise_event(store, "stale_data", "old bars")
    resume(store, "bars refreshed and checked")
    raise_event(store, "vm_audit_broken", "503 audit_broken")
    assert run_state(store).aborted
    with pytest.raises(PolicyError):
        resume(store, "again")


def test_the_store_itself_stops_a_run_after_an_abort(tmp_path):
    store = make_store(tmp_path)
    raise_event(store, "secret_leaked", "canary in a log")
    with pytest.raises(EvidenceAborted):
        store.append("intent", _intent())
    with pytest.raises(EvidenceAborted):
        store.append("pause", {"event": "stale_data", "scope": "all", "reason": "late"})
    store.append("session_close", {"outcome": "aborted"})


def _intent() -> dict:
    return {
        "intent_id": "a" * 64, "signal_id": "sig-1", "leg": "entry", "intent_sha256": "b" * 64, "symbol": "RELIANCE",
        "side": "BUY", "quantity": "2", "limit_price": "100.00",
    }


# --- the recognised corruption codes are aborts even while a pause is open ----------------------------------


@pytest.mark.parametrize(
    "event", ["vm_audit_broken", "vm_state_unreadable", "vm_state_unwritable", "vm_config_invalid", "vm_clock_inconsistent"]
)
def test_a_corruption_code_aborts_even_while_a_pause_is_open(tmp_path, event):
    store = make_store(tmp_path)
    raise_event(store, "relay_session_expired", "session expired")
    assert not run_state(store).aborted
    raise_event(store, event, "typed answer while paused")
    assert run_state(store).aborted and run_state(store).abort_events == (event,)


# --- resume --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["", "   ", "\n", None, 5])
def test_resume_needs_a_non_empty_reason_and_writes_nothing_without_one(tmp_path, reason):
    store = make_store(tmp_path)
    raise_event(store, "stale_data", "old bars")
    before = store.records()
    with pytest.raises(PolicyError) as caught:
        resume(store, reason)  # type: ignore[arg-type]
    assert caught.value.code == "reason_required"
    assert store.records() == before and run_state(store).blocks(policy.STEP_ADMIT, isin=CLEAR_ISIN)


def test_resume_clears_only_pauses_after_a_reason(tmp_path):
    store = make_store(tmp_path)
    first = raise_event(store, "stale_data", "old bars")
    second = raise_event(store, "missing_bar", "no bar", isin=ISIN)
    cleared = resume(store, "  checked the bars by hand  ")
    assert cleared["cleared_pause_seqs"] == [first["seq"], second["seq"]] and cleared["reason"] == "checked the bars by hand"
    state = run_state(store)
    assert state.open_pauses == () and not state.aborted and blocked_steps(store) == set()


def test_resume_can_clear_one_named_pause_and_refuses_unknown_or_closed_ones(tmp_path):
    store = make_store(tmp_path)
    first = raise_event(store, "stale_data", "old bars")
    second = raise_event(store, "divergence_breach", "gap")
    resume(store, "bars fixed", pause_seqs=[first["seq"]])
    assert [p.event for p in run_state(store).open_pauses] == ["divergence_breach"]
    for seqs in ([first["seq"]], [999], []):
        with pytest.raises(PolicyError) as caught:
            resume(store, "again", pause_seqs=seqs)
        assert caught.value.code == "unknown_pause"
    resume(store, "gap explained", pause_seqs=[second["seq"]])
    with pytest.raises(PolicyError) as nothing:
        resume(store, "once more")
    assert nothing.value.code == "nothing_to_resume"


def test_resume_refuses_a_drawdown_halt_and_writes_nothing(tmp_path):
    store = make_store(tmp_path)
    halt = raise_event(store, "drawdown_halt", "trailing drawdown past 8 percent")
    before = store.records()
    for seqs in (None, [halt["seq"]]):
        with pytest.raises(PolicyError) as caught:
            resume(store, "operator says it is fine", pause_seqs=seqs)
        assert caught.value.code == "drawdown_resume_requires_latch"
    assert store.records() == before
    assert [p.event for p in run_state(store).open_pauses] == ["drawdown_halt"]
    assert blocked_steps(store) == {"admit", "mint"}


def test_a_drawdown_halt_does_not_hide_behind_another_pause_in_the_same_resume(tmp_path):
    store = make_store(tmp_path)
    halt = raise_event(store, "drawdown_halt", "trailing drawdown past 8 percent")
    stale = raise_event(store, "stale_data", "old bars")
    with pytest.raises(PolicyError) as caught:
        resume(store, "bars fixed")  # an all-open resume would have swept the halt away
    assert caught.value.code == "drawdown_resume_requires_latch"
    resume(store, "bars fixed", pause_seqs=[stale["seq"]])  # naming only the other pause still works
    assert [p.seq for p in run_state(store).open_pauses] == [halt["seq"]]


def test_resume_refuses_an_unresolved_corporate_action_and_writes_nothing(tmp_path):
    """Only the ledger knows whether the split or bonus was booked, so the portfolio owns this release (review r2)."""
    store = make_store(tmp_path)
    pause = raise_event(store, "unresolved_corporate_action", "bonus on a held name", isin=ISIN)
    before = store.records()
    for seqs in (None, [pause["seq"]]):
        with pytest.raises(PolicyError) as caught:
            resume(store, "operator says it is fine", pause_seqs=seqs)
        assert caught.value.code == "corporate_action_resume_requires_portfolio"
    assert store.records() == before
    assert [p.event for p in run_state(store).open_pauses] == ["unresolved_corporate_action"]


def test_a_corporate_action_pause_does_not_hide_behind_another_pause_in_the_same_resume(tmp_path):
    store = make_store(tmp_path)
    action = raise_event(store, "unresolved_corporate_action", "bonus on a held name", isin=ISIN)
    stale = raise_event(store, "stale_data", "old bars")
    with pytest.raises(PolicyError) as caught:
        resume(store, "bars fixed")  # an all-open resume would have swept the pause away
    assert caught.value.code == "corporate_action_resume_requires_portfolio"
    resume(store, "bars fixed", pause_seqs=[stale["seq"]])
    assert [p.seq for p in run_state(store).open_pauses] == [action["seq"]]


@pytest.mark.parametrize("event", ["divergence_breach", "stale_data", "relay_no_session"])
def test_other_entry_and_all_pauses_stay_resumable(tmp_path, event):
    store = make_store(tmp_path)
    raise_event(store, event, "operator review needed")
    resume(store, "reviewed")
    assert run_state(store).open_pauses == ()


# --- no timer or restart clears anything (T-64-16) ---------------------------------------------------------


def test_time_and_new_marks_never_release_a_drawdown_halt(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    raise_event(store, "drawdown_halt", "trailing drawdown past 8 percent")
    for day in range(1, 11):
        clock.now += timedelta(days=day)
        store.append(
            "mark",
            {
                "as_of_date": f"2026-11-{day:02d}", "equity": "50000", "peak": "50000", "drawdown": "0",
                "cash": "50000", "held_capital": "0", "exposure": "0",
            },
        )  # equity fully recovered, days pass: still halted
        assert blocked_steps(store) == {"admit", "mint"}
    assert [p.event for p in run_state(store).open_pauses] == ["drawdown_halt"]


def test_a_pause_and_an_abort_survive_a_reopen(tmp_path):
    store = make_store(tmp_path)
    raise_event(store, "stale_data", "old bars")
    raise_event(store, "missing_bar", "no bar", isin=ISIN)
    assert [p.event for p in run_state(reopen(tmp_path)).open_pauses] == ["stale_data", "missing_bar"]
    raise_event(store, "audit_corrupt", "chain break")
    fresh = reopen(tmp_path)
    assert run_state(fresh).aborted
    with pytest.raises(PolicyError):
        resume(fresh, "operator reviewed")


STATE_CHILD = r"""
import json, sys
from shadow_india.evidence import EvidenceStore
from shadow_india.policy import run_state

store = EvidenceStore.open(sys.argv[1], run_id="run1", session_id="s-9")
state = run_state(store)
print(json.dumps({"aborted": state.aborted, "pauses": [p.event for p in state.open_pauses]}))
"""


def test_a_restart_in_a_new_process_reads_the_same_state(tmp_path):
    store = make_store(tmp_path)
    raise_event(store, "drawdown_halt", "trailing drawdown past 8 percent")
    done = subprocess.run(
        [sys.executable, "-P", "-c", STATE_CHILD, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": BACKEND},
        capture_output=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr.decode()
    assert json.loads(done.stdout) == {"aborted": False, "pauses": ["drawdown_halt"]}


def test_the_policy_is_a_function_of_the_records_alone():
    records = [
        {"kind": "pause", "seq": 1, "event": "stale_data", "scope": "all"},
        {"kind": "pause", "seq": 2, "event": "divergence_breach", "scope": "entries"},
        {"kind": "resume", "seq": 3, "cleared_pause_seqs": [1]},
    ]
    state = state_from_records(records)
    assert [(p.seq, p.event) for p in state.open_pauses] == [(2, "divergence_breach")]
    assert state_from_records(records) == state
    assert state_from_records([]) == policy.RunState((), ())


# --- the terminal run end at -15% (D-08, 64-03 Task 2 addendum) -------------------------------------------


def end_run(store: EvidenceStore):
    return raise_event(store, policy.DRAWDOWN_FLATTEN_15, "equity_at_or_below_minus_15_percent_of_peak")


def test_the_run_end_is_recorded_without_a_scope_and_is_not_a_pause_or_an_abort(tmp_path):
    store = make_store(tmp_path)
    record = end_run(store)
    assert (record["kind"], record["event"]) == ("run_ended", "drawdown_flatten_15") and "scope" not in record
    state = run_state(store)
    assert state.ended and state.ended_events == ("drawdown_flatten_15",)
    assert not state.aborted and state.open_pauses == ()


def test_the_run_end_blocks_entries_for_good_and_lets_booking_expiry_and_reads_run(tmp_path):
    store = make_store(tmp_path)
    end_run(store)
    state = run_state(store)
    for isin in (ISIN, OTHER_ISIN, CLEAR_ISIN):
        for step in (policy.STEP_ADMIT, policy.STEP_MINT):
            assert state.blocks(step, isin=isin)
            with pytest.raises(policy.RunEnded) as caught:
                state.require_entry(step, isin)
            assert caught.value.code == "run_ended"
    for step in (policy.STEP_BOOK, policy.STEP_EXPIRE, policy.STEP_READ_ONLY):
        assert not state.blocks(step)
        state.require(step)


def test_resume_refuses_the_run_end_and_writes_nothing(tmp_path):
    store = make_store(tmp_path)
    end_run(store)
    before = store.records()
    for reason, seqs in (("operator reviewed the flatten", None), ("again", [1]), ("x", [])):
        with pytest.raises(PolicyError) as caught:
            resume(store, reason, pause_seqs=seqs)
        assert caught.value.code in {"resume_refused_run_ended", "unknown_pause"}
    with pytest.raises(PolicyError) as plain:
        resume(store, "operator reviewed the flatten")
    assert plain.value.code == "resume_refused_run_ended"
    assert store.records() == before and run_state(store).ended


def test_resume_of_another_pause_never_clears_the_run_end(tmp_path):
    store = make_store(tmp_path)
    end_run(store)
    pause = raise_event(store, "stale_data", "old bars")
    resume(store, "bars refreshed", pause_seqs=[pause["seq"]])
    state = run_state(store)
    assert state.open_pauses == () and state.ended
    assert blocked_steps(store) == {"admit", "mint"}
    with pytest.raises(PolicyError) as again:
        resume(store, "one more try")
    assert again.value.code == "resume_refused_run_ended"


def test_no_mark_no_time_and_no_restart_clears_the_run_end(tmp_path):
    clock = Clock()
    store = make_store(tmp_path, clock)
    end_run(store)
    for day in range(1, 8):
        clock.now += timedelta(days=day)
        store.append(
            "mark",
            {
                "as_of_date": f"2026-11-{day:02d}", "equity": "60000", "peak": "60000", "drawdown": "0",
                "cash": "60000", "held_capital": "0", "exposure": "0",
            },
        )
        assert blocked_steps(reopen(tmp_path)) == {"admit", "mint"}
    assert run_state(reopen(tmp_path)).ended


RUN_END_CHILD = r"""
import json, sys
from shadow_india.evidence import EvidenceStore
from shadow_india.policy import run_state

store = EvidenceStore.open(sys.argv[1], run_id="run1", session_id="s-9")
state = run_state(store)
print(json.dumps({"ended": state.ended, "admit": state.blocks("admit", isin="INE999A01010"),
                  "book": state.blocks("book")}))
"""


def test_a_new_process_reads_the_run_end_from_the_records(tmp_path):
    store = make_store(tmp_path)
    end_run(store)
    done = subprocess.run(
        [sys.executable, "-P", "-c", RUN_END_CHILD, str(tmp_path)],
        env={**os.environ, "PYTHONPATH": BACKEND},
        capture_output=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr.decode()
    assert json.loads(done.stdout) == {"ended": True, "admit": True, "book": False}


def test_an_abort_outranks_a_run_end_and_a_run_end_outranks_a_pause(tmp_path):
    store = make_store(tmp_path)
    raise_event(store, "stale_data", "old bars")
    end_run(store)
    with pytest.raises(policy.RunEnded):
        run_state(store).require_entry(policy.STEP_ADMIT, ISIN)
    raise_event(store, "audit_corrupt", "chain break")
    state = run_state(store)
    with pytest.raises(RunAborted):
        state.require_entry(policy.STEP_ADMIT, ISIN)
    with pytest.raises(RunAborted):
        state.require(policy.STEP_BOOK)


# --- buys stop, sells stay (D-08): the events that create an exit must not block it --------------------------------


ENTRY_STEPS = (policy.STEP_ADMIT, policy.STEP_MINT)


def test_the_side_defaults_to_the_stricter_buy_and_an_unknown_side_is_refused(tmp_path):
    store = make_store(tmp_path)
    raise_event(store, policy.DRAWDOWN_HALT, "equity_at_or_below_minus_8_percent_of_peak")
    state = run_state(store)
    for step in ENTRY_STEPS:
        assert state.blocks(step, isin=ISIN) and state.blocks(step, isin=ISIN, side=policy.SIDE_BUY)
        assert not state.blocks(step, isin=ISIN, side=policy.SIDE_SELL)
    for side in ("", "BUY", "short", None, 5, "buy\n", "sell\n"):
        for step in ALL_STEPS:
            with pytest.raises(PolicyError) as caught:
                state.blocks(step, isin=ISIN, side=side)  # type: ignore[arg-type]
            assert caught.value.code == "unknown_side"
            with pytest.raises(PolicyError) as required:
                state.require(step, isin=ISIN, side=side)  # type: ignore[arg-type]
            assert required.value.code == "unknown_side"


@pytest.mark.parametrize("step", ENTRY_STEPS)
def test_the_flatten_sell_passes_after_the_run_end_while_a_buy_is_refused(tmp_path, step):
    store = make_store(tmp_path)
    end_run(store)
    state = run_state(store)
    state.require_entry(step, ISIN, side=policy.SIDE_SELL)
    state.require(step, isin=ISIN, side=policy.SIDE_SELL)
    assert not state.blocks(step, isin=ISIN, side=policy.SIDE_SELL)
    with pytest.raises(policy.RunEnded) as caught:
        state.require_entry(step, ISIN, side=policy.SIDE_BUY)
    assert caught.value.code == "run_ended"
    with pytest.raises(policy.RunEnded):
        state.require_entry(step, ISIN)  # the default side is the buy


@pytest.mark.parametrize("step", ENTRY_STEPS)
def test_the_halve_sell_passes_under_drawdown_halt_while_a_buy_is_refused(tmp_path, step):
    store = make_store(tmp_path)
    raise_event(store, policy.DRAWDOWN_HALT, "equity_at_or_below_minus_8_percent_of_peak")
    state = run_state(store)
    state.require_entry(step, ISIN, side=policy.SIDE_SELL)
    with pytest.raises(RunPaused):
        state.require_entry(step, ISIN, side=policy.SIDE_BUY)


@pytest.mark.parametrize("event", ENTRIES_PAUSE)
@pytest.mark.parametrize("step", ENTRY_STEPS)
def test_an_entries_pause_blocks_buys_only(tmp_path, event, step):
    store = make_store(tmp_path)
    raise_event(store, event, "operator review needed")
    state = run_state(store)
    assert state.blocks(step, isin=CLEAR_ISIN, side=policy.SIDE_BUY)
    assert not state.blocks(step, isin=CLEAR_ISIN, side=policy.SIDE_SELL)


@pytest.mark.parametrize("event", D07_PAUSE_ISIN)
@pytest.mark.parametrize("step", ENTRY_STEPS)
def test_an_instrument_pause_blocks_a_buy_of_that_name_and_never_its_sell(tmp_path, event, step):
    store = make_store(tmp_path)
    raise_event(store, event, "data problem", isin=ISIN)
    state = run_state(store)
    assert state.blocks(step, isin=ISIN, side=policy.SIDE_BUY)
    assert not state.blocks(step, isin=ISIN, side=policy.SIDE_SELL)
    assert not state.blocks(step, isin=OTHER_ISIN, side=policy.SIDE_BUY)


@pytest.mark.parametrize("step", ENTRY_STEPS)
def test_a_sell_still_needs_a_real_isin(tmp_path, step):
    state = run_state(make_store(tmp_path))
    for bad in (None, "", "ine002a01018", ISIN + "\n", "\n" + ISIN, 5):
        with pytest.raises(PolicyError) as caught:
            state.blocks(step, isin=bad, side=policy.SIDE_SELL)  # type: ignore[arg-type]
        assert caught.value.code == "isin_required"
        with pytest.raises(PolicyError) as required:
            state.require_entry(step, bad, side=policy.SIDE_SELL)  # type: ignore[arg-type]
        assert required.value.code == "isin_required"


@pytest.mark.parametrize("event", D07_PAUSE_ALL + P06_PAUSE_ALL)
@pytest.mark.parametrize("step", ENTRY_STEPS)
def test_an_all_pause_blocks_the_sell_too(tmp_path, event, step):
    store = make_store(tmp_path)
    raise_event(store, event, "operator review needed")
    state = run_state(store)
    with pytest.raises(RunPaused):
        state.require_entry(step, ISIN, side=policy.SIDE_SELL)


@pytest.mark.parametrize("step", ENTRY_STEPS)
def test_an_abort_still_blocks_a_sell(tmp_path, step):
    store = make_store(tmp_path)
    raise_event(store, "audit_corrupt", "chain break")
    state = run_state(store)
    with pytest.raises(RunAborted):
        state.require_entry(step, ISIN, side=policy.SIDE_SELL)
    assert state.blocks(step, isin=ISIN, side=policy.SIDE_SELL)


def test_an_abort_after_a_run_end_blocks_the_flatten_sell(tmp_path):
    store = make_store(tmp_path)
    end_run(store)
    raise_event(store, "unexplained_account_position", "ticker not in the run universe")
    state = run_state(store)
    for step in ENTRY_STEPS:
        with pytest.raises(RunAborted):
            state.require_entry(step, ISIN, side=policy.SIDE_SELL)


def test_a_sell_leaves_booking_expiry_and_reads_alone_and_the_side_changes_nothing_for_them(tmp_path):
    store = make_store(tmp_path)
    end_run(store)
    raise_event(store, policy.DRAWDOWN_HALT, "equity_at_or_below_minus_8_percent_of_peak")
    raise_event(store, "missing_bar", "no bar", isin=ISIN)
    state = run_state(store)
    for step in (policy.STEP_BOOK, policy.STEP_EXPIRE, policy.STEP_READ_ONLY):
        for side in (policy.SIDE_BUY, policy.SIDE_SELL):
            assert not state.blocks(step, side=side)
