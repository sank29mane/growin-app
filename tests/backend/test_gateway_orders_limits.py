"""VM limits file and rule evaluator (Phase 63-01, Task 2).

The decision cases live in fixtures/relay_orders/limits_vectors.json and are
read here, not duplicated inline, so the Mac suite (63-02) runs the same ones.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from backend.costs.ticks import InstrumentClass, resolve_nse_cash_tick  # noqa: E402
from backend.private_config.loader import _check_india_limits  # noqa: E402
from backend.private_config.schemas import IndiaLimits  # noqa: E402
from backend.private_config.loader import PrivateConfigError  # noqa: E402
from gateway_vm.orders import limits as vm  # noqa: E402
from gateway_vm.orders.intent import parse_intent  # noqa: E402
from gateway_vm.orders.pipeline import RuleGuard  # noqa: E402

FIXTURES = ROOT / "tests" / "backend" / "fixtures" / "relay_orders"
LV = json.loads((FIXTURES / "limits_vectors.json").read_text(encoding="utf-8"))
SV = json.loads((FIXTURES / "signing_vectors.json").read_text(encoding="utf-8"))
BASE_INTENT = dict(SV["rows"][0]["payload"]["intent"])
UID = os.getuid()


def write_limits(tmp_path: Path, fields, *, mode: int = 0o644, name: str = "limits.json") -> Path:
    path = tmp_path / name
    path.write_text(fields if isinstance(fields, str) else json.dumps(fields))
    path.chmod(mode)
    return path


def good() -> dict:
    return dict(LV["limits"])


# ---------------------------------------------------------------- the loader


def test_loader_accepts_the_vector_limits_and_hash_matches(tmp_path):
    limits = vm.load_limits(write_limits(tmp_path, good()), expected_owner_uid=UID)
    assert limits.sha256 == LV["limits_sha256"] == SV["limits_sha256"]
    assert limits.capital_cap == Decimal("50000")
    assert limits.fat_finger_collar == Decimal("0.02")


def test_limits_sha256_is_sha256_of_canonical_json_of_the_nine_keys():
    canonical = json.dumps(good(), sort_keys=True, separators=(",", ":")).encode()
    assert vm.limits_sha256(good()) == hashlib.sha256(canonical).hexdigest()


def test_limits_sha256_changes_with_any_value():
    changed = good()
    changed["capital_cap"] = "50001"
    assert vm.limits_sha256(changed) != LV["limits_sha256"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda f: f.pop("fat_finger_collar"),
        lambda f: f.update(extra="1"),
        lambda f: f.update(capital_cap=50000),
        lambda f: f.update(capital_cap="5e4"),
        lambda f: f.update(capital_cap="-1"),
        lambda f: f.update(capital_cap="0"),
        lambda f: f.update(capital_cap=" 50000"),
        lambda f: f.update(per_position_cap="50001"),  # above capital_cap
        lambda f: f.update(per_position_cap="0"),
        lambda f: f.update(drawdown_halt="0.08"),
        lambda f: f.update(drawdown_halt="0"),
        lambda f: f.update(drawdown_flatten="-0.08"),  # not below halt
        lambda f: f.update(drawdown_flatten="-1"),
        lambda f: f.update(position_stop="0"),
        lambda f: f.update(position_stop="-1"),
        lambda f: f.update(fat_finger_collar="0"),
        lambda f: f.update(fat_finger_collar="1"),
        lambda f: f.update(fat_finger_collar="1.5"),
        lambda f: f.update(currency="GBP"),
        lambda f: f.update(workspace="uk"),
        lambda f: f.update(schema_version=2),
        lambda f: f.update(schema_version=True),
        lambda f: f.update(schema_version="1"),
    ],
)
def test_loader_refuses_invalid_content(tmp_path, mutation):
    fields = good()
    mutation(fields)
    with pytest.raises(vm.LimitsError):
        vm.load_limits(write_limits(tmp_path, fields), expected_owner_uid=UID)


def test_loader_refuses_floats_duplicates_and_garbage(tmp_path):
    text = json.dumps(good())
    for bad in (
        text.replace('"schema_version": 1', '"schema_version": 1.0'),
        text.replace('"currency": "INR"', '"currency": "INR", "currency": "INR"'),
        "{not json",
        "[]",
        "",
    ):
        with pytest.raises(vm.LimitsError):
            vm.load_limits(write_limits(tmp_path, bad), expected_owner_uid=UID)


def test_loader_refuses_missing_file_symlink_and_directory(tmp_path):
    with pytest.raises(vm.LimitsError):
        vm.load_limits(tmp_path / "absent.json", expected_owner_uid=UID)
    real = write_limits(tmp_path, good())
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(vm.LimitsError):
        vm.load_limits(link, expected_owner_uid=UID)
    with pytest.raises(vm.LimitsError):
        vm.load_limits(tmp_path, expected_owner_uid=UID)


@pytest.mark.parametrize("mode", [0o664, 0o646, 0o666, 0o620, 0o602])
def test_loader_refuses_a_file_writable_by_group_or_other(tmp_path, mode):
    with pytest.raises(vm.LimitsError):
        vm.load_limits(write_limits(tmp_path, good(), mode=mode), expected_owner_uid=UID)


def test_loader_refuses_the_wrong_owner(tmp_path):
    path = write_limits(tmp_path, good())
    with pytest.raises(vm.LimitsError):
        vm.load_limits(path, expected_owner_uid=UID + 1)
    # The production default is root: a file owned by this (non-root) user is refused.
    if UID != 0:
        with pytest.raises(vm.LimitsError):
            vm.load_limits(path)


def test_ordering_rules_match_backend_check_india_limits():
    """Same accept and refuse decisions as backend _check_india_limits on the five shared fields."""
    shared = ("capital_cap", "per_position_cap", "drawdown_halt", "drawdown_flatten", "position_stop")
    variants = [
        {},
        {"capital_cap": "0"},
        {"per_position_cap": "50000"},
        {"per_position_cap": "50001"},
        {"drawdown_halt": "0"},
        {"drawdown_halt": "-0.0001"},
        {"drawdown_flatten": "-0.08"},
        {"drawdown_flatten": "-0.0799"},
        {"drawdown_flatten": "-1"},
        {"drawdown_flatten": "-0.9999"},
        {"position_stop": "0"},
        {"position_stop": "-1"},
        {"position_stop": "-0.9999"},
    ]
    for change in variants:
        fields = good()
        fields.update(change)
        try:
            _check_india_limits(IndiaLimits(**{k: fields[k] for k in ("schema_version", "workspace", "currency", *shared)}))
            backend_ok = True
        except PrivateConfigError:
            backend_ok = False
        try:
            vm.Limits.from_fields(fields)
            vm_ok = True
        except vm.LimitsError:
            vm_ok = False
        assert vm_ok == backend_ok, change


# -------------------------------------------------------------- tick table


def test_vendored_tick_table_is_byte_identical_to_the_backend_schedule():
    ours = (ROOT / "gateway/vm/gateway_vm/orders/data/nse_cash_tick_sizes.json").read_bytes()
    theirs = (ROOT / "backend/costs/schedules/nse_cash_tick_sizes.json").read_bytes()
    assert ours == theirs


def test_vm_ticks_match_resolve_nse_cash_tick_for_equity():
    table = vm.load_tick_table()
    prices = [
        "0.01", "1", "99.99", "249.99", "250", "250.01", "999.99", "1000", "1000.01",
        "4999.99", "5000", "5000.01", "9999.99", "10000", "10000.01", "19999.99",
        "20000", "20000.01", "123456",
    ]
    days = [date(2021, 1, 1), date(2024, 6, 9), date(2024, 6, 10), date(2025, 4, 14),
            date(2025, 4, 15), date(2026, 10, 8)]
    for day in days:
        for series in ("EQ", "BE"):
            for price in prices:
                expected = resolve_nse_cash_tick(
                    session_date=day,
                    band_reference_price=Decimal(price),
                    instrument_class=InstrumentClass.EQUITY,
                    series=series,
                ).tick.value
                assert table.resolve(day, series, Decimal(price)) == expected, (day, series, price)


def test_vm_tick_table_fails_closed_outside_its_coverage():
    table = vm.load_tick_table()
    with pytest.raises(vm.TickUnavailable):
        table.resolve(date(2020, 12, 31), "EQ", Decimal("100"))
    with pytest.raises(vm.TickUnavailable):
        table.resolve(date(2026, 10, 8), "SM", Decimal("100"))


# --------------------------------------------------------------- evaluator


def _intent(spec: dict):
    body = dict(BASE_INTENT)
    body.update(
        side=spec["side"],
        stock_code=spec["stock_code"],
        isin=spec["isin"],
        quantity=spec["quantity"],
        limit_price=spec["limit_price"],
    )
    return parse_intent(json.dumps(body))


def _flags(raw: dict) -> vm.RiskFlags:
    return vm.RiskFlags(
        halt=raw["halt"],
        ended=raw["ended"],
        mac_halt=raw["mac_halt"],
        account_mismatch=raw["account_mismatch"],
        stops=frozenset(raw["stops"]),
        ledger_cost={k: Decimal(v) for k, v in raw["ledger_cost"].items()},
    )


def _account(raw: dict) -> vm.AccountSnapshot:
    return vm.AccountSnapshot(
        holdings=tuple(vm.Holding(h["isin"], h["quantity"], Decimal(h["cost"])) for h in raw["holdings"]),
        open_orders=tuple(
            vm.OpenOrder(o["isin"], o["side"], o["quantity"], Decimal(o["limit_price"]))
            for o in raw["open_orders"]
        ),
    )


def _quote(raw: dict | None) -> vm.Quote | None:
    if raw is None:
        return None
    return vm.Quote(
        stock_code=raw["stock_code"],
        isin=raw["isin"],
        series=raw["series"],
        ltp=Decimal(raw["ltp"]),
        lower_circuit=Decimal(raw["lower_circuit"]),
        upper_circuit=Decimal(raw["upper_circuit"]),
        previous_close=Decimal(raw["previous_close"]),
        session_date=date.fromisoformat(raw["session_date"]),
    )


class VectorTickReference:
    """``TickReferencePort`` over one vector row: the reference and the month it was taken in.

    The port contract (D-09) is the close on the last trading day of the calendar month
    BEFORE the session. A reference dated to any other month, or undated, is one the port
    cannot supply for this session, so it raises, as a real port must.
    """

    def __init__(self, reference: str | None, month: str | None) -> None:
        self._reference = None if reference is None else Decimal(reference)
        self._month = None if month is None else date.fromisoformat(month)

    def band_reference(self, isin: str, session_date: date) -> Decimal:
        if self._reference is None or self._month is None:
            raise LookupError("no dated reference")
        wanted = (session_date.year - 1, 12) if session_date.month == 1 else (session_date.year, session_date.month - 1)
        if (self._month.year, self._month.month) != wanted:
            raise LookupError("reference is from the wrong month")
        return self._reference


def band_reference_for(case: dict, quote: vm.Quote | None) -> Decimal | None:
    """What RuleGuard hands the evaluator: the port's answer, or None on any failure."""
    port = VectorTickReference(case["tick_reference"], case["tick_reference_month"])
    return RuleGuard._band_reference(SimpleNamespace(_tick_reference=port), quote)  # type: ignore[arg-type]


