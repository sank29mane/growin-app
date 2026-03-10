"""
Data Fabrication Engine
Centralized, deterministic data fetching module to replace ad-hoc agent fetching.
Prevents "God Object" Coordinator by isolating IO logic here.
"""

import asyncio
import logging
from typing import Dict, Any, Optional
from datetime import datetime

from market_context import MarketContext, PriceData, TimeSeriesItem, ResearchData, NewsArticle, SocialData, WhaleData
from data_engine import get_alpaca_client, get_finnhub_client
from utils.news_client import NewsDataIOClient

logger = logging.getLogger(__name__)

class DataFabricator:
    """
    Fabricates a complete MarketContext by fetching all required data in parallel.
    Deterministic IO layer.
    """

    def __init__(self):
        self.alpaca = get_alpaca_client()
        self.finnhub = get_finnhub_client()
        self.news_client = NewsDataIOClient()
        
    async def fabricate_context(self, intent: str, ticker: Optional[str], account_type: Optional[str], user_settings: Optional[Dict[str, Any]] = None) -> MarketContext:
        """
        Main entry point: Build the context based on intent.
        
        Args:
            intent: "market_analysis", "price_check", etc.
            ticker: The primary ticker symbol (if any).
            account_type: "invest", "isa", or None.
            user_settings: Validated user settings (e.g. risk profile).
            
        Returns:
            MarketContext: Populated with all available raw data.
        """
        if user_settings is None:
            user_settings = {}
        start_time = datetime.now()
        
        # 1. Initialize empty context
        context = MarketContext(
            query=f"Auto-generated for {intent}", # Will be overwritten by coordinator usually
            intent=intent,
            ticker=ticker,
            user_context={"account_type": account_type, **user_settings}
        )
        
        # 2. Determine what to fetch based on Intent
        tasks = []
        
        # Always fetch Price if we have a ticker
        if ticker:
            tasks.append(self._fetch_price_data(ticker))
        
        # Fetch News/Social for analysis
        if intent in ["market_analysis", "analytical", "forecast_request"] and ticker:
            tasks.append(self._fetch_news_data(ticker))
            tasks.append(self._fetch_social_data(ticker))
            
        # Fetch Whale data for deep dives
        if intent in ["market_analysis", "whale_watch"] and ticker:
            tasks.append(self._fetch_whale_data(ticker))

        # 3. Execute IO in parallel
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 4. Populate Context
            for res in results:
                if isinstance(res, Exception):
                    logger.error(f"DataFabricator fetch failed: {res}")
                    continue
                
                if res is None:
                    continue

                # Type-based injection
                if isinstance(res, PriceData):
                    context.price = res
                elif isinstance(res, ResearchData):
                    context.research = res
                elif isinstance(res, SocialData):
                    context.social = res
                elif isinstance(res, WhaleData):
                    context.whale = res
                    
        # 5. Measure latency
        context.total_latency_ms = (datetime.now() - start_time).total_seconds() * 1000
        
        return context

    async def _fetch_price_data(self, ticker: str) -> Optional[PriceData]:
        from cache_manager import cache
        from decimal import Decimal
        from utils.financial_math import create_decimal, safe_div

        cache_key = f"price_data:{ticker}"
        cached = cache.get(cache_key)
        if cached:
            # logger.info(f"Cache hit for {ticker}")
            return cached

        try:
            from status_manager import status_manager
            status_manager.set_status("coordinator", "working", f"Fetching price data for {ticker}...")
            
            from utils.data_frayer import get_data_frayer
            frayer = get_data_frayer()
            
            # Use SOTA Data Fraying to combine all providers
            frayed_bars = await frayer.fetch_frayed_bars(ticker, limit=1000)
            
            # Extract last close and history using Decimal
            last_hist_close = Decimal('0')
            history_series = []
            
            if frayed_bars:
                last_hist_close = create_decimal(frayed_bars[-1]["c"])
                history_series = [
                    TimeSeriesItem(
                        timestamp=int(b['t']),
                        open=create_decimal(b['o']),
                        high=create_decimal(b['h']),
                        low=create_decimal(b['l']),
                        close=create_decimal(b['c']),
                        volume=create_decimal(b['v'])
                    ) for b in frayed_bars
                ]

            # Fetch current quote from mandated source with fallback
            current_price = Decimal('0')
            currency = "USD"
            source_used = "Unknown"
            is_uk = ticker.upper().endswith(".L")

            async def fetch_primary_quote():
                nonlocal current_price, currency, source_used
                if not is_uk:
                    # US Primary: Alpaca
                    quote_result = await self.alpaca.get_real_time_quote(ticker)
                    if quote_result and "current_price" in quote_result and quote_result["current_price"] > 0:
                        current_price = create_decimal(quote_result["current_price"])
                        source_used = "Alpaca"
                        return True
                else:
                    # UK Primary: Finnhub
                    quote_result = await self.finnhub.get_real_time_quote(ticker)
                    if quote_result and "current_price" in quote_result and quote_result["current_price"] > 0:
                        current_price = create_decimal(quote_result["current_price"])
                        source_used = "Finnhub"
                        currency = "GBP"
                        return True
                return False

            primary_success = await fetch_primary_quote()

            if not primary_success:
                # UNIVERSAL FALLBACK: Yahoo Finance
                try:
                    import yfinance as yf
                    def fetch_yf_quote():
                        t = yf.Ticker(ticker)
                        p = getattr(t.fast_info, 'last_price', 0.0)
                        if not p or p <= 0:
                            hist = t.history(period="1d")
                            if not hist.empty: p = hist['Close'].iloc[-1]
                        return p if p else 0.0
                    
                    yf_val = await asyncio.to_thread(fetch_yf_quote)
                    if yf_val > 0:
                        current_price = create_decimal(yf_val)
                        source_used = "YFinance"
                        currency = "GBP" if is_uk else "USD"
                        logger.info(f"✅ Fallback yfinance Quote for {ticker}: {current_price}")
                except Exception as yf_e:
                    logger.warning(f"Fallback yfinance quote failed for {ticker}: {yf_e}")

            # --- T212 PORTFOLIO CHECK (Real-time fallback for owned assets) ---
            if current_price <= 0:
                try:
                    from app_context import state
                    if state.mcp_client:
                         pos_result = await state.mcp_client.call_tool("get_position_details", {"ticker": ticker}, timeout=5.0)
                         if pos_result and hasattr(pos_result, 'content') and pos_result.content:
                             import json
                             text = pos_result.content[0].text
                             if "not found" not in text.lower():
                                 pos_data = json.loads(text)
                                 t212_price = pos_data.get("currentPrice") or pos_data.get("current_price")
                                 if t212_price:
                                     logger.info(f"✅ Recovered Real-Time Price from T212 Portfolio for {ticker}: {t212_price}")
                                     current_price = create_decimal(t212_price)
                                     source_used = "Trading212"
                                     currency = pos_data.get("currencyCode", "GBP")
                except Exception as mcp_e:
                    logger.warning(f"Failed to check T212 portfolio for price: {mcp_e}")

            # --- SOTA DATA VALIDATION LOGIC ---
            if last_hist_close > 0 and current_price > 0:
                # Check for Pence/Pound mismatch (approx 100x factor)
                # Ratio of 100 means current is GBX, history is GBP
                if Decimal('80') < (current_price / last_hist_close) < Decimal('120'):
                    logger.warning(f"Data Mismatch (GBX/GBP): History={last_hist_close}, Curr={current_price}. Treating as GBX->GBP adjustment.")
                    current_price = current_price / Decimal('100')
                    
                # Ratio of 0.01 means current is GBP, history is GBX
                elif Decimal('0.008') < (current_price / last_hist_close) < Decimal('0.012'):
                     logger.warning(f"Data Mismatch (GBP/GBX): History={last_hist_close}, Curr={current_price}. Treating as GBP->GBX adjustment.")
                     current_price = current_price * Decimal('100')
            
            # Final Safety: If still 0, use history
            if current_price <= 0 and last_hist_close > 0:
                 current_price = last_hist_close
                 source_used = "History"

            # CHECK FOR VALID DATA - Trigger Fallback if empty
            if not history_series or current_price <= 0:
                raise ValueError("Insufficient data retrieved from providers")

            p_data = PriceData(
                ticker=ticker,
                current_price=current_price,
                currency=currency,
                source=source_used,
                history_series=history_series
            )
            cache.set(cache_key, p_data, ttl=60) # 60s cache for prices
            return p_data
        except Exception as e:
            logger.error(f"Price fetch failed for {ticker}: {e}")
            return None

    async def _fetch_news_data(self, ticker: str) -> Optional[ResearchData]:
        """Fetch news using NewsDataIOClient."""
        from status_manager import status_manager
        status_manager.set_status("research_agent", "working", f"Searching news for {ticker}...")
        try:
            # 1. Fetch from NewsData.io (centralized)
            articles = await self.news_client.fetch_latest_news(ticker)
            
            if not articles:
                return ResearchData(ticker=ticker, sentiment_score=0.0, sentiment_label="NEUTRAL", articles=[])

            # 2. Simple sentiment synthesis (Logic move from ResearchAgent)
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            analyzer = SentimentIntensityAnalyzer()
            
            sentiments = []
            rich_articles = []
            for art in articles[:5]:
                text = f"{art['title']}. {art.get('description', '')}"
                score = analyzer.polarity_scores(text)['compound']
                sentiments.append(score)
                rich_articles.append(NewsArticle(
                    title=art['title'],
                    description=art.get('description', ''),
                    source=art['source']['name'],
                    sentiment=score,
                    url=art.get('url')
                ))
            
            avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0
            
            label = "NEUTRAL"
            if avg_sentiment >= 0.05: label = "BULLISH"
            elif avg_sentiment <= -0.05: label = "BEARISH"

            return ResearchData(
                ticker=ticker,
                sentiment_score=avg_sentiment,
                sentiment_label=label,
                articles=rich_articles
            ) 
            
        except Exception as e:
            logger.error(f"News fetch failed: {e}")
            return None

    async def _fetch_social_data(self, ticker: str) -> Optional[SocialData]:
        """Fetch social sentiment"""
        # Placeholder for social API
        return None

    async def _fetch_whale_data(self, ticker: str) -> Optional[WhaleData]:
        """Fetch whale alerts"""
        # Placeholder
        return None
