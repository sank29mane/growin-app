"""Contract-note fixture schema and comparator, on a SYNTHETIC fixture.

Provenance: SYNTHETIC. The notes below are computed from published rates (the
7,000 rupee worked example) for tests. They are not contract notes and no
operator number appears anywhere in this file.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from decimal import Decimal

import pytest

from costs.contract_note import (
    FIXTURE_SCHEMA,
    REQUIRED_BUY_LINES,
    REQUIRED_LINES,
    TOLERANCE,
    ContractNote,
    ContractNoteError,
    ContractNoteTrade,
    ReferenceKind,
    compare_note,
    compare_preview,
    load_fixture,
    load_fixture_text,
    price_note,
)
from costs.core import InputError, ScheduleNotEffective, Side
from costs.schedule import load_schedule_set

D = Decimal
ISIN = "INE0TEST0001"
PROVENANCE = "SYNTHETIC: computed from published rates for tests; not a contract note"


def buy_note() -> dict:
    return {
        "label": "synthetic-delivery-buy",
        "trade_date": "2026-10-05",
        "exchange": "NSE",
        "segment": "cash",
        "brokerage_billing": "gross",
        "dp_source": "not_applicable",
        "trades": [{"order_ref": "o1", "isin": ISIN, "side": "BUY", "quantity": 70, "price": "100.00"}],
        "lines": {
            "brokerage": "4.90", "exchange_transaction": "0.21", "sebi_fee": "0.01",
            "gst": "0.92", "stt": "7.00", "stamp_duty": "1.05",
        },
        "dp_lines": None,
        "note_total_charges": None,
        "unmapped_lines": [],
    }


def sell_note() -> dict:
    return {
        "label": "synthetic-delivery-sell",
        "trade_date": "2026-10-06",
        "exchange": "NSE",
        "segment": "cash",
        "brokerage_billing": "gross",
        "dp_source": "contract_note",
        "trades": [{"order_ref": "o2", "isin": ISIN, "side": "SELL", "quantity": 70, "price": "100.00"}],
        "lines": {
            "brokerage": "4.90", "exchange_transaction": "0.21", "sebi_fee": "0.01",
            "gst": "0.92", "stt": "7.00",
        },
        "dp_lines": {"dp_charge": "20.00", "dp_gst": "3.60"},
        "note_total_charges": None,
        "unmapped_lines": [],
    }


def document(*notes: dict, attested=True) -> dict:
    return {
        "schema": FIXTURE_SCHEMA,
        "redaction_attested": attested,
        "provenance": PROVENANCE,
        "notes": list(notes) if notes else [buy_note(), sell_note()],
    }


def load(doc: dict):
    return load_fixture_text(json.dumps(doc))


def one_note(note: dict) -> ContractNote:
    return load(document(note)).notes[0]


SCHEDULES = load_schedule_set()


def priced(note: ContractNote):
    return price_note(note, SCHEDULES, workspace="india", currency="INR")


def compare(note: ContractNote, **kwargs):
    return compare_note(note, priced(note), schedules=SCHEDULES, **kwargs)


# ---- comparator --------------------------------------------------------------


def test_synthetic_notes_pass_with_zero_deltas():
    fixture = load(document())
    assert fixture.provenance.startswith("SYNTHETIC")
    assert len(fixture.fixture_hash) == 64
    for note in fixture.notes:
        result = compare(note)
        assert result.passed is True
        assert result.authoritative is True
        assert result.kind is ReferenceKind.CONTRACT_NOTE
        assert result.rows and all(row.delta == D("0.00") for row in result.rows)
        assert result.missing_required == () and result.unmapped == ()


@pytest.mark.parametrize("amount", ["4.95", "4.85"])
def test_exactly_the_tolerance_passes(amount):
    note = buy_note()
    note["lines"]["brokerage"] = amount
    assert compare(one_note(note)).passed is True


def test_just_beyond_the_tolerance_fails():
    note = buy_note()
    note["lines"]["brokerage"] = "4.96"
    result = compare(one_note(note))
    assert result.passed is False
    failing = [row for row in result.rows if row.line == "brokerage"][0]
    assert failing.within_tolerance is False
    assert failing.delta == D("0.06")
    assert all(row.within_tolerance for row in result.rows if row.line != "brokerage")


def test_tolerance_cannot_be_loosened():
    note = one_note(buy_note())
    estimate = priced(note)
    assert TOLERANCE == D("0.05")
    with pytest.raises(InputError):
        compare_note(note, estimate, schedules=SCHEDULES, tolerance=D("0.06"))
    with pytest.raises(InputError):
        compare_preview({"brokerage": "4.90"}, estimate, tolerance=D("0.06"))
    assert compare_note(note, estimate, schedules=SCHEDULES, tolerance=D("0.01")).passed is True
    compare_preview({"brokerage": "4.90"}, estimate, tolerance=D("0.01"))


def test_dp_not_supplied_is_unverified_not_failed():
    sell = sell_note()
    sell["dp_source"] = "not_supplied"
    sell["dp_lines"] = None
    result = compare(one_note(sell))
    assert result.passed is True
    assert "dp_charge" in result.unverified
    assert "dp_gst" in result.unverified


def test_unmapped_note_lines_fail_the_comparison():
    note = buy_note()
    note["unmapped_lines"] = ["clearing_charges"]
    result = compare(one_note(note))
    assert result.passed is False
    assert result.unmapped == ("clearing_charges",)
    assert all(row.within_tolerance for row in result.rows)


def test_prepaid_credit_note_compares_against_brokerage_zeroed():
    note = buy_note()
    note["brokerage_billing"] = "prepaid_credit_applied"
    note["lines"]["brokerage"] = "0.00"
    note["lines"]["gst"] = "0.04"
    result = compare(one_note(note))
    assert result.passed is True
    assert [row.delta for row in result.rows if row.line in ("brokerage", "gst")] == [D("0.00"), D("0.00")]
    gross = buy_note()
    gross["lines"]["brokerage"] = "0.00"
    gross["lines"]["gst"] = "0.04"
    assert compare(one_note(gross)).passed is False


def test_gst_components_must_sum_to_the_gst_line():
    note = buy_note()
    note["gst_components"] = {"cgst": "0.46", "sgst": "0.46"}
    assert one_note(note).gst_components == {"cgst": D("0.46"), "sgst": D("0.46")}
    note["gst_components"] = {"cgst": "0.46", "sgst": "0.45"}
    with pytest.raises(ContractNoteError):
        one_note(note)


def test_note_total_must_match_the_printed_lines():
    note = buy_note()
    note["note_total_charges"] = "14.09"
    assert one_note(note).note_total_charges == D("14.09")
    note["note_total_charges"] = "14.10"
    one_note(note)
    note["note_total_charges"] = "14.11"
    with pytest.raises(ContractNoteError):
        one_note(note)
    sell = sell_note()
    sell["note_total_charges"] = "36.64"
    one_note(sell)
    sell["note_total_charges"] = "13.04"
    with pytest.raises(ContractNoteError):
        one_note(sell)


# ---- thin notes --------------------------------------------------------------


def test_thin_notes_fail_at_load():
    empty = buy_note()
    empty["lines"] = {}
    with pytest.raises(ContractNoteError):
        one_note(empty)
    only_brokerage = buy_note()
    only_brokerage["lines"] = {"brokerage": "4.90"}
    with pytest.raises(ContractNoteError):
        one_note(only_brokerage)
    for name in REQUIRED_LINES:
        note = buy_note()
        del note["lines"][name]
        with pytest.raises(ContractNoteError, match=name):
            one_note(note)
    for name in REQUIRED_BUY_LINES:
        note = buy_note()
        del note["lines"][name]
        with pytest.raises(ContractNoteError, match=name):
            one_note(note)


def test_a_sell_note_without_stamp_duty_loads():
    assert "stamp_duty" not in one_note(sell_note()).lines


def test_a_hand_built_thin_note_cannot_pass_compare_note():
    trade = ContractNoteTrade("o1", ISIN, Side.BUY, 70, D("100.00"))
    thin = ContractNote(
        label="hand-built", trade_date=date(2026, 10, 5), exchange="NSE", segment="cash",
        brokerage_billing="gross", dp_source="not_applicable", trades=(trade,),
        lines={"brokerage": D("4.90")}, gst_components=None, dp_lines=None,
        note_total_charges=None, unmapped_lines=(),
    )
    result = compare(thin)
    assert all(row.within_tolerance for row in result.rows)
    assert result.missing_required == ("exchange_transaction", "stt", "gst", "stamp_duty")
    assert result.passed is False


# ---- schema, redaction and identifier rejection ------------------------------


def with_edit(edit) -> str:
    doc = copy.deepcopy(document())
    edit(doc)
    return json.dumps(doc)


BAD_DOCUMENTS = {
    "unknown top-level key": lambda d: d.update(extra="x"),
    "unknown note key": lambda d: d["notes"][0].update(extra="x"),
    "unknown trade key": lambda d: d["notes"][0]["trades"][0].update(extra="x"),
    "unknown lines key": lambda d: d["notes"][0]["lines"].update(extra="1.00"),
    "unknown dp_lines key": lambda d: d["notes"][1]["dp_lines"].update(extra="1.00"),
    "redaction not attested": lambda d: d.update(redaction_attested=False),
    "redaction missing": lambda d: d.pop("redaction_attested"),
    "PAN-shaped provenance": lambda d: d.update(provenance="transcribed for ABCDE1234F"),
    "PAN-shaped label": lambda d: d["notes"][0].update(label="ABCDE1234F"),
    "eight digit run": lambda d: d["notes"][0].update(label="note-12345678"),
    "email-like unmapped line": lambda d: d["notes"][0].update(unmapped_lines=["a@b"]),
    "order_ref uppercase": lambda d: d["notes"][0]["trades"][0].update(order_ref="O1"),
    "order_ref too long": lambda d: d["notes"][0]["trades"][0].update(order_ref="a" * 17),
    "order_ref with underscore": lambda d: d["notes"][0]["trades"][0].update(order_ref="o_1"),
    "isin too short": lambda d: d["notes"][0]["trades"][0].update(isin="INE0TEST000"),
    "isin wrong prefix": lambda d: d["notes"][0]["trades"][0].update(isin="US0TEST00001"),
    "wrong schema": lambda d: d.update(schema="growin.costs.contract_note_fixture/2"),
    "wrong exchange": lambda d: d["notes"][0].update(exchange="BSE"),
    "bad billing": lambda d: d["notes"][0].update(brokerage_billing="free"),
    "bad dp_source": lambda d: d["notes"][0].update(dp_source="guess"),
    "dp_lines with not_applicable": lambda d: d["notes"][0].update(dp_lines={"dp_charge": "1.00"}),
    "dp_lines missing for contract_note": lambda d: d["notes"][1].update(dp_lines=None),
    "negative line": lambda d: d["notes"][0]["lines"].update(brokerage="-4.90"),
    "zero price": lambda d: d["notes"][0]["trades"][0].update(price="0"),
    "int quantity as string": lambda d: d["notes"][0]["trades"][0].update(quantity="70"),
    "no trades": lambda d: d["notes"][0].update(trades=[]),
    "no notes": lambda d: d.update(notes=[]),
}


@pytest.mark.parametrize("label", list(BAD_DOCUMENTS))
def test_load_rejects(label):
    with pytest.raises(ContractNoteError):
        load_fixture_text(with_edit(BAD_DOCUMENTS[label]))


def test_bare_json_numbers_are_rejected():
    text = json.dumps(document()).replace('"price": "100.00"', '"price": 100.00', 1)
    with pytest.raises(ContractNoteError):
        load_fixture_text(text)
    text = json.dumps(document()).replace('"brokerage": "4.90"', '"brokerage": 4.90', 1)
    with pytest.raises(ContractNoteError):
        load_fixture_text(text)
    text = json.dumps(document()).replace('"price": "100.00"', '"price": 100', 1)
    with pytest.raises(ContractNoteError):
        load_fixture_text(text)


def test_duplicate_keys_are_rejected():
    text = json.dumps(document()).replace('"exchange": "NSE",', '"exchange": "NSE", "exchange": "NSE",', 1)
    with pytest.raises(ContractNoteError):
        load_fixture_text(text)


def test_prices_with_decimals_and_isins_are_not_mistaken_for_identifiers():
    fixture = load(document())
    assert fixture.notes[0].trades[0].isin == ISIN


def test_load_fixture_reads_a_file(tmp_path):
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(document()), encoding="utf-8")
    assert load_fixture(path) == load(document())


def test_a_note_dated_before_the_schedule_cannot_be_priced():
    note = buy_note()
    note["trade_date"] = "2024-09-30"
    with pytest.raises(ScheduleNotEffective):
        priced(one_note(note))


# ---- preview tripwire --------------------------------------------------------

PREVIEW = {
    "brokerage": "4.90", "exchange_turnover_charges": "0.21", "sebi_charges": "0.01", "stt": "7.00",
    "stamp_duty": "1.05", "gst": "0.92", "total_brokerage": "14.09",
}


def single_buy_estimate():
    return priced(one_note(buy_note()))


def test_preview_match_is_advisory_never_authoritative():
    result = compare_preview(PREVIEW, single_buy_estimate())
    assert result.authoritative is False
    assert result.kind is ReferenceKind.PREVIEW_ORDER
    assert result.passed is True
    assert result.warnings == ()
    assert result.unmapped == ()


def test_preview_mismatch_raises_warnings_not_errors():
    result = compare_preview({**PREVIEW, "gst": "1.02"}, single_buy_estimate())
    assert result.authoritative is False
    assert result.warnings
    assert "gst" in result.warnings[0]
    assert result.passed is False


def test_preview_off_by_a_paisa_is_tolerated():
    result = compare_preview({**PREVIEW, "gst": "0.93"}, single_buy_estimate())
    assert result.passed is True
    assert result.warnings == ()


def test_preview_unknown_fields_are_reported_not_trusted():
    result = compare_preview({**PREVIEW, "surprise": "1.00"}, single_buy_estimate())
    assert result.unmapped == ("surprise",)


def test_preview_needs_a_single_order_estimate():
    two_orders = buy_note()
    two_orders["trades"].append({"order_ref": "o3", "isin": ISIN, "side": "BUY", "quantity": 10, "price": "100.00"})
    estimate = priced(one_note(two_orders))
    with pytest.raises(InputError):
        compare_preview(PREVIEW, estimate)
