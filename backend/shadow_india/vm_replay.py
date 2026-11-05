"""Offline replay of the shadow portfolio through the VM's own risk code (64-05 Task 2b, D-23, D-24).

D-06 keeps the Mac shadow portfolio apart from broker holdings, so the VM never sees it. D-23 closes that gap
offline: for every intent of a session, rebuild the shadow portfolio as it stood at that decision from the
evidence, hand it to ``gateway_vm.orders.limits.evaluate`` loaded from a checkout of the deployed build, and
require the VM's verdict to agree with the verdict the Mac recorded. Nothing is sent anywhere. This module builds
no relay client and no HTTP client, and the test suite runs it under a network sentinel.

What the replay refuses (typed ``ReplayRefused``, nothing written):

- ``replay_build_mismatch``: no recorded build identity (the session's ``vm_build`` records, which must all agree), the checkout's commit is not the recorded one, the file
  hashes under ``orders/`` differ from the recorded ones, the code cannot be loaded, or the public surface the
  replay calls (``evaluate``'s signature, the dataclass fields it builds) is not the one this file pins. The
  replay never adapts to a changed surface and never falls back to the working tree;
- ``replay_limits_mismatch``: two hashes must both hold. The IndiaLimits file hash (what the manifest and every
  signal carry) must equal the hash of the limits the caller loaded from the pinned private config, and the nine-key
  P-06 hash the Mac minted with (recorded on the session's ``vm_build``) must equal the hash of the nine keys the
  recorded build builds from that same config. The collar lives only in ``execution.json``, so the second check is
  what pins it to the session. No default and no working-tree limits file is ever read;
- ``replay_pins_missing`` / ``replay_pins_invalid``: the quote or tick reference an ``intent`` record pins (all eight
  fields, written in the same append as the intent) is absent or malformed;
- ``replay_session_open``: the session has no ``session_close`` yet. Only closed sessions are replayed;
- ``replay_evidence_invalid`` / ``replay_state_ambiguous``: the evidence cannot be folded into one portfolio, or a
  record lacks a field the replay reads;
- ``replay_signal_missing`` / ``replay_signal_mismatch``: an entry intent's ``signal`` record is absent or names
  another symbol, ISIN or series. The intent record the Mac writes carries neither ISIN nor series, so for an entry
  both come from the signal it points at (``signal_id``), where they are required;
- ``replay_leg_unknown`` / ``replay_instrument_unresolved``: the runner's exit and swing sells have no signal. Their
  id (``exit-<reason>-<date>-<ISIN>-tN``, ``swing-<date>-<ISIN>``) names the ISIN, which must be a position the
  evidence holds, under the same symbol, and the series comes from the pinned quote. A leg or id shape that is
  none of these, or an ISIN that is not held, refuses.

The build that runs is the build that was hashed: the source bytes of ``orders/`` are read once, hashed, and those
same bytes are compiled and executed. No import machinery and no cached bytecode is consulted. The build's data
files (the tick table) are read from a private copy of the hashed bytes, never from the checkout, so a file swapped
after the hash walk cannot change a verdict.

What the replay records (``SessionReplay``): the build and limits hashes, the intents compared and the verdict.
The first disagreement stops the session, is kept with both verdicts, opens a defect and pauses the run through
the ``ReplaySink`` port. A kill switch code in a replay verdict is a replay defect, never a divergence: the
replay asks what the VM would decide with the kill switch open, because the live flag stays blocked during shadow
and would otherwise refuse every intent first.

Claim boundary (D-06): the evidence is "Mac simulation plus separately tested VM guards".

Pure over its inputs: no clock, no network, no subprocess, no randomness. It reads the checkout it is pointed at
and nothing else.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import itertools
import json
import re
import sys
import tempfile
import types
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

# The runner's leg names and exit-id pattern, copied rather than imported: importing ``runner`` would pull the relay
# client, the execution service and httpx into this pure module. A test pins every value equal to the runner's, so an
# id the runner mints cannot drift from the id this file accepts; another test pins the import closure.
LEG_ENTRY = "entry"
LEG_EXIT = "exit"
LEG_SWING = "swing"
_EXIT_ID = re.compile(r"^exit-(halve|stop|flatten)-\d{8}-[A-Z]{2}[A-Z0-9]{9}[0-9]-t[1-9][0-9]*\Z")

D06_LABEL = "Mac simulation plus separately tested VM guards"

# Refusal and defect codes. Stable, and none carries a value.
BUILD_MISMATCH = "replay_build_mismatch"
LIMITS_MISMATCH = "replay_limits_mismatch"
PINS_MISSING = "replay_pins_missing"
PINS_INVALID = "replay_pins_invalid"
EVIDENCE_INVALID = "replay_evidence_invalid"
SIGNAL_MISSING = "replay_signal_missing"
SIGNAL_MISMATCH = "replay_signal_mismatch"
LEG_UNKNOWN = "replay_leg_unknown"
INSTRUMENT_UNRESOLVED = "replay_instrument_unresolved"
STATE_AMBIGUOUS = "replay_state_ambiguous"
KILL_SWITCH_IN_VERDICT = "replay_kill_switch_in_verdict"
DIVERGENCE = "replay_divergence"
DAY_MISSING = "replay_day_missing"
BUILD_HASH_MIXED = "replay_build_hash_mixed"
SESSION_REFUSED = "replay_session_refused"
SESSION_OPEN = "replay_session_open"
NOT_DECIDE_DAY = "replay_not_a_decide_day"
STRAY_RECORD = "replay_record_outside_decide_days"
NONDETERMINISTIC = "replay_nondeterministic"
DEFECT = "replay_defect"
NOT_REPLAYED = "not_replayed:no_decide"

# Shadow never calls ``OrdersClient.halt`` and never reads an account id mismatch, so a session is replayed with both
# flags false. A VM answer carrying either code is classified ABORT on the Mac (``vm_codes``), so such a run has ended.
MAC_HALT = False
ACCOUNT_MISMATCH = False

PASS = "PASS"
FAIL = "FAIL"

# Mac verdicts.
ADMITTED = "admitted"
DENIED = "denied"
NO_DECISION = "none"

# The kill switch is asked open (module docstring). ``evaluate`` appends ``kill_switch`` first whenever this is
# false, which would hide every portfolio rule behind one code.
REPLAY_KILL_ENABLED = True
KILL_SWITCH = "kill_switch"

# The pinned code map: the Mac reason code to the VM reason code it must meet. These are the names ORDERS-API
# "Mac admission codes" lists as shared with O6, spelled the same on both sides. A Mac code that is not here
# (the P-16 slippage codes, ``intent_invalid``, ``india_limits_unavailable``, a cash refusal) has no VM
# counterpart, so the replay cannot call the two verdicts equal: an unmapped code is a divergence (D-23).
MAC_TO_VM_CODE: Mapping[str, str] = {
    name: name
    for name in (
        "capital_cap",
        "per_position_cap",
        "collar",
        "circuit_band",
        "off_tick",
        "isin_mismatch",
        "instrument_unsupported",
        "sell_exceeds_holding",
        "halt_latch",
        "pilot_ended",
        "stop_open",
        "session_closed",
        "quote_unavailable",
        "tick_reference_unavailable",
    )
}

# The surface of the recorded build this file calls. A difference is ``replay_build_mismatch``.
EVALUATE_PARAMETERS: tuple[tuple[str, str], ...] = (
    ("limits", "POSITIONAL_OR_KEYWORD"),
    ("flags", "POSITIONAL_OR_KEYWORD"),
    ("account", "POSITIONAL_OR_KEYWORD"),
    ("quote", "POSITIONAL_OR_KEYWORD"),
    ("now_ist", "POSITIONAL_OR_KEYWORD"),
    ("intent", "POSITIONAL_OR_KEYWORD"),
    ("kill_enabled", "KEYWORD_ONLY"),
    ("tick_reference", "KEYWORD_ONLY"),
    ("tick_table", "KEYWORD_ONLY"),
)
DATACLASS_FIELDS: Mapping[str, tuple[str, ...]] = {
    "Intent": (
        "intent_id", "proposal_id", "workspace", "broker", "mode", "exchange", "product", "order_type", "validity",
        "side", "stock_code", "isin", "quantity", "limit_price", "reason", "batch_id", "limits_sha256",
        "params_sha256", "key_id",
    ),
    "Quote": (
        "stock_code", "isin", "series", "ltp", "lower_circuit", "upper_circuit", "previous_close", "session_date",
    ),
    "RiskFlags": ("halt", "ended", "mac_halt", "account_mismatch", "stops", "ledger_cost"),
    "AccountSnapshot": ("holdings", "open_orders", "trades"),
    "Holding": ("isin", "quantity", "cost"),
    "OpenOrder": ("isin", "side", "quantity", "limit_price"),
    "Limits": (
        "capital_cap", "per_position_cap", "drawdown_halt", "drawdown_flatten", "position_stop",
        "fat_finger_collar", "sha256",
    ),
}
LIMIT_KEYS: tuple[str, ...] = (
    "schema_version", "workspace", "currency", "capital_cap", "per_position_cap", "drawdown_halt",
    "drawdown_flatten", "position_stop", "fat_finger_collar",
)

# ``evaluate`` reads none of these three. They stay placeholders so the Intent is whole.
_UNUSED_HEX = "0" * 64

_HEX40 = re.compile(r"^[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"^[0-9a-f]{64}\Z")
_SOURCE_PACKAGE = "gateway_vm"
_ORDERS_DIR = "orders"
_LOAD_ORDER = ("intent", "challenge", "limits")
_load_counter = itertools.count(1)
_PRIVATE_ROOT = Path(__file__).resolve().parents[2] / "private"  # the repo's private/ is never read


class ReplayRefused(Exception):
    """The replay will not run. ``code`` is stable and carries no value; nothing was written."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- pins ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class BuildIdentity:
    """The deployed VM build the session recorded (OI-9): the commit and the hash of every file under ``orders/``."""

    commit: str
    orders_sha256: Mapping[str, str]

    @property
    def digest(self) -> str:
        return _sha256_json({"commit": self.commit, "orders": dict(sorted(self.orders_sha256.items()))})


