
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock
from agents.coordinator_agent import CoordinatorAgent
from agents.base_agent import BaseAgent, AgentResponse

async def test_escalation_path():
    print("=== Testing Coordinator Escalation Path ===")
    
    # 1. Setup Mock MCP Client
    mock_mcp = AsyncMock()
    # Mock search_instruments tool
    mock_mcp.call_tool.return_value = MagicMock(content=[
        MagicMock(text=json.dumps([
            {"ticker": "LLOY", "name": "Lloyds Banking Group PLC"}
        ]))
    ])
    
    # 2. Setup Coordinator
    # We use a mock LLM to avoid real API calls in this test
    coordinator = CoordinatorAgent(mock_mcp, model_name="mock-model")
    coordinator.llm = AsyncMock()
    coordinator.llm.ainvoke.return_value = MagicMock(content=json.dumps({
        "type": "price_check",
        "needs": ["quant"],
        "reason": "Test escalation"
    }))
    
    # 3. Setup Failing Specialist
    mock_agent = AsyncMock(spec=BaseAgent)
    mock_agent.config = MagicMock()
    mock_agent.config.name = "QuantAgent"
    
    # First call fails with 404, second call (retry) succeeds
    mock_agent.execute.side_effect = [
        AgentResponse(agent_name="QuantAgent", success=False, data={}, error="404 Not Found: LLOY1", latency_ms=100),
        AgentResponse(agent_name="QuantAgent", success=True, data={"price": 45.0}, error=None, latency_ms=150)
    ]
    
    # 4. Trigger Resolution logic manually or via _run_specialist
    print("Running _run_specialist with failing ticker 'LLOY1'...")
    context = {"ticker": "LLOY1"}
    
    # We need to monkeypatch the agent in the coordinator if we want to test process_query,
    # but testing _run_specialist directly is cleaner.
    result = await coordinator._run_specialist(mock_agent, context)
    
    print(f"Final Result Success: {result.success}")
    print(f"Final Data: {result.data}")
    
    # Verify Tier 2 was called
    mock_mcp.call_tool.assert_called_with("search_instruments", {"query": "LLOY1"})
    print("✅ Tier 2 escalation verified: search_instruments called.")
    
    if result.success:
        print("✅ Escalation recovery verified: Agent succeeded on retry.")

if __name__ == "__main__":
    asyncio.run(test_escalation_path())
