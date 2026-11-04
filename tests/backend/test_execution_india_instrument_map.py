"""The Mac guard checks the quote against the instrument's Breeze stock code, never the NSE symbol (Phase 64 fix).

The quote and the VM intent are keyed by the Breeze stock code, which differs from the NSE symbol for many names
(CANBK trades as CANBAN). Every value here is synthetic. The ledger and private/ directory live in tmp, the clock is
injected, and nothing contacts a broker or the relay.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import india_limits_support as ils
from execution import AdmissionDecision
from execution.india_guard import (
    INSTRUMENT_UNMAPPED,
    IndiaAdmissionGuard,
    IndiaInstrumentMap,
    IndiaLimitDenied,
)
from private_config import load_workspace_config
from risk_india import rules

SYMBOL = "CANBK"
CODE = "CANBAN"
ISIN = "INE476A01022"
OTHER_ISIN = "INE467B01029"
TICKER = f"NSE:CASH:{SYMBOL}"


def instrument(symbol: str = SYMBOL, code: str = CODE, isin: str = ISIN) -> SimpleNamespace:
    return SimpleNamespace(symbol=symbol, stock_code=code, isin=isin)


def mapped() -> IndiaInstrumentMap:
    return IndiaInstrumentMap.from_instruments([instrument()])


def evidence(**overrides):
    values = {"stock_code": CODE, "isin": ISIN}
    values.update(overrides)
    return ils.make_evidence(**values)


@pytest.fixture
def world(tmp_path):
    private = ils.india_private_dir(tmp_path)
    ledger = ils.open_ledger(tmp_path)
    try:
        yield SimpleNamespace(private=private, ledger=ledger)
    finally:
        ledger.close()


def guard_for(world, instruments):
    return ils.make_guard(world.ledger, world.private, instruments=instruments)


def deny_code(world, guard, *, side="BUY", ticker=TICKER, quote=None):
    intent = ils.make_intent("p1", side=side, ticker=ticker)
    with pytest.raises(IndiaLimitDenied) as refused:
        guard.check_order(intent, world.ledger, quote if quote is not None else evidence())
    return refused.value


def test_a_differing_symbol_and_code_is_admitted_when_the_mapping_matches(world):
    guard = guard_for(world, mapped())
    intent = ils.make_intent("p1", ticker=TICKER)
    detail = guard.check_order(intent, world.ledger, evidence())
    assert detail["limits_sha256"] == guard.limits_sha256


def test_a_quote_whose_code_is_not_the_mapped_code_is_denied_isin_mismatch(world):
    guard = guard_for(world, mapped())
    assert deny_code(world, guard, quote=evidence(stock_code="OTHERCODE")).code == "isin_mismatch"


def test_a_quote_carrying_the_nse_symbol_is_denied_when_the_mapped_code_differs(world):
    guard = guard_for(world, mapped())
    refused = deny_code(world, guard, quote=evidence(stock_code=SYMBOL))
    assert refused.code == "isin_mismatch"


def test_a_quote_of_another_isin_is_still_denied_isin_mismatch(world):
    guard = guard_for(world, mapped())
    assert deny_code(world, guard, quote=evidence(isin=OTHER_ISIN)).code == "isin_mismatch"


def test_a_name_missing_from_the_map_is_denied_instrument_unmapped(world):
    guard = guard_for(world, IndiaInstrumentMap.from_instruments([instrument("OTHER", "OTHERCODE", OTHER_ISIN)]))
    refused = deny_code(world, guard)
    assert refused.code == INSTRUMENT_UNMAPPED == "instrument_unmapped"


def test_a_missing_map_never_falls_back_to_the_symbol(world):
    """The quote's code equals the symbol, which is exactly what the old guard accepted. No map: denied."""

    config = load_workspace_config(world.private, "india", require_india_execution=True)
    guard = IndiaAdmissionGuard.from_config(config, world.ledger, clock=lambda: ils.NOW)
    refused = deny_code(world, guard, ticker=ils.TICKER, quote=ils.make_evidence())
    assert refused.code == INSTRUMENT_UNMAPPED


def test_an_unmapped_sell_is_denied_too(world):
    guard = guard_for(world, IndiaInstrumentMap.from_instruments([]))
    assert deny_code(world, guard, side="SELL").code == INSTRUMENT_UNMAPPED


def test_a_non_nse_cash_ticker_stays_instrument_unsupported(world):
    guard = guard_for(world, mapped())
    assert deny_code(world, guard, ticker="BSE:CASH:CANBK").code == "instrument_unsupported"


def test_the_map_refuses_conflicting_entries():
    with pytest.raises(rules.RiskConfigError):
        IndiaInstrumentMap.from_instruments([instrument(), instrument(code="OTHERCODE", isin=OTHER_ISIN)])  # symbol
    with pytest.raises(rules.RiskConfigError):
        IndiaInstrumentMap.from_instruments([instrument(), instrument("OTHER", CODE, OTHER_ISIN)])  # code
    with pytest.raises(rules.RiskConfigError):
        IndiaInstrumentMap.from_instruments([instrument(), instrument("OTHER", "OTHERCODE", ISIN)])  # isin
    with pytest.raises(rules.RiskConfigError):
        IndiaInstrumentMap.from_instruments([instrument(code="")])


def test_the_guard_refuses_something_that_is_not_a_map(world):
    with pytest.raises(rules.RiskConfigError):
        guard_for(world, {TICKER: CODE})  # type: ignore[arg-type]


def test_the_admission_service_records_instrument_unmapped_and_reserves_nothing(world):
    guard = guard_for(world, IndiaInstrumentMap.from_instruments([]))
    service = ils.make_service(world.ledger, guard)
    admission = ils.prepare(service, ils.make_intent("p9", ticker=TICKER), evidence=evidence())
    assert admission.decision is AdmissionDecision.DENIED
    assert admission.reason_code == "instrument_unmapped"
    assert world.ledger.get_reservation("p9") is None
    assert world.ledger.get_order("p9").state == "REJECTED"


def test_the_admission_service_admits_a_mapped_differing_name(world):
    guard = guard_for(world, mapped())
    service = ils.make_service(world.ledger, guard)
    admission = ils.prepare(service, ils.make_intent("p8", ticker=TICKER), evidence=evidence())
    assert admission.decision is AdmissionDecision.ADMITTED
