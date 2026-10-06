"""Cross-sectional momentum signals and market-level features (D-04, D-05, D-20).

Everything is Decimal. Signals read adjusted prices only; a quarantined row, a
session gap or a missing price inside a window gives no signal (D-05).

Dividend handling (D-20, only when ``dividend_amount_unknown`` events are
supplied):

* ``base``: the ex-date return is the open-to-close return, so the ex-date gap
  (previous close to ex-date open) counts as zero in signal and volatility
  returns.
* ``sens2pct``: adjusted prices before each ex-date are multiplied by the
  sealed sensitivity factor (D-19 criteria, 0.98 in the proposal; never a literal here) and the gap is not zeroed, which is the "assume a 2%
  dividend" alternative.

With no events, every mode is the same plain adjusted-price return series.
"""

from __future__ import annotations

import bisect
import decimal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from costs.core import LookaheadError

from .data import Bar, DatasetView, DecisionView, DividendEvents
from .params import StrategyParams

MODE_BASE = "base"
MODE_SENSITIVITY = "sens2pct"
_CTX = decimal.Context(prec=40, rounding=decimal.ROUND_HALF_EVEN)
_ONE = Decimal(1)
_ZERO = Decimal(0)


class _Series:
    """Per-name arrays aligned to its visible rows."""

    __slots__ = ("bars", "dates", "ret", "price", "bad", "s1", "s2", "ps")

    def __init__(self, bars: tuple[Bar, ...], ret: list[Decimal | None]) -> None:
        self.bars = bars
        self.dates = [bar.session for bar in bars]
        self.ret = ret
        n = len(bars)
        self.price: list[Decimal] = [_ONE] * n
        self.bad: list[int] = [0] * n
        self.s1: list[Decimal] = [_ZERO] * n
        self.s2: list[Decimal] = [_ZERO] * n
        self.ps: list[Decimal] = [_ONE] * n  # running sum of the price index, for breadth
        for i in range(1, n):
            r = ret[i]
            self.bad[i] = self.bad[i - 1] + (1 if r is None else 0)
            use = _ZERO if r is None else r
            self.price[i] = self.price[i - 1] * (_ONE + use)
            self.s1[i] = self.s1[i - 1] + use
            self.s2[i] = self.s2[i - 1] + use * use
            self.ps[i] = self.ps[i - 1] + self.price[i]


def _factor_for(session: date, events: Sequence[date], factor: Decimal) -> Decimal:
    count = len(events) - bisect.bisect_right(events, session)
    return factor**count if count else _ONE


def _returns(bars: tuple[Bar, ...], calendar_index: Mapping[date, int], ex_dates: frozenset[date], mode: str,
             factor: Decimal | None) -> list[Decimal | None]:
    events = sorted(ex_dates)
    out: list[Decimal | None] = [None] * len(bars)
    for i in range(1, len(bars)):
        prev, cur = bars[i - 1], bars[i]
        if prev.quarantined or cur.quarantined or prev.adj_close is None or cur.adj_close is None:
            continue
        if calendar_index[cur.session] - calendar_index[prev.session] != 1:
            continue
        if mode == MODE_SENSITIVITY and events:
            assert factor is not None
            out[i] = cur.adj_close * _factor_for(cur.session, events, factor) / (prev.adj_close * _factor_for(prev.session, events, factor)) - _ONE
        elif mode == MODE_BASE and cur.session in ex_dates:
            if cur.adj_open is None or cur.adj_open <= 0:
                continue
            out[i] = cur.adj_close / cur.adj_open - _ONE
        else:
            out[i] = cur.adj_close / prev.adj_close - _ONE
    return out


@dataclass(frozen=True)
class FeatureRow:
    session: date
    volatility: Decimal
    drawdown: Decimal
    breadth: Decimal


