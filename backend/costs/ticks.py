"""Dated NSE cash tick-size table and limit alignment.

The tick size is an explicit, dated input. Each table version cites the NSE
circulars it comes from and lists the exchange series its circular covers; a
date no version covers (before 2021-01-01), a series a version does not list,
and any instrument NSE sets per security (Gold ETFs) are not encoded and fail
closed. Equities and ETFs live in separate tables because the version model
has no instrument class. Use ``resolve_nse_cash_tick``: the caller states the
instrument class and the series, and the table is chosen from them. The
class is the caller's duty: ETFs trade in series EQ, so nothing here can tell
an ETF from a stock; an ETF passed as EQUITY resolves from the equity table.
The Breeze security master shows a different tick for some names, so neither
source is trusted until a live quote settles it (Phase 61); every resolved
tick records which table version it came from and that version's hash.

``ticks`` may import from ``fills``; ``fills`` never imports ``ticks``.
"""

from __future__ import annotations

import decimal
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from functools import lru_cache
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from .core import (
    COST_CONTEXT,
    ScheduleError,
    Side,
    TickSizeUnavailable,
    canonical_json,
    load_strict_json,
    positive_decimal,
    sha256_hex,
    strict_decimal,
)
from .fills import TickSize

TICK_SCHEMA = "growin.costs.tick_sizes/1"
EQUITY_TICK_TABLE_PATH = Path(__file__).parent / "schedules" / "nse_cash_tick_sizes.json"
# Exchange Traded Funds other than Gold ETFs. Never use it for a Gold ETF: NSE sets those one by one.
NON_GOLD_ETF_TICK_TABLE_PATH = Path(__file__).parent / "schedules" / "nse_cash_etf_tick_sizes.json"

_VERSION_KEYS = (
    "version",
    "effective_from",
    "effective_to",
    "exchange",
    "segment",
    "status",
    "reference_price_rule",
    "series",
    "sources",
    "bands",
)


@dataclass(frozen=True)
class TickBand:
    """A price band. The lower bound belongs to the band unless the previous band is upper-inclusive.

    ``upper_inclusive`` puts a price exactly at ``upper`` in this band instead of the next one. It is
    optional in the file and defaults to False, so a version that does not set it resolves as before.
    """

    lower: Decimal
    upper: Decimal | None
    tick: Decimal
    upper_inclusive: bool = False


@dataclass(frozen=True)
class TickTableVersion:
    version: str
    effective_from: date
    effective_to: date | None
    exchange: str
    segment: str
    status: str
    reference_price_rule: str
    series: tuple[str, ...]
    sources: tuple[str, ...]
    bands: tuple[TickBand, ...]
    version_hash: str

    def covers(self, d: date) -> bool:
        if d < self.effective_from:
            return False
        return self.effective_to is None or d <= self.effective_to


@dataclass(frozen=True)
class TickTable:
    schema: str
    source_id: str
    versions: tuple[TickTableVersion, ...]


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
    if not isinstance(value, str) or not value.strip():
        raise ScheduleError(f"{path}.{key}: expected a non-empty string")
    return value


def _dec(raw: Mapping[str, Any], key: str, path: str) -> Decimal:
    value = raw[key]
    if not isinstance(value, str):
        raise ScheduleError(f"{path}.{key}: numerics must be JSON strings")
    try:
        return strict_decimal(value, f"{path}.{key}")
    except ValueError as exc:
        raise ScheduleError(str(exc)) from exc