@dataclass(frozen=True)
class QuotePin:
    ltp: Decimal
    lower_circuit: Decimal
    upper_circuit: Decimal
    previous_close: Decimal
    series: str
    session_date: date


@dataclass(frozen=True)
class IntentPin:
    quote: QuotePin
    tick_reference: Decimal | None  # None is a recorded "the port could not supply it", not a missing pin


@dataclass(frozen=True)
class ReplayLimits:
    """What the caller loaded from the pinned private config: the nine P-06 keys and the IndiaLimits file hash.

    The values are edge: they are never part of a ``repr``, a record, a log line or the report. Only hashes are.
    """

    fields: Mapping[str, Any] = field(repr=False)
    file_sha256: str


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()


def _dec(value: Any, *, code: str = PINS_INVALID) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ReplayRefused(code)
    try:
        number = Decimal(value if isinstance(value, str) else str(value))
    except InvalidOperation:
        raise ReplayRefused(code) from None
    if not number.is_finite():
        raise ReplayRefused(code)
    return number


def _whole(value: Decimal) -> int:
    if value != value.to_integral_value() or value <= 0:
        raise ReplayRefused(EVIDENCE_INVALID)
    return int(value)


def _need(record: Mapping[str, Any], key: str) -> Any:
    """A field the replay reads. Absent is ``replay_evidence_invalid``, never a bare ``KeyError``."""

    try:
        return record[key]
    except (KeyError, TypeError):
        raise ReplayRefused(EVIDENCE_INVALID) from None


def _seq(record: Mapping[str, Any]) -> int:
    value = _need(record, "seq")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReplayRefused(EVIDENCE_INVALID)
    return value


# The eight fields an ``intent`` record pins (``evidence.QUOTE_PIN_FIELDS``, written by ``admission.quote_pin_fields``).
PIN_FIELDS: tuple[str, ...] = (
    "quote_ltp", "quote_lower_circuit", "quote_upper_circuit", "quote_previous_close", "quote_series",
    "quote_session_date", "tick_reference", "quote_raw_sha256",
)


def pins_from_evidence(records: Sequence[Mapping[str, Any]], session_id: str) -> dict[str, IntentPin]:
    """The quote and tick pin of every ``intent`` record of the session. One missing field is ``replay_pins_missing``.

    ``tick_reference`` must be a KEY of the record: its value is a decimal string or null, and null replays as
    ``tick_reference=None`` (the Mac could not supply one; the VM judges that). A missing key is a missing pin.
    """

    out: dict[str, IntentPin] = {}
    for record in records:
        if record.get("kind") != "intent" or record.get("session_id") != session_id:
            continue
        if any(name not in record for name in PIN_FIELDS):
            raise ReplayRefused(PINS_MISSING)
        series, when = record["quote_series"], record["quote_session_date"]
        if not isinstance(series, str):
            raise ReplayRefused(PINS_INVALID)
        try:
            session_date = date.fromisoformat(when)
        except (TypeError, ValueError):
            raise ReplayRefused(PINS_INVALID) from None
        tick = record["tick_reference"]
        out[str(_need(record, "intent_id"))] = IntentPin(
            QuotePin(
                _dec(record["quote_ltp"]), _dec(record["quote_lower_circuit"]), _dec(record["quote_upper_circuit"]),
                _dec(record["quote_previous_close"]), series, session_date,
            ),
            None if tick is None else _dec(tick),
        )
    return out


def build_from_evidence(records: Sequence[Mapping[str, Any]], session_id: str) -> tuple[BuildIdentity, str]:
    """The build and the nine-key mint hash the session recorded. Every ``vm_build`` of the session must be equal.

    No ``vm_build`` for the session, two unequal ones, a malformed one, or a digest that does not recompute from the
    commit and the file map is ``replay_build_mismatch``. Nothing else (no argument, no file) names a build.
    """

    found = [r for r in records if r.get("kind") == "vm_build" and r.get("session_id") == session_id]
    if not found:
        raise ReplayRefused(BUILD_MISMATCH)
    shapes = {
        (
            r.get("commit"), json.dumps(r.get("orders_sha256"), sort_keys=True, default=str), r.get("digest"),
            r.get("mint_limits_sha256"),
        )
        for r in found
    }
    if len(shapes) != 1:
        raise ReplayRefused(BUILD_MISMATCH)
    first = found[0]
    commit, files, digest, mint = first.get("commit"), first.get("orders_sha256"), first.get("digest"), first.get("mint_limits_sha256")
    if (
        not isinstance(commit, str) or not _HEX40.match(commit)
        or not isinstance(files, dict) or not files
        or not all(isinstance(k, str) and isinstance(v, str) and _HEX64.match(v) for k, v in files.items())
        or not isinstance(digest, str) or not isinstance(mint, str) or not _HEX64.match(mint)
    ):
        raise ReplayRefused(BUILD_MISMATCH)
    build = BuildIdentity(commit, dict(files))
    if build.digest != digest:
        raise ReplayRefused(BUILD_MISMATCH)
    return build, mint


