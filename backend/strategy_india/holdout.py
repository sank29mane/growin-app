"""Sealed holdout (D-12) and the D-19 pass criteria (PROPOSED, awaiting operator confirmation).

* ``HoldoutRange`` is the most recent ~250 sessions of the 59 window. Its range
  and hash are registered before development.
* ``D19_TEMPLATE`` holds the PROPOSED criteria as data. A registration seals a
  copy and its ``holdout_criteria_sha256``. Changing a number later is a
  one-line edit of the template plus a new registration. No verdict logic
  carries a literal threshold.
* ``open_holdout`` is the only way to obtain a ``HoldoutGrant``. It refuses a
  missing or changed criteria set, a pinned-head mismatch and a second open,
  and it logs the open event in the registry before any holdout data is read.
* ``evaluate_verdict`` returns PASS, FAIL or INCONCLUSIVE. INCONCLUSIVE counts
  as not passed and spends the holdout like any other outcome.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from .errors import HoldoutSpent, HoldoutViolation, RegistryError, StrategyIndiaError
from .registry import Entry, Registry, criteria_hash, sha256_text, canonical_json, spent_ranges

PASS = "PASS"
FAIL = "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"

HOLDOUT_SESSIONS = 250

# PROPOSED (D-19). The operator has not confirmed these values. Edit a value here,
# then register again: the sealed copy in every earlier registration stays as it was.
D19_TEMPLATE: dict[str, Any] = {
    "version": "d19-proposed-1",
    "status": "PROPOSED",
    "gate_scenario": "phase62_gate",
    "gate_k_ticks": 3,
    "benchmark": "liquid_etf_buy_and_hold_one_round_trip",
    "require_net_return_above_benchmark": True,
    "max_drawdown_floor": "-0.15",
    "flatten_event_fails": True,
    "max_annualised_swaps": "65",
    "annualisation_sessions": 250,
    "exclude_first_build": True,
    "missing_evidence": "inconclusive",
    "dividend_sensitivity_factor": "0.98",
    "dividend_sensitivity_flip": "inconclusive",
    "one_shot": True,
}


def default_criteria() -> dict[str, Any]:
    """A fresh copy of the PROPOSED D-19 template."""
    return copy.deepcopy(D19_TEMPLATE)


def criteria_sha256(criteria: Mapping[str, Any]) -> str:
    return criteria_hash(criteria)


@dataclass(frozen=True)
class HoldoutRange:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise HoldoutViolation("holdout range ends before it starts")

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end

    def overlaps(self, other: "HoldoutRange") -> bool:
        return self.start <= other.end and other.start <= self.end

    def as_payload(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}

    @classmethod
    def from_payload(cls, raw: Mapping[str, Any]) -> "HoldoutRange":
        return cls(date.fromisoformat(raw["start"]), date.fromisoformat(raw["end"]))


def holdout_range_for(sessions: Sequence[date], count: int = HOLDOUT_SESSIONS) -> HoldoutRange:
    """The most recent ``count`` sessions of the window (D-12)."""
    ordered = sorted(set(sessions))
    if len(ordered) <= count:
        raise HoldoutViolation("the window is not longer than the holdout; no development window remains")
    return HoldoutRange(ordered[-count], ordered[-1])


def holdout_digest(dataset_sha256: str, holdout: HoldoutRange, sessions: Sequence[date]) -> str:
    """The registered holdout hash: dataset hash, range and the exact session list inside it."""
    inside = [day.isoformat() for day in sorted(set(sessions)) if holdout.contains(day)]
    return sha256_text(canonical_json({"dataset_sha256": dataset_sha256, "range": holdout.as_payload(), "sessions": inside}))


# ---- one-shot open -----------------------------------------------------------
@dataclass(frozen=True)
class HoldoutGrant:
    """Proof that the holdout was opened and logged. Only ``open_holdout`` builds one."""

    registration_hash: str
    event_hash: str
    holdout: HoldoutRange
    criteria_sha256: str
    _issued_by: object = field(repr=False, compare=False, default=None)


_ISSUER = object()


def is_grant(candidate: object) -> bool:
    return isinstance(candidate, HoldoutGrant) and candidate._issued_by is _ISSUER


def open_holdout(
    registry: Registry,
    *,
    criteria: Mapping[str, Any] | None,
    expected_head: str,
    registration_hash: str | None = None,
    logged_at: str | None = None,
) -> HoldoutGrant:
    """Open the holdout once. The event is logged in the registry before the grant is returned."""
    if not criteria:
        raise RegistryError("D-19 holdout criteria are absent; the holdout stays sealed")
    entries = registry.entries(expected_head=expected_head)
    registrations = [entry for entry in entries if entry.kind == "registration"]
    if not registrations:
        raise RegistryError("no registration record exists; the holdout stays sealed")
    reg: Entry | None = registrations[-1] if registration_hash is None else next(
        (entry for entry in registrations if entry.entry_hash == registration_hash), None
    )
    if reg is None:
        raise RegistryError("no registration with that entry hash")
    if criteria_hash(criteria) != reg.payload["holdout_criteria_sha256"]:
        raise RegistryError("D-19 criteria differ from the sealed criteria; the holdout stays sealed")
    holdout = HoldoutRange.from_payload(reg.payload["holdout_range"])
    opened = [entry for entry in entries if entry.kind == "holdout_open"]
    for event in opened:
        if event.payload["registration_entry_hash"] == reg.entry_hash:
            raise HoldoutSpent("this registration's holdout was already opened; it is one shot")
    for spent in spent_ranges(opened):
        if holdout.overlaps(HoldoutRange(*spent)):
            raise HoldoutSpent("the holdout overlaps a spent holdout")
    payload = {
        "registration_entry_hash": reg.entry_hash,
        "holdout_range": holdout.as_payload(),
        "holdout_sha256": reg.payload["holdout_sha256"],
        "criteria_sha256": reg.payload["holdout_criteria_sha256"],
        "logged_at": logged_at,
    }
    event = registry.append_holdout_open(payload)
    return HoldoutGrant(reg.entry_hash, event.entry_hash, holdout, payload["criteria_sha256"], _ISSUER)


# ---- verdict -----------------------------------------------------------------
@dataclass(frozen=True)
class HoldoutEvidence:
    """What a holdout evaluation measured under one dividend assumption. All Decimal."""

    net_return: Decimal
    benchmark_net_return: Decimal
    max_drawdown: Decimal
    flatten_events: int
    swaps: int  # filled position exits, the first portfolio build is not a swap
    holdout_sessions: int
    no_assumed_fill_attempts: int


@dataclass(frozen=True)
class HoldoutVerdict:
    verdict: str
    criteria_sha256: str
    breaches: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    sensitivity_flips: tuple[str, ...]
    annualised_swaps: Decimal
    passed: bool


def annualised_swaps(swaps: int, sessions: int, annualisation_sessions: int) -> Decimal:
    if sessions <= 0:
        raise StrategyIndiaError("holdout sessions must be positive")
    return Decimal(swaps) * Decimal(annualisation_sessions) / Decimal(sessions)


def _breaches(criteria: Mapping[str, Any], ev: HoldoutEvidence) -> list[str]:
    out: list[str] = []
    if criteria["require_net_return_above_benchmark"] and not ev.net_return > ev.benchmark_net_return:
        out.append("net_return_not_above_benchmark")
    if not ev.max_drawdown > Decimal(criteria["max_drawdown_floor"]):
        out.append("max_drawdown_at_or_below_floor")
    if criteria["flatten_event_fails"] and ev.flatten_events > 0:
        out.append("flatten_event")
    swaps = annualised_swaps(ev.swaps, ev.holdout_sessions, criteria["annualisation_sessions"])
    if swaps > Decimal(criteria["max_annualised_swaps"]):
        out.append("annualised_swaps_above_budget")
    return out


def check_supported(criteria: Mapping[str, Any]) -> None:
    """The verdict logic implements one policy for missing evidence and flips. Refuse anything else."""
    for key in ("missing_evidence", "dividend_sensitivity_flip"):
        if criteria.get(key) != "inconclusive":
            raise StrategyIndiaError(f"criteria {key} must be 'inconclusive'; no other policy is implemented")
    if criteria.get("one_shot") is not True:
        raise StrategyIndiaError("criteria one_shot must be true")


def check_gate_scenario(criteria: Mapping[str, Any], *, k_ticks: int, phase62_gate: bool) -> None:
    """The scenario the verdict is judged on must be 60's gate scenario at the sealed k."""
    if not phase62_gate or k_ticks != criteria["gate_k_ticks"]:
        raise StrategyIndiaError("the holdout must be judged on the phase62 gate scenario at the sealed k_ticks")


