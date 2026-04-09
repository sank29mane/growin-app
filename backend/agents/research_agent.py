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

from .base_agent import BaseAgent, AgentConfig, AgentResponse
from market_context import ResearchData, NewsArticle
from typing import Dict, Any, List, Optional
import logging
import os
import asyncio
import re
from pydantic import BaseModel, Field
from magentic import prompt as mag_prompt
from resilience import get_circuit_breaker, CircuitBreakerOpenError

# Pre-compiled regex for fast title normalization
TITLE_CLEAN_PATTERN = re.compile(r'[^a-zA-Z0-9]')

logger = logging.getLogger(__name__)

newsdata_cb = get_circuit_breaker("newsdata", failure_threshold=3, recovery_timeout=30.0)
tavily_cb = get_circuit_breaker("tavily", failure_threshold=3, recovery_timeout=30.0)
newsapi_cb = get_circuit_breaker("newsapi", failure_threshold=3, recovery_timeout=30.0)

class NewsDataQueryParams(BaseModel):
    """Structured parameters for NewsData.io API query."""
    q: str = Field(..., description="The primary search query or keywords")
    qInTitle: Optional[str] = Field(None, description="Keywords to search in the article title")
    country: Optional[str] = Field(None, description="Country codes (e.g. 'gb,us,in')")
    category: Optional[str] = Field(None, description="News category (e.g. 'business,technology,politics')")
    domain: Optional[str] = Field(None, description="Specific domains to include (e.g. 'reuters.com,bloomberg.com')")

