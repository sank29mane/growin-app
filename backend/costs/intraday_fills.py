"""Intraday limit-fill model over post-submission minute bars (Phase 64, 64-INTRADAY-DESIGN).

This is a separate model from ``fills`` (the daily-bar one) and edits neither it nor its
schedule. It produces conservative simulated fills from evidence. It does not estimate the
real fill probability, and every result says so.

Two stages (R-33):

* Stage A, ``assess_day``, runs once per symbol-day over the whole payload and yields one
  hashed ``DayIntegrity`` verdict. It never reads a submission time, lifetime, side, limit,
  quantity or ``k``. A failing verdict makes every attempt of that symbol-day INCONCLUSIVE.
* Stage B, ``simulate_intraday``, runs per attempt over ``AdmittedBars`` only. ``AdmittedBars``
  can be built only by ``admit_bars`` from a passing verdict, and it re-checks every bar.

Rules are named ``R-<n>`` after the design record. Evidence that cannot support a conclusion
(missing, stale, gapped, capped, empty, volume-inconsistent, unproven) is INCONCLUSIVE with a
typed code. It is never a fill and never a miss.

Label semantics are unproven for all of Phase 64 (OI-20), so the admission rule is the
conservative one: a bar labelled ``T`` counts only if ``T - interval`` is at or after the
submission and ``T + interval`` is at or before the lifetime end. A schedule that claims
proven labels is refused unless both an independent evidence reference and the operator's
approval reference are present; even then this module applies the conservative rule. The
relaxation itself is a later approved task.

Pure: no I/O beyond the schedule loader, no clock, no network, no randomness, Decimal only.
The caller fetches the evidence, reads the bhavcopy and the CAS-class table, and owns the clock.
"""

from __future__ import annotations

import decimal
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from .cas_class import CAS, CAS_CLASSES, NON_CAS, CasClassRow
from .core import (
    COST_CONTEXT,
    IST,
    InputError,
    ScheduleError,
    Side,
    canonical_json,
    floor_to,
    load_strict_json,
    positive_decimal,
    require_aware,
    require_date,
    require_sha256,
    require_text,
    seal,
    sha256_hex,
    strict_decimal,
    strict_int,
)
from .fills import (
    FILLED_AT_LIMIT,
    MISSED_LOCKED_AT_BAND,
    MISSED_THRESHOLD,
    MISSED_VOLUME_CAP_ZERO,
    NO_FILL_AMBIGUOUS_SINGLE_PRICE,
    NO_FILL_BAND_DATE_MISMATCH,
    NO_FILL_BAND_UNAVAILABLE,
    PARTIAL_VOLUME_CAP,
    REJECTED_OFF_TICK,
    REJECTED_OUTSIDE_BAND,
    BandUnavailable,
    PriceBand,
    TickSize,
)

# ---- INCONCLUSIVE taxonomy (design section 8: 22 codes, one per cause) -------------------

NO_EVIDENCE_FETCHED = "no_evidence_fetched"
EVIDENCE_FETCHED_TOO_LATE = "evidence_fetched_too_late"
INTERVAL_NOT_APPROVED = "interval_not_approved"
PRE_SUBMISSION_BAR_OFFERED = "pre_submission_bar_offered"
GAP_IN_SEQUENCE = "gap_in_sequence"
DUPLICATE_BAR = "duplicate_bar"
BAR_ORDER = "bar_order"
CAPPED_ANSWER = "capped_answer"
EMPTY_ANSWER = "empty_answer"
VOLUME_CHECK_FAILED = "volume_check_failed"
LABEL_SEMANTICS_UNPROVEN = "label_semantics_unproven"
VOLUME_REFERENCE_MISSING = "volume_reference_missing"
PRICE_RANGE_CHECK_FAILED = "price_range_check_failed"
EVIDENCE_FETCHED_BEFORE_CLOSE = "evidence_fetched_before_close"
EVIDENCE_SCOPE_MISMATCH = "evidence_scope_mismatch"
LABEL_MALFORMED = "label_malformed"
BAR_INCONSISTENT = "bar_inconsistent"
SESSION_NOT_STANDARD = "session_not_standard"
EVIDENCE_HASH_UNVERIFIED = "evidence_hash_unverified"
NO_ADMISSIBLE_BAR = "no_admissible_bar"
SIBLING_UNRESOLVED = "sibling_unresolved"
SESSION_SCOPE_UNKNOWN = "session_scope_unknown"

INCONCLUSIVE_CODES = (
    NO_EVIDENCE_FETCHED,
    EVIDENCE_FETCHED_TOO_LATE,
    INTERVAL_NOT_APPROVED,
    PRE_SUBMISSION_BAR_OFFERED,
    GAP_IN_SEQUENCE,
    DUPLICATE_BAR,
    BAR_ORDER,
    CAPPED_ANSWER,
    EMPTY_ANSWER,
    VOLUME_CHECK_FAILED,
    LABEL_SEMANTICS_UNPROVEN,
    VOLUME_REFERENCE_MISSING,
    PRICE_RANGE_CHECK_FAILED,
    EVIDENCE_FETCHED_BEFORE_CLOSE,
    EVIDENCE_SCOPE_MISMATCH,
    LABEL_MALFORMED,
    BAR_INCONSISTENT,
    SESSION_NOT_STANDARD,
    EVIDENCE_HASH_UNVERIFIED,
    NO_ADMISSIBLE_BAR,
    SIBLING_UNRESOLVED,
    SESSION_SCOPE_UNKNOWN,
)
NO_EVIDENCE_REASONS = ("route_unavailable", "relay_error", "session_expired", "budget_exhausted")

CAPPED_ROW_COUNT = 1000
SAME_DAY_FETCH_RULE = "same_day"
ASSUMPTION_LABELS = (
    "OHLCV assumption (OI-3)",
    "conservative simulated fill from evidence",
    "not evidence of actual fill probability",
)
# Evidence kinds that may back a proven-label claim. A Breeze bar snapshot (the 64-02 probe
# record included) is deliberately absent: two snapshots cannot fix a candle boundary (R-27).
LABEL_EVIDENCE_KINDS = ("icici_written_statement", "trade_timestamp_comparison")
INTERVALS = {"1minute": timedelta(minutes=1)}

INTRADAY_SCHEMA = "growin.costs.intraday_fill_scenarios/1"
DEFAULT_INTRADAY_SCENARIOS_PATH = Path(__file__).parent / "schedules" / "intraday_bar_fill_scenarios.json"


