"""Operator golden test for COST-01 (ROADMAP criterion 1).

Runs against the operator's redacted delivery buy and sell contract notes when
that fixture exists in a gitignored local folder, and skips with a clear
message when it does not. No contract-note number lives in this file or in
the repository. The harness logic itself is exercised on a SYNTHETIC fixture
so a skip never hides a broken harness.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest

from costs.contract_note import (
    FIXTURE_RELATIVE_PATH,
    REQUIRED_BUY_LINES,
    REQUIRED_LINES,
    compare_note,
    load_fixture,
    price_note,
)
from costs.core import LINE_ORDER, ScheduleNotEffective
from costs.schedule import PricingBasis, load_schedule_set

THIS_CHECKOUT = Path(__file__).resolve().parents[2]


def main_checkout_root() -> Path | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=THIS_CHECKOUT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    return Path(completed.stdout.strip()).parent


def candidate_paths() -> list[Path]:
    paths = [THIS_CHECKOUT / FIXTURE_RELATIVE_PATH]
    main_root = main_checkout_root()
    if main_root is not None:
        paths.append(main_root / FIXTURE_RELATIVE_PATH)
    return paths


def find_fixture() -> Path | None:
    for path in candidate_paths():
        if path.is_file():
            return path
    return None


def check_golden(path: Path) -> None:
    """Assert every golden property for the fixture at ``path``."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    notes_raw = raw["notes"]
    # D-09 as amended (operator-approved 2026-10-17): across its notes the fixture must hold
    # at least one BUY and at least one SELL trade. A mixed note is allowed; so are separate
    # buy-only and sell-only notes. Neither side may be missing.
    sides = {trade["side"] for note in notes_raw for trade in note["trades"]}
    assert "BUY" in sides, "the fixture needs at least one BUY trade in one of its notes"
    assert "SELL" in sides, "the fixture needs at least one SELL trade in one of its notes"
    for note in notes_raw:
        for name in REQUIRED_LINES:
            assert name in note["lines"], f"note {note['label']!r} does not print required line {name!r}"
        if any(trade["side"] == "BUY" for trade in note["trades"]):
            for name in REQUIRED_BUY_LINES:
                assert name in note["lines"], f"buy note {note['label']!r} does not print {name!r}"

    fixture = load_fixture(path)
    schedules = load_schedule_set()
    for note in fixture.notes:
        try:
            estimate = price_note(note, schedules, workspace="india", currency="INR")
        except ScheduleNotEffective as exc:
            pytest.fail(
                f"note {note.label!r} is dated before the schedule's first effective date; "
                f"supply a note dated on or after it ({exc})"
            )
        result = compare_note(note, estimate, schedules=schedules)
        table = "\n".join(
            f"  {row.line}: note={row.reference} model={row.model} delta={row.delta}"
            f"{'' if row.within_tolerance else '  <-- outside tolerance'}"
            for row in result.rows
        )
        assert result.passed, (
            f"note {note.label!r} does not match the model within 0.05 per line\n{table}\n"
            f"missing_required={result.missing_required} unmapped={result.unmapped}"
        )


def test_operator_golden_delivery_round_trip():
    path = find_fixture()
    if path is None:
        searched = " and ".join(str(p) for p in candidate_paths())
        pytest.skip(f"operator golden fixture not supplied; looked in {searched}")
    check_golden(path)


MATCHED_PREFIX = "matched to the operator's redacted contract notes within 0.05 per printed line on "
R1_VERSION = "icici-prime9999-ivalue-nse-cash-2024-10-01.r1"
DP_ATTESTATION = "dp amount matches an operator-observed ledger debit (attested "
ALWAYS_UNCONFIRMED ="intraday brokerage and same-day rules unconfirmed (no same-day note)"


