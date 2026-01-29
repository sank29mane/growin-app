
import asyncio
import logging
import yfinance as yf
from agents.forecasting_agent import ForecastingAgent, AgentConfig
from utils.currency_utils import DataSourceNormalizer

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def run_debug():
    print("--- Debugging SSLN.L Variance (Production Flow) ---")
    ticker = "SSLN.L"
    
    # 1. Fetch Data using Data Engine (mimics real app)
    print(f"Fetching data via data_engine.AlpacaClient for {ticker}...")
    from data_engine import get_alpaca_client
    client = get_alpaca_client()
    
    # Request 512+ bars to ensure TTM triggers
    # Timeframe 1Day (as per screenshot likely usage for "1Month" view which defaults to 1Day resolution)
    result = await client.get_historical_bars(ticker, timeframe="1Day", limit=1000)
    
    if not result or not result.get("bars"):
        print("Error: No data found via data_engine.")
        return

    ohlcv_data = result["bars"]
    print(f"\nFetched {len(ohlcv_data)} bars.")
    print(f"Data Sample (Tail 5):")
    for bar in ohlcv_data[-5:]:
        import datetime
        dt = datetime.datetime.fromtimestamp(bar['t']/1000).isoformat()
        print(f"  {dt} | Close: {bar['c']}")
        
    # 2. Run ForecastingAgent
    print("\nInitializing ForecastingAgent...")
    config = AgentConfig(name="DebugAgent", enabled=True)
    agent = ForecastingAgent(config=config)
    
    print("\nRunning Agent.analyze()...")
    response = await agent.analyze({
        "ticker": ticker,
        "ohlcv_data": ohlcv_data,
        "days": 7,
        "timeframe": "1Day"
    })
    
    print("\n--- Agent Response ---")
    if response.success:
        data = response.data
        print(f"Success: {response.success}")
        print(f"Algorithm: {data.get('algorithm')}")
        print(f"Note: {data.get('note')}")
        f_24h = data.get('forecast_24h')
        print(f"Forecast 24h: {f_24h}")
        
        last_close = ohlcv_data[-1]['c']
        print(f"Last Actual Close: {last_close}")
        
        if f_24h:
             var = (f_24h - last_close) / last_close * 100
             print(f"Variance: {var:.2f}%")
    else:
        print(f"Failed: {response.error}")

if __name__ == "__main__":
    asyncio.run(run_debug())
