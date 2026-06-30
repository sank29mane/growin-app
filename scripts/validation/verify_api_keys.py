import os
import sys
import asyncio
import httpx
import logging
from dotenv import load_dotenv

# Add backend to path so we can import modules
sys.path.append(os.path.join(os.getcwd(), "backend"))

from app_logging import setup_logging
logger = setup_logging("api_verifier")

# Load backend/.env
env_path = os.path.join(os.getcwd(), "backend", ".env")
load_dotenv(env_path)

async def test_alpaca():
    """Verify Alpaca keys and connection."""
    api_key = os.getenv("ALPACA_API_KEY")
    api_secret = os.getenv("ALPACA_SECRET_KEY")
    use_paper = os.getenv("ALPACA_USE_PAPER", "true").lower() == "true"
    base_url = "https://paper-api.alpaca.markets" if use_paper else "https://api.alpaca.markets"
    
    if not api_key or not api_secret:
        return "❌ Alpaca: Keys missing"
    
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(api_key, api_secret, paper=use_paper)
        account = client.get_account()
        return f"✅ Alpaca ({'Paper' if use_paper else 'Live'}): Connected (Status: {account.status})"
    except Exception as e:
        return f"❌ Alpaca: Failed ({str(e)})"

async def test_trading212():
    """Verify Trading 212 keys and connection."""
    api_key = os.getenv("TRADING212_API_KEY")
    api_secret = os.getenv("TRADING212_API_SECRET")
    use_demo = os.getenv("TRADING212_USE_DEMO", "true").lower() == "true"
    base_url = "https://demo.trading212.com/api/v0" if use_demo else "https://live.trading212.com/api/v0"
    
    if not api_key:
        return "❌ Trading 212: Key missing"
    
    try:
        headers = {"Authorization": api_key}
        if api_secret:
            import base64
            credentials = f"{api_key}:{api_secret}"
            encoded = base64.b64encode(credentials.encode()).decode()
            headers = {"Authorization": f"Basic {encoded}"}
            
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{base_url}/equity/account/cash", headers=headers)
            if response.status_code == 200:
                return f"✅ Trading 212 ({'Demo' if use_demo else 'Live'}): Connected"
            else:
                return f"❌ Trading 212: Failed (HTTP {response.status_code}: {response.text})"
    except Exception as e:
        return f"❌ Trading 212: Error ({str(e)})"

async def test_finnhub():
    """Verify Finnhub API key."""
    api_key = os.getenv("FINNHUB_API_KEY")
    if not api_key:
        return "❌ Finnhub: Key missing"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"https://finnhub.io/api/v1/quote?symbol=AAPL&token={api_key}")
            if response.status_code == 200 and "c" in response.json():
                return "✅ Finnhub: Connected (Ticker AAPL active)"
            else:
                return f"❌ Finnhub: Failed ({response.text})"
    except Exception as e:
        return f"❌ Finnhub: Error ({str(e)})"

async def test_newsdata():
    """Verify NewsData.io API key."""
    api_key = os.getenv("NEWSDATA_API_KEY")
    if not api_key:
        return "❌ NewsData: Key missing"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"https://newsdata.io/api/1/news?apikey={api_key}&q=finance")
            if response.status_code == 200:
                return "✅ NewsData: Connected"
            else:
                return f"❌ NewsData: Failed (HTTP {response.status_code})"
    except Exception as e:
        return f"❌ NewsData: Error ({str(e)})"

async def test_tavily():
    """Verify Tavily Search API key."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return "❌ Tavily: Key missing"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                json={"api_key": api_key, "query": "Apple stock news", "search_depth": "basic"}
            )
            if response.status_code == 200:
                return "✅ Tavily: Connected"
            else:
                return f"❌ Tavily: Failed (HTTP {response.status_code})"
    except Exception as e:
        return f"❌ Tavily: Error ({str(e)})"

async def test_huggingface():
    """Verify HuggingFace Token."""
    token = os.getenv("HF_TOKEN")
    if not token:
        return "❌ HuggingFace: Token missing"
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = {"Authorization": f"Bearer {token}"}
            # Test by getting user info
            response = await client.get("https://huggingface.co/api/whoami-v2", headers=headers)
            if response.status_code == 200:
                user = response.json().get("name", "Unknown")
                return f"✅ HuggingFace: Connected (User: {user})"
            else:
                return f"❌ HuggingFace: Failed (HTTP {response.status_code})"
    except Exception as e:
        return f"❌ HuggingFace: Error ({str(e)})"

async def main():
    print("\n🚀 Growin App API Verification Suite (SOTA 2026)")
    print("-" * 50)
    
    results = await asyncio.gather(
        test_alpaca(),
        test_trading212(),
        test_finnhub(),
        test_newsdata(),
        test_tavily(),
        test_huggingface()
    )
    
    for result in results:
        print(result)
    
    print("-" * 50)
    print("Verification Complete.\n")

if __name__ == "__main__":
    asyncio.run(main())
