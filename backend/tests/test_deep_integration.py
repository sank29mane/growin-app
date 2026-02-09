
import sys
import os
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

# Add backend to path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from agents.research_agent import ResearchAgent
from app_context import state

async def test_deep_integration():
    print("Testing ResearchAgent -> RAG Integration...")
    
    # Mock RAG Manager
    mock_rag = MagicMock()
    state.rag_manager = mock_rag
    
    agent = ResearchAgent()
    agent.newsdata_key = "dummy_key" # Bypass check
    
    # Mock internal methods to avoid real API calls
    # We simulate finding articles
    agent._fetch_newsapi = AsyncMock(return_value=[])
    agent._fetch_tavily = AsyncMock(return_value=[])
    # Simulate valid NewsData.io results
    agent._fetch_newsdata = AsyncMock(return_value=[
        {
            "title": "Apple hits all time high",
            "description": "Stock soars on AI news.",
            "url": "http://test.com",
            "source": {"name": "TestNews"}
        }
    ])
    
    # We need to mock AsyncMock which isn't standard in unittest.mock until py3.8+, 
    # but we can use a helper class or just patch the method to return a future.
    
    with patch.object(agent, '_fetch_newsdata', new_callable=AsyncMock) as mock_fetch:
        mock_fetch.return_value = ([
                {
                    "title": "Apple hits all time high",
                    "description": "Stock soars on AI news.",
                    "url": "http://test.com",
                    "source": {"name": "TestNews"}
                }
            ])
        
        result = await agent.analyze({"ticker": "AAPL"})
        
        if result.success:
            print("✅ Research analysis successful")
            
            # Check if RAG was called
            if mock_rag.add_document.called:
                print("✅ RAG ingestion triggered")
                call_args = mock_rag.add_document.call_args
                print(f"   Saved Content: {call_args[1].get('content')}")
                assert "Apple" in call_args[1].get('content')
            else:
                print("❌ RAG ingestion FAILED (Method not called)")
        else:
            print(f"❌ Research analysis failed: {result.error}")


if __name__ == "__main__":
    asyncio.run(test_deep_integration())
