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
R1 = "icici-prime9999-ivalue-nse-cash-2024-10-01.r1"
R2 = "icici-prime9999-ivalue-nse-cash-2024-10-02.r2"
SCHEDULE_PATH = Path(__file__).resolve().parents[2] / "backend" / "costs" / "schedules" / "icici_nse_cash_charges.json"

# Any change to the committed schedule needs a new version id and a new literal
# here, in the same commit. A rate edit cannot land without a visible test change.
# r1 changed hash once, when its window was closed at 2024-10-01 (no recorded run
# used the open-ended r1); r1's rates are untouched.
EXPECTED_SCHEDULE_HASHES = {
    R1: "a3162a825532ff3da9da3b71b0f9de0ed44b52790ee7254f75400ae966c3f7cf",
    R2: "681910bf1f587cc1a29fb0525ddc3e62b1643a047f362782e3c3dc48fb6a334f",
}
MATCHED_PREFIX = "matched to the operator's redacted contract notes within 0.05 per printed line on "


def raw_document() -> dict:
    return json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))


def write_variant(tmp_path: Path, document: dict, name: str = "variant.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def committed(version: str = R1):
    return load_schedule_set().get(version)


def test_committed_file_loads_two_versions_with_expected_scope():
    schedule_set = load_schedule_set()
    assert [v.version for v in schedule_set.versions] == [R1, R2]
    r1, r2 = schedule_set.versions
    assert (r1.effective_from, r1.effective_to) == (date(2024, 10, 1), date(2024, 10, 1))
    assert (r2.effective_from, r2.effective_to) == (date(2024, 10, 2), None)
    for version in (r1, r2):
        assert (version.workspace, version.exchange, version.segment, version.currency) == (
            "india", "NSE", "cash", "INR",
        )


def assert_shared_rates(version):
    assert version.brokerage.delivery_rate == D("0.0007")
    assert version.brokerage.delivery_min_per_order == D("0")
    assert version.brokerage.intraday_rate == D("0.0005")
    assert version.brokerage.intraday_cap_per_order == D("20")
    assert version.statutory.stt_delivery_rate == D("0.001")
    assert version.statutory.stt_intraday_sell_rate == D("0.00025")
    assert version.statutory.stamp_delivery_buy_rate == D("0.00015")
    assert version.statutory.stamp_intraday_buy_rate == D("0.00003")
    assert version.statutory.sebi_fee_rate == D("0.000001")
    assert version.statutory.ipft_rate == D("0")
    assert version.gst.rate == D("0.18")
    assert version.gst.applies_to == ("brokerage", "exchange_transaction", "sebi_fee", "ipft")
    assert version.dp.charge_per_debit == D("20")
    assert version.dp.gst_applies is True
    assert version.dp.basis == "per_sell_order"
    assert version.rounding.default_quantum == D("0.01")
    assert version.account_overhead.amc_annual_ex_gst == D("300")
    assert version.account_overhead.amc_gst_rate == D("0.18")
    assert [fee.item for fee in version.account_overhead.plan_fees] == [
        "prime_9999_one_time_fee", "ivalue_one_time_fee",
    ]
    assert version.excluded_from_delivery_model == ("shares_as_margin_interest",)


def test_r1_rates_are_the_published_rates_table_untouched():
    version = committed(R1)
    assert_shared_rates(version)
    assert version.statutory.exchange_transaction_rate == D("0.0000297")
    assert version.gst.base == "rounded_lines"
    assert dict(version.rounding.line_quantum) == {}


def test_r2_rates_differ_from_r1_only_by_the_three_calibrations():
    version = committed(R2)
    assert_shared_rates(version)
    # exchange_transaction line: printed per-ISIN rows and the IPFT footnote prove the combined rate.
    assert version.statutory.exchange_transaction_rate == D("0.0000307")
    # gst line: the printed charges-GST component matches the unrounded exchange and SEBI base.
    assert version.gst.base == "unrounded_lines"
    # stt and stamp_duty lines: printed at the note total, rounded to the whole rupee.
    assert dict(version.rounding.line_quantum) == {"stt": D("1"), "stamp_duty": D("1")}


def test_schedule_hashes_are_pinned():
    for version, expected in EXPECTED_SCHEDULE_HASHES.items():
        assert committed(version).schedule_hash == expected, version


def test_hash_ignores_whitespace_and_key_order(tmp_path):
    document = raw_document()
    reordered_versions = [{key: item[key] for key in reversed(list(item))} for item in document["versions"]]
    path = write_variant(tmp_path, {"versions": reordered_versions, "schema": document["schema"]})
    loaded = load_schedule_set(path)
    for version, expected in EXPECTED_SCHEDULE_HASHES.items():
        assert loaded.get(version).schedule_hash == expected


def test_r2_status_is_matched_and_names_the_unconfirmed_items():
    status = committed(R2).status
    assert status.startswith(MATCHED_PREFIX)
    date.fromisoformat(status[len(MATCHED_PREFIX):].split(";", 1)[0])
    assert "; unconfirmed: " in status
    assert status.endswith("intraday brokerage and same-day rules unconfirmed (no same-day note)")
    assert "dp basis unconfirmed" in status
    # The DP amount is operator-attested (no document), never marked confirmed.
    assert "dp amount matches an operator-observed ledger debit (attested " in status
    assert "no document)" in status
    assert "unconfirmed (not printed)" in status


def test_r1_is_closed_superseded_and_still_resolvable_when_pinned():
    r1 = committed(R1)
    assert r1.effective_to == date(2024, 10, 1)
    assert f"superseded by {R2} from 2024-10-02" in r1.status
    assert "kept for pinned replay" in r1.status
    resolved = load_schedule_set().resolve(date(2026, 10, 6), PricingBasis.pinned(R1))
    assert resolved.version == R1


def test_date_lookup_across_the_r1_r2_boundary():
    schedule_set = load_schedule_set()
    assert schedule_set.for_date(date(2024, 10, 1)).version == R1
    assert schedule_set.for_date(date(2024, 10, 2)).version == R2
    assert schedule_set.for_date(date(2026, 10, 6)).version == R2
    with pytest.raises(ScheduleNotEffective):
        schedule_set.for_date(date(2024, 9, 30))


def test_pinned_r1_replay_prices_with_the_r1_rates_after_the_split():
    fills = [TradeFill("b", "INE0TEST0001", "NSE", Side.BUY, 70, D("100.00"), date(2026, 10, 5))]
    schedule_set = load_schedule_set()
    pinned = price_trade_day(
        fills, schedule_set.get(R1), workspace="india", currency="INR", pricing_basis=PricingBasis.pinned(R1)
    )
    current = price_trade_day(
        fills, schedule_set.for_date(date(2026, 10, 5)), workspace="india", currency="INR",
        pricing_basis=PricingBasis.trade_date(),
    )
    assert pinned.schedule_version == R1 and current.schedule_version == R2
    assert pinned.line("stamp_duty") == D("1.05") and current.line("stamp_duty") == D("1")


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
    basis = PricingBasis.pinned(R1)
    schedule = schedule_set.resolve(date(2022, 6, 1), basis)
    assert schedule.version == R1
    estimate = price_trade_day(
        one_sell(date(2022, 6, 1)), schedule, workspace="india", currency="INR", pricing_basis=basis
    )
    assert estimate.pricing_basis.mode == "pinned"
    assert estimate.pricing_basis.pinned_version == R1
    assert estimate.schedule_version == R1
    assert estimate.total == D("36.64")


def test_pinned_basis_changes_the_estimate_hash():
    schedule_set = load_schedule_set()
    fills = one_sell(date(2026, 10, 6))
    schedule = schedule_set.for_date(date(2026, 10, 6))
    by_date = price_trade_day(fills, schedule, workspace="india", currency="INR", pricing_basis=PricingBasis.trade_date())
    pinned = price_trade_day(fills, schedule, workspace="india", currency="INR", pricing_basis=PricingBasis.pinned(R2))
    assert by_date.total == pinned.total
    assert by_date.estimate_hash != pinned.estimate_hash


def test_pinned_to_unknown_version_raises():
    with pytest.raises(ScheduleError):
        load_schedule_set().resolve(date(2026, 10, 6), PricingBasis.pinned("no-such-version"))


def test_price_trade_day_rejects_a_trade_date_basis_outside_coverage():
    schedule = committed(R2)
    with pytest.raises(ScheduleNotEffective):
        price_trade_day(
            one_sell(date(2022, 6, 1)), schedule, workspace="india", currency="INR",
            pricing_basis=PricingBasis.trade_date(),
        )


def test_hash_literals_look_like_sha256():
    for literal in EXPECTED_SCHEDULE_HASHES.values():
        assert re.fullmatch(r"[0-9a-f]{64}", literal)
