"""Mac India rule set against the shared limits vectors (Phase 63-02, Task 1).

``limits_vectors.json`` is the same file the VM suite reads (63-01). The Mac rule set
in ``backend/risk_india/rules.py`` is a separate implementation, so every row here is
a cross-check between two codebases, not a re-run of one.
"""

from __future__ import annotations

import ast
import dataclasses
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from costs.core import InputError
from costs.ticks import InstrumentClass
from risk_india import rules

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "backend" / "fixtures" / "relay_orders"
LV = json.loads((FIXTURES / "limits_vectors.json").read_text(encoding="utf-8"))
SV = json.loads((FIXTURES / "signing_vectors.json").read_text(encoding="utf-8"))
CASES = LV["evaluator_cases"]
PACKAGE = ROOT / "backend" / "risk_india"


def good() -> dict:
    return dict(LV["limits"])


def _flags(raw: dict) -> rules.RiskFlags:
    return rules.RiskFlags(
        halt=raw["halt"],
        ended=raw["ended"],
        mac_halt=raw["mac_halt"],
        account_mismatch=raw["account_mismatch"],
        stops=frozenset(raw["stops"]),
        ledger_cost={k: Decimal(v) for k, v in raw["ledger_cost"].items()},
    )


def _account(raw: dict) -> rules.Account:
    return rules.Account(
        holdings=tuple(
            rules.Holding(h["isin"], h["quantity"], Decimal(h["cost"])) for h in raw["holdings"]
        ),
        open_orders=tuple(
            rules.OpenOrder(o["isin"], o["side"], o["quantity"], Decimal(o["limit_price"]))
            for o in raw["open_orders"]
        ),
    )


def _quote(raw: dict | None, tick_reference: str | None) -> rules.Quote | None:
    if raw is None:
        return None
    return rules.Quote(
        stock_code=raw["stock_code"],
        isin=raw["isin"],
        series=raw["series"],
        ltp=Decimal(raw["ltp"]),
        lower_circuit=Decimal(raw["lower_circuit"]),
        upper_circuit=Decimal(raw["upper_circuit"]),
        previous_close=Decimal(raw["previous_close"]),
        session_date=date.fromisoformat(raw["session_date"]),
        tick_reference=None if tick_reference is None else Decimal(tick_reference),
    )


def _order(spec: dict) -> rules.OrderRequest:
    return rules.OrderRequest(
        side=spec["side"],
        stock_code=spec["stock_code"],
        isin=spec["isin"],
        quantity=spec["quantity"],
        limit_price=Decimal(spec["limit_price"]),
    )


def run_case(case: dict) -> rules.Decision:
    return rules.evaluate(
        rules.Limits.from_fields(LV["limits"]),
        _flags(case["flags"]),
        _account(case["account"]),
        _quote(case["quote"], case["tick_reference"]),
        datetime.fromisoformat(case["now_ist"]),
        _order(case["intent"]),
        kill_enabled=case["kill_enabled"],
    )


# ------------------------------------------------------------- vector rows


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_mac_rules_match_the_vm_vector(case):
    decision = run_case(case)
    assert list(decision.codes) == case["expected_codes"]
    assert decision.allowed is (case["expected_codes"] == [])


def test_every_vector_row_is_exercised_and_the_file_is_the_shared_one():
    assert len(CASES) >= 59
    assert LV["test_only"] is True
    assert CASES == json.loads((FIXTURES / "limits_vectors.json").read_text())["evaluator_cases"]


def test_mac_vector_rows_cover_the_edges_the_plan_names():
    names = {c["name"] for c in CASES}
    assert {
        "session_1509_59_open",
        "session_1510_00_closed",
        "buy_exact_capital_cap_passes",
        "buy_one_paisa_over_capital_cap",
        "collar_buy_exactly_2pct_passes",
        "collar_buy_just_over_2pct",
        "stop_open_refuses_buy_on_a_different_isin",
        "stop_open_allows_sell_of_the_stopped_isin",
        "buy_allowed_again_after_the_exit_fill_clears_the_stop",
        "halt_allows_sell",
        "ended_allows_sell",
        "mac_halt_refuses_sell",
    } <= names


