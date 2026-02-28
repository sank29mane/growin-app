import sys
import os
import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure backend is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# We rely on conftest.py for basic mocking of heavy dependencies.
# If we need specific mocks for this file, we should do it in setUp.

from agents.coordinator_agent import CoordinatorAgent
from market_context import MarketContext

class TestCoordinatorFixes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Mock MCP client
        self.mock_mcp = MagicMock()
        self.mock_mcp.call_tool = AsyncMock(return_value=[])
        
        # Initialize Coordinator
        # We patch LLMFactory to avoid loading real models
        with patch('agents.llm_factory.LLMFactory.create_llm', new_callable=AsyncMock) as mock_factory:
            mock_llm = MagicMock()
            mock_llm.ainvoke = AsyncMock(return_value=MagicMock(content="INTENT: price_check\nTICKER: AAPL\nREASON: Test"))
            mock_factory.return_value = mock_llm
            self.coordinator = CoordinatorAgent(mcp_client=self.mock_mcp)

    async def test_ticker_fix_alphanumeric(self):
        """Test that tickers with dots are preserved (e.g. VOD.L)"""
        # _attempt_ticker_fix is private but we can test it directly or via process_query
        fixed = await self.coordinator._attempt_ticker_fix("VOD.L")
        self.assertEqual(fixed, "VOD.L")
        
        fixed2 = await self.coordinator._attempt_ticker_fix("VOD.")
        self.assertEqual(fixed2, "VOD")

    async def test_ticker_fix_malformed(self):
        """Test that malformed tickers are cleaned"""
        fixed = await self.coordinator._attempt_ticker_fix("AAPL$")
        self.assertEqual(fixed, "AAPL")

    async def test_ticker_resolution_tier2(self):
        """Test Tier 2 resolution via search_instruments tool"""
        # Configure tool response
        import json
        self.mock_mcp.call_tool.return_value = MagicMock(content=[
            MagicMock(text=json.dumps([
                {"ticker": "LLOY", "name": "Lloyds Banking Group"},
                {"ticker": "AAPL", "name": "Apple Inc"}
            ]))
        ])
        
        # Test resolution for name
        resolved = await self.coordinator._resolve_ticker_via_search("Lloyds")
        # Note: Depending on ticker_utils, it might become LLOY.L
        self.assertTrue(resolved.startswith("LLOY"))

    async def test_coordinator_self_correction_flow(self):
        """Test that coordinator escalates to Tier 2 on specialist failure"""
        # Mock QuantAgent to fail initially
        mock_quant = MagicMock()
        mock_quant.config.name = "QuantAgent"
        # First call fails with "not found", second succeeds
        fail_res = MagicMock(success=False, error="Ticker AAPL_WRONG not found", agent_name="QuantAgent", latency_ms=100)
        success_res = MagicMock(success=True, data={"rsi": 50}, agent_name="QuantAgent", latency_ms=100)
        
        mock_quant.execute = AsyncMock(side_effect=[fail_res, success_res])
        self.coordinator.quant_agent = mock_quant
        
        # Mock Tier 2 resolution to return AAPL
        with patch.object(self.coordinator, '_resolve_ticker_via_search', new_callable=AsyncMock) as mock_search:
            mock_search.return_value = "AAPL"
            
            # Execute specialist via coordinator helper
            result = await self.coordinator._run_specialist(mock_quant, {"ticker": "AAPL_WRONG"})
            
            self.assertTrue(result.success)
            self.assertEqual(mock_search.call_count, 1)
            self.assertEqual(mock_quant.execute.call_count, 2)

    async def test_ticker_normalization_integration(self):
        """Verify that ticker normalization is applied during processing"""
        # Patch the source of normalize_ticker since it's imported locally
        with patch('utils.ticker_utils.normalize_ticker') as mock_norm:
            mock_norm.return_value = "VOD.L"
            
            # We need to mock _classify_intent to avoid LLM call
            with patch.object(self.coordinator, '_classify_intent', new_callable=AsyncMock) as mock_classify:
                mock_classify.return_value = {"type": "price_check", "primary_ticker": "VOD"}
                
                # Mock data fabrication
                with patch.object(self.coordinator.data_fabricator, 'fabricate_context', new_callable=AsyncMock) as mock_fab:
                    mock_fab.return_value = MagicMock(ticker="VOD.L", user_context={}, agents_failed=[])
                    
                    await self.coordinator.process_query("Price of VOD")
                    
                    # Verify normalization was called
                    mock_norm.assert_called()

if __name__ == '__main__':
    unittest.main()
