"""59 dataset access for the engine (D-03, D-05, D-07, D-12, D-20).

* ``DatasetView`` is the research loader. Built with a registered holdout and no
  grant, it refuses every request for a session on or after the holdout start:
  rows, windows, feature warm-up and benchmark paths all go through it. Only a
  ``HoldoutGrant`` from ``holdout.open_holdout`` makes the holdout readable.
* ``DecisionView`` hands a signal provider the data known at one decision date
  and raises ``LookaheadError`` for anything later.
* Signals read ``adj_*`` and a row with ``adjusted_quarantined`` gives none.
  Fills read ``raw_*`` as ``SessionBar(price_basis="raw")``.
* ``UniverseEligibility`` calls 59 ``evaluate_universe`` with ``as_of`` equal to
  the decision date, so unknown eligibility excludes the name.
"""

from __future__ import annotations

import bisect
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Protocol

from costs.core import LookaheadError
from costs.fills import BandUnavailable, PriceBand, SessionBar, TickSize
from pilot_data import surveillance as _surveillance
from pilot_data import universe as _universe
from pilot_data.price_bands import BandObservation

from .errors import DataError, HoldoutViolation
from .holdout import HoldoutGrant, HoldoutRange, is_grant

EXCHANGE = "NSE"


@dataclass(frozen=True)
class Bar:
    """One session of one name. Raw prices feed fills and marks; adjusted prices feed signals."""

    anchor_isin: str
    isin: str
    stock_code: str
    session: date
    series: str
    raw_open: Decimal
    raw_high: Decimal
    raw_low: Decimal
    raw_close: Decimal
    raw_volume: int
    adj_open: Decimal | None
    adj_high: Decimal | None
    adj_low: Decimal | None
    adj_close: Decimal | None
    quarantined: bool

    @property
    def traded_value(self) -> Decimal:
        return self.raw_close * self.raw_volume


def bar_from_row(row: Any) -> Bar:
    """Map a 59 ``DatasetRow`` (or any object with its attributes) to a ``Bar``."""
    quarantined = bool(row.adjusted_quarantined)
    adj = (row.adj_open, row.adj_high, row.adj_low, row.adj_close)
    if quarantined:
        adj = (None, None, None, None)
    elif any(value is None for value in adj):
        raise DataError("a non-quarantined row has a missing adjusted price")
    return Bar(
        anchor_isin=row.anchor_isin, isin=row.isin, stock_code=row.stock_code, session=row.trade_date,
        series=row.series,
        raw_open=row.raw_open, raw_high=row.raw_high, raw_low=row.raw_low, raw_close=row.raw_close,
        raw_volume=row.raw_volume, adj_open=adj[0], adj_high=adj[1], adj_low=adj[2], adj_close=adj[3],
        quarantined=quarantined,
    )


@dataclass(frozen=True)
class DividendUnknownEvent:
    """A 59 event tagged ``dividend_amount_unknown`` (D-20). ``ex_date`` is the first ex-dividend session."""

    anchor_isin: str
    event_id: str
    ex_date: date


class DividendEvents:
    def __init__(self, events: Iterable[DividendUnknownEvent] = ()) -> None:
        by_anchor: dict[str, set[date]] = {}
        listed: dict[tuple[str, str], DividendUnknownEvent] = {}
        for event in events:
            if not event.event_id:
                raise DataError("a dividend_amount_unknown event needs an event_id")
            key = (event.anchor_isin, event.event_id)
            if key in listed and listed[key] != event:
                raise DataError("two different dividend events share an anchor and event_id")
            listed[key] = event
            by_anchor.setdefault(event.anchor_isin, set()).add(event.ex_date)
        self._by_anchor = {key: frozenset(value) for key, value in by_anchor.items()}
        self._events = tuple(sorted(listed.values(), key=lambda e: (e.anchor_isin, e.event_id, e.ex_date)))

    def sealed_sha256(self) -> str:
        """Hash of the canonical, sorted (anchor_isin, event_id, ex_date) list. An empty list has a hash too."""
        from .registry import canonical_sha256

        return canonical_sha256(
            [[e.anchor_isin, e.event_id, e.ex_date.isoformat()] for e in self._events]
        )

    def ex_dates(self, anchor_isin: str) -> frozenset[date]:
        return self._by_anchor.get(anchor_isin, frozenset())

    def all(self) -> tuple[DividendUnknownEvent, ...]:
        return self._events

    def __bool__(self) -> bool:
        return bool(self._events)