def test_stop_open_precedes_every_non_hard_block_code():
    hard = {"kill_switch", "mac_halt", "account_mismatch", "pilot_ended"}
    seen = 0
    for case in CASES:
        codes = run_case(case).codes
        if "stop_open" in codes:
            seen += 1
            assert set(codes[: codes.index("stop_open")]) <= hard, case["name"]
    assert seen >= 4


def test_the_decision_is_pure_over_its_inputs():
    case = next(c for c in CASES if c["name"] == "buy_ok")
    assert run_case(case).codes == run_case(case).codes == ()


def test_session_helpers_use_ist_not_the_host_zone():
    assert rules.session_open(rules.to_ist(datetime.fromisoformat("2026-10-08T03:45:00+00:00")))
    assert not rules.session_open(
        rules.to_ist(datetime.fromisoformat("2026-10-08T03:44:59+00:00"))
    )
    cutoff = rules.to_ist(datetime.fromisoformat("2026-10-08T09:40:00+00:00"))  # 15:10:00 IST
    assert not rules.session_open(cutoff)
    with pytest.raises(rules.RiskConfigError):
        rules.to_ist(datetime(2026, 10, 8, 10, 0))


# ---------------------------------------------------------------- the hash


def test_limits_sha256_equals_the_vector_value_in_both_vector_files():
    assert rules.limits_sha256(good()) == LV["limits_sha256"] == SV["limits_sha256"]
    assert rules.Limits.from_fields(good()).sha256 == LV["limits_sha256"]


@pytest.mark.parametrize("key", rules.LIMIT_KEYS)
def test_changing_any_one_limits_field_changes_the_hash(key):
    changed = good()
    changed[key] = {
        "schema_version": 2,
        "workspace": "uk",
        "currency": "GBP",
    }.get(key, "0.5")
    assert rules.limits_sha256(changed) != LV["limits_sha256"]


def test_limits_hash_is_independent_of_key_order_and_refuses_extra_or_missing_keys():
    reordered = dict(reversed(list(good().items())))
    assert rules.limits_sha256(reordered) == LV["limits_sha256"]
    with pytest.raises(rules.RiskConfigError):
        rules.limits_sha256({**good(), "extra": "1"})
    missing = good()
    del missing["position_stop"]
    with pytest.raises(rules.RiskConfigError):
        rules.limits_sha256(missing)


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"workspace": "uk"},
        {"currency": "GBP"},
        {"capital_cap": 50000},  # not a string
        {"capital_cap": "0"},
        {"capital_cap": "50000.0e0"},
        {"per_position_cap": "50001"},
        {"per_position_cap": "0"},
        {"drawdown_halt": "0.08"},
        {"drawdown_halt": "0"},
        {"drawdown_flatten": "-0.05"},
        {"drawdown_flatten": "-1"},
        {"position_stop": "0"},
        {"position_stop": "-1"},
        {"fat_finger_collar": "0"},
        {"fat_finger_collar": "1"},
    ],
)
def test_invalid_limits_are_refused(mutation):
    with pytest.raises(rules.RiskConfigError):
        rules.Limits.from_fields({**good(), **mutation})


def test_order_request_refuses_bad_shapes():
    base = dict(side="buy", stock_code="TESTCO", isin="INE000A01012", quantity=1, limit_price=Decimal("1"))
    rules.OrderRequest(**base)
    for bad in (
        {"side": "hold"},
        {"quantity": 0},
        {"quantity": True},
        {"quantity": 1.5},
        {"limit_price": 100.0},
        {"limit_price": Decimal("0")},
    ):
        with pytest.raises(rules.RiskConfigError):
            rules.OrderRequest(**{**base, **bad})


# ------------------------------------------------------------------- ticks


