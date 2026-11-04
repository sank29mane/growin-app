"""Chat ticker extraction: ordinary words are never tickers; the coordinator wins.

Found in a Phase 67 chat run: "Should I buy ... TCS" set ticker=SHOULD and
"Buy ... RELIANCE" set ticker=BUY (then BUY.L via TickerResolver's short-ticker
UK rule). No network and no live model: the routing reply comes from the
loopback stub.
"""

from unittest.mock import MagicMock

import pytest

import model_registry_testkit as kit  # noqa: F401
from model_registry_testkit import (  # noqa: F401  (fixtures)
    key_env,
    network_guard,
    registry_factory,
    risk_workspace,
    stub,
)
from utils import extract_ticker_from_text


@pytest.mark.parametrize(
    "message, expected",
    [
        ("Should I buy 5000 rupees of TCS for a two week swing trade?", "TCS"),
        ("Buy 30000 rupees of RELIANCE right now with 3x margin", "RELIANCE"),
        ("What does the RSI indicator measure? Keep it short.", None),
        ("$AAPL earnings", "AAPL"),
        ("how is VOD.L doing", "VOD.L"),
        ("Is HDFCBANK a good swing trade", "HDFCBANK"),
        ("Good evening!", None),
        ("what about BAJAJFINSV?", "BAJAJFINSV"),
        ("is 3GLD a good hold", "3GLD"),
        ("Tell me about the NIFTYBEES fund", "NIFTYBEES"),
        ("Should I invest in an ETF or a SIP in INR?", None),
        ("Check the price", None),
        ("how is (TCS) doing today", "TCS"),
        ("", None),
    ],
)
def test_heuristic_only_accepts_capitalised_tokens(message, expected):
    assert extract_ticker_from_text(message) == expected


@pytest.mark.parametrize(
    "word", ["Should", "should", "Buy", "buy", "Reliance", "reliance", "Tomorrow", "Swing", "margin"]
)
def test_ordinary_words_never_become_tickers(word):
    assert extract_ticker_from_text(f"{word} is a word") is None
    assert extract_ticker_from_text(f"so {word} then") is None


def test_ordinary_words_are_not_tickers_even_in_a_long_sentence():
    text = "Should I buy something tomorrow or should I wait for the market to settle down?"
    assert extract_ticker_from_text(text) is None


# --- coordinator TICKER vs the chat heuristic -------------------------------------------

@pytest.fixture
def _guard(network_guard):
    return network_guard


@pytest.mark.asyncio
async def test_coordinator_ticker_beats_a_conflicting_heuristic_guess(stub, registry_factory, _guard):
    registry_factory()
    from agents.orchestrator_agent import OrchestratorAgent

    stub.reply_text = "INTENT: swing_trade\nTICKER: TCS\nREASON: stub"
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    intent = await orchestrator._classify_intent("Should I buy 5000 rupees of TCS?")
    assert intent["primary_ticker"] == "TCS"
    assert intent["ticker_source"] == "coordinator"
    # chat_routes handed in a wrong guess; the coordinator's answer replaces it.
    assert orchestrator._reconcile_ticker("SHOULD", intent) == "TCS"
    assert orchestrator._reconcile_ticker(None, intent) == "TCS"


@pytest.mark.asyncio
async def test_coordinator_none_keeps_the_heuristic_and_both_empty_stays_none(stub, registry_factory, _guard):
    registry_factory()
    from agents.orchestrator_agent import OrchestratorAgent

    stub.reply_text = "INTENT: conversational\nTICKER: NONE\nREASON: stub"
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    intent = await orchestrator._classify_intent("Good evening!")
    assert intent["primary_ticker"] is None
    assert orchestrator._reconcile_ticker(None, intent) is None
    assert orchestrator._reconcile_ticker("VOD.L", intent) == "VOD.L"


@pytest.mark.asyncio
async def test_a_heuristic_routing_fallback_never_overrides_the_chat_ticker(stub, registry_factory, _guard):
    registry_factory()
    from agents.orchestrator_agent import OrchestratorAgent

    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    intent = orchestrator._heuristic_classify("Should I buy 30000 rupees of RELIANCE right now?")
    assert intent["primary_ticker"] == "RELIANCE"
    assert orchestrator._reconcile_ticker("TCS", intent) == "TCS"
    assert orchestrator._heuristic_classify("Should I buy something now?")["primary_ticker"] is None