def expected_unconfirmed_items(fixture) -> list[str]:
    """Derive, from the fixture alone, every item r2's status must name as unconfirmed."""
    items: list[str] = []
    if not any(note.brokerage_billing == "gross" for note in fixture.notes):
        items.append("delivery brokerage rate unconfirmed (prepaid credit)")
    printed: set[str] = set()
    for note in fixture.notes:
        printed.update(note.lines)
        printed.update(note.dp_lines or {})
    items.extend(f"{line} unconfirmed (not printed)" for line in LINE_ORDER if line not in printed)
    distinguishes_dp_basis = False
    for note in fixture.notes:
        sell_orders: dict[str, set[str]] = {}
        for trade in note.trades:
            if trade.side.value == "SELL":
                sell_orders.setdefault(trade.isin, set()).add(trade.order_ref)
        if any(len(orders) >= 2 for orders in sell_orders.values()):
            distinguishes_dp_basis = True
    if not distinguishes_dp_basis:
        items.append("dp basis unconfirmed (one sell order per ISIN)")
    items.append(ALWAYS_UNCONFIRMED)
    return items


def check_confirmed_version_status(path: Path) -> None:
    fixture = load_fixture(path)
    schedules = load_schedule_set()
    versions = {schedules.resolve(note.trade_date, PricingBasis.trade_date()).version for note in fixture.notes}
    assert len(versions) == 1, f"every note must resolve to one schedule version, got {sorted(versions)}"
    confirmed = schedules.get(next(iter(versions)))
    assert confirmed.version != R1_VERSION
    assert confirmed.status.startswith(MATCHED_PREFIX), confirmed.status
    date_text = confirmed.status[len(MATCHED_PREFIX):].split(";", 1)[0]
    assert date.fromisoformat(date_text)
    items = expected_unconfirmed_items(fixture)
    if not any(note.dp_lines for note in fixture.notes):
        # No document shows a DP debit: the amount is recorded as operator-attested, not confirmed.
        assert DP_ATTESTATION in confirmed.status
    assert confirmed.status.endswith("; unconfirmed: " + "; ".join(items)), confirmed.status
    for item in items:
        assert item in confirmed.status, f"status does not name {item!r}"

    r1 = schedules.get(R1_VERSION)
    assert r1.effective_to == date(2024, 10, 1)
    assert "superseded by" in r1.status
    assert schedules.resolve(date(2022, 6, 1), PricingBasis.pinned(R1_VERSION)).version == R1_VERSION


def test_confirmed_version_status_names_unconfirmed_items():
    path = find_fixture()
    if path is None:
        searched = " and ".join(str(p) for p in candidate_paths())
        pytest.skip(f"operator golden fixture not supplied; looked in {searched}")
    check_confirmed_version_status(path)


# ---- harness self-test on a SYNTHETIC fixture (never the operator's) ---------


def synthetic_document() -> dict:
    isin = "INE0TEST0001"
    return {
        "schema": "growin.costs.contract_note_fixture/1",
        "redaction_attested": True,
        "provenance": "SYNTHETIC: computed from published rates for tests; not a contract note",
        "notes": [
            {
                "label": "synthetic-buy", "trade_date": "2026-10-05", "exchange": "NSE", "segment": "cash",
                "brokerage_billing": "gross", "dp_source": "not_applicable",
                "trades": [{"order_ref": "o1", "isin": isin, "side": "BUY", "quantity": 70, "price": "100.00"}],
                "lines": {"brokerage": "4.90", "exchange_transaction": "0.21", "sebi_fee": "0.01",
                          "gst": "0.92", "stt": "7.00", "stamp_duty": "1.00"},
                "dp_lines": None, "note_total_charges": None, "unmapped_lines": [],
            },
            {
                "label": "synthetic-sell", "trade_date": "2026-10-06", "exchange": "NSE", "segment": "cash",
                "brokerage_billing": "gross", "dp_source": "contract_note",
                "trades": [{"order_ref": "o2", "isin": isin, "side": "SELL", "quantity": 70, "price": "100.00"}],
                "lines": {"brokerage": "4.90", "exchange_transaction": "0.21", "sebi_fee": "0.01",
                          "gst": "0.92", "stt": "7.00"},
                "dp_lines": {"dp_charge": "20.00", "dp_gst": "3.60"}, "note_total_charges": None,
                "unmapped_lines": [],
            },
        ],
    }


