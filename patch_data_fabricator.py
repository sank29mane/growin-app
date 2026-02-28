import os

with open("backend/data_fabricator.py", "r") as f:
    content = f.read()

# Fix import
if "from data_engine import get_alpaca_client, get_finnhub_client" in content:
    content = content.replace(
        "from data_engine import get_alpaca_client, get_finnhub_client",
        "from data_engine import get_alpaca_client, get_finnhub_client, get_news_client"
    )

# Add client to init
if "self.finnhub = get_finnhub_client()" in content:
    content = content.replace(
        "self.finnhub = get_finnhub_client()",
        "self.finnhub = get_finnhub_client()\n        self.news = get_news_client()"
    )

# Replace _fetch_news_data logic
fetch_news_old = """    async def _fetch_news_data(self, ticker: str) -> Optional[ResearchData]:
        \"\"\"Fetch news using existing logic.\"\"\"
        from status_manager import status_manager
        status_manager.set_status("research_agent", "working", f"Searching news for {ticker}...")
        try:
            # Placeholder: In a real refactor, we'd move the NewsDataIOClient usage here.
            # Using a basic stub that would be populated by the actual API
            # For Phase 1, we might return None and let the ResearchAgent fallback run?
            # No, the goal is centralized fetching.

            # TODO: Import NewsDataIOClient here when migrated
            return ResearchData(
                ticker=ticker,
                sentiment_score=0.1,
                sentiment_label="NEUTRAL",
                articles=[
                    NewsArticle(
                        title=f"Market analysis for {ticker}",
                        description="General market commentary suggests neutral trading conditions.",
                        source="MarketAnalyst",
                        sentiment=0.0
                    )
                ]
            )

        except Exception as e:
            logger.error(f"News fetch failed: {e}")
            return None"""

fetch_news_new = """    async def _fetch_news_data(self, ticker: str) -> Optional[ResearchData]:
        \"\"\"Fetch news using NewsDataIOClient.\"\"\"
        from status_manager import status_manager
        status_manager.set_status("research_agent", "working", f"Searching news for {ticker}...")
        try:
            raw_articles = await self.news.fetch_news(ticker)
            if not raw_articles:
                return ResearchData(
                    ticker=ticker,
                    sentiment_score=0.0,
                    sentiment_label="NEUTRAL",
                    articles=[],
                    top_headlines=["No news data available"]
                )

            try:
                from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
                analyzer = SentimentIntensityAnalyzer()
            except ImportError:
                analyzer = None
                logger.warning("vaderSentiment not found. Using neutral sentiment.")

            sentiments = []
            rich_articles = []
            headlines = []

            for article in raw_articles:
                title = article.get('title', '')
                description = article.get('description') or ''
                text = f"{title}. {description}"

                compound = 0.0
                if analyzer:
                    compound = analyzer.polarity_scores(text)['compound']

                sentiments.append(compound)
                rich_articles.append(NewsArticle(
                    title=title,
                    description=description if len(description) < 300 else f"{description[:300]}...",
                    source=article.get('source', {}).get('name', 'NewsData.io'),
                    url=article.get('url'),
                    sentiment=compound
                ))
                headlines.append(title)

            avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0

            if avg_sentiment >= 0.05:
                label = "BULLISH"
            elif avg_sentiment <= -0.05:
                label = "BEARISH"
            else:
                label = "NEUTRAL"

            return ResearchData(
                ticker=ticker,
                sentiment_score=avg_sentiment,
                sentiment_label=label,
                articles=rich_articles,
                top_headlines=headlines[:5],
                sources=["NewsData.io"]
            )

        except Exception as e:
            logger.error(f"News fetch failed: {e}")
            return None"""

if fetch_news_old in content:
    content = content.replace(fetch_news_old, fetch_news_new)
    with open("backend/data_fabricator.py", "w") as f:
        f.write(content)
    print("Patched data_fabricator.py successfully.")
else:
    print("Could not find the old _fetch_news_data method.")
