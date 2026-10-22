"""Pause and abort policy for the shadow run (64-03 Task 1, D-07, D-08, P-06, T-64-16).

Every event that can stop a run has exactly one row here: a scoped pause or an abort. There is no default
row, so an event nobody classified raises instead of falling through to a permissive answer.

State is derived only from the evidence records, never from memory, a timer or a clock, so a restart cannot
clear anything:

- an abort is terminal. It blocks every later step, including read-only ones, and ``resume`` refuses it;
- a pause blocks by scope and is cleared only by ``resume`` with a non-empty reason;
- a run end (``drawdown_flatten_15``, D-08) is terminal for buys only: a buy's admission and mint are blocked for
  good, while sell admission, sell mint, booking, expiry and read-only steps keep running so the simulated
  flatten can finish. No record clears it and ``resume`` refuses it;
- ``resume`` clears pauses and nothing else, and it refuses ``drawdown_halt``: the 63 latch in
  ``risk_india/drawdown.py`` refuses a reset while drawdown is at or below the halt line, so this file may not
  hold a weaker release. ``shadow_india.portfolio.ShadowPortfolio.release_halt`` is the latch-backed release: it
  resets the 63 latch first and only then writes the ``resume`` record for the halt pause. Parity with D-05;
- ``resume`` refuses ``unresolved_corporate_action`` too: only the ledger knows whether the split or bonus was booked, so
  ``ShadowPortfolio.release_corporate_action`` is the release. It refuses ``corporate_action_not_booked`` while the
  ledger still holds the pre-ex quantity, because resuming then would mark an unadjusted quantity at the adjusted
  close and read a false -50%.

D-08 says buys stop and sells stay, so admit and mint take a ``side`` (``buy`` or ``sell``). Scopes: ``all`` blocks
every step except read-only checks, on both sides; ``entries`` blocks the admission and mint of a BUY;
``isin:<ISIN>`` blocks the admission and mint of a BUY of that instrument. The run end blocks buys the same way.
An abort blocks both sides. Without that split the events that create an exit (the -8% halt, the -15% end, an
unresolved corporate action on a held name) would block the exit they create.

``side`` defaults to ``buy``, the stricter reading, so a caller that forgets it is refused more, never less. A
sell is never refused by an ``entries`` or ``isin:`` scope, but it still names a valid ISIN: admit and mint always
need one, on either side, so every entry-step check has the same shape, the audit line carries the instrument, and a
future instrument-scoped sell rule cannot be skipped by omitting it. Booking and expiry take no side: they never
open exposure. A pause from the P-06 list (r3 ruling e) uses ``all`` so no later admission, mint, booking or expiry
runs in the invocation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .evidence import EvidenceStore

PAUSE = "pause"
ABORT = "abort"
RUN_ENDED = "run_ended"

SCOPE_ALL = "all"
SCOPE_ENTRIES = "entries"
SCOPE_ISIN = "isin"  # the record carries ``isin:<ISIN>``

STEP_ADMIT = "admit"
STEP_MINT = "mint"
STEP_BOOK = "book"
STEP_EXPIRE = "expire"
STEP_READ_ONLY = "read_only"
STEPS = frozenset({STEP_ADMIT, STEP_MINT, STEP_BOOK, STEP_EXPIRE, STEP_READ_ONLY})

SIDE_BUY = "buy"
SIDE_SELL = "sell"
SIDES = frozenset({SIDE_BUY, SIDE_SELL})

DRAWDOWN_HALT = "drawdown_halt"
UNRESOLVED_CORPORATE_ACTION = "unresolved_corporate_action"
AMBIGUOUS_EXIT_FILL = "ambiguous_exit_fill"
DRAWDOWN_FLATTEN_15 = "drawdown_flatten_15"

_ENTRY_STEPS = frozenset({STEP_ADMIT, STEP_MINT})
_ALL_BLOCKED = frozenset({STEP_ADMIT, STEP_MINT, STEP_BOOK, STEP_EXPIRE})
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]\Z")


class PolicyError(Exception):
    """A policy refusal. ``code`` is stable and carries no value."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RunAborted(PolicyError):
    """The run holds an abort. Nothing later may run."""


class RunPaused(PolicyError):
    """An open pause blocks this step."""


class RunEnded(PolicyError):
    """The run ended at the -15% flatten (D-08). A buy is blocked for good; sells stay."""


@dataclass(frozen=True)
class Rule:
    action: str  # PAUSE, ABORT or RUN_ENDED
    scope: str  # SCOPE_ALL, SCOPE_ENTRIES or SCOPE_ISIN; aborts end the whole run, a run end blocks buys only


def _pause(scope: str) -> Rule:
    return Rule(PAUSE, scope)


_ABORT = Rule(ABORT, SCOPE_ALL)

