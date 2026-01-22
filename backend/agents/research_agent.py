"""
Research Agent - Multi-source news aggregation and sentiment analysis

Data Sources:
  1. NewsAPI - Traditional news (requires NEWSAPI_KEY)
  2. Tavily - AI-powered search (requires TAVILY_API_KEY)
  3. NewsData.io - Business/Economy news (requires NEWSDATA_API_KEY)

Rate Limits:
  - NewsData.io: 200 credits/day (10 articles/credit = 2000 articles/day)
  - Tavily: Per plan limits
  - NewsAPI: 100 requests/day (free tier)

Supported Markets: UK (LSE), India (NSE), US
"""

from base_agent import BaseAgent, AgentConfig, AgentResponse
from market_context import ResearchData, NewsArticle
from typing import Dict, Any, List
import logging
import os
import asyncio
import re

logger = logging.getLogger(__name__)


class ResearchAgent(BaseAgent):
    """
    Multi-source news aggregation and sentiment analysis agent.
    
    Aggregates news from NewsAPI, Tavily, and NewsData.io, then analyzes
    sentiment using VADER to provide trading insights.
    
    Attributes:
        newsapi_key: API key for NewsAPI
        tavily_key: API key for Tavily
        newsdata_key: API key for NewsData.io
    
    Performance: ~500-1000ms (multiple API calls in parallel)
    """
    
    def __init__(self, config: AgentConfig = None):
        if config is None:
            config = AgentConfig(
                name="ResearchAgent",
                enabled=True,
                timeout=20.0,  # Increased for multiple API calls
                cache_ttl=600  # Cache news for 10 minutes
            )
        super().__init__(config)
        
        # Helper to validate keys (not placeholders)
        def is_valid(k):
            return k and len(k) > 10 and not k.startswith("YOUR_") and "REPLACE" not in k.upper() and "=" not in k

        # Load and validate API keys
        self.newsapi_key = os.getenv("NEWSAPI_KEY") if is_valid(os.getenv("NEWSAPI_KEY")) else None
        self.tavily_key = os.getenv("TAVILY_API_KEY") if is_valid(os.getenv("TAVILY_API_KEY")) else None
        self.newsdata_key = os.getenv("NEWSDATA_API_KEY") if is_valid(os.getenv("NEWSDATA_API_KEY")) else None
        
        # Log available sources
        sources = []
        if self.newsapi_key: sources.append("NewsAPI")
        if self.tavily_key: sources.append("Tavily")
        if self.newsdata_key: sources.append("NewsData.io")
        
        if sources:
            logger.info(f"ResearchAgent initialized with sources: {', '.join(sources)}")
        else:
            logger.warning("No news API keys found. ResearchAgent will run in placeholder mode.")

    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Fetch news from multiple sources and analyze sentiment.
        
        Args:
            context: Dict containing:
                - ticker: Stock symbol or "MARKET" for broad outlook
                - company_name: Optional company name for better search
        
        Returns:
            AgentResponse with ResearchData containing sentiment and headlines
        """
        ticker = context.get("ticker", "MARKET")
        company_name = context.get("company_name", ticker)
        
        if not any([self.newsapi_key, self.tavily_key, self.newsdata_key]):
            return self._neutral_response(ticker, error="No news API keys configured")
        
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            sentiment_analyzer = SentimentIntensityAnalyzer()
            
            articles = []
            
            # 1. NewsAPI - Traditional news (specific tickers only)
            if self.newsapi_key and ticker != "MARKET":
                articles.extend(await self._fetch_newsapi(ticker, company_name))
            
            # 2. Tavily - AI-powered semantic search (all queries)
            if self.tavily_key:
                articles.extend(await self._fetch_tavily(ticker, company_name))
            
            # 3. NewsData.io - Business/Economy focus (UK, India, US)
            if self.newsdata_key:
                articles.extend(await self._fetch_newsdata(ticker, company_name))
            
            if not articles:
                return self._neutral_response(ticker, error="No news found from any source")
            
            # Deduplicate and analyze
            unique_articles = self._deduplicate_articles(articles)
            sentiments, rich_articles, headlines, sources = self._analyze_sentiment(unique_articles, sentiment_analyzer)
            
            # Calculate average sentiment
            avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0
            label = self._get_sentiment_label(avg_sentiment)
            
            research_data = ResearchData(
                ticker=ticker,
                sentiment_score=avg_sentiment,
                sentiment_label=label,
                articles=rich_articles[:10],
                top_headlines=headlines[:5],
                sources=sources[:5]
            )
            
            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=research_data.model_dump(),
                latency_ms=0
            )
            
        except ImportError:
            return self._neutral_response(ticker, error="Missing dependencies")
        except Exception as e:
            logger.error(f"Research analysis failed: {e}")
            return self._neutral_response(ticker, error=str(e))

    async def _fetch_newsapi(self, ticker: str, company_name: str) -> List[Dict]:
        """Fetch from NewsAPI (traditional news sources)."""
        try:
            from newsapi import NewsApiClient
            from datetime import datetime, timedelta
            
            newsapi = NewsApiClient(api_key=self.newsapi_key)
            from_date = (datetime.now() - timedelta(days=7)).isoformat()
            
            def fetch():
                return newsapi.get_everything(
                    q=f"{company_name} stock OR {ticker}",
                    from_param=from_date,
                    language='en',
                    sort_by='relevancy',
                    page_size=5
                )
            
            response = await asyncio.to_thread(fetch)
            return response.get('articles', [])
        except Exception as e:
            logger.warning(f"NewsAPI failed: {e}")
            return []

    async def _fetch_tavily(self, ticker: str, company_name: str) -> List[Dict]:
        """Fetch from Tavily (AI-powered search)."""
        try:
            from tavily import TavilyClient
            
            tavily = TavilyClient(api_key=self.tavily_key)
            
            if ticker == "MARKET":
                query = "latest stock market outlook for LSE (UK), NSE (India), and US markets"
            else:
                query = f"latest financial news for {company_name} ({ticker}) stock"
            
            def fetch():
                return tavily.search(
                    query=query,
                    search_depth="advanced",
                    max_results=8
                )
            
            response = await asyncio.to_thread(fetch)
            
            # Normalize to common format
            return [
                {
                    'title': r.get('title'),
                    'description': r.get('content') or r.get('snippet'),
                    'source': {'name': 'Tavily'},
                    'url': r.get('url')
                }
                for r in response.get('results', [])
            ]
        except Exception as e:
            logger.warning(f"Tavily failed: {e}")
            return []

    async def _fetch_newsdata(self, ticker: str, company_name: str) -> List[Dict]:
        """
        Fetch from NewsData.io (business/economy focus).
        
        Rate limit: 200 credits/day, 30 credits/15min
        Each credit = 10 articles
        """
        try:
            import httpx
            
            # Build query params
            params = {
                "apikey": self.newsdata_key,
                "language": "en",
                "category": "business,technology,politics", # Expanded for deeper insights
                "country": "gb,in,us",
            }
            
            # Intelligent country targeting
            ticker_upper = ticker.upper()
            if ticker_upper.endswith(".L") or ticker_upper.endswith(".IL"):
                params["country"] = "gb"
            elif ticker_upper.endswith(".BO") or ticker_upper.endswith(".NS"):
                params["country"] = "in"
            elif ticker == "MARKET":
                params["country"] = "gb,in,us" # Outlook needs broad scope
            
            # Add search query
            if ticker == "MARKET":
                params["q"] = "stock market outlook LSE FTSE NSE NIFTY US"
            else:
                # Use 'q' for NewsData.io 'latest' endpoint
                params["q"] = ticker.upper()
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    "https://newsdata.io/api/1/latest",
                    params=params
                )
                response.raise_for_status()
                data = response.json()
            
            # Normalize to common format
            articles = []
            for article in data.get('results', [])[:10]:
                articles.append({
                    'title': article.get('title'),
                    'description': article.get('description') or article.get('content'),
                    'source': {'name': article.get('source_name', 'NewsData.io')},
                    'url': article.get('link')
                })
            
            logger.info(f"NewsData.io returned {len(articles)} articles")
            return articles
            
        except Exception as e:
            logger.warning(f"NewsData.io failed: {e}")
            return []

    def _deduplicate_articles(self, articles: List[Dict]) -> List[Dict]:
        """Remove duplicate articles based on normalized title or URL."""
        seen_titles = set()
        seen_urls = set()
        unique = []
        for art in articles:
            title = art.get('title', '').lower().strip()
            url = art.get('url', '')
            # Simple normalization: remove non-alphanumeric for title check
            clean_title = re.sub(r'[^a-zA-Z0-9]', '', title)
            
            if clean_title and clean_title not in seen_titles and (not url or url not in seen_urls):
                seen_titles.add(clean_title)
                if url: seen_urls.add(url)
                unique.append(art)
        return unique

    def _analyze_sentiment(self, articles: List[Dict], analyzer) -> tuple:
        """Analyze sentiment of articles using VADER and create rich NewsArticle objects."""
        sentiments = []
        rich_articles = []
        headlines = []
        sources = []
        
        for article in articles[:15]:  # Limit to 15 for performance
            title = article.get('title', '')
            description = article.get('description') or ''
            text = f"{title}. {description}"
            
            # VADER sentiment analysis
            scores = analyzer.polarity_scores(text)
            compound = scores['compound']
            sentiments.append(compound)
            
            source_name = article.get('source', {}).get('name', 'Unknown')
            
            # Create rich model
            rich_articles.append(NewsArticle(
                title=title,
                description=description if len(description) < 300 else f"{description[:300]}...",
                source=source_name,
                url=article.get('url'),
                sentiment=compound
            ))
            
            headlines.append(title)
            if source_name not in sources:
                sources.append(source_name)
        
        return sentiments, rich_articles, headlines, sources

    def _get_sentiment_label(self, score: float) -> str:
        """Convert sentiment score to label."""
        if score >= 0.05:
            return "BULLISH"
        elif score <= -0.05:
            return "BEARISH"
        return "NEUTRAL"

    def _neutral_response(self, ticker: str, error: str = None) -> AgentResponse:
        """Return neutral sentiment when no data available."""
        research_data = ResearchData(
            ticker=ticker,
            sentiment_score=0.0,
            sentiment_label="NEUTRAL",
            top_headlines=[f"News unavailable: {error}" if error else "No data"],
            sources=["System"]
        )
        
        return AgentResponse(
            agent_name=self.config.name,
            success=False, # Mark as failure when no data found
            data=research_data.model_dump(),
            latency_ms=0,
            error=error
        )
