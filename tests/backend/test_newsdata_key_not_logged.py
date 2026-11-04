"""The newsdata.io key travels in the X-ACCESS-KEY header and nowhere else.

Found in a Phase 67 chat run: a 422 from newsdata raised an httpx error whose text
held the full request URL, `apikey=<key>` included, and it was logged with a
traceback. A canary key stands in for the real one. The HTTP layer is an
httpx.MockTransport, so nothing leaves the process.
"""

import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from resilience import CircuitBreaker, CircuitState
from utils.http_client import agent_http_client
from utils import news_client
from utils.news_client import NEWSDATA_KEY_HEADER, NewsDataIOClient

CANARY = "CANARY-NEWSDATA-KEY-123"

# What the failing production call looked like: smart-query params, no key.
SMART_PARAMS = {
    "q": "TCS stock",
    "qInTitle": "TCS",
    "domain": "reuters.com",
    "language": "en",
    "excludefield": "ai_summary",
}


@pytest.fixture
def research_agent(monkeypatch):
    monkeypatch.setenv("NEWSDATA_API_KEY", CANARY)
    monkeypatch.delenv("NEWSAPI_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    from agents.research_agent import ResearchAgent

    agent = ResearchAgent()
    assert agent.newsdata_key == CANARY
    # A private breaker so repeated failures here cannot open the shared one.
    agent._newsdata_cb = CircuitBreaker(name="newsdata_research_test", failure_threshold=1000)
    agent._generate_smart_query = AsyncMock(return_value=dict(SMART_PARAMS))
    return agent


@pytest.fixture
def news_client_instance():
    news_client.newsdata_circuit._state = CircuitState.CLOSED
    news_client.newsdata_circuit._failure_count = 0
    yield NewsDataIOClient(api_key=CANARY)
    news_client.newsdata_circuit._state = CircuitState.CLOSED
    news_client.newsdata_circuit._failure_count = 0


@pytest.fixture
def http(monkeypatch):
    """Install a MockTransport behind agent_http_client. Returns (set_handler, requests)."""
    requests: list[httpx.Request] = []
    state = {"handler": lambda request: httpx.Response(200, json={"results": []})}

    def _route(request: httpx.Request):
        requests.append(request)
        return state["handler"](request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_route))
    monkeypatch.setattr(agent_http_client, "_client", client)

    def set_handler(fn):
        state["handler"] = fn

    yield set_handler, requests


def _log_text(caplog) -> str:
    """Everything a log reader could see: message, args and formatted traceback."""
    out = []
    fmt = logging.Formatter()
    for rec in caplog.records:
        out.append(rec.getMessage())
        out.append(fmt.format(rec))
        out.append(repr(rec.args))
    return "\n".join(out)


def _assert_key_only_in_header(requests, caplog):
    assert requests, "no request was made"
    for req in requests:
        assert req.headers[NEWSDATA_KEY_HEADER] == CANARY
        assert CANARY not in str(req.url)
        assert "apikey" not in req.url.query.decode().lower()
    assert CANARY not in _log_text(caplog)


def _status_handler(status):
    return lambda request: httpx.Response(status, json={"results": {"message": "bad"}}, request=request)


def _raising_handler(exc_type):
    def handler(request):
        # Worst case: the transport error text itself carries the key.
        raise exc_type(f"boom for {request.url} key={CANARY}", request=request)

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [422, 429, 500])
async def test_research_agent_http_error_never_logs_the_key(research_agent, http, caplog, status):
    set_handler, requests = http
    set_handler(_status_handler(status))
    caplog.set_level(logging.DEBUG)

    assert await research_agent._fetch_newsdata("TCS", "Tata Consultancy") == []

    _assert_key_only_in_header(requests, caplog)
    assert f"HTTP {status}" in _log_text(caplog)
    # The smart-query params reached the wire intact apart from the key.
    assert requests[0].url.params["qInTitle"] == "TCS"
    assert requests[0].url.params["excludefield"] == "ai_summary"


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_type", [httpx.ConnectError, httpx.ReadTimeout])
async def test_research_agent_transport_error_never_logs_the_key(research_agent, http, caplog, exc_type):
    set_handler, requests = http
    set_handler(_raising_handler(exc_type))
    caplog.set_level(logging.DEBUG)

    assert await research_agent._fetch_newsdata("TCS", "Tata Consultancy") == []

    _assert_key_only_in_header(requests, caplog)
    assert exc_type.__name__ in _log_text(caplog)


