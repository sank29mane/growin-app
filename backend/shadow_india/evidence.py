"""The anchored, hash-chained evidence store for one shadow run (64-03 Task 1, P-08, D-04, T-64-13/15/32).

This implements the ``EvidencePort`` that ``shadow_india.admission`` is tested against: ``append(kind,
payload)`` returns the stored record and ``records()`` returns every complete record in order.

Layout, under ``<root>/<run_id>/`` (0700), every file 0600::

    manifest.json       written last by ``create``: run id, run class, the pinned hashes, ``holdout_dated_inputs`` (D-31), its own sha256
    evidence.jsonl      one canonical JSON object per line; each carries the sha256 of the previous one
    evidence.anchor     the durable ``(seq, head sha256)``; replaced atomically, never ahead of the log
    evidence.lock       flock target for writers and readers
    abort.json          the sidecar written when corruption is found; it makes every later open refuse
    torn/               an unterminated tail line is set aside here before the next append

The anchor rules mirror the VM order audit (``gateway/vm/gateway_vm/orders/audit.py``) without importing it.
An append is: verify the whole chain, write the line, fsync the file, then atomically replace the anchor
(file and directory fsynced). Opening demands that the log holds at least ``anchor.seq`` entries, that entry
``anchor.seq`` hashes to the anchor head, that at most ONE complete entry lies beyond the anchor (the crash
window between the fsync and the anchor; it is adopted by rewriting the anchor), and that an unterminated
line exists only as the single tail line after exactly ``anchor.seq`` complete entries (a torn write; it is
copied to ``torn/`` and only then cut from the log). Everything else is corruption: the abort is recorded in
the sidecar, nothing is truncated or rewritten, and every write is refused.

A hash chain and an anchor on the same disk cannot see an attacker who rewrites both. The external witness
covers that: each session close prints ``witness run=<id> seq=<n> head=<sha256>``, the operator pastes it into
the run's witness log, and ``verify_witness`` refuses a store whose chain does not contain every pasted head.

Nothing here reads a secret. Records carry typed fields only: a key or value that looks like a token, a login
URL, an account id or an IP address is refused before anything is written.
"""

from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

GENESIS = "0" * 64
MANIFEST_SCHEMA = "shadow-india-manifest/1"
RUN_CLASSES = frozenset({"plumbing", "qualifying"})
IST = timezone(timedelta(hours=5, minutes=30))

DIR_MODE = 0o700
FILE_MODE = 0o600

LOG_NAME = "evidence.jsonl"
ANCHOR_NAME = "evidence.anchor"
LOCK_NAME = "evidence.lock"
MANIFEST_NAME = "manifest.json"
SIDECAR_NAME = "abort.json"
# Fallback abort marker: when the sidecar cannot be written (ENOSPC, a failing disk), the anchor is renamed to
# ``<prefix><code>``. A rename needs no new blocks, keeps every byte, and a store without its anchor refuses to open.
ABORT_MARKER_PREFIX = ANCHOR_NAME + ".aborted."
TORN_DIR = "torn"

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_HEX64 = re.compile(r"^[0-9a-f]{64}\Z")
_ENVELOPE = ("seq", "prev_sha256", "entry_sha256", "kind", "run_id", "session_id", "run_class", "utc", "ist")
_RESERVED = frozenset(_ENVELOPE) - {"session_id"}

Clock = Callable[[], datetime]

# The abort latch: run directory -> first abort code. Set BEFORE any abort I/O and never cleared, so a failed
# sidecar write cannot leave a live store (or a second store in this process) accepting appends.
_LATCHED: dict[str, str] = {}


# --- errors --------------------------------------------------------------------------------------


