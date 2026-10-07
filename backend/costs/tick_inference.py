"""Per-security tick inferred from the security's own traded prices (operator decision A5, 2026-10-07).

NSE circulars for 2025-04-15 to 2026-09-06 say only "Rs 0.01 / Rs 0.05 as per respective ETF", so the
committed non-Gold ETF table leaves that window uncovered on purpose. For ONE named security the tick can
instead be read off the grid its prices sit on, and only inside such an uncovered window:

* a sample in which every price is a multiple of 0.05 gives 0.05, because a 0.01-tick security would
  land on the 0.05 grid with probability 0.2 per distinct price (at least 50 distinct prices bound that
  below 1e-34);
* a sample in which at least half of ALL prices, and at least half of the prices in EVERY block of
  ``BLOCK_SESSIONS`` consecutive sessions, are off the 0.05 grid gives 0.01. A real 0.01 security puts
  about 80% of its prices off that grid, so a block of 20 sessions (80 prices) falls below 50% with
  probability near 1e-9, while a 0.05 security with a few corrupted prints sits near 1% off the grid and a
  security whose tick changed inside the window has whole blocks that are aligned. Those refuse; so does
  anything between "nothing off the grid" and "half of every block off the grid";
* everything else is unavailable and the caller refuses, as it does for an uncovered date today.

Unavailable is the answer for: too few sessions, prices or distinct prices; a missing, non-Decimal or
non-positive price; a price off the 0.01 grid (not an as-traded price); a duplicate session; and a mixed
sample (some prices are off the 0.05 grid, yet not half of all of them, or not half of every block). 0.05
is the pessimistic answer for k-tick costs; 0.01 is the optimistic one, so a sample that cannot rule out
0.05 never resolves to 0.01 by default.

The schedule rows stay authoritative: ``uncovered_windows`` only ever returns dates NO version covers,
and ``InferredTickSource`` answers only inside its window. Every result, available or not, carries
provenance (method, window, sample counts, thresholds, the tick, a hash of the input rows) and a hash of
that provenance, which the caller seals.

Each unavailable result has two texts. ``reason`` carries the sample counts and is sealed in the provenance
only. ``category`` is one of the ``CATEGORIES`` below, has no digit in it, and is the only text a caller may
show an operator: the sample includes holdout sessions, so a count, price or date from it must not leak.
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

METHOD = "price-grid-inference/2"
COARSE_GRID = Decimal("0.05")
FINE_GRID = Decimal("0.01")
MIN_SESSIONS = 100
MIN_PRICES = 400
MIN_DISTINCT_PRICES = 50
BLOCK_SESSIONS = 20  # rolling window; also the longest tick change at the end of the sample that still refuses
MIN_OFF_GRID_PERCENT = 50  # of a 0.01 tick security's prices (about 80% of them are off the 0.05 grid)
THRESHOLDS = {
    "min_sessions": MIN_SESSIONS,
    "min_prices": MIN_PRICES,
    "min_distinct_prices": MIN_DISTINCT_PRICES,
    "block_sessions": BLOCK_SESSIONS,
    "min_off_0_05_percent": MIN_OFF_GRID_PERCENT,
}
CATEGORY_DUPLICATE = "duplicate session"
CATEGORY_MISSING = "missing price"
CATEGORY_OFF_GRID = "price off 0.01 grid"
CATEGORY_TOO_SMALL = "sample too small"
CATEGORY_MIXED = "mixed sample / possible tick change"
CATEGORIES = (CATEGORY_DUPLICATE, CATEGORY_MISSING, CATEGORY_OFF_GRID, CATEGORY_TOO_SMALL, CATEGORY_MIXED)
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


def _worst_block(session_off: Sequence[int], session_n: Sequence[int]) -> tuple[int, int]:
    """(off-grid prices, prices) of the block with the smallest share off the 0.05 grid, over every run of
    ``BLOCK_SESSIONS`` consecutive sessions (all sessions when there are fewer). Every window is checked, so
    a change in the first or the last 20 sessions is seen however the sample is cut. Integers only."""
    size = min(BLOCK_SESSIONS, len(session_off))
    worst: tuple[int, int] | None = None
    for i in range(len(session_off) - size + 1):
        off, total = sum(session_off[i:i + size]), sum(session_n[i:i + size])
        if worst is None or off * worst[1] < worst[0] * total:
            worst = (off, total)
    return worst if worst is not None else (0, 0)


@dataclass(frozen=True)
class InferredTickSource:
    """One security's inferred tick (or the recorded reason there is none) for one uncovered window."""

    security: str
    window_start: date
    window_end: date
    status: str
    tick: Decimal | None
    reason: str | None  # carries sample counts: sealed in the provenance, never shown to an operator
    category: str | None  # one of CATEGORIES, no digits: the only failure text an operator may see
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
            and record.get("category") == self.category
            and (self.category is None) == (self.status == INFERRED)
            and (self.category is None or self.category in CATEGORIES)
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
                f"{self.window_end.isoformat()}: {self.category}"
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
    category: str | None = None
    prices: list[Decimal] = []
    session_off: list[int] = []  # prices off the 0.05 grid, per session
    session_n: list[int] = []
    sessions = [obs.session for obs in sample]
    if len(set(sessions)) != len(sessions):
        reason, category = "a session appears more than once in the sample", CATEGORY_DUPLICATE
    for obs in sample:
        if reason is not None:
            break
        if not obs.prices or not all(_valid(p) for p in obs.prices):
            reason = f"missing, non-Decimal or non-positive price on {obs.session.isoformat()}"
            category = CATEGORY_MISSING
            break
        prices.extend(obs.prices)
        session_off.append(sum(1 for p in obs.prices if not _on_grid(p, COARSE_GRID)))
        session_n.append(len(obs.prices))
    off_fine = 0 if reason else sum(1 for p in prices if not _on_grid(p, FINE_GRID))
    off_coarse = 0 if reason else sum(1 for p in prices if not _on_grid(p, COARSE_GRID))
    distinct = len(set(prices))
    worst_block = _worst_block(session_off, session_n) if not reason else (0, 0)
    tick: Decimal | None = None
    if reason is None and off_fine:
        reason = f"{off_fine} prices are not multiples of 0.01, so they are not as-traded prices"
        category = CATEGORY_OFF_GRID
    elif reason is None and (len(sample) < MIN_SESSIONS or len(prices) < MIN_PRICES or distinct < MIN_DISTINCT_PRICES):
        reason = (
            f"sample too small: {len(sample)} sessions, {len(prices)} prices, {distinct} distinct prices "
            f"(need {MIN_SESSIONS}, {MIN_PRICES}, {MIN_DISTINCT_PRICES})"
        )
        category = CATEGORY_TOO_SMALL
    elif reason is None and off_coarse and not (
        off_coarse * 100 >= MIN_OFF_GRID_PERCENT * len(prices)
        and worst_block[0] * 100 >= MIN_OFF_GRID_PERCENT * worst_block[1]
    ):
        reason = (
            f"mixed sample: {off_coarse} of {len(prices)} prices are off the 0.05 grid and the worst block of "
            f"{BLOCK_SESSIONS} sessions has {worst_block[0]} of {worst_block[1]} off it (0.01 needs at least "
            f"{MIN_OFF_GRID_PERCENT}% overall and in every block), so the tick may have changed inside the "
            f"window or some prints are corrupted"
        )
        category = CATEGORY_MIXED
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
        "category": category,
        "sessions": len(sample),
        "prices": len(prices),
        "distinct_prices": distinct,
        "prices_off_0_05": off_coarse,
        "prices_off_0_01": off_fine,
        "worst_block_off_0_05": f"{worst_block[0]}/{worst_block[1]}",
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
        category=category,
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