def test_ticks_resolve_through_the_mac_resolver_not_the_vm_copy(monkeypatch):
    """The off-tick code must come from costs.ticks: stub it and the answer follows."""
    case = next(c for c in CASES if c["name"] == "buy_ok")
    assert run_case(case).codes == ()
    calls: list[dict] = []

    def refuse(**kwargs):
        calls.append(kwargs)
        raise InputError("no tick table")

    monkeypatch.setattr(rules, "resolve_nse_cash_tick", refuse)
    assert run_case(case).codes == ("off_tick",)
    assert calls and calls[0]["instrument_class"] is InstrumentClass.EQUITY
    assert calls[0]["series"] == "EQ"
    assert calls[0]["band_reference_price"] == Decimal(case["tick_reference"])


def test_the_band_reference_is_the_tick_reference_never_previous_close(monkeypatch):
    """The monthly reference and the daily close sit on opposite sides of Rs 250 in these rows."""
    seen: list[Decimal] = []
    real = rules.resolve_nse_cash_tick

    def spy(**kwargs):
        seen.append(kwargs["band_reference_price"])
        return real(**kwargs)

    monkeypatch.setattr(rules, "resolve_nse_cash_tick", spy)
    case = next(c for c in CASES if c["name"] == "tick_ref_monthly_below_250_daily_above_allows_001")
    assert Decimal(case["quote"]["previous_close"]) >= 250 > Decimal(case["tick_reference"])
    assert run_case(case).codes == ()
    assert seen == [Decimal(case["tick_reference"])]


def test_the_tick_reference_vector_rows_the_vm_added_are_all_present():
    names = {c["name"] for c in CASES}
    assert {
        "tick_ref_monthly_below_250_daily_above_allows_001",
        "tick_ref_monthly_above_250_daily_below_refuses_001",
        "tick_ref_monthly_above_250_daily_below_allows_005",
        "tick_ref_monthly_above_1000_daily_at_1000_refuses_005",
        "tick_ref_unavailable_fails_closed",
        "tick_ref_zero_is_unavailable",
    } <= names
    assert all("tick_reference" in c for c in CASES)


@pytest.mark.parametrize("reference", [None, Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_a_missing_or_unusable_tick_reference_refuses_with_its_own_code(reference):
    case = next(c for c in CASES if c["name"] == "buy_ok")
    quote = _quote(case["quote"], case["tick_reference"])
    assert quote is not None
    quote = dataclasses.replace(quote, tick_reference=reference)
    codes = rules.evaluate(
        rules.Limits.from_fields(LV["limits"]), _flags(case["flags"]), _account(case["account"]), quote,
        datetime.fromisoformat(case["now_ist"]), _order(case["intent"]), kill_enabled=True,
    ).codes
    assert codes == ("tick_reference_unavailable",)


def test_a_quote_dated_outside_the_tick_table_fails_closed_as_off_tick():
    case = dict(next(c for c in CASES if c["name"] == "buy_ok"))
    case["now_ist"] = "2020-12-30T10:00:00+05:30"
    case["quote"] = {**case["quote"], "session_date": "2020-12-30"}
    assert "off_tick" in run_case(case).codes


# ------------------------------------------------- independence of the VM


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.add(("." * node.level) + (node.module or ""))
    return roots


def test_nothing_under_risk_india_imports_the_vm_or_the_outside_world():
    allowed = {
        "__future__", "hashlib", "json", "re", "dataclasses", "datetime", "decimal", "typing",
        "collections.abc", "types", "costs.core", "costs.ticks", ".rules", ".exits", ".drawdown",
    }
    files = sorted(PACKAGE.glob("*.py"))
    assert {f.name for f in files} >= {"__init__.py", "rules.py"}
    for path in files:
        roots = _imported_roots(path)
        assert not {r for r in roots if "gateway" in r}, path
        assert roots <= allowed, (path.name, roots - allowed)


def test_risk_india_is_decimal_only_with_no_float_and_no_io():
    banned_calls = {"open", "print", "eval", "exec", "float", "input"}
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant):
                assert not isinstance(node.value, float), (path.name, node.lineno)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in banned_calls, (path.name, node.lineno)
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"environ", "getenv", "now", "today", "utcnow"}, (
                    path.name,
                    node.lineno,
                )
