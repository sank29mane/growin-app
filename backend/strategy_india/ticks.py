"""The single tick-size adapter (the only module in this package that may import ``costs.ticks``).

``resolve_nse_cash_tick`` takes an explicit
``InstrumentClass`` and series, has no defaults, and fails closed for Gold ETFs, unlisted
series and dates no circular covers. This adapter maps this package's class names onto that
enum (``EQUITY`` for universe stocks, ``NON_GOLD_ETF`` for the benchmark ETF) and keeps the
registered tables for gating and the registration hash. Registrations must match the committed
schedules used by the public resolver. Any ``TickSizeUnavailable``
(including ETF dates no circular covers, such as 2025-04-15 to 2026-09-06) surfaces as a
recorded attempt, fold-level unknown or an INCONCLUSIVE verdict, never as a default tick.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from costs.core import Side, TickSizeUnavailable
from costs.fills import TickSize
from costs.ticks import (
    InstrumentClass,
    TickTable,
    committed_tick_table,
    resolve_nse_cash_tick,
)
from costs.ticks import align_limit as _align_limit

from .registry import canonical_sha256

EQUITY = "EQUITY"
NON_GOLD_ETF = "NON_GOLD_ETF"
SUPPORTED_CLASSES = (EQUITY, NON_GOLD_ETF)
SUPPORTED_SERIES = ("EQ",)
_COSTS_CLASS = {EQUITY: InstrumentClass.EQUITY, NON_GOLD_ETF: InstrumentClass.NON_GOLD_ETF}


class TickTables:
    """Tick tables keyed by instrument class. A class without a table fails closed."""

    def __init__(self, tables: Mapping[str, TickTable]) -> None:
        for name, table in tables.items():
            if name not in SUPPORTED_CLASSES:
                raise TickSizeUnavailable(f"instrument class {name!r} is not supported")
            if table != committed_tick_table(_COSTS_CLASS[name]):
                raise TickSizeUnavailable(f"tick table for {name} differs from the committed schedule")
        self._tables = dict(tables)

    def classes(self) -> tuple[str, ...]:
        return tuple(sorted(self._tables))

    def sha256(self) -> str:
        """Hash over every class's version ids and version hashes, for the registration."""
        body = {
            name: [[v.version, v.version_hash] for v in table.versions] for name, table in sorted(self._tables.items())
        }
        return canonical_sha256(body)

    def covers(self, instrument_class: str, day: date, *, series: str) -> bool:
        """Check class, series and session coverage without reading a price."""
        if series not in SUPPORTED_SERIES:
            return False
        try:
            table = self.table_for(instrument_class)
        except TickSizeUnavailable:
            return False
        return any(version.covers(day) and series in version.series for version in table.versions)

    def table_for(self, instrument_class: str) -> TickTable:
        if instrument_class not in SUPPORTED_CLASSES:
            raise TickSizeUnavailable(f"instrument class {instrument_class!r} is not supported; nothing defaults")
        table = self._tables.get(instrument_class)
        if table is None:
            raise TickSizeUnavailable(f"no tick table is encoded for instrument class {instrument_class}")
        return table


def load_default_tables() -> TickTables:
    """The committed equity and non-Gold ETF tables, read as provenance data only."""
    return TickTables({name: committed_tick_table(cls) for name, cls in _COSTS_CLASS.items()})


def resolve_tick(
    tables: TickTables,
    *,
    session_date: date,
    band_reference_price: Decimal,
    instrument_class: str,
    series: str,
) -> TickSize:
    if series not in SUPPORTED_SERIES:
        raise TickSizeUnavailable(f"series {series!r} is out of scope for tick lookup")
    tables.table_for(instrument_class)  # the class must be registered; nothing defaults
    return resolve_nse_cash_tick(
        session_date=session_date,
        band_reference_price=band_reference_price,
        instrument_class=_COSTS_CLASS[instrument_class],
        series=series,
    ).tick


def align_limit(price: Decimal, tick: TickSize, side: Side) -> Decimal:
    """Floor a buy limit and ceil a sell limit to the tick."""
    return _align_limit(price, tick, side)
