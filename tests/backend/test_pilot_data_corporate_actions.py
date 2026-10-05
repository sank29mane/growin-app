"""Purpose grammar and typed, append-only corporate-action events."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from pilot_data.bhavcopy import ingest_pr_zip
from pilot_data.corporate_actions import (
    derive_corporate_actions,
    events_for_symbol,
    is_adjustable,
    parse_purpose,
)
from pilot_data.core import SourceDescriptor
from pilot_data.store import PilotDataStore

import pilot_data_testkit as kit

FETCHED = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)


def kinds(purpose):
    return [part.kind for part in parse_purpose(purpose)]


def test_face_value_splits_and_consolidations():
    (split,) = parse_purpose("FVSPLT FRM RS 10 TO RS 2")
    assert (split.kind, split.old_fv, split.new_fv) == ("split", Decimal("10"), Decimal("2"))
    (re_split,) = parse_purpose("FVSPLT FRM RS 10 TO RE 1")
    assert (re_split.kind, re_split.old_fv, re_split.new_fv) == ("split", Decimal("10"), Decimal("1"))
    assert kinds("FVSPLT FRM RS 5 TO RS 2") == ["split"]
    (consolidation,) = parse_purpose("FVSPLT FRM RS 1 TO RS 10")
    assert consolidation.kind == "consolidation"
    assert kinds("FVSPLT FRM RS 0 TO RS 2") == ["unknown"]
    assert kinds("FVSPLT FRM RS 2 TO RS 2") == ["unknown"]


def test_long_form_face_value_split_parses_identically():
    short = parse_purpose("FVSPLT FRM RS 10 TO RE 1")
    long = parse_purpose("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share")
    assert short == long


def test_bonus_ratios():
    (four_one,) = parse_purpose("BONUS 4:1")
    assert (four_one.kind, four_one.bonus_new, four_one.bonus_held) == ("bonus", Decimal("4"), Decimal("1"))
    (one_two,) = parse_purpose("BONUS 1:2")
    assert (one_two.bonus_new, one_two.bonus_held) == (Decimal("1"), Decimal("2"))
    assert kinds("BONUS 0:1") == ["unknown"]


@pytest.mark.parametrize(
    "purpose,amount",
    [("INTDIV - RS 7 PER SH", "7"), ("DIV - RS 117 PER SH", "117"), ("INTDIV - RS 240 PER SH", "240"),
     ("FINAL DIVIDEND RS 5.50 PER SHARE", "5.50"), ("SPLDIV - RS 3 PER SH", "3")],
)
def test_single_dividends(purpose, amount):
    (part,) = parse_purpose(purpose)
    assert part.kind == "dividend" and part.dividend_per_share == Decimal(amount)


def test_agm_dividend_gives_non_price_plus_dividend_and_is_adjustable():
    parts = parse_purpose("AGM/DIV RS 23.80 PER SH")
    assert [p.kind for p in parts] == ["non_price", "dividend"]
    assert parts[1].dividend_per_share == Decimal("23.80")
    assert is_adjustable(parts)


def test_two_dividend_parts_sum():
    parts = parse_purpose("INTDIV - RS 7 PER SH + SPLDIV - RS 3 PER SH")
    assert [p.kind for p in parts] == ["dividend"]
    assert parts[0].dividend_per_share == Decimal("10")


@pytest.mark.parametrize(
    "purpose,kind",
    [("RIGHTS 1:1 @ PRM RS 3/-", "rights"), ("DEMERGER", "demerger"), ("MERGER", "merger"),
     ("AMALGAMATION", "merger"), ("CAPITAL REDUCTION", "other_price_affecting")],
)
def test_non_adjustable_price_events(purpose, kind):
    parts = parse_purpose(purpose)
    assert [p.kind for p in parts] == [kind]
    assert not is_adjustable(parts)


@pytest.mark.parametrize("purpose", ["ANNUAL GENERAL MEETING", "INTEREST PAYMENT", "REDEMPTION", "STP", "AGM", "EGM"])
def test_non_price_allowlist_is_adjustable_with_no_price_parts(purpose):
    parts = parse_purpose(purpose)
    assert [p.kind for p in parts] == ["non_price"]
    assert is_adjustable(parts)


def test_div_stp_without_an_amount_is_unknown():
    parts = parse_purpose("DIV/STP")
    assert [p.kind for p in parts] == ["unknown", "non_price"]
    assert not is_adjustable(parts)


@pytest.mark.parametrize("purpose", ["SOMETHING ELSE", "", "BONUS", "DIV RS", "ANNUAL GENERAL MEETING AND SPLIT"])
def test_unrecognised_text_is_unknown_and_not_adjustable(purpose):
    parts = parse_purpose(purpose)
    assert "unknown" in [p.kind for p in parts]
    assert not is_adjustable(parts)


def test_normalisation_collapses_case_and_whitespace():
    assert parse_purpose("  fvsplt   frm rs 10 to rs 2 ") == parse_purpose("FVSPLT FRM RS 10 TO RS 2")


# ------------------------------------------------------------ events and sightings
@pytest.fixture
def store(tmp_path):
    with PilotDataStore(tmp_path / "pilot", workspace="india") as opened:
        yield opened


def ingest_pr(store, day, bc_rows):
    ingest_pr_zip(
        store,
        SourceDescriptor(source="nse_archive", kind="pr_zip", locator=f"https://nsearchives.nseindia.com/pr/{day}",
                         fetched_at=FETCHED, for_date=day),
        kit.pr_zip(day, [kit.pd_index_row()], bc_rows, []),
        trade_date=day,
    )


def test_same_action_across_files_and_series_is_one_event(store):
    ex = date(2024, 5, 15)
    purpose = "FVSPLT FRM RS 10 TO RS 2"
    for day in (date(2024, 5, 2), date(2024, 5, 3), date(2024, 5, 6)):
        ingest_pr(store, day, [kit.bc_row("EQ", "CANBK", "CANARA BANK", purpose, ex_date=ex),
                               kit.bc_row("BE", "CANBK", "CANARA BANK", purpose, ex_date=ex)])
    outcome = derive_corporate_actions(store, workspace="india")
    assert outcome.events_inserted == 1 and outcome.sightings_inserted == 6 and outcome.conflicts == 0
    (event,) = events_for_symbol(store, "CANBK", ex_from=date(2024, 5, 1), ex_to=date(2024, 5, 31))
    assert event.series_seen == ("BE", "EQ")
    assert event.first_seen_file_date == date(2024, 5, 2) and event.last_seen_file_date == date(2024, 5, 6)
    assert event.sightings == 6 and event.ex_date == ex and event.adjustable
    assert [p.kind for p in event.parts] == ["split"]
    assert len(event.event_id) == 64


def test_derive_is_idempotent_and_ids_are_stable(store):
    ingest_pr(store, date(2024, 5, 2), [kit.bc_row("EQ", "ABC", "ABC LTD", "BONUS 1:1", ex_date=date(2024, 6, 3))])
    first = derive_corporate_actions(store, workspace="india")
    again = derive_corporate_actions(store, workspace="india")
    assert first.events_inserted == 1 and again.events_inserted == 0 and again.events_identical == 1
    assert again.sightings_inserted == 0 and again.conflicts == 0
    (a,) = events_for_symbol(store, "ABC", ex_from=date(2024, 1, 1), ex_to=date(2024, 12, 31))
    ingest_pr(store, date(2024, 5, 3), [kit.bc_row("EQ", "ABC", "ABC LTD", "BONUS 1:1", ex_date=date(2024, 6, 3))])
    derive_corporate_actions(store, workspace="india")
    (b,) = events_for_symbol(store, "ABC", ex_from=date(2024, 1, 1), ex_to=date(2024, 12, 31))
    assert a.event_id == b.event_id and b.sightings == 2


def test_events_for_symbol_filters_by_ex_date_and_includes_null_ex_dates(store):
    ingest_pr(
        store, date(2024, 5, 2),
        [
            kit.bc_row("EQ", "ABC", "ABC LTD", "BONUS 1:1", ex_date=date(2024, 6, 3)),
            kit.bc_row("EQ", "ABC", "ABC LTD", "INTDIV - RS 2 PER SH", ex_date=date(2025, 6, 3)),
            kit.bc_row("EQ", "ABC", "ABC LTD", "DEMERGER"),
            kit.bc_row("EQ", "OTHER", "OTHER LTD", "BONUS 1:1", ex_date=date(2024, 6, 3)),
        ],
    )
    derive_corporate_actions(store, workspace="india")
    found = events_for_symbol(store, "ABC", ex_from=date(2024, 1, 1), ex_to=date(2024, 12, 31))
    assert [(e.ex_date, e.parts[0].kind) for e in found] == [(None, "demerger"), (date(2024, 6, 3), "bonus")]
    assert found[0].ex_date is None and not found[0].adjustable


# Wordings seen in NSE Bc files 2021-2026 that the first classifier missed.
@pytest.mark.parametrize(
    "purpose,expected",
    [
        ("FV SPLT FRM RS 2 TO RE 1", [("split", "2", "1")]),
        ("FV SPLIT FRM RS 5 TO RE 1", [("split", "5", "1")]),
        ("FVSPLT FRM RS 10 TO RS 2", [("split", "10", "2")]),
        ("AGM/DIV-RS 2.50 PER SH", [("non_price", None, None), ("dividend", "2.50", None)]),
        ("DIV-RE 0.50 PER SH", [("dividend", "0.50", None)]),
        ("DIVIDEND - RS 4 PER SHARE", [("dividend", "4", None)]),
        ("BUY BACK", [("non_price", None, None)]),
        ("BUYBACK", [("non_price", None, None)]),
        ("INT PAYMENT/REDEMPTION", [("non_price", None, None), ("non_price", None, None)]),
        ("INTEREST PAYMENT/REDEMPTI", [("non_price", None, None), ("non_price", None, None)]),
        ("INT PAYMENT/REDEMPTN", [("non_price", None, None), ("non_price", None, None)]),
        ("INTDIV - RS 8 PR SH", [("dividend", "8", None)]),
        ("AGM/DIV- RS 12.5 PR SH", [("non_price", None, None), ("dividend", "12.5", None)]),
        ("INT DIV - RS 7.50 PR SH", [("dividend", "7.50", None)]),
        ("BONUS- 1:2", [("bonus", None, None)]),
        ("RGHTS 11:64@PRM RS 473/-", [("rights", None, None)]),
        ("EOGM", [("non_price", None, None)]),
        ("FULL REDEMPTION", [("non_price", None, None)]),
        ("PARTREDEMP-RS 12.5 TO 10", [("non_price", None, None)]),
        ("INT PYMNT/PART RDMPTION", [("non_price", None, None), ("non_price", None, None)]),
    ],
)
def test_real_nse_purpose_wordings_classify(purpose, expected):
    got = []
    for part in parse_purpose(purpose):
        amount = part.dividend_per_share if part.kind == "dividend" else part.old_fv
        got.append((part.kind, None if amount is None else str(amount), None if part.new_fv is None else str(part.new_fv)))
    assert got == expected


@pytest.mark.parametrize("purpose", ["INTERIM DIVIDEND", "DIVRS 5", "REDEMPTIONS", "FV SPLT FRM RS 2 TO RS 2"])
def test_purposes_without_usable_terms_stay_unknown(purpose):
    # No amount, an unknown shape or a no-op split: never guessed.
    assert [part.kind for part in parse_purpose(purpose)] == ["unknown"] * len(parse_purpose(purpose))
