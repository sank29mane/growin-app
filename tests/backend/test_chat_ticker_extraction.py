# ruff: noqa: F811  (pytest fixtures are imported from the testkit, then requested by name)
"""Chat ticker extraction: ordinary words are never tickers; the coordinator wins.

Found in a Phase 67 chat run: "Should I buy ... TCS" set ticker=SHOULD and
"Buy ... RELIANCE" set ticker=BUY (then BUY.L via TickerResolver's short-ticker
UK rule). No network and no live model: the routing reply comes from the
loopback stub.
"""

from types import SimpleNamespace
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
from utils import extract_dollar_ticker, extract_ticker_from_text, validate_ticker_symbol


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
        # round 1 (O5): all-caps shouting that is not a symbol
        ("URGENT NEWS please", None),
        ("HELLO there, what is the LTP", None),
        ("What should I buy?", None),
        # round 1 (T4): `$` candidates are whole tokens, validated like any other
        ("$5000 in TCS", "TCS"),
        ("I have $5000 to put in RELIANCE", "RELIANCE"),
        ("$ABCDEFGHIJKLM is huge", None),
        ("$100K into HDFCBANK", "HDFCBANK"),
        ("$5K budget", None),
        ("$AAPL.", "AAPL"),
        ("what about ($MSFT)?", "MSFT"),
        ("$infy results", "INFY"),
        ("$VOD.L today", "VOD.L"),
        ("$AAPL.com", None),
        ("price in $", None),
    ],
)
def test_heuristic_only_accepts_capitalised_tokens(message, expected):
    assert extract_ticker_from_text(message) == expected


@pytest.mark.parametrize("abbr", ["P&L", "M&A", "R&D", "Q&A", "S&P", "S&P500", "PNL"])
def test_ampersand_abbreviations_are_not_tickers(abbr):
    assert extract_ticker_from_text(f"What is my {abbr} this week?") is None
    assert extract_ticker_from_text(f"{abbr} then buy TCS") == "TCS"
    assert extract_ticker_from_text(f"({abbr})") is None
    assert validate_ticker_symbol(abbr) is None
    assert validate_ticker_symbol(abbr.lower()) is None


def test_real_ampersand_symbols_still_pass():
    # NSE lists M&M and M&MFIN, so `&` itself must stay legal.
    assert extract_ticker_from_text("how is M&M doing") == "M&M"
    assert extract_ticker_from_text("is M&MFIN a good hold") == "M&MFIN"
    assert validate_ticker_symbol("M&M") == "M&M"


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


# --- round 1 (T3): coordinator validation, explicit `$` wins ------------------------------


def _routed(ticker, source="coordinator"):
    return {"primary_ticker": ticker, "ticker_source": source, "type": "swing_trade"}


@pytest.fixture
def reconcile():
    from agents.orchestrator_agent import OrchestratorAgent

    return OrchestratorAgent._reconcile_ticker


@pytest.mark.parametrize("junk", ["BUY", "SHOULD", "NONE", "ETF", "RSI", "NEWS", "HELLO", "5000", "ABCDEFGHIJKLM", "A.LONGSUFFIX"])
def test_a_coordinator_stop_word_or_invalid_symbol_never_replaces_the_heuristic(reconcile, junk):
    assert reconcile("RELIANCE", _routed(junk), "buy 30000 rupees of RELIANCE") == "RELIANCE"
    assert reconcile(None, _routed(junk), "hello there") is None


def test_a_valid_coordinator_symbol_still_wins_in_general(reconcile):
    assert reconcile("SHOULD", _routed("TCS"), "Should I buy TCS?") == "TCS"
    assert reconcile("RELIANCE", _routed("VOD.L"), "x") == "VOD.L"
    assert reconcile(None, _routed("GE"), "x") == "GE"  # 2-letter symbols pass from the coordinator


def test_an_explicit_dollar_symbol_beats_the_coordinator(reconcile):
    assert reconcile("INFY", _routed("TCS"), "Is $INFY better than the rest?") == "INFY"
    assert reconcile(None, _routed("TCS"), "$infy outlook") == "INFY"
    # money is not a symbol
    assert reconcile("RELIANCE", _routed("TCS"), "put $5000 in RELIANCE") == "TCS"


def test_validate_ticker_symbol():
    assert validate_ticker_symbol("tcs") == "TCS"
    assert validate_ticker_symbol("VOD.L") == "VOD.L"
    for bad in (None, "", "BUY", "5000", "ABCDEFGHIJKLM", "X"):
        assert validate_ticker_symbol(bad) is None


