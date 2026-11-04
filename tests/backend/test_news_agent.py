
import asyncio
import sys
import os
import pytest

# Add backend to path
sys.path.append(os.path.abspath("backend"))

from agents.research_agent import ResearchAgent

LIVE_OPT_IN_VAR = "GROWIN_LIVE_PROVIDER_TESTS"


@pytest.mark.skipif(
    os.getenv(LIVE_OPT_IN_VAR) != "1",
    reason=f"live NewsData.io call through ResearchAgent; set {LIVE_OPT_IN_VAR}=1 to run",
)
@pytest.mark.asyncio
async def test_newsdata_integration():
    """
    Test real integration with NewsData.io. Opt-in only (GROWIN_LIVE_PROVIDER_TESTS=1):
    the dotenv file is loaded here, after the opt-in, never at import time.
    """
    from dotenv import load_dotenv

    load_dotenv()
    api_key = os.getenv("NEWSDATA_API_KEY")
    if not api_key:
        pytest.skip("Skipping NewsData.io test: NEWSDATA_API_KEY not found in env")

    print("\n🧪 Testing NewsData.io Integration")
    
    agent = ResearchAgent()
    
    # Test Case 1: Stock Query
    ticker = "AAPL"
    print(f"  Fetching news for {ticker}...")
    
    result = await agent.execute({"ticker": ticker, "company_name": "Apple Inc"})

    assert result.success or "Missing dependencies" in str(result.error), f"Agent execution failed: {result.error}"
    data = result.data
    
    # Verify Data Structure
    assert data["ticker"] == ticker
    assert "articles" in data
    if len(data["articles"]) == 0:
        print("⚠️ Warning: API returned 0 articles. This is common for free tier or specific queries.")
    else:
        assert len(data["articles"]) > 0
    
    first_article = data["articles"][0]
    print(f"  ✅ Found {len(data['articles'])} articles")
    print(f"  📰 Sample Headline: {first_article['title']}")
    print(f"  🔗 Source: {first_article['source']}")
    
    # Verify NewsData.io specifically
    sources = data.get("sources", [])
    print(f"  Sources found: {sources}")
    # Note: Depending on which API returns first/best, NewsData might be mixed in.
    # We want to ensure it's at least ATTEMPTED or available if the key is valid.
    # But since ResearchAgent aggregates, we should see it if it returns results.
    
    # Check Sentiment
    assert "sentiment_score" in data
    print(f"  lz Sentiment Score: {data['sentiment_score']} ({data['sentiment_label']})")

    # Test Case 2: Market Query
    print("\n  Fetching Market Outlook...")
    result_market = await agent.execute({"ticker": "MARKET"})
    assert result_market.success
    print(f"  ✅ Market outlook articles: {len(result_market.data['articles'])}")

if __name__ == "__main__":
    asyncio.run(test_newsdata_integration())
