"""
Test script to verify LM Studio nemotron-3-nano usage.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent.absolute() / 'backend'))

from agents.llm_factory import LLMFactory
from lm_studio_client import LMStudioClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TestNemotron")

async def test_nemotron():
    model_name = "nemotron-3-nano"
    
    logger.info(f"Testing model: {model_name}")
    
    try:
        # 1. Direct Client Check
        client = LMStudioClient()
        connected = await client.check_connection()
        logger.info(f"LM Studio Connected: {connected}")
        
        if not connected:
            logger.error("❌ LM Studio not running at http://127.0.0.1:1234")
            return

        loaded = await client.list_loaded_models()
        logger.info(f"Currently loaded models: {loaded}")
        
        # 2. Factory Check
        logger.info(f"Attempting to create LLM for {model_name} via Factory...")
        llm = await LLMFactory.create_llm(model_name)
        
        if llm:
            logger.info(f"✅ Successfully created LLM of type: {type(llm).__name__}")
            if hasattr(llm, "active_model_id"):
                logger.info(f"Active Model ID: {llm.active_model_id}")
            
            # 3. Simple Generation Test
            logger.info("Testing simple generation...")
            if hasattr(llm, "chat"):
                resp = await llm.chat(messages=[{"role": "user", "content": "Say hello!"}])
                logger.info(f"Response: {resp.get('content')}")
            else:
                from langchain_core.messages import HumanMessage
                resp = await llm.ainvoke([HumanMessage(content="Say hello!")])
                logger.info(f"Response: {resp.content}")
        else:
            logger.error("❌ Factory returned None")
            
    except Exception as e:
        logger.error(f"❌ Test failed: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(test_nemotron())