# --- the recorded build ------------------------------------------------------------------------------------------


def _read_orders_sources(vm_source: Path) -> dict[str, bytes]:
    """Every file under ``<vm_source>/gateway_vm/orders`` as bytes, keyed by posix path below it.

    ``__pycache__`` is left out: it is not the code, and nothing here ever loads it.
    """

    root = Path(vm_source) / _SOURCE_PACKAGE / _ORDERS_DIR
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


def _digests(sources: Mapping[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}


def orders_tree_sha256(vm_source: Path) -> dict[str, str]:
    """sha256 of every file under ``<vm_source>/gateway_vm/orders``, keyed by posix path below it."""

    return _digests(_read_orders_sources(vm_source))


def read_git_commit(vm_source: Path) -> str | None:
    """The commit a checkout is on, read from its ``.git`` files (no subprocess). Looks at the source dir and two parents.

    Handles a plain ``.git`` directory, a linked worktree (``.git`` file, ``commondir``), a detached HEAD and
    ``packed-refs``. ``None`` when nothing readable names a 40-hex commit.
    """

    start = Path(vm_source).resolve()
    for base in (start, start.parent, start.parent.parent):
        marker = base / ".git"
        try:
            if marker.is_dir():
                gitdir = marker
            elif marker.is_file():
                text = marker.read_text(encoding="ascii").strip()
                if not text.startswith("gitdir:"):
                    return None
                gitdir = (base / text[len("gitdir:"):].strip()).resolve()
            else:
                continue
            head = (gitdir / "HEAD").read_text(encoding="ascii").strip()
            if _HEX40.match(head):
                return head
            if not head.startswith("ref: "):
                return None
            ref = head[len("ref: "):].strip()
            common = gitdir
            pointer = gitdir / "commondir"
            if pointer.is_file():
                common = (gitdir / pointer.read_text(encoding="ascii").strip()).resolve()
            for directory in (gitdir, common):
                loose = directory / ref
                if loose.is_file() and _HEX40.match(loose.read_text(encoding="ascii").strip()):
                    return loose.read_text(encoding="ascii").strip()
            packed = common / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="ascii").splitlines():
                    parts = line.split(" ")
                    if len(parts) == 2 and parts[1] == ref and _HEX40.match(parts[0]):
                        return parts[0]
            return None
        except (OSError, UnicodeDecodeError):
            return None
    return None


@dataclass(frozen=True)
class _Vm:
    """The loaded build: its evaluator and the classes ``evaluate`` takes."""

    evaluate: Any
    intent_cls: Any
    quote_cls: Any
    flags_cls: Any
    account_cls: Any
    holding_cls: Any
    open_order_cls: Any
    limits_cls: Any
    ist: Any
    workdir: Any = None  # the private copy of the hashed files the build reads at run time; ``close`` removes it

    def close(self) -> None:
        if self.workdir is not None:
            self.workdir.cleanup()


def _exec_source(module_name: str, filename: str, source: bytes, package: str) -> Any:
    """Run ``source`` as module ``module_name`` of the private ``package``. The one place this file executes loaded code.

    The bytes are the ones that were hashed. Nothing reads a cached ``.pyc``: there is no spec, no loader and no
    path finder here. The package has no ``__path__`` on purpose, so a relative import of a file that was not
    loaded here fails instead of reaching the file system.
    """

    module = types.ModuleType(module_name)
    module.__file__ = filename
    module.__package__ = package
    sys.modules[module_name] = module  # the relative imports inside the package resolve through here
    code = compile(source, filename, "exec", dont_inherit=True)
    exec(code, module.__dict__)
    return module


def _forget_package(package: str) -> None:
    """Take the private package and every module of it out of ``sys.modules``, a module whose exec failed included."""

    for name in [n for n in list(sys.modules) if n == package or n.startswith(package + ".")]:
        sys.modules.pop(name, None)


def _load_build(vm_source: Path, commit: str, sources: Mapping[str, bytes]) -> _Vm:
    """Run ``gateway_vm.orders`` from the hashed ``sources`` as a private package, then take it out of ``sys.modules``.

    The package is loaded under a name no other build shares, so a session replayed against another checkout, or a
    ``gateway_vm`` the test run already imported from the working tree, can never answer for this one.

    The hashed bytes of every file are also written to a private directory, and each module is loaded as if it lived
    there. ``evaluate`` reads the tick table from ``Path(__file__).parent / "data"`` on every call, so this is what
    keeps that read on the hashed bytes. The caller owns the directory through ``_Vm.close``.
    """

    orders_dir = Path(vm_source) / _SOURCE_PACKAGE / _ORDERS_DIR
    token = hashlib.sha256(f"{orders_dir.resolve()}:{commit}".encode()).hexdigest()[:12]
    package = f"_growin_vm_replay_{token}_{next(_load_counter)}"
    workdir = tempfile.TemporaryDirectory(prefix="growin-vm-replay-")  # mode 0700, never the checkout
    try:
        private = Path(workdir.name) / _ORDERS_DIR
        for name, data in sources.items():
            target = private / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o400)
        _exec_source(package, str(private / "__init__.py"), sources["__init__.py"], package)
        modules: dict[str, Any] = {}
        for name in _LOAD_ORDER:
            modules[name] = _exec_source(f"{package}.{name}", str(private / f"{name}.py"), sources[f"{name}.py"], package)
        limits, intent = modules["limits"], modules["intent"]
        vm = _Vm(
            evaluate=limits.evaluate,
            intent_cls=intent.Intent,
            quote_cls=limits.Quote,
            flags_cls=limits.RiskFlags,
            account_cls=limits.AccountSnapshot,
            holding_cls=limits.Holding,
            open_order_cls=limits.OpenOrder,
            limits_cls=limits.Limits,
            ist=limits.IST,
            workdir=workdir,
        )
        _check_surface(vm)
    except BaseException as exc:
        workdir.cleanup()
        if isinstance(exc, (ReplayRefused, KeyboardInterrupt, SystemExit)):
            raise
        raise ReplayRefused(BUILD_MISMATCH) from None  # any load failure of the recorded build is the one refusal
    finally:
        _forget_package(package)
    return vm


def _check_surface(vm: _Vm) -> None:
    """Refuse a build whose ``evaluate`` or the types it takes are not the surface this file was written against."""

    try:
        parameters = tuple((p.name, p.kind.name) for p in inspect.signature(vm.evaluate).parameters.values())
        if parameters != EVALUATE_PARAMETERS:
            raise ReplayRefused(BUILD_MISMATCH)
        classes = {
            "Intent": vm.intent_cls, "Quote": vm.quote_cls, "RiskFlags": vm.flags_cls,
            "AccountSnapshot": vm.account_cls, "Holding": vm.holding_cls, "OpenOrder": vm.open_order_cls,
            "Limits": vm.limits_cls,
        }
        for name, cls in classes.items():
            if not dataclasses.is_dataclass(cls) or tuple(f.name for f in dataclasses.fields(cls)) != DATACLASS_FIELDS[name]:
                raise ReplayRefused(BUILD_MISMATCH)
        if not callable(getattr(vm.limits_cls, "from_fields", None)):
            raise ReplayRefused(BUILD_MISMATCH)
    except ReplayRefused:
        raise
    except (TypeError, ValueError, AttributeError):
        raise ReplayRefused(BUILD_MISMATCH) from None