class IntradayOutcome(str, Enum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    MISSED = "MISSED"
    REJECTED = "REJECTED"
    NO_ASSUMED_FILL = "NO_ASSUMED_FILL"
    INCONCLUSIVE = "INCONCLUSIVE"


class IntradayRefusal(InputError):
    """An admission step refused; ``simulate_intraday`` maps it to an INCONCLUSIVE ``code``."""

    code = ""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


# ---- scenario schedule -------------------------------------------------------------------

_FILE_KEYS = (
    "schema",
    "version",
    "assumption_note",
    "fill_price_rule",
    "interval",
    "lifetime_end_ist",
    "admission_rule",
    "label_semantics",
    "label_evidence_ref",
    "label_approval_ref",
    "gap_policy",
    "volume_basis",
    "lower_bound_status",
    "volume_ratio_band",
    "day_check_window_ist",
    "price_range_tolerance_ticks",
    "session_timing_version",
    "continuous_end_ist",
    "no_band_single_price",
    "earliest_fetch_ist",
    "latest_fetch_ist",
    "next_day_fetch",
    "scenarios",
)
_SCENARIO_KEYS = ("id", "k_ticks", "volume_participation")
_BAND_KEYS = ("lower", "upper")
_NEXT_DAY_KEYS = ("rule", "window_start_ist", "window_end_ist", "max_calls")
_EVIDENCE_REF_KEYS = ("source_kind", "record_hash")
_HHMM = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d", re.ASCII)
_LABEL = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):00", re.ASCII)


@dataclass(frozen=True)
class VolumeBand:
    lower: Decimal | None
    upper: Decimal


@dataclass(frozen=True)
class LabelEvidenceRef:
    source_kind: str
    record_hash: str


@dataclass(frozen=True)
class NextDayFetch:
    rule: str
    window_start: time
    window_end: time
    max_calls: int


@dataclass(frozen=True)
class IntradayScenario:
    scenario_id: str
    k_ticks: int
    volume_participation: Decimal


@dataclass(frozen=True)
class IntradayScenarioSet:
    version: str
    schedule_hash: str
    assumption_note: str
    interval: str
    lifetime_end: time
    admission_rule: str
    label_semantics: str
    label_evidence_ref: LabelEvidenceRef | None
    label_approval_ref: str | None
    gap_policy: str
    volume_basis: str
    lower_bound_status: str
    cas_band: VolumeBand
    non_cas_band: VolumeBand
    day_window_start: time
    day_window_end: time
    price_tolerance_ticks: Decimal
    session_timing_version: str
    cas_continuous_end: time
    non_cas_continuous_end: time
    earliest_fetch: time
    latest_fetch: time
    next_day_fetch: NextDayFetch | None
    scenarios: tuple[IntradayScenario, ...]

    @property
    def interval_delta(self) -> timedelta:
        return INTERVALS[self.interval]

    def band_for(self, cas_class: str) -> VolumeBand:
        if cas_class == CAS:
            return self.cas_band
        if cas_class == NON_CAS:
            return self.non_cas_band
        raise InputError(f"unknown CAS class {cas_class!r}")

    def continuous_end_for(self, cas_class: str) -> time:
        return self.cas_continuous_end if cas_class == CAS else self.non_cas_continuous_end

    def label_claim_refused(self) -> bool:
        """R-27: True when the schedule claims proven labels without independent, approved backing."""
        if self.label_semantics == "unproven":
            return False
        ref = self.label_evidence_ref
        return (
            ref is None
            or ref.source_kind not in LABEL_EVIDENCE_KINDS
            or self.label_approval_ref is None
        )


def _exact(raw: Mapping[str, Any], keys: tuple[str, ...], path: str) -> None:
    for key in raw:
        if key not in keys:
            raise ScheduleError(f"{path}.{key}: unknown key")
    for key in keys:
        if key not in raw:
            raise ScheduleError(f"{path}.{key}: missing required key")


