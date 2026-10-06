"""Where slippage and liquidity figures go, and the deterministic slippage check (Phase 63-02).

Two separate things, kept separate on purpose:

* The risk critic (the registry's ``risk_critic`` role) is advisory. It now receives the
  structured ``risk_governance`` figures (slippage_bps, liquidity_status, POV, 30-day ADV,
  systemic level) as prompt text, next to the decision's own wording. It still decides
  nothing: whatever it returns is returned, and no LLM-side rule gates on a figure.
* The gate is deterministic: ``risk_india.rules.slippage_check``, a pure Decimal function
  with the cap passed in. A buy is measured against the quote's ask and a sell against its
  bid; a missing side refuses (SLIPPAGE_QUOTE_UNAVAILABLE). Phase 63-04 wires it into India
  admission with ``max_slippage_bps = 25`` (operator answer 2026-10-07). It is India only; the UK
  gate belongs to Phase 66 and nothing for it lives here.

History: this file used to characterise the gap (the critic saw no ``risk_governance``
fields and no deterministic gate existed). Its docstring anticipated that Phase 63 would
flip those two tests on purpose; the replacements below assert more than the originals.
"""

import ast
import inspect
import json
import os
import re
import sys
import textwrap
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

# Add backend to path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from market_context import MarketContext, RiskGovernanceData, PriceData
from agents.decision_agent import DecisionAgent
from agents.risk_agent import RiskAgent, format_risk_governance
from risk_india import rules
import model_registry_testkit as kit
from model_registry_testkit import key_env, network_guard, offline_registry, registry_factory, stub  # noqa: F401

# The India cap, stated here on purpose: the code under test has no default for it.
INDIA_MAX_SLIPPAGE_BPS = Decimal("25")


def _illiquid_context() -> MarketContext:
    return MarketContext(
        query="Buy 1,000,000 shares of ILLIQ",
        ticker="ILLIQ",
        price=PriceData(ticker="ILLIQ", current_price=Decimal("1.0")),
        risk_governance=RiskGovernanceData(
            slippage_bps=Decimal("150.0"),
            liquidity_status="ILLIQUID",
        ),
    )


def _full_context() -> MarketContext:
    context = _illiquid_context()
    context.risk_governance = RiskGovernanceData(
        slippage_bps=Decimal("150.0"),
        liquidity_status="ILLIQUID",
        pov_participation=Decimal("0.35"),
        adv_30d=Decimal("1250000"),
        systemic_risk_level="ELEVATED",
    )
    return context


# ------------------------------------------------------------ the critic prompt


@pytest.mark.asyncio
async def test_slippage_and_liquidity_reach_the_decision_prompt(offline_registry):
    from unittest.mock import MagicMock

    prompt = DecisionAgent(mcp_client=MagicMock())._build_prompt(_illiquid_context(), "Buy ILLIQ")
    assert "Est. Slippage: 150.0 bps" in prompt
    assert "ILLIQUID" in prompt


@pytest.mark.asyncio
async def test_figures_in_the_decision_text_reach_the_critic_prompt(stub, registry_factory):
    registry_factory()
    recommendation = "BUY 1,000,000 ILLIQ. LIQUIDITY: Status: ILLIQUID | Est. Slippage: 150.0 bps"
    review = await RiskAgent().review(_illiquid_context(), recommendation)

    [request] = stub.snapshot()
    assert request.model == kit.model_id_for("risk_critic")
    prompt = json.dumps(request.json["messages"])
    assert "Est. Slippage: 150.0 bps" in prompt
    assert "ILLIQUID" in prompt
    # The verdict is whatever the critic returned. Nothing here gates on slippage.
    assert review["status"] == "APPROVED"


