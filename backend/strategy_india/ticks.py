"""The single tick-size adapter (the only module in this package that may import ``costs.ticks``).

``resolve_nse_cash_tick`` takes an explicit
``InstrumentClass`` and series, has no defaults, and fails closed for Gold ETFs, unlisted
series and dates no circular covers. This adapter maps this package's class names onto that
enum (``EQUITY`` for universe stocks, ``NON_GOLD_ETF`` for the benchmark ETF) and keeps the
registered tables for gating and the registration hash. Registrations must match the committed
schedules used by the public resolver. Any ``TickSizeUnavailable``
(including ETF dates no circular covers, such as 2025-04-15 to 2026-09-06) surfaces as a
recorded attempt, fold-level unknown or an INCONCLUSIVE verdict, never as a default tick.

The one exception is operator decision A5: for the configured benchmark ETF only, and only inside a date
window no committed ETF version covers, the tick may come from ``costs.tick_inference`` (the security's own
traded prices). ``TickTables`` holds those inferred sources, the committed rows win wherever they cover a
date, and the inference provenance is part of ``TickTables.sha256()``, which the registration seals.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from costs import tick_inference as _inference
from costs.core import Side, TickSizeUnavailable, positive_decimal
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
    """Tick tables keyed by instrument class. A class without a table fails closed.

    ``inferred`` holds per-security sources for NON_GOLD_ETF dates the committed table leaves uncovered.
    Each must sit exactly on one such window of the committed table, so it cannot answer a date a schedule
    row covers, and it is answered only for the security it names.
    """

    def __init__(
        self,
        tables: Mapping[str, TickTable],
        inferred: Iterable[_inference.InferredTickSource] = (),
    ) -> None:
        for name, table in tables.items():
            if name not in SUPPORTED_CLASSES:
                raise TickSizeUnavailable(f"instrument class {name!r} is not supported")
            if table != committed_tick_table(_COSTS_CLASS[name]):
                raise TickSizeUnavailable(f"tick table for {name} differs from the committed schedule")
        self._tables = dict(tables)
        sources = tuple(inferred)
        if sources and NON_GOLD_ETF not in self._tables:
            raise TickSizeUnavailable("an inferred ETF tick needs the NON_GOLD_ETF table to be registered")
        windows = _inference.uncovered_windows(self._tables[NON_GOLD_ETF]) if sources else ()
        seen: set[tuple[str, date, date]] = set()
        for source in sources:
            key = (source.security, source.window_start, source.window_end)
            if (source.window_start, source.window_end) not in windows:
                raise TickSizeUnavailable(
                    f"inferred tick window {source.window_start}..{source.window_end} is not a date range "
                    f"the NON_GOLD_ETF schedule leaves uncovered"
                )
            if key in seen:
                raise TickSizeUnavailable(f"two inferred ticks for {source.security} in the same window")
            seen.add(key)
        self._inferred = tuple(sorted(sources, key=lambda s: (s.security, s.window_start)))

    def classes(self) -> tuple[str, ...]:
        return tuple(sorted(self._tables))

    def sha256(self) -> str:
        """Hash over every class's version ids and version hashes, plus any inferred tick provenance.

        With no inferred source the body is exactly what it was before inference existed, so a registration
        sealed without inference keeps its hash. With inferred sources the provenance hashes (method, window,
        sample counts, tick, input rows hash) are part of the body, so changed input rows change the seal.
        """
        body: dict[str, Any] = {
            name: [[v.version, v.version_hash] for v in table.versions] for name, table in sorted(self._tables.items())
        }
        if self._inferred:
            body["inferred_etf_ticks"] = [
                [s.security, s.window_start.isoformat(), s.window_end.isoformat(), s.provenance_sha256]
                for s in self._inferred
            ]
        return canonical_sha256(body)

    def inference_provenance(self) -> list[dict[str, Any]]:
        """The full provenance records of the inferred sources, for the operator's output."""
        return [{**s.provenance, "provenance_sha256": s.provenance_sha256} for s in self._inferred]

    def inferred_for(self, security: str, day: date) -> _inference.InferredTickSource | None:
        """The inferred source for this security whose window holds ``day``, answerable or not."""
        for source in self._inferred:
            if source.security == security and source.in_window(day):
                return source
        return None

    def schedule_covers(self, instrument_class: str, day: date, *, series: str) -> bool:
        """Whether a committed schedule row covers the class, series and session."""
        try:
            table = self.table_for(instrument_class)
        except TickSizeUnavailable:
            return False
        return any(version.covers(day) and series in version.series for version in table.versions)

    def covers(self, instrument_class: str, day: date, *, series: str, security: str | None = None) -> bool:
        """Check class, series and session coverage without reading a price.

        ``security`` names the instrument. Only a NON_GOLD_ETF date no schedule row covers can be answered by
        an inferred source for that same security, and only when the inference is available.
        """
        if series not in SUPPORTED_SERIES:
            return False
        if self.schedule_covers(instrument_class, day, series=series):
            return True
        if instrument_class != NON_GOLD_ETF or security is None:
            return False
        source = self.inferred_for(security, day)
        return source is not None and source.covers(day)

    def uncovered_reason(self, instrument_class: str, day: date, *, security: str | None) -> str | None:
        """Why an inferred source exists for this security and date but cannot answer, if so."""
        if instrument_class != NON_GOLD_ETF or security is None:
            return None
        source = self.inferred_for(security, day)
        return None if source is None or source.covers(day) else source.reason

    def table_for(self, instrument_class: str) -> TickTable:
        if instrument_class not in SUPPORTED_CLASSES:
            raise TickSizeUnavailable(f"instrument class {instrument_class!r} is not supported; nothing defaults")
        table = self._tables.get(instrument_class)
        if table is None:
            raise TickSizeUnavailable(f"no tick table is encoded for instrument class {instrument_class}")
        return table