class EvidenceError(Exception):
    """Base of every evidence refusal. ``code`` is stable and never carries a value or a path."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class EvidenceCorrupt(EvidenceError):
    """The store does not verify. The sidecar is written, every byte kept, every write refused."""


class EvidenceRefused(EvidenceError):
    """A record, a location or a permission was refused. Nothing was written."""


class EvidenceAborted(EvidenceError):
    """The run holds an abort record: only ``abort`` and ``session_close`` may still be appended."""


class WitnessMismatch(EvidenceError):
    """The chain does not contain a head the operator pasted into the witness log."""


# --- record schemas (D-04) -------------------------------------------------------------------------

# Field types: id, code, text, symbol, isin, series, date, iso, dec, sha, int, bool, codes, ints.
_HASHES = {
    name: "sha"
    for name in (
        "strategy_sha256", "params_sha256", "dataset_sha256", "universe_sha256", "band_sha256",
        "tick_sha256", "limits_sha256", "cost_sha256", "scenario_sha256",
    )
}

SCHEMAS: dict[str, tuple[dict[str, str], dict[str, str]]] = {
    # kind: (required fields, optional fields). ``session_id`` defaults to the store's.
    "signal": (
        {"signal_id": "id", "symbol": "symbol", "isin": "isin", "series": "series", "as_of_date": "date", **_HASHES},
        {"side": "code", "quantity": "dec", "limit_price": "dec", "reason": "text",
         "adjusted_inputs_sha256": "sha", "raw_inputs_sha256": "sha", "freshness_seconds": "int"},
    ),
    "non_evaluation_day": ({"as_of_date": "date", "reason": "code"}, {"cadence_state": "text"}),
    "intent": (
        {"intent_id": "id", "signal_id": "id", "leg": "id", "intent_sha256": "sha", "symbol": "symbol",
         "side": "code", "quantity": "dec", "limit_price": "dec"},
        {"isin": "isin", "series": "series", "reference_quote": "dec", "reason": "text"},
    ),
    "admission": (
        {"intent_id": "id", "decision": "code", "reason_code": "code"},
        {"risk_decision": "code", "refusal_codes": "codes"},
    ),
    "denied": ({"intent_id": "id", "code": "code"}, {}),
    "reservation_refused": ({"intent_id": "id", "code": "code"}, {}),
    "reservation": ({"intent_id": "id", "reservation_created_at": "text"}, {"reserved_notional": "dec"}),
    "mint_started": ({"intent_id": "id", "at": "iso"}, {}),
    "vm_decision": (
        {"intent_id": "id", "audit_seq": "int", "entry_hash": "sha", "intent_sha256": "sha",
         "codes": "codes", "recovered_from_audit": "bool"},
        {"status": "int", "code": "code", "kill": "code", "vm_portfolio_checks": "code"},
    ),
    "vm_ambiguous": ({"intent_id": "id"}, {}),
    "vm_late_entry": ({"intent_id": "id", "audit_seq": "int", "entry_hash": "sha"}, {}),
    "prep_abandoned": ({"intent_id": "id"}, {}),
    "attempt": ({"intent_id": "id", "attempt_id": "id", "outcome": "code"}, {"reason": "code"}),
    "attempt_inconclusive": ({"intent_id": "id", "code": "code"}, {}),
    "session_excluded": ({"code": "code", "excluded_from_qualification": "bool"}, {}),
    "booking_intent": (
        {"intent_id": "id", "booking_id": "id", "cumulative_quantity": "dec", "price": "dec", "charges": "dec",
         "schedule_sha256": "sha", "scenario_set_sha256": "sha", "attempt_id": "id"},
        {},
    ),
    "booking_done": ({"intent_id": "id", "booking_id": "id", "ledger_event_id": "int"}, {}),
    "booking_refused": ({"intent_id": "id", "booking_id": "id", "code": "code"}, {}),
    "fill_result": (
        {"intent_id": "id", "attempt_id": "id", "outcome": "code"},
        {"filled_quantity": "dec", "price": "dec", "charges": "dec", "scenario": "code", "reason": "code"},
    ),
    "expiry": ({"intent_id": "id", "reason": "code"}, {"released_quantity": "dec"}),
    "mark": (
        {"as_of_date": "date", "equity": "dec", "peak": "dec", "drawdown": "dec", "cash": "dec",
         "held_capital": "dec", "exposure": "dec"},
        {"symbol": "symbol", "close": "dec", "peak_source": "code", "simulator_drawdown_pct": "dec",
         "simulator_drawdown_basis": "code"},
    ),
    "position_mark": (
        {"as_of_date": "date", "isin": "isin", "symbol": "symbol", "quantity": "dec", "close": "dec", "cost": "dec",
         "mark_basis": "code"},
        {"corporate_action": "code", "carried_quantity": "dec", "carried_value": "dec"},
    ),
    "evaluation_inputs": ({"as_of_date": "date", "position_mark_seqs": "ints"}, {}),
    "evaluation_commit": ({"as_of_date": "date", "position_mark_seqs": "ints"}, {"equity": "dec"}),
    "stop_open_report": ({"as_of_date": "date", "isins": "codes", "buys_blocked": "bool"}, {}),
    "run_ended": ({"event": "code", "reason": "text"}, {}),
    "latch": ({"latch": "code", "scope": "code", "reason": "code"}, {}),
    "pause": ({"event": "code", "scope": "code", "reason": "text"}, {}),
    "abort": ({"event": "code", "reason": "text"}, {}),
    "resume": ({"reason": "text", "cleared_pause_seqs": "ints"}, {}),
    "session_close": ({"outcome": "code"}, {"record_count": "int"}),
    # 64-03 Task 3 (runner): the cadence anchor and the VM audit base a restart must not move, and the VM-side
    # identity of an intent (the VM derives its own id and hash from the O3 body, so the audit cannot be matched
    # to the ledger's intent without this record).
    "run_start": (
        {"run_start_session": "date", "audit_base_seq": "int", "audit_base_sha256": "sha", "window_start": "date"}, {}
    ),
    "vm_intent": ({"intent_id": "id", "vm_intent_id": "id", "vm_intent_sha256": "sha"}, {}),
}
RECORD_KINDS = frozenset(SCHEMAS)
# After an abort only these may still be written: a further cause, and the close that prints the witness.
ALLOWED_AFTER_ABORT = frozenset({"abort", "session_close"})

_ID = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}\Z")
_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}\Z")
_TEXT = re.compile(r"^[\x20-\x7e]{1,500}\Z")
_SYMBOL = re.compile(r"^[A-Za-z0-9&._-]{1,32}\Z")
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]\Z")
_SERIES = re.compile(r"^[A-Z0-9]{1,4}\Z")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
_DEC = re.compile(r"^-?\d+(\.\d+)?\Z")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_type(kind: str, value: Any) -> bool:
    if kind == "int":
        return _is_int(value)
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "codes":
        return isinstance(value, list) and all(isinstance(v, str) and _CODE.match(v) for v in value)
    if kind == "ints":
        return isinstance(value, list) and all(_is_int(v) for v in value)
    if not isinstance(value, str):
        return False
    if kind == "id":
        return bool(_ID.match(value))
    if kind == "code":
        return bool(_CODE.match(value))
    if kind == "text":
        return bool(_TEXT.match(value)) and bool(value.strip())
    if kind == "symbol":
        return bool(_SYMBOL.match(value))
    if kind == "isin":
        return bool(_ISIN.match(value))
    if kind == "series":
        return bool(_SERIES.match(value))
    if kind == "sha":
        return bool(_HEX64.match(value))
    if kind == "date":
        try:
            return bool(_DATE.match(value)) and datetime.strptime(value, "%Y-%m-%d") is not None
        except ValueError:
            return False
    if kind == "iso":
        try:
            return datetime.fromisoformat(value).tzinfo is not None
        except ValueError:
            return False
    if kind == "dec":
        try:
            return bool(_DEC.match(value)) and Decimal(value).is_finite()
        except InvalidOperation:
            return False
    return False


# --- canary rows (T-64-15): nothing that could be a secret reaches the file ------------------------

_FORBIDDEN_KEY = re.compile(
    r"(token|secret|passw|authorization|bearer|api[_-]?key|credential|cookie|login|callback|"
    r"(^|[_-])(account|ip|ip_address|client_ip|egress_ip)($|[_-]))",
    re.IGNORECASE,
)
_URL = re.compile(r"([a-z][a-z0-9+.-]*://|\bwww\.)", re.IGNORECASE)
_BEARER = re.compile(r"\bbearer\s+\S", re.IGNORECASE)
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.")
_IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
_IPV6 = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Fa-f:])")


def _looks_like_ip(text: str) -> bool:
    for pattern in (_IPV4, _IPV6):
        for candidate in pattern.findall(text):
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                continue
            return True
    return False


def _walk(value: Any) -> Iterable[tuple[bool, Any]]:
    """Yield ``(is_key, item)`` for every key and every scalar inside ``value``."""

    if isinstance(value, Mapping):
        for key, inner in value.items():
            yield True, key
            yield from _walk(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from _walk(inner)
    else:
        yield False, value


def _scan_for_secrets(payload: Mapping[str, Any], forbidden: Sequence[str]) -> None:
    folded = tuple(item.casefold() for item in forbidden if item)
    for is_key, item in _walk(payload):
        if isinstance(item, float):
            raise EvidenceRefused("float_refused")
        if not isinstance(item, str):
            if is_key:
                raise EvidenceRefused("bad_key")
            continue
        if is_key and _FORBIDDEN_KEY.search(item):
            raise EvidenceRefused("secret_key_refused")
        low = item.casefold()
        if any(literal in low for literal in folded):
            raise EvidenceRefused("canary_refused")
        if _URL.search(item):
            raise EvidenceRefused("url_refused")
        if _BEARER.search(item) or _JWT.search(item):
            raise EvidenceRefused("token_refused")
        if _looks_like_ip(item):
            raise EvidenceRefused("ip_refused")


def validate_payload(kind: str, payload: Mapping[str, Any], *, forbidden: Sequence[str] = ()) -> dict[str, Any]:
    """Return the checked body (without ``session_id``) or raise EvidenceRefused. No side effects."""

    if kind not in SCHEMAS:
        raise EvidenceRefused("unknown_kind")
    if not isinstance(payload, Mapping) or not all(isinstance(key, str) for key in payload):
        raise EvidenceRefused("payload_not_a_mapping")
    _scan_for_secrets(payload, forbidden)
    required, optional = SCHEMAS[kind]
    body = {key: value for key, value in payload.items() if key != "session_id"}
    if any(key in _RESERVED or key == "kind" for key in body):
        raise EvidenceRefused("reserved_field")
    if "session_id" in payload and not _check_type("id", payload["session_id"]):
        raise EvidenceRefused("bad_field")
    allowed = {**required, **optional}
    if set(body) - set(allowed):
        raise EvidenceRefused("unknown_field")
    for name in required:
        if name not in body:
            raise EvidenceRefused("missing_field")
    for name, value in body.items():
        if not _check_type(allowed[name], value):
            raise EvidenceRefused("bad_field")
    return body


# --- canonical form ------------------------------------------------------------------------------


def _canonical(body: Mapping[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _entry_hash(entry: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in entry.items() if k != "entry_sha256"})).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short evidence write")
        view = view[written:]


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new_file(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_file(path: Path, data: bytes) -> None:
    """Atomic replace: temp file written and fsynced, renamed over ``path``, then the directory fsynced."""

    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)
    _fsync_dir(path.parent)


# --- the run directory ---------------------------------------------------------------------------


def default_root() -> Path:
    """Where production evidence lives (mirrors ``backend/venue_registry.py``). Never used implicitly."""

    return Path.home() / "Library" / "Application Support" / "Growin" / "workspaces" / "india-shadow"


def _refuse_unsafe_root(root: Path) -> Path:
    resolved = root.expanduser().resolve()
    repo = Path(__file__).resolve().parents[2]
    if resolved == repo or repo in resolved.parents:  # ``private/`` lives inside the repo checkout
        raise EvidenceRefused("evidence_root_inside_repo_or_private")
    return resolved


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    run_class: str
    created_utc: str
    created_ist: str
    hashes: Mapping[str, str]
    manifest_sha256: str
    holdout_dated_inputs: bool = False  # D-31: the run reads inputs that may be dated inside the sealed 62 holdout

    def mismatches(self, current: Mapping[str, str]) -> tuple[str, ...]:
        """Names whose current hash differs from the manifest, or that one side lacks. Sorted."""

        names = set(self.hashes) | set(current)
        return tuple(sorted(name for name in names if self.hashes.get(name) != current.get(name)))


def _manifest_body(
    run_id: str, run_class: str, created: datetime, hashes: Mapping[str, str], holdout_dated_inputs: bool
) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "run_id": run_id,
        "run_class": run_class,
        "created_utc": created.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        "created_ist": created.astimezone(IST).isoformat(timespec="microseconds"),
        "hashes": dict(sorted(hashes.items())),
        "holdout_dated_inputs": holdout_dated_inputs,
    }


@dataclass(frozen=True)
class _Scan:
    entries: list[dict[str, Any]]
    good_bytes: int  # length of the newline-terminated prefix
    tail: bytes  # the unterminated tail line, if any
    anchor_seq: int
    anchor_head: str
    manifest: RunManifest


class _Run:
    """Read-only access to one run directory: the verification every open, append and witness check shares."""

    def __init__(self, run_dir: Path, run_id: str) -> None:
        self.dir = run_dir
        self.run_id = run_id
        self.log = run_dir / LOG_NAME
        self.anchor = run_dir / ANCHOR_NAME
        self.lock = run_dir / LOCK_NAME
        self.manifest_path = run_dir / MANIFEST_NAME
        self.sidecar = run_dir / SIDECAR_NAME
        self.torn = run_dir / TORN_DIR

    # permissions are checked, never repaired: a loosened mode is reported to the operator
    def check_permissions(self) -> None:
        try:
            if os.stat(self.dir).st_mode & 0o077:
                raise EvidenceRefused("permissions_too_open")
            for path in (self.log, self.anchor, self.lock, self.manifest_path, self.sidecar):
                if path.exists() and os.stat(path).st_mode & 0o077:
                    raise EvidenceRefused("permissions_too_open")
        except OSError as exc:
            raise EvidenceRefused("run_directory_unreadable") from exc

    def latch(self, code: str) -> None:
        _LATCHED.setdefault(str(self.dir), code)

    def recorded_sidecar_code(self) -> str | None:
        latched = _LATCHED.get(str(self.dir))
        if latched is not None:
            return latched
        try:
            marker = next((p for p in sorted(self.dir.iterdir()) if p.name.startswith(ABORT_MARKER_PREFIX)), None)
        except OSError:
            marker = None
        if marker is not None:
            code = marker.name[len(ABORT_MARKER_PREFIX):]
            return code if _CODE.match(code) else "abort_marker_invalid"
        if not self.sidecar.exists():
            return None
        try:
            data = json.loads(self.sidecar.read_bytes().decode("ascii"))
            code = data.get("code")
            if isinstance(code, str) and _CODE.match(code):
                return code
        except (OSError, ValueError, UnicodeDecodeError, AttributeError):
            pass
        return "sidecar_unreadable"

    def read_manifest(self) -> RunManifest:
        if not self.dir.is_dir():
            raise EvidenceRefused("store_missing")
        if not self.manifest_path.exists():
            raise EvidenceRefused("store_incomplete")
        try:
            data = json.loads(self.manifest_path.read_bytes().decode("ascii"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise EvidenceCorrupt("manifest_invalid") from exc
        base_keys = {"schema", "run_id", "run_class", "created_utc", "created_ist", "hashes", "manifest_sha256"}
        # ``holdout_dated_inputs`` is written by every ``create``; a manifest from before the field existed lacks it and
        # reads as False, which a holdout-dated read refuses.
        if not isinstance(data, dict) or set(data) not in (base_keys, base_keys | {"holdout_dated_inputs"}):
            raise EvidenceCorrupt("manifest_invalid")
        digest = data["manifest_sha256"]
        if not isinstance(digest, str) or hashlib.sha256(_canonical({k: v for k, v in data.items() if k != "manifest_sha256"})).hexdigest() != digest:
            raise EvidenceCorrupt("manifest_hash_mismatch")
        hashes = data["hashes"]
        if (
            data["schema"] != MANIFEST_SCHEMA
            or data["run_id"] != self.run_id
            or data["run_class"] not in RUN_CLASSES
            or not isinstance(hashes, dict)
            or not hashes
            or not all(isinstance(k, str) and re.match(r"^[a-z][a-z0-9_]{0,40}\Z", k) and isinstance(v, str) and _HEX64.match(v) for k, v in hashes.items())
            or not isinstance(data.get("holdout_dated_inputs", False), bool)
        ):
            raise EvidenceCorrupt("manifest_invalid")
        return RunManifest(
            self.run_id, data["run_class"], str(data["created_utc"]), str(data["created_ist"]), dict(hashes), digest,
            data.get("holdout_dated_inputs", False),
        )

    def read_anchor(self) -> tuple[int, str]:
        if not self.anchor.exists():
            raise EvidenceCorrupt("anchor_missing")
        try:
            data = json.loads(self.anchor.read_bytes().decode("ascii"))
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise EvidenceCorrupt("anchor_invalid") from exc
        if (
            not isinstance(data, dict)
            or set(data) != {"seq", "head_sha256"}
            or not _is_int(data["seq"])
            or not isinstance(data["head_sha256"], str)
            or not _HEX64.match(data["head_sha256"])
        ):
            raise EvidenceCorrupt("anchor_invalid")
        return data["seq"], data["head_sha256"]

    def inspect(self) -> _Scan:
        manifest = self.read_manifest()
        anchor_seq, anchor_head = self.read_anchor()
        if not self.log.exists():
            raise EvidenceCorrupt("log_missing")
        try:
            raw = self.log.read_bytes()
        except OSError as exc:
            raise EvidenceCorrupt("log_unreadable") from exc
        cut = raw.rfind(b"\n") + 1
        complete, tail = raw[:cut], raw[cut:]
        entries: list[dict[str, Any]] = []
        prev = GENESIS
        for number, line in enumerate(complete.split(b"\n")[:-1], start=1):
            try:
                entry = json.loads(line.decode("ascii"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise EvidenceCorrupt("line_invalid") from exc
            if not isinstance(entry, dict) or not set(_ENVELOPE) <= set(entry) or entry["kind"] not in RECORD_KINDS:
                raise EvidenceCorrupt("line_invalid")
            if entry["seq"] != number or entry["prev_sha256"] != prev:
                raise EvidenceCorrupt("chain_break")
            if entry["run_id"] != self.run_id or entry["run_class"] != manifest.run_class:
                raise EvidenceCorrupt("run_mismatch")
            if not isinstance(entry["entry_sha256"], str) or _entry_hash(entry) != entry["entry_sha256"]:
                raise EvidenceCorrupt("entry_hash_mismatch")
            prev = entry["entry_sha256"]
            entries.append(entry)
        if len(entries) < anchor_seq:
            raise EvidenceCorrupt("log_shorter_than_anchor")
        if (GENESIS if anchor_seq == 0 else entries[anchor_seq - 1]["entry_sha256"]) != anchor_head:
            raise EvidenceCorrupt("head_mismatch")
        if len(entries) > anchor_seq + 1:
            raise EvidenceCorrupt("ahead_of_anchor")
        if tail and len(entries) != anchor_seq:
            raise EvidenceCorrupt("torn_tail_not_after_anchor")
        return _Scan(entries, len(complete), tail, anchor_seq, anchor_head, manifest)

    def write_sidecar(self, code: str) -> None:
        """Record the abort durably. Never overwrites (the first cause stands) and never touches the log.

        The in-memory latch is set first, so a failure below cannot reopen the store in this process. If the
        sidecar cannot be written, the anchor is renamed to a marker that carries the code. If that fails too the
        abort is not durable, and ``EvidenceRefused("abort_not_recorded")`` says so (the latch still holds).
        """

        self.latch(code)
        body = _canonical({"abort": "evidence_corruption", "code": code, "schema": "shadow-india-abort/1"}) + b"\n"
        try:
            _write_new_file(self.sidecar, body)
            _fsync_dir(self.dir)
            return
        except FileExistsError:
            return  # a second report: the first sidecar stands
        except OSError:
            pass
        if not self.anchor.exists():
            return  # no anchor to rename, and a store without its anchor refuses to open
        try:
            os.rename(self.anchor, self.dir / (ABORT_MARKER_PREFIX + code))
            _fsync_dir(self.dir)
        except OSError as exc:
            raise EvidenceRefused("abort_not_recorded") from exc


class EvidenceStore:
    """Append-only evidence for one run and one session. Implements ``admission.EvidencePort``."""

    def __init__(
        self,
        run: _Run,
        manifest: RunManifest,
        *,
        session_id: str,
        clock: Clock,
        forbidden: Sequence[str],
    ) -> None:
        self._run = run
        self.manifest = manifest
        self.session_id = session_id
        self._clock = clock
        self._forbidden = tuple(forbidden)
        # Test seam for the crash probes: called with each stage name; a probe kills the process there.
        self._checkpoint: Callable[[str], None] = lambda stage: None

    # -- construction ---------------------------------------------------------------------------

    @classmethod
    def create(
        cls,
        root: str | Path,
        *,
        run_id: str,
        run_class: str,
        session_id: str,
        hashes: Mapping[str, str],
        holdout_dated_inputs: bool = False,
        clock: Clock = _utc_now,
        forbidden: Sequence[str] = (),
    ) -> "EvidenceStore":
        if not _RUN_ID.match(run_id):
            raise EvidenceRefused("bad_run_id")
        if not isinstance(holdout_dated_inputs, bool):
            raise EvidenceRefused("bad_holdout_flag")
        if run_class not in RUN_CLASSES:
            raise EvidenceRefused("bad_run_class")
        if not _check_type("id", session_id):
            raise EvidenceRefused("bad_session_id")
        if not hashes or not all(
            isinstance(k, str) and re.match(r"^[a-z][a-z0-9_]{0,40}\Z", k) and isinstance(v, str) and _HEX64.match(v)
            for k, v in hashes.items()
        ):
            raise EvidenceRefused("bad_manifest_hashes")
        # Every field the manifest and later records will carry, scanned before the first byte is written.
        _scan_for_secrets(
            {"run_id": run_id, "run_class": run_class, "session_id": session_id, "hashes": dict(hashes)}, forbidden
        )
        base = _refuse_unsafe_root(Path(root))
        base.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        run = _Run(base / run_id, run_id)
        try:
            os.mkdir(run.dir, DIR_MODE)
        except FileExistsError as exc:
            raise EvidenceRefused("run_exists") from exc
        os.chmod(run.dir, DIR_MODE)
        now = clock()
        if now.tzinfo is None:
            raise EvidenceRefused("naive_clock")
        _write_new_file(run.anchor, _canonical({"head_sha256": GENESIS, "seq": 0}) + b"\n")
        _write_new_file(run.log, b"")
        _write_new_file(run.lock, b"")
        body = _manifest_body(run_id, run_class, now, hashes, holdout_dated_inputs)
        digest = hashlib.sha256(_canonical(body)).hexdigest()
        _write_new_file(run.manifest_path, _canonical({**body, "manifest_sha256": digest}) + b"\n")  # last: commits create
        _fsync_dir(run.dir)
        _fsync_dir(base)
        return cls.open(base, run_id=run_id, session_id=session_id, clock=clock, forbidden=forbidden)

    @classmethod
    def open(
        cls,
        root: str | Path,
        *,
        run_id: str,
        session_id: str,
        clock: Clock = _utc_now,
        forbidden: Sequence[str] = (),
    ) -> "EvidenceStore":
        if not _RUN_ID.match(run_id):
            raise EvidenceRefused("bad_run_id")
        if not _check_type("id", session_id):
            raise EvidenceRefused("bad_session_id")
        _scan_for_secrets({"run_id": run_id, "session_id": session_id}, forbidden)
        base = _refuse_unsafe_root(Path(root))
        run = _Run(base / run_id, run_id)
        if run.dir.is_dir():
            recorded = run.recorded_sidecar_code()
            if recorded is not None:
                raise EvidenceCorrupt(recorded)
            run.check_permissions()
        manifest = cls._guard(run, run.read_manifest)
        store = cls(run, manifest, session_id=session_id, clock=clock, forbidden=forbidden)
        with store._locked(exclusive=True):
            store._load_and_settle()
        return store

    @staticmethod
    def _guard(run: _Run, action: Callable[[], Any]) -> Any:
        try:
            return action()
        except EvidenceCorrupt as exc:
            run.write_sidecar(exc.code)
            raise

    # -- locking --------------------------------------------------------------------------------

    def _locked(self, *, exclusive: bool):
        return _Flock(self._run.lock, exclusive)

    # -- verification and settling -------------------------------------------------------------

    def _load(self) -> _Scan:
        recorded = self._run.recorded_sidecar_code()
        if recorded is not None:
            raise EvidenceCorrupt(recorded)
        self._run.check_permissions()
        return self._guard(self._run, self._run.inspect)

    def _load_and_settle(self) -> _Scan:
        scan = self._load()
        if scan.tail:
            self._set_aside(scan)
            scan = self._load()
        if len(scan.entries) == scan.anchor_seq + 1:
            # One complete, chain-verified entry beyond the anchor. It may have been written and never fsynced
            # (a crash between the write and the fsync), so make it durable BEFORE the anchor names it: an anchor
            # must never be ahead of the durable log, or a second crash could lose an anchored entry.
            self._sync_log()
            head = scan.entries[-1]["entry_sha256"]
            self._write_anchor(len(scan.entries), head)
            scan = self._load()
        return scan

    def _sync_log(self) -> None:
        try:
            _fsync_path(self._run.log)
        except OSError as exc:
            raise EvidenceRefused("evidence_not_written") from exc

    def _set_aside(self, scan: _Scan) -> None:
        """Archive the torn tail durably (file, ``torn/``, run dir all fsynced), and only then cut it from the log."""

        run = self._run
        try:
            run.torn.mkdir(mode=DIR_MODE, exist_ok=True)
            os.chmod(run.torn, DIR_MODE)
            existing = [p for p in sorted(run.torn.iterdir()) if p.is_file()]
            archive = next((p for p in existing if p.read_bytes() == scan.tail), None)
            if archive is None:
                archive = run.torn / f"tail-after-{scan.anchor_seq:08d}-{len(existing):04d}.bin"
                _write_new_file(archive, scan.tail)
            # A matching copy left by an earlier attempt may never have been synced: sync it again, then the
            # directory entries (``torn/`` itself lives in the run directory). Truncate only after all of it.
            _fsync_path(archive)
            _fsync_dir(run.torn)
            _fsync_dir(run.dir)
            fd = os.open(run.log, os.O_WRONLY | os.O_CLOEXEC)
            try:
                os.ftruncate(fd, scan.good_bytes)
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise EvidenceRefused("torn_tail_not_set_aside") from exc

    def _write_anchor(self, seq: int, head: str) -> None:
        _replace_file(self._run.anchor, _canonical({"head_sha256": head, "seq": seq}) + b"\n")

    # -- the port -----------------------------------------------------------------------------

    def append(self, kind: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = validate_payload(kind, payload, forbidden=self._forbidden)
        now = self._clock()
        if now.tzinfo is None:
            raise EvidenceRefused("naive_clock")
        with self._locked(exclusive=True):
            scan = self._load_and_settle()
            if kind not in ALLOWED_AFTER_ABORT and any(e["kind"] == "abort" for e in scan.entries):
                raise EvidenceAborted("run_aborted")
            prev = scan.entries[-1]["entry_sha256"] if scan.entries else GENESIS
            entry: dict[str, Any] = {
                **body,
                "seq": len(scan.entries) + 1,
                "prev_sha256": prev,
                "kind": kind,
                "run_id": self._run.run_id,
                # A record about another session (recovery on a later day) names that session.
                "session_id": payload.get("session_id", self.session_id),
                "run_class": scan.manifest.run_class,
                "utc": now.astimezone(timezone.utc).isoformat(timespec="microseconds"),
                "ist": now.astimezone(IST).isoformat(timespec="microseconds"),
            }
            # The whole envelope, generated fields included (run_id, session_id, times), before any write.
            _scan_for_secrets(entry, self._forbidden)
            entry["entry_sha256"] = _entry_hash(entry)
            line = _canonical(entry) + b"\n"
            self._checkpoint("before_write")
            try:
                fd = os.open(self._run.log, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC)
                try:
                    _write_all(fd, line)
                    self._checkpoint("after_write")
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError as exc:
                raise EvidenceRefused("evidence_not_written") from exc
            self._checkpoint("after_fsync")
            try:
                self._write_anchor(entry["seq"], entry["entry_sha256"])
            except OSError as exc:
                raise EvidenceRefused("anchor_not_written") from exc
            self._checkpoint("after_anchor")
            return dict(entry)

    def records(self) -> list[dict[str, Any]]:
        """Every complete, verified record in order. Read-only: a torn tail is ignored, not repaired."""

        with self._locked(exclusive=False):
            return [dict(entry) for entry in self._load().entries]

    # -- witness --------------------------------------------------------------------------------

    def head(self) -> tuple[int, str]:
        entries = self.records()
        return (len(entries), entries[-1]["entry_sha256"] if entries else GENESIS)

    def witness_line(self) -> str:
        seq, head = self.head()
        return f"witness run={self._run.run_id} seq={seq} head={head}"

    @property
    def run_dir(self) -> Path:
        return self._run.dir


class _Flock:
    def __init__(self, path: Path, exclusive: bool) -> None:
        self._path = path
        self._mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        self._fd: int | None = None

    def __enter__(self) -> "_Flock":
        try:
            self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, FILE_MODE)
            fcntl.flock(self._fd, self._mode)
        except OSError as exc:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            raise EvidenceRefused("evidence_lock_unavailable") from exc
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


_WITNESS = re.compile(r"\bwitness run=(\S+) seq=(\d+) head=([0-9a-f]{64})\b")


def _run_of(root: str | Path, run_id: str) -> _Run:
    if not _RUN_ID.match(run_id):
        raise EvidenceRefused("bad_run_id")
    return _Run(_refuse_unsafe_root(Path(root)) / run_id, run_id)


def read_records(root: str | Path, run_id: str) -> list[dict[str, Any]]:
    """Verify a run read-only and return its records. Raises EvidenceCorrupt without writing a sidecar."""

    run = _run_of(root, run_id)
    recorded = run.recorded_sidecar_code() if run.dir.is_dir() else None
    if recorded is not None:
        raise EvidenceCorrupt(recorded)
    return [dict(entry) for entry in run.inspect().entries]


def verify_witness(root: str | Path, run_id: str, witness_text: str, *, min_heads: int = 1) -> list[tuple[int, str]]:
    """Refuse a store whose chain lacks a head the operator pasted into the witness log.

    Returns the verified ``(seq, head)`` pairs. Deleting the log tail and the anchor together passes the
    chain and anchor checks; only a pasted head from before the deletion exposes it. That exposure is terminal:
    a mismatch writes the abort sidecar, so every later open and append is refused with the bytes kept.
    """

    entries = read_records(root, run_id)
    pasted = [(int(seq), head) for run, seq, head in _WITNESS.findall(witness_text) if run == run_id]
    if len(pasted) < min_heads:
        raise WitnessMismatch("witness_missing")
    for seq, head in pasted:
        if seq > len(entries) or (GENESIS if seq == 0 else entries[seq - 1]["entry_sha256"]) != head:
            _run_of(root, run_id).write_sidecar("witness_head_not_in_chain")
            raise WitnessMismatch("witness_head_not_in_chain")
    return pasted


__all__ = [
    "ALLOWED_AFTER_ABORT",
    "EvidenceAborted",
    "EvidenceCorrupt",
    "EvidenceError",
    "EvidenceRefused",
    "EvidenceStore",
    "GENESIS",
    "IST",
    "RECORD_KINDS",
    "RunManifest",
    "SCHEMAS",
    "WitnessMismatch",
    "default_root",
    "read_records",
    "validate_payload",
    "verify_witness",
]
