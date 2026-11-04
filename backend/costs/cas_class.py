"""CAS class types and the operator approval log for the snapshot procedure (D-34).

The intraday fill model (``intraday_fills``) needs to know whether a security was in
closing-auction scope on a session date, because its volume check band is pinned per class.
The class is read from the operator-saved NSE security-master snapshot for exactly that
session date (``pilot_data.cas_snapshots``, ``ElgbltyClsgAuctnSsn``); no agent fetches NSE.
This module holds only the pure part:

* ``CasClassRow``: one resolved class with the provenance the simulator records;
* ``CasUnresolved``: a typed verdict with exactly one sub-reason and a detail string;
* the append-only approval log ``schedules/nse_cas_procedure.json`` and its loader.

Fail closed, like the holiday table in the gateway and ``ticks.py``:

* There is no default class and no carry-forward. A symbol the snapshot cannot resolve is a
  ``CasUnresolved``; the simulator caller maps it to the INCONCLUSIVE code
  ``session_scope_unknown`` and records the sub-reason.
* A procedure version counts only when the log holds an approval entry for it and no
  revocation. The committed log ships with no entry: until the operator approves the
  procedure, nothing resolves.
* The log is hash-chained (``prev_sha256`` is the canonical sha256 of the entry before it),
  so an edited or removed earlier entry makes the whole log refuse to load.

The simulator never reads a file: it receives the resolved row or the unresolved verdict.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .core import (
    InputError,
    ScheduleError,
    canonical_json,
    canonical_value,
    load_strict_json,
    sha256_hex,
)

CAS_PROCEDURE_SCHEMA = "growin.costs.cas_procedure/1"
PROCEDURE_PATH = Path(__file__).parent / "schedules" / "nse_cas_procedure.json"
CAS = "cas"
NON_CAS = "non_cas"
CAS_CLASSES = (CAS, NON_CAS)
# The INCONCLUSIVE code a caller reports when a symbol-day has no resolved class.
SCOPE_UNKNOWN_CODE = "session_scope_unknown"

# One sub-reason per unresolved verdict (D-34 b, 64-02 Task 2b). The tuple is the precedence order.
SUB_REASONS = (
    "snapshot_missing",
    "snapshot_wrong_date",
    "snapshot_hash_mismatch",
    "procedure_unapproved",
    "series_unsupported",
    "identity_invalid",
    "identity_mismatch",
    "identity_ambiguous",
    "flag_unknown",
    "flag_conflict",
)

U24_UNVERIFIED = "unverified"
U24_VERIFIED = "verified"
U24_STATUSES = (U24_UNVERIFIED, U24_VERIFIED)

_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_FILE_KEYS = ("schema", "entries")
_APPROVAL_ENTRY_KEYS = (
    "procedure_version",
    "spec_sha256",
    "impl_sha256",
    "u24_status",
    "u24_evidence_ref",
    "operator_approval",
    "prev_sha256",
)
_REVOCATION_ENTRY_KEYS = ("revokes", "operator_approval", "prev_sha256")
_APPROVAL_KEYS = ("line", "date")


@dataclass(frozen=True)
class IciciRef:
    """The ICICI master row a Breeze stock code resolved through (D-34 e)."""

    snapshot_date: date
    snapshot_sha256: str
    row_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot_date, date) or isinstance(self.snapshot_date, datetime):
            raise InputError("icici_ref.snapshot_date must be a datetime.date")
        for name in ("snapshot_sha256", "row_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise InputError(f"icici_ref.{name} must be 64 lowercase hex characters")


@dataclass(frozen=True)
class CasClassRow:
    """One resolved class plus the provenance the simulator records (R-28, R-31)."""

    isin: str
    nse_symbol: str
    series: str
    cas_class: str
    snapshot_date: date
    snapshot_sha256: str
    index_sha256: str
    procedure_version: int
    spec_sha256: str
    impl_sha256: str
    u24_status: str
    u24_evidence_ref: str | None
    stock_code: str | None
    icici_ref: IciciRef | None
    # The implementation that ran this resolution; ``impl_sha256`` is the one the stored index was built under. Evidence
    # only: not part of equality and not in ``cas_record`` (the shared R-28 and R-31 field list).
    running_impl_sha256: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.running_impl_sha256 is not None and not (
            isinstance(self.running_impl_sha256, str) and _SHA256.fullmatch(self.running_impl_sha256)
        ):
            raise InputError("cas row running_impl_sha256 must be 64 lowercase hex characters or None")
        for name in ("isin", "nse_symbol", "series"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise InputError(f"cas row {name} must be a non-empty string")
        if self.cas_class not in CAS_CLASSES:
            raise InputError(f"cas row class must be one of {CAS_CLASSES}, got {self.cas_class!r}")
        if not isinstance(self.snapshot_date, date) or isinstance(self.snapshot_date, datetime):
            raise InputError("cas row snapshot_date must be a datetime.date")
        for name in ("snapshot_sha256", "index_sha256", "spec_sha256", "impl_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise InputError(f"cas row {name} must be 64 lowercase hex characters")
        if isinstance(self.procedure_version, bool) or not isinstance(self.procedure_version, int):
            raise InputError("cas row procedure_version must be an int")
        if self.u24_status not in U24_STATUSES:
            raise InputError(f"cas row u24_status must be one of {U24_STATUSES}")
        if self.icici_ref is not None and not isinstance(self.icici_ref, IciciRef):
            raise InputError("cas row icici_ref must be an IciciRef")


@dataclass(frozen=True)
class CasUnresolved:
    """A typed verdict: no class, exactly one sub-reason, and what was known when it stopped."""

    sub_reason: str
    detail: str
    session_date: date
    procedure_version: int
    snapshot_sha256: str | None = None
    index_sha256: str | None = None
    spec_sha256: str | None = None
    impl_sha256: str | None = None
    u24_status: str | None = None
    u24_evidence_ref: str | None = None
    stock_code: str | None = None
    icici_ref: IciciRef | None = None

    def __post_init__(self) -> None:
        if self.sub_reason not in SUB_REASONS:
            raise InputError(f"sub_reason must be one of {SUB_REASONS}, got {self.sub_reason!r}")
        if not isinstance(self.detail, str) or not self.detail.strip():
            raise InputError("an unresolved verdict needs a detail string")
        if not isinstance(self.session_date, date) or isinstance(self.session_date, datetime):
            raise InputError("session_date must be a datetime.date")
        if isinstance(self.procedure_version, bool) or not isinstance(self.procedure_version, int):
            raise InputError("procedure_version must be an int")


def cas_record(verdict: CasClassRow | CasUnresolved | None, nse_symbol: str) -> dict[str, Any]:
    """The one CAS field list of R-28, R-31 and D-34 e, as canonical primitives.

    ``cas_class`` (or ``sub_reason`` and ``detail``), ``nse_symbol`` (the attempt pin),
    ``snapshot_date``, ``snapshot_sha256``, ``index_sha256``, ``procedure_version``,
    ``spec_sha256``, ``impl_sha256``, ``u24_status``, ``u24_evidence_ref`` and ``icici_ref``.
    """
    base: dict[str, Any] = {
        "cas_class": None,
        "sub_reason": None,
        "detail": None,
        "nse_symbol": nse_symbol,
        "snapshot_date": None,
        "snapshot_sha256": None,
        "index_sha256": None,
        "procedure_version": None,
        "spec_sha256": None,
        "impl_sha256": None,
        "u24_status": None,
        "u24_evidence_ref": None,
        "icici_ref": None,
    }
    if isinstance(verdict, CasClassRow):
        base.update(
            cas_class=verdict.cas_class,
            snapshot_date=verdict.snapshot_date.isoformat(),
            snapshot_sha256=verdict.snapshot_sha256,
            index_sha256=verdict.index_sha256,
            procedure_version=verdict.procedure_version,
            spec_sha256=verdict.spec_sha256,
            impl_sha256=verdict.impl_sha256,
            u24_status=verdict.u24_status,
            u24_evidence_ref=verdict.u24_evidence_ref,
            icici_ref=canonical_value(verdict.icici_ref),
        )
    elif isinstance(verdict, CasUnresolved):
        base.update(
            sub_reason=verdict.sub_reason,
            detail=verdict.detail,
            snapshot_date=verdict.session_date.isoformat(),
            snapshot_sha256=verdict.snapshot_sha256,
            index_sha256=verdict.index_sha256,
            procedure_version=verdict.procedure_version,
            spec_sha256=verdict.spec_sha256,
            impl_sha256=verdict.impl_sha256,
            u24_status=verdict.u24_status,
            u24_evidence_ref=verdict.u24_evidence_ref,
            icici_ref=canonical_value(verdict.icici_ref),
        )
    return base


# ---- the approval log --------------------------------------------------------------------


@dataclass(frozen=True)
class ProcedureEntry:
    """One approved procedure identity (an approval entry of the log)."""

    procedure_version: int
    spec_sha256: str
    impl_sha256: str
    u24_status: str
    u24_evidence_ref: str | None
    approval_line: str
    approval_date: date
    prev_sha256: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "procedure_version": self.procedure_version,
            "spec_sha256": self.spec_sha256,
            "impl_sha256": self.impl_sha256,
            "u24_status": self.u24_status,
            "u24_evidence_ref": self.u24_evidence_ref,
            "operator_approval": {"line": self.approval_line, "date": self.approval_date.isoformat()},
            "prev_sha256": self.prev_sha256,
        }


@dataclass(frozen=True)
class ProcedureLog:
    """The validated log: raw entries in file order, approvals and the revoked versions."""

    raw_entries: tuple[dict[str, Any], ...]
    approvals: tuple[ProcedureEntry, ...]
    revoked: frozenset[int]


def entry_sha256(raw_entry: Mapping[str, Any]) -> str:
    """Canonical sha256 of one log entry; the next entry carries it as ``prev_sha256``."""
    return sha256_hex(canonical_json(raw_entry))


def _mapping(raw: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(raw, dict):
        raise ScheduleError(f"{path}: expected an object")
    return raw


def _exact(raw: Mapping[str, Any], keys: tuple[str, ...], path: str) -> None:
    for key in raw:
        if key not in keys:
            raise ScheduleError(f"{path}.{key}: unknown key")
    for key in keys:
        if key not in raw:
            raise ScheduleError(f"{path}.{key}: missing required key")


def _text(raw: Mapping[str, Any], key: str, path: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ScheduleError(f"{path}.{key}: expected a non-empty string")
    return value


def _sha(raw: Mapping[str, Any], key: str, path: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ScheduleError(f"{path}.{key}: expected 64 lowercase hex characters")
    return value


def _approval(raw: Any, path: str) -> tuple[str, date]:
    approval = _mapping(raw, path)
    _exact(approval, _APPROVAL_KEYS, path)
    stamp = _text(approval, "date", path)
    try:
        return _text(approval, "line", path), date.fromisoformat(stamp)
    except ValueError as exc:
        raise ScheduleError(f"{path}.date: not an ISO date") from exc


def parse_procedure_log(raw: Any) -> ProcedureLog:
    """Validate a parsed log. Any malformed, reordered, edited or removed earlier entry raises."""
    top = _mapping(raw, "$")
    _exact(top, _FILE_KEYS, "$")
    if top["schema"] != CAS_PROCEDURE_SCHEMA:
        raise ScheduleError(f"$.schema: expected {CAS_PROCEDURE_SCHEMA!r}")
    entries = top["entries"]
    if not isinstance(entries, list):
        raise ScheduleError("$.entries: expected a list")
    approvals: list[ProcedureEntry] = []
    revoked: set[int] = set()
    previous: str | None = None
    last_version = 0
    for n, item in enumerate(entries):
        path = f"entries[{n}]"
        entry = _mapping(item, path)
        if "revokes" in entry:
            _exact(entry, _REVOCATION_ENTRY_KEYS, path)
        else:
            _exact(entry, _APPROVAL_ENTRY_KEYS, path)
        if entry["prev_sha256"] != previous:
            raise ScheduleError(f"{path}.prev_sha256: does not match the entry before it (the log was edited)")
        _approval(entry["operator_approval"], f"{path}.operator_approval")
        if "revokes" in entry:
            target = entry["revokes"]
            if isinstance(target, bool) or not isinstance(target, int):
                raise ScheduleError(f"{path}.revokes: expected an integer version")
            if target not in {a.procedure_version for a in approvals}:
                raise ScheduleError(f"{path}.revokes: version {target} has no earlier approval entry")
            if target in revoked:
                raise ScheduleError(f"{path}.revokes: version {target} is already revoked")
            revoked.add(target)
        else:
            version = entry["procedure_version"]
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                raise ScheduleError(f"{path}.procedure_version: expected an integer of at least 1")
            if version <= last_version:
                raise ScheduleError(f"{path}.procedure_version: versions must strictly increase")
            last_version = version
            u24 = entry["u24_status"]
            evidence = entry["u24_evidence_ref"]
            if u24 not in U24_STATUSES:
                raise ScheduleError(f"{path}.u24_status: expected one of {U24_STATUSES}")
            if u24 == U24_VERIFIED:
                if not isinstance(evidence, str) or not _SHA256.fullmatch(evidence):
                    raise ScheduleError(f"{path}.u24_evidence_ref: verified needs the saved circular's sha256")
            elif evidence is not None:
                raise ScheduleError(f"{path}.u24_evidence_ref: must be null while u24_status is unverified")
            line, stamp = _approval(entry["operator_approval"], f"{path}.operator_approval")
            approvals.append(
                ProcedureEntry(
                    procedure_version=version,
                    spec_sha256=_sha(entry, "spec_sha256", path),
                    impl_sha256=_sha(entry, "impl_sha256", path),
                    u24_status=u24,
                    u24_evidence_ref=evidence,
                    approval_line=line,
                    approval_date=stamp,
                    prev_sha256=previous,
                )
            )
        previous = entry_sha256(entry)
    return ProcedureLog(tuple(dict(e) for e in entries), tuple(approvals), frozenset(revoked))


def load_procedure_log() -> ProcedureLog:
    """Read and validate ``PROCEDURE_PATH``. Nothing is cached, so a revocation takes effect at once."""
    source = Path(PROCEDURE_PATH)
    raw = load_strict_json(source.read_text(encoding="utf-8"), ScheduleError, source.name)
    return parse_procedure_log(raw)


def approved_entry(log: ProcedureLog, version: int) -> ProcedureEntry | None:
    """The approval entry of ``version`` when it exists and is not revoked, else None."""
    if isinstance(version, bool) or not isinstance(version, int):
        raise InputError("procedure_version must be an int")
    if version in log.revoked:
        return None
    for entry in log.approvals:
        if entry.procedure_version == version:
            return entry
    return None


def is_append_only_extension(base: tuple[Mapping[str, Any], ...], branch: tuple[Mapping[str, Any], ...]) -> bool:
    """True when ``base`` (the entries on main) is an unchanged prefix of ``branch``."""
    if len(branch) < len(base):
        return False
    return all(canonical_json(old) == canonical_json(new) for old, new in zip(base, branch))
