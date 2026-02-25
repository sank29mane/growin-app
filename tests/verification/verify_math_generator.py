
import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch, AsyncMock
from backend.agents.math_generator_agent import MathGeneratorAgent
from backend.schemas import MathScriptRequest

class TestMathGeneratorAgent(unittest.IsolatedAsyncioTestCase):
    async def test_analyze_success(self):
        # Mocking the LLM response
        mock_content = json.dumps({
            "script": "import mlx.core as mx
def calculate(): return mx.array([1, 2, 3])",
            "explanation": "Test script using MLX"
        })
        
        # Create agent
        agent = MathGeneratorAgent()
        
        # Mock ainvoke
        with patch.object(agent.llm, 'ainvoke', new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = MagicMock(content=mock_content)
            
            # Prepare request
            request_data = {
                "query": "Calculate sum of 1, 2, 3 using MLX",
                "context_data": {},
                "required_stats": ["sum"]
            }
            
            # Run analysis
            # We call execute because it handles latency and telemetry (though it might fail if telemetry is not set up)
            # Actually, let's call analyze directly to avoid side effects of BaseAgent.execute
            response = await agent.analyze(request_data)
            
            # Assertions
            self.assertTrue(response.success)
            self.assertEqual(response.data["explanation"], "Test script using MLX")
            self.assertIn("import mlx.core as mx", response.data["script"])
            self.assertEqual(response.data["engine_requirement"], "npu")
            
    async def test_analyze_invalid_json(self):
        agent = MathGeneratorAgent()
        
        with patch.object(agent.llm, 'ainvoke', new_callable=AsyncMock) as mock_invoke:
            mock_invoke.return_value = MagicMock(content="This is not JSON")
            
            request_data = {
                "query": "Fail me",
                "context_data": {},
                "required_stats": []
            }
            
            response = await agent.analyze(request_data)
            
            self.assertTrue(response.success) # It should succeed with fallback
            self.assertIn("Fallback", response.data["script"])
            self.assertIn("Error", response.data["explanation"])

if __name__ == "__main__":
    unittest.main()
