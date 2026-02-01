
import os
import sys
import asyncio
import logging
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("verify_agents")

# Load environment
load_dotenv()

async def verify():
    logger.info("--- Environment Check ---")
    keys = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "NEWSAPI_KEY", "TAVILY_API_KEY", "NEWSDATA_API_KEY", "FINNHUB_API_KEY"]
    for k in keys:
        val = os.getenv(k)
        status = "✅ Present" if val and len(val) > 5 else "❌ Missing/Empty"
        logger.info(f"{k}: {status}")

    logger.info("\n--- Agent Initialization & Execution Check ---")
    
    from agents.research_agent import ResearchAgent
    from agents.social_agent import SocialAgent
    from agents.whale_agent import WhaleAgent
    from agents.quant_agent import QuantAgent
    from agents.forecasting_agent import ForecastingAgent
    from data_engine import get_alpaca_client

    # Initialize Agents
    research = ResearchAgent()
    social = SocialAgent()
    whale = WhaleAgent()
    quant = QuantAgent()
    forecast = ForecastingAgent()

    logger.info(f"ResearchAgent Sources: {research.newsapi_key and 'NewsAPI '}{research.tavily_key and 'Tavily '}{research.newsdata_key and 'NewsData'}")
    logger.info(f"SocialAgent Key: {'✅' if social.tavily_key else '❌'}")
    logger.info(f"WhaleAgent Key: {'✅' if os.getenv('ALPACA_API_KEY') else '❌'}")
    logger.info(f"QuantAgent TA-Lib: {'✅' if quant.available else '❌ (Simple Fallback)'}")
    logger.info(f"ForecastAgent TTM: {'✅' if forecast.forecaster.ttm_available else '❌ (Statistical Fallback)'}")

    # Test Data Engine
    logger.info("\n--- Data Engine Check ---")
    alpaca = get_alpaca_client()
    bars = await alpaca.get_historical_bars("AAPL", limit=50)
    logger.info(f"Alpaca 'AAPL' bars: {'✅ Found ' + str(len(bars.get('bars'))) if bars else '❌ Failed'}")

    # Test Execution (AAPL)
    logger.info("\n--- Execution Test (Ticker: AAPL) ---")
    
    # 1. Whale
    w_res = await whale.analyze({"ticker": "AAPL"})
    logger.info(f"WhaleAgent: {'✅ Success' if w_res.success else '❌ Failed: ' + str(w_res.error)}")

    # 2. Research
    r_res = await research.analyze({"ticker": "AAPL"})
    logger.info(f"ResearchAgent: {'✅ Success' if r_res.success else '❌ Failed: ' + str(r_res.error)}")

    # 3. Social
    s_res = await social.analyze({"ticker": "AAPL"})
    logger.info(f"SocialAgent: {'✅ Success' if s_res.success else '❌ Failed: ' + str(s_res.error)}")
    
    # Test Execution (MARKET)
    logger.info("\n--- Execution Test (Ticker: MARKET) ---")
    
    # 1. Whale (Expected to fail)
    w_res_m = await whale.analyze({"ticker": "MARKET"})
    logger.info(f"WhaleAgent: {'✅ Success' if w_res_m.success else '❌ Failed (Expected): ' + str(w_res_m.error)}")

    # 2. Research (Should work)
    r_res_m = await research.analyze({"ticker": "MARKET"})
    logger.info(f"ResearchAgent: {'✅ Success' if r_res_m.success else '❌ Failed: ' + str(r_res_m.error)}")

    # 3. Social (Should work)
    s_res_m = await social.analyze({"ticker": "MARKET"})
    logger.info(f"SocialAgent: {'✅ Success' if s_res_m.success else '❌ Failed: ' + str(s_res_m.error)}")

    # 4. Quant (Expected failure if no data passed)
    q_res = await quant.analyze({"ticker": "MARKET", "ohlcv_data": []})
    logger.info(f"QuantAgent: {'✅ Success' if q_res.success else '❌ Failed (Expected): ' + str(q_res.error)}")

if __name__ == "__main__":
    asyncio.run(verify())
