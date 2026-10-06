"""Adjustment factors, as-of adjusted series, jump detection and rawness verification."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from pilot_data.adjustment import (
    AdjustmentPolicy,
    AppliedFactor,
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
        f"back_adjusted:as_of=2024-05-15:factor_set={first.factor_set_sha256}:policy=pilot-adjust/2")
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


JUMP_DAY = date(2024, 6, 4)


def applied_factor(price_factor, kind="split"):
    return AppliedFactor(event_id="ev", ex_date=JUMP_DAY, kind=kind, price_factor=Decimal(price_factor),
                         volume_factor=Decimal(1) / Decimal(price_factor), structural_factor=Decimal(price_factor))


def jump_factor_set(open_after, purpose):
    bars = jump_bars(open_after)
    return factor_set(bars, [event("CANBK", purpose, JUMP_DAY)], JUMP_DAY), bars


def test_only_an_applied_price_factor_explains_the_jump():
    # 100 -> 30 is a 0.3 ratio; a 0.2 factor leaves a residual of 1.5, inside the band
    assert detect_unrecorded_actions(jump_bars("30"), [applied_factor("0.2")], POLICY) == ()
    assert len(detect_unrecorded_actions(jump_bars("30"), [applied_factor("0.99", "dividend")], POLICY)) == 1
    assert len(detect_unrecorded_actions(jump_bars("30"), [], POLICY)) == 1


def test_a_non_price_event_does_not_hide_a_crash():
    fs, bars = jump_factor_set("30", "AGM")
    assert fs.applied == ()
    assert [u.reason for u in fs.unresolved] == ["price_jump_without_action"]
    series = adjusted_series(bars, fs, as_of=JUMP_DAY, workspace="india")
    assert [b.adjusted_quarantined for b in series.bars] == [True, False]


def test_an_undersized_dividend_does_not_hide_a_big_drop():
    fs, bars = jump_factor_set("30", "DIV - RS 1 PER SH")
    assert [a.kind for a in fs.applied] == ["dividend"]
    (flag,) = fs.unresolved
    assert flag.reason == "price_jump_without_action" and flag.ex_date == JUMP_DAY
    assert flag.detail["applied_price_factor"] == "0.9900"
    series = adjusted_series(bars, fs, as_of=JUMP_DAY, workspace="india")
    assert series.bars[0].adjusted_quarantined and series.bars[0].adj_close is None


def test_a_bonus_with_an_extra_unexplained_drop_is_still_flagged():
    fs, bars = jump_factor_set("25", "BONUS 1:1")  # bonus explains 0.5, the rest (0.5) is a crash
    assert [a.kind for a in fs.applied] == ["bonus"]
    (flag,) = fs.unresolved
    assert flag.reason == "price_jump_without_action" and flag.detail["residual_after_factor"] == "0.5000"
    series = adjusted_series(bars, fs, as_of=JUMP_DAY, workspace="india")
    assert series.bars[0].adjusted_quarantined


def test_a_correctly_explained_split_or_bonus_stays_clean():
    for purpose, open_after in (("FVSPLT FRM RS 10 TO RS 2", "30"), ("FVSPLT FRM RS 10 TO RS 2", "20"),
                                ("BONUS 1:1", "50"), ("BONUS 1:1", "45")):
        fs, bars = jump_factor_set(open_after, purpose)
        assert fs.unresolved == (), (purpose, open_after)
        assert not adjusted_series(bars, fs, as_of=JUMP_DAY, workspace="india").bars[0].adjusted_quarantined


def test_an_unadjustable_event_on_the_date_still_reports_the_jump_and_itself():
    fs, _ = jump_factor_set("30", "DEMERGER")
    assert {u.reason for u in fs.unresolved} == {"not_adjustable", "price_jump_without_action"}


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


def test_events_before_the_first_bar_in_range_are_ignored():
    # Part 1 passes bars from the window start; a dividend that went ex before it
    # changes no bar in range and must not be reported as dividend_reference_missing.
    flat = [bar(D14, "100"), bar(D15, "100")]
    early = event("CANBK", "DIV - RS 5 PER SH", date(2024, 5, 10))
    fs = factor_set(flat, [early], D15)
    assert fs.unresolved == () and fs.applied == ()
    on_first_bar = event("CANBK", "DIV - RS 5 PER SH", D14)  # no earlier close: still reported, never guessed
    assert [u.reason for u in factor_set(flat, [on_first_bar], D15).unresolved] == ["dividend_reference_missing"]


# ------------------------------------------------------------------ D-20: amount-less interim dividends
J3, J4, J5, J6 = date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5), date(2024, 6, 6)
UNKNOWN = "dividend_amount_unknown"


def flat_bars(*days, open_by_day=None, close="100"):
    open_by_day = open_by_day or {}
    return [bar(day, close, open_=open_by_day.get(day, close), high="120", low="20") for day in days]


def interim(day, symbol="CANBK"):
    return event(symbol, "INTERIM DIVIDEND", day)


def test_policy_is_versioned_and_names_the_unknown_dividend_rule():
    assert (POLICY.version, POLICY.unknown_dividend_policy) == ("pilot-adjust/2", "unknown_zero/1")
    fs = factor_set(flat_bars(J3, J4), [interim(J4)], J4)
    assert fs.policy_version == "pilot-adjust/2"


def test_an_amountless_interim_dividend_is_resolved_as_factor_one_and_tagged():
    bars = flat_bars(J3, J4, J5, open_by_day={J4: "98"})  # a 2 percent open gap is a plausible dividend
    fs = factor_set(bars, [interim(J4)], J5)
    assert fs.unresolved == ()
    (item,) = fs.applied
    assert (item.ex_date, item.kind, item.price_factor, item.volume_factor, item.structural_factor) == (
        J4, UNKNOWN, Decimal(1), Decimal(1), Decimal(1))
    assert item.tags == (UNKNOWN,) and fs.unknown_dividend_events() == (item,)
    series = adjusted_series(bars, fs, as_of=J5, workspace="india")
    before, ex_day, after = series.bars
    assert not before.adjusted_quarantined and before.adj_close == before.raw_close  # price return: no scaling
    assert (before.dividend_amount_unknown, before.dividend_amount_unknown_ex_date) == (True, False)
    assert (ex_day.dividend_amount_unknown, ex_day.dividend_amount_unknown_ex_date) == (True, True)
    assert (after.dividend_amount_unknown, after.dividend_amount_unknown_ex_date) == (False, False)
    assert not any(b.adjusted_quarantined for b in series.bars)


def test_a_final_or_special_dividend_with_no_amount_is_out_of_scope_and_still_withholds():
    bars = flat_bars(J3, J4, J5)
    fs = factor_set(bars, [event("CANBK", "FINAL DIVIDEND", J4)], J5)
    assert fs.applied == () and [(u.kind, u.reason) for u in fs.unresolved] == [("unknown", "not_adjustable")]
    assert adjusted_series(bars, fs, as_of=J5, workspace="india").bars[0].adjusted_quarantined


def test_an_event_after_as_of_or_without_an_ex_date_is_not_lifted():
    bars = flat_bars(J3, J4)
    early = factor_set(bars[:1], [interim(J4)], J3)
    assert early.applied == () and early.unresolved == ()
    series = adjusted_series(bars[:1], early, as_of=J3, workspace="india")
    assert (series.bars[0].dividend_amount_unknown, series.bars[0].dividend_amount_unknown_ex_date) == (False, False)
    undated = factor_set(bars, [event("CANBK", "INTERIM DIVIDEND", None)], J4)
    assert [(u.kind, u.reason) for u in undated.unresolved] == [(UNKNOWN, "missing_ex_date")]
    assert all(b.adjusted_quarantined for b in adjusted_series(bars, undated, as_of=J4, workspace="india").bars)


@pytest.mark.parametrize(
    "other,purpose,open_after",
    [
        ("same-day bonus", "BONUS 1:1", "50"),
        ("same-day split", "FVSPLT FRM RS 10 TO RS 2", "20"),
        ("next-day bonus", "BONUS 1:1", "50"),
        ("same-day rights", "RIGHTS 1:1 @ PRM RS 3/-", "100"),
        ("same-day demerger", "DEMERGER", "100"),
    ],
)
def test_an_amountless_dividend_next_to_a_split_bonus_or_unresolved_action_is_not_lifted(other, purpose, open_after):
    other_day = J5 if other.startswith("next-day") else J4
    bars = flat_bars(J3, J4, J5, J6, open_by_day={other_day: open_after})
    fs = factor_set(bars, [interim(J4), event("CANBK", purpose, other_day)], J6)
    assert [a.kind for a in fs.applied if UNKNOWN in a.tags] == []
    (conflict,) = [u for u in fs.unresolved if u.reason == "dividend_amount_unknown_conflict"]
    assert conflict.ex_date == J4 and conflict.kind == UNKNOWN and conflict.detail["nearby"]
    series = adjusted_series(bars, fs, as_of=J6, workspace="india")
    assert series.bars[0].adjusted_quarantined and series.bars[0].adj_close is None
    assert not any(b.dividend_amount_unknown or b.dividend_amount_unknown_ex_date for b in series.bars)


def test_a_bonus_and_an_amountless_dividend_in_one_purpose_is_not_lifted():
    bars = flat_bars(J3, J4)
    fs = factor_set(bars, [event("CANBK", "BONUS 1:1/INTERIM DIVIDEND", J4)], J4)
    assert fs.applied == () and [(u.kind, u.reason) for u in fs.unresolved] == [(UNKNOWN, "not_adjustable")]


def test_a_distant_split_does_not_block_the_lift_but_still_applies():
    days = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 7, 15), date(2024, 7, 16)]
    bars = flat_bars(*days, open_by_day={days[3]: "50"})
    fs = factor_set(bars, [interim(days[1]), event("CANBK", "BONUS 1:1", days[3])], days[3])
    assert fs.unresolved == ()
    assert sorted(a.kind for a in fs.applied) == sorted([UNKNOWN, "bonus"])
    assert [a.ex_date for a in fs.applied] == sorted(a.ex_date for a in fs.applied)


def test_an_amount_known_dividend_nearby_does_not_block_the_lift():
    bars = flat_bars(J3, J4, J5, J6)
    fs = factor_set(bars, [interim(J4), event("CANBK", "DIV - RS 5 PER SH", J5)], J6)
    assert fs.unresolved == ()
    by_kind = {a.kind: a for a in fs.applied}
    assert by_kind[UNKNOWN].tags == (UNKNOWN,) and by_kind["dividend"].tags == ()


def test_a_large_unexplained_gap_on_the_ex_date_is_still_flagged():
    bars = flat_bars(J3, J4, J5, open_by_day={J4: "30"})  # open at 0.30 of the previous close
    fs = factor_set(bars, [interim(J4)], J5)
    reasons = {u.reason for u in fs.unresolved}
    assert reasons == {"price_jump_without_action", "dividend_amount_unknown_conflict"}
    (jump,) = [u for u in fs.unresolved if u.reason == "price_jump_without_action"]
    assert jump.ex_date == J4 and jump.detail["open_over_previous_close"] == "0.3000"
    assert fs.unknown_dividend_events() == ()
    series = adjusted_series(bars, fs, as_of=J5, workspace="india")
    assert series.bars[0].adjusted_quarantined and series.bars[0].adj_close is None
    assert not series.bars[1].adjusted_quarantined
    # a 0.56 ratio is inside 59's jump band but far too big for a dividend: still not lifted
    inside = factor_set(flat_bars(J3, J4, J5, open_by_day={J4: "56"}), [interim(J4)], J5)
    assert [(u.reason, u.detail["nearby"]) for u in inside.unresolved] == [
        ("dividend_amount_unknown_conflict", "gap:0.5600")]
    assert inside.unknown_dividend_events() == ()


def test_an_amount_known_dividend_is_unchanged_and_untagged():
    bars = [bar(date(2024, 5, 6), "1358.80"), bar(date(2024, 5, 7), "1340")]
    fs = factor_set(bars, [event("HCLTECH", "INTDIV - RS 18 PER SH", date(2024, 5, 7))], date(2024, 5, 7))
    (item,) = fs.applied
    assert item.price_factor == Decimal(1) - Decimal(18) / Decimal("1358.80") and item.tags == ()
    assert fs.unknown_dividend_events() == () and fs.unresolved == ()
    series = adjusted_series(bars, fs, as_of=date(2024, 5, 7), workspace="india")
    assert not any(b.dividend_amount_unknown or b.dividend_amount_unknown_ex_date for b in series.bars)
    assert series.bars[0].adj_close == (Decimal("1358.80") * item.price_factor).quantize(Decimal("0.0001"))


def test_two_amountless_dividends_close_together_are_both_lifted():
    bars = flat_bars(J3, J4, J5, J6)
    fs = factor_set(bars, [interim(J4), interim(J6)], J6)
    assert fs.unresolved == () and [a.ex_date for a in fs.unknown_dividend_events()] == [J4, J6]


def test_the_factor_set_with_an_amountless_dividend_is_deterministic_and_persists_once(store):
    bars = flat_bars(J3, J4, J5)
    lin = lineage(start=J3)
    first = factor_set(bars, [interim(J4)], J5, lin)
    second = factor_set(bars, [interim(J4)], J5, lin)
    assert first.factor_set_sha256 == second.factor_set_sha256
    assert first.factor_set_sha256 != factor_set(bars, [], J5, lin).factor_set_sha256
    persist_factor_set(store, first, workspace="india")
    persist_factor_set(store, second, workspace="india")
    assert store.query("SELECT count(*) FROM adjustment_factor_sets")[0][0] == 1
    assert quarantine_unresolved(store, lin, first, workspace="india") == ()  # nothing left to quarantine
    assert store.query("SELECT count(*) FROM quarantine_records")[0][0] == 0
    assert adjusted_series(bars, first, as_of=J5, workspace="india") == adjusted_series(
        bars, second, as_of=J5, workspace="india")


def test_a_conflict_quarantines_with_its_own_reason_code_and_is_idempotent(store):
    bars = flat_bars(J3, J4, J5, open_by_day={J4: "50"})
    lin = lineage(start=J3)
    fs = factor_set(bars, [interim(J4), event("CANBK", "RIGHTS 1:1 @ PRM RS 3/-", J4)], J5, lin)
    first = quarantine_unresolved(store, lin, fs, workspace="india")
    quarantine_unresolved(store, lin, fs, workspace="india")
    assert "ca_unresolved_dividend_amount_unknown_conflict" in {r.reason_code for r in first}
    count = store.query("SELECT count(*) FROM quarantine_records WHERE check_name = 'corporate_action'")[0][0]
    assert count == len(first)


def gap_set(open_after, *, events=None, days=(J3, J4, J5)):
    bars = flat_bars(*days, open_by_day={J4: open_after})
    return factor_set(bars, events or [interim(J4)], days[-1]), bars


def test_a_hidden_one_for_two_bonus_gap_is_not_read_as_a_dividend():
    fs, bars = gap_set("66.7")  # a hidden 1:2 bonus opens at 2/3 of the previous close
    assert fs.unknown_dividend_events() == ()
    assert [(u.reason, u.detail["nearby"]) for u in fs.unresolved] == [
        ("dividend_amount_unknown_conflict", "gap:0.6670")]
    series = adjusted_series(bars, fs, as_of=J5, workspace="india")
    assert series.bars[0].adjusted_quarantined and series.bars[0].adj_close is None
    assert not any(b.dividend_amount_unknown or b.dividend_amount_unknown_ex_date for b in series.bars)


@pytest.mark.parametrize(
    "open_after,lifted",
    [("79", False), ("79.99", False), ("80", True), ("81", True), ("98", True), ("100", True), ("119", True),
     ("120", True), ("121", False), ("150", False)],
)
def test_the_ex_date_gap_must_stay_within_the_policy_threshold_either_way(open_after, lifted):
    fs, _ = gap_set(open_after)
    assert (len(fs.unknown_dividend_events()) == 1) is lifted
    if lifted:
        assert fs.unresolved == ()
    else:
        assert [u.reason for u in fs.unresolved] == ["dividend_amount_unknown_conflict"]
        assert fs.unresolved[0].detail["nearby"].startswith("gap:")


def test_the_gap_threshold_is_policy_and_changes_the_factor_set_hash():
    assert POLICY.unknown_dividend_max_gap == Decimal("0.20")
    bars = flat_bars(J3, J4, J5, open_by_day={J4: "85"})
    default = compute_factor_set(lineage(), bars, [interim(J4)], as_of=J5, policy=POLICY)
    tight = compute_factor_set(lineage(), bars, [interim(J4)], as_of=J5,
                               policy=AdjustmentPolicy(unknown_dividend_max_gap=Decimal("0.10")))
    assert len(default.unknown_dividend_events()) == 1 and tight.unknown_dividend_events() == ()
    assert default.unknown_dividend_params["max_gap"] == "0.2"
    assert default.unknown_dividend_params["policy"] == "unknown_zero/1"
    same_outcome = compute_factor_set(lineage(), flat_bars(J3, J4, J5), [interim(J4)], as_of=J5,
                                      policy=AdjustmentPolicy(unknown_dividend_max_gap=Decimal("0.30")))
    base = compute_factor_set(lineage(), flat_bars(J3, J4, J5), [interim(J4)], as_of=J5, policy=POLICY)
    assert same_outcome.factor_set_sha256 != base.factor_set_sha256  # the threshold is hashed


def test_a_missing_ex_date_bar_or_previous_bar_fails_closed():
    no_ex_bar = factor_set(flat_bars(J3, J5), [interim(J4)], J5)  # nothing traded on the ex-date
    assert no_ex_bar.unknown_dividend_events() == ()
    assert [(u.reason, u.detail["nearby"]) for u in no_ex_bar.unresolved] == [
        ("dividend_amount_unknown_conflict", "gap:unmeasurable")]
    # the ex-date is the first bar: nothing earlier to measure against
    no_previous = factor_set(flat_bars(J4, J5), [interim(J4)], J5)
    assert no_previous.unknown_dividend_events() == ()
    assert [u.detail["nearby"] for u in no_previous.unresolved] == ["gap:unmeasurable"]


def test_a_different_undated_unresolved_action_blocks_a_dated_interim_dividend():
    fs = factor_set(flat_bars(J3, J4, J5), [interim(J4), event("CANBK", "DEMERGER", None)], J5)
    assert fs.unknown_dividend_events() == ()
    assert {u.reason for u in fs.unresolved} == {"missing_ex_date", "dividend_amount_unknown_conflict"}
    (conflict,) = [u for u in fs.unresolved if u.reason == "dividend_amount_unknown_conflict"]
    assert "missing_ex_date:no_ex_date" in conflict.detail["nearby"]


def test_an_ex_date_before_the_first_bar_is_ignored():
    fs = factor_set(flat_bars(J3, J4), [interim(date(2024, 6, 1))], J4)
    assert fs.applied == () and fs.unresolved == ()


def test_the_conflict_window_edge_is_seven_calendar_days_inclusive():
    june_11, june_12 = date(2024, 6, 11), date(2024, 6, 12)
    days = [J3, J4, date(2024, 6, 10), june_11, june_12]
    for bonus_day, blocked in ((june_11, True), (june_12, False)):  # 7 and 8 days after the 4th
        bars = [bar(d, "100", open_="50" if d == bonus_day else "100", high="120", low="20") for d in days]
        fs = factor_set(bars, [interim(J4), event("CANBK", "BONUS 1:1", bonus_day)], june_12)
        assert [a.kind for a in fs.applied if UNKNOWN in a.tags] == ([] if blocked else [UNKNOWN]), bonus_day
        assert any(u.reason == "dividend_amount_unknown_conflict" for u in fs.unresolved) is blocked, bonus_day


def test_bars_are_tagged_even_when_a_later_unresolved_action_withholds_their_values():
    days = [J3, J4, J5, date(2024, 7, 15), date(2024, 7, 16)]
    fs = factor_set(flat_bars(*days), [interim(J4), event("CANBK", "RIGHTS 1:1 @ PRM RS 3/-", days[4])], days[4])
    assert len(fs.unknown_dividend_events()) == 1  # 42 days apart: not a conflict
    series = adjusted_series(flat_bars(*days), fs, as_of=days[4], workspace="india")
    assert [(b.adjusted_quarantined, b.dividend_amount_unknown) for b in series.bars] == [
        (True, True), (True, True), (True, False), (True, False), (False, False)]


def test_the_hidden_bonus_case_keeps_the_factor_set_hash_it_had_on_main():
    # The conflict label is part of the hashed factor set; pilot-adjust/2 has shipped, so it must not drift.
    fs, _ = gap_set("66.7")
    assert fs.factor_set_sha256 == "0f15f9f39964fbb84a8e0f6714569f6b8622d610f72fe2aad63d3066a473e27a"
