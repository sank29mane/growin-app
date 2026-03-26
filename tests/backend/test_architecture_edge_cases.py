import os
os.environ['OPENAI_API_KEY'] = 'sk-dummy-key-for-test'
from agents.llm_factory import LLMFactory
import pytest
import asyncio
import json
from unittest.mock import MagicMock, patch, AsyncMock
from decimal import Decimal
from utils.data_frayer import MarketDataFrayer
from data_engine import AlpacaClient
from agents.decision_agent import DecisionAgent
from market_context import MarketContext

@pytest.mark.asyncio
async def test_frayer_resilience_us_primary_failure():
    """Test US stock: Alpaca (Primary) fails, yfinance (Fallback) succeeds"""
    frayer = MarketDataFrayer()

    # Mock Alpaca to return empty/fail
    # Mock yfinance to return data
    with patch.object(frayer.alpaca, 'get_historical_bars', new_callable=AsyncMock) as mock_alpaca, \
         patch.object(frayer, '_fetch_yfinance_fallback', new_callable=AsyncMock) as mock_yf:

        mock_alpaca.return_value = {"bars": []}
        mock_yf.return_value = {"bars": [{"t": 1000, "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "v": 100}]*10}

        result = await frayer.fetch_frayed_bars("AAPL", limit=10)

        assert len(result) == 1
        assert result[0]["_p"] == "YFinance"
        mock_alpaca.assert_called_once()
        mock_yf.assert_called_once()

@pytest.mark.asyncio
async def test_frayer_resilience_uk_primary_failure():
    """Test UK stock: Finnhub (Primary) fails, yfinance (Fallback) succeeds"""
    frayer = MarketDataFrayer()

    # Mock Finnhub failure
    with patch("data_engine.get_finnhub_client") as mock_get_finnhub, \
         patch.object(frayer, '_fetch_yfinance_fallback', new_callable=AsyncMock) as mock_yf:

        mock_finnhub = MagicMock()
        mock_finnhub.get_historical_bars = AsyncMock(return_value={"bars": []})
        mock_get_finnhub.return_value = mock_finnhub

        mock_yf.return_value = {"bars": [{"t": 1000, "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "v": 100}]*10}

        result = await frayer.fetch_frayed_bars("LLOY.L", limit=10)

        assert len(result) == 1
        assert result[0]["_p"] == "YFinance"
        mock_yf.assert_called_once()

@pytest.mark.asyncio
async def test_decision_agent_agentic_loop_tool_execution():
    """Test DecisionAgent correctly identifies and executes a tool call in its loop"""
    # Need to patch LLMFactory to avoid actual initialization
    with patch.object(LLMFactory, 'create_llm', new_callable=AsyncMock) as mock_factory, \
         patch("utils.skill_loader.get_skill_loader") as mock_sl, \
         patch("agents.decision_agent.extract_tool_calls") as mock_ext:
        mock_sl.return_value.get_relevant_skills.return_value = ""

        # Tool call extraction mock
        from agents.decision_agent import ToolCall
        mock_ext.side_effect = [[ToolCall(tool_name="docker_run_python", arguments={"script": "print(2+2)", "engine": "npu"})], []]
        mock_llm = MagicMock()
        mock_factory.return_value = mock_llm

        agent = DecisionAgent(model_name="native-mlx")
        await agent._initialize_llm()

        # Turn 1: LLM outputs a tool call
        # Turn 2: LLM outputs final answer based on tool result
        tool_call_content = '[TOOL:docker_run_python({"script": "print(2+2)", "engine": "npu"})]'
        final_answer = "The result of the calculation is 4."

        mock_llm.chat = AsyncMock(side_effect=[
            {"content": tool_call_content},
            {"content": final_answer}
        ])

        mock_context = MarketContext(query="Analyze generic item 2+2", intent="analytical")
        import app_context
        app_context.state._rag_manager = None

        with patch("app_context.state.mcp_client.call_tool", new_callable=AsyncMock) as mock_tool:
            mock_tool.return_value = MagicMock(content=[MagicMock(text='{"stdout": "4", "exit_code": 0}')])

            result = await agent.make_decision(mock_context, "Analyze generic item 2+2")

            # The result could be a dictionary with 'content', or a string.
            if isinstance(result, dict) and 'content' in result:
                assert "4" in result['content']
            else:
                assert "4" in result
            assert mock_tool.call_count == 1
            # Verify it used the NPU engine as instructed in persona
            name, args = mock_tool.call_args[0]
            assert name == "docker_run_python"
            assert args["engine"] == "npu"

@pytest.mark.asyncio
async def test_sandbox_security_timeout():
    """Test that the Docker sandbox correctly handles execution timeouts"""
    from docker_mcp_server import DockerMCPServer
    server = DockerMCPServer()

    # Mock docker client
    server.client = MagicMock()
    mock_container = MagicMock()
    server.client.containers.create.return_value = mock_container

    # Simulate timeout by making container.wait raise Exception
    mock_container.wait.side_effect = Exception("Timeout")

    # We must also mock _ensure_image to return True
    with patch.object(server, '_ensure_image', return_value=True):
        result = server.execute_script("import time; time.sleep(20)", timeout=1)

        assert result["status"] == "timeout"
        assert "timed out" in result["error"]
        mock_container.kill.assert_called_once()

@pytest.mark.asyncio
async def test_price_validation_mismatch_correction():
    """Test that DataFabricator corrects GBX/GBP mismatches (100x factor)"""
    from data_fabricator import DataFabricator
    fab = DataFabricator()

    # Mock history close at 1.0 (GBP) and current quote at 100.0 (GBX)
    with patch.object(fab, '_fetch_news_data', new_callable=AsyncMock), \
         patch.object(fab, '_fetch_social_data', new_callable=AsyncMock), \
         patch("utils.data_frayer.get_data_frayer") as mock_frayer_get:

        mock_frayer = mock_frayer_get.return_value
        mock_frayer.fetch_frayed_bars = AsyncMock(return_value=[{"t": 1, "o": 1, "h": 1, "l": 1, "c": 1.0, "v": 1}])

        # Alpaca mock for US quote
        fab.alpaca.get_real_time_quote = AsyncMock(return_value={"current_price": 100.0})

        # Test 100x correction
        res = await fab._fetch_price_data("AAPL") # US ticker -> Alpaca quote

        # 100.0 (quote) / 1.0 (history) = 100 -> Corrects to 1.0
        assert res.current_price == Decimal('1.0')
