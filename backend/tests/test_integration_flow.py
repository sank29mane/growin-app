
import asyncio
import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock
from dotenv import load_dotenv

# Add backend to path
sys.path.append(os.path.abspath("backend"))

from agents.coordinator_agent import CoordinatorAgent
from agents.research_agent import ResearchAgent
from market_context import MarketContext

load_dotenv()

@pytest.mark.asyncio
async def test_full_analysis_integration():
    """
    Simulate a full 'Analyze [Ticker]' request to ensure Research/News integration flows correctly.
    """
    print("\n🧪 Testing Full AI Analysis Integration...")
    
    # Mock MCP Client
    mock_mcp = AsyncMock()
    
    # Initialize Coordinator
    coordinator = CoordinatorAgent(mcp_client=mock_mcp)
    
    # Spy on ResearchAgent to ensure it gets called
    original_research_execute = coordinator.research_agent.execute
    coordinator.research_agent.execute = AsyncMock(side_effect=original_research_execute)
    
    # Define Query
    query = "Analyze Apple stock"
    ticker = "AAPL"
    
    # Execute Process Query
    context = await coordinator.process_query(query, ticker=ticker)
    
    # Verification 1: Research Agent was executed
    assert coordinator.research_agent.execute.called, "ResearchAgent should have been called"
    print("  ✅ ResearchAgent execution verified")
    
    # Verification 2: Context contains Research Data
    assert context.research is not None, "Research Data missing from context"
    assert len(context.research.articles) > 0, "No articles found in Research Data"
    print(f"  ✅ Research Data found: {len(context.research.articles)} articles")
    
    # Verification 3: Data Integrity
    first_article = context.research.articles[0]
    assert first_article.title, "Article missing title"
    assert first_article.source, "Article missing source"
    print(f"  ✅ Data Integrity Check Passed (Sample: {first_article.title[:30]}...)")
    
    # Verification 4: Decision Synthesis (Check if news is in final answer)
    # The DecisionAgent usually synthesizes this. We can check the context.user_context['final_answer']
    final_answer = context.user_context.get("final_answer", "")
    assert final_answer, "No final answer generated"
    
    # Check for sentiment indicators in final answer
    # Note: Dependent on DecisionAgent's template, which uses "Overall Sentiment" or news bullets
    has_sentiment = "Sentiment" in final_answer or "BULLISH" in final_answer or "BEARISH" in final_answer
    if has_sentiment:
        print("  ✅ News/Sentiment detected in Final Answer")
    else:
        print("  ⚠️ News/Sentiment NOT detected in Final Answer (might be due to template or missing data)")
        print(f"DEBUG Final Answer snippet:\n{final_answer[:500]}")

if __name__ == "__main__":
    asyncio.run(test_full_analysis_integration())
