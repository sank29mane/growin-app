import asyncio
import json
import logging
import sys
import os
from unittest.mock import MagicMock
from typing import Dict, Any, List, Optional

# Set up paths to import from backend
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../backend')))

# Mock the status_manager before importing coordinator_agent
status_manager_mock = MagicMock()
sys.modules["status_manager"] = status_manager_mock

# Mock cache_manager
cache_manager_module = MagicMock()
cache_mock = MagicMock()
cache_mock.get.return_value = None # Ensure cache miss returns None, not a Mock
cache_manager_module.cache = cache_mock
sys.modules["cache_manager"] = cache_manager_module

from coordinator_agent import CoordinatorAgent
from base_agent import AgentResponse, BaseAgent, AgentConfig
import trading212_mcp_server
print(f"DEBUG: Using trading212_mcp_server from: {trading212_mcp_server.__file__}")
from trading212_mcp_server import normalize_ticker

print(f"DEBUG: normalize_ticker('MAG5') = '{normalize_ticker('MAG5')}'")
print(f"DEBUG: normalize_ticker('3GLD') = '{normalize_ticker('3GLD')}'")

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MockMcpClient:
    async def call_tool(self, name, arguments):
        if name == "search_instruments":
            query = arguments.get("query", "").upper()
            if "MAGNIFICENT" in query or "MAG 7" in query:
                return [{"ticker": "MAG5", "name": "Leverage Shares 5x Long Mag 7"}]
            if "GOLD" in query and "LEVERAGED" in query:
                return [{"ticker": "3GLD", "name": "WisdomTree Physical Gold 3x"}]
            if "TESLA" in query:
                return [{"ticker": "TSLA", "name": "Tesla Inc"}]
        return []

class FailingAgent(BaseAgent):
    def __init__(self, name="FailingAgent"):
        super().__init__(AgentConfig(name=name, role="Tester", description="Always fails first time"))
        self.call_count = 0

    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        self.call_count += 1
        ticker = context.get("ticker", "")
        
        # Succeed only if ticker is normalized correctly using Tier 1 or Tier 2
        if ticker in ["MAG5.L", "3GLD.L", "TSLA"]:
            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data={"resolved_ticker": ticker},
                latency_ms=10
            )
        
        # Otherwise fail with a "delisted" or "not found" error to trigger self-healing
        return AgentResponse(
            agent_name=self.config.name,
            success=False,
            data={},
            error=f"Ticker {ticker} not found or delisted",
            latency_ms=10
        )

async def test_tiered_resolution():
    print("\n--- Starting Tiered Ticker Resolution Test ---")
    
    mock_mcp = MockMcpClient()
    coordinator = CoordinatorAgent(mcp_client=mock_mcp, model_name="granite-tiny")
    
    test_cases = [
        {"input": "MAG5.L", "expected": "MAG5.L", "desc": "Tier 1: Already normalized"},
        {"input": "3GLD.L", "expected": "3GLD.L", "desc": "Tier 1: Already normalized UK"},
        {"input": "MAG5", "expected": "MAG5.L", "desc": "Tier 1 Rules: MAG5 -> MAG5.L"},
        {"input": "magnificent 7", "expected": "MAG5.L", "desc": "Tier 2 Search: 'magnificent 7' -> MAG5.L"},
        {"input": "leveraged gold", "expected": "3GLD.L", "desc": "Tier 2 Search: 'leveraged gold' -> 3GLD.L"}
    ]
    
    failing_agent = FailingAgent()
    
    for case in test_cases:
        print(f"\nTesting: {case['desc']} ('{case['input']}')")
        # For Tier 1 tests, we test normalize_ticker indirectly via agent recovery IF we pass it a messy ticker
        # But wait, run_specialist is what we're testing. 
        # coordinator.process_query calls normalize_ticker (not really, it calls _attempt_ticker_fix)
        # Specialist input is context["ticker"].
        
        context = {"ticker": case["input"]}
        
        # In actual flow, CoordinatorAgent.process_query would have already done some normalization.
        # But run_specialist is our "Self-Healing" catch-all.
        
        result = await coordinator._run_specialist(failing_agent, context)
        
        print(f"Result: Success={result.success}, Data={result.data}, Error={result.error}")
        
        if result.success and result.data.get("resolved_ticker") == case["expected"]:
            print(f"✅ PASSED: Resolved to {case['expected']}")
        else:
            print(f"❌ FAILED: Expected {case['expected']}, got {result.data.get('resolved_ticker')} (Error: {result.error})")

if __name__ == "__main__":
    asyncio.run(test_tiered_resolution())
