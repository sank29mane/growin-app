import pytest
from utils.ticker_utils import TickerResolver

@pytest.fixture
def resolver():
    return TickerResolver()

def test_normalize_us_tickers(resolver):
    assert resolver.normalize("AAPL") == "AAPL"
    assert resolver.normalize("msft") == "MSFT"
    assert resolver.normalize("$TSLA") == "TSLA"
    assert resolver.normalize("GOOGL") == "GOOGL"

def test_normalize_uk_tickers(resolver):
    assert resolver.normalize("LLOY") == "LLOY.L"
    assert resolver.normalize("VOD") == "VOD.L"
    assert resolver.normalize("BARC") == "BARC.L"
    # Special mappings
    assert resolver.normalize("BPL") == "BP.L"
    assert resolver.normalize("AZNL") == "AZN.L"

def test_normalize_leveraged_etps(resolver):
    assert resolver.normalize("3GLD") == "3GLD.L"
    assert resolver.normalize("5QQQ") == "5QQQ.L"
    assert resolver.normalize("NVD3") == "NVD3.L"

def test_normalize_t212_suffixes(resolver):
    assert resolver.normalize("AAPL_US_EQ") == "AAPL"
    assert resolver.normalize("LLOY_EQ") == "LLOY.L"
    assert resolver.normalize("BP_GB_EQ") == "BP.L"

def test_extract_tickers(resolver):
    text = "Compare AAPL and MSFT performance."
    extracted = resolver.extract(text)
    assert "AAPL" in extracted
    assert "MSFT" in extracted

    text = "Check Lloyds (LLOY) and BP."
    extracted = resolver.extract(text)
    assert "LLOY.L" in extracted
    assert "BP.L" in extracted

    text = "Is 3GLD a good buy?"
    extracted = resolver.extract(text)
    assert "3GLD.L" in extracted

@pytest.mark.asyncio
async def test_resolve_tiered(resolver):
    # Single ticker
    assert await resolver.resolve("AAPL") == "AAPL"
    assert await resolver.resolve("lloy") == "LLOY.L"

    # Query sentence
    assert await resolver.resolve("What is the price of MSFT?") == "MSFT"
    assert await resolver.resolve("Show me 3GLD chart") == "3GLD.L"