def _date(raw: Mapping[str, Any], key: str, path: str) -> date:
    value = raw[key]
    if not isinstance(value, str):
        raise ScheduleError(f"{path}.{key}: expected an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ScheduleError(f"{path}.{key}: not an ISO date") from exc


def _parse_bands(raw: Any, path: str) -> tuple[TickBand, ...]:
    if not isinstance(raw, list) or not raw:
        raise ScheduleError(f"{path}: expected a non-empty list of bands")
    bands: list[TickBand] = []
    for n, item in enumerate(raw):
        band_path = f"{path}[{n}]"
        band = _mapping(item, band_path)
        _exact({k: v for k, v in band.items() if k != "upper_inclusive"}, ("from", "to", "tick"), band_path)
        upper_inclusive = band.get("upper_inclusive", False)
        if not isinstance(upper_inclusive, bool):
            raise ScheduleError(f"{band_path}.upper_inclusive: expected a JSON boolean")
        if upper_inclusive and band["to"] is None:
            raise ScheduleError(f"{band_path}.upper_inclusive: an open-ended band has no upper bound")
        lower = _dec(band, "from", band_path)
        upper = None if band["to"] is None else _dec(band, "to", band_path)
        tick = _dec(band, "tick", band_path)
        if tick <= 0:
            raise ScheduleError(f"{band_path}.tick: must be greater than zero")
        if lower < 0 or (upper is not None and upper <= lower):
            raise ScheduleError(f"{band_path}: band bounds are not increasing")
        bands.append(TickBand(lower, upper, tick, upper_inclusive))
    if bands[0].lower != 0:
        raise ScheduleError(f"{path}[0].from: the first band must start at 0")
    for n, (earlier, later) in enumerate(zip(bands, bands[1:])):
        if earlier.upper is None:
            raise ScheduleError(f"{path}[{n}].to: an open-ended band must be last")
        if earlier.upper != later.lower:
            raise ScheduleError(f"{path}[{n + 1}].from: bands must be contiguous (gap or overlap)")
    if bands[-1].upper is not None:
        raise ScheduleError(f"{path}[{len(bands) - 1}].to: the last band must be open-ended")
    return tuple(bands)


def _parse_version(raw: Any, index: int) -> TickTableVersion:
    path = f"versions[{index}]"
    top = _mapping(raw, path)
    _exact(top, _VERSION_KEYS, path)
    for key, expected in (("exchange", "NSE"), ("segment", "cash")):
        if top[key] != expected:
            raise ScheduleError(f"{path}.{key}: expected {expected!r}, got {top[key]!r}")
    sources = top["sources"]
    if not isinstance(sources, list) or not all(isinstance(item, str) and item for item in sources):
        raise ScheduleError(f"{path}.sources: expected a list of non-empty strings")
    series = top["series"]
    if (
        not isinstance(series, list)
        or not series
        or not all(isinstance(item, str) and item and item == item.strip() for item in series)
        or len(set(series)) != len(series)
    ):
        raise ScheduleError(f"{path}.series: expected a non-empty list of distinct series codes")
    effective_from = _date(top, "effective_from", path)
    effective_to = None if top["effective_to"] is None else _date(top, "effective_to", path)
    if effective_to is not None and effective_to < effective_from:
        raise ScheduleError(f"{path}.effective_to: precedes effective_from")
    return TickTableVersion(
        series=tuple(series),
        version=_text(top, "version", path),
        effective_from=effective_from,
        effective_to=effective_to,
        exchange=top["exchange"],
        segment=top["segment"],
        status=_text(top, "status", path),
        reference_price_rule=_text(top, "reference_price_rule", path),
        sources=tuple(sources),
        bands=_parse_bands(top["bands"], f"{path}.bands"),
        version_hash=sha256_hex(canonical_json(top)),
    )


def _load_tick_table(path: Path) -> TickTable:
    """Load one tick table file. The path is required: there is deliberately no default table."""
    source = Path(path)
    with decimal.localcontext(COST_CONTEXT):
        raw = _mapping(load_strict_json(source.read_text(encoding="utf-8"), ScheduleError, source.name), "$")
        _exact(raw, ("schema", "source_id", "versions"), "$")
        if raw["schema"] != TICK_SCHEMA:
            raise ScheduleError(f"$.schema: expected {TICK_SCHEMA!r}")
        source_id = _text(raw, "source_id", "$")
        versions_raw = raw["versions"]
        if not isinstance(versions_raw, list) or not versions_raw:
            raise ScheduleError("$.versions: expected a non-empty list")
        versions = sorted((_parse_version(item, n) for n, item in enumerate(versions_raw)), key=lambda v: v.effective_from)
    ids = [version.version for version in versions]
    if len(set(ids)) != len(ids):
        raise ScheduleError("$.versions: duplicate version id")
    for earlier, later in zip(versions, versions[1:]):
        if earlier.effective_to is None:
            raise ScheduleError(f"$.versions: {earlier.version} is open-ended but is not the latest version")
        if earlier.effective_to >= later.effective_from:
            raise ScheduleError(f"$.versions: {earlier.version} overlaps {later.version}")
    return TickTable(raw["schema"], source_id, tuple(versions))


def _covering_version(table: TickTable, session_date: date) -> TickTableVersion:
    # A datetime is a date subclass but is not a session date; a str or None would otherwise
    # surface as a bare TypeError from the comparison below.
    if not isinstance(session_date, date) or isinstance(session_date, datetime):
        raise TickSizeUnavailable(f"session_date must be a datetime.date, got {type(session_date).__name__}")
    for version in table.versions:
        if version.covers(session_date):
            return version
    raise TickSizeUnavailable(
        f"no tick table version covers {session_date.isoformat()}; that date has no sourced tick version (not guessed)"
    )


def _tick_from_version(table: TickTable, version: TickTableVersion, reference: Decimal) -> TickSize:
    for band in version.bands:
        # Bands are contiguous from 0 and tried in order, so the lower edge is implied by the
        # previous band's upper edge: exclusive upper hands the edge price to the next band,
        # inclusive upper keeps it.
        if band.upper is None or reference < band.upper or (band.upper_inclusive and reference == band.upper):
            return TickSize(
                value=band.tick,
                effective_from=version.effective_from,
                source=f"{table.source_id}:{version.version}",
                source_hash=version.version_hash,
                effective_to=version.effective_to,
            )
    raise TickSizeUnavailable(f"{version.version}: no band holds the reference price")  # unreachable: last band is open


def _resolve_tick_from_table(table: TickTable, *, session_date: date, band_reference_price: Decimal) -> TickSize:
    """Pick the dated tick for a session from the band holding the reference price.

    Private on purpose: it does not know the instrument class or the series, so a Gold ETF resolved
    through the non-Gold ETF table would silently get Rs 0.01. Production callers use
    ``resolve_nse_cash_tick``; a test that scans ``backend/`` fails if any other module imports this.
    """
    with decimal.localcontext(COST_CONTEXT):
        reference = positive_decimal(band_reference_price, "band_reference_price")
        return _tick_from_version(table, _covering_version(table, session_date), reference)


class InstrumentClass(Enum):
    """What kind of NSE cash instrument a tick is wanted for. The caller classifies; nothing here can."""

    EQUITY = "equity"
    NON_GOLD_ETF = "non_gold_etf"
    GOLD_ETF = "gold_etf"


@dataclass(frozen=True)
class NseCashTickResolution:
    """A resolved tick plus the provenance a caller records: table version id, its hash, class and series."""

    tick: TickSize
    version_id: str
    version_hash: str
    instrument_class: InstrumentClass
    series: str


_TABLE_PATHS = {
    InstrumentClass.EQUITY: EQUITY_TICK_TABLE_PATH,
    InstrumentClass.NON_GOLD_ETF: NON_GOLD_ETF_TICK_TABLE_PATH,
}


@lru_cache(maxsize=None)
def _committed_table(instrument_class: InstrumentClass) -> TickTable:
    return _load_tick_table(_TABLE_PATHS[instrument_class])


def committed_tick_table(instrument_class: InstrumentClass) -> TickTable:
    """The committed table for a class, as read-only provenance data (versions, hashes, sources).

    It carries no resolver: resolving a tick goes through ``resolve_nse_cash_tick``, which adds the
    class and series checks. GOLD_ETF and any non-enum value raise ``TickSizeUnavailable``.
    """
    if not isinstance(instrument_class, InstrumentClass):
        raise TickSizeUnavailable(f"instrument_class must be an InstrumentClass, got {instrument_class!r}")
    if instrument_class is InstrumentClass.GOLD_ETF:
        raise TickSizeUnavailable("Gold ETF ticks are set per security by NSE and are not encoded")
    return _committed_table(instrument_class)


def resolve_nse_cash_tick(
    *,
    session_date: date,
    band_reference_price: Decimal,
    instrument_class: InstrumentClass,
    series: str,
) -> NseCashTickResolution:
    """Resolve the NSE cash tick for a session, instrument class and series. No argument has a default.

    The table is chosen from ``instrument_class``. Fails closed with ``TickSizeUnavailable`` for
    GOLD_ETF (NSE sets those per security and there is no per-security source), for any value that
    is not an ``InstrumentClass``, for a date no version covers, and for a series the covering
    version does not list. Classifying the instrument is the caller's duty: an ETF passed as
    EQUITY resolves from the equity table, because ETFs trade in series EQ.
    """
    if not isinstance(instrument_class, InstrumentClass):
        raise TickSizeUnavailable(f"instrument_class must be an InstrumentClass, got {instrument_class!r}")
    if instrument_class is InstrumentClass.GOLD_ETF:
        raise TickSizeUnavailable("Gold ETF ticks are set per security by NSE and are not encoded")
    if not isinstance(series, str):
        raise TickSizeUnavailable(f"series must be a string, got {series!r}")
    table = _committed_table(instrument_class)
    with decimal.localcontext(COST_CONTEXT):
        reference = positive_decimal(band_reference_price, "band_reference_price")
        version = _covering_version(table, session_date)
        if series not in version.series:
            raise TickSizeUnavailable(
                f"series {series!r} is not covered by {version.version} for {instrument_class.value} "
                f"(covered: {', '.join(version.series)})"
            )
        tick = _tick_from_version(table, version, reference)
    return NseCashTickResolution(
        tick=tick,
        version_id=version.version,
        version_hash=version.version_hash,
        instrument_class=instrument_class,
        series=series,
    )


def _tick_value(tick: TickSize | Decimal) -> Decimal:
    return tick.value if isinstance(tick, TickSize) else positive_decimal(tick, "tick")


def is_on_tick(price: Decimal, tick: TickSize | Decimal) -> bool:
    with decimal.localcontext(COST_CONTEXT):
        return positive_decimal(price, "price") % _tick_value(tick) == 0


def align_limit(price: Decimal, tick: TickSize | Decimal, side: Side) -> Decimal:
    """Floor a buy limit and ceil a sell limit to the tick. An aligned price is returned unchanged."""
    with decimal.localcontext(COST_CONTEXT):
        value = _tick_value(tick)
        amount = positive_decimal(price, "price")
        if amount % value == 0:
            return amount
        rounding = ROUND_FLOOR if side is Side.BUY else ROUND_CEILING
        return (amount / value).to_integral_value(rounding=rounding) * value
