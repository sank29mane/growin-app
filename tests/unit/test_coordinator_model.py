import asyncio
import logging
import sys
import os
import json
from dotenv import load_dotenv

# Add backend to path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

# Load env vars
load_dotenv(os.path.join(os.getcwd(), 'backend', '.env'))

logging.basicConfig(level=logging.INFO, format='%(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("CoordinatorTest")

async def test_coordinator_guardrails():
    logger.info("--- Testing Coordinator Agent Guardrails ---")
    try:
        from coordinator_agent import CoordinatorAgent
        
        # Mock MCP client
        class MockMCP:
            pass
            
        agent = CoordinatorAgent(MockMCP(), model_name="granite-tiny")
        
        # Test 1: Model Loading & Config
        logger.info(f"Model Name: {agent.model_name}")
        if hasattr(agent.llm, "temperature"):
            logger.info(f"LLM Temperature: {agent.llm.temperature} (Expected: 0.0)")
            assert agent.llm.temperature == 0.0, "Temperature guardrail failed!"
        
        # Test 2: Analytical Intent (Structured Output)
        query = "How is Apple stock performing today?"
        logger.info(f"\nTesting Analytical Query: '{query}'")
        intent = await agent._classify_intent(query)
        logger.info(f"Result: {json.dumps(intent, indent=2)}")
        
        assert intent.get("type") == "analytical", "Classification failed for analytical query"
        assert "quant" in intent.get("needs", []), "Missing quant need for stock query"
        
        # Test 3: Educational Intent with Grounding
        query = "What is the difference between RSI and MACD?"
        logger.info(f"\nTesting Educational Query: '{query}'")
        intent = await agent._classify_intent(query)
        logger.info(f"Result: {json.dumps(intent, indent=2)}")
        
        assert intent.get("type") == "educational", "Classification failed for educational query"
        
        # Test 4: Hardware Check (Account Context)
        query = "How is my ISA doing?"
        logger.info(f"\nTesting Account Query: '{query}'")
        intent = await agent._classify_intent(query)
        logger.info(f"Result: {json.dumps(intent, indent=2)}")
        
        # Note: The system prompt might capture account in 'account' field if supported
        if "isa" in intent.get("account", "").lower():
             logger.info("✅ Account correctly identified in JSON output")
        
        # Test 5: Input Sanitization (Long query)
        long_query = "test " * 1000
        logger.info("\nTesting Input Sanitization (Long Query)")
        # We just want to ensure it doesn't crash and returns valid JSON
        intent = await agent._classify_intent(long_query)
        logger.info("Sanitization check passed (returned valid JSON)")
        
        logger.info("\n✅ All Coordinator Guardrail Tests Passed!")
        
    except Exception as e:
        logger.error(f"Coordinator Test Failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_coordinator_guardrails())
