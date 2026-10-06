"""Per-security tick inferred from the security's own traded prices (operator decision A5, 2026-10-07).

NSE circulars for 2025-04-15 to 2026-09-06 say only "Rs 0.01 / Rs 0.05 as per respective ETF", so the
committed non-Gold ETF table leaves that window uncovered on purpose. For ONE named security the tick can
instead be read off the grid its prices sit on, and only inside such an uncovered window:

* any open, high, low or close that is not a multiple of 0.05 proves the tick is 0.01;
* a large enough sample in which every price is a multiple of 0.05 gives 0.05, because a 0.01-tick
  security would land on the 0.05 grid with probability 0.2 per distinct price (at least 50 distinct
  prices bound that below 1e-34);
* everything else is unavailable and the caller refuses, as it does for an uncovered date today.

Unavailable is the answer for: too few sessions, prices or distinct prices; a missing, non-Decimal or
non-positive price; a price off the 0.01 grid (not an as-traded price); a duplicate session; and a mixed
sample (some prices prove 0.01, yet a run of more than ``MAX_ALIGNED_RUN`` consecutive sessions has every
price on the 0.05 grid, which a single 0.01-tick regime does not produce, so the tick may have changed
inside the window). 0.05 is the pessimistic answer for k-tick costs; 0.01 is the optimistic one, so a
sample that cannot rule out 0.05 never resolves to 0.01 by default.

The schedule rows stay authoritative: ``uncovered_windows`` only ever returns dates NO version covers,
and ``InferredTickSource`` answers only inside its window. Every result, available or not, carries
provenance (method, window, sample counts, thresholds, the tick, a hash of the input rows) and a hash of
that provenance, which the caller seals.
"""

from __future__ import annotations

import decimal
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from .core import COST_CONTEXT, TickSizeUnavailable, canonical_json, sha256_hex
from .fills import TickSize
from .ticks import TickTable

METHOD = "price-grid-inference/1"
COARSE_GRID = Decimal("0.05")
FINE_GRID = Decimal("0.01")
MIN_SESSIONS = 100
MIN_PRICES = 400
MIN_DISTINCT_PRICES = 50
MAX_ALIGNED_RUN = 20
THRESHOLDS = {
    "min_sessions": MIN_SESSIONS,
    "min_prices": MIN_PRICES,
    "min_distinct_prices": MIN_DISTINCT_PRICES,
    "max_aligned_run": MAX_ALIGNED_RUN,
}
INFERRED = "inferred"
UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class TickObservation:
    """The prices of one session (open, high, low, close in any order the caller keeps stable)."""

    session: date
    prices: tuple[Any, ...]


def uncovered_windows(table: TickTable) -> tuple[tuple[date, date], ...]:
    """Inclusive date ranges strictly between two versions that no version covers.

    Dates before the first version and after the last are not windows: before 2021-01-01 nothing is
    sourced, and the last version is open-ended.
    """
    out: list[tuple[date, date]] = []
    for earlier, later in zip(table.versions, table.versions[1:]):
        if earlier.effective_to is None:
            continue  # the table loader refuses this shape; nothing to infer from it
        start, end = earlier.effective_to + timedelta(days=1), later.effective_from - timedelta(days=1)
        if start <= end:
            out.append((start, end))
    return tuple(out)


def _price_text(price: Any) -> str:
    if isinstance(price, Decimal) and price.is_finite():
        return format(price.normalize(), "f")
    return f"invalid:{type(price).__name__}"


def _valid(price: Any) -> bool:
    return isinstance(price, Decimal) and not isinstance(price, bool) and price.is_finite() and price > 0


def _on_grid(price: Decimal, grid: Decimal) -> bool:
    try:
        with decimal.localcontext(COST_CONTEXT):
            return price % grid == 0
    except decimal.InvalidOperation:
        return False  # a price too large to test is not an as-traded price: it counts as off the grid


@dataclass(frozen=True)
class InferredTickSource:
    """One security's inferred tick (or the recorded reason there is none) for one uncovered window."""

    security: str
    window_start: date
    window_end: date
    status: str
    tick: Decimal | None
    reason: str | None
    provenance: Mapping[str, Any]
    provenance_sha256: str

    def __post_init__(self) -> None:
        # Only ``infer_tick`` may produce a consistent source: a hand-built one with a different tick,
        # window or reason than its provenance, or with a provenance hash that does not match, is refused.
        record = self.provenance
        consistent = (
            isinstance(record, Mapping)
            and self.provenance_sha256 == sha256_hex(canonical_json(record))
            and record.get("method") == METHOD
            and record.get("security") == self.security
            and record.get("window_start") == self.window_start.isoformat()
            and record.get("window_end") == self.window_end.isoformat()
            and record.get("status") == self.status
            and record.get("tick") == (str(self.tick) if self.tick is not None else None)
            and record.get("reason") == self.reason
            and (self.status, self.tick in (COARSE_GRID, FINE_GRID)) in ((INFERRED, True), (UNAVAILABLE, False))
        )
        if not consistent:
            raise TickSizeUnavailable("an inferred tick source must match its provenance and its hash")

    def covers(self, day: date) -> bool:
        if not isinstance(day, date) or isinstance(day, datetime):
            return False
        return self.status == INFERRED and self.window_start <= day <= self.window_end

    def in_window(self, day: date) -> bool:
        return isinstance(day, date) and not isinstance(day, datetime) and self.window_start <= day <= self.window_end

    def tick_size(self, day: date) -> TickSize:
        if self.status != INFERRED or self.tick is None:
            raise TickSizeUnavailable(
                f"no tick can be inferred for {self.security} in {self.window_start.isoformat()}.."
                f"{self.window_end.isoformat()}: {self.reason}"
            )
        if not self.in_window(day):
            raise TickSizeUnavailable(f"{day} is outside the inference window of {self.security}")
        return TickSize(
            value=self.tick,
            effective_from=self.window_start,
            effective_to=self.window_end,
            source=f"{METHOD}:{self.security}",
            source_hash=self.provenance_sha256,
        )


