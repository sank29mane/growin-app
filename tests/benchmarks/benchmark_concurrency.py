import asyncio
import time
from unittest.mock import AsyncMock, patch, MagicMock
import sys
import os

# Add backend to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app_context import state, ChatMessage
from routes.chat_routes import chat_message
from chat_manager import ChatManager

async def simulate_load(concurrent_users: int = 5):
    print(f"--- CONCURRENCY BENCHMARK: {concurrent_users} Users ---")
    
    # Setup mocks to avoid external IO but test internal processing
    # Patching at the route level to test the full endpoint logic
    with patch("agents.coordinator_agent.CoordinatorAgent") as MockCoord, \
         patch("agents.decision_agent.DecisionAgent") as MockDecision, \
         patch("routes.chat_routes.update_conversation_title_if_needed", new_callable=AsyncMock):
        
        coord_instance = MockCoord.return_value
        decision_instance = MockDecision.return_value
        
        async def delayed_process(*args, **kwargs):
            await asyncio.sleep(0.01) # 10ms artificial delay
            return MagicMock(
                user_context={},
                model_dump=lambda **kwargs: {},
                dict=lambda **kwargs: {}
            )
            
        coord_instance.process_query = AsyncMock(side_effect=delayed_process)
        
        async def delayed_decision(*args, **kwargs):
            await asyncio.sleep(0.01) # 10ms artificial delay
            return "Friendly expert response"
            
        decision_instance.make_decision = AsyncMock(side_effect=delayed_decision)
        
        # Use in-memory DB for concurrency test
        state.chat_manager = ChatManager(db_path=":memory:")
        
        tasks = []
        start_time = time.perf_counter()
        
        for i in range(concurrent_users):
            req = ChatMessage(message=f"User {i} query", model_name="native-mlx")
            tasks.append(chat_message(req, accept="application/json"))
            
        print(f"Dispatching {concurrent_users} concurrent requests...")
        responses = await asyncio.gather(*tasks)
        
        total_time = (time.perf_counter() - start_time)
        avg_latency = (total_time / concurrent_users) * 1000 # ms
        
        print(f"\nTotal Time: {total_time:.2f}s")
        print(f"Average Latency: {avg_latency:.2f}ms")
        print(f"Throughput: {concurrent_users / total_time:.2f} req/s")
        
        # Verify all succeeded
        for res in responses:
            assert res["response"] == "Friendly expert response"
            
        print("\nBenchmark Success: System handled concurrent load with consistent responses.")

if __name__ == "__main__":
    asyncio.run(simulate_load(10))
