"""Append-only, hash-chained registration record (D-10, D-11, D-12, D-19).

Tracked code holds only the schema and the verifier. The registry takes every
hash as a plain input and imports nothing from ``pilot_data`` or ``costs``: it
carries its own canonical JSON and sha256 helpers.

File format: one JSON object per line. Each entry is
``{seq, kind, payload, prev_hash, entry_hash}`` where ``entry_hash`` is the
sha256 of the canonical JSON of the other four fields and ``prev_hash`` is the
previous entry's hash (64 zeros for the first). Editing or deleting an entry
anywhere but the tail breaks the chain. Truncating the tail leaves a valid
chain, so every consumer that must notice it (holdout open) also takes the
head hash pinned outside the file (D-11: path plus sha256).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .errors import HoldoutSpent, RegistryError, RegistryMismatch

GENESIS = "0" * 64
KIND_REGISTRATION = "registration"
KIND_HOLDOUT_OPEN = "holdout_open"
KIND_HOLDOUT_INVALID = "holdout_invalid"
KIND_HOLDOUT_VERDICT = "holdout_verdict"

HASH_FIELDS = (
    "params_sha256",
    "dataset_sha256",
    "coverage_report_sha256",
    "coverage_file_sha256",
    "fill_scenarios_sha256",
    "charge_schedule_sha256",
    "tick_table_sha256",
    "hurdle_map_sha256",
    "dividend_events_sha256",
    "holdout_sha256",
    "holdout_criteria_sha256",
)
SCALAR_FIELDS = (
    "hypothesis",
    "parameter_budget_n",
    "git_commit",
    "seed",
    "benchmark_ids",
    "charge_schedule_version",
    "fold_rules",
    "holdout_range",
    "holdout_criteria",
    "spent_holdout_event_hashes",
)
REGISTRATION_FIELDS = HASH_FIELDS + SCALAR_FIELDS
# Every field compared against the live inputs. The criteria body is covered by its own hash.
LIVE_CHECKED_FIELDS = tuple(name for name in REGISTRATION_FIELDS if name not in ("hypothesis", "holdout_criteria", "spent_holdout_event_hashes"))

_SHA = re.compile(r"[0-9a-f]{64}", re.ASCII)
_GIT = re.compile(r"[0-9a-f]{40}", re.ASCII)


def canonical_json(obj: Any) -> str:
    _reject_inexact(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _reject_inexact(obj: Any) -> None:
    if isinstance(obj, float):
        raise RegistryError("registry records hold no floats; write numerics as strings")
    if isinstance(obj, Mapping):
        for key, item in obj.items():
            if not isinstance(key, str):
                raise RegistryError("registry mapping keys must be strings")
            _reject_inexact(item)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _reject_inexact(item)
    elif obj is not None and not isinstance(obj, (str, int, bool)):
        raise RegistryError(f"{type(obj).__name__} is not a registry value")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_sha256(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def criteria_hash(criteria: Mapping[str, Any]) -> str:
    """The ``holdout_criteria_sha256`` of a D-19 criteria mapping."""
    return canonical_sha256(dict(criteria))


def durable_jsonl(path: Path) -> bytes:
    """Read the newline-committed prefix, retaining records before a torn append."""
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        logging.getLogger(__name__).warning("Ignoring incomplete final line in %s", path)
        data = data[:data.rfind(b"\n") + 1]
    return data


def trim_incomplete_tail(fd: int, path: Path) -> None:
    """Discard only an uncommitted tail before the next append, under single-writer ownership."""
    size = len(durable_jsonl(path))
    if os.fstat(fd).st_size != size:
        os.ftruncate(fd, size)
        os.fsync(fd)


@dataclass(frozen=True)
class Entry:
    seq: int
    kind: str
    payload: dict[str, Any]
    prev_hash: str
    entry_hash: str


def _entry_hash(seq: int, kind: str, payload: Mapping[str, Any], prev_hash: str) -> str:
    return canonical_sha256({"seq": seq, "kind": kind, "payload": payload, "prev_hash": prev_hash})


def _range(raw: Any, field: str) -> tuple[date, date]:
    try:
        start, end = date.fromisoformat(raw["start"]), date.fromisoformat(raw["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RegistryError(f"{field} must hold ISO start and end dates") from exc
    if end < start:
        raise RegistryError(f"{field} ends before it starts")
    return start, end


def _overlap(a: tuple[date, date], b: tuple[date, date]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def validate_registration(record: Mapping[str, Any]) -> dict[str, Any]:
    """Strict shape check. Unknown or missing keys, bad hashes and bad ranges all refuse."""
    unknown = sorted(set(record) - set(REGISTRATION_FIELDS))
    missing = sorted(set(REGISTRATION_FIELDS) - set(record))
    if unknown or missing:
        raise RegistryError(f"registration keys differ from the schema (unknown {unknown}, missing {missing})")
    out = dict(record)
    for name in HASH_FIELDS:
        if not isinstance(out[name], str) or not _SHA.fullmatch(out[name]):
            raise RegistryError(f"{name} must be 64 lowercase hex characters")
    if not isinstance(out["git_commit"], str) or not _GIT.fullmatch(out["git_commit"]):
        raise RegistryError("git_commit must be 40 lowercase hex characters")
    for name in ("parameter_budget_n", "seed"):
        if isinstance(out[name], bool) or not isinstance(out[name], int):
            raise RegistryError(f"{name} must be an int")
    if out["parameter_budget_n"] < 1:
        raise RegistryError("parameter_budget_n must be at least 1")
    if not isinstance(out["hypothesis"], str) or not out["hypothesis"].strip():
        raise RegistryError("hypothesis must be a non-empty string")
    if not isinstance(out["benchmark_ids"], list) or not out["benchmark_ids"]:
        raise RegistryError("benchmark_ids must be a non-empty list")
    if not isinstance(out["charge_schedule_version"], str) or not out["charge_schedule_version"]:
        raise RegistryError("charge_schedule_version must be a non-empty string")
    if not isinstance(out["fold_rules"], dict) or not out["fold_rules"]:
        raise RegistryError("fold_rules must be a non-empty mapping")
    _range(out["holdout_range"], "holdout_range")
    criteria = out["holdout_criteria"]
    if not isinstance(criteria, dict) or not criteria:
        raise RegistryError("D-19 holdout criteria must be sealed in the registration")
    if criteria_hash(criteria) != out["holdout_criteria_sha256"]:
        raise RegistryError("holdout_criteria_sha256 does not match the sealed criteria")
    spent = out["spent_holdout_event_hashes"]
    if not isinstance(spent, list) or not all(isinstance(item, str) and _SHA.fullmatch(item) for item in spent):
        raise RegistryError("spent_holdout_event_hashes must be a list of sha256 hex strings")
    canonical_json(out)  # float and type guard
    return out


def check_live_inputs(record: Mapping[str, Any], live: Mapping[str, Any]) -> None:
    """Refuse unless every recorded value equals the live input (D-10). A missing live key is a mismatch."""
    for name in LIVE_CHECKED_FIELDS:
        if name not in live:
            raise RegistryMismatch(name, f"live input {name} was not supplied")
        if canonical_json(record[name]) != canonical_json(live[name]):
            raise RegistryMismatch(name)


class Registry:
    """One registry file. All writes append a verified, chained line."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ---- reading and verification -------------------------------------------
    def entries(self, *, expected_head: str | None = None) -> tuple[Entry, ...]:
        """Read and verify the whole chain. Raises ``RegistryError`` on any break."""
        if not self.path.exists():
            raise RegistryError("registry file is missing")
        try:
            text = durable_jsonl(self.path).decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise RegistryError("registry file is unreadable") from exc
        entries: list[Entry] = []
        prev = GENESIS
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                raise RegistryError(f"registry line {number} is blank")
            try:
                raw = json.loads(line)
                entry = Entry(raw["seq"], raw["kind"], raw["payload"], raw["prev_hash"], raw["entry_hash"])
            except (ValueError, KeyError, TypeError) as exc:
                raise RegistryError(f"registry line {number} is malformed") from exc
            if set(raw) != {"seq", "kind", "payload", "prev_hash", "entry_hash"}:
                raise RegistryError(f"registry line {number} has unexpected keys")
            if (type(entry.seq) is not int or not isinstance(entry.kind, str)
                    or not isinstance(entry.payload, dict) or not isinstance(entry.prev_hash, str)
                    or not isinstance(entry.entry_hash, str)):
                raise RegistryError(f"registry line {number} has invalid field types")
            if entry.seq != number - 1:
                raise RegistryError(f"registry line {number} breaks the sequence")
            if entry.prev_hash != prev:
                raise RegistryError(f"registry line {number} breaks the hash chain")
            if _entry_hash(entry.seq, entry.kind, entry.payload, entry.prev_hash) != entry.entry_hash:
                raise RegistryError(f"registry line {number} was edited")
            self._validate_entry(entry, entries)
            entries.append(entry)
            prev = entry.entry_hash
        if expected_head is not None and prev != expected_head:
            raise RegistryError("registry head differs from the pinned head hash (tail removed or extended)")
        return tuple(entries)

    def verify(self, *, expected_head: str | None = None) -> str:
        """Return the head hash after verifying the chain."""
        entries = self.entries(expected_head=expected_head)
        return entries[-1].entry_hash if entries else GENESIS

    def head_hash(self) -> str:
        return self.verify()

    def registrations(self) -> tuple[Entry, ...]:
        return tuple(entry for entry in self.entries() if entry.kind == KIND_REGISTRATION)

    def registration(self, entry_hash: str | None = None) -> Entry:
        found = self.registrations()
        if not found:
            raise RegistryError("no registration record exists; the engine refuses to run")
        if entry_hash is None:
            return found[-1]
        for entry in found:
            if entry.entry_hash == entry_hash:
                return entry
        raise RegistryError("no registration with that entry hash")

    def holdout_events(self) -> tuple[Entry, ...]:
        return tuple(entry for entry in self.entries() if entry.kind == KIND_HOLDOUT_OPEN)

    def holdout_results(self) -> tuple[Entry, ...]:
        """First final outcome per open; only an in-progress INVALID can be superseded."""
        results: dict[str, Entry] = {}
        for entry in self.entries():
            if entry.kind == KIND_HOLDOUT_OPEN:
                results[entry.entry_hash] = entry
            elif entry.kind in (KIND_HOLDOUT_INVALID, KIND_HOLDOUT_VERDICT):
                opened = entry.payload["holdout_open_event_hash"]
                current = results[opened]
                if current.kind == KIND_HOLDOUT_OPEN or (
                    current.kind == KIND_HOLDOUT_INVALID and current.payload["reason"] == "in_progress"
                ):
                    results[opened] = entry
        return tuple(results.values())

    @staticmethod
    def _validate_entry(entry: Entry, existing: Sequence[Entry]) -> None:
        """Check semantic links as well as the enclosing chain digest."""
        payload = entry.payload
        if entry.kind == KIND_REGISTRATION:
            validate_registration(payload)
            return
        if entry.kind not in (KIND_HOLDOUT_OPEN, KIND_HOLDOUT_INVALID, KIND_HOLDOUT_VERDICT):
            raise RegistryError("unknown registry entry kind")
        registrations = {e.entry_hash: e for e in existing if e.kind == KIND_REGISTRATION}
        registration = registrations.get(payload.get("registration_entry_hash")) if isinstance(
            payload.get("registration_entry_hash"), str) else None
        if registration is None:
            raise RegistryError("holdout record must link to an existing registration")
        if entry.kind == KIND_HOLDOUT_OPEN:
            if payload.get("holdout_range") != registration.payload["holdout_range"]:
                raise RegistryError("holdout open range differs from its registration")
            return
        opens = {e.entry_hash: e for e in existing if e.kind == KIND_HOLDOUT_OPEN}
        opened = opens.get(payload.get("holdout_open_event_hash")) if isinstance(
            payload.get("holdout_open_event_hash"), str) else None
        if opened is None or opened.payload["registration_entry_hash"] != registration.entry_hash:
            raise RegistryError("holdout outcome must link to the matching registration and open")
        if entry.kind == KIND_HOLDOUT_INVALID:
            if not isinstance(payload.get("reason"), str) or not payload["reason"]:
                raise RegistryError("holdout INVALID must have a reason")
        else:
            verdict = payload.get("verdict")
            body = payload.get("verdict_payload")
            if (verdict not in ("PASS", "FAIL", "INCONCLUSIVE") or not isinstance(body, dict)
                    or body.get("verdict") != verdict
                    or body.get("criteria_sha256") != registration.payload["holdout_criteria_sha256"]
                    or payload.get("verdict_sha256") != canonical_sha256(body)):
                raise RegistryError("holdout verdict payload or digest is invalid")

    # ---- appending -----------------------------------------------------------
    def _append(self, kind: str, payload: Mapping[str, Any]) -> Entry:
        if not isinstance(payload, Mapping):
            raise RegistryError("registry payload must be a mapping")
        existing = self.entries() if self.path.exists() else ()
        prev = existing[-1].entry_hash if existing else GENESIS
        seq = len(existing)
        body = json.loads(canonical_json(dict(payload)))
        entry = Entry(seq, kind, body, prev, _entry_hash(seq, kind, body, prev))
        self._validate_entry(entry, existing)
        line = canonical_json(
            {"seq": entry.seq, "kind": entry.kind, "payload": entry.payload, "prev_hash": entry.prev_hash,
             "entry_hash": entry.entry_hash}
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            trim_incomplete_tail(fd, self.path)
            data = memoryview((line + "\n").encode("utf-8"))
            while data:
                data = data[os.write(fd, data):]
            os.fsync(fd)
        finally:
            os.close(fd)
        return entry

    def register(self, record: Mapping[str, Any]) -> Entry:
        """Seal a registration. A holdout overlapping a spent one, or one that does not cite every spent event, is refused."""
        body = validate_registration(record)
        spent = self.holdout_events() if self.path.exists() else ()
        new_range = _range(body["holdout_range"], "holdout_range")
        for event in spent:
            if _overlap(new_range, _range(event.payload["holdout_range"], "spent holdout range")):
                raise HoldoutSpent("the new holdout overlaps a spent holdout")
        if sorted(body["spent_holdout_event_hashes"]) != sorted(event.entry_hash for event in spent):
            raise RegistryError("the registration must cite the hash of every spent holdout event")
        return self._append(KIND_REGISTRATION, body)

    def append_holdout_open(self, payload: Mapping[str, Any]) -> Entry:
        return self._append(KIND_HOLDOUT_OPEN, payload)

    def append_holdout_verdict(self, payload: Mapping[str, Any]) -> Entry:
        """Durably record PASS, FAIL or INCONCLUSIVE before exporting a report."""
        return self._append(KIND_HOLDOUT_VERDICT, payload)

    def append_holdout_invalid(self, payload: Mapping[str, Any]) -> Entry:
        """A typed INVALID verdict for an opened holdout whose evaluation failed."""
        return self._append(KIND_HOLDOUT_INVALID, payload)

    def invalid_events(self) -> tuple[Entry, ...]:
        """Effective INVALID records, excluding ones superseded by a later verdict."""
        return tuple(entry for entry in self.holdout_results() if entry.kind == KIND_HOLDOUT_INVALID)


def spent_ranges(entries: Sequence[Entry]) -> list[tuple[date, date]]:
    return [_range(entry.payload["holdout_range"], "spent holdout range") for entry in entries if entry.kind == KIND_HOLDOUT_OPEN]