def run_case(case: dict) -> tuple[str, ...]:
    limits = vm.Limits.from_fields(LV["limits"])
    quote = _quote(case["quote"])
    return vm.evaluate(
        limits,
        _flags(case["flags"]),
        _account(case["account"]),
        quote,
        vm.to_ist(datetime.fromisoformat(case["now_ist"])),
        _intent(case["intent"]),
        kill_enabled=case["kill_enabled"],
        tick_reference=band_reference_for(case, quote),
    )


CASES = LV["evaluator_cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_evaluator_vector(case):
    assert list(run_case(case)) == case["expected_codes"]


def test_every_evaluator_row_dates_its_tick_reference():
    assert all("tick_reference_month" in c for c in CASES)
    assert all((c["tick_reference_month"] is None) == (c["tick_reference"] is None) for c in CASES)


@pytest.mark.parametrize("name", ["tick_ref_stale_month_refuses", "tick_ref_wrong_month_refuses"])
def test_a_stale_or_wrong_month_reference_is_refused_and_never_used(name):
    case = next(c for c in CASES if c["name"] == name)
    assert case["tick_reference"] is not None
    assert list(run_case(case)) == ["tick_reference_unavailable"]
    # The same value dated to the previous month would have been accepted, so the date is the only guard.
    assert list(run_case({**case, "tick_reference_month": "2026-09-30"})) == []
    assert band_reference_for(case, _quote(case["quote"])) is None


@pytest.mark.parametrize(
    "month, session, usable",
    [
        ("2026-09-01", "2026-10-08", True),
        ("2026-09-30", "2026-10-30", True),
        ("2026-10-01", "2026-10-08", False),
        ("2026-08-31", "2026-10-08", False),
        ("2026-12-31", "2027-01-04", True),
        ("2027-01-04", "2027-01-04", False),
        ("2025-12-31", "2027-01-04", False),
        (None, "2026-10-08", False),
    ],
)
def test_the_port_harness_accepts_only_the_previous_calendar_month(month, session, usable):
    port = VectorTickReference("250.00", month)
    if usable:
        assert port.band_reference("INE000A01012", date.fromisoformat(session)) == Decimal("250.00")
    else:
        with pytest.raises(LookupError):
            port.band_reference("INE000A01012", date.fromisoformat(session))


def test_vector_file_covers_the_required_edges():
    names = {c["name"] for c in CASES}
    required = {
        "buy_exact_capital_cap_passes",
        "buy_one_paisa_over_capital_cap",
        "capital_cap_exact_with_open_buy_pending_notional_passes",
        "capital_cap_open_buy_pushes_over",
        "per_position_cap_includes_open_buys",
        "collar_buy_exactly_2pct_passes",
        "collar_buy_just_over_2pct",
        "collar_sell_exactly_2pct_passes",
        "collar_sell_just_over_2pct",
        "circuit_above_upper_refused",
        "tick_005_off_tick_refused",
        "session_1509_59_open",
        "session_1510_00_closed",
        "session_saturday_closed",
        "etf_or_inf_isin_unsupported",
        "sell_above_holding_minus_open_sells",
        "halt_refuses_buy",
        "halt_allows_sell",
        "ended_allows_sell",
        "stop_open_refuses_buy_on_a_different_isin",
        "stop_open_allows_sell_of_the_stopped_isin",
        "buy_allowed_again_after_the_exit_fill_clears_the_stop",
        "mac_halt_refuses_sell",
        "account_mismatch_refuses_sell",
        "kill_disabled_blocks",
        "tick_ref_monthly_below_250_daily_above_allows_001",
        "tick_ref_monthly_above_250_daily_below_refuses_001",
        "tick_ref_monthly_above_250_daily_below_allows_005",
        "tick_ref_monthly_above_1000_daily_at_1000_refuses_005",
        "tick_ref_unavailable_fails_closed",
        "tick_ref_zero_is_unavailable",
        "tick_ref_stale_month_refuses",
        "tick_ref_wrong_month_refuses",
    }
    assert required <= names
    assert {d["name"] for d in LV["drawdown_cases"]} >= {
        "minus_8_00_exactly_latches_halt",
        "minus_15_00_exactly_latches_ended_and_halt_and_stop",
        "stop_at_exactly_cost_x_0_88",
        "gap_through_minus_15_sets_both",
    }


def test_stop_open_is_returned_before_every_non_hard_block_buy_code():
    """The Mac vectors rely on this ordering: pin it directly as well."""
    for case in CASES:
        codes = case["expected_codes"]
        if "stop_open" in codes:
            hard = {"kill_switch", "mac_halt", "account_mismatch", "pilot_ended"}
            leading = codes[: codes.index("stop_open")]
            assert set(leading) <= hard, case["name"]


def test_evaluator_is_pure_over_its_inputs():
    case = next(c for c in CASES if c["name"] == "buy_ok")
    first = run_case(case)
    assert run_case(case) == first == ()


def test_session_helpers_use_ist_not_the_host_zone():
    utc = datetime.fromisoformat("2026-10-08T03:45:00+00:00")  # 09:15 IST
    assert vm.session_open(vm.to_ist(utc))
    assert not vm.session_open(vm.to_ist(datetime.fromisoformat("2026-10-08T03:44:59+00:00")))
    with pytest.raises(ValueError):
        vm.to_ist(datetime(2026, 10, 8, 10, 0))