@pytest.mark.asyncio
async def test_research_agent_fallback_query_sends_no_key_in_url(research_agent, http, caplog):
    set_handler, requests = http
    set_handler(_status_handler(422))
    research_agent._generate_smart_query = AsyncMock(return_value=None)  # force the fallback params
    caplog.set_level(logging.DEBUG)

    await research_agent._fetch_newsdata("AAPL", "Apple")
    await research_agent._fetch_newsdata("MARKET", "")

    assert len(requests) == 2
    _assert_key_only_in_header(requests, caplog)


@pytest.mark.asyncio
async def test_research_agent_regulatory_path_uses_the_header(research_agent, http, caplog):
    set_handler, requests = http
    set_handler(_status_handler(422))
    caplog.set_level(logging.DEBUG)

    assert await research_agent._fetch_regulatory_news("VOD.L", "Vodafone") == []
    set_handler(_raising_handler(httpx.ConnectError))
    assert await research_agent._fetch_regulatory_news("VOD.L", "Vodafone") == []

    assert len(requests) == 2
    _assert_key_only_in_header(requests, caplog)


@pytest.mark.asyncio
async def test_exception_that_escapes_the_http_helper_carries_no_key(research_agent, http):
    set_handler, requests = http
    set_handler(_status_handler(422))

    with pytest.raises(httpx.HTTPStatusError) as caught:
        await research_agent._newsdata_get("https://newsdata.io/api/1/latest", dict(SMART_PARAMS))

    assert CANARY not in str(caught.value)
    assert CANARY not in repr(caught.value)
    assert requests[0].headers[NEWSDATA_KEY_HEADER] == CANARY


@pytest.mark.asyncio
async def test_a_key_smuggled_into_params_is_stripped_from_the_url(research_agent, http, caplog):
    set_handler, requests = http
    caplog.set_level(logging.DEBUG)

    await research_agent._newsdata_get(
        "https://newsdata.io/api/1/latest", {"q": "x", "apikey": CANARY, "APIKEY": CANARY}
    )

    _assert_key_only_in_header(requests, caplog)


@pytest.mark.asyncio
async def test_smart_query_generation_neither_adds_nor_logs_the_key(research_agent, monkeypatch, caplog):
    from agents import research_agent as module

    del research_agent._generate_smart_query  # use the real method, mock the model call
    monkeypatch.setattr(
        module,
        "generate_news_query",
        AsyncMock(return_value=module.NewsDataQueryParams(q="TCS", qInTitle="TCS", domain="reuters.com")),
    )
    caplog.set_level(logging.DEBUG)

    params = await research_agent._generate_smart_query("News for Tata (TCS)")

    assert "apikey" not in {k.lower() for k in params}
    assert CANARY not in str(params)
    assert CANARY not in _log_text(caplog)
    assert CANARY not in str(research_agent._basic_news_query("x"))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [422, 500])
async def test_news_client_http_error_never_logs_the_key(news_client_instance, http, caplog, status):
    set_handler, requests = http
    set_handler(_status_handler(status))
    caplog.set_level(logging.DEBUG)

    assert await news_client_instance.fetch_latest_news("TCS RNS") == []

    _assert_key_only_in_header(requests, caplog)
    assert f"HTTP {status}" in _log_text(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_type", [httpx.ConnectError, httpx.ReadTimeout])
async def test_news_client_transport_error_never_logs_the_key(news_client_instance, http, caplog, exc_type):
    set_handler, requests = http
    set_handler(_raising_handler(exc_type))
    caplog.set_level(logging.DEBUG)

    assert await news_client_instance.fetch_latest_news("TCS RNS") == []

    _assert_key_only_in_header(requests, caplog)


@pytest.mark.asyncio
async def test_news_client_success_sends_key_in_header_and_parses_results(news_client_instance, http, caplog):
    set_handler, requests = http
    set_handler(
        lambda request: httpx.Response(
            200, json={"results": [{"title": "T", "description": "D", "link": "https://x", "pubDate": "2026-11-05"}]}
        )
    )
    caplog.set_level(logging.DEBUG)

    articles = await news_client_instance.fetch_regulatory_news("VOD.L")

    assert [a["title"] for a in articles] == ["T"]
    assert requests[0].url.params["q"] == "VOD.L RNS"
    _assert_key_only_in_header(requests, caplog)


@pytest.mark.asyncio
async def test_missing_key_makes_no_request(http):
    set_handler, requests = http
    client = NewsDataIOClient(api_key=None)
    client.api_key = None
    news_client.newsdata_circuit._state = CircuitState.CLOSED
    assert await client.fetch_latest_news("x") == []
    assert requests == []


def test_error_summary_scrubs_the_key_from_non_http_errors():
    text = news_client.newsdata_error_summary(ValueError(f"bad {CANARY} " + "x" * 500), CANARY)
    assert CANARY not in text
    assert len(text) < 300
