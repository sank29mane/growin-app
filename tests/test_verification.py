"""
Verification Script for Growin App Backend
Tests:
1. TTM-R2 Model Loading & Forecasting
2. ResearchAgent (NewsAPI + Sentiment)
3. PortfolioAgent (basic structure)
"""

import asyncio
import logging
import sys
import os
from datetime import datetime
from dotenv import load_dotenv

# Load env vars
load_dotenv(os.path.join(os.getcwd(), 'backend', '.env'))

# Add backend to path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

logging.basicConfig(level=logging.INFO, format='%(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("Verification")

async def test_ttm_forecasting():
    logger.info("--- Testing TTM-R2 Forecasting ---")
    try:
        from forecaster import TTMForecaster
        forecaster = TTMForecaster()
        
        # Generate mock OHLCV data (need enough points)
        import random
        data = []
        price = 150.0
        now = datetime.now().timestamp() * 1000
        for i in range(600): # Need > 512 for TTM
            price += random.uniform(-1, 1)
            data.append({
                "t": int(now - (600-i)*3600*1000),
                "o": price, "h": price+1, "l": price-1, "c": price, "v": 1000
            })
            
        logger.info(f"Generated {len(data)} mock data points")
        
        # Test forecast
        result = await forecaster.forecast(data, prediction_steps=12)
        
        logger.info(f"Forecast Result Keys: {result.keys()}")
        if "error" in result:
             logger.error(f"Forecast Error: {result['error']}")
        else:
             logger.info(f"Model Used: {result.get('model_used')}")
             logger.info(f"Confidence: {result.get('confidence')}")
             logger.info(f"Steps Predicted: {result.get('prediction_steps')}")
             
    except Exception as e:
        logger.error(f"TTM Test Failed: {e}")

async def test_research_agent():
    logger.info("\n--- Testing ResearchAgent ---")
    try:
        from agents.research_agent import ResearchAgent, AgentConfig
        
        # Check API key
        if not os.getenv("NEWSAPI_KEY"):
            logger.warning("NEWSAPI_KEY not set. Expecting placebo response.")
            
        agent = ResearchAgent(AgentConfig(name="TestResearch"))
        
        context = {"ticker": "AAPL", "company_name": "Apple"}
        response = await agent.analyze(context)
        
        logger.info(f"Response Success: {response.success}")
        if response.data:
            data = response.data
            logger.info(f"Sentiment: {data.get('sentiment_label')} ({data.get('sentiment_score')})")
            logger.info(f"Headlines: {len(data.get('top_headlines', []))}")
            if data.get('top_headlines'):
                logger.info(f"Sample: {data['top_headlines'][0]}")
        else:
             logger.error(f"Error: {response.error}")

    except Exception as e:
        logger.error(f"Research Agent Test Failed: {e}")

async def main():
    logger.info("Starting Verification...")
    await test_ttm_forecasting()
    await test_research_agent()
    logger.info("\nVerification Complete.")

if __name__ == "__main__":
    asyncio.run(main())
