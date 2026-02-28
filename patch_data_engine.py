import re
import os

with open("backend/data_engine.py", "r") as f:
    content = f.read()

news_client_code = """
class NewsDataIOClient:
    \"\"\"
    Client for fetching news data from NewsData.io.
    \"\"\"
    def __init__(self):
        self.api_key = os.getenv("NEWSDATA_API_KEY")
        if not self.api_key:
            logger.warning("NewsDataIOClient: API key not set. Returning empty results.")

    async def fetch_news(self, ticker: str, company_name: str = "") -> List[Dict[str, Any]]:
        \"\"\"Fetch recent news for a given ticker or market from NewsData.io.\"\"\"
        if not self.api_key:
            return []

        try:
            import httpx

            # Base params
            params = {
                "apikey": self.api_key,
                "language": "en",
                "excludefield": "ai_summary"
            }

            url = "https://newsdata.io/api/1/latest"

            if ticker == "MARKET":
                 # Simplify for client: default to market endpoint without smart query
                 url = "https://newsdata.io/api/1/market"
                 params.update({"removeduplicate": 0})
            else:
                 q = f"{company_name} OR {ticker} stock" if company_name else f"{ticker} stock"
                 params["q"] = q
                 is_uk = ticker.upper().endswith(".L")
                 params["country"] = "gb" if is_uk else "us"
                 if "NSE" in ticker.upper(): params["country"] = "in"

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()

            # Normalize to common format
            articles = []
            for article in data.get('results', [])[:10]:
                articles.append({
                    'title': article.get('title'),
                    'description': article.get('description') or article.get('content'),
                    'source': {'name': article.get('source_id', 'NewsData.io')},
                    'url': article.get('link'),
                    'published_at': article.get('pubDate')
                })

            logger.info(f"NewsDataIOClient returned {len(articles)} articles for {ticker}")
            return articles

        except Exception as e:
            logger.warning(f"NewsDataIOClient failed: {e}")
            return []
"""

# Find the end of FinnhubClient to insert NewsDataIOClient
if "class FinnhubClient" in content:
    lines = content.split('\n')
    idx = 0
    for i, line in enumerate(lines):
        if line.startswith("def get_alpaca_client"):
            idx = i
            break

    new_content = '\n'.join(lines[:idx]) + '\n' + news_client_code + '\n' + '\n'.join(lines[idx:])

    # Also add get_news_client helper
    if "def get_finnhub_client():" in new_content:
        new_content = new_content.replace(
            "def get_finnhub_client():\n    \"\"\"Returns a FinnhubClient instance.\"\"\"\n    return FinnhubClient()",
            "def get_finnhub_client():\n    \"\"\"Returns a FinnhubClient instance.\"\"\"\n    return FinnhubClient()\n\n\ndef get_news_client():\n    \"\"\"Returns a NewsDataIOClient instance.\"\"\"\n    return NewsDataIOClient()"
        )

    with open("backend/data_engine.py", "w") as f:
        f.write(new_content)
    print("Patched data_engine.py successfully.")
else:
    print("Could not find FinnhubClient class.")
