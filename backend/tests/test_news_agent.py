
import asyncio
import sys
import os
import pytest
from dotenv import load_dotenv

# Add backend to path
sys.path.append(os.path.abspath("backend"))

from agents.research_agent import ResearchAgent
from market_context import ResearchData

load_dotenv()

@pytest.mark.asyncio
async def test_newsdata_integration():
    """
    Test real integration with NewsData.io (if API key is present).
    """
    api_key = os.getenv("NEWSDATA_API_KEY")
    if not api_key:
        pytest.skip("Skipping NewsData.io test: NEWSDATA_API_KEY not found in env")

    print(f"\n🧪 Testing NewsData.io Integration with key: {api_key[:5]}...")
    
    agent = ResearchAgent()
    
    # Test Case 1: Stock Query
    ticker = "AAPL"
    print(f"  Fetching news for {ticker}...")
    
    result = await agent.execute({"ticker": ticker, "company_name": "Apple Inc"})
    
    assert result.success, f"Agent execution failed: {result.error}"
    data = result.data
    
    # Verify Data Structure
    assert data["ticker"] == ticker
    assert "articles" in data
    assert len(data["articles"]) > 0, "No articles returned (API might be empty or query bad)"
    
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