@pytest.mark.asyncio
async def test_critic_receives_the_risk_governance_fields_even_when_the_text_omits_them(stub, registry_factory):
    """Replaces the gap characterisation: the recommendation says nothing about liquidity."""
    registry_factory()
    await RiskAgent().review(_full_context(), "BUY 1,000,000 ILLIQ")

    [request] = stub.snapshot()
    prompt = json.dumps(request.json["messages"])
    # JSON escaping turns the newlines between lines into \n, so match on each line's text.
    assert "slippage_bps: 150.0" in prompt
    assert "liquidity_status: ILLIQUID" in prompt
    assert "pov_participation: 0.35" in prompt
    assert "adv_30d: 1250000" in prompt
    assert "systemic_risk_level: ELEVATED" in prompt
    assert "150" in prompt and "ILLIQUID" in prompt
    assert "unavailable" not in prompt


@pytest.mark.asyncio
async def test_unset_figures_read_unavailable_in_the_prompt_never_zero(stub, registry_factory):
    registry_factory()
    await RiskAgent().review(_illiquid_context(), "BUY 1,000,000 ILLIQ")  # pov and adv unset

    [request] = stub.snapshot()
    prompt = json.dumps(request.json["messages"])
    assert "slippage_bps: 150.0" in prompt and "liquidity_status: ILLIQUID" in prompt
    assert "pov_participation: unavailable" in prompt
    assert "adv_30d: unavailable" in prompt
    assert "pov_participation: 0" not in prompt and "adv_30d: 0" not in prompt


@pytest.mark.asyncio
async def test_without_risk_governance_the_prompt_says_unavailable_not_zero(stub, registry_factory):
    registry_factory()
    context = _illiquid_context()
    context.risk_governance = None
    review = await RiskAgent().review(context, "BUY 1,000,000 ILLIQ")

    [request] = stub.snapshot()
    prompt = json.dumps(request.json["messages"])
    assert "unavailable" in prompt
    assert "is not zero" in prompt
    assert "slippage_bps: 0" not in prompt and "ILLIQUID" not in prompt and "STABLE" not in prompt
    assert review["status"] == "APPROVED"


def test_format_risk_governance_text_is_exact_and_decimal_safe():
    assert format_risk_governance(None).startswith("- slippage_bps, liquidity_status")
    text = format_risk_governance(
        RiskGovernanceData(
            slippage_bps=Decimal("1E+2"), adv_30d=Decimal("1E+6"), pov_participation=Decimal("0.05"),
            liquidity_status="THIN", systemic_risk_level="ELEVATED",
        )
    )
    assert text.splitlines() == [
        "- slippage_bps: 100",
        "- liquidity_status: THIN",
        "- pov_participation: 0.05",
        "- adv_30d: 1000000",
        "- systemic_risk_level: ELEVATED",
    ]


def test_an_empty_risk_governance_object_reads_unavailable_for_every_figure():
    """The model defaults (STABLE, LOW) are not computed values: an empty object must not say them."""
    text = format_risk_governance(RiskGovernanceData())
    assert text.splitlines() == [
        "- slippage_bps: unavailable",
        "- liquidity_status: unavailable",
        "- pov_participation: unavailable",
        "- adv_30d: unavailable",
        "- systemic_risk_level: unavailable",
    ]
    assert "STABLE" not in text and "LOW" not in text


def test_explicitly_supplied_stable_and_low_are_shown_as_computed_values():
    text = format_risk_governance(RiskGovernanceData(liquidity_status="STABLE", systemic_risk_level="LOW"))
    lines = text.splitlines()
    assert lines[1] == "- liquidity_status: STABLE" and lines[4] == "- systemic_risk_level: LOW"
    assert lines[0] == "- slippage_bps: unavailable"  # the numeric figures stay unset
    only_liquidity = format_risk_governance(RiskGovernanceData(liquidity_status="STABLE")).splitlines()
    assert only_liquidity[1] == "- liquidity_status: STABLE"
    assert only_liquidity[4] == "- systemic_risk_level: unavailable"


def test_a_field_assigned_after_construction_counts_as_computed():
    risk_governance = RiskGovernanceData()
    risk_governance.liquidity_status = "THIN"
    risk_governance.adv_30d = Decimal("1000")
    lines = format_risk_governance(risk_governance).splitlines()
    assert lines[1] == "- liquidity_status: THIN" and lines[3] == "- adv_30d: 1000"
    assert lines[4] == "- systemic_risk_level: unavailable"


