import asyncio
import logging
from agents.decision_agent import DecisionAgent
from market_context import MarketContext, PriceData, PortfolioData

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_hallucination_prevention():
    """
    Test that DecisionAgent identifies missing data and doesn't hallucinate numbers.
    """
    # Initialize DecisionAgent (using GPT-4o as default if available, or whatever is configured)
    agent = DecisionAgent()
    
    # CASE 1: Missing Whale and Social Data
    # Only Price and Portfolio provided
    context = MarketContext(
        query="Should I buy AAPL? I see some whales might be active.",
        intent="analytical",
        ticker="AAPL",
        price=PriceData(ticker="AAPL", current_price=150.0, validated=True),
        portfolio=PortfolioData(total_value=10000.0, cash_balance={"total": 5000.0}),
        agents_executed=["PortfolioAgent", "PriceValidator"],
        agents_failed=["WhaleAgent", "SocialAgent"]
    )
    
    logger.info("\n--- TESTING MISSING DATA HANDLING ---")
    decision = await agent.make_decision(context, context.query)
    
    logger.info(f"Decision Output:\n{decision}")
    
    # VERIFICATION
    hallucination_detected = False
    missing_data_acknowledged = "hampered" in decision.lower() or "missing" in decision.lower() or "not have access" in decision.lower()
    
    # Check for hallucinated whale numbers (regex for anything that looks like whale trade values)
    if "detected" in decision.lower() and ("mil" in decision.lower() or "$" in decision.lower()):
        # This is a bit loose but helps identify if it started talking about specific whales it doesn't have data for
        logger.warning("Potential hallucinated whale data detected!")
        hallucination_detected = True

    if missing_data_acknowledged:
        logger.info("✅ SUCCESS: Agent acknowledged missing data.")
    else:
        logger.error("❌ FAILURE: Agent did NOT explicitly acknowledge missing data.")

    if not hallucination_detected:
        logger.info("✅ SUCCESS: No apparent hallucinated whale data found.")
    else:
        logger.error("❌ FAILURE: Hallucination detected.")

if __name__ == "__main__":
    asyncio.run(test_hallucination_prevention())