POLICY: Mapping[str, Rule] = {
    # D-07 abort: a mutation attempt, a bypass, a leaked secret, corrupt audit or state, an unexplained position.
    "broker_mutation_attempted": _ABORT,
    "authorize_attempted": _ABORT,
    "bypass_detected": _ABORT,
    "secret_leaked": _ABORT,
    "audit_corrupt": _ABORT,
    "state_corrupt": _ABORT,
    "unexplained_account_position": _ABORT,
    "evidence_corrupt": _ABORT,
    "ledger_evidence_disagree": _ABORT,
    # P-06 abort: typed VM answers that are corruption, or proof the kill-first order did not hold.
    "vm_audit_broken": _ABORT,
    "vm_state_unreadable": _ABORT,
    "vm_state_unwritable": _ABORT,
    "vm_config_invalid": _ABORT,
    "vm_clock_inconsistent": _ABORT,
    "vm_order_code": _ABORT,
    "vm_unknown_code": _ABORT,
    "code_status_mismatch": _ABORT,
    "vm_challenge_or_success": _ABORT,
    "vm_423_no_audit_entry": _ABORT,
    "vm_audit_duplicate_entries": _ABORT,
    "vm_audit_codes_unexpected": _ABORT,
    "vm_audit_chain_break": _ABORT,
    # D-07 pause: data, identity and band problems stay scoped where the instrument is known.
    "missing_bar": _pause(SCOPE_ISIN),
    UNRESOLVED_CORPORATE_ACTION: _pause(SCOPE_ISIN),
    # A sell booked on a name with an open halve under an id that is not that halve's: what the halve still owes is unknown.
    AMBIGUOUS_EXIT_FILL: _pause(SCOPE_ISIN),
    "identity_ambiguity": _pause(SCOPE_ISIN),
    "band_ambiguity": _pause(SCOPE_ISIN),
    "stale_data": _pause(SCOPE_ALL),
    "freshness_check_failed": _pause(SCOPE_ALL),
    "session_expired": _pause(SCOPE_ALL),
    "quota_failure": _pause(SCOPE_ALL),
    "egress_failure": _pause(SCOPE_ALL),
    "contract_mismatch": _pause(SCOPE_ALL),
    "hash_mismatch": _pause(SCOPE_ALL),
    "divergence_breach": _pause(SCOPE_ENTRIES),
    # D-08: the -8% trailing drawdown halts buys (sells stay). `resume` refuses it; Task 2 adds the latch-backed release.
    DRAWDOWN_HALT: _pause(SCOPE_ENTRIES),
    # D-08: the -15% flatten ends the run. Terminal for buys; sells, booking, expiry and marks run until flat.
    DRAWDOWN_FLATTEN_15: Rule(RUN_ENDED, SCOPE_ENTRIES),
    # P-06 pause: relay codes and answers that are not typed.
    "kill_label_unreadable": _pause(SCOPE_ALL),
    "untyped_response": _pause(SCOPE_ALL),
    "relay_unauthorized": _pause(SCOPE_ALL),
    "relay_no_session": _pause(SCOPE_ALL),
    "relay_session_expired": _pause(SCOPE_ALL),
    "relay_egress_mismatch": _pause(SCOPE_ALL),
    "relay_budget_unavailable": _pause(SCOPE_ALL),
    "relay_budget_exhausted": _pause(SCOPE_ALL),
    "relay_contract_mismatch": _pause(SCOPE_ALL),
    "relay_forbidden_client": _pause(SCOPE_ALL),
}


def rule_for(event: str) -> Rule:
    """The row for ``event``. An unclassified event raises: there is no default."""

    try:
        return POLICY[event]
    except KeyError:
        raise PolicyError("unknown_event") from None


@dataclass(frozen=True)
class OpenPause:
    seq: int
    event: str
    scope: str


@dataclass(frozen=True)
class RunState:
    """What the evidence says about the run's stops. Pure: the same records give the same state."""

    abort_events: tuple[str, ...]
    open_pauses: tuple[OpenPause, ...]
    ended_events: tuple[str, ...] = ()

    @property
    def aborted(self) -> bool:
        return bool(self.abort_events)

    @property
    def ended(self) -> bool:
        return bool(self.ended_events)

    def blocks(self, step: str, *, isin: str | None = None, side: str = SIDE_BUY) -> bool:
        if step not in STEPS:
            raise PolicyError("unknown_step")
        if side not in SIDES:
            raise PolicyError("unknown_side")
        if step in _ENTRY_STEPS and (not isinstance(isin, str) or not _ISIN.match(isin)):
            raise PolicyError("isin_required")
        if self.aborted:
            return True
        buy_entry = step in _ENTRY_STEPS and side == SIDE_BUY
        if self.ended and buy_entry:
            return True
        for pause in self.open_pauses:
            if pause.scope == SCOPE_ALL and step in _ALL_BLOCKED:
                return True
            if pause.scope == SCOPE_ENTRIES and buy_entry:
                return True
            if pause.scope == f"{SCOPE_ISIN}:{isin}" and buy_entry:
                return True
        return False

    def require(self, step: str, *, isin: str | None = None, side: str = SIDE_BUY) -> None:
        """Raise unless ``step`` may run. Call this before every admission, mint, booking and expiry.

        Entry steps raise ``isin_required`` without a valid ISIN. Prefer ``require_entry`` for them. ``side`` is
        ``buy`` by default; only a buy is refused by a run end, an ``entries`` pause or an ``isin:`` pause.
        """

        if not self.blocks(step, isin=isin, side=side):
            return
        if self.aborted:
            raise RunAborted("run_aborted")
        if self.ended and step in _ENTRY_STEPS and side == SIDE_BUY:
            raise RunEnded("run_ended")
        raise RunPaused("run_paused")

    def require_entry(self, step: str, isin: str, *, side: str = SIDE_BUY) -> None:
        """``require`` for an entry step, with the instrument mandatory so a runner cannot forget it."""

        if step not in _ENTRY_STEPS:
            raise PolicyError("not_an_entry_step")
        self.require(step, isin=isin, side=side)