@pytest.mark.asyncio
async def test_an_empty_risk_governance_object_reaches_the_critic_as_unavailable(stub, registry_factory):
    registry_factory()
    context = _illiquid_context()
    context.risk_governance = RiskGovernanceData()
    await RiskAgent().review(context, "BUY 1,000,000 ILLIQ")

    [request] = stub.snapshot()
    prompt = json.dumps(request.json["messages"])
    assert "liquidity_status: unavailable" in prompt and "systemic_risk_level: unavailable" in prompt
    assert "liquidity_status: STABLE" not in prompt and "systemic_risk_level: LOW" not in prompt


@pytest.mark.asyncio
async def test_the_critic_stays_advisory_and_does_not_gate_on_a_figure(stub, registry_factory):
    registry_factory()
    review = await RiskAgent().review(_full_context(), "BUY 1,000,000 ILLIQ")
    assert review["status"] == "APPROVED"  # the stub's verdict, returned as is, at 150 bps ILLIQUID
    source = inspect.getsource(sys.modules["agents.risk_agent"])
    assert "SLIPPAGE_LIMIT" not in source and "max_slippage_bps" not in source


# ------------------------------------------------- the deterministic slippage gate

REF = Decimal("10000")


def _book(bid=None, ask=None, ltp="10100") -> rules.Quote:
    """A quote whose only interesting fields are the book; ltp is deliberately far from both."""
    return rules.Quote(
        stock_code="TESTCO", isin="INE000A01012", series="EQ", ltp=Decimal(ltp),
        lower_circuit=Decimal("9000"), upper_circuit=Decimal("11000"), previous_close=Decimal("10000"),
        session_date=date(2026, 10, 8), tick_reference=Decimal("10000"), bid=bid, ask=ask,
    )


def _check(side: str, reference, fill, cap):
    """The reference sits on the side the order takes; the other side is a decoy."""
    if side == "buy":
        return rules.slippage_check(side, _book(bid=Decimal("1"), ask=reference), fill, cap)
    return rules.slippage_check(side, _book(bid=reference, ask=Decimal("1000000")), fill, cap)


def _ok(side: str, fill: str, cap=INDIA_MAX_SLIPPAGE_BPS, ref=REF) -> rules.SlippageResult:
    return _check(side, ref, Decimal(fill), cap)


def test_buy_at_exactly_25_bps_above_the_reference_passes_and_25_01_fails():
    at_cap = _ok("buy", "10025.00")  # 25.00 bps
    assert at_cap.ok and at_cap.code is None and at_cap.slippage_bps == Decimal("25")
    over = _ok("buy", "10025.01")  # 25.01 bps
    assert not over.ok and over.code == "SLIPPAGE_LIMIT" and over.reason == "over_cap"
    assert over.slippage_bps == Decimal("25.01")


def test_sell_sign_flips_25_bps_below_passes_and_25_01_fails():
    assert _ok("sell", "9975.00").ok  # exactly 25.00 bps below the reference
    over = _ok("sell", "9974.99")  # 25.01 bps below
    assert not over.ok and over.code == "SLIPPAGE_LIMIT"
    assert over.slippage_bps == Decimal("25.01")


def test_a_price_better_than_the_reference_passes_on_both_sides():
    assert _ok("buy", "9000.00").ok  # bought far below the reference
    assert _ok("sell", "11000.00").ok  # sold far above it
    below = _ok("buy", "9990.00")
    assert below.ok and below.slippage_bps < 0


def test_the_wrong_direction_is_not_the_same_breach():
    assert _ok("buy", "10025.01").ok is False and _ok("sell", "10025.01").ok is True
    assert _ok("sell", "9974.99").ok is False and _ok("buy", "9974.99").ok is True


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_the_edge_is_exact_at_other_reference_prices(side):
    for ref in ("37.85", "1", "99999.99", "0.05"):
        reference = Decimal(ref)
        step = reference * Decimal("25") / Decimal("10000")
        exact = reference + step if side == "buy" else reference - step
        just_over = (reference + step * Decimal("1.0001")) if side == "buy" else (reference - step * Decimal("1.0001"))
        assert _check(side, reference, exact, INDIA_MAX_SLIPPAGE_BPS).ok, (side, ref)
        assert not _check(side, reference, just_over, INDIA_MAX_SLIPPAGE_BPS).ok, (side, ref)