class DatasetView:
    """Guarded read access to dataset bars. Build one with ``from_rows``."""

    def __init__(self, by_anchor: Mapping[str, Sequence[Bar]], holdout: HoldoutRange, visible_end: date, opened: bool,
                 hidden: Mapping[str, Sequence[Bar]] | None) -> None:
        self._holdout = holdout
        self._opened = opened
        self.visible_end = visible_end
        self._by_anchor: dict[str, tuple[Bar, ...]] = {key: tuple(value) for key, value in by_anchor.items()}
        self._dates: dict[str, list[date]] = {key: [bar.session for bar in value] for key, value in self._by_anchor.items()}
        self._hidden = dict(hidden) if hidden is not None else None
        calendar = sorted({bar.session for bars in self._by_anchor.values() for bar in bars})
        self._calendar = calendar
        self._cal_index = {day: i for i, day in enumerate(calendar)}

    @classmethod
    def from_rows(cls, rows: Iterable[Any], *, holdout: HoldoutRange) -> "DatasetView":
        """Research view: rows inside the holdout are retained privately and never returned."""
        bars = [row if isinstance(row, Bar) else bar_from_row(row) for row in rows]
        if not bars:
            raise DataError("the dataset has no rows")
        grouped: dict[str, list[Bar]] = {}
        for bar in sorted(bars, key=lambda b: (b.anchor_isin, b.session)):
            grouped.setdefault(bar.anchor_isin, []).append(bar)
        for anchor, items in grouped.items():
            days = [bar.session for bar in items]
            if len(set(days)) != len(days):
                raise DataError(f"duplicate session rows for {anchor}")
        visible = {key: [b for b in items if b.session < holdout.start] for key, items in grouped.items()}
        visible = {key: value for key, value in visible.items() if value}
        before = [b.session for items in visible.values() for b in items]
        if not before:
            raise DataError("no development sessions precede the holdout")
        return cls(visible, holdout, max(before), False, grouped)

    # ---- guards ---------------------------------------------------------------
    @property
    def holdout(self) -> HoldoutRange:
        return self._holdout

    @property
    def opened(self) -> bool:
        return self._opened

    def guard(self, day: date) -> None:
        if not self._opened and day >= self._holdout.start:
            raise HoldoutViolation(f"session {day.isoformat()} is inside or after the sealed holdout")

    def open(self, grant: HoldoutGrant) -> "DatasetView":
        """A view with the holdout readable. Needs a grant from ``open_holdout``."""
        if not is_grant(grant) or grant.holdout != self._holdout:
            raise HoldoutViolation("a valid holdout grant for this holdout range is required")
        if self._hidden is None:
            raise HoldoutViolation("this view holds no holdout rows")
        end = max(b.session for items in self._hidden.values() for b in items)
        return DatasetView(self._hidden, self._holdout, end, True, None)

    # ---- reads ----------------------------------------------------------------
    def anchors(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_anchor))

    def sessions(self, start: date | None = None, end: date | None = None) -> list[date]:
        if end is not None:
            self.guard(end)
        if start is not None:
            self.guard(start)
        lo = bisect.bisect_left(self._calendar, start) if start is not None else 0
        hi = bisect.bisect_right(self._calendar, end) if end is not None else len(self._calendar)
        return self._calendar[lo:hi]

    def calendar_index(self, day: date) -> int:
        self.guard(day)
        try:
            return self._cal_index[day]
        except KeyError:
            raise DataError(f"{day.isoformat()} is not a dataset session") from None

    def next_session(self, day: date) -> date | None:
        """The next dataset session after ``day`` or None. The holdout guard applies to the answer."""
        i = bisect.bisect_right(self._calendar, day)
        if i >= len(self._calendar):
            return None
        return self._calendar[i]

    def series(self, anchor: str, *, end: date | None = None) -> tuple[Bar, ...]:
        if end is not None:
            self.guard(end)
        bars = self._by_anchor.get(anchor, ())
        if end is None:
            return bars
        return bars[: bisect.bisect_right(self._dates[anchor], end)]

    def bar(self, anchor: str, day: date) -> Bar | None:
        self.guard(day)
        days = self._dates.get(anchor)
        if not days:
            return None
        i = bisect.bisect_left(days, day)
        if i < len(days) and days[i] == day:
            return self._by_anchor[anchor][i]
        return None

    def bars_on(self, day: date) -> dict[str, Bar]:
        self.guard(day)
        out: dict[str, Bar] = {}
        for anchor, days in self._dates.items():
            i = bisect.bisect_left(days, day)
            if i < len(days) and days[i] == day:
                out[anchor] = self._by_anchor[anchor][i]
        return out

    def previous_bar(self, anchor: str, day: date) -> Bar | None:
        """The bar before ``day`` in this name's own series."""
        self.guard(day)
        days = self._dates.get(anchor)
        if not days:
            return None
        i = bisect.bisect_left(days, day)
        return self._by_anchor[anchor][i - 1] if i > 0 else None

    def median_traded_value(self, anchor: str, end: date, window: int) -> Decimal | None:
        """Median raw traded value over the last ``window`` rows up to and including ``end``."""
        bars = self.series(anchor, end=end)[-window:]
        if len(bars) < window:
            return None
        values = sorted(bar.traded_value for bar in bars)
        mid = len(values) // 2
        return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