def state_from_records(records: Sequence[Mapping[str, Any]]) -> RunState:
    aborts: list[str] = []
    ended: list[str] = []
    pauses: dict[int, OpenPause] = {}
    for record in records:
        kind = record.get("kind")
        if kind == ABORT:
            aborts.append(str(record["event"]))
        elif kind == RUN_ENDED:
            ended.append(str(record["event"]))
        elif kind == PAUSE:
            pauses[int(record["seq"])] = OpenPause(int(record["seq"]), str(record["event"]), str(record["scope"]))
        elif kind == "resume":
            for seq in record["cleared_pause_seqs"]:
                pauses.pop(int(seq), None)
    return RunState(tuple(aborts), tuple(pauses.values()), tuple(ended))


def run_state(store: EvidenceStore) -> RunState:
    return state_from_records(store.records())


def raise_event(store: EvidenceStore, event: str, reason: str, *, isin: str | None = None) -> Mapping[str, Any]:
    """Record the pause, abort or run end the table assigns to ``event`` and return the stored record."""

    rule = rule_for(event)
    if rule.action == ABORT:
        return store.append(ABORT, {"event": event, "reason": reason})
    if rule.action == RUN_ENDED:
        return store.append(RUN_ENDED, {"event": event, "reason": reason})
    if rule.scope == SCOPE_ISIN:
        if isin is None or not _ISIN.match(isin):
            raise PolicyError("isin_required")
        scope = f"{SCOPE_ISIN}:{isin}"
    else:
        scope = rule.scope
    return store.append(PAUSE, {"event": event, "scope": scope, "reason": reason})


def resume(store: EvidenceStore, reason: str, *, pause_seqs: Sequence[int] | None = None) -> Mapping[str, Any]:
    """Clear open pauses after operator review. Refuses an abort, a blank reason and an empty request.

    No timer, restart or caller state reaches this: it reads the evidence and writes one ``resume`` record.
    A run end is never in the pause list, so no ``pause_seqs`` can name it; with nothing else open the refusal is
    ``resume_refused_run_ended`` so the terminal class is not mistaken for an empty request.
    """

    state = run_state(store)
    if state.aborted:
        raise PolicyError("resume_refused_abort")
    if not isinstance(reason, str) or not reason.strip():
        raise PolicyError("reason_required")
    open_seqs = {pause.seq for pause in state.open_pauses}
    if not open_seqs:
        raise PolicyError("resume_refused_run_ended" if state.ended else "nothing_to_resume")
    chosen = sorted(open_seqs) if pause_seqs is None else sorted(set(pause_seqs))
    if not chosen or not set(chosen) <= open_seqs:
        raise PolicyError("unknown_pause")
    if any(pause.event == DRAWDOWN_HALT for pause in state.open_pauses if pause.seq in chosen):
        raise PolicyError("drawdown_resume_requires_latch")
    if any(pause.event == UNRESOLVED_CORPORATE_ACTION for pause in state.open_pauses if pause.seq in chosen):
        raise PolicyError("corporate_action_resume_requires_portfolio")
    return store.append("resume", {"reason": reason.strip(), "cleared_pause_seqs": chosen})


__all__ = [
    "ABORT",
    "AMBIGUOUS_EXIT_FILL",
    "DRAWDOWN_FLATTEN_15",
    "DRAWDOWN_HALT",
    "PAUSE",
    "POLICY",
    "RUN_ENDED",
    "OpenPause",
    "PolicyError",
    "Rule",
    "RunAborted",
    "RunEnded",
    "RunPaused",
    "RunState",
    "STEP_ADMIT",
    "STEP_BOOK",
    "STEP_EXPIRE",
    "STEP_MINT",
    "STEP_READ_ONLY",
    "SIDES",
    "SIDE_BUY",
    "SIDE_SELL",
    "UNRESOLVED_CORPORATE_ACTION",
    "raise_event",
    "resume",
    "rule_for",
    "run_state",
    "state_from_records",
]
