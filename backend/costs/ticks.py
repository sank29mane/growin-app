"""Dated NSE cash tick-size table and limit alignment.

The tick size is an explicit, dated input. Each table version cites the NSE
circulars it comes from; a date no version covers (before 2021-01-01, and any
period NSE sets per security, such as Gold ETFs) is not encoded and fails
closed. Equities and ETFs live in separate tables because the version model
has no instrument class: a caller picks the table for the instrument. The
Breeze security master shows a different tick for some names, so neither
source is trusted until a live quote settles it (Phase 61); every resolved
tick records which table version it came from and that version's hash.

``ticks`` may import from ``fills``; ``fills`` never imports ``ticks``.
"""

from __future__ import annotations

import decimal
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
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
DEFAULT_TICK_TABLE_PATH = Path(__file__).parent / "schedules" / "nse_cash_tick_sizes.json"
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
    effective_from = _date(top, "effective_from", path)
    effective_to = None if top["effective_to"] is None else _date(top, "effective_to", path)
    if effective_to is not None and effective_to < effective_from:
        raise ScheduleError(f"{path}.effective_to: precedes effective_from")
    return TickTableVersion(
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


def load_tick_table(path: Path | None = None) -> TickTable:
    source = DEFAULT_TICK_TABLE_PATH if path is None else Path(path)
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


def resolve_tick_from_table(table: TickTable, *, session_date: date, band_reference_price: Decimal) -> TickSize:
    """Pick the dated tick for a session from the band holding the reference price."""
    with decimal.localcontext(COST_CONTEXT):
        reference = positive_decimal(band_reference_price, "band_reference_price")
        for version in table.versions:
            if not version.covers(session_date):
                continue
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
        raise TickSizeUnavailable(
            f"no tick table version covers {session_date.isoformat()}; that date has no sourced tick version (not guessed)"
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