def load_recorded_build(vm_source: Path, recorded: BuildIdentity | None) -> _Vm:
    """The build the session recorded, or ``replay_build_mismatch``. Never the working tree, never a guess."""

    if recorded is None:
        raise ReplayRefused(BUILD_MISMATCH)
    source = Path(vm_source)
    resolved = source.resolve()
    if resolved == _PRIVATE_ROOT or _PRIVATE_ROOT in resolved.parents:
        raise ReplayRefused(BUILD_MISMATCH)
    try:
        commit = read_git_commit(source)
        sources = _read_orders_sources(source)  # read once: these bytes are hashed below and are the ones that run
    except OSError:
        raise ReplayRefused(BUILD_MISMATCH) from None
    if commit is None or commit != recorded.commit:
        raise ReplayRefused(BUILD_MISMATCH)
    if _digests(sources) != dict(recorded.orders_sha256):
        raise ReplayRefused(BUILD_MISMATCH)
    return _load_build(source, commit, sources)


# --- limits -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Identity:
    """The instrument of an intent: from its ``signal`` record (an entry) or from its id (an exit or swing sell).

    ``series`` is ``None`` for a sell: no signal names it, so the pinned quote must.
    """

    isin: str
    series: str | None


def _signal_index(records: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for record in records:
        signal_id = record.get("signal_id")
        if record.get("kind") != "signal" or not isinstance(signal_id, str):
            continue
        known = out.get(signal_id)
        if known is not None and tuple(known.get(k) for k in _SIGNAL_FIELDS) != tuple(record.get(k) for k in _SIGNAL_FIELDS):
            raise ReplayRefused(EVIDENCE_INVALID)  # one signal id naming two different instruments
        out.setdefault(signal_id, record)
    return out


_SIGNAL_FIELDS = ("symbol", "isin", "series", "limits_sha256")


_ISIN_PATTERN = "[A-Z]{2}[A-Z0-9]{9}[0-9]"
_SWING_ID = re.compile(rf"^swing-\d{{8}}-({_ISIN_PATTERN})\Z")  # the runner mints ``swing-<YYYYMMDD>-<ISIN>``


def _leg_of(intent: Mapping[str, Any]) -> str:
    """The leg of an intent, checked against the id shape the runner mints for it. Anything else is ``replay_leg_unknown``."""

    leg, signal_id = _need(intent, "leg"), _need(intent, "signal_id")
    if leg == LEG_ENTRY:
        return LEG_ENTRY
    if not isinstance(signal_id, str) or _need(intent, "side") != "sell":
        raise ReplayRefused(LEG_UNKNOWN)
    if leg == LEG_EXIT and _EXIT_ID.fullmatch(signal_id):
        return LEG_EXIT
    if leg == LEG_SWING and _SWING_ID.fullmatch(signal_id):
        return LEG_SWING
    raise ReplayRefused(LEG_UNKNOWN)


def _sell_isin(leg: str, signal_id: str) -> str:
    """The ISIN an exit or swing id names. ``exit-<reason>-<date>-<ISIN>-tN`` has no dash inside any field."""

    if leg == LEG_EXIT:
        return signal_id.split("-")[3]
    matched = _SWING_ID.fullmatch(signal_id)
    assert matched is not None  # _leg_of matched it
    return matched.group(1)


def _signal_of(intent: Mapping[str, Any], signals: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any]:
    """The signal record of an intent, or a typed refusal.

    The Mac's intent record holds the symbol, side, quantity and price but neither ISIN nor series (the admission
    writer never emits them), so both are read from the signal, where they are required. An intent that does carry
    one must agree with it.
    """

    signal_id = _need(intent, "signal_id")
    signal = signals.get(signal_id) if isinstance(signal_id, str) else None
    if signal is None:
        raise ReplayRefused(SIGNAL_MISSING)
    if not all(isinstance(signal.get(k), str) for k in ("symbol", "isin", "series")):
        raise ReplayRefused(EVIDENCE_INVALID)
    if _need(intent, "symbol") != signal["symbol"]:
        raise ReplayRefused(SIGNAL_MISMATCH)
    for name in ("isin", "series"):
        if name in intent and intent[name] != signal[name]:
            raise ReplayRefused(SIGNAL_MISMATCH)
    return signal


def _entry_identity(intent: Mapping[str, Any], signals: Mapping[str, Mapping[str, Any]]) -> _Identity:
    signal = _signal_of(intent, signals)
    return _Identity(signal["isin"], signal["series"])


def checked_limits(
    vm: _Vm,
    nine_fields: Mapping[str, Any],
    file_sha256: str,
    mint_limits_sha256: str,
    manifest_hashes: Mapping[str, str],
    records: Sequence[Mapping[str, Any]],
    session_id: str,
    signals: Mapping[str, Mapping[str, Any]],
) -> Any:
    """The recorded build's ``Limits``, after both hashes of the session held. Any failure is ``replay_limits_mismatch``.

    1. ``file_sha256`` (the IndiaLimits hash of the loaded ``limits.json``) equals the manifest's ``limits_sha256``, and
       every signal of the session, and the signal behind every entry intent of it, recorded the same value.
    2. The nine keys build under the recorded build, and their ``sha256`` equals ``mint_limits_sha256`` (what the Mac
       minted with on this session). The collar comes from ``execution.json`` alone, so this is the only check that pins it.

    The two hashes are different documents and are never compared with each other. Both checks run for every session,
    one with only sells included.
    """

    if manifest_hashes.get("limits_sha256") != file_sha256:
        raise ReplayRefused(LIMITS_MISMATCH)
    for record in records:
        if record.get("kind") == "signal" and record.get("session_id") == session_id:
            if record.get("limits_sha256") != file_sha256:
                raise ReplayRefused(LIMITS_MISMATCH)
        elif record.get("kind") == "intent" and record.get("session_id") == session_id:
            # An exit or swing sell has no signal, so there is no per-signal hash to check: the session-level one stands.
            if _leg_of(record) == LEG_ENTRY and _signal_of(record, signals).get("limits_sha256") != file_sha256:
                raise ReplayRefused(LIMITS_MISMATCH)
    try:
        limits = vm.limits_cls.from_fields(dict(nine_fields))
    except Exception:  # noqa: BLE001 - a limits document the recorded build rejects cannot be the session's limits
        raise ReplayRefused(LIMITS_MISMATCH) from None
    if limits.sha256 != mint_limits_sha256:
        raise ReplayRefused(LIMITS_MISMATCH)
    return limits


# --- the portfolio at a decision point ---------------------------------------------------------------------------


class _Fold:
    """The shadow portfolio rebuilt from evidence records in order. Never the ledger, never a broker figure.

    Money rules copy ``ExecutionLedger.book_simulated_fill``: a buy adds ``quantity x price + charges`` to the
    position's cost, a sell removes the average cost of the shares sold, and a fully sold position has no cost.
    The admitted quantity of an order is the intent's quantity.
    """

    def __init__(self, signals: Mapping[str, Mapping[str, Any]]) -> None:
        self.signals = signals
        self.intents: dict[str, Mapping[str, Any]] = {}
        self.reserved: set[str] = set()
        self.terminal: set[str] = set()
        self.booked: dict[str, Decimal] = {}
        self.pending: dict[str, Mapping[str, Any]] = {}
        self.positions: dict[str, list[Decimal]] = {}  # isin -> [quantity, cost]
        self.symbols: dict[str, str] = {}  # isin -> the symbol its booked buys were made under
        self.halt = False
        self.ended = False
        self.stops: set[str] = set()

    def apply(self, record: Mapping[str, Any]) -> None:
        kind = record.get("kind")
        if kind == "intent":
            self.intents[str(_need(record, "intent_id"))] = record
        elif kind == "reservation":
            self.reserved.add(str(_need(record, "intent_id")))
        elif kind == "expiry":
            self.terminal.add(str(_need(record, "intent_id")))
        elif kind == "booking_intent":
            self.pending[str(_need(record, "booking_id"))] = record
        elif kind == "booking_refused":
            self.pending.pop(str(_need(record, "booking_id")), None)
        elif kind == "booking_done":
            booking = self.pending.pop(str(_need(record, "booking_id")), None)
            if booking is None:
                raise ReplayRefused(EVIDENCE_INVALID)
            self._book(booking)
        elif kind == "run_ended":
            self.ended = True
        elif kind == "latch":
            self._latch(str(record.get("latch")), str(record.get("scope")), str(record.get("reason")))

    def identity(self, intent: Mapping[str, Any]) -> _Identity:
        """The instrument of an intent. A sell must name a position the evidence holds, under the symbol it was bought as."""

        leg = _leg_of(intent)
        if leg == LEG_ENTRY:
            return _entry_identity(intent, self.signals)
        isin = _sell_isin(leg, _need(intent, "signal_id"))
        held = self.positions.get(isin, [Decimal(0), Decimal(0)])[0]
        if held <= 0 or self.symbols.get(isin) != _need(intent, "symbol"):
            raise ReplayRefused(INSTRUMENT_UNRESOLVED)
        return _Identity(isin, None)

    def _latch(self, name: str, scope: str, reason: str) -> None:
        if name == "stop" and scope.startswith("isin:"):
            isin = scope[len("isin:"):]
            if reason == "cleared_by_fill":
                self.stops.discard(isin)
            elif reason.startswith("raised_"):
                self.stops.add(isin)
        elif name == "halt" and scope == "entries":
            if reason == "released_by_operator":
                self.halt = False
            elif reason.startswith("raised_"):
                self.halt = True
        elif name == "ended":
            self.ended = True

    def _book(self, booking: Mapping[str, Any]) -> None:
        intent_id = str(_need(booking, "intent_id"))
        intent = self.intents.get(intent_id)
        if intent is None:
            raise ReplayRefused(EVIDENCE_INVALID)
        isin = self.identity(intent).isin
        cumulative = _dec(_need(booking, "cumulative_quantity"), code=EVIDENCE_INVALID)
        price = _dec(_need(booking, "price"), code=EVIDENCE_INVALID)
        charges = _dec(_need(booking, "charges"), code=EVIDENCE_INVALID)
        prior = self.booked.get(intent_id, Decimal(0))
        delta = cumulative - prior
        admitted = _dec(_need(intent, "quantity"), code=EVIDENCE_INVALID)
        if delta <= 0 or cumulative > admitted:
            raise ReplayRefused(EVIDENCE_INVALID)
        held, cost = self.positions.get(isin, [Decimal(0), Decimal(0)])
        side = _need(intent, "side")
        if side == "buy":
            held, cost = held + delta, cost + delta * price + charges
            self.symbols[isin] = str(_need(intent, "symbol"))
        elif side == "sell":
            if delta > held:
                raise ReplayRefused(EVIDENCE_INVALID)
            remaining = held - delta
            cost = Decimal(0) if remaining == 0 else max(Decimal(0), cost - cost * delta / held)
            held = remaining
        else:
            raise ReplayRefused(EVIDENCE_INVALID)
        self.positions[isin] = [held, cost]
        self.booked[intent_id] = cumulative
        if cumulative == admitted:
            self.terminal.add(intent_id)

    def snapshot(self, vm: _Vm) -> tuple[Any, Any]:
        """(AccountSnapshot, RiskFlags) as the VM takes them, at the point the fold has reached."""

        if self.pending:
            raise ReplayRefused(STATE_AMBIGUOUS)  # a booking is half written: holdings are not one thing yet
        holdings, costs = [], {}
        for isin, (held, cost) in sorted(self.positions.items()):
            if held > 0:
                holdings.append(vm.holding_cls(isin, _whole(held), cost))
                costs[isin] = cost
        orders = []
        for intent_id in sorted(self.reserved - self.terminal):
            intent = self.intents.get(intent_id)
            if intent is None:
                raise ReplayRefused(EVIDENCE_INVALID)  # a reservation for an intent that is not on the evidence
            unfilled = _dec(_need(intent, "quantity"), code=EVIDENCE_INVALID) - self.booked.get(intent_id, Decimal(0))
            if unfilled > 0:
                orders.append(
                    vm.open_order_cls(
                        self.identity(intent).isin,
                        str(_need(intent, "side")),
                        _whole(unfilled),
                        _dec(_need(intent, "limit_price"), code=EVIDENCE_INVALID),
                    )
                )
        account = vm.account_cls(holdings=tuple(holdings), open_orders=tuple(orders), trades=())
        flags = vm.flags_cls(
            halt=self.halt,
            ended=self.ended,
            mac_halt=MAC_HALT,
            account_mismatch=ACCOUNT_MISMATCH,
            stops=frozenset(self.stops),
            ledger_cost=costs,
        )
        return account, flags


# --- the Mac's verdict -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MacVerdict:
    decision: str  # ADMITTED, DENIED or NO_DECISION
    codes: tuple[str, ...]
    full: bool  # True when the Mac recorded every code, not only the first


_MAC_KINDS = frozenset({"admission", "denied", "reservation_refused", "reservation"})


def mac_verdicts(records: Sequence[Mapping[str, Any]]) -> dict[str, MacVerdict]:
    """The first Mac decision record per intent: an ``admission`` row, a ``denied`` or ``reservation_refused`` code, a reservation."""

    out: dict[str, MacVerdict] = {}
    for record in records:
        kind = record.get("kind")
        if kind not in _MAC_KINDS:
            continue
        intent_id = str(_need(record, "intent_id"))
        if intent_id in out:
            continue
        if kind == "reservation":
            out[intent_id] = MacVerdict(ADMITTED, (), True)
        elif kind in ("denied", "reservation_refused"):
            out[intent_id] = MacVerdict(DENIED, (str(_need(record, "code")),), False)
        else:
            decision = str(record.get("decision", "")).lower()
            if decision == ADMITTED:
                out[intent_id] = MacVerdict(ADMITTED, (), True)
            elif decision == DENIED:
                listed = record.get("refusal_codes")
                codes = tuple(str(c) for c in listed) if isinstance(listed, list) and listed else (str(record.get("reason_code")),)
                out[intent_id] = MacVerdict(DENIED, codes, isinstance(listed, list) and bool(listed))
    return out


def compare(mac: MacVerdict, vm_codes: tuple[str, ...]) -> str | None:
    """``None`` when the two verdicts agree, else the closed word naming why they do not."""

    if mac.decision == NO_DECISION:
        return "mac_decision_missing"
    if mac.decision == ADMITTED:
        return None if not vm_codes else "mac_admitted_vm_refused"
    mapped = []
    for code in mac.codes:
        if code not in MAC_TO_VM_CODE:
            return "mac_code_unmapped"
        mapped.append(MAC_TO_VM_CODE[code])
    if not vm_codes:
        return "mac_refused_vm_admitted"
    if mac.full:
        return None if tuple(mapped) == vm_codes else "reason_codes_differ"
    return None if mapped[0] == vm_codes[0] else "reason_codes_differ"


# --- results and the sink ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentVerdict:
    intent_id: str
    mac_decision: str
    mac_codes: tuple[str, ...]
    vm_codes: tuple[str, ...]
    agree: bool


@dataclass(frozen=True)
class Divergence:
    intent_id: str
    reason: str
    mac_decision: str
    mac_codes: tuple[str, ...]
    vm_codes: tuple[str, ...]


@dataclass(frozen=True)
class SessionReplay:
    run_id: str
    session_id: str
    verdict: str  # PASS or FAIL
    build_digest: str
    build_commit: str
    limits_sha256: str  # the IndiaLimits file hash (the manifest's and the signals')
    mint_limits_sha256: str  # the nine-key P-06 hash the Mac minted with
    intents_compared: int
    no_intents: bool
    verdicts: tuple[IntentVerdict, ...] = ()
    divergences: tuple[Divergence, ...] = ()
    defects: tuple[str, ...] = ()
    already_recorded: bool = False  # the session already holds an equal ``vm_replay`` record: nothing was appended


class ReplaySink(Protocol):
    """Where a session's result goes. ``__main__`` implements it over the evidence store and policy.

    ``record_session`` writes the per-session record. ``open_defect`` opens a defect on a divergence or a replay
    defect. ``pause_run`` pauses the run on a divergence so ``decide`` refuses until an operator reason.
    """

    def record_session(self, result: SessionReplay) -> None: ...

    def open_defect(self, code: str, session_id: str, detail: str) -> None: ...

    def pause_run(self, event: str, reason: str) -> None: ...


def _ist_of(record: Mapping[str, Any], vm: _Vm) -> datetime:
    try:
        stamp = datetime.fromisoformat(str(record["ist"]))
    except (KeyError, ValueError):
        raise ReplayRefused(EVIDENCE_INVALID) from None
    if stamp.tzinfo is None:
        raise ReplayRefused(EVIDENCE_INVALID)
    return stamp.astimezone(vm.ist)


def _vm_intent(vm: _Vm, record: Mapping[str, Any], limits_sha256: str, identity: _Identity) -> Any:
    side = str(_need(record, "side"))
    price = _dec(_need(record, "limit_price"), code=EVIDENCE_INVALID)
    values = {
        "intent_id": str(_need(record, "intent_id")),
        "proposal_id": str(_need(record, "intent_id")),
        "workspace": "india",
        "broker": "icici-breeze",
        "mode": "SHADOW",
        "exchange": "NSE",
        "product": "cash",
        "order_type": "limit",
        "validity": "day",
        "side": side,
        "stock_code": str(_need(record, "symbol")),
        "isin": identity.isin,
        "quantity": _whole(_dec(_need(record, "quantity"), code=EVIDENCE_INVALID)),
        "limit_price": format(price, "f"),
        "reason": "entry" if side == "buy" else "exit",
        "batch_id": None,
        "limits_sha256": limits_sha256,
        "params_sha256": _UNUSED_HEX,
        "key_id": _UNUSED_HEX,
    }
    return vm.intent_cls(**values)


def _vm_quote(vm: _Vm, record: Mapping[str, Any], pin: IntentPin, identity: _Identity) -> Any:
    if identity.series is not None and pin.quote.series != identity.series:
        raise ReplayRefused(SIGNAL_MISMATCH)  # a sell has no signal, so its pinned series stands
    return vm.quote_cls(
        stock_code=str(_need(record, "symbol")),
        isin=identity.isin,
        series=pin.quote.series,
        ltp=pin.quote.ltp,
        lower_circuit=pin.quote.lower_circuit,
        upper_circuit=pin.quote.upper_circuit,
        previous_close=pin.quote.previous_close,
        session_date=pin.quote.session_date,
    )


def _stored_replays(records: Sequence[Mapping[str, Any]], session_id: str) -> list[Mapping[str, Any]]:
    return [r for r in records if r.get("kind") == "vm_replay" and r.get("session_id") == session_id]


def _same_as_stored(stored: Mapping[str, Any], result: SessionReplay) -> bool:
    """The fields the record keeps, against a fresh result: verdict, build, both limits hashes, the count and the reasons."""

    return (
        stored.get("verdict") == result.verdict
        and stored.get("build_digest") == result.build_digest
        and stored.get("limits_sha256") == result.limits_sha256
        and stored.get("mint_limits_sha256") == result.mint_limits_sha256
        and stored.get("intents_compared") == result.intents_compared
        and list(stored.get("divergence_reasons", [])) == [d.reason for d in result.divergences]
    )


def replay_session(
    records: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    session_id: str,
    limits: ReplayLimits,
    manifest_hashes: Mapping[str, str],
    vm_source: Path,
    sink: ReplaySink | None = None,
) -> SessionReplay:
    """Replay one closed session. Raises ``ReplayRefused`` before any verdict exists; otherwise returns and records the result.

    The build, the nine-key mint hash and every quote and tick pin come from the evidence. The caller supplies only
    what the pinned private config holds (``limits``) and the manifest's hashes.

    A session that already holds a ``vm_replay`` record is not recorded again: an equal fresh result is reported
    ``already_recorded``, and a different one opens a ``replay_nondeterministic`` defect and is a FAIL.
    """

    if session_id not in replay_sessions(records):  # the open-run day or an operator day is not replayed, and records nothing
        raise ReplayRefused(NOT_DECIDE_DAY)
    if not any(r.get("kind") == "session_close" and r.get("session_id") == session_id for r in records):
        raise ReplayRefused(SESSION_OPEN)
    build, mint_limits_sha256 = build_from_evidence(records, session_id)
    vm = load_recorded_build(vm_source, build)
    try:
        signals = _signal_index(records)
        checked = checked_limits(
            vm, limits.fields, limits.file_sha256, mint_limits_sha256, manifest_hashes, records, session_id, signals
        )
        pins = pins_from_evidence(records, session_id)
        macs = mac_verdicts(records)
        fold = _Fold(signals)
        verdicts: list[IntentVerdict] = []
        divergences: list[Divergence] = []
        defects: list[str] = []
        seen = 0
        for record in sorted(records, key=_seq):
            is_ours = record.get("kind") == "intent" and record.get("session_id") == session_id
            if is_ours and not divergences and not defects:
                seen += 1
                outcome = _replay_intent(vm, checked, pins, fold, record, macs)
                if isinstance(outcome, Divergence):
                    divergences.append(outcome)
                    verdicts.append(IntentVerdict(outcome.intent_id, outcome.mac_decision, outcome.mac_codes, outcome.vm_codes, False))
                elif isinstance(outcome, str):
                    defects.append(outcome)
                else:
                    verdicts.append(outcome)
            fold.apply(record)
        result = SessionReplay(
            run_id=run_id,
            session_id=session_id,
            verdict=FAIL if divergences or defects else PASS,
            build_digest=build.digest,
            build_commit=build.commit,
            limits_sha256=limits.file_sha256,
            mint_limits_sha256=mint_limits_sha256,
            intents_compared=len(verdicts),
            no_intents=seen == 0,
            verdicts=tuple(verdicts),
            divergences=tuple(divergences),
            defects=tuple(defects),
        )
    finally:
        vm.close()  # the private copy of the build's files
    stored = _stored_replays(records, session_id)
    if stored:
        if all(_same_as_stored(r, result) for r in stored):
            if sink is not None and result.verdict == FAIL:
                _finish_enforcement(records, session_id, result, max(_seq(r) for r in stored), sink)
            return dataclasses.replace(result, already_recorded=True)
        result = dataclasses.replace(result, verdict=FAIL, defects=result.defects + (NONDETERMINISTIC,))
        if sink is not None:  # the nondeterminism defect, and the defect and pause of any divergence this pass found
            _finish_enforcement(records, session_id, result, max(_seq(r) for r in stored), sink)
        return result
    if sink is not None:
        sink.record_session(result)
        for action in _enforcement(session_id, result):
            _apply(sink, session_id, action)
    return result


def _enforcement(session_id: str, result: SessionReplay) -> list[tuple[str, str, str]]:
    """The writes a FAIL owes after its ``vm_replay`` record, in order: ``(kind, code_or_event, detail_or_reason)``.

    A divergence opens a defect and pauses the run (scope all); a defect of the session opens its own defect.
    """

    actions: list[tuple[str, str, str]] = []
    for divergence in result.divergences:
        actions.append(("defect", DIVERGENCE, divergence.reason))
        actions.append(("pause", DIVERGENCE, f"{session_id}:{divergence.intent_id}:{divergence.reason}"))
    for defect in result.defects:
        actions.append(("defect", defect, defect))
    return actions


def _apply(sink: ReplaySink, session_id: str, action: tuple[str, str, str]) -> None:
    kind, first, second = action
    if kind == "defect":
        sink.open_defect(first, session_id, second)
    else:
        sink.pause_run(first, second)


def _finish_enforcement(
    records: Sequence[Mapping[str, Any]], session_id: str, result: SessionReplay, recorded_seq: int, sink: ReplaySink
) -> None:
    """A stored FAIL whose defect or pause writes never landed (the process stopped after the record): write what is missing.

    Defects are always completed (the D-23 verdict reads them). A missing pause is written only when no ``resume``
    came after the stored record: an operator who resumed after the record has already decided, and a re-run must
    not undo that. Nothing is written for what is already there, so a further re-run writes nothing.
    """

    held_defects = {
        (r.get("code"), r.get("detail")) for r in records if r.get("kind") == "replay_defect" and r.get("session_id") == session_id
    }
    held_pauses = {r.get("reason") for r in records if r.get("kind") == "pause" and r.get("event") == DIVERGENCE}
    resumed = any(r.get("kind") == "resume" and _seq(r) > recorded_seq for r in records)
    for action in _enforcement(session_id, result):
        kind, first, second = action
        if kind == "defect" and (first, second) in held_defects:
            continue
        if kind == "pause" and (second in held_pauses or resumed):
            continue
        _apply(sink, session_id, action)


def _replay_intent(
    vm: _Vm,
    limits: Any,
    pins: Mapping[str, IntentPin],
    fold: _Fold,
    record: Mapping[str, Any],
    macs: Mapping[str, MacVerdict],
) -> IntentVerdict | Divergence | str:
    intent_id = str(_need(record, "intent_id"))
    pin = pins.get(intent_id)
    if pin is None:
        raise ReplayRefused(PINS_MISSING)
    decided = _ist_of(record, vm)
    identity = fold.identity(record)
    account, flags = fold.snapshot(vm)
    codes = vm.evaluate(
        limits,
        flags,
        account,
        _vm_quote(vm, record, pin, identity),
        decided,
        _vm_intent(vm, record, limits.sha256, identity),
        kill_enabled=REPLAY_KILL_ENABLED,
        tick_reference=pin.tick_reference,
        # No tick_table: the VM resolves its default, the data file beside the loaded build (hashed with orders/).
    )
    vm_codes = tuple(str(c) for c in codes)
    if KILL_SWITCH in vm_codes:
        return KILL_SWITCH_IN_VERDICT
    mac = macs.get(intent_id, MacVerdict(NO_DECISION, (), False))
    reason = compare(mac, vm_codes)
    if reason is not None:
        return Divergence(intent_id, reason, mac.decision, mac.codes, vm_codes)
    return IntentVerdict(intent_id, mac.decision, mac.codes, vm_codes, True)


# --- the run --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunReplay:
    run_id: str
    verdict: str
    sessions: tuple[SessionReplay, ...]
    missing: tuple[str, ...] = ()
    refused: Mapping[str, str] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    not_replayed: tuple[str, ...] = ()  # sessions with no decide (r3): listed, never dropped, never replayed

    @property
    def build_digests(self) -> tuple[str, ...]:
        return tuple(sorted({s.build_digest for s in self.sessions}))


def evidence_sessions(records: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted({str(r["session_id"]) for r in records if r.get("session_id")}))


_DECIDE_KINDS = frozenset({"signal", "non_evaluation_day", "intent"})


def replay_sessions(records: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """The decide days: sessions that hold a ``signal``, a ``non_evaluation_day`` or an ``intent`` record.

    A ``vm_build`` alone never makes a replay day. The open-run record sits on the run-start session, which gets no
    ``session_close`` when the first decide is on a later day, so counting it would refuse ``replay_session_open``.
    """

    return tuple(sorted({str(r["session_id"]) for r in records if r.get("kind") in _DECIDE_KINDS and r.get("session_id")}))


def not_replayed_sessions(records: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    """EVERY session id in the evidence outside ``replay_sessions``, whatever records it holds (no record-kind allowlist).

    The open-run day holds ``run_start`` and ``vm_build``; a decide that paused before any signal can hold ``vm_build``,
    ``pause``, ``expiry`` or booking rows; an operator day holds ``resume``. None of them is a replay day and none is dropped.
    """

    decided = set(replay_sessions(records))
    return tuple(sorted(set(evidence_sessions(records)) - decided))


def run_verdict(
    run_id: str,
    sessions: Sequence[str],
    results: Mapping[str, SessionReplay],
    refused: Mapping[str, str] | None = None,
    not_replayed: Sequence[str] = (),
) -> RunReplay:
    """The D-23 verdict for a run: PASS only when every session has a PASS record, all at one build hash.

    A session with evidence and no replay record is ``replay_day_missing`` and the run FAILs. There is no waiver.
    """

    refused = dict(refused or {})
    missing = tuple(s for s in sorted(set(sessions)) if s not in results and s not in refused)
    reasons: list[str] = []
    if missing:
        reasons.append(DAY_MISSING)
    if refused:
        reasons.append(SESSION_REFUSED)
    ordered = tuple(results[s] for s in sorted(results))
    if any(r.verdict != PASS for r in ordered):
        reasons.append(DIVERGENCE)
    if len({r.build_digest for r in ordered}) > 1:
        reasons.append(BUILD_HASH_MIXED)
    if not ordered and not missing and not refused:
        reasons.append(DAY_MISSING)  # a run with nothing replayed has not passed
    return RunReplay(
        run_id, FAIL if reasons else PASS, ordered, missing, refused, tuple(dict.fromkeys(reasons)), tuple(sorted(not_replayed))
    )


def replay_run(
    records: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    limits: ReplayLimits,
    manifest_hashes: Mapping[str, str],
    vm_source: Path,
    sink: ReplaySink | None = None,
) -> RunReplay:
    """``--all``: every decide day of the evidence (``replay_sessions``). None is skipped.

    A session that refuses is listed with its refusal code and the run FAILs; it writes no verdict. The sessions
    outside the decide days are listed as ``not_replayed:no_decide``.
    """

    wanted = replay_sessions(records)
    results: dict[str, SessionReplay] = {}
    refused: dict[str, str] = {}
    for session_id in wanted:
        try:
            results[session_id] = replay_session(
                records, run_id=run_id, session_id=session_id, limits=limits, manifest_hashes=manifest_hashes,
                vm_source=vm_source, sink=sink,
            )
        except ReplayRefused as exc:
            refused[session_id] = exc.code
    return run_verdict(run_id, wanted, results, refused, not_replayed_sessions(records))


def _stored_result(record: Mapping[str, Any]) -> SessionReplay:
    reasons = [str(r) for r in record.get("divergence_reasons", [])]
    intent_id = str(record.get("divergent_intent_id", "-"))
    return SessionReplay(
        run_id=str(record.get("run_id", "")),
        session_id=str(record["session_id"]),
        verdict=str(record["verdict"]),
        build_digest=str(record["build_digest"]),
        build_commit=str(record["build_commit"]),
        limits_sha256=str(record["limits_sha256"]),
        mint_limits_sha256=str(record["mint_limits_sha256"]),
        intents_compared=int(record["intents_compared"]),
        no_intents=bool(record["no_intents"]),
        divergences=tuple(Divergence(intent_id, reason, "recorded", (), ()) for reason in reasons),
        defects=tuple(str(c) for c in record.get("defects", [])),
        already_recorded=True,
    )


def recorded_run_verdict(records: Sequence[Mapping[str, Any]], refused: Mapping[str, str] | None = None) -> RunReplay:
    """The D-23 verdict from the STORED records only (what 64-05 Task 2 and the entry gate read).

    PASS needs all of: at least one decide day; every decide day has a ``vm_replay`` record; every such record is PASS;
    no ``replay_defect`` record exists for any session; no ``vm_replay`` record for a session that is not a decide day
    (``replay_record_outside_decide_days``); one build digest across the run. A FAIL record is sticky: it is
    never replaced by a later PASS record for the same session, and a ``resume`` of the pause it raised changes nothing
    here (this reads replay records, not pause state). ``refused`` holds the refusal codes of the current pass. Every one
    is kept, even for a session that holds a stored PASS: a later change of the private config must not be hidden by an
    earlier record, and the run FAILs (``replay_session_refused``).
    """

    run_id = next((str(r["run_id"]) for r in records if r.get("run_id")), "")
    wanted = replay_sessions(records)
    stored = [r for r in records if r.get("kind") == "vm_replay"]
    chosen: dict[str, Mapping[str, Any]] = {}
    for record in stored:
        session = str(record["session_id"])
        held = chosen.get(session)
        if held is None or (held.get("verdict") == PASS and record.get("verdict") != PASS):
            chosen[session] = record  # a FAIL replaces a PASS and never the other way round
    stray = sorted(session for session in chosen if session not in wanted)
    results = {session: _stored_result(record) for session, record in chosen.items() if session in wanted}
    open_refusals = dict(refused or {})  # a stored PASS never hides a refusal of this pass
    run = run_verdict(run_id, wanted, results, open_refusals, not_replayed_sessions(records))
    reasons = list(run.reasons)
    if stray:  # a replay record for a session that is not a decide day proves nothing and must not count as a replayed day
        reasons.append(STRAY_RECORD)
    if any(r.get("kind") == "replay_defect" for r in records):
        reasons.append(DEFECT)
    if len({str(r["build_digest"]) for r in stored}) > 1:
        reasons.append(BUILD_HASH_MIXED)
    reasons = list(dict.fromkeys(reasons))
    return dataclasses.replace(run, verdict=FAIL if reasons else PASS, reasons=tuple(reasons))


def render_report(run: RunReplay) -> str:
    """The text of ``64-VM-REPLAY.md``: build hash, both limits hashes, intents compared, verdict and divergences per session.

    Hashes, commits, counts and closed codes only: no limit value, account id or key id ever reaches this text.
    """

    lines = [
        "# 64 VM replay (D-23)",
        "",
        f"Evidence label: {D06_LABEL}.",
        "",
        f"Run: {run.run_id}",
        f"D-23 verdict: {run.verdict}",
        f"Reasons: {', '.join(run.reasons) if run.reasons else 'none'}",
        f"Build hashes: {', '.join(run.build_digests) if run.build_digests else 'none'}",
        "",
        "| Session | Verdict | Build commit | Build hash | Limits hash | Mint limits hash | Intents compared | Note |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in run.sessions:
        note = "no_intents" if s.no_intents else ", ".join(list(s.defects) + [d.reason for d in s.divergences]) or "-"
        lines.append(
            f"| {s.session_id} | {s.verdict} | {s.build_commit} | {s.build_digest} | {s.limits_sha256} | "
            f"{s.mint_limits_sha256} | {s.intents_compared} | {note} |"
        )
    for session_id in run.missing:
        lines.append(f"| {session_id} | {FAIL} | - | - | - | - | 0 | {DAY_MISSING} |")
    for session_id, code in sorted(run.refused.items()):
        lines.append(f"| {session_id} | {FAIL} | - | - | - | - | 0 | {code} |")
    for session_id in run.not_replayed:
        lines.append(f"| {session_id} | - | - | - | - | - | 0 | {NOT_REPLAYED} |")
    divergent = [(s, d) for s in run.sessions for d in s.divergences]
    if divergent:
        lines += ["", "## Divergences", ""]
        for s, d in divergent:
            seen = "" if d.mac_decision == "recorded" else f"; Mac {d.mac_decision} {list(d.mac_codes)}; VM {list(d.vm_codes)}"
            lines.append(f"- {s.session_id} {d.intent_id}: {d.reason}{seen}")
    lines += ["", "No waiver exists for a divergence, a missing day or a refused session.", ""]
    return "\n".join(lines)


__all__ = [
    "ADMITTED",
    "BUILD_HASH_MIXED",
    "BUILD_MISMATCH",
    "BuildIdentity",
    "D06_LABEL",
    "DAY_MISSING",
    "DEFECT",
    "DENIED",
    "DIVERGENCE",
    "Divergence",
    "FAIL",
    "IntentPin",
    "IntentVerdict",
    "KILL_SWITCH_IN_VERDICT",
    "LIMITS_MISMATCH",
    "MAC_TO_VM_CODE",
    "MacVerdict",
    "NONDETERMINISTIC",
    "NOT_REPLAYED",
    "NO_DECISION",
    "PASS",
    "PIN_FIELDS",
    "PINS_INVALID",
    "PINS_MISSING",
    "QuotePin",
    "REPLAY_KILL_ENABLED",
    "ReplayLimits",
    "ReplayRefused",
    "ReplaySink",
    "RunReplay",
    "SESSION_OPEN",
    "NOT_DECIDE_DAY",
    "STRAY_RECORD",
    "SessionReplay",
    "build_from_evidence",
    "checked_limits",
    "compare",
    "evidence_sessions",
    "load_recorded_build",
    "mac_verdicts",
    "not_replayed_sessions",
    "orders_tree_sha256",
    "pins_from_evidence",
    "read_git_commit",
    "recorded_run_verdict",
    "render_report",
    "replay_run",
    "replay_session",
    "replay_sessions",
    "run_verdict",
]
