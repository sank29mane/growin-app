"""Schedule loader: rate table, hash pin, strict validation, date lookup, basis."""

from __future__ import annotations

import copy
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from costs.charges import price_trade_day
from costs.core import ScheduleError, ScheduleNotEffective, Side, TradeFill
from costs.schedule import PricingBasis, load_schedule_set

D = Decimal
VERSION = "icici-prime9999-ivalue-nse-cash-2024-10-01.r1"
SCHEDULE_PATH = Path(__file__).resolve().parents[2] / "backend" / "costs" / "schedules" / "icici_nse_cash_charges.json"

# Any change to the committed schedule needs a new version id and a new literal
# here, in the same commit. A rate edit cannot land without a visible test change.
EXPECTED_SCHEDULE_HASH = "aeb1a8c4f58fcf077d9a4e18e3e1b3fd5cd362cc7f228465d2fb5dbceb50a79f"


def raw_document() -> dict:
    return json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))


def write_variant(tmp_path: Path, document: dict, name: str = "variant.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def committed():
    return load_schedule_set().versions[0]


def test_committed_file_loads_one_version_with_expected_scope():
    schedule_set = load_schedule_set()
    assert len(schedule_set.versions) == 1
    version = schedule_set.versions[0]
    assert version.version == VERSION
    assert version.effective_from == date(2024, 10, 1)
    assert version.effective_to is None
    assert (version.workspace, version.exchange, version.segment, version.currency) == (
        "india", "NSE", "cash", "INR",
    )


def test_every_rate_matches_the_rates_table():
    version = committed()
    assert version.brokerage.delivery_rate == D("0.0007")
    assert version.brokerage.delivery_min_per_order == D("0")
    assert version.brokerage.intraday_rate == D("0.0005")
    assert version.brokerage.intraday_cap_per_order == D("20")
    assert version.statutory.stt_delivery_rate == D("0.001")
    assert version.statutory.stt_intraday_sell_rate == D("0.00025")
    assert version.statutory.stamp_delivery_buy_rate == D("0.00015")
    assert version.statutory.stamp_intraday_buy_rate == D("0.00003")
    assert version.statutory.exchange_transaction_rate == D("0.0000297")
    assert version.statutory.sebi_fee_rate == D("0.000001")
    assert version.statutory.ipft_rate == D("0")
    assert version.gst.rate == D("0.18")
    assert version.gst.applies_to == ("brokerage", "exchange_transaction", "sebi_fee", "ipft")
    assert version.gst.base == "rounded_lines"
    assert version.dp.charge_per_debit == D("20")
    assert version.dp.gst_applies is True
    assert version.dp.basis == "per_sell_order"
    assert version.rounding.default_quantum == D("0.01")
    assert dict(version.rounding.line_quantum) == {}
    assert version.account_overhead.amc_annual_ex_gst == D("300")
    assert version.account_overhead.amc_gst_rate == D("0.18")
    assert [fee.item for fee in version.account_overhead.plan_fees] == [
        "prime_9999_one_time_fee", "ivalue_one_time_fee",
    ]
    assert version.excluded_from_delivery_model == ("shares_as_margin_interest",)


def test_schedule_hash_is_pinned():
    assert committed().schedule_hash == EXPECTED_SCHEDULE_HASH


def test_hash_ignores_whitespace_and_key_order(tmp_path):
    document = raw_document()
    version = document["versions"][0]
    reordered = {key: version[key] for key in reversed(list(version))}
    path = write_variant(tmp_path, {"versions": [reordered], "schema": document["schema"]})
    assert load_schedule_set(path).versions[0].schedule_hash == EXPECTED_SCHEDULE_HASH


# ---- strict validation -------------------------------------------------------


def mutate(path_keys, value):
    """Return a function that sets raw_document()['versions'][0][...path_keys] = value."""

    def apply(document: dict) -> None:
        target = document["versions"][0]
        for key in path_keys[:-1]:
            target = target[key]
        target[path_keys[-1]] = value

    return apply


def delete(path_keys):
    def apply(document: dict) -> None:
        target = document["versions"][0]
        for key in path_keys[:-1]:
            target = target[key]
        del target[path_keys[-1]]

    return apply


def top_level_extra(document: dict) -> None:
    document["extra"] = "x"


STRICT_CASES = [
    ("unknown top-level key", top_level_extra, "$.extra"),
    ("unknown version key", mutate(("bogus",), "x"), "versions[0].bogus"),
    ("unknown brokerage key", mutate(("brokerage", "bogus"), "0"), "versions[0].brokerage.bogus"),
    ("missing brokerage key", delete(("brokerage", "delivery_rate")), "versions[0].brokerage.delivery_rate"),
    ("missing version key", delete(("dp",)), "versions[0].dp"),
    ("rate of 1", mutate(("brokerage", "delivery_rate"), "1"), "versions[0].brokerage.delivery_rate"),
    ("negative rate", mutate(("statutory", "stt_delivery_rate"), "-0.0001"), "versions[0].statutory.stt_delivery_rate"),
    ("negative minimum", mutate(("brokerage", "delivery_min_per_order"), "-1"), "versions[0].brokerage.delivery_min_per_order"),
    ("negative dp charge", mutate(("dp", "charge_per_debit"), "-20"), "versions[0].dp.charge_per_debit"),
    ("negative amc", mutate(("account_overhead", "amc_annual_ex_gst"), "-1"), "versions[0].account_overhead.amc_annual_ex_gst"),
    ("workspace uk", mutate(("workspace",), "uk"), "versions[0].workspace"),
    ("currency GBP", mutate(("currency",), "GBP"), "versions[0].currency"),
    ("exchange BSE", mutate(("exchange",), "BSE"), "versions[0].exchange"),
    ("gst applies to stt", mutate(("gst", "applies_to"), ["brokerage", "stt"]), "versions[0].gst.applies_to"),
    ("dp basis per_trade", mutate(("dp", "basis"), "per_trade"), "versions[0].dp.basis"),
    ("gst base nearest", mutate(("gst", "base"), "nearest"), "versions[0].gst.base"),
    ("misspelled quantum key", mutate(("rounding", "line_quantum"), {"brokerge": "0.01"}), "versions[0].rounding.line_quantum.brokerge"),
    ("zero quantum", mutate(("rounding", "line_quantum"), {"stt": "0"}), "versions[0].rounding.line_quantum.stt"),
    ("zero default quantum", mutate(("rounding", "default_quantum"), "0"), "versions[0].rounding.default_quantum"),
    ("unknown overhead key", mutate(("account_overhead", "bogus"), "x"), "versions[0].account_overhead.bogus"),
    ("unknown plan fee key", mutate(("account_overhead", "plan_fees"), [{"item": "a", "amount_ex_gst": None, "gst_rate": None, "treatment": "t", "bogus": 1}]), "plan_fees[0].bogus"),
    ("numeric as JSON int", mutate(("brokerage", "delivery_rate"), 1), "versions[0].brokerage.delivery_rate"),
    ("effective_to before effective_from", mutate(("effective_to",), "2024-09-30"), "versions[0].effective_to"),
]


@pytest.mark.parametrize("label, edit, path_fragment", STRICT_CASES, ids=[case[0] for case in STRICT_CASES])
def test_strict_validation_names_the_key_path(tmp_path, label, edit, path_fragment):
    document = raw_document()
    edit(document)
    with pytest.raises(ScheduleError) as excinfo:
        load_schedule_set(write_variant(tmp_path, document))
    assert path_fragment in str(excinfo.value), str(excinfo.value)


def test_wrong_schema_string_raises(tmp_path):
    document = raw_document()
    document["schema"] = "growin.costs.charge_schedule/2"
    with pytest.raises(ScheduleError):
        load_schedule_set(write_variant(tmp_path, document))


def test_empty_versions_list_raises(tmp_path):
    document = raw_document()
    document["versions"] = []
    with pytest.raises(ScheduleError):
        load_schedule_set(write_variant(tmp_path, document))


# ---- multi-version date lookup ----------------------------------------------


def versions_variant(tmp_path, specs, name="multi.json"):
    """specs: list of (version_id, effective_from, effective_to) built from the committed version."""
    base = raw_document()["versions"][0]
    document = {"schema": raw_document()["schema"], "versions": []}
    for version_id, effective_from, effective_to in specs:
        entry = copy.deepcopy(base)
        entry["version"] = version_id
        entry["effective_from"] = effective_from
        entry["effective_to"] = effective_to
        document["versions"].append(entry)
    return write_variant(tmp_path, document, name)


def test_two_versions_resolve_by_date(tmp_path):
    path = versions_variant(tmp_path, [("A", "2024-10-01", "2025-03-31"), ("B", "2025-04-01", None)])
    schedule_set = load_schedule_set(path)
    assert schedule_set.for_date(date(2025, 3, 31)).version == "A"
    assert schedule_set.for_date(date(2025, 4, 1)).version == "B"
    assert schedule_set.for_date(date(2030, 1, 1)).version == "B"
    with pytest.raises(ScheduleNotEffective):
        schedule_set.for_date(date(2024, 9, 30))


def test_versions_are_sorted_by_effective_from(tmp_path):
    path = versions_variant(tmp_path, [("B", "2025-04-01", None), ("A", "2024-10-01", "2025-03-31")])
    assert [v.version for v in load_schedule_set(path).versions] == ["A", "B"]


def test_gap_between_versions_fails_closed(tmp_path):
    path = versions_variant(tmp_path, [("A", "2024-10-01", "2025-02-28"), ("B", "2025-04-01", None)])
    schedule_set = load_schedule_set(path)
    with pytest.raises(ScheduleNotEffective):
        schedule_set.for_date(date(2025, 3, 15))


def test_closed_final_version_fails_closed_after_its_end(tmp_path):
    path = versions_variant(tmp_path, [("A", "2024-10-01", "2025-03-31")])
    with pytest.raises(ScheduleNotEffective):
        load_schedule_set(path).for_date(date(2025, 4, 1))


def test_overlapping_ranges_raise_at_load(tmp_path):
    path = versions_variant(tmp_path, [("A", "2024-10-01", "2025-04-01"), ("B", "2025-04-01", None)])
    with pytest.raises(ScheduleError, match="overlap"):
        load_schedule_set(path)


def test_duplicate_version_id_raises_at_load(tmp_path):
    path = versions_variant(tmp_path, [("A", "2024-10-01", "2025-03-31"), ("A", "2025-04-01", None)])
    with pytest.raises(ScheduleError, match="duplicate"):
        load_schedule_set(path)


def test_open_ended_non_latest_version_raises_at_load(tmp_path):
    path = versions_variant(tmp_path, [("A", "2024-10-01", None), ("B", "2025-04-01", None)])
    with pytest.raises(ScheduleError, match="open-ended"):
        load_schedule_set(path)


def test_adding_a_version_never_changes_an_existing_hash(tmp_path):
    single = load_schedule_set(versions_variant(tmp_path, [("A", "2024-10-01", "2025-03-31")], "one.json"))
    double = load_schedule_set(
        versions_variant(tmp_path, [("A", "2024-10-01", "2025-03-31"), ("B", "2025-04-01", None)], "two.json")
    )
    assert single.get("A").schedule_hash == double.get("A").schedule_hash
    assert double.get("A").schedule_hash != double.get("B").schedule_hash


def test_get_unknown_version_raises():
    with pytest.raises(ScheduleError):
        load_schedule_set().get("no-such-version")


# ---- pricing basis -----------------------------------------------------------


def one_sell(trade_date: date) -> list[TradeFill]:
    return [TradeFill("s1", "INE0TEST0001", "NSE", Side.SELL, 70, D("100.00"), trade_date)]


def test_trade_date_basis_before_first_version_raises():
    with pytest.raises(ScheduleNotEffective):
        load_schedule_set().resolve(date(2022, 6, 1), PricingBasis.trade_date())


def test_pinned_basis_resolves_outside_coverage_and_is_recorded():
    schedule_set = load_schedule_set()
    basis = PricingBasis.pinned(VERSION)
    schedule = schedule_set.resolve(date(2022, 6, 1), basis)
    assert schedule.version == VERSION
    estimate = price_trade_day(
        one_sell(date(2022, 6, 1)), schedule, workspace="india", currency="INR", pricing_basis=basis
    )
    assert estimate.pricing_basis.mode == "pinned"
    assert estimate.pricing_basis.pinned_version == VERSION
    assert estimate.schedule_version == VERSION
    assert estimate.total == D("36.64")


def test_pinned_basis_changes_the_estimate_hash():
    schedule_set = load_schedule_set()
    fills = one_sell(date(2026, 10, 6))
    schedule = schedule_set.for_date(date(2026, 10, 6))
    by_date = price_trade_day(fills, schedule, workspace="india", currency="INR", pricing_basis=PricingBasis.trade_date())
    pinned = price_trade_day(fills, schedule, workspace="india", currency="INR", pricing_basis=PricingBasis.pinned(VERSION))
    assert by_date.total == pinned.total
    assert by_date.estimate_hash != pinned.estimate_hash


def test_pinned_to_unknown_version_raises():
    with pytest.raises(ScheduleError):
        load_schedule_set().resolve(date(2026, 10, 6), PricingBasis.pinned("no-such-version"))


def test_price_trade_day_rejects_a_trade_date_basis_outside_coverage():
    schedule = committed()
    with pytest.raises(ScheduleNotEffective):
        price_trade_day(
            one_sell(date(2022, 6, 1)), schedule, workspace="india", currency="INR",
            pricing_basis=PricingBasis.trade_date(),
        )


def test_hash_literal_looks_like_sha256():
    assert re.fullmatch(r"[0-9a-f]{64}", EXPECTED_SCHEDULE_HASH)