def evaluate_verdict(
    criteria: Mapping[str, Any],
    base: HoldoutEvidence,
    sensitivity: HoldoutEvidence | None = None,
) -> HoldoutVerdict:
    """PASS only when every criterion holds and no evidence is missing. FAIL outranks INCONCLUSIVE."""
    check_supported(criteria)
    breaches = _breaches(criteria, base)
    missing: list[str] = []
    if base.no_assumed_fill_attempts > 0:
        missing.append("no_assumed_fill_attempt")
    flips: list[str] = []
    if sensitivity is not None:
        if sensitivity.no_assumed_fill_attempts > 0 and "no_assumed_fill_attempt" not in missing:
            missing.append("no_assumed_fill_attempt")
        sens = _breaches(criteria, sensitivity)
        flips = [name for name in sens if name not in breaches]
    swaps = annualised_swaps(base.swaps, base.holdout_sessions, criteria["annualisation_sessions"])
    if breaches:
        verdict = FAIL
    elif missing or flips:
        verdict = INCONCLUSIVE
    else:
        verdict = PASS
    return HoldoutVerdict(
        verdict=verdict,
        criteria_sha256=criteria_hash(criteria),
        breaches=tuple(breaches),
        missing_evidence=tuple(missing),
        sensitivity_flips=tuple(flips),
        annualised_swaps=swaps,
        passed=verdict == PASS,
    )