class DecisionView:
    """What a signal provider may read at one decision date. Anything later raises ``LookaheadError``."""

    def __init__(self, view: DatasetView, as_of: date) -> None:
        self._view = view
        self.as_of = as_of

    def _check(self, day: date) -> None:
        if day > self.as_of:
            raise LookaheadError(f"{day.isoformat()} is after the decision date {self.as_of.isoformat()}")

    def close(self, anchor: str, day: date, *, basis: str = "adj") -> Decimal | None:
        self._check(day)
        bar = self._view.bar(anchor, day)
        if bar is None:
            return None
        value = bar.adj_close if basis == "adj" else bar.raw_close
        return value

    def history(self, anchor: str, *, end: date, count: int, basis: str = "adj") -> tuple[Decimal | None, ...]:
        self._check(end)
        bars = self._view.series(anchor, end=end)[-count:]
        return tuple(bar.adj_close if basis == "adj" else bar.raw_close for bar in bars)

    def anchors(self) -> tuple[str, ...]:
        return self._view.anchors()


# ---- band mapping (D-05, D-01a) ------------------------------------------------
def _round_to_tick(value: Decimal, tick: Decimal, rounding: str) -> Decimal:
    return (value / tick).to_integral_value(rounding=rounding) * tick


def _band_source_hash(observation: BandObservation) -> str:
    joined = "|".join(sorted(observation.source_sha256s)) or "none"
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def band_for_session(
    bar: Bar,
    *,
    previous_raw_close: Decimal | None,
    observation: BandObservation | None,
    unavailable_reason: str | None,
    tick: TickSize,
) -> PriceBand | BandUnavailable:
    """Map a 59 band observation to 60's band type. Anything unknown becomes ``BandUnavailable``.

    ``fixed`` becomes absolute limits from the previous raw close, widened to the tick grid so a real bar
    can never fall outside its own band. ``no_band`` stays ``no_band``. Every unknown status, an
    ``UnavailableBand`` row from the coverage report, a missing observation and a missing previous
    close all become ``BandUnavailable`` (NO_ASSUMED_FILL downstream).
    """
    if unavailable_reason is not None:
        return BandUnavailable(unavailable_reason)
    if observation is None:
        return BandUnavailable("band_observation_missing")
    if observation.status == "no_band":
        return PriceBand("no_band", None, None, bar.session, "nse-band:" + str(observation.source_kind),
                         _band_source_hash(observation))
    if observation.status != "fixed":
        return BandUnavailable(observation.reason or "band_unknown")
    if observation.percent is None or previous_raw_close is None or previous_raw_close <= 0:
        return BandUnavailable("band_percent_or_previous_close_missing")
    fraction = observation.percent / Decimal(100)
    lower = _round_to_tick(previous_raw_close * (1 - fraction), tick.value, ROUND_FLOOR)
    upper = _round_to_tick(previous_raw_close * (1 + fraction), tick.value, ROUND_CEILING)
    if lower <= 0:
        return BandUnavailable("band_lower_not_positive")
    return PriceBand("fixed", lower, upper, bar.session, "nse-band:" + str(observation.source_kind),
                     _band_source_hash(observation))