class SignalTable:
    """Precomputed signal state for one view, parameter set and dividend mode."""

    def __init__(self, view: DatasetView, params: StrategyParams, events: DividendEvents, *, mode: str = MODE_BASE,
                 sensitivity_factor: Decimal | None = None) -> None:
        if mode == MODE_SENSITIVITY and (sensitivity_factor is None or not _ZERO < sensitivity_factor < _ONE):
            raise ValueError("the sensitivity run needs the sealed factor, strictly between 0 and 1")
        self.params = params
        self.mode = mode
        sessions = view.sessions()
        self._calendar = sessions
        cal_index = {day: i for i, day in enumerate(sessions)}
        self._cal_index = cal_index
        self._series: dict[str, _Series] = {}
        with decimal.localcontext(_CTX):
            for anchor in view.anchors():
                bars = view.series(anchor)
                self._series[anchor] = _Series(bars, _returns(bars, cal_index, events.ex_dates(anchor), mode, sensitivity_factor))

    # ---- per-name -------------------------------------------------------------
    def _locate(self, anchor: str, day: date) -> tuple[_Series, int] | None:
        series = self._series.get(anchor)
        if series is None:
            return None
        i = bisect.bisect_left(series.dates, day)
        if i < len(series.dates) and series.dates[i] == day:
            return series, i
        return None

    def raw_score(self, anchor: str, day: date) -> Decimal | None:
        """Momentum (optionally divided by daily volatility) known at the close of ``day``, or None."""
        found = self._locate(anchor, day)
        if found is None:
            return None
        series, i = found
        p = self.params
        end = i - p.skip_sessions
        start = end - p.lookback_sessions
        vol_start = i - p.vol_window
        if start < 0 or vol_start < 0:
            return None
        if series.bad[end] - series.bad[start] != 0 or series.bad[i] - series.bad[vol_start] != 0:
            return None
        if series.bars[i].quarantined:
            return None
        with decimal.localcontext(_CTX):
            momentum = series.price[end] / series.price[start] - _ONE
            if not p.vol_adjusted:
                return momentum
            n = Decimal(p.vol_window)
            total = series.s1[i] - series.s1[vol_start]
            square = series.s2[i] - series.s2[vol_start]
            variance = (square - total * total / n) / (n - _ONE)
            if variance <= 0:
                return None
            return momentum / variance.sqrt()

    def last_gap_neutral_return(self, anchor: str, day: date) -> Decimal | None:
        found = self._locate(anchor, day)
        if found is None:
            return None
        series, i = found
        return series.ret[i]

    # ---- market features for the regime filter ----------------------------------
    def market_features(self, *, min_names: int = 3) -> tuple[FeatureRow, ...]:
        """Equal-weight market proxy volatility, drawdown from its rolling high and breadth, per session."""
        spec = self.params.regime
        sessions = self._calendar
        sums = [_ZERO] * len(sessions)
        counts = [0] * len(sessions)
        above = [0] * len(sessions)
        eligible_breadth = [0] * len(sessions)
        window = spec.breadth_window
        with decimal.localcontext(_CTX):
            for series in self._series.values():
                for i in range(1, len(series.bars)):
                    r = series.ret[i]
                    t = self._cal_index[series.dates[i]]
                    if r is not None:
                        sums[t] += r
                        counts[t] += 1
                    if i >= window and series.bad[i] - series.bad[i - window] == 0:
                        mean = (series.ps[i] - series.ps[i - window]) / window
                        eligible_breadth[t] += 1
                        if series.price[i] > mean:
                            above[t] += 1
            proxy: list[Decimal | None] = [None] * len(sessions)
            for t in range(len(sessions)):
                if counts[t] >= min_names:
                    proxy[t] = sums[t] / counts[t]
            index = [_ONE] * len(sessions)
            for t in range(1, len(sessions)):
                r = proxy[t]
                index[t] = index[t - 1] * (_ONE + (r if r is not None else _ZERO))
            rows: list[FeatureRow] = []
            vw = spec.vol_window
            for t in range(len(sessions)):
                window_rets = proxy[max(0, t - vw + 1): t + 1]
                if t < vw or len(window_rets) < vw or any(item is None for item in window_rets):
                    continue
                if eligible_breadth[t] < min_names:
                    continue
                n = Decimal(vw)
                mean = sum(window_rets, _ZERO) / n  # type: ignore[arg-type]
                var = sum(((item - mean) ** 2 for item in window_rets), _ZERO) / (n - _ONE)  # type: ignore[operator]
                high = max(index[max(0, t - spec.drawdown_window + 1): t + 1])
                rows.append(FeatureRow(sessions[t], var.sqrt(), index[t] / high - _ONE,
                                       Decimal(above[t]) / Decimal(eligible_breadth[t])))
        return tuple(rows)


def zscores(raw: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Cross-sectional z-scores (population standard deviation). Empty when the cross-section is flat."""
    if len(raw) < 2:
        return {}
    with decimal.localcontext(_CTX):
        n = Decimal(len(raw))
        mean = sum(raw.values(), _ZERO) / n
        var = sum(((v - mean) ** 2 for v in raw.values()), _ZERO) / n
        if var <= 0:
            return {}
        sd = var.sqrt()
        return {key: (value - mean) / sd for key, value in raw.items()}


@dataclass(frozen=True)
class DecisionContext:
    """What a provider sees at one decision date. Reads of later dates raise ``LookaheadError``."""

    as_of: date
    eligible: frozenset[str]
    view: DecisionView
    _table: SignalTable

    def raw_score(self, anchor: str, day: date | None = None) -> Decimal | None:
        when = day if day is not None else self.as_of
        if when > self.as_of:
            raise LookaheadError(f"{when.isoformat()} is after the decision date {self.as_of.isoformat()}")
        return self._table.raw_score(anchor, when)


class SignalProvider(Protocol):
    def scores(self, ctx: DecisionContext) -> Mapping[str, Decimal]: ...


class MomentumProvider:
    """The registered signal: (vol-adjusted) lookback return with a skip, on adjusted prices."""

    def scores(self, ctx: DecisionContext) -> Mapping[str, Decimal]:
        out: dict[str, Decimal] = {}
        for anchor in sorted(ctx.eligible):
            value = ctx.raw_score(anchor)
            if value is not None:
                out[anchor] = value
        return out
