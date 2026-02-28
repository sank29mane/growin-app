import asyncio
import os
import sys
sys.path.append(os.path.abspath("backend"))

os.environ["NEWSDATA_API_KEY"] = "mock_key_for_test"

from data_engine import get_news_client

class MockHttpxResponse:
    def __init__(self, json_data, status_code):
        self._json_data = json_data
        self.status_code = status_code
    def raise_for_status(self):
        pass
    def json(self):
        return self._json_data

class MockAsyncClient:
    def __init__(self, **kwargs):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass
    async def get(self, url, params):
        return MockHttpxResponse({
            "results": [
                {
                    "title": "Apple is doing great",
                    "description": "Stock is soaring today.",
                    "source_id": "MockSource",
                    "link": "http://example.com/apple",
                    "pubDate": "2023-10-10"
                }
            ]
        }, 200)

import httpx
httpx.AsyncClient = MockAsyncClient

from data_fabricator import DataFabricator

async def test():
    print("Testing NewsDataIOClient via DataFabricator...")
    fabricator = DataFabricator()
    data = await fabricator._fetch_news_data("AAPL")
    if data:
        print(f"Sentiment Score: {data.sentiment_score}")
        print(f"Sentiment Label: {data.sentiment_label}")
        print(f"Articles Found: {len(data.articles)}")
        if data.articles:
            print(f"Sample Headline: {data.articles[0].title}")
    else:
        print("Fetch failed or no data returned.")

if __name__ == "__main__":
    asyncio.run(test())