def infer_benchmark_ticks(
    rows: Iterable[Any], benchmark_isins: Sequence[str]
) -> tuple[_inference.InferredTickSource, ...]:
    """Infer the ETF tick for each configured benchmark ISIN over each window the committed ETF table leaves
    uncovered, from the Phase 59 dataset rows (raw open, high, low and close of series EQ). Unavailable
    results are kept: they are sealed too, and they make the preflight refuse with their reason."""
    wanted = set(benchmark_isins)
    by_security: dict[str, list[_inference.TickObservation]] = {}
    for row in rows:
        if row.anchor_isin in wanted and row.series == "EQ":
            day = getattr(row, "trade_date", None) or row.session
            by_security.setdefault(row.anchor_isin, []).append(
                _inference.TickObservation(day, (row.raw_open, row.raw_high, row.raw_low, row.raw_close))
            )
    windows = _inference.uncovered_windows(committed_tick_table(InstrumentClass.NON_GOLD_ETF))
    return _inference.infer_ticks(securities=sorted(wanted), windows=windows, observations_by_security=by_security)


def load_default_tables(*, rows: Iterable[Any] | None = None, benchmark_isins: Sequence[str] = ()) -> TickTables:
    """The committed equity and non-Gold ETF tables, read as provenance data only.

    With ``rows`` and ``benchmark_isins`` the configured benchmark ETFs also get inferred ticks for the
    uncovered ETF window (A5). Without them nothing is inferred and the gap stays uncovered.
    """
    tables = {name: committed_tick_table(cls) for name, cls in _COSTS_CLASS.items()}
    if not benchmark_isins:
        return TickTables(tables)
    if rows is None:
        raise TickSizeUnavailable("benchmark ISINs were given without dataset rows to infer from")
    return TickTables(tables, inferred=infer_benchmark_ticks(rows, benchmark_isins))


def resolve_tick(
    tables: TickTables,
    *,
    session_date: date,
    band_reference_price: Decimal,
    instrument_class: str,
    series: str,
    security: str | None = None,
) -> TickSize:
    """Resolve a tick. Committed schedule rows are authoritative wherever they cover ``session_date``.

    ``security`` matters only for NON_GOLD_ETF on a date no schedule row covers: then an inferred source for
    that same security answers, or the date stays unavailable. Nothing defaults.
    """
    if series not in SUPPORTED_SERIES:
        raise TickSizeUnavailable(f"series {series!r} is out of scope for tick lookup")
    tables.table_for(instrument_class)  # the class must be registered; nothing defaults
    if security is not None and instrument_class == NON_GOLD_ETF:
        # No schedule check is needed here: TickTables only accepts a source whose window is a date range
        # no committed version covers, so inferred_for() can only hold a date the schedule leaves open.
        source = tables.inferred_for(security, session_date)
        if source is not None:
            positive_decimal(band_reference_price, "band_reference_price")
            return source.tick_size(session_date)
    return resolve_nse_cash_tick(
        session_date=session_date,
        band_reference_price=band_reference_price,
        instrument_class=_COSTS_CLASS[instrument_class],
        series=series,
    ).tick


def align_limit(price: Decimal, tick: TickSize, side: Side) -> Decimal:
    """Floor a buy limit and ceil a sell limit to the tick."""
    return _align_limit(price, tick, side)