# ---- the reference is the side of the book the order takes (bid for a sell, ask for a buy)

BID, ASK = Decimal("9980.00"), Decimal("10000.00")  # a 20 point spread, ltp 10100 is far from both


def test_a_buy_is_measured_against_the_ask_and_a_sell_against_the_bid():
    quote = _book(bid=BID, ask=ASK)
    # exactly 25 bps above the ask (10025.00) and 25 bps below the bid (9955.05)
    buy_edge = rules.slippage_check("buy", quote, Decimal("10025.00"), INDIA_MAX_SLIPPAGE_BPS)
    sell_edge = rules.slippage_check("sell", quote, Decimal("9955.05"), INDIA_MAX_SLIPPAGE_BPS)
    assert buy_edge.ok and buy_edge.slippage_bps == Decimal("25")
    assert sell_edge.ok and sell_edge.slippage_bps == Decimal("25")
    # one hundredth of a basis point worse, on each side
    assert not rules.slippage_check("buy", quote, Decimal("10025.01"), INDIA_MAX_SLIPPAGE_BPS).ok
    assert not rules.slippage_check("sell", quote, Decimal("9955.04"), INDIA_MAX_SLIPPAGE_BPS).ok


def test_the_other_side_of_the_book_and_the_last_price_are_never_the_reference():
    quote = _book(bid=BID, ask=ASK, ltp="10100")
    # 10025.00 is 25 bps over the ask but 45 bps over the bid and under ltp: only the ask matters.
    assert rules.slippage_check("buy", quote, Decimal("10025.00"), INDIA_MAX_SLIPPAGE_BPS).ok
    assert not rules.slippage_check("buy", quote, Decimal("10025.00"), "10").ok  # 25 bps over a 10 bps cap
    # 9955.05 is 25 bps under the bid but 45 bps under the ask: only the bid matters.
    assert rules.slippage_check("sell", quote, Decimal("9955.05"), INDIA_MAX_SLIPPAGE_BPS).ok
    # A buy at the bid is "better than the ask"; a sell at the ask is "better than the bid".
    assert rules.slippage_check("buy", quote, BID, INDIA_MAX_SLIPPAGE_BPS).slippage_bps < 0
    assert rules.slippage_check("sell", quote, ASK, INDIA_MAX_SLIPPAGE_BPS).slippage_bps < 0


def test_a_missing_side_of_the_book_refuses_with_a_typed_code():
    buy = rules.slippage_check("buy", _book(bid=BID, ask=None), ASK, INDIA_MAX_SLIPPAGE_BPS)
    sell = rules.slippage_check("sell", _book(bid=None, ask=ASK), BID, INDIA_MAX_SLIPPAGE_BPS)
    for result, reason in ((buy, "ask_missing"), (sell, "bid_missing")):
        assert not result.ok and result.code == rules.SLIPPAGE_QUOTE_UNAVAILABLE
        assert result.code != rules.SLIPPAGE_LIMIT and result.reason == reason
        assert result.slippage_bps is None
    # Only the order's own side is needed: a buy does not need a bid, a sell does not need an ask.
    assert rules.slippage_check("buy", _book(bid=None, ask=ASK), ASK, INDIA_MAX_SLIPPAGE_BPS).ok
    assert rules.slippage_check("sell", _book(bid=BID, ask=None), BID, INDIA_MAX_SLIPPAGE_BPS).ok


