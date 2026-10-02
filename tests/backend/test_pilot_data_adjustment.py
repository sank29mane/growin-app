"""Adjustment factors, as-of adjusted series, jump detection and rawness verification."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from pilot_data.adjustment import (
    AdjustmentPolicy,
    adjusted_series,
    compute_factor_set,
    detect_unrecorded_actions,
    persist_factor_set,
    quarantine_unresolved,
    verify_source_unadjusted,
)
from pilot_data.core import PilotDataError
from pilot_data.corporate_actions import CorporateActionEvent, event_id_for, is_adjustable, normalise_purpose, parse_purpose
from pilot_data.models import IsinSegment, Lineage, RawDailyBar
from pilot_data.store import PilotDataStore

POLICY = AdjustmentPolicy()
NEW, OLD = "INE476A01022", "INE476A01014"
HASH = "a" * 64
FAR_PAST = date(2000, 1, 1)


def bar(day, close, *, open_=None, high=None, low=None, volume=1000, isin=NEW):
    close = Decimal(close)
    open_ = Decimal(open_) if open_ is not None else close
    top = Decimal(high) if high is not None else max(open_, close)
    bottom = Decimal(low) if low is not None else min(open_, close)
    return RawDailyBar(trade_date=day, isin=isin, series="EQ", nse_symbol="CANBK", open=open_, high=top, low=bottom,
                       close=close, volume=volume, traded_value=None, source_kind="bhavcopy", source_sha256=HASH)


def event(symbol, purpose, ex_date, *, first_seen=FAR_PAST):
    norm = normalise_purpose(purpose)
    parts = parse_purpose(purpose)
    return CorporateActionEvent(
        event_id=event_id_for(symbol, ex_date, norm), nse_symbol=symbol, series_seen=("EQ",), ex_date=ex_date,
        record_date=None, purpose_norm=norm, parts=parts, adjustable=is_adjustable(parts),
        first_seen_file_date=first_seen, last_seen_file_date=first_seen, sightings=1, evidence_sha256s=("e" * 64,),
    )


def lineage(start=date(2024, 1, 1), end=date(2025, 12, 31), isin=NEW, stock_code="CANBAN"):
    return Lineage(
        workspace="india", anchor_isin=isin, anchor_series="EQ", stock_code=stock_code,
        segments=(IsinSegment(isin=isin, nse_symbol="CANBK", valid_from=start, valid_to=end, link="anchor"),),
        resolved_from=start, built_as_of=end, basis="bhavcopy_walk",
    )


D14, D15 = date(2024, 5, 14), date(2024, 5, 15)
CANBK_BARS = [
    bar(D14, "566.55", open_="555.4", high="569", low="553.55", volume=9219466),
    bar(D15, "119", open_="116.25", high="119.6", low="116", volume=58148316),
]
CANBK_EVENT = event("CANBK", "FVSPLT FRM RS 10 TO RS 2", D15)


def factor_set(bars, events, as_of, lin=None):
    return compute_factor_set(lin or lineage(), bars, events, as_of=as_of, policy=POLICY)


# ------------------------------------------------------------------ verified real factors
def test_canbk_split_factor_and_adjusted_series():
    fs = factor_set(CANBK_BARS, [CANBK_EVENT], D15)
    (applied,) = fs.applied
    assert (applied.price_factor, applied.volume_factor, applied.kind) == (Decimal("0.2"), Decimal("5"), "split")
    assert fs.unresolved == ()  # the FVSPLT event on the ex-date explains the open/previous-close jump
    series = adjusted_series(CANBK_BARS, fs, as_of=D15, workspace="india")
    first, second = series.bars
    assert str(first.adj_close) == "113.3100" and first.adj_volume == 9219466 * 5
    assert first.cumulative_price_factor == Decimal("0.2") and not first.adjusted_quarantined
    assert second.adj_close == Decimal("119.0000") and second.adj_volume == 58148316
    assert first.raw_close == Decimal("566.55")  # raw layer untouched


def test_garfibres_bonus_four_to_one():
    bars = [bar(date(2025, 1, 2), "4650.95"), bar(date(2025, 1, 3), "960", open_="958.50", high="970", low="950")]
    fs = factor_set(bars, [event("GARFIBRES", "BONUS 4:1", date(2025, 1, 3))], date(2025, 1, 3))
    assert fs.applied[0].price_factor == Decimal("0.2") and fs.applied[0].volume_factor == Decimal("5")


def test_hcltech_dividend_uses_the_prior_raw_close():
    bars = [bar(date(2024, 5, 6), "1358.80"), bar(date(2024, 5, 7), "1340")]
    fs = factor_set(bars, [event("HCLTECH", "INTDIV - RS 18 PER SH", date(2024, 5, 7))], date(2024, 5, 7))
    assert fs.applied[0].price_factor == Decimal(1) - Decimal(18) / Decimal("1358.80")
    assert fs.applied[0].volume_factor == Decimal(1) and fs.applied[0].kind == "dividend"
    assert fs.applied[0].structural_factor == Decimal(1)


def test_parts_multiply_and_events_compound():
    bars = [bar(date(2024, 1, 10), "400"), bar(date(2024, 2, 12), "210"), bar(date(2024, 3, 12), "100")]
    both = event("CANBK", "FVSPLT FRM RS 10 TO RS 5 AND BONUS 1:1", date(2024, 2, 12))
    later = event("CANBK", "BONUS 1:1", date(2024, 3, 12))
    fs = factor_set(bars, [both, later], date(2024, 3, 12))
    assert [(f.price_factor, f.volume_factor, f.kind) for f in fs.applied] == [
        (Decimal("0.25"), Decimal("4"), "split+bonus"), (Decimal("0.5"), Decimal("2"), "bonus")]
    series = adjusted_series(bars, fs, as_of=date(2024, 3, 12), workspace="india")
    assert [b.cumulative_price_factor for b in series.bars] == [Decimal("0.125"), Decimal("0.5"), Decimal("1")]


# ------------------------------------------------------------------ no look-ahead
def test_as_of_before_the_ex_date_applies_nothing():
    fs = factor_set(CANBK_BARS, [CANBK_EVENT], D14)
    assert fs.applied == () and fs.unresolved == ()
    series = adjusted_series(CANBK_BARS, fs, as_of=D14, workspace="india")
    assert [b.adj_close for b in series.bars] == [Decimal("566.5500")]  # equals raw


def test_an_event_first_seen_after_as_of_is_ignored_even_if_its_ex_date_is_earlier():
    late = event("CANBK", "FVSPLT FRM RS 10 TO RS 2", D14, first_seen=D15)
    fs = factor_set(CANBK_BARS[:1], [late], D14)
    assert fs.applied == ()
    seen = event("CANBK", "FVSPLT FRM RS 10 TO RS 2", D14, first_seen=D14)
    assert len(factor_set(CANBK_BARS[:1], [seen], D14).applied) == 1


def test_series_requires_a_factor_set_for_the_same_as_of():
    fs = factor_set(CANBK_BARS, [CANBK_EVENT], D15)
    with pytest.raises(PilotDataError) as caught:
        adjusted_series(CANBK_BARS, fs, as_of=D14, workspace="india")
    assert caught.value.code == "factor_set_as_of_mismatch"


# ------------------------------------------------------------------ unresolved actions
@pytest.mark.parametrize("purpose,kind", [("RIGHTS 1:1 @ PRM RS 3/-", "rights"), ("DEMERGER", "demerger"),
                                          ("MERGER", "merger"), ("SOMETHING ELSE", "unknown")])
def test_non_adjustable_events_withhold_adjusted_prices_before_them(purpose, kind):
    bars = [bar(date(2024, 5, 13), "100"), bar(D14, "100"), bar(D15, "100")]
    fs = factor_set(bars, [event("CANBK", purpose, D15)], D15)
    assert fs.applied == () and [(u.kind, u.reason, u.ex_date) for u in fs.unresolved] == [
        (kind, "not_adjustable", D15)]
    series = adjusted_series(bars, fs, as_of=D15, workspace="india")
    flags = [(b.adjusted_quarantined, b.adj_close, b.raw_close) for b in series.bars]
    assert flags == [(True, None, Decimal("100")), (True, None, Decimal("100")), (False, Decimal("100.0000"), Decimal("100"))]


def test_price_affecting_event_without_an_ex_date_quarantines_every_bar():
    fs = factor_set(CANBK_BARS, [event("CANBK", "DEMERGER", None), event("CANBK", "ANNUAL GENERAL MEETING", None)], D15)
    assert [(u.kind, u.reason) for u in fs.unresolved if u.reason == "missing_ex_date"] == [("demerger", "missing_ex_date")]
    series = adjusted_series(CANBK_BARS, fs, as_of=D15, workspace="india")
    assert all(b.adjusted_quarantined and b.adj_close is None for b in series.bars)


def test_dividend_reference_rules():
    far = [bar(date(2024, 4, 22), "100")]  # more than five weekdays before 7 May
    missing = factor_set(far, [event("CANBK", "DIV - RS 5 PER SH", date(2024, 5, 7))], date(2024, 5, 7))
    assert [u.reason for u in missing.unresolved] == ["dividend_reference_missing"]
    near = [bar(date(2024, 4, 30), "100")]  # exactly five weekdays before 7 May
    ok = factor_set(near, [event("CANBK", "DIV - RS 5 PER SH", date(2024, 5, 7))], date(2024, 5, 7))
    assert ok.unresolved == () and ok.applied[0].price_factor == Decimal("0.95")
    exceeds = factor_set([bar(date(2024, 5, 6), "100")], [event("CANBK", "DIV - RS 100 PER SH", date(2024, 5, 7))],
                         date(2024, 5, 7))
    assert [u.reason for u in exceeds.unresolved] == ["dividend_exceeds_price"]


def test_adjustment_basis_and_factor_set_hash_are_stable():
    first = factor_set(CANBK_BARS, [CANBK_EVENT], D15)
    second = factor_set(CANBK_BARS, [CANBK_EVENT], D15)
    assert first.factor_set_sha256 == second.factor_set_sha256 and len(first.factor_set_sha256) == 64
    series = adjusted_series(CANBK_BARS, first, as_of=D15, workspace="india")
    assert series.adjustment_basis == (
        f"back_adjusted:as_of=2024-05-15:factor_set={first.factor_set_sha256}:policy=pilot-adjust/1")
    other = factor_set(CANBK_BARS[:1], [], D15)
    assert other.factor_set_sha256 != first.factor_set_sha256
    assert [c.code for c in series.caveats][:2] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS"]


@pytest.fixture
def store(tmp_path):
    with PilotDataStore(tmp_path / "pilot", workspace="india") as opened:
        yield opened


def test_persist_and_quarantine_are_idempotent_and_scoped_to_adjusted(store):
    bars = [bar(date(2024, 5, 13), "100"), bar(D14, "100"), bar(D15, "100")]
    lin = lineage(start=date(2024, 5, 13))
    fs = factor_set(bars, [event("CANBK", "RIGHTS 1:1 @ PRM RS 3/-", D15)], D15, lin)
    persist_factor_set(store, fs, workspace="india")
    persist_factor_set(store, fs, workspace="india")
    assert store.query("SELECT count(*) FROM adjustment_factor_sets")[0][0] == 1
    (record,) = quarantine_unresolved(store, lin, fs, workspace="india")
    quarantine_unresolved(store, lin, fs, workspace="india")
    assert (record.check, record.reason_code, record.scope) == ("corporate_action", "ca_unresolved_rights", "adjusted")
    assert (record.date_from, record.date_to) == (date(2024, 5, 13), D14)
    assert fs.factor_set_sha256 in record.evidence_sha256s and "e" * 64 in record.evidence_sha256s
    assert store.query("SELECT count(*) FROM quarantine_records WHERE check_name = 'corporate_action'")[0][0] == 1


def test_specific_reasons_name_the_quarantine_code(store):
    fs = factor_set([bar(date(2024, 5, 6), "100")], [event("CANBK", "DIV - RS 100 PER SH", date(2024, 5, 7))],
                    date(2024, 5, 7))
    (record,) = quarantine_unresolved(store, lineage(start=date(2024, 5, 1)), fs, workspace="india")
    assert record.reason_code == "ca_unresolved_dividend_exceeds_price"


# ------------------------------------------------------------------ Task 2: jumps
def jump_bars(open_after, prev_close="100"):
    return [bar(date(2024, 6, 3), prev_close),
            bar(date(2024, 6, 4), open_after, open_=open_after, high=open_after, low=open_after)]


def test_unexplained_jumps_are_flagged_at_the_boundaries():
    flagged = detect_unrecorded_actions(jump_bars("55"), [], POLICY)  # exactly 0.55
    assert [(u.kind, u.reason, u.ex_date) for u in flagged] == [
        ("suspected_unrecorded", "price_jump_without_action", date(2024, 6, 4))]
    assert flagged[0].detail["open_over_previous_close"] == "0.5500"
    assert len(detect_unrecorded_actions(jump_bars("180"), [], POLICY)) == 1  # exactly 1.80
    assert detect_unrecorded_actions(jump_bars("60"), [], POLICY) == ()
    assert detect_unrecorded_actions(jump_bars("179.99"), [], POLICY) == ()


def test_any_event_on_the_date_explains_the_jump():
    for purpose in ("FVSPLT FRM RS 10 TO RS 2", "BONUS 1:1", "DEMERGER"):
        assert detect_unrecorded_actions(jump_bars("30"), [event("CANBK", purpose, date(2024, 6, 4))], POLICY) == ()


def test_a_jump_feeds_the_unresolved_list_and_withholds_earlier_adjusted_prices():
    bars = [bar(date(2024, 6, 3), "100"), bar(date(2024, 6, 4), "30", open_="30"), bar(date(2024, 6, 5), "31")]
    fs = factor_set(bars, [], date(2024, 6, 5))
    assert [u.reason for u in fs.unresolved] == ["price_jump_without_action"]
    series = adjusted_series(bars, fs, as_of=date(2024, 6, 5), workspace="india")
    assert [b.adjusted_quarantined for b in series.bars] == [True, False, False]


# ------------------------------------------------------------------ Task 2: rawness
def ref_cand(pre_ratio, post_ratio, *, skip=None):
    reference = [bar(D14, "566.55"), bar(D15, "119")]
    candidate = [
        bar(D14, str((Decimal("566.55") * Decimal(pre_ratio)).quantize(Decimal("0.0001")))),
        bar(D15, str((Decimal("119") * Decimal(post_ratio)).quantize(Decimal("0.0001")))),
    ]
    if skip is not None:
        candidate = [b for b in candidate if b.trade_date != skip]
    return reference, candidate


def verdict_for(pre, post, **kwargs):
    reference, candidate = ref_cand(pre, post, **kwargs)
    fs = factor_set(reference, [CANBK_EVENT], D15)
    return verify_source_unadjusted(reference, candidate, fs, source_label="breeze_v2_1day", workspace="india")


def test_raw_adjusted_inconclusive_and_no_overlap_verdicts():
    assert verdict_for("1", "1").verdicts[0].verdict == "raw_confirmed"
    assert verdict_for("1.004", "0.996").verdicts[0].verdict == "raw_confirmed"  # inside the 0.5 percent tolerance
    adjusted = verdict_for("0.2", "1")
    assert adjusted.verdicts[0].verdict == "adjusted_detected" and adjusted.overall == "adjusted_detected"
    assert verdict_for("0.5", "1").verdicts[0].verdict == "inconclusive"
    assert verdict_for("1", "1", skip=D15).verdicts[0].verdict == "no_overlap"
    assert verdict_for("1", "1", skip=D14).verdicts[0].verdict == "no_overlap"


def test_overall_and_caveats_follow_the_verdicts():
    raw = verdict_for("1", "1")
    assert raw.overall == "raw_confirmed"
    assert [c.code for c in raw.caveats] == ["SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS"]
    unverified = verdict_for("1", "1", skip=D15)
    assert unverified.overall == "unverified" and "BREEZE_RAW_UNVERIFIED" in [c.code for c in unverified.caveats]
    adjusted = verdict_for("0.2", "1")
    assert "BREEZE_RAW_UNVERIFIED" in [c.code for c in adjusted.caveats]
    reference, candidate = ref_cand("1", "1")
    empty = verify_source_unadjusted(reference, candidate, factor_set(reference, [], D15),
                                     source_label="breeze_v2_1day", workspace="india")
    assert empty.verdicts == () and empty.overall == "unverified"


def test_dividends_are_excluded_from_rawness_verdicts():
    bars = [bar(date(2024, 5, 6), "1358.80"), bar(date(2024, 5, 7), "1340")]
    fs = factor_set(bars, [event("HCLTECH", "INTDIV - RS 18 PER SH", date(2024, 5, 7))], date(2024, 5, 7))
    report = verify_source_unadjusted(bars, bars, fs, source_label="breeze_v2_1day", workspace="india")
    assert report.verdicts == () and report.overall == "unverified"


def test_two_segment_lineage_uses_raw_closes_across_the_isin_change():
    # the reference bars carry the old ISIN before the split; factors are keyed by event date, not ISIN
    bars = [bar(D14, "566.55", isin=OLD), bar(D15, "119")]
    lin = Lineage(
        workspace="india", anchor_isin=NEW, anchor_series="EQ", stock_code="CANBAN",
        segments=(IsinSegment(isin=OLD, nse_symbol="CANBK", valid_from=D14, valid_to=D14, link="isin_change_split",
                              evidence="x"),
                  IsinSegment(isin=NEW, nse_symbol="CANBK", valid_from=D15, valid_to=D15, link="anchor")),
        resolved_from=D14, built_as_of=D15, basis="bhavcopy_walk",
    )
    fs = compute_factor_set(lin, bars, [CANBK_EVENT], as_of=D15, policy=POLICY)
    series = adjusted_series(bars, fs, as_of=D15, workspace="india")
    assert series.bars[0].isin == OLD and series.bars[0].adj_close == Decimal("113.3100")