def session_bar_for(
    bar: Bar,
    *,
    previous_raw_close: Decimal | None,
    observation: BandObservation | None,
    unavailable_reason: str | None,
    tick: TickSize,
) -> SessionBar:
    """Fills use raw prices: ``SessionBar(price_basis="raw")``."""
    return SessionBar(
        isin=bar.isin, exchange=EXCHANGE, session_date=bar.session,
        open=bar.raw_open, high=bar.raw_high, low=bar.raw_low, close=bar.raw_close, volume=bar.raw_volume,
        price_basis="raw",
        price_band=band_for_session(
            bar, previous_raw_close=previous_raw_close, observation=observation,
            unavailable_reason=unavailable_reason, tick=tick,
        ),
        source="pilot-dataset/1:raw",
    )


class BandSource(Protocol):
    def observe(self, isin: str, session: date) -> BandObservation | None: ...


# ---- eligibility (D-07) ----------------------------------------------------------
@dataclass(frozen=True)
class EligibilitySnapshot:
    as_of: date
    eligible: frozenset[str]  # anchor ISINs
    smallcap: Mapping[str, str]  # anchor ISIN -> small | not_small | unclassified
    result_sha256: str


class EligibilitySource(Protocol):
    def snapshot(self, as_of: date) -> EligibilitySnapshot: ...


class UniverseEligibility:
    """Calls 59 ``evaluate_universe`` once per rebalance with ``as_of`` equal to the decision date."""

    def __init__(self, store: Any, targets: Any, policy: Any, *, mode: str = "research",
                 allow_missing_surveillance_before: date | None = None, status_source: Any = None) -> None:
        self._store = store
        self._targets = targets
        self.policy = policy
        self._mode = mode
        self._allow = allow_missing_surveillance_before
        self._status = status_source if status_source is not None else _universe.NoTradingStatusSource()

    def inputs_available(self, day: date) -> str | None:
        """Why ``snapshot(day)`` would fail for a missing input, or None. Looks for stored snapshots only, not prices."""
        needs_surveillance = not (self._mode == "research" and self._allow is not None and day < self._allow)
        if needs_surveillance:
            for kind in ("asm", "gsm"):
                if _surveillance.snapshot_for(self._store, kind, day) is None:
                    # D-12: the reason reaches the operator in a holdout refusal, so it carries no date
                    return f"no {kind.upper()} surveillance snapshot is effective on a decision date"
        return None

    def snapshot(self, as_of: date) -> EligibilitySnapshot:
        result = _universe.evaluate_universe(
            self._store, as_of=as_of, targets=self._targets, policy=self.policy, mode=self._mode,
            allow_missing_surveillance_before=self._allow, workspace="india", status_source=self._status,
        )
        if result.as_of != as_of:
            raise LookaheadError(f"eligibility came back for {result.as_of.isoformat()}, not {as_of.isoformat()}")
        return EligibilitySnapshot(
            as_of=result.as_of,
            eligible=frozenset(result.eligible_isins),
            smallcap={decision.anchor_isin: decision.smallcap_class for decision in result.decisions},
            result_sha256=result.result_sha256,
        )


