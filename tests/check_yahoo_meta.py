import asyncio
import httpx
import json
import os
import sys

async def fetch_yahoo_meta(ticker):
    headers = {'User-Agent': 'Mozilla/5.0'}
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range=1d&interval=1d"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            res = data.get("chart", {}).get("result", [])
            if res:
                meta = res[0].get("meta", {})
                return {
                    "ticker": ticker,
                    "currency": meta.get("currency"),
                    "price": meta.get("regularMarketPrice"),
                    "full_name": meta.get("longName")
                }
        except Exception as e:
            return {"ticker": ticker, "error": str(e)}
    return {"ticker": ticker, "error": "No result"}

async def main():
    tickers = ["LQQ3.L", "3QQQ.L", "LQQS.L", "QQQS.L"]
    results = await asyncio.gather(*(fetch_yahoo_meta(t) for t in tickers))
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