@mag_prompt(
    "Generate optimal NewsData.io API query parameters for a professional financial research agent.\n"
    "Context: Researching news for {ticker} (Company: {company_name}).\n"
    "Goal: High-relevance financial and market-moving news only.\n"
    "Market Context: {market_context}\n"
    "Requirements:\n"
    "1. Query 'q' should be concise and optimized for news search.\n"
    "2. If it's a specific company, include its name.\n"
    "3. Focus on recent business, economy, or technology categories if applicable.\n"
    "4. Exclude irrelevant buzzwords."
)
def generate_news_query(ticker: str, company_name: str, market_context: str) -> NewsDataQueryParams:
    ...

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
        if self.newsapi_key:
            sources.append("NewsAPI")
        if self.tavily_key:
            sources.append("Tavily")
        if self.newsdata_key:
            sources.append("NewsData.io")
        
        if sources:
            logger.info(f"ResearchAgent initialized with sources: {', '.join(sources)}")
        else:
            logger.warning("No news API keys found. ResearchAgent will run in placeholder mode.")

        # Cache for prompt template
        self._prompt_template = None

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
            
            # --- Smart Query Expansion Strategy ---
            # 1. Primary: Ticker Search
            # 2. Secondary: Company Name Search (if primary empty)
            # 3. Tertiary: Sector Search (if both empty)
            
            queries_to_try = [(ticker, company_name)]
            if ticker != "MARKET" and company_name and company_name != ticker:
                queries_to_try.append((company_name, company_name))
            
            # Attempt to fetch data using Fallback Chains per source
            for query_ticker, query_name in queries_to_try:
                current_articles = []
                
                # 0. Regulatory News (SOTA 2026 - High Priority)
                reg_news = await self._fetch_regulatory_news(query_ticker, query_name)
                current_articles.extend(reg_news)

                # 1. NewsAPI (Circuit Breaker protected via direct fetch logic)
                if self.newsapi_key and query_ticker != "MARKET":
                    news_res = await self._fetch_newsapi(query_ticker, query_name)
                    current_articles.extend(news_res)

                # 2. Tavily (Circuit Breaker protected)
                if self.tavily_key:
                    tavily_res = await self._fetch_tavily(query_ticker, query_name)
                    current_articles.extend(tavily_res)
                
                # 3. NewsData.io (Circuit Breaker protected)
                if self.newsdata_key:
                    newsdata_res = await self._fetch_newsdata(query_ticker, query_name)
                    current_articles.extend(newsdata_res)
                
                if current_articles:
                    articles = current_articles
                    break # Stop if we found data
                else:
                    logger.info(f"ResearchAgent: No results for '{query_ticker}'. Expanding query...")

            # Fallback to sector if still nothing? (Optional, maybe for V2)
            
            if not articles:
                # FAIL SOFT: Return success with neutral data instead of failing
                return self._neutral_response(ticker, error=None, success=True, msg="No relevant news found via active sources.")
            
            # Deduplicate and analyze
            unique_articles = self._deduplicate_articles(articles)
            sentiments, weights, rich_articles, headlines, sources = self._analyze_sentiment(unique_articles, sentiment_analyzer)
            
            # Calculate weighted average sentiment
            if sentiments:
                total_weighted_sentiment = sum(s for s in sentiments) # already multiplied by weight in _analyze
                total_weight = sum(weights)
                avg_sentiment = total_weighted_sentiment / total_weight if total_weight > 0 else 0.0
            else:
                avg_sentiment = 0.0
                
            label = self._get_sentiment_label(avg_sentiment)
            
            research_data = ResearchData(
                ticker=ticker,
                sentiment_score=avg_sentiment,
                sentiment_label=label,
                articles=rich_articles[:10],
                top_headlines=headlines[:5],
                sources=sources[:5]
            )

            # --- RAG INTEGRATION: Store individual articles for Timeline ---
            try:
                from app_context import state
                if state.rag_manager and rich_articles:
                    for art in rich_articles:
                        state.rag_manager.add_news_article(
                            ticker=ticker,
                            title=art.title,
                            summary=art.description or "",
                            sentiment=float(art.sentiment or 0.0),
                            source=art.source
                        )
                    logger.info(f"ResearchAgent: Stored {len(rich_articles)} articles in RAG for timeline.")
            except Exception as e:
                logger.warning(f"ResearchAgent: Failed to store in RAG: {e}")
            # ---------------------------------------
            
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
            # Fail soft on generic errors too, unless critical
            return self._neutral_response(ticker, error=str(e), success=True) # Return neutral data on crash

    def _neutral_response(self, ticker: str, error: str = None, success: bool = False, msg: str = None) -> AgentResponse:
        """Return neutral sentiment when no data available."""
        # If successfully ran but found no data, treat as success=True
        # If config missing, success=False
        
        final_msg = msg or (f"News unavailable: {error}" if error else "No data")
        
        research_data = ResearchData(
            ticker=ticker,
            sentiment_score=0.0,
            sentiment_label="NEUTRAL",
            top_headlines=[final_msg],
            sources=["System"]
        )
        
        return AgentResponse(
            agent_name=self.config.name,
            success=success, 
            data=research_data.model_dump(),
            latency_ms=0,
            error=error
        )

    async def _fetch_regulatory_news(self, ticker: str, company_name: str) -> List[Dict]:
        """
        Fetch verified regulatory news (LSE RNS, SEC Filings).
        Weighted as higher importance in final sentiment calculation.
        """
        articles = []
        try:
            is_uk = ticker.upper().endswith(".L")
            
            # 1. LSE RNS (Regulatory News Service) via NewsData.io
            if is_uk and self.newsdata_key:
                import httpx
                params = {
                    "apikey": self.newsdata_key,
                    "q": f"{ticker} RNS",
                    "country": "gb",
                    "category": "business"
                }
                from resilience import execute_with_breaker
                try:
                    data = await execute_with_breaker(newsdata_cb, "GET", "https://newsdata.io/api/1/latest", params=params, timeout=10.0)
                    for art in data.get('results', [])[:5]:
                        articles.append({
                            'title': f"[RNS] {art.get('title')}",
                            'description': art.get('description'),
                            'source': {'name': 'LSE RNS'},
                            'url': art.get('link'),
                            'is_regulatory': True
                        })
                except CircuitBreakerOpenError:
                    logger.warning(f"Regulatory news (NewsData) skipped: circuit breaker is OPEN")

            # 2. SEC Filings / News via Tavily
            if not is_uk and self.tavily_key:
                import httpx
                # Specialized query for SEC filings
                query = f"latest SEC filings and regulatory announcements for {ticker}"
                
                url = "https://api.tavily.com/search"
                headers = {"Content-Type": "application/json"}
                payload = {
                    "api_key": self.tavily_key,
                    "query": query,
                    "search_depth": "advanced",
                    "max_results": 5
                }

                from resilience import execute_with_breaker
                try:
                    data = await execute_with_breaker(tavily_cb, "POST", url, headers=headers, json=payload)
                    for r in data.get('results', []):
                        # Only include if relevant to SEC or regulatory
                        content = (r.get('title', '') + (r.get('content', '') or '')).upper()
                        if any(kw in content for kw in ["SEC", "FORM 8-K", "10-Q", "10-K", "FILING", "REGULATORY"]):
                            articles.append({
                                'title': f"[SEC] {r.get('title')}",
                                'description': r.get('content') or r.get('snippet'),
                                'source': {'name': 'SEC EDGAR / News'},
                                'url': r.get('url'),
                                'is_regulatory': True
                            })
                except CircuitBreakerOpenError:
                    logger.warning(f"Regulatory news (Tavily) skipped: circuit breaker is OPEN")
            
            if articles:
                logger.info(f"ResearchAgent: Found {len(articles)} regulatory announcements for {ticker}")
            
            return articles
        except Exception as e:
            logger.warning(f"Regulatory news fetch failed: {e}")
            return []

    async def _fetch_newsapi(self, ticker: str, company_name: str) -> List[Dict]:
        """Fetch from NewsAPI (traditional news sources)."""
        try:
            import httpx
            from datetime import datetime, timedelta
            
            from_date = (datetime.now() - timedelta(days=7)).isoformat()
            url = "https://newsapi.org/v2/everything"
            params = {
                "q": f"{company_name} stock OR {ticker}",
                "from": from_date,
                "language": "en",
                "sortBy": "relevancy",
                "pageSize": 5,
                "apiKey": self.newsapi_key
            }
            
            from resilience import execute_with_breaker
            data = await execute_with_breaker(newsapi_cb, "GET", url, params=params)
            return data.get('articles', [])
        except CircuitBreakerOpenError:
            logger.warning(f"NewsAPI skipped: circuit breaker is OPEN")
            return []
        except Exception as e:
            logger.warning(f"NewsAPI failed: {e}")
            return []

    async def _fetch_tavily(self, ticker: str, company_name: str) -> List[Dict]:
        """Fetch from Tavily (AI-powered search)."""
        try:
            import httpx
            
            is_uk = ticker.upper().endswith(".L")
            
            if ticker == "MARKET":
                query = "latest stock market outlook for LSE (UK), NSE (India), and US markets"
            else:
                region = "LSE UK" if is_uk else "US"
                query = f"latest financial news for {company_name} ({ticker}) stock on {region} market"
            
            url = "https://api.tavily.com/search"
            headers = {"Content-Type": "application/json"}
            payload = {
                "api_key": self.tavily_key,
                "query": query,
                "search_depth": "advanced",
                "max_results": 8
            }
            
            from resilience import execute_with_breaker
            response = await execute_with_breaker(tavily_cb, "POST", url, headers=headers, json=payload)
            
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
        except CircuitBreakerOpenError:
            logger.warning(f"Tavily skipped: circuit breaker is OPEN")
            return []
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
            
            # Base params
            params = {
                "apikey": self.newsdata_key,
                "language": "en",
                "excludefield": "ai_summary" # simplified as per verification
            }

            if ticker == "MARKET":
                 params = await self._generate_smart_query("Global stock market outlook")
                 url = "https://newsdata.io/api/1/latest" # Smart query usually works better with latest unless strict market endpoint needed
                 # But sticking to user preference for 'market' endpoint if 'q' is missing? 
                 # Actually, smart query returns 'q', so we use /latest usually.
                 # However, if the LLM suggests strict market logic, we follow.
                 # Let's try /latest for flexibility with smart 'q'.
            else:
                 user_prompt = f"News for {company_name} ({ticker})"
                 params = await self._generate_smart_query(user_prompt)
                 url = "https://newsdata.io/api/1/latest"

            # Fallback if LLM fails
            if not params:
                 logger.info("Smart query generation failed, using fallback logic.")
                 params = {
                    "apikey": self.newsdata_key,
                    "language": "en",
                    "excludefield": "ai_summary"
                 }
                 if ticker == "MARKET":
                     url = "https://newsdata.io/api/1/market"
                     params.update({"removeduplicate": 0})
                 else:
                     url = "https://newsdata.io/api/1/latest"
                     q = f"{company_name} OR {ticker} stock" if company_name else f"{ticker} stock"
                     params["q"] = q
                     is_uk = ticker.upper().endswith(".L")
                     params["country"] = "gb" if is_uk else "us"
                     # Add 'in' for India support if requested, but architecture mandates US/UK partitioning
                     if "NSE" in ticker.upper(): params["country"] = "in"

            async def _do_fetch_newsdata():
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(url, params=params)
                    response.raise_for_status()
                    return response.json()

            data = await newsdata_cb.call(_do_fetch_newsdata)
            
            # Normalize to common format
            articles = []
            for article in data.get('results', [])[:10]:
                articles.append({
                    'title': article.get('title'),
                    'description': article.get('description') or article.get('content'),
                    'source': {'name': article.get('source_id', 'NewsData.io')}, 
                    'url': article.get('link')
                })
            
            logger.info(f"NewsData.io returned {len(articles)} articles")
            return articles
            
        except CircuitBreakerOpenError:
            logger.warning(f"NewsData.io skipped: circuit breaker is OPEN")
            return []
        except Exception as e:
            logger.warning(f"NewsData.io failed: {e}")
            return []

    async def _generate_smart_query(self, user_query: str) -> Optional[Dict]:
        """
        SOTA 2026: Agentic News Query Generation via Magentic.
        Uses structured output via Pydantic to ensure valid API parameters.
        """
        try:
            # Context for the LLM
            market_context = f"Analyzing {user_query} for market-moving events."
            
            # Execute magentic prompt (async execution)
            # This is significantly more robust than manual string parsing.
            params_obj = await asyncio.to_thread(generate_news_query, user_query, user_query, market_context)
            
            # Convert Pydantic object to dict for the API client
            params = params_obj.model_dump(exclude_none=True)
            
            # Ensure API key and other required fields are present
            params["apikey"] = self.newsdata_key
            params["language"] = "en"
            params["excludefield"] = "ai_summary"
            
            logger.info(f"Smart Query Generated (Magentic): {params}")
            return params
            
        except Exception as e:
            logger.warning(f"Magentic smart query generation failed: {e}. Falling back to basic query.")
            # Fail-soft: Return a basic query if the structured generation fails
            return {
                "q": f"{user_query} stock market news",
                "apikey": self.newsdata_key,
                "language": "en"
            }

    def _deduplicate_articles(self, articles: List[Dict]) -> List[Dict]:
        """Remove duplicate articles based on normalized title or URL."""
        seen_titles = set()
        seen_urls = set()
        unique = []
        for art in articles:
            title = art.get('title', '').lower().strip()
            url = art.get('url', '')
            # Simple normalization: remove non-alphanumeric for title check
            clean_title = TITLE_CLEAN_PATTERN.sub('', title)
            
            if clean_title and clean_title not in seen_titles and (not url or url not in seen_urls):
                seen_titles.add(clean_title)
                if url:
                    seen_urls.add(url)
                unique.append(art)
        return unique

    def _analyze_sentiment(self, articles: List[Dict], analyzer) -> tuple:
        """Analyze sentiment of articles using VADER and create rich NewsArticle objects."""
        sentiments = []
        weights = []
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
            
            # SOTA 2026: Institutional Weighting
            is_reg = article.get('is_regulatory', False)
            weight = 2.0 if is_reg else 1.0
            
            sentiments.append(compound * weight)
            weights.append(weight)
            
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
        
        return sentiments, weights, rich_articles, headlines, sources

    def _get_sentiment_label(self, score: float) -> str:
        """Convert sentiment score to label."""
        if score >= 0.05:
            return "BULLISH"
        elif score <= -0.05:
            return "BEARISH"
        return "NEUTRAL"