def check_events_against_rows(events: DividendEvents, rows: Iterable[Any]) -> None:
    """Require the exact Phase 59 D-20 tags on every accepted row.

    The amount-unknown tag covers dates through the last listed ex-date, including
    quarantined rows. The ex-date flag is exact membership. Events without an
    accepted bar, or with only later history, do not require a tagged row.
    """
    dates = {anchor: events.ex_dates(anchor) for anchor in {e.anchor_isin for e in events.all()}}
    last = {anchor: max(found) for anchor, found in dates.items()}
    for row in rows:
        anchor, day = row.anchor_isin, getattr(row, "trade_date", None) or row.session
        want_tag = anchor in last and day <= last[anchor]
        if getattr(row, "dividend_amount_unknown", False) != want_tag:
            raise DataError(
                f"row {anchor} {day.isoformat()} dividend_amount_unknown must be {want_tag}",
                code="events_mismatch",
            )
        want_ex = day in dates.get(anchor, ())
        if getattr(row, "dividend_amount_unknown_ex_date", False) != want_ex:
            reason = "has a bar on its ex-date that is not flagged" if want_ex else "is flagged as an ex-date that no event lists"
            raise DataError(f"row {anchor} {day.isoformat()} {reason}", code="events_mismatch")


def dataset_digest(rows: Iterable[Any], events: DividendEvents) -> str:
    """Reproduce Phase 59's event-bound dataset hash without a dependency on its newer API.

    PR #542 adds the manifest events to the hash only when non-empty. This adapter
    uses that canonical form so preflight can verify those exports after #542 merges,
    while the row-only digest of datasets without events stays unchanged.
    """
    from pilot_data.core import canonical_sha256

    payloads = [row.payload() for row in sorted(rows, key=lambda r: (r.anchor_isin, r.trade_date))]
    if not events:
        return canonical_sha256(payloads)
    listed: dict[str, list[list[str]]] = {}
    for event in events.all():
        listed.setdefault(event.anchor_isin, []).append([event.ex_date.isoformat(), event.event_id])
    return canonical_sha256({"rows": payloads, "dividend_amount_unknown_events":
                             {anchor: sorted(found) for anchor, found in sorted(listed.items())}})


def events_from_manifest(manifest: Any) -> DividendEvents:
    """D-20 events come from ``manifest.dividend_amount_unknown_events`` (anchor to event id and ex-date), never
    from the per-bar ex-date flag: a row carries that flag only when an accepted bar falls on the ex-date.
    59 ``verify_dataset`` has already cross-checked the list against the row tags.
    """
    return DividendEvents(
        DividendUnknownEvent(anchor, event.event_id, event.ex_date)
        for anchor, events in sorted(getattr(manifest, "dividend_amount_unknown_events", {}).items())
        for event in events
    )


def load_dataset_rows(path: Path, *, expected_dataset_sha256: str | None = None) -> tuple[Any, list[Any]]:
    """Verify a published 59 dataset directory and read its rows (read-only)."""
    from pilot_data.dataset import read_dataset_rows, verify_dataset

    manifest = verify_dataset(Path(path), workspace="india")
    if expected_dataset_sha256 is not None and manifest.dataset_sha256 != expected_dataset_sha256:
        raise DataError("dataset_sha256 differs from the expected value")
    return manifest, read_dataset_rows(Path(path))
