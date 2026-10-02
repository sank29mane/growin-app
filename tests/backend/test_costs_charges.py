"""D-09 same-day classification, DP basis, rounding overrides and GST base.

Every fill here is SYNTHETIC. Expected lines were computed with Decimal
arithmetic from the published rates, not copied from a contract note.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from costs import charges
from costs.charges import (
    PROVISIONAL_SAME_DAY_PARTIAL_STATUTORY,
    SAME_DAY_PARTIAL_STATUTORY_STATUS,
    classify_same_day_brokerage,
    price_trade_day,
)
from costs.core import LINE_ORDER, CostModelError, Side, TradeFill, canonical_json, seal
from costs.schedule import PricingBasis, load_schedule_set

D = Decimal
ISIN = "INE0TEST0001"
DAY = date(2026, 10, 6)
SCHEDULE_PATH = Path(__file__).resolve().parents[2] / "backend" / "costs" / "schedules" / "icici_nse_cash_charges.json"
CHARGES_PATH = Path(__file__).resolve().parents[2] / "backend" / "costs" / "charges.py"


def fill(order_id, side, quantity, price, *, isin=ISIN, exchange="NSE", day=DAY):
    return TradeFill(order_id, isin, exchange, side, quantity, D(price), day)


def committed_schedule():
    return load_schedule_set().versions[0]


def variant_schedule(tmp_path, edit):
    document = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    edit(document["versions"][0])
    path = tmp_path / "variant.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_schedule_set(path).versions[0]


def price(fills, schedule=None):
    return price_trade_day(
        fills, schedule or committed_schedule(), workspace="india", currency="INR",
        pricing_basis=PricingBasis.trade_date(),
    )


def expected(*amounts):
    return tuple(D(a) for a in amounts)


def lines_of(estimate):
    return tuple(estimate.line(name) for name in LINE_ORDER)


def classes(estimate):
    return {row.order_id: (row.classification, row.amount) for row in estimate.order_brokerage}


def test_next_session_sell_stays_delivery():
    estimate = price([fill("sell", Side.SELL, 70, "100.00")])
    assert lines_of(estimate) == expected("4.90", "0.21", "0.01", "0.00", "0.92", "7.00", "0.00", "20.00", "3.60")
    assert estimate.total == D("36.64")
    assert classes(estimate) == {"sell": ("delivery", D("4.90"))}
    assert estimate.provisional_flags == ()


def test_net_zero_same_day_is_intraday():
    estimate = price([fill("nz-b", Side.BUY, 70, "100.00"), fill("nz-s", Side.SELL, 70, "101.00")])
    assert classes(estimate) == {"nz-b": ("intraday", D("3.50")), "nz-s": ("intraday", D("3.54"))}
    assert estimate.line("brokerage") == D("7.04")
    assert lines_of(estimate) == expected("7.04", "0.42", "0.01", "0.00", "1.34", "1.77", "0.21", "0.00", "0.00")
    assert estimate.total == D("10.79")
    assert estimate.dp_debits == 0
    assert estimate.provisional_flags == ()


def test_intraday_brokerage_is_capped_at_twenty_rupees_per_order():
    estimate = price([fill("c-b", Side.BUY, 500, "100.00"), fill("c-s", Side.SELL, 500, "100.00")])
    assert classes(estimate) == {"c-b": ("intraday", D("20.00")), "c-s": ("intraday", D("20.00"))}
    assert lines_of(estimate) == expected("40.00", "2.97", "0.10", "0.00", "7.75", "12.50", "1.50", "0.00", "0.00")
    assert estimate.total == D("64.82")


PARTIAL_BUY = [fill("pb-b", Side.BUY, 100, "100.00"), fill("pb-s", Side.SELL, 60, "101.00")]
PARTIAL_SELL = [fill("ps-s", Side.SELL, 100, "100.00"), fill("ps-b", Side.BUY, 60, "99.00")]


def test_partial_square_off_buy_leg_larger():
    estimate = price(PARTIAL_BUY)
    assert classes(estimate) == {
        "pb-b": ("delivery", D("7.00")),
        "pb-s": ("squared_off_no_brokerage", D("0.00")),
    }
    assert lines_of(estimate) == expected("7.00", "0.48", "0.02", "0.00", "1.35", "5.52", "0.78", "0.00", "0.00")
    assert estimate.total == D("15.15")


def test_partial_square_off_sell_leg_larger():
    estimate = price(PARTIAL_SELL)
    assert classes(estimate) == {
        "ps-s": ("delivery", D("7.00")),
        "ps-b": ("squared_off_no_brokerage", D("0.00")),
    }
    assert lines_of(estimate) == expected("7.00", "0.47", "0.02", "0.00", "1.35", "5.50", "0.18", "20.00", "3.60")
    assert estimate.total == D("38.12")


def test_partial_square_off_statutory_is_provisional():
    for fills in (PARTIAL_BUY, PARTIAL_SELL):
        estimate = price(fills)
        assert estimate.provisional_flags == (PROVISIONAL_SAME_DAY_PARTIAL_STATUTORY,)
        assert PROVISIONAL_SAME_DAY_PARTIAL_STATUTORY == "same_day_partial_statutory_split"
        assert SAME_DAY_PARTIAL_STATUTORY_STATUS in canonical_json(estimate)
        assert "provisional, unvalidated" in SAME_DAY_PARTIAL_STATUTORY_STATUS
    validated = [
        [fill("sell", Side.SELL, 70, "100.00")],
        [fill("buy", Side.BUY, 70, "100.00")],
        [fill("nz-b", Side.BUY, 70, "100.00"), fill("nz-s", Side.SELL, 70, "101.00")],
    ]
    for fills in validated:
        estimate = price(fills)
        assert estimate.provisional_flags == ()
        assert SAME_DAY_PARTIAL_STATUTORY_STATUS not in canonical_json(estimate)


def test_changing_only_provisional_flags_changes_the_estimate_hash():
    estimate = price(PARTIAL_BUY)
    assert seal(estimate, "estimate_hash").estimate_hash == estimate.estimate_hash
    stripped = dataclasses.replace(estimate, provisional_flags=())
    assert seal(stripped, "estimate_hash").estimate_hash != estimate.estimate_hash


def test_brokerage_classification_is_separate_path(monkeypatch):
    estimate_rows = {}
    for fills in (PARTIAL_BUY, PARTIAL_SELL):
        estimate_rows[fills[0].order_id] = price(fills).order_brokerage

    def boom(*args, **kwargs):
        raise AssertionError("classify_same_day_brokerage must not call the statutory split")

    monkeypatch.setattr(charges, "split_same_day_statutory", boom)
    for fills in (PARTIAL_BUY, PARTIAL_SELL):
        rows = classify_same_day_brokerage(fills, committed_schedule())
        assert rows == estimate_rows[fills[0].order_id]


def _calls_in(function_name: str) -> set[str]:
    tree = ast.parse(CHARGES_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return {
                call.func.id
                for call in ast.walk(node)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            }
    raise AssertionError(f"{function_name} not found in charges.py")


def test_classification_and_statutory_split_never_call_each_other():
    assert "split_same_day_statutory" not in _calls_in("classify_same_day_brokerage")
    assert "classify_same_day_brokerage" not in _calls_in("split_same_day_statutory")


def test_dp_basis_per_sell_order_is_the_default():
    estimate = price([fill("d1", Side.SELL, 35, "100.00"), fill("d2", Side.SELL, 35, "100.00")])
    assert estimate.dp_debits == 2
    assert estimate.line("dp_charge") == D("40.00")
    assert estimate.line("dp_gst") == D("7.20")
    assert estimate.total == D("60.24")


def test_dp_basis_per_isin_per_day(tmp_path):
    schedule = variant_schedule(tmp_path, lambda v: v["dp"].update(basis="per_isin_per_day"))
    estimate = price([fill("d1", Side.SELL, 35, "100.00"), fill("d2", Side.SELL, 35, "100.00")], schedule)
    assert estimate.dp_debits == 1
    assert estimate.line("dp_charge") == D("20.00")
    assert estimate.line("dp_gst") == D("3.60")
    assert estimate.total == D("36.64")


def test_rounding_quantum_override(tmp_path):
    buy = [fill("b", Side.BUY, 76, "100.00")]
    assert price(buy).line("stt") == D("7.60")
    schedule = variant_schedule(tmp_path, lambda v: v["rounding"].update(line_quantum={"stt": "1"}))
    assert price(buy, schedule).line("stt") == D("8")


def test_gst_base_rounded_versus_unrounded(tmp_path):
    buy = [fill("b", Side.BUY, 27, "100.00")]
    assert price(buy).line("gst") == D("0.35")
    schedule = variant_schedule(tmp_path, lambda v: v["gst"].update(base="unrounded_lines"))
    assert price(buy, schedule).line("gst") == D("0.36")


def test_errors():
    with pytest.raises(CostModelError):
        price([])
    with pytest.raises(CostModelError):
        price([fill("a", Side.BUY, 1, "100.00"), fill("b", Side.BUY, 1, "100.00", day=date(2026, 10, 7))])
    with pytest.raises(CostModelError):
        price([fill("a", Side.BUY, 1, "100.00"), fill("b", Side.BUY, 1, "100.00", exchange="BSE")])


def test_multiple_isins_on_one_day_are_classified_independently():
    other = "INE0TEST0002"
    fills = [
        fill("a-b", Side.BUY, 70, "100.00"),
        fill("a-s", Side.SELL, 70, "100.00"),
        fill("o-s", Side.SELL, 10, "100.00", isin=other),
    ]
    estimate = price(fills)
    assert classes(estimate)["a-b"][0] == "intraday"
    assert classes(estimate)["o-s"][0] == "delivery"
    assert estimate.dp_debits == 1