def write(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "golden.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_harness_passes_a_matching_synthetic_fixture(tmp_path):
    check_golden(write(tmp_path, synthetic_document()))


def test_harness_fails_when_a_line_is_off(tmp_path):
    document = synthetic_document()
    document["notes"][0]["lines"]["stt"] = "7.10"
    with pytest.raises(AssertionError, match="stt"):
        check_golden(write(tmp_path, document))


def mixed_synthetic_document() -> dict:
    """One note holding a BUY in one ISIN and a SELL in another (no same-stock pair)."""
    return {
        "schema": "growin.costs.contract_note_fixture/1",
        "redaction_attested": True,
        "provenance": "SYNTHETIC: computed from published rates for tests; not a contract note",
        "notes": [
            {
                "label": "synthetic-mixed", "trade_date": "2026-10-05", "exchange": "NSE", "segment": "cash",
                "brokerage_billing": "gross", "dp_source": "not_supplied",
                "trades": [
                    {"order_ref": "o1", "isin": "INE0TEST0001", "side": "BUY", "quantity": 70, "price": "100.00"},
                    {"order_ref": "o2", "isin": "INE0TEST0002", "side": "SELL", "quantity": 70, "price": "100.00"},
                ],
                "lines": {"brokerage": "9.80", "exchange_transaction": "0.43", "sebi_fee": "0.01",
                          "gst": "1.84", "stt": "14.00", "stamp_duty": "1.00"},
                "dp_lines": None, "note_total_charges": None, "unmapped_lines": [],
            },
        ],
    }


def test_harness_passes_a_mixed_buy_and_sell_note(tmp_path):
    check_golden(write(tmp_path, mixed_synthetic_document()))


def test_harness_needs_a_sell_somewhere(tmp_path):
    document = synthetic_document()
    document["notes"] = document["notes"][:1]
    with pytest.raises(AssertionError, match="SELL"):
        check_golden(write(tmp_path, document))


def test_harness_needs_a_buy_somewhere(tmp_path):
    document = synthetic_document()
    document["notes"] = document["notes"][1:]
    with pytest.raises(AssertionError, match="BUY"):
        check_golden(write(tmp_path, document))


def test_harness_rejects_a_mixed_note_that_lacks_the_buy_side_required_line(tmp_path):
    document = mixed_synthetic_document()
    del document["notes"][0]["lines"]["stamp_duty"]
    with pytest.raises(AssertionError, match="stamp_duty"):
        check_golden(write(tmp_path, document))


def test_status_items_follow_the_fixture(tmp_path):
    gross = load_fixture(write(tmp_path, mixed_synthetic_document()))
    assert expected_unconfirmed_items(gross) == [
        "ipft unconfirmed (not printed)",
        "dp_charge unconfirmed (not printed)",
        "dp_gst unconfirmed (not printed)",
        "dp basis unconfirmed (one sell order per ISIN)",
        ALWAYS_UNCONFIRMED,
    ]
    document = mixed_synthetic_document()
    document["notes"][0]["brokerage_billing"] = "prepaid_credit_applied"
    document["notes"][0]["trades"].append(
        {"order_ref": "o3", "isin": "INE0TEST0002", "side": "SELL", "quantity": 1, "price": "100.00"}
    )
    credited = load_fixture(write(tmp_path, document))
    items = expected_unconfirmed_items(credited)
    assert items[0] == "delivery brokerage rate unconfirmed (prepaid credit)"
    assert "dp basis unconfirmed (one sell order per ISIN)" not in items
    assert items[-1] == ALWAYS_UNCONFIRMED


def test_harness_reports_a_missing_required_line_by_name(tmp_path):
    document = synthetic_document()
    del document["notes"][0]["lines"]["stamp_duty"]
    with pytest.raises(AssertionError, match="stamp_duty"):
        check_golden(write(tmp_path, document))


def test_both_search_locations_are_named_in_the_skip_message():
    paths = candidate_paths()
    assert paths[0] == THIS_CHECKOUT / FIXTURE_RELATIVE_PATH
    assert len(paths) == 2 or main_checkout_root() is None