def test_extract_dollar_ticker_is_whole_token_only():
    assert extract_dollar_ticker("$5000 in TCS") is None
    assert extract_dollar_ticker("$ABCDEFGHIJKLM") is None
    assert extract_dollar_ticker("$AAPL earnings") == "AAPL"


# --- round 1 (O1): the ticker sent downstream, through run() and run_stream() --------------


class _ReachedFabricator(Exception):
    """Raised by the stubbed data fabricator: everything before it ran for real."""


async def _ticker_sent_downstream(orchestrator, entry: str, query: str, chat_ticker):
    seen = {}

    async def fabricate_context(**kwargs):
        seen.update(kwargs)
        raise _ReachedFabricator

    orchestrator.data_fabricator.fabricate_context = fabricate_context
    kwargs = dict(query=query, history=[], ticker=chat_ticker, account_type="invest")
    with pytest.raises(_ReachedFabricator):
        if entry == "run":
            await orchestrator.run(**kwargs)
        else:
            async for _ in orchestrator.run_stream(**kwargs):
                pass
    return seen["ticker"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["run", "run_stream"])
@pytest.mark.parametrize("coordinator_symbol", ["RELIANCE", "TCS"])
async def test_the_ticker_sent_downstream_is_the_coordinators_not_the_chat_guess(
    stub, registry_factory, _guard, entry, coordinator_symbol
):
    registry_factory()
    from agents.orchestrator_agent import OrchestratorAgent

    stub.reply_text = f"INTENT: swing_trade\nTICKER: {coordinator_symbol}\nREASON: stub"
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    expected = orchestrator.ticker_resolver.normalize(coordinator_symbol)
    wrong = orchestrator.ticker_resolver.normalize("SHOULD")
    assert expected != wrong

    # chat_routes handed in SHOULD, as the old extractor did for this sentence.
    sent = await _ticker_sent_downstream(
        orchestrator, entry, f"Should I buy 5000 rupees of {coordinator_symbol} for a swing trade?", "SHOULD"
    )
    assert sent == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["run", "run_stream"])
async def test_an_explicit_dollar_symbol_reaches_downstream_over_the_coordinator(
    stub, registry_factory, _guard, entry
):
    registry_factory()
    from agents.orchestrator_agent import OrchestratorAgent

    stub.reply_text = "INTENT: swing_trade\nTICKER: RELIANCE\nREASON: stub"
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    sent = await _ticker_sent_downstream(orchestrator, entry, "Is $HDFCBANK a good swing trade", "HDFCBANK")
    assert sent == orchestrator.ticker_resolver.normalize("HDFCBANK")


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["run", "run_stream"])
async def test_a_stop_word_from_the_coordinator_does_not_reach_downstream(stub, registry_factory, _guard, entry):
    registry_factory()
    from agents.orchestrator_agent import OrchestratorAgent

    stub.reply_text = "INTENT: swing_trade\nTICKER: BUY\nREASON: stub"
    orchestrator = OrchestratorAgent(mcp_client=MagicMock(), chat_manager=MagicMock())
    sent = await _ticker_sent_downstream(orchestrator, entry, "Buy 30000 rupees of RELIANCE now", "RELIANCE")
    assert sent == "RELIANCE"


# --- round 1 (O3): /agent/analyze uses the shared extractor ----------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, expected",
    [("What should I buy?", None), ("Analyze RELIANCE for me", "RELIANCE"), ("How is $INFY doing", "INFY")],
)
async def test_agent_analyze_route_uses_the_shared_extractor(monkeypatch, query, expected):
    from routes import chat_routes
    from agents import orchestrator_agent

    captured = {}

    class FakeOrchestrator:
        def __init__(self, **_):
            pass

        async def run(self, **kwargs):
            captured.update(kwargs)
            return {"content": "ok"}

    monkeypatch.setattr(orchestrator_agent, "OrchestratorAgent", FakeOrchestrator)
    monkeypatch.setattr(chat_routes, "require_chat_registry", lambda: None)
    monkeypatch.setattr(
        chat_routes, "state", SimpleNamespace(mcp_client=SimpleNamespace(session=True), chat_manager=MagicMock())
    )
    request = chat_routes.AnalyzeRequest(query=query)

    assert await chat_routes.analyze_portfolio(request) == {"messages": [], "final_answer": "ok"}
    assert captured["ticker"] == expected
