"""Benchmarks (D-17).

* Primary: a liquid Nifty ETF chosen from the 59 dataset by median traded value
  (ADV) and bar completeness over the window it will be judged on. Bought at the
  first session's raw close and sold at the last session's raw close with one
  round trip priced by the 60 estimator. It is a Nifty 50 proxy, not Nifty 500.
* Secondary: Nifty 500 TRI, loaded only by path plus sha256. A missing file or a
  bad hash gives the label "TRI unavailable" and no substitute. Only a derived
  period return leaves this module; no TRI level is stored, printed or written.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from costs.charges import estimate_delivery_round_trip
from costs.schedule import PricingBasis, ScheduleSet

from .data import DatasetView
from .errors import DataError
from .params import BenchmarkSpec
from .ticks import NON_GOLD_ETF, TickTables, resolve_tick

ZERO = Decimal(0)
ONE = Decimal(1)
TRI_LABEL = "TRI unavailable"
_DATE_FORMATS = ("%Y-%m-%d", "%d %b %Y", "%d-%b-%Y")


@dataclass(frozen=True)
class EtfChoice:
    anchor_isin: str
    stock_code: str
    median_traded_value: Decimal
    completeness: Decimal
    considered: tuple[tuple[str, str, str], ...]  # (anchor, median traded value, completeness)


def choose_etf(view: DatasetView, spec: BenchmarkSpec, *, start: date, end: date) -> EtfChoice:
    """Highest median ADV among candidates with enough bar completeness. Ties break on the ISIN."""
    sessions = view.sessions(start, end)
    if not sessions:
        raise DataError("no sessions in the benchmark window")
    floor = Decimal(spec.min_completeness)
    scored: list[tuple[Decimal, str, str, Decimal]] = []
    considered: list[tuple[str, str, str]] = []
    for anchor in sorted(spec.candidate_isins):
        bars = [bar for bar in view.series(anchor, end=end) if bar.session >= start]
        if not bars:
            continue
        completeness = Decimal(len(bars)) / Decimal(len(sessions))
        values = sorted(bar.traded_value for bar in bars)
        mid = len(values) // 2
        median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
        considered.append((anchor, str(median), str(completeness)))
        if completeness >= floor:
            scored.append((median, anchor, bars[0].stock_code, completeness))
    if not scored:
        raise DataError("no candidate ETF has enough bar completeness in the window")
    scored.sort(key=lambda item: (-item[0], item[1]))
    median, anchor, code, completeness = scored[0]
    return EtfChoice(anchor, code, median, completeness, tuple(considered))


@dataclass(frozen=True)
class EtfResult:
    anchor_isin: str
    shares: int
    entry_price: Decimal
    exit_price: Decimal
    round_trip_cost: Decimal
    gross_return: Decimal
    net_return: Decimal
    curve: tuple[tuple[date, Decimal], ...]  # equity marked each session, round trip cost taken at the exit


def etf_buy_and_hold(
    view: DatasetView,
    anchor: str,
    *,
    start: date,
    end: date,
    capital: Decimal,
    isin_for_costs: str | None = None,
    ticks: TickTables,
    schedules: ScheduleSet,
    pricing_basis: PricingBasis,
) -> EtfResult:
    sessions = [s for s in view.sessions(start, end) if view.bar(anchor, s) is not None]
    if len(sessions) < 2:
        raise DataError("the ETF needs at least two bars in the window")
    first, last = view.bar(anchor, sessions[0]), view.bar(anchor, sessions[-1])
    assert first is not None and last is not None
    # The ETF is NON_GOLD_ETF. A date no tick table covers raises TickSizeUnavailable: the benchmark is then
    # unknown (fold-level missing evidence, holdout INCONCLUSIVE). Nothing defaults.
    for bar in (first, last):
        resolve_tick(ticks, session_date=bar.session, band_reference_price=bar.raw_close,
                     instrument_class=NON_GOLD_ETF, series=bar.series)
    shares = int(capital // first.raw_close)
    if shares < 1:
        raise DataError("the capital cannot buy one ETF share")
    estimate = estimate_delivery_round_trip(
        workspace="india", currency="INR", isin=isin_for_costs or first.isin, exchange="NSE", quantity=shares,
        buy_price=first.raw_close, sell_price=last.raw_close, buy_date=sessions[0], sell_date=sessions[-1],
        schedules=schedules, pricing_basis=pricing_basis,
    )
    leftover = capital - shares * first.raw_close
    curve: list[tuple[date, Decimal]] = []
    for s in sessions:
        bar = view.bar(anchor, s)
        assert bar is not None
        value = leftover + shares * bar.raw_close
        curve.append((s, value - (estimate.total if s == sessions[-1] else ZERO)))
    gross = shares * (last.raw_close - first.raw_close) / capital
    return EtfResult(anchor, shares, first.raw_close, last.raw_close, estimate.total, gross,
                     curve[-1][1] / capital - ONE, tuple(curve))


# ---- TRI -----------------------------------------------------------------------------
@dataclass(frozen=True)
class TriUnavailable:
    reason: str
    label: str = TRI_LABEL


@dataclass(frozen=True)
class TriSeries:
    sha256: str
    _levels: tuple[tuple[date, Decimal], ...]

    def level_on_or_before(self, day: date) -> Decimal | None:
        found: Decimal | None = None
        for when, level in self._levels:
            if when <= day:
                found = level
            else:
                break
        return found

    def period_return(self, start: date, end: date) -> Decimal | None:
        """Derived total return between two dates. The only TRI-derived figure that leaves this module."""
        a, b = self.level_on_or_before(start), self.level_on_or_before(end)
        if a is None or b is None or a <= 0:
            return None
        return b / a - ONE


def _parse_date(text: str) -> date:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError("unrecognised date format")


def load_tri(path: Path | None, expected_sha256: str | None) -> TriSeries | TriUnavailable:
    """Load a gross TRI CSV by path plus sha256. Any problem gives ``TriUnavailable`` and never a substitute."""
    if path is None or expected_sha256 is None:
        return TriUnavailable("no_reference")
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return TriUnavailable("file_missing")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        return TriUnavailable("hash_mismatch")
    try:
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        names = {name.strip().lower(): name for name in (reader.fieldnames or [])}
        date_col = next(orig for low, orig in names.items() if "date" in low)
        level_col = next(orig for low, orig in names.items() if low == "total returns index")
        levels = sorted((_parse_date(row[date_col]), Decimal(row[level_col].strip())) for row in reader)
    except (StopIteration, ValueError, InvalidOperation, KeyError, UnicodeDecodeError, TypeError):
        return TriUnavailable("unreadable")
    if len(levels) < 2:
        return TriUnavailable("too_short")
    return TriSeries(digest, tuple(levels))
