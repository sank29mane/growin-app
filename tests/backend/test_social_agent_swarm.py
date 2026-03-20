import unittest
import sys
from unittest.mock import patch, MagicMock

# Create a proper package mock for chromadb
class MockChromaDB:
    pass

class MockUtils:
    pass

mock_chromadb = MagicMock()
mock_chromadb.utils = MagicMock()
mock_chromadb.utils.embedding_functions = MagicMock()

sys.modules['chromadb'] = mock_chromadb
sys.modules['chromadb.utils'] = mock_chromadb.utils
sys.modules['chromadb.utils.embedding_functions'] = mock_chromadb.utils.embedding_functions

sys.modules['sentence_transformers'] = MagicMock()
sys.modules['cache_manager'] = MagicMock()
sys.modules['app_logging'] = MagicMock()
sys.modules['telemetry_store'] = MagicMock()
sys.modules['alpaca'] = MagicMock()
sys.modules['alpaca.trading'] = MagicMock()
sys.modules['alpaca.trading.client'] = MagicMock()

mock_sklearn = MagicMock()
mock_sklearn.preprocessing = MagicMock()
sys.modules['sklearn'] = mock_sklearn
sys.modules['sklearn.preprocessing'] = mock_sklearn.preprocessing

mock_scipy = MagicMock()
sys.modules['scipy'] = mock_scipy
sys.modules['scipy.signal'] = MagicMock()
sys.modules['scipy.stats'] = MagicMock()
sys.modules['scipy.optimize'] = MagicMock()

mock_mcp = MagicMock()
mock_mcp.client = MagicMock()
mock_mcp.client.stdio = MagicMock()
mock_mcp.client.sse = MagicMock()

sys.modules['mcp'] = mock_mcp
sys.modules['mcp.client'] = mock_mcp.client
sys.modules['mcp.client.stdio'] = mock_mcp.client.stdio
sys.modules['mcp.client.sse'] = mock_mcp.client.sse
sys.modules['yfinance'] = MagicMock()

from agents.social_agent import SocialAgent
from agents.social_swarm import RedditMicroAgent, TwitterMicroAgent
from agents.social_swarm.base_micro import MicroAgentResponse
from decimal import Decimal
import asyncio

class TestSocialAgentSwarm(unittest.IsolatedAsyncioTestCase):
    
    @patch('agents.social_agent.os.getenv')
    async def test_social_agent_no_key(self, mock_getenv):
        mock_getenv.return_value = None
        agent = SocialAgent()
        res = await agent.analyze({"ticker": "AAPL"})
        # Fail soft on generic errors is False for Tavily API Key missing
        self.assertFalse(res.success)
        self.assertEqual(res.data['ticker'], "AAPL")
        self.assertEqual(res.error, "Tavily API key missing")

    @patch('agents.social_agent.os.getenv')
    async def test_social_agent_with_data(self, mock_getenv):
        mock_getenv.return_value = "fake_key"
        agent = SocialAgent()
        
        # Patch the swarm instances manually
        mock_reddit = MagicMock()
        async def fetch_reddit(ticker, company_name):
            return MicroAgentResponse(
                source="Reddit",
                sentiment_score=Decimal("0.5"),
                mention_volume=10,
                top_discussions=["Reddit disc 1"],
                success=True
            )
        mock_reddit.fetch_data = fetch_reddit
        
        mock_twitter = MagicMock()
        async def fetch_twitter(ticker, company_name):
            return MicroAgentResponse(
                source="Twitter",
                sentiment_score=Decimal("0.1"),
                mention_volume=5,
                top_discussions=["Twitter disc 1"],
                success=True
            )
        mock_twitter.fetch_data = fetch_twitter
        
        agent.swarm = [mock_reddit, mock_twitter]
        
        res = await agent.analyze({"ticker": "AAPL"})
        
        self.assertTrue(res.success)
        self.assertEqual(res.data['mention_volume'], "HIGH")
        # 10 * 0.5 + 5 * 0.1 = 5.5 / 15 = 0.3666...
        self.assertAlmostEqual(float(res.data['sentiment_score']), 0.36666, places=4)
        self.assertEqual(res.data['sentiment_label'], "BULLISH")
        self.assertIn("Reddit disc 1", res.data['top_discussions'])
        self.assertIn("Twitter disc 1", res.data['top_discussions'])

if __name__ == "__main__":
    unittest.main()
