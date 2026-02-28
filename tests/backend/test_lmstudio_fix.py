import asyncio
import logging
import sys
import os

# Add backend to path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

# Configure logging to stdout
logging.basicConfig(level=logging.INFO, stream=sys.stdout)

import data_engine # Ensure dependency exists
from agents.llm_factory import LLMFactory
from lm_studio_client import LMStudioClient

async def test_lm_studio_auto():
    print("--- Testing LM Studio Auto-Detection ---")
    try:
        # This should trigger the auto-detection logic we just fixed
        client = await LLMFactory.create_llm("lmstudio-auto")
        
        if isinstance(client, LMStudioClient):
            print(f"✅ Successfully initialized LM Studio Client")
            print(f"✅ Active Model ID: {client.active_model_id}")
            
            print("\n--- Testing Chat with Auto-Detected Model ---")
            # Test chat without explicit model_id
            response = await client.chat(input_text="Hello, who are you?")
            print(f"Assistant: {response.get('content')}")
            
            if response.get('content'):
                print("✅ Chat test passed!")
            else:
                print("❌ Chat response empty or failed")
        else:
            print(f"❌ Expected LMStudioClient, got {type(client)}")
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"❌ Test failed with error: {e}")

if __name__ == "__main__":
    asyncio.run(test_lm_studio_auto())