def infer_tick(
    *,
    security: str,
    window_start: date,
    window_end: date,
    observations: Iterable[TickObservation],
) -> InferredTickSource:
    """Infer the tick of one security over one window from its own prices. Never defaults, never raises
    for thin or odd data: an unavailable result is recorded (and sealed) with its reason instead."""
    if not isinstance(security, str) or not security.strip():
        raise TickSizeUnavailable("an inferred tick needs a named security")
    if window_end < window_start:
        raise TickSizeUnavailable("the inference window ends before it starts")
    sample = sorted(
        (obs for obs in observations if window_start <= obs.session <= window_end), key=lambda obs: obs.session
    )
    input_rows_sha256 = sha256_hex(
        canonical_json([[obs.session.isoformat(), [_price_text(p) for p in obs.prices]] for obs in sample])
    )
    reason: str | None = None
    prices: list[Decimal] = []
    aligned_run = longest_run = 0
    sessions = [obs.session for obs in sample]
    if len(set(sessions)) != len(sessions):
        reason = "a session appears more than once in the sample"
    for obs in sample:
        if reason is not None:
            break
        if not obs.prices or not all(_valid(p) for p in obs.prices):
            reason = f"missing, non-Decimal or non-positive price on {obs.session.isoformat()}"
            break
        prices.extend(obs.prices)
        if all(_on_grid(p, COARSE_GRID) for p in obs.prices):
            aligned_run += 1
            longest_run = max(longest_run, aligned_run)
        else:
            aligned_run = 0
    off_fine = 0 if reason else sum(1 for p in prices if not _on_grid(p, FINE_GRID))
    off_coarse = 0 if reason else sum(1 for p in prices if not _on_grid(p, COARSE_GRID))
    distinct = len(set(prices))
    tick: Decimal | None = None
    if reason is None and off_fine:
        reason = f"{off_fine} prices are not multiples of 0.01, so they are not as-traded prices"
    elif reason is None and (len(sample) < MIN_SESSIONS or len(prices) < MIN_PRICES or distinct < MIN_DISTINCT_PRICES):
        reason = (
            f"sample too small: {len(sample)} sessions, {len(prices)} prices, {distinct} distinct prices "
            f"(need {MIN_SESSIONS}, {MIN_PRICES}, {MIN_DISTINCT_PRICES})"
        )
    elif reason is None and off_coarse and longest_run > MAX_ALIGNED_RUN:
        reason = (
            f"mixed sample: {off_coarse} prices prove 0.01 but {longest_run} consecutive sessions sit on the "
            f"0.05 grid, so the tick may have changed inside the window"
        )
    elif reason is None:
        tick = FINE_GRID if off_coarse else COARSE_GRID
    provenance: dict[str, Any] = {
        "method": METHOD,
        "security": security,
        "series": "EQ",
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "status": INFERRED if tick is not None else UNAVAILABLE,
        "tick": str(tick) if tick is not None else None,
        "reason": reason,
        "sessions": len(sample),
        "prices": len(prices),
        "distinct_prices": distinct,
        "prices_off_0_05": off_coarse,
        "prices_off_0_01": off_fine,
        "longest_run_on_0_05": longest_run,
        "thresholds": dict(THRESHOLDS),
        "input_rows_sha256": input_rows_sha256,
    }
    return InferredTickSource(
        security=security,
        window_start=window_start,
        window_end=window_end,
        status=provenance["status"],
        tick=tick,
        reason=reason,
        provenance=provenance,
        provenance_sha256=sha256_hex(canonical_json(provenance)),
    )


def infer_ticks(
    *,
    securities: Sequence[str],
    windows: Sequence[tuple[date, date]],
    observations_by_security: Mapping[str, Sequence[TickObservation]],
) -> tuple[InferredTickSource, ...]:
    """One result per (security, window), including the unavailable ones, in a stable order."""
    return tuple(
        infer_tick(
            security=security, window_start=start, window_end=end,
            observations=observations_by_security.get(security, ()),
        )
        for security in sorted(set(securities))
        for start, end in windows
    )
