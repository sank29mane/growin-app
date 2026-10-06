"""The single tick-size adapter (the only module in this package that may import ``costs.ticks``).

``costs.ticks`` has no instrument classification: ``load_tick_table()`` with no path
silently loads the equity table and the series is not an input. This adapter makes both
explicit. Every lookup names an instrument class (``EQUITY`` for universe stocks,
``NON_GOLD_ETF`` for the benchmark ETF) and a series, there are no defaults, and anything
else raises ``TickSizeUnavailable``. A table is registered per class; today only the equity
table is encoded, so a ``NON_GOLD_ETF`` lookup fails closed. Any ``TickSizeUnavailable``
(including ETF dates no circular covers) surfaces as a recorded attempt, fold-level
unknown or an INCONCLUSIVE verdict, never as a default tick.

When PR #539 adds the public resolver (``resolve_nse_cash_tick``), the swap is the body of
``resolve_tick`` and ``load_default_tables`` below.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path

from costs import ticks as _costs_ticks
from costs.core import Side, TickSizeUnavailable
from costs.fills import TickSize

from .registry import canonical_sha256

EQUITY = "EQUITY"
NON_GOLD_ETF = "NON_GOLD_ETF"
SUPPORTED_CLASSES = (EQUITY, NON_GOLD_ETF)
SUPPORTED_SERIES = ("EQ",)
# Explicit path: the no-argument default of load_tick_table() is never used.
EQUITY_TABLE_PATH = _costs_ticks.DEFAULT_TICK_TABLE_PATH


class TickTables:
    """Tick tables keyed by instrument class. A class without a table fails closed."""

    def __init__(self, tables: Mapping[str, _costs_ticks.TickTable]) -> None:
        for name in tables:
            if name not in SUPPORTED_CLASSES:
                raise TickSizeUnavailable(f"instrument class {name!r} is not supported")
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
        return any(version.covers(day) for version in table.versions)

    def table_for(self, instrument_class: str) -> _costs_ticks.TickTable:
        if instrument_class not in SUPPORTED_CLASSES:
            raise TickSizeUnavailable(f"instrument class {instrument_class!r} is not supported; nothing defaults")
        table = self._tables.get(instrument_class)
        if table is None:
            raise TickSizeUnavailable(f"no tick table is encoded for instrument class {instrument_class}")
        return table


def load_table(path: Path) -> _costs_ticks.TickTable:
    """Load one table from an explicit path (never the no-argument default)."""
    return _costs_ticks.load_tick_table(Path(path))


def load_default_tables() -> TickTables:
    """Today's main: only the equity table exists. Loaded from an explicit path."""
    return TickTables({EQUITY: _costs_ticks.load_tick_table(EQUITY_TABLE_PATH)})


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
    table = tables.table_for(instrument_class)
    return _costs_ticks.resolve_tick_from_table(
        table, session_date=session_date, band_reference_price=band_reference_price
    )


def align_limit(price: Decimal, tick: TickSize, side: Side) -> Decimal:
    """Floor a buy limit and ceil a sell limit to the tick."""
    return _costs_ticks.align_limit(price, tick, side)
