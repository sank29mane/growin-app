"""Sealed holdout (D-12) and the D-19 pass criteria (PROPOSED, awaiting operator confirmation).

* ``HoldoutRange`` is the most recent ~250 sessions of the 59 window. Its range
  and hash are registered before development.
* The D-19 criteria VALUES never live in tracked code. They are read from a private
  file by path plus sha256 (``load_criteria_file``; the expected place is
  ``private/india/holdout_criteria.json``, listed with its hash in the operator's
  config, consistent with 58). A registration seals a copy and its
  ``holdout_criteria_sha256``. Tracked code holds only ``CRITERIA_SCHEMA`` (key
  names and types) and the verdict logic, which carries no literal threshold.
  A tracked EXAMPLE file with the proposed numbers lives under
  ``tests/backend/fixtures/strategy_india/`` and is a fixture, not a default.
* ``open_holdout`` is the only way to obtain a ``HoldoutGrant``. It refuses a
  missing or changed criteria set, a pinned-head mismatch and a second open,
  and it logs the open event in the registry before any holdout data is read.
Threat model: the registry, spent-holdouts ledger and head file protect an
honest but fallible operator or agent from accidental reuse and crashes. They
do not protect against deliberate editing of several private files on the same
machine. The external anchor covers that case: after an open, the operator
records the post-open head in Phase 58 ``holdout_refs``, and the orchestrator
records its hash in the phase SUMMARY. The CLI prints this instruction.

* ``evaluate_verdict`` returns PASS, FAIL or INCONCLUSIVE. INCONCLUSIVE counts
  as not passed and spends the holdout like any other outcome.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import HoldoutSpent, HoldoutViolation, RegistryError, StrategyIndiaError
from .registry import Entry, Registry, criteria_hash, sha256_text, canonical_json, spent_ranges

PASS = "PASS"
FAIL = "FAIL"
INCONCLUSIVE = "INCONCLUSIVE"

HOLDOUT_SESSIONS = 250

# Key names and value types only. No threshold appears here.
CRITERIA_SCHEMA: dict[str, type] = {
    "version": str,
    "status": str,
    "gate_scenario": str,
    "gate_k_ticks": int,
    "benchmark": str,
    "require_net_return_above_benchmark": bool,
    "max_drawdown_floor": str,
    "flatten_event_fails": bool,
    "max_annualised_swaps": str,
    "annualisation_sessions": int,
    "exclude_first_build": bool,
    "missing_evidence": str,
    "dividend_sensitivity_factor": str,
    "dividend_sensitivity_flip": str,
    "one_shot": bool,
}
CRITERIA_FILE_NAME = "holdout_criteria.json"  # under private/india/


def parse_criteria(raw: Any) -> dict[str, Any]:
    """Strict shape check of a criteria mapping: exact keys, exact types, decimal strings parse."""
    if not isinstance(raw, dict):
        raise RegistryError("D-19 criteria must be a JSON object")
    unknown = sorted(set(raw) - set(CRITERIA_SCHEMA))
    missing = sorted(set(CRITERIA_SCHEMA) - set(raw))
    if unknown or missing:
        raise RegistryError(f"D-19 criteria keys differ from the schema (unknown {unknown}, missing {missing})")
    for key, kind in CRITERIA_SCHEMA.items():
        value = raw[key]
        if isinstance(value, bool) != (kind is bool) or not isinstance(value, kind):
            raise RegistryError(f"D-19 criteria {key} must be {kind.__name__}")
    for key in ("max_drawdown_floor", "max_annualised_swaps", "dividend_sensitivity_factor"):
        try:
            value = Decimal(raw[key])
            if not value.is_finite():
                raise InvalidOperation
        except InvalidOperation:
            raise RegistryError(f"D-19 criteria {key} is not a decimal string") from None
    if not Decimal(0) < Decimal(raw["dividend_sensitivity_factor"]) < Decimal(1):
        raise RegistryError("D-19 criteria dividend_sensitivity_factor must lie between 0 and 1")
    check_supported(raw)  # the verdict logic must be able to honour every policy value, or nothing can be sealed
    return dict(raw)


def load_criteria_file(private_dir: Path, relative: str, expected_sha256: str | None) -> dict[str, Any]:
    """Load criteria by path plus sha256 from the private directory. Absent, unhashed or changed refuses."""
    if not relative or expected_sha256 is None:
        raise RegistryError("D-19 criteria file reference (path and sha256) is absent; the holdout stays sealed")
    posix = PurePosixPath(relative)
    if posix.is_absolute() or ".." in posix.parts or "\\" in relative:
        raise RegistryError("D-19 criteria path must be relative and stay inside the private directory")
    path = Path(private_dir) / posix
    if path.is_symlink() or not path.is_file():
        raise RegistryError("D-19 criteria file is missing; the holdout stays sealed")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise RegistryError("D-19 criteria file hash differs from its reference; the holdout stays sealed")
    try:
        return parse_criteria(json.loads(raw.decode("utf-8"), parse_float=_no_float))
    except ValueError as exc:
        if isinstance(exc, StrategyIndiaError):
            raise
        raise RegistryError("D-19 criteria file is not valid JSON") from exc


def _no_float(token: str) -> Any:
    raise RegistryError(f"D-19 criteria hold no bare floats ({token}); write numerics as strings")


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
    # _append fsyncs this provisional outcome before a grant can expose any data.
    registry.append_holdout_invalid({
        "holdout_open_event_hash": event.entry_hash, "registration_entry_hash": reg.entry_hash,
        "reason": "in_progress",
    })
    return HoldoutGrant(reg.entry_hash, event.entry_hash, holdout, payload["criteria_sha256"], _ISSUER)


# ---- verdict -----------------------------------------------------------------
@dataclass(frozen=True)
class HoldoutEvidence:
    """What a holdout evaluation measured under one dividend assumption. All Decimal."""

    net_return: Decimal
    benchmark_net_return: Decimal | None  # None: the ETF benchmark is unknown (for example no tick table covers its dates)
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
    if (
        criteria["require_net_return_above_benchmark"]
        and ev.benchmark_net_return is not None
        and not ev.net_return > ev.benchmark_net_return
    ):
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
    """Refuse any criteria the verdict logic cannot honour. Called at parse, at registration and at the
    holdout pre-flight, so an unsupported value can never be discovered after the holdout is spent."""

    def bad(message: str) -> StrategyIndiaError:
        return StrategyIndiaError(f"criteria {message}", code="criteria_unsupported")

    for key in ("missing_evidence", "dividend_sensitivity_flip"):
        if criteria.get(key) != "inconclusive":
            raise bad(f"{key} must be 'inconclusive'; no other policy is implemented")
    if criteria.get("one_shot") is not True:
        raise bad("one_shot must be true")
    if criteria.get("exclude_first_build") is not True:
        raise bad("exclude_first_build must be true; the first build is excluded structurally")
    if criteria.get("gate_scenario") != "phase62_gate":
        raise bad("gate_scenario must be phase62_gate")
    if criteria.get("benchmark") != "liquid_etf_buy_and_hold_one_round_trip":
        raise bad("benchmark must be liquid_etf_buy_and_hold_one_round_trip")
    sessions = criteria.get("annualisation_sessions")
    if isinstance(sessions, bool) or not isinstance(sessions, int) or sessions <= 0:
        raise bad("annualisation_sessions must be a positive int (zero would hide every swap)")
    k = criteria.get("gate_k_ticks")
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise bad("gate_k_ticks must be an int of at least 1")
    try:
        floor, swaps = Decimal(criteria["max_drawdown_floor"]), Decimal(criteria["max_annualised_swaps"])
    except (KeyError, InvalidOperation, TypeError):
        raise bad("max_drawdown_floor and max_annualised_swaps must be decimal strings") from None
    if not floor.is_finite() or not swaps.is_finite():
        raise bad("decimal criteria must be finite")
    if not Decimal(-1) < floor < Decimal(0):
        raise bad("max_drawdown_floor must lie between -1 and 0")
    if swaps < 0:
        raise bad("max_annualised_swaps must not be negative")


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
    if criteria["require_net_return_above_benchmark"] and base.benchmark_net_return is None:
        missing.append("benchmark_unavailable")
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