def test_a_quote_with_no_book_at_all_never_falls_back_to_ltp():
    bare = rules.Quote(  # bid and ask left at their defaults
        "TESTCO", "INE000A01012", "EQ", Decimal("10000"), Decimal("9000"), Decimal("11000"),
        Decimal("10000"), date(2026, 10, 8), tick_reference=Decimal("10000"),
    )
    for side in ("buy", "sell"):
        result = rules.slippage_check(side, bare, Decimal("10000"), INDIA_MAX_SLIPPAGE_BPS)
        assert not result.ok and result.code == rules.SLIPPAGE_QUOTE_UNAVAILABLE
        none = rules.slippage_check(side, None, Decimal("10000"), INDIA_MAX_SLIPPAGE_BPS)
        assert not none.ok and none.code == rules.SLIPPAGE_QUOTE_UNAVAILABLE
    for notquote in (REF, "10000", {"ask": REF, "bid": REF}):
        assert rules.slippage_check("buy", notquote, REF, INDIA_MAX_SLIPPAGE_BPS).code == (  # type: ignore[arg-type]
            rules.SLIPPAGE_QUOTE_UNAVAILABLE
        )


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_an_unusable_reference_or_a_missing_price_fails_closed(side):
    for reference in (
        None, "", Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), 10000.0, True,
    ):
        result = _check(side, reference, Decimal("10000"), INDIA_MAX_SLIPPAGE_BPS)
        assert not result.ok and result.code == rules.SLIPPAGE_QUOTE_UNAVAILABLE, reference
        assert result.slippage_bps is None
    for price in (None, Decimal("0"), Decimal("Infinity"), Decimal("NaN"), 10000.0, ""):
        result = _check(side, REF, price, INDIA_MAX_SLIPPAGE_BPS)  # type: ignore[arg-type]
        assert not result.ok and result.code == "SLIPPAGE_LIMIT" and result.reason == "price_missing", price
        assert result.slippage_bps is None


def test_a_missing_cap_fails_closed_and_a_zero_or_negative_cap_is_refused():
    missing = rules.slippage_check("buy", _book(ask=REF), REF, None)
    assert not missing.ok and missing.code == "SLIPPAGE_LIMIT" and missing.reason == "cap_missing"
    for bad in (Decimal("0"), Decimal("-25"), Decimal("-0.01"), "0", "-5", Decimal("NaN"), 25.0, True, ""):
        with pytest.raises(rules.RiskConfigError):
            rules.slippage_check("buy", _book(ask=REF), REF, bad)  # type: ignore[arg-type]


def test_cap_arrives_as_a_decimal_string_and_the_function_has_no_default_cap():
    quote = _book(bid=REF, ask=REF)
    assert rules.slippage_check("buy", quote, Decimal("10025.00"), "25").ok
    assert not rules.slippage_check("buy", quote, Decimal("10025.01"), "25").ok
    parameters = inspect.signature(rules.slippage_check).parameters
    assert [p.default for p in parameters.values()] == [inspect.Parameter.empty] * len(parameters)
    tree = ast.parse(textwrap.dedent(inspect.getsource(rules.slippage_check)))
    constants = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)]
    assert 25 not in constants and "25" not in constants  # the India value is the caller's, not hidden here
    with pytest.raises(rules.RiskConfigError):
        rules.slippage_check("hold", quote, REF, INDIA_MAX_SLIPPAGE_BPS)


def test_a_tighter_and_a_looser_cap_move_the_edge():
    assert not _ok("buy", "10010.01", cap=Decimal("10")).ok and _ok("buy", "10010.00", cap=Decimal("10")).ok
    assert _ok("buy", "10050.00", cap=Decimal("50")).ok and not _ok("buy", "10050.01", cap=Decimal("50")).ok


def test_the_check_is_pure():
    quote = _book(bid=REF, ask=REF)
    a = rules.slippage_check("buy", quote, Decimal("10025.01"), INDIA_MAX_SLIPPAGE_BPS)
    assert a == rules.slippage_check("buy", quote, Decimal("10025.01"), INDIA_MAX_SLIPPAGE_BPS)


# -------------------------------------------------------------- India only here


def test_no_uk_slippage_work_lives_in_phase_63():
    uk = re.compile(r"\b(uk|gbp|trading212|trading 212|t212|isa)\b", re.IGNORECASE)
    package = Path(rules.__file__).parent
    for path in sorted(package.glob("*.py")):
        assert uk.search(path.read_text(encoding="utf-8")) is None, path.name