def _object(raw: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(raw, dict):
        raise ScheduleError(f"{path}: expected an object")
    return raw


def _text(raw: Mapping[str, Any], key: str, path: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ScheduleError(f"{path}.{key}: expected a non-empty string")
    return value


def _fixed(raw: Mapping[str, Any], key: str, expected: str, path: str) -> str:
    value = _text(raw, key, path)
    if value != expected:
        raise ScheduleError(f"{path}.{key}: only {expected!r} is supported, got {value!r}")
    return value


def _hhmm(value: Any, path: str) -> time:
    if not isinstance(value, str) or not _HHMM.fullmatch(value):
        raise ScheduleError(f"{path}: expected HH:MM")
    return time.fromisoformat(value)


def _decimal_text(value: Any, path: str) -> Decimal:
    if not isinstance(value, str):
        raise ScheduleError(f"{path}: numerics must be JSON strings")
    try:
        return strict_decimal(value, path)
    except InputError as exc:
        raise ScheduleError(str(exc)) from exc


def _sha_text(value: Any, path: str) -> str:
    try:
        return require_sha256(value, path)
    except InputError as exc:
        raise ScheduleError(str(exc)) from exc


def _parse_band(raw: Any, path: str, status: str) -> VolumeBand:
    band = _object(raw, path)
    _exact(band, _BAND_KEYS, path)
    upper = _decimal_text(band["upper"], f"{path}.upper")
    if upper <= 0:
        raise ScheduleError(f"{path}.upper: must be above 0")
    lower: Decimal | None = None
    if status == "provisional":
        if band["lower"] is not None:
            raise ScheduleError(f"{path}.lower: a provisional schedule pins lower as null")
    else:
        if band["lower"] is None:
            raise ScheduleError(f"{path}.lower: a final schedule needs a numeric lower bound per class")
        lower = _decimal_text(band["lower"], f"{path}.lower")
        if not 0 < lower < upper:
            raise ScheduleError(f"{path}.lower: must be above 0 and below the upper bound")
    return VolumeBand(lower, upper)


def _parse_next_day(raw: Any) -> NextDayFetch | None:
    if raw is None:
        return None
    path = "$.next_day_fetch"
    spec = _object(raw, path)
    _exact(spec, _NEXT_DAY_KEYS, path)
    start = _hhmm(spec["window_start_ist"], f"{path}.window_start_ist")
    end = _hhmm(spec["window_end_ist"], f"{path}.window_end_ist")
    if not start < end:
        raise ScheduleError(f"{path}: window_start_ist must precede window_end_ist")
    max_calls = spec["max_calls"]
    if isinstance(max_calls, bool) or not isinstance(max_calls, int) or max_calls < 1:
        raise ScheduleError(f"{path}.max_calls: expected an integer of at least 1")
    return NextDayFetch(_fixed(spec, "rule", "next_day_exception_v1", path), start, end, max_calls)


def _parse_scenarios(raw: Any) -> tuple[IntradayScenario, ...]:
    if not isinstance(raw, list) or not raw:
        raise ScheduleError("$.scenarios: expected a non-empty list")
    built: list[IntradayScenario] = []
    for n, item in enumerate(raw):
        path = f"$.scenarios[{n}]"
        scenario = _object(item, path)
        _exact(scenario, _SCENARIO_KEYS, path)
        k_ticks = scenario["k_ticks"]
        if isinstance(k_ticks, bool) or not isinstance(k_ticks, int) or k_ticks < 1:
            raise ScheduleError(f"{path}.k_ticks: expected an integer of at least 1")
        participation = _decimal_text(scenario["volume_participation"], f"{path}.volume_participation")
        if not 0 < participation <= 1:
            raise ScheduleError(f"{path}.volume_participation: must be above 0 and at most 1")
        built.append(IntradayScenario(_text(scenario, "id", path), k_ticks, participation))
    if len({s.scenario_id for s in built}) != len(built):
        raise ScheduleError("$.scenarios: duplicate scenario id")
    if len({s.k_ticks for s in built}) != len(built):
        raise ScheduleError("$.scenarios: duplicate k_ticks")
    return tuple(built)


def parse_intraday_scenarios(text: str, name: str = "intraday scenarios") -> IntradayScenarioSet:
    with decimal.localcontext(COST_CONTEXT):
        raw = _object(load_strict_json(text, ScheduleError, name), "$")
        _exact(raw, _FILE_KEYS, "$")
        if raw["schema"] != INTRADAY_SCHEMA:
            raise ScheduleError(f"$.schema: expected {INTRADAY_SCHEMA!r}")
        _fixed(raw, "fill_price_rule", "at_limit", "$")
        interval = _text(raw, "interval", "$")
        if interval not in INTERVALS:
            raise ScheduleError(f"$.interval: not an approved interval, got {interval!r}")
        _fixed(raw, "admission_rule", "label_minus_interval_ge_submission", "$")
        label_semantics = _text(raw, "label_semantics", "$")
        if label_semantics not in ("unproven", "proven"):
            raise ScheduleError(f"$.label_semantics: expected 'unproven' or 'proven', got {label_semantics!r}")
        evidence_ref: LabelEvidenceRef | None = None
        if raw["label_evidence_ref"] is not None:
            ref = _object(raw["label_evidence_ref"], "$.label_evidence_ref")
            _exact(ref, _EVIDENCE_REF_KEYS, "$.label_evidence_ref")
            evidence_ref = LabelEvidenceRef(
                _text(ref, "source_kind", "$.label_evidence_ref"),
                _sha_text(ref["record_hash"], "$.label_evidence_ref.record_hash"),
            )
        approval_ref = (
            None
            if raw["label_approval_ref"] is None
            else _sha_text(raw["label_approval_ref"], "$.label_approval_ref")
        )
        _fixed(raw, "gap_policy", "inconclusive", "$")
        _fixed(raw, "volume_basis", "threshold_crossing_post_submission_ohlcv", "$")
        _fixed(raw, "no_band_single_price", "not_credited_ambiguous", "$")
        status = _text(raw, "lower_bound_status", "$")
        if status not in ("provisional", "final"):
            raise ScheduleError(f"$.lower_bound_status: expected 'provisional' or 'final', got {status!r}")
        bands = _object(raw["volume_ratio_band"], "$.volume_ratio_band")
        _exact(bands, CAS_CLASSES, "$.volume_ratio_band")
        window = raw["day_check_window_ist"]
        if not isinstance(window, list) or len(window) != 2:
            raise ScheduleError("$.day_check_window_ist: expected [start, end]")
        window_start = _hhmm(window[0], "$.day_check_window_ist[0]")
        window_end = _hhmm(window[1], "$.day_check_window_ist[1]")
        if not window_start < window_end:
            raise ScheduleError("$.day_check_window_ist: start must precede end")
        tolerance = _decimal_text(raw["price_range_tolerance_ticks"], "$.price_range_tolerance_ticks")
        if tolerance < 0:
            raise ScheduleError("$.price_range_tolerance_ticks: must not be negative")
        ends = _object(raw["continuous_end_ist"], "$.continuous_end_ist")
        _exact(ends, CAS_CLASSES, "$.continuous_end_ist")
        earliest = _hhmm(raw["earliest_fetch_ist"], "$.earliest_fetch_ist")
        latest = _hhmm(raw["latest_fetch_ist"], "$.latest_fetch_ist")
        if not earliest < latest:
            raise ScheduleError("$.earliest_fetch_ist must precede $.latest_fetch_ist")
        return IntradayScenarioSet(
            version=_text(raw, "version", "$"),
            schedule_hash=sha256_hex(canonical_json(raw)),
            assumption_note=_text(raw, "assumption_note", "$"),
            interval=interval,
            lifetime_end=_hhmm(raw["lifetime_end_ist"], "$.lifetime_end_ist"),
            admission_rule=raw["admission_rule"],
            label_semantics=label_semantics,
            label_evidence_ref=evidence_ref,
            label_approval_ref=approval_ref,
            gap_policy=raw["gap_policy"],
            volume_basis=raw["volume_basis"],
            lower_bound_status=status,
            cas_band=_parse_band(bands[CAS], "$.volume_ratio_band.cas", status),
            non_cas_band=_parse_band(bands[NON_CAS], "$.volume_ratio_band.non_cas", status),
            day_window_start=window_start,
            day_window_end=window_end,
            price_tolerance_ticks=tolerance,
            session_timing_version=_text(raw, "session_timing_version", "$"),
            cas_continuous_end=_hhmm(ends[CAS], "$.continuous_end_ist.cas"),
            non_cas_continuous_end=_hhmm(ends[NON_CAS], "$.continuous_end_ist.non_cas"),
            earliest_fetch=earliest,
            latest_fetch=latest,
            next_day_fetch=_parse_next_day(raw["next_day_fetch"]),
            scenarios=_parse_scenarios(raw["scenarios"]),
        )


def load_intraday_scenarios(path: Path | None = None) -> IntradayScenarioSet:
    source = DEFAULT_INTRADAY_SCENARIOS_PATH if path is None else Path(path)
    return parse_intraday_scenarios(source.read_text(encoding="utf-8"), source.name)


# ---- inputs ------------------------------------------------------------------------------


@dataclass(frozen=True)
class MinuteBar:
    """One upstream row. Values are not range-checked here: Stage A turns bad rows into INCONCLUSIVE."""

    label: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def __post_init__(self) -> None:
        if not isinstance(self.label, str):
            raise InputError("bar.label must be a string")
        for name in ("open", "high", "low", "close"):
            if not isinstance(getattr(self, name), Decimal):
                raise InputError(f"bar.{name} must be a Decimal")
        if isinstance(self.volume, bool) or not isinstance(self.volume, int):
            # A non-int volume is a caller bug, not a market fact. Negative ints reach Stage A.
            raise InputError("bar.volume must be an int")


@dataclass(frozen=True)
class IntradayEvidence:
    """What the relay client returned for one symbol and date, plus the fetch facts."""

    stock_code: str
    session_date: date
    interval: str
    source_sha256: str
    sha256_verified: bool
    fetched_at: datetime
    route: str
    request_params: tuple[tuple[str, str], ...]
    empty_flag: bool
    bars: tuple[MinuteBar, ...]
    next_session_date: date | None = None

    def __post_init__(self) -> None:
        require_text(self.stock_code, "evidence.stock_code")
        require_date(self.session_date, "evidence.session_date")
        require_text(self.interval, "evidence.interval")
        require_sha256(self.source_sha256, "evidence.source_sha256")
        if not isinstance(self.sha256_verified, bool) or not isinstance(self.empty_flag, bool):
            raise InputError("evidence.sha256_verified and evidence.empty_flag must be bool")
        require_aware(self.fetched_at, "evidence.fetched_at")
        require_text(self.route, "evidence.route")
        if not isinstance(self.request_params, tuple) or not isinstance(self.bars, tuple):
            raise InputError("evidence.request_params and evidence.bars must be tuples")
        if not all(isinstance(bar, MinuteBar) for bar in self.bars):
            raise InputError("evidence.bars must hold MinuteBar rows")
        if self.next_session_date is not None:
            require_date(self.next_session_date, "evidence.next_session_date")


@dataclass(frozen=True)
class BhavcopyDay:
    isin: str
    series: str
    session_date: date
    total_traded_quantity: int
    high: Decimal
    low: Decimal

    def __post_init__(self) -> None:
        require_text(self.isin, "bhavcopy.isin")
        require_text(self.series, "bhavcopy.series")
        require_date(self.session_date, "bhavcopy.session_date")
        strict_int(self.total_traded_quantity, "bhavcopy.total_traded_quantity", minimum=0)
        positive_decimal(self.high, "bhavcopy.high")
        positive_decimal(self.low, "bhavcopy.low")


@dataclass(frozen=True)
class SessionFacts:
    """The caller's calendar answer for the session date (R-9)."""

    standard: bool
    timing_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.standard, bool):
            raise InputError("session.standard must be a bool")
        require_text(self.timing_version, "session.timing_version")


@dataclass(frozen=True)
class IntradayAttempt:
    """R-1: the pinned facts of one order attempt. A missing or naive input raises, never a result."""

    intent_id: str
    isin: str
    series: str
    stock_code: str
    session_date: date
    side: Side
    quantity: int
    limit_price: Decimal
    tick: TickSize
    band: PriceBand | BandUnavailable
    submitted_at: datetime
    scenario_set_hash: str

    def __post_init__(self) -> None:
        require_text(self.intent_id, "intent_id")
        require_text(self.isin, "isin")
        require_text(self.series, "series")
        require_text(self.stock_code, "stock_code")
        require_date(self.session_date, "session_date")
        if not isinstance(self.side, Side):
            raise InputError("side must be a Side")
        strict_int(self.quantity, "quantity", minimum=1)
        if not isinstance(self.limit_price, Decimal):
            raise InputError("limit_price must be a Decimal")
        positive_decimal(self.limit_price, "limit_price")
        if not isinstance(self.tick, TickSize):
            raise InputError("tick must be a TickSize")
        if not isinstance(self.band, (PriceBand, BandUnavailable)):
            raise InputError("band must be a PriceBand or BandUnavailable")
        require_aware(self.submitted_at, "submitted_at")
        if self.submitted_at.astimezone(IST).date() != self.session_date:
            raise InputError("submitted_at must fall on the session date (IST)")
        require_sha256(self.scenario_set_hash, "scenario_set_hash")


# ---- Stage A: whole-day integrity --------------------------------------------------------


@dataclass(frozen=True)
class ParsedBar:
    label: str
    start: datetime  # the label as an aware IST instant
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class DayIntegrity:
    """Hashed Stage A verdict for one symbol-day. ``verdict_code`` None means it passed."""

    verdict_code: str | None
    isin: str
    series: str
    stock_code: str
    session_date: date
    source_sha256: str
    interval: str
    route: str
    fetched_at: datetime
    row_count: int
    schedule_version: str
    schedule_hash: str
    gap_policy: str
    label_semantics: str
    evidence_fetch_rule: str | None
    session_timing_version: str
    cas_class: str | None
    cas_table_version: str | None
    cas_source_ref: str | None
    continuous_end: str | None
    volume_sum: int | None
    bhavcopy_total: int | None
    volume_ratio: Decimal | None
    volume_band_lower: Decimal | None
    volume_band_upper: Decimal | None
    lower_bound_status: str
    volume_lower_check: str | None
    bar_high: Decimal | None
    bar_low: Decimal | None
    reference_high: Decimal | None
    reference_low: Decimal | None
    daily_bar_volume_disagree: bool | None
    tick: TickSize
    band: PriceBand | BandUnavailable
    bars: tuple[ParsedBar, ...]
    day_hash: str

    @property
    def passed(self) -> bool:
        return self.verdict_code is None


def verify_day_integrity(day: DayIntegrity) -> None:
    """Raise if the verdict's hash does not match its content (a tampered or hand-built verdict)."""
    if seal(day, "day_hash").day_hash != day.day_hash:
        raise InputError("DayIntegrity hash does not match its content")


def _check_tick(tick: TickSize, session_date: date) -> None:
    if not isinstance(tick, TickSize):
        raise InputError("tick must be a TickSize")
    if tick.effective_from > session_date or (tick.effective_to is not None and tick.effective_to < session_date):
        raise InputError(f"tick from {tick.source!r} is not effective on {session_date.isoformat()}")


def _parse_label(label: str, session_date: date) -> datetime | None:
    """R-5: ``YYYY-MM-DD HH:MM:00`` on the session date, else None."""
    match = _LABEL.fullmatch(label)
    if match is None:
        return None
    day, hour, minute = match.groups()
    if day != session_date.isoformat() or int(hour) > 23 or int(minute) > 59:
        return None
    return datetime.combine(session_date, time(int(hour), int(minute)), tzinfo=IST)


def _bar_inconsistent(bar: ParsedBar, band: PriceBand | BandUnavailable, session_date: date) -> bool:
    """R-6, mirroring ``fills._validate_bar`` plus a volume check."""
    prices = (bar.open, bar.high, bar.low, bar.close)
    if any(not price.is_finite() or price <= 0 for price in prices):
        return True
    if bar.volume < 0:
        return True
    if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
        return True
    if isinstance(band, PriceBand) and band.category == "fixed" and band.effective_date == session_date:
        assert band.lower is not None and band.upper is not None
        if bar.low < band.lower or bar.high > band.upper:
            return True
    return False


def _freshness(evidence: IntradayEvidence, schedule: IntradayScenarioSet) -> tuple[str | None, str | None]:
    """R-18 and D-27: (INCONCLUSIVE code, fetch rule pinned)."""
    fetched = evidence.fetched_at.astimezone(IST)
    fetch_day, fetch_time = fetched.date(), fetched.time()
    if fetch_day < evidence.session_date:
        return EVIDENCE_FETCHED_BEFORE_CLOSE, None
    if fetch_day == evidence.session_date:
        if fetch_time < schedule.earliest_fetch:
            return EVIDENCE_FETCHED_BEFORE_CLOSE, None
        if fetch_time > schedule.latest_fetch:
            return EVIDENCE_FETCHED_TOO_LATE, None
        return None, SAME_DAY_FETCH_RULE
    window = schedule.next_day_fetch
    if (
        window is not None
        and evidence.next_session_date is not None
        and evidence.next_session_date > evidence.session_date
        and fetch_day == evidence.next_session_date
        and window.window_start <= fetch_time <= window.window_end
    ):
        return None, window.rule
    return EVIDENCE_FETCHED_TOO_LATE, None


def assess_day(
    evidence: IntradayEvidence,
    *,
    schedule: IntradayScenarioSet,
    isin: str,
    series: str,
    stock_code: str,
    session_date: date,
    tick: TickSize,
    band: PriceBand | BandUnavailable,
    session: SessionFacts | None,
    cas_row: CasClassRow | None,
    bhavcopy: BhavcopyDay | None,
    daily_bar_volume: int | None = None,
) -> DayIntegrity:
    """Stage A (R-2 to R-9, R-15 to R-19, R-27, R-33). Reads no attempt field."""
    with decimal.localcontext(COST_CONTEXT):
        require_text(isin, "isin")
        require_text(series, "series")
        require_text(stock_code, "stock_code")
        require_date(session_date, "session_date")
        _check_tick(tick, session_date)
        if not isinstance(band, (PriceBand, BandUnavailable)):
            raise InputError("band must be a PriceBand or BandUnavailable")
        if daily_bar_volume is not None:
            strict_int(daily_bar_volume, "daily_bar_volume", minimum=0)
        return _assess_day(
            evidence, schedule, isin, series, stock_code, session_date, tick, band,
            session, cas_row, bhavcopy, daily_bar_volume,
        )


def _assess_day(
    evidence: IntradayEvidence,
    schedule: IntradayScenarioSet,
    isin: str,
    series: str,
    stock_code: str,
    session_date: date,
    tick: TickSize,
    band: PriceBand | BandUnavailable,
    session: SessionFacts | None,
    cas_row: CasClassRow | None,
    bhavcopy: BhavcopyDay | None,
    daily_bar_volume: int | None,
) -> DayIntegrity:
    facts: dict[str, Any] = {
        "verdict_code": None,
        "evidence_fetch_rule": None,
        "cas_class": None,
        "cas_table_version": None,
        "cas_source_ref": None,
        "continuous_end": None,
        "volume_sum": None,
        "bhavcopy_total": None,
        "volume_ratio": None,
        "volume_band_lower": None,
        "volume_band_upper": None,
        "volume_lower_check": None,
        "bar_high": None,
        "bar_low": None,
        "reference_high": None,
        "reference_low": None,
        "daily_bar_volume_disagree": None,
    }
    parsed: list[ParsedBar] = []

    def finish(code: str | None) -> DayIntegrity:
        if code is not None:
            facts["verdict_code"] = code
        day = DayIntegrity(
            isin=isin,
            series=series,
            stock_code=stock_code,
            session_date=session_date,
            source_sha256=evidence.source_sha256,
            interval=evidence.interval,
            route=evidence.route,
            fetched_at=evidence.fetched_at,
            row_count=len(evidence.bars),
            schedule_version=schedule.version,
            schedule_hash=schedule.schedule_hash,
            gap_policy=schedule.gap_policy,
            label_semantics=schedule.label_semantics,
            session_timing_version=schedule.session_timing_version,
            lower_bound_status=schedule.lower_bound_status,
            tick=tick,
            band=band,
            bars=tuple(parsed) if code is None else (),
            day_hash="",
            **facts,
        )
        return seal(day, "day_hash")

    # R-27: a schedule that claims proven labels needs independent evidence and approval.
    if schedule.label_claim_refused():
        return finish(LABEL_SEMANTICS_UNPROVEN)
    # R-3: the approved interval only; no bar is read otherwise.
    if evidence.interval != schedule.interval:
        return finish(INTERVAL_NOT_APPROVED)
    # R-2: the evidence must be for this symbol and date.
    if evidence.stock_code != stock_code or evidence.session_date != session_date:
        return finish(EVIDENCE_SCOPE_MISMATCH)
    # R-19: the client must have verified the payload hash.
    if not evidence.sha256_verified:
        return finish(EVIDENCE_HASH_UNVERIFIED)
    # R-18 and D-27: fetch window.
    code, rule = _freshness(evidence, schedule)
    facts["evidence_fetch_rule"] = rule
    if code is not None:
        return finish(code)
    # R-8: empty or capped is never zero volume and never a miss.
    if evidence.empty_flag or not evidence.bars:
        return finish(EMPTY_ANSWER)
    if len(evidence.bars) >= CAPPED_ROW_COUNT:
        return finish(CAPPED_ANSWER)
    # R-5: labels.
    for bar in evidence.bars:
        start = _parse_label(bar.label, session_date)
        if start is None:
            return finish(LABEL_MALFORMED)
        parsed.append(ParsedBar(bar.label, start, bar.open, bar.high, bar.low, bar.close, bar.volume))
    # R-7 over the whole payload, not only admitted bars. Duplicates are never summed.
    labels = [bar.label for bar in parsed]
    if len(set(labels)) != len(labels):
        return finish(DUPLICATE_BAR)
    if any(later.start <= earlier.start for earlier, later in zip(parsed, parsed[1:])):
        return finish(BAR_ORDER)
    # R-6.
    if any(_bar_inconsistent(bar, band, session_date) for bar in parsed):
        return finish(BAR_INCONSISTENT)
    # R-9: standard session and CAS class.
    if (
        session is None
        or not session.standard
        or session.timing_version != schedule.session_timing_version
    ):
        return finish(SESSION_NOT_STANDARD)
    if cas_row is None or cas_row.isin != isin or cas_row.session_date != session_date:
        return finish(SESSION_SCOPE_UNKNOWN)
    facts["cas_class"] = cas_row.cas_class
    facts["cas_table_version"] = cas_row.table_version
    facts["cas_source_ref"] = f"{cas_row.publication_id}:{cas_row.source_sha256}"
    facts["continuous_end"] = schedule.continuous_end_for(cas_row.cas_class).strftime("%H:%M")
    # R-15: sum over every bar in D, admitted by any attempt or not.
    if (
        bhavcopy is None
        or bhavcopy.isin != isin
        or bhavcopy.series != series
        or bhavcopy.session_date != session_date
        or bhavcopy.total_traded_quantity <= 0
    ):
        return finish(VOLUME_REFERENCE_MISSING)
    window = [
        bar for bar in parsed if schedule.day_window_start <= bar.start.time() <= schedule.day_window_end
    ]
    volume_sum = sum(bar.volume for bar in window)
    ratio = Decimal(volume_sum) / Decimal(bhavcopy.total_traded_quantity)
    volume_band = schedule.band_for(cas_row.cas_class)
    facts.update(
        volume_sum=volume_sum,
        bhavcopy_total=bhavcopy.total_traded_quantity,
        volume_ratio=ratio,
        volume_band_lower=volume_band.lower,
        volume_band_upper=volume_band.upper,
    )
    if volume_band.lower is None:
        facts["volume_lower_check"] = "skipped_provisional"
    else:
        facts["volume_lower_check"] = "passed" if ratio >= volume_band.lower else "failed"
    if ratio > volume_band.upper or facts["volume_lower_check"] == "failed":
        return finish(VOLUME_CHECK_FAILED)
    # R-16: bar extremes against the bhavcopy day range, within the tolerance.
    allowance = schedule.price_tolerance_ticks * tick.value
    facts.update(reference_high=bhavcopy.high, reference_low=bhavcopy.low)
    if window:
        facts["bar_high"] = max(bar.high for bar in window)
        facts["bar_low"] = min(bar.low for bar in window)
        if facts["bar_high"] > bhavcopy.high + allowance or facts["bar_low"] < bhavcopy.low - allowance:
            return finish(PRICE_RANGE_CHECK_FAILED)
    # R-17: record only, never changes the outcome.
    if daily_bar_volume is not None:
        if daily_bar_volume == 0:
            facts["daily_bar_volume_disagree"] = volume_sum > 0
        else:
            daily_ratio = Decimal(volume_sum) / Decimal(daily_bar_volume)
            facts["daily_bar_volume_disagree"] = daily_ratio > volume_band.upper or (
                volume_band.lower is not None and daily_ratio < volume_band.lower
            )
    return finish(None)


# ---- admission ---------------------------------------------------------------------------

_ADMIT_KEY = object()


@dataclass(frozen=True)
class ExcludedBars:
    reason: str  # before_submission, after_lifetime or outside_session
    count: int
    first_label: str
    last_label: str


def _ceil_minute(moment: datetime) -> datetime:
    floor = moment.replace(second=0, microsecond=0)
    return floor if floor == moment else floor + timedelta(minutes=1)


def _lifetime_end(attempt: IntradayAttempt, schedule: IntradayScenarioSet) -> datetime:
    return datetime.combine(attempt.session_date, schedule.lifetime_end, tzinfo=IST)


def _admissible(start: datetime, submitted: datetime, lifetime_end: datetime, interval: timedelta) -> bool:
    """R-10 and R-11: the bar cannot have started before the submission or ended after the lifetime."""
    return start - interval >= submitted and start + interval <= lifetime_end


@dataclass(frozen=True)
class AdmittedBars:
    """R-4: the only input of the fill decision. Built by ``admit_bars``; it re-checks every bar."""

    intent_id: str
    source_sha256: str
    day_hash: str
    submitted_at: datetime
    lifetime_end: datetime
    interval: timedelta
    bars: tuple[ParsedBar, ...]
    exclusions: tuple[ExcludedBars, ...]
    window_first: str
    window_last: str
    _key: object = None

    def __post_init__(self) -> None:
        if self._key is not _ADMIT_KEY:
            raise InputError("AdmittedBars can only be built by admit_bars from a passing DayIntegrity")
        for bar in self.bars:
            if bar.start - self.interval < self.submitted_at or bar.start + self.interval > self.lifetime_end:
                raise IntradayRefusal(
                    f"bar {bar.label} is outside the post-submission window", PRE_SUBMISSION_BAR_OFFERED
                )


def _exclusions(
    day: DayIntegrity,
    submitted: datetime,
    lifetime_end: datetime,
    interval: timedelta,
    schedule: IntradayScenarioSet,
) -> tuple[ExcludedBars, ...]:
    """R-13: every bar kept out of this attempt's window, with its reason."""
    buckets: dict[str, list[str]] = {}
    for bar in day.bars:
        clock = bar.start.time()
        if not schedule.day_window_start <= clock <= schedule.day_window_end:
            reason = "outside_session"
        elif bar.start - interval < submitted:
            reason = "before_submission"
        elif bar.start + interval > lifetime_end:
            reason = "after_lifetime"
        else:
            continue
        buckets.setdefault(reason, []).append(bar.label)
    return tuple(
        ExcludedBars(reason, len(labels), labels[0], labels[-1]) for reason, labels in sorted(buckets.items())
    )


def admit_bars(day: DayIntegrity, attempt: IntradayAttempt, schedule: IntradayScenarioSet) -> AdmittedBars:
    """Build one attempt's admitted window (R-10 to R-14). Needs a passing, untampered verdict."""
    verify_day_integrity(day)
    if not day.passed:
        raise InputError(f"admit_bars needs a passing DayIntegrity, got {day.verdict_code!r}")
    if attempt.scenario_set_hash != schedule.schedule_hash or day.schedule_hash != schedule.schedule_hash:
        raise InputError("attempt, verdict and schedule must pin the same scenario-set hash")
    if (attempt.isin, attempt.series, attempt.stock_code, attempt.session_date) != (
        day.isin, day.series, day.stock_code, day.session_date,
    ):
        raise InputError("attempt does not belong to this symbol-day")
    submitted = attempt.submitted_at.astimezone(IST)
    lifetime_end = _lifetime_end(attempt, schedule)
    interval = schedule.interval_delta
    window_open = datetime.combine(attempt.session_date, schedule.day_window_start, tzinfo=IST)
    window_close = datetime.combine(attempt.session_date, schedule.day_window_end, tzinfo=IST)
    first = max(_ceil_minute(submitted + interval), window_open)
    last = min(lifetime_end - interval, window_close)
    if first > last:
        raise IntradayRefusal("no label can satisfy both the submission and lifetime rules", NO_ADMISSIBLE_BAR)
    admitted = tuple(
        bar
        for bar in day.bars
        if window_open <= bar.start <= window_close and _admissible(bar.start, submitted, lifetime_end, interval)
    )
    present = {bar.start for bar in admitted}
    moment = first
    while moment <= last:
        # R-14: the window is computed from the submission and lifetime, never from the data.
        if moment not in present:
            raise IntradayRefusal(f"label {moment.strftime('%H:%M')} is missing inside the window", GAP_IN_SEQUENCE)
        moment += interval
    return AdmittedBars(
        intent_id=attempt.intent_id,
        source_sha256=day.source_sha256,
        day_hash=day.day_hash,
        submitted_at=submitted,
        lifetime_end=lifetime_end,
        interval=interval,
        bars=admitted,
        exclusions=_exclusions(day, submitted, lifetime_end, interval, schedule),
        window_first=first.strftime("%H:%M"),
        window_last=last.strftime("%H:%M"),
        _key=_ADMIT_KEY,
    )


# ---- results -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioFill:
    scenario_id: str
    k_ticks: int
    volume_participation: Decimal
    outcome: IntradayOutcome
    reason_code: str
    filled_quantity: int
    fill_price: Decimal | None
    crossing_bars: int
    crossing_volume: int
    volume_cap: int
    allowance_charged: Decimal
    cap_label_window: tuple[str, ...]


@dataclass(frozen=True)
class IntradayAttemptResult:
    intent_id: str
    isin: str
    stock_code: str
    session_date: date
    side: Side
    requested_quantity: int
    limit_price: Decimal
    tick_size: Decimal
    submitted_at: datetime
    lifetime_end: datetime | None
    schedule_version: str
    schedule_hash: str
    interval: str
    source_sha256: str | None
    day_integrity_hash: str | None
    day_verdict_code: str | None
    cas_class: str | None
    cas_table_version: str | None
    continuous_end: str | None
    evidence_fetch_rule: str | None
    gap_policy: str
    label_semantics: str
    lower_bound_status: str
    volume_ratio: Decimal | None
    volume_band_lower: Decimal | None
    volume_band_upper: Decimal | None
    volume_lower_check: str | None
    exclusions: tuple[ExcludedBars, ...]
    locked_bars: int
    ambiguous_single_price_bars: int
    band_check: str
    inconclusive_code: str | None
    inconclusive_detail: str | None
    scenarios: tuple[ScenarioFill, ...]
    k1_fill_k3_miss: bool
    labels: tuple[str, ...]
    result_hash: str


def _scenario_stub(scenario: IntradayScenario, outcome: IntradayOutcome, reason: str) -> ScenarioFill:
    return ScenarioFill(
        scenario.scenario_id, scenario.k_ticks, scenario.volume_participation, outcome, reason,
        0, None, 0, 0, 0, Decimal(0), (),
    )


def _k1_fill_k3_miss(schedule: IntradayScenarioSet, fills: Sequence[ScenarioFill]) -> bool:
    by_k = {fill.k_ticks: fill for fill in fills}
    if 1 not in by_k or 3 not in by_k:
        return False
    k1, k3 = by_k[1], by_k[3]
    if k3.outcome is IntradayOutcome.INCONCLUSIVE or k1.outcome is IntradayOutcome.INCONCLUSIVE:
        return False
    return k1.filled_quantity > 0 and k3.filled_quantity == 0


def _build_result(
    attempt: IntradayAttempt,
    schedule: IntradayScenarioSet,
    day: DayIntegrity | None,
    fills: Sequence[ScenarioFill],
    *,
    exclusions: tuple[ExcludedBars, ...] = (),
    locked: int = 0,
    ambiguous: int = 0,
    band_check: str = "not_evaluated",
    inconclusive_code: str | None = None,
    inconclusive_detail: str | None = None,
) -> IntradayAttemptResult:
    result = IntradayAttemptResult(
        intent_id=attempt.intent_id,
        isin=attempt.isin,
        stock_code=attempt.stock_code,
        session_date=attempt.session_date,
        side=attempt.side,
        requested_quantity=attempt.quantity,
        limit_price=attempt.limit_price,
        tick_size=attempt.tick.value,
        submitted_at=attempt.submitted_at,
        lifetime_end=_lifetime_end(attempt, schedule),
        schedule_version=schedule.version,
        schedule_hash=schedule.schedule_hash,
        interval=schedule.interval,
        source_sha256=None if day is None else day.source_sha256,
        day_integrity_hash=None if day is None else day.day_hash,
        day_verdict_code=None if day is None else day.verdict_code,
        cas_class=None if day is None else day.cas_class,
        cas_table_version=None if day is None else day.cas_table_version,
        continuous_end=None if day is None else day.continuous_end,
        evidence_fetch_rule=None if day is None else day.evidence_fetch_rule,
        gap_policy=schedule.gap_policy,
        label_semantics=schedule.label_semantics,
        lower_bound_status=schedule.lower_bound_status,
        volume_ratio=None if day is None else day.volume_ratio,
        volume_band_lower=None if day is None else day.volume_band_lower,
        volume_band_upper=None if day is None else day.volume_band_upper,
        volume_lower_check=None if day is None else day.volume_lower_check,
        exclusions=exclusions,
        locked_bars=locked,
        ambiguous_single_price_bars=ambiguous,
        band_check=band_check,
        inconclusive_code=inconclusive_code,
        inconclusive_detail=inconclusive_detail,
        scenarios=tuple(fills),
        k1_fill_k3_miss=_k1_fill_k3_miss(schedule, fills),
        labels=ASSUMPTION_LABELS,
        result_hash="",
    )
    return seal(result, "result_hash")


def _inconclusive(
    attempt: IntradayAttempt,
    schedule: IntradayScenarioSet,
    day: DayIntegrity | None,
    code: str,
    detail: str | None = None,
) -> IntradayAttemptResult:
    fills = [_scenario_stub(sc, IntradayOutcome.INCONCLUSIVE, code) for sc in schedule.scenarios]
    return _build_result(attempt, schedule, day, fills, inconclusive_code=code, inconclusive_detail=detail)


def _check_attempts(attempts: Sequence[IntradayAttempt], schedule: IntradayScenarioSet) -> list[IntradayAttempt]:
    if not attempts:
        raise InputError("simulate_intraday needs at least one attempt")
    if not all(isinstance(attempt, IntradayAttempt) for attempt in attempts):
        raise InputError("attempts must be IntradayAttempt instances")
    ids = [attempt.intent_id for attempt in attempts]
    if len(set(ids)) != len(ids):
        raise InputError("duplicate intent_id in one symbol-day")
    for attempt in attempts:
        if attempt.scenario_set_hash != schedule.schedule_hash:
            raise InputError(f"attempt {attempt.intent_id!r} pins a different scenario-set hash")
    # R-24: first-in-first-out by (submission time, intent id); the input order never matters.
    return sorted(attempts, key=lambda a: (a.submitted_at.astimezone(IST), a.intent_id))


def no_evidence_results(
    attempts: Sequence[IntradayAttempt], schedule: IntradayScenarioSet, *, reason: str
) -> tuple[IntradayAttemptResult, ...]:
    """INCONCLUSIVE ``no_evidence_fetched`` for attempts whose evidence fetch did not happen."""
    if reason not in NO_EVIDENCE_REASONS:
        raise InputError(f"reason must be one of {NO_EVIDENCE_REASONS}, got {reason!r}")
    with decimal.localcontext(COST_CONTEXT):
        ordered = _check_attempts(attempts, schedule)
        return tuple(_inconclusive(a, schedule, None, NO_EVIDENCE_FETCHED, reason) for a in ordered)


# ---- Stage B -----------------------------------------------------------------------------


Verdict = tuple[IntradayOutcome, str]


def _attempt_screen(attempt: IntradayAttempt) -> tuple[str, Verdict | None]:
    """R-20, in the precedence of ``fills._screen``: off-tick, then the band verdicts."""
    band = attempt.band
    if attempt.limit_price % attempt.tick.value != 0:
        return "not_evaluated", (IntradayOutcome.REJECTED, REJECTED_OFF_TICK)
    if isinstance(band, BandUnavailable):
        return f"unavailable:{band.reason}", (IntradayOutcome.NO_ASSUMED_FILL, NO_FILL_BAND_UNAVAILABLE)
    if band.effective_date != attempt.session_date:
        return (
            f"date_mismatch:{band.effective_date.isoformat()}",
            (IntradayOutcome.NO_ASSUMED_FILL, NO_FILL_BAND_DATE_MISMATCH),
        )
    if band.category == "no_band":
        return "no_band", None
    assert band.lower is not None and band.upper is not None
    if not band.lower <= attempt.limit_price <= band.upper:
        return "outside_band", (IntradayOutcome.REJECTED, REJECTED_OUTSIDE_BAND)
    return "inside_band", None


def _classify(attempt: IntradayAttempt, bar: ParsedBar) -> str:
    """R-22 and R-32: ``locked`` and ``ambiguous`` bars are never credited, and are checked first."""
    band = attempt.band
    assert isinstance(band, PriceBand)
    if bar.high != bar.low:
        return "normal"
    if band.category == "no_band":
        return "ambiguous"
    if (attempt.side is Side.BUY and bar.high == band.upper) or (attempt.side is Side.SELL and bar.low == band.lower):
        return "locked"
    return "normal"


def _crosses(attempt: IntradayAttempt, bar: ParsedBar, k_ticks: int) -> bool:
    """R-21: a buy credits ``low <= limit - k*tick``, a sell ``high >= limit + k*tick``."""
    offset = k_ticks * attempt.tick.value
    if attempt.side is Side.BUY:
        return bar.low <= attempt.limit_price - offset
    return bar.high >= attempt.limit_price + offset


def _fill_scenario(
    attempt: IntradayAttempt,
    admitted: AdmittedBars,
    kinds: Mapping[str, str],
    scenario: IntradayScenario,
    remaining: dict[str, Decimal],
) -> ScenarioFill:
    """R-21 to R-24 for one attempt and one ``k``. Reads only ``admitted.bars``; updates ``remaining``."""
    credited = [
        bar for bar in admitted.bars if kinds[bar.label] == "normal" and _crosses(attempt, bar, scenario.k_ticks)
    ]
    if not credited:
        if any(kind == "locked" for kind in kinds.values()):
            return _scenario_stub(scenario, IntradayOutcome.MISSED, MISSED_LOCKED_AT_BAND)
        if any(kind == "ambiguous" for kind in kinds.values()):
            return _scenario_stub(scenario, IntradayOutcome.NO_ASSUMED_FILL, NO_FILL_AMBIGUOUS_SINGLE_PRICE)
        return _scenario_stub(scenario, IntradayOutcome.MISSED, MISSED_THRESHOLD)
    available = sum((remaining[bar.label] for bar in credited), Decimal(0))
    cap = int(floor_to(available, Decimal(1)))
    filled = min(attempt.quantity, cap)
    window = tuple(bar.label for bar in credited if remaining[bar.label] > 0)
    # R-24: charge the full quantity, latest credited label first, whether or not it filled.
    left = Decimal(attempt.quantity)
    charged = Decimal(0)
    for bar in reversed(credited):
        take = min(remaining[bar.label], left)
        remaining[bar.label] -= take
        left -= take
        charged += take
    if filled == 0:
        outcome, reason = IntradayOutcome.MISSED, MISSED_VOLUME_CAP_ZERO
    elif filled < attempt.quantity:
        outcome, reason = IntradayOutcome.PARTIAL, PARTIAL_VOLUME_CAP
    else:
        outcome, reason = IntradayOutcome.FILLED, FILLED_AT_LIMIT
    return ScenarioFill(
        scenario.scenario_id, scenario.k_ticks, scenario.volume_participation, outcome, reason,
        filled, attempt.limit_price if filled > 0 else None, len(credited),
        sum(bar.volume for bar in credited), cap, charged,
        (window[0], window[-1]) if window else (),
    )


def simulate_intraday(
    day: DayIntegrity, attempts: Sequence[IntradayAttempt], schedule: IntradayScenarioSet
) -> tuple[IntradayAttemptResult, ...]:
    """Run every attempt of one symbol-day under k=1, 2 and 3 from the same evidence (R-26)."""
    with decimal.localcontext(COST_CONTEXT):
        verify_day_integrity(day)
        ordered = _check_attempts(attempts, schedule)
        for attempt in ordered:
            if (attempt.isin, attempt.series, attempt.stock_code, attempt.session_date) != (
                day.isin, day.series, day.stock_code, day.session_date,
            ):
                raise InputError(f"attempt {attempt.intent_id!r} does not belong to this symbol-day")
            if attempt.tick != day.tick or attempt.band != day.band:
                raise InputError(f"attempt {attempt.intent_id!r} pins a different tick or band than the verdict")
        if day.schedule_hash != schedule.schedule_hash:
            raise InputError("verdict and schedule pin different scenario-set hashes")
        if not day.passed:
            assert day.verdict_code is not None
            return tuple(_inconclusive(a, schedule, day, day.verdict_code) for a in ordered)
        # One allowance table per scenario, shared by every attempt of the ISIN-day, both sides (R-24).
        remaining = {
            sc.scenario_id: {bar.label: sc.volume_participation * Decimal(bar.volume) for bar in day.bars}
            for sc in schedule.scenarios
        }
        unresolved = False
        results: list[IntradayAttemptResult] = []
        for attempt in ordered:
            band_check, verdict = _attempt_screen(attempt)
            if verdict is not None:
                fills = [_scenario_stub(sc, verdict[0], verdict[1]) for sc in schedule.scenarios]
                results.append(_build_result(attempt, schedule, day, fills, band_check=band_check))
                continue
            try:
                admitted = admit_bars(day, attempt, schedule)
            except IntradayRefusal as refusal:
                unresolved = True  # R-25: attempt-local causes poison later siblings
                results.append(_inconclusive(attempt, schedule, day, refusal.code, str(refusal)))
                continue
            if unresolved:
                results.append(_inconclusive(attempt, schedule, day, SIBLING_UNRESOLVED))
                continue
            kinds = {bar.label: _classify(attempt, bar) for bar in admitted.bars}
            fills = [
                _fill_scenario(attempt, admitted, kinds, sc, remaining[sc.scenario_id])
                for sc in schedule.scenarios
            ]
            results.append(
                _build_result(
                    attempt, schedule, day, fills,
                    exclusions=admitted.exclusions,
                    locked=sum(1 for kind in kinds.values() if kind == "locked"),
                    ambiguous=sum(1 for kind in kinds.values() if kind == "ambiguous"),
                    band_check=band_check,
                )
            )
        return tuple(results)
