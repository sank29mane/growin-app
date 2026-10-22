"""Pause and abort policy for the shadow run (64-03 Task 1, D-07, D-08, P-06, T-64-16).

Every event that can stop a run has exactly one row here: a scoped pause or an abort. There is no default
row, so an event nobody classified raises instead of falling through to a permissive answer.

State is derived only from the evidence records, never from memory, a timer or a clock, so a restart cannot
clear anything:

- an abort is terminal. It blocks every later step, including read-only ones, and ``resume`` refuses it;
- a pause blocks by scope and is cleared only by ``resume`` with a non-empty reason;
- ``resume`` clears pauses and nothing else.

Scopes: ``all`` blocks every step except read-only checks; ``entries`` blocks admission and mint; ``isin:<ISIN>``
blocks admission and mint for that instrument. A pause from the P-06 list (r3 ruling e) uses ``all`` so no
later admission, mint, booking or expiry runs in the invocation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .evidence import EvidenceStore

PAUSE = "pause"
ABORT = "abort"

SCOPE_ALL = "all"
SCOPE_ENTRIES = "entries"
SCOPE_ISIN = "isin"  # the record carries ``isin:<ISIN>``

STEP_ADMIT = "admit"
STEP_MINT = "mint"
STEP_BOOK = "book"
STEP_EXPIRE = "expire"
STEP_READ_ONLY = "read_only"
STEPS = frozenset({STEP_ADMIT, STEP_MINT, STEP_BOOK, STEP_EXPIRE, STEP_READ_ONLY})

_ENTRY_STEPS = frozenset({STEP_ADMIT, STEP_MINT})
_ALL_BLOCKED = frozenset({STEP_ADMIT, STEP_MINT, STEP_BOOK, STEP_EXPIRE})
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


class PolicyError(Exception):
    """A policy refusal. ``code`` is stable and carries no value."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RunAborted(PolicyError):
    """The run holds an abort. Nothing later may run."""


class RunPaused(PolicyError):
    """An open pause blocks this step."""


@dataclass(frozen=True)
class Rule:
    action: str  # PAUSE or ABORT
    scope: str  # SCOPE_ALL, SCOPE_ENTRIES or SCOPE_ISIN; aborts end the whole run


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
    "unresolved_corporate_action": _pause(SCOPE_ISIN),
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
    # D-08: the -8% trailing drawdown halts entries; only `resume` with a reason releases it.
    "drawdown_halt": _pause(SCOPE_ENTRIES),
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

    @property
    def aborted(self) -> bool:
        return bool(self.abort_events)

    def blocks(self, step: str, *, isin: str | None = None) -> bool:
        if step not in STEPS:
            raise PolicyError("unknown_step")
        if self.aborted:
            return True
        for pause in self.open_pauses:
            if pause.scope == SCOPE_ALL and step in _ALL_BLOCKED:
                return True
            if pause.scope == SCOPE_ENTRIES and step in _ENTRY_STEPS:
                return True
            if pause.scope == f"{SCOPE_ISIN}:{isin}" and step in _ENTRY_STEPS:
                return True
        return False

    def require(self, step: str, *, isin: str | None = None) -> None:
        """Raise unless ``step`` may run. Call this before every admission, mint, booking and expiry."""

        if not self.blocks(step, isin=isin):
            return
        if self.aborted:
            raise RunAborted("run_aborted")
        raise RunPaused("run_paused")


def state_from_records(records: Sequence[Mapping[str, Any]]) -> RunState:
    aborts: list[str] = []
    pauses: dict[int, OpenPause] = {}
    for record in records:
        kind = record.get("kind")
        if kind == ABORT:
            aborts.append(str(record["event"]))
        elif kind == PAUSE:
            pauses[int(record["seq"])] = OpenPause(int(record["seq"]), str(record["event"]), str(record["scope"]))
        elif kind == "resume":
            for seq in record["cleared_pause_seqs"]:
                pauses.pop(int(seq), None)
    return RunState(tuple(aborts), tuple(pauses.values()))


def run_state(store: EvidenceStore) -> RunState:
    return state_from_records(store.records())


def raise_event(store: EvidenceStore, event: str, reason: str, *, isin: str | None = None) -> Mapping[str, Any]:
    """Record the pause or abort the table assigns to ``event`` and return the stored record."""

    rule = rule_for(event)
    if rule.action == ABORT:
        return store.append(ABORT, {"event": event, "reason": reason})
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
    """

    state = run_state(store)
    if state.aborted:
        raise PolicyError("resume_refused_abort")
    if not isinstance(reason, str) or not reason.strip():
        raise PolicyError("reason_required")
    open_seqs = {pause.seq for pause in state.open_pauses}
    if not open_seqs:
        raise PolicyError("nothing_to_resume")
    chosen = sorted(open_seqs) if pause_seqs is None else sorted(set(pause_seqs))
    if not chosen or not set(chosen) <= open_seqs:
        raise PolicyError("unknown_pause")
    return store.append("resume", {"reason": reason.strip(), "cleared_pause_seqs": chosen})


__all__ = [
    "ABORT",
    "PAUSE",
    "POLICY",
    "OpenPause",
    "PolicyError",
    "Rule",
    "RunAborted",
    "RunPaused",
    "RunState",
    "STEP_ADMIT",
    "STEP_BOOK",
    "STEP_EXPIRE",
    "STEP_MINT",
    "STEP_READ_ONLY",
    "raise_event",
    "resume",
    "rule_for",
    "run_state",
    "state_from_records",
]
