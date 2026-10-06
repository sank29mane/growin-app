"""Back-adjustment factors from typed corporate actions, as-of adjusted series, and rawness checks.

Raw bars are never touched: adjusted prices are a separate derived layer (D-07, PDAT-04).
Factors come only from adjustable typed events and raw closes. Anything unresolved (rights,
demergers, mergers, unknown purposes, missing ex-dates, unusable dividend references and
unexplained price jumps) withholds adjusted prices before it and is quarantined with scope
`adjusted`. NSE does not adjust previous-close columns on ex-dates, so previous-close ratios
are never used to infer a factor.

D-20 (Phase 62): a lone amount-less "INTERIM DIVIDEND" event is applied as dividend amount unknown,
treated as zero. The price factor is 1 (a price-return series across it), the event and every bar
whose value depends on the assumption carry the `dividend_amount_unknown` tag, and the quarantine
is lifted only for that event. It is not lifted when any split, bonus, other unresolved action or
unexplained price jump sits on or within `unknown_dividend_conflict_days` of its ex-date, or when
the ex-date open gap exceeds `unknown_dividend_max_gap` either way (or cannot be measured). A factor
of 1 never explains a jump, so a hidden split is still flagged.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .core import (
    BREEZE_RAW_UNVERIFIED_CAVEAT,
    CaveatedResult,
    PilotDataError,
    canonical_sha256,
    dec_str,
    standard_caveats,
    utc_naive,
)
from .corporate_actions import ActionPart, CorporateActionEvent, events_for_symbol
from .models import Lineage, QuarantineRecord, RawDailyBar
from .store import PilotDataStore

STRUCTURAL_KINDS = ("split", "consolidation", "bonus")
DIVIDEND_AMOUNT_UNKNOWN = "dividend_amount_unknown"
SPECIFIC_REASONS = frozenset(
    {
        "dividend_reference_missing", "dividend_exceeds_price", "price_jump_without_action", "missing_ex_date",
        "dividend_amount_unknown_conflict",
    }
)

FACTOR_SETS_DDL = (
    "CREATE TABLE IF NOT EXISTS adjustment_factor_sets("
    "factor_set_sha256 VARCHAR PRIMARY KEY, workspace VARCHAR NOT NULL, anchor_isin VARCHAR NOT NULL, "
    "as_of DATE NOT NULL, policy_version VARCHAR NOT NULL, payload_json VARCHAR NOT NULL, "
    "recorded_at_utc TIMESTAMP NOT NULL, row_sha256 VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL)"
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AdjustmentPolicy(_Frozen):
    version: Literal["pilot-adjust/2"] = "pilot-adjust/2"
    # pilot-adjust/2 adds the D-20 rule for amount-less interim dividends (see the module docstring).
    unknown_dividend_policy: Literal["unknown_zero/1"] = "unknown_zero/1"
    unknown_dividend_conflict_days: int = Field(default=7, ge=0)  # calendar days either side of the ex-date
    # Largest ex-date open gap (open over previous close, as a fraction either way) still read as a
    # dividend. Tighter than the jump band on purpose: a hidden 1:2 bonus gaps -33 percent.
    unknown_dividend_max_gap: Decimal = Field(default=Decimal("0.20"), gt=0, lt=1)
    quantum: Decimal = Decimal("0.0001")
    rounding: Literal["ROUND_HALF_EVEN"] = "ROUND_HALF_EVEN"
    jump_low: Decimal = Decimal("0.55")
    jump_high: Decimal = Decimal("1.80")
    dividend_reference_max_sessions: int = Field(default=5, ge=1)


class AppliedFactor(_Frozen):
    event_id: str
    ex_date: date
    kind: str
    price_factor: Decimal
    volume_factor: Decimal
    structural_factor: Decimal  # split, consolidation and bonus parts only (rawness checks use this)
    tags: tuple[str, ...] = ()  # "dividend_amount_unknown" when the factor rests on the zero assumption


class UnresolvedAction(_Frozen):
    event_id: str | None
    ex_date: date | None
    kind: str
    reason: str
    detail: dict[str, str] = Field(default_factory=dict)
    evidence_sha256s: tuple[str, ...] = ()


class FactorSet(_Frozen):
    anchor_isin: str
    as_of: date
    policy_version: str
    applied: tuple[AppliedFactor, ...]
    unresolved: tuple[UnresolvedAction, ...]
    lineage_sha256: str
    reference_sha256: str
    factor_set_sha256: str
    unknown_dividend_params: dict[str, str] = Field(default_factory=dict)

    def unknown_dividend_events(self) -> tuple[AppliedFactor, ...]:
        """Applied events resting on the D-20 zero assumption, in ex-date order."""
        return tuple(item for item in self.applied if DIVIDEND_AMOUNT_UNKNOWN in item.tags)


class AdjustedBar(_Frozen):
    trade_date: date
    isin: str
    raw_open: Decimal
    raw_high: Decimal
    raw_low: Decimal
    raw_close: Decimal
    raw_volume: int
    adj_open: Decimal | None
    adj_high: Decimal | None
    adj_low: Decimal | None
    adj_close: Decimal | None
    adj_volume: int | None
    cumulative_price_factor: Decimal
    adjusted_quarantined: bool
    # D-20: the bar is on or before the ex-date of an amount-unknown dividend, so its adjusted value
    # (if not withheld) depends on the zero assumption. Set whether or not the value is withheld.
    dividend_amount_unknown: bool = False
    # D-20: this bar is the ex-date of an amount-unknown dividend; its open gap is not a signal.
    dividend_amount_unknown_ex_date: bool = False


class AdjustedSeries(CaveatedResult):
    anchor_isin: str
    as_of: date
    bars: tuple[AdjustedBar, ...]
    adjustment_basis: str
    factor_set_sha256: str


class RawnessVerdict(_Frozen):
    event_id: str
    ex_date: date
    kind: str
    factor: Decimal
    pre_date: date | None
    pre_ratio: Decimal | None
    post_date: date | None
    post_ratio: Decimal | None
    verdict: Literal["raw_confirmed", "adjusted_detected", "inconclusive", "no_overlap"]


class RawnessReport(CaveatedResult):
    anchor_isin: str
    source_label: str
    verdicts: tuple[RawnessVerdict, ...]
    overall: Literal["raw_confirmed", "adjusted_detected", "unverified"]


# --------------------------------------------------------------------------- events
def ensure_adjustment_tables(store: PilotDataStore) -> None:
    store.ensure_table("adjustment_factor_sets", FACTOR_SETS_DDL, key_columns=("factor_set_sha256",))


def events_for_lineage(store: PilotDataStore, lineage: Lineage) -> tuple[CorporateActionEvent, ...]:
    """Typed events per segment symbol and date range, plus symbol events with no ex-date."""
    found: dict[str, CorporateActionEvent] = {}
    for segment in lineage.segments:
        for event in events_for_symbol(
            store, segment.nse_symbol, ex_from=segment.valid_from, ex_to=segment.valid_to
        ):
            found.setdefault(event.event_id, event)
    ordered = sorted(found.values(), key=lambda e: (e.ex_date is None, e.ex_date or date.max, e.event_id))
    return tuple(ordered)


# --------------------------------------------------------------------------- factor maths
def _part_factors(part: ActionPart) -> tuple[Decimal, Decimal]:
    if part.kind in ("split", "consolidation"):
        assert part.old_fv is not None and part.new_fv is not None
        return part.new_fv / part.old_fv, part.old_fv / part.new_fv
    assert part.bonus_new is not None and part.bonus_held is not None
    total = part.bonus_new + part.bonus_held
    return part.bonus_held / total, total / part.bonus_held


def _weekdays_between(start: date, end: date) -> int:
    """Weekdays in (start, end]: a conservative stand-in for sessions, since no calendar is an input here."""
    count, day = 0, start + timedelta(days=1)
    while day <= end:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def _reference_close_before(
    bars: Sequence[RawDailyBar], ex_date: date, max_sessions: int
) -> RawDailyBar | None:
    candidates = [bar for bar in bars if bar.trade_date < ex_date]
    if not candidates:
        return None
    latest = max(candidates, key=lambda bar: bar.trade_date)
    return latest if _weekdays_between(latest.trade_date, ex_date) <= max_sessions else None


def detect_unrecorded_actions(
    reference_bars: Sequence[RawDailyBar], applied: Sequence[AppliedFactor], policy: AdjustmentPolicy
) -> tuple[UnresolvedAction, ...]:
    """Flag open/previous-close jumps that the applied price factors do not explain.

    A jump outside the policy band is suppressed only when the price factor applied on that exact
    date brings the ratio back inside the band. Events that apply no price factor (an AGM, an
    unresolved or non-price purpose) never suppress it, and a residual still outside the band
    after the factor is flagged, so earlier bars stay withheld.
    """
    ordered = sorted(reference_bars, key=lambda bar: bar.trade_date)
    factor_by_date: dict[date, Decimal] = {}
    for item in applied:
        factor_by_date[item.ex_date] = factor_by_date.get(item.ex_date, Decimal(1)) * item.price_factor
    quantum = Decimal("0.0001")
    flagged: list[UnresolvedAction] = []
    for previous, current in zip(ordered, ordered[1:]):
        low = current.open <= policy.jump_low * previous.close
        high = current.open >= policy.jump_high * previous.close
        if not (low or high):
            continue
        ratio = current.open / previous.close
        factor = factor_by_date.get(current.trade_date)
        detail = {
            "open_over_previous_close": str(ratio.quantize(quantum, rounding=ROUND_HALF_EVEN)),
            "previous_date": previous.trade_date.isoformat(),
        }
        if factor is not None and factor > 0:
            residual = ratio / factor
            if policy.jump_low < residual < policy.jump_high:
                continue
            detail["applied_price_factor"] = str(factor.quantize(quantum, rounding=ROUND_HALF_EVEN))
            detail["residual_after_factor"] = str(residual.quantize(quantum, rounding=ROUND_HALF_EVEN))
        flagged.append(
            UnresolvedAction(
                event_id=None, ex_date=current.trade_date, kind="suspected_unrecorded",
                reason="price_jump_without_action", detail=detail,
            )
        )
    return tuple(flagged)


def _first_blocking_kind(event: CorporateActionEvent) -> str:
    for part in event.parts:
        if part.kind not in ("split", "consolidation", "bonus", "dividend", "non_price"):
            return part.kind
    return "unknown"


def _ex_date_gap(bars: Sequence[RawDailyBar], by_day: dict[date, RawDailyBar], ex_date: date) -> Decimal | None:
    """Ex-date open over the previous accepted close, or None when either bar is missing."""
    current = by_day.get(ex_date)
    earlier = [bar for bar in bars if bar.trade_date < ex_date]
    if current is None or not earlier or max(earlier, key=lambda bar: bar.trade_date).close <= 0:
        return None
    return current.open / max(earlier, key=lambda bar: bar.trade_date).close


def _resolve_unknown_dividends(
    candidates: Sequence[CorporateActionEvent],
    bars: Sequence[RawDailyBar],
    applied: list[AppliedFactor],
    unresolved: list[UnresolvedAction],
    policy: AdjustmentPolicy,
) -> None:
    """D-20: apply each amount-less interim dividend as factor 1 unless something else is nearby.

    Mutates `applied` and `unresolved`. A conflict is any unresolved action with no ex-date or an
    ex-date within the policy window, any unexplained price jump in that window, or any applied
    event in that window that carries a structural factor (split, consolidation, bonus). The
    ex-date open gap must also sit within `unknown_dividend_max_gap` of the previous close, and a
    gap that cannot be measured (no ex-date bar or no earlier bar) fails closed. A
    conflicted event stays unresolved, so the bars before it keep being withheld.
    """
    window = timedelta(days=policy.unknown_dividend_conflict_days)
    max_gap = policy.unknown_dividend_max_gap
    by_day = {bar.trade_date: bar for bar in bars}
    # Jumps are found without the factor-1 events: a factor of 1 never explains one.
    jumps = detect_unrecorded_actions(bars, applied, policy)
    others = [*unresolved, *jumps]
    lifted: list[AppliedFactor] = []
    for event in candidates:
        assert event.ex_date is not None
        near = sorted(
            {
                f"{item.reason}:{item.ex_date.isoformat() if item.ex_date else 'no_ex_date'}"
                for item in others
                if item.ex_date is None or abs(item.ex_date - event.ex_date) <= window
            }
            | {
                f"structural_{item.kind}:{item.ex_date.isoformat()}"
                for item in applied
                if item.structural_factor != 1 and abs(item.ex_date - event.ex_date) <= window
            }
        )
        gap = _ex_date_gap(bars, by_day, event.ex_date)
        if gap is None:
            near.append("gap:unmeasurable")
        elif not (1 - max_gap <= gap <= 1 + max_gap):
            near.append(f"gap:{gap.quantize(Decimal('0.000000001'), rounding=ROUND_HALF_EVEN)}")
        if near:
            unresolved.append(
                UnresolvedAction(
                    event_id=event.event_id, ex_date=event.ex_date, kind=DIVIDEND_AMOUNT_UNKNOWN,
                    reason="dividend_amount_unknown_conflict", detail={"nearby": ",".join(near)},
                    evidence_sha256s=event.evidence_sha256s,
                )
            )
            continue
        lifted.append(
            AppliedFactor(
                event_id=event.event_id, ex_date=event.ex_date, kind=DIVIDEND_AMOUNT_UNKNOWN,
                price_factor=Decimal(1), volume_factor=Decimal(1), structural_factor=Decimal(1),
                tags=(DIVIDEND_AMOUNT_UNKNOWN,),
            )
        )
    applied.extend(lifted)
    applied.sort(key=lambda item: (item.ex_date, item.event_id))


def compute_factor_set(
    lineage: Lineage,
    reference_bars: Sequence[RawDailyBar],
    events: Sequence[CorporateActionEvent],
    *,
    as_of: date,
    policy: AdjustmentPolicy,
) -> FactorSet:
    bars = sorted((bar for bar in reference_bars if bar.trade_date <= as_of), key=lambda bar: bar.trade_date)
    known = [event for event in events if event.first_seen_file_date <= as_of]
    applied: list[AppliedFactor] = []
    unresolved: list[UnresolvedAction] = []
    unknown_dividends: list[CorporateActionEvent] = []
    for event in sorted(known, key=lambda e: (e.ex_date is None, e.ex_date or date.max, e.event_id)):
        is_unknown_dividend = any(part.kind == DIVIDEND_AMOUNT_UNKNOWN for part in event.parts)
        if is_unknown_dividend and event.adjustable:
            if event.ex_date is None:
                unresolved.append(
                    UnresolvedAction(
                        event_id=event.event_id, ex_date=None, kind=DIVIDEND_AMOUNT_UNKNOWN,
                        reason="missing_ex_date", evidence_sha256s=event.evidence_sha256s,
                    )
                )
            elif event.ex_date <= as_of and not (bars and event.ex_date < bars[0].trade_date):
                unknown_dividends.append(event)
            continue
        price_parts = [part for part in event.parts if part.kind in (*STRUCTURAL_KINDS, "dividend")]
        if event.ex_date is None:
            if not event.adjustable or price_parts:
                kind = _first_blocking_kind(event) if not event.adjustable else price_parts[0].kind
                unresolved.append(
                    UnresolvedAction(
                        event_id=event.event_id, ex_date=None, kind=kind, reason="missing_ex_date",
                        evidence_sha256s=event.evidence_sha256s,
                    )
                )
            continue
        if event.ex_date > as_of:
            continue
        if bars and event.ex_date < bars[0].trade_date:
            # Ex-date before the first bar in range: a backward adjustment only
            # changes bars before the ex-date, so it cannot affect this series.
            continue
        if not event.adjustable:
            unresolved.append(
                UnresolvedAction(
                    event_id=event.event_id, ex_date=event.ex_date, kind=_first_blocking_kind(event),
                    reason="not_adjustable", detail={"purpose": event.purpose_norm},
                    evidence_sha256s=event.evidence_sha256s,
                )
            )
            continue
        if not price_parts:
            continue  # only non-price parts: nothing to apply
        price, volume, structural = Decimal(1), Decimal(1), Decimal(1)
        failed: UnresolvedAction | None = None
        for part in price_parts:
            if part.kind == "dividend":
                reference = _reference_close_before(bars, event.ex_date, policy.dividend_reference_max_sessions)
                if reference is None:
                    failed = UnresolvedAction(
                        event_id=event.event_id, ex_date=event.ex_date, kind="dividend",
                        reason="dividend_reference_missing", evidence_sha256s=event.evidence_sha256s,
                    )
                    break
                amount = part.dividend_per_share or Decimal(0)
                if amount >= reference.close:
                    failed = UnresolvedAction(
                        event_id=event.event_id, ex_date=event.ex_date, kind="dividend",
                        reason="dividend_exceeds_price",
                        detail={"dividend": dec_str(amount), "reference_close": dec_str(reference.close)},
                        evidence_sha256s=event.evidence_sha256s,
                    )
                    break
                price *= Decimal(1) - amount / reference.close
            else:
                part_price, part_volume = _part_factors(part)
                price *= part_price
                structural *= part_price
                volume *= part_volume
        if failed is not None:
            unresolved.append(failed)
            continue
        applied.append(
            AppliedFactor(
                event_id=event.event_id, ex_date=event.ex_date, kind="+".join(p.kind for p in price_parts),
                price_factor=price, volume_factor=volume, structural_factor=structural,
            )
        )
    if unknown_dividends:
        _resolve_unknown_dividends(unknown_dividends, bars, applied, unresolved, policy)
    unresolved.extend(detect_unrecorded_actions(bars, applied, policy))
    unresolved.sort(key=lambda u: (u.ex_date is None, u.ex_date or date.max, u.event_id or "", u.reason))
    reference_sha = canonical_sha256(
        [[bar.trade_date.isoformat(), bar.isin, dec_str(bar.close), bar.source_sha256] for bar in bars]
    )
    unknown_params = {
        "policy": policy.unknown_dividend_policy, "conflict_days": str(policy.unknown_dividend_conflict_days),
        "max_gap": dec_str(policy.unknown_dividend_max_gap),
    }
    content = {
        "anchor_isin": lineage.anchor_isin,
        "as_of": as_of.isoformat(),
        "policy_version": policy.version,
        "applied": [item.model_dump(mode="json") for item in applied],
        "unresolved": [item.model_dump(mode="json") for item in unresolved],
        "lineage_sha256": lineage.content_sha256(),
        "reference_sha256": reference_sha,
        "unknown_dividend_params": unknown_params,
    }
    return FactorSet(
        anchor_isin=lineage.anchor_isin, as_of=as_of, policy_version=policy.version, applied=tuple(applied),
        unresolved=tuple(unresolved), lineage_sha256=content["lineage_sha256"], reference_sha256=reference_sha,
        unknown_dividend_params=unknown_params,
        factor_set_sha256=canonical_sha256(content),
    )


def adjusted_series(
    bars: Sequence[RawDailyBar], factor_set: FactorSet, *, as_of: date, workspace: Literal["india"]
) -> AdjustedSeries:
    policy = AdjustmentPolicy()
    if factor_set.policy_version != policy.version:
        raise PilotDataError("policy_version_mismatch", "factor set was built under a different adjustment policy")
    if factor_set.as_of != as_of:
        raise PilotDataError("factor_set_as_of_mismatch", "adjusted series must use a factor set built for the same as_of")
    ordered = sorted((bar for bar in bars if bar.trade_date <= as_of), key=lambda bar: bar.trade_date)
    unresolved_dates = [u.ex_date for u in factor_set.unresolved if u.ex_date is not None]
    quarantine_all = any(u.ex_date is None for u in factor_set.unresolved)
    unknown_ex_dates = {item.ex_date for item in factor_set.unknown_dividend_events() if item.ex_date <= as_of}
    last_unknown = max(unknown_ex_dates, default=None)
    out: list[AdjustedBar] = []
    for bar in ordered:
        later = [item for item in factor_set.applied if item.ex_date > bar.trade_date and item.ex_date <= as_of]
        price_factor, volume_factor = Decimal(1), Decimal(1)
        for item in later:
            price_factor *= item.price_factor
            volume_factor *= item.volume_factor
        withheld = quarantine_all or any(bar.trade_date < day for day in unresolved_dates)
        if withheld:
            adj = (None, None, None, None, None)
        else:
            q = policy.quantum
            adj = (
                (bar.open * price_factor).quantize(q, rounding=ROUND_HALF_EVEN),
                (bar.high * price_factor).quantize(q, rounding=ROUND_HALF_EVEN),
                (bar.low * price_factor).quantize(q, rounding=ROUND_HALF_EVEN),
                (bar.close * price_factor).quantize(q, rounding=ROUND_HALF_EVEN),
                int((Decimal(bar.volume) * volume_factor).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)),
            )
        out.append(
            AdjustedBar(
                trade_date=bar.trade_date, isin=bar.isin, raw_open=bar.open, raw_high=bar.high, raw_low=bar.low,
                raw_close=bar.close, raw_volume=bar.volume, adj_open=adj[0], adj_high=adj[1], adj_low=adj[2],
                adj_close=adj[3], adj_volume=adj[4], cumulative_price_factor=price_factor,
                adjusted_quarantined=withheld,
                dividend_amount_unknown=(last_unknown is not None and bar.trade_date <= last_unknown),
                dividend_amount_unknown_ex_date=bar.trade_date in unknown_ex_dates,
            )
        )
    basis = (
        f"back_adjusted:as_of={as_of.isoformat()}:factor_set={factor_set.factor_set_sha256}:"
        f"policy={factor_set.policy_version}"
    )
    return AdjustedSeries(
        workspace=workspace, caveats=standard_caveats(), anchor_isin=factor_set.anchor_isin, as_of=as_of,
        bars=tuple(out), adjustment_basis=basis, factor_set_sha256=factor_set.factor_set_sha256,
    )


# --------------------------------------------------------------------------- rawness
def verify_source_unadjusted(
    reference_bars: Sequence[RawDailyBar],
    candidate_bars: Sequence[RawDailyBar],
    factor_set: FactorSet,
    *,
    tolerance: Decimal = Decimal("0.005"),
    source_label: str,
    workspace: Literal["india"],
) -> RawnessReport:
    """Classify a candidate source (Breeze) around each split, consolidation and bonus."""
    reference = {bar.trade_date: bar for bar in reference_bars}
    candidate = {bar.trade_date: bar for bar in candidate_bars}
    shared = sorted(set(reference) & set(candidate))
    verdicts: list[RawnessVerdict] = []
    for item in factor_set.applied:
        if item.structural_factor == 1:
            continue  # dividends alone are excluded: their effect falls inside the tolerance
        factor = item.structural_factor
        pre_date = max((d for d in shared if d < item.ex_date), default=None)
        post_date = min((d for d in shared if d >= item.ex_date), default=None)
        pre_ratio = post_ratio = None
        if pre_date is not None and post_date is not None:
            pre_ratio = candidate[pre_date].close / reference[pre_date].close
            post_ratio = candidate[post_date].close / reference[post_date].close
            if abs(pre_ratio - 1) <= tolerance and abs(post_ratio - 1) <= tolerance:
                verdict = "raw_confirmed"
            elif abs(pre_ratio - factor) <= tolerance * factor and abs(post_ratio - 1) <= tolerance:
                verdict = "adjusted_detected"
            else:
                verdict = "inconclusive"
        else:
            verdict = "no_overlap"
        verdicts.append(
            RawnessVerdict(
                event_id=item.event_id, ex_date=item.ex_date, kind=item.kind, factor=factor, pre_date=pre_date,
                pre_ratio=pre_ratio, post_date=post_date, post_ratio=post_ratio, verdict=verdict,
            )
        )
    kinds = {v.verdict for v in verdicts}
    if "adjusted_detected" in kinds:
        overall: Literal["raw_confirmed", "adjusted_detected", "unverified"] = "adjusted_detected"
    elif "raw_confirmed" in kinds:
        overall = "raw_confirmed"
    else:
        overall = "unverified"
    extra = () if overall == "raw_confirmed" else (BREEZE_RAW_UNVERIFIED_CAVEAT,)
    return RawnessReport(
        workspace=workspace, caveats=standard_caveats(*extra), anchor_isin=factor_set.anchor_isin,
        source_label=source_label, verdicts=tuple(verdicts), overall=overall,
    )


# --------------------------------------------------------------------------- persistence
def persist_factor_set(store: PilotDataStore, factor_set: FactorSet, *, workspace: Literal["india"]) -> None:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    ensure_adjustment_tables(store)
    store.append_rows(
        "adjustment_factor_sets",
        [
            {
                "factor_set_sha256": factor_set.factor_set_sha256, "workspace": store.workspace,
                "anchor_isin": factor_set.anchor_isin, "as_of": factor_set.as_of,
                "policy_version": factor_set.policy_version,
                "payload_json": json.dumps(factor_set.model_dump(mode="json"), sort_keys=True),
                "recorded_at_utc": utc_naive(datetime.now(timezone.utc)),
                "row_sha256": factor_set.factor_set_sha256, "source_sha256": factor_set.reference_sha256,
            }
        ],
        check="adjustment_factor_set",
    )


def quarantine_unresolved(
    store: PilotDataStore, lineage: Lineage, factor_set: FactorSet, *, workspace: Literal["india"]
) -> tuple[QuarantineRecord, ...]:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    records: list[QuarantineRecord] = []
    symbol = lineage.segments[-1].nse_symbol
    for item in factor_set.unresolved:
        end = factor_set.as_of if item.ex_date is None else item.ex_date - timedelta(days=1)
        if end < lineage.resolved_from:
            continue  # nothing before the ex-date to withhold
        reason = item.reason if item.reason in SPECIFIC_REASONS else item.kind
        detail: dict[str, str] = {"kind": item.kind, "reason": item.reason, **item.detail}
        if item.event_id is not None:
            detail["event_id"] = item.event_id
        records.append(
            QuarantineRecord(
                workspace="india", check="corporate_action", reason_code=f"ca_unresolved_{reason}",
                scope="adjusted", isin=lineage.anchor_isin, nse_symbol=symbol, stock_code=lineage.stock_code,
                series=lineage.anchor_series, date_from=lineage.resolved_from, date_to=end, detail=detail,
                evidence_sha256s=(*item.evidence_sha256s, factor_set.factor_set_sha256),
            )
        )
    store.record_quarantine(records)
    return tuple(records)


__all__ = [
    "AdjustmentPolicy", "AppliedFactor", "UnresolvedAction", "FactorSet", "AdjustedBar", "AdjustedSeries",
    "RawnessVerdict", "RawnessReport", "events_for_lineage", "compute_factor_set", "adjusted_series",
    "detect_unrecorded_actions", "verify_source_unadjusted", "persist_factor_set", "quarantine_unresolved",
]
