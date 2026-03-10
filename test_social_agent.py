import asyncio
from backend.agents.social_agent import SocialAgent

async def test():
    agent = SocialAgent()
    res = await agent.analyze({"ticker": "TSLA", "company_name": "Tesla"})
    print(res)

if __name__ == "__main__":
    asyncio.run(test())
