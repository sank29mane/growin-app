import asyncio
from backend.agents.coordinator_agent import CoordinatorAgent

async def test_coordinator():
    agent = CoordinatorAgent()
    context = {"query": "test query", "ticker": "AAPL"}
    try:
        response = await agent.analyze(context)
        print(response)
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_coordinator())
