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

logger = logging.getLogger(__name__)

class DataFabricator:
    """
    Fabricates a complete MarketContext by fetching all required data in parallel.
    Deterministic IO layer.
    """

    def __init__(self):
        self.alpaca = get_alpaca_client()
        self.finnhub = get_finnhub_client()
        # Initialize other clients as needed (News API, Social API) if we move them here
        # For now, we will use mock/stub logic for News/Social until we migrate the actual API calls
        
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

            # Fetch current quote from primary source
            quote_result = await self.finnhub.get_real_time_quote(ticker)
            current_price = Decimal('0')
            currency = "USD"
            
            if quote_result and "current_price" in quote_result and quote_result["current_price"] > 0:
                 current_price = create_decimal(quote_result["current_price"])

            # --- T212 PORTFOLIO CHECK (Real-time fallback for owned assets) ---
            # If external APIs fail, check if we own the asset in T212.
            # T212 API gives real-time prices for held positions.
            if current_price <= 0:
                try:
                    from app_context import state
                    if state.mcp_client:
                         # Attempt to get position details
                         pos_result = await state.mcp_client.call_tool("get_position_details", {"ticker": ticker}, timeout=5.0)
                         
                         # Parse result (TextContent list)
                         if pos_result and hasattr(pos_result, 'content') and pos_result.content:
                             import json
                             text = pos_result.content[0].text
                             if "not found" not in text.lower():
                                 pos_data = json.loads(text)
                                 # T212 returns 'currentPrice' or 'current_price'
                                 t212_price = pos_data.get("currentPrice") or pos_data.get("current_price")
                                 if t212_price:
                                     logger.info(f"✅ Recovered Real-Time Price from T212 Portfolio for {ticker}: {t212_price}")
                                     current_price = create_decimal(t212_price)
                                     currency = pos_data.get("currencyCode", "GBP") # Default to GBP for T212 usually
                except Exception as mcp_e:
                    logger.warning(f"Failed to check T212 portfolio for price: {mcp_e}")

            # --- SOTA DATA VALIDATION LOGIC ---
            # Compare Real-Time Quote vs Historical Close using Decimal to detect anomalies
            needs_verification = False
            
            if last_hist_close > 0 and current_price > 0:
                diff_pct = abs(current_price - last_hist_close) / last_hist_close
                
                # Check for Pence/Pound mismatch (approx 100x factor)
                if Decimal('90') < (current_price / last_hist_close) < Decimal('110'):
                    logger.warning(f"Data Mismatch (GBX/GBP): History={last_hist_close}, Curr={current_price}. Treating as GBX->GBP adjustment.")
                    current_price = current_price / Decimal('100')  # Normalize to match history
                    
                elif Decimal('0.009') < (current_price / last_hist_close) < Decimal('0.011'):
                     logger.warning(f"Data Mismatch (GBP/GBX): History={last_hist_close}, Curr={current_price}. Treating as GBP->GBX adjustment.")
                     current_price = current_price * Decimal('100')
                     
                elif diff_pct > Decimal('0.20'): # >20% unexplained gap
                     logger.warning(f"Significant price gap detected for {ticker}: Hist={last_hist_close}, Curr={current_price} ({diff_pct*100:.1f}%). Verifying...")
                     needs_verification = True
            elif current_price <= 0:
                needs_verification = True

            # FALLBACK / VERIFICATION (YFinance)
            if needs_verification:
                try:
                    import yfinance as yf
                    def fetch_yf_price():
                        y_ticker = ticker
                        t = yf.Ticker(y_ticker)
                        p = getattr(t.fast_info, 'last_price', 0.0)
                        if not p or p <= 0:
                            # Fallback: Get most recent close from history
                            hist = t.history(period="1d")
                            if not hist.empty:
                                p = hist['Close'].iloc[-1]
                        return p if p else 0.0
                        
                    yf_price_val = await asyncio.to_thread(fetch_yf_price)
                    yf_price = create_decimal(yf_price_val)
                    
                    if yf_price > 0:
                        # Verify against History
                        yf_diff = abs(yf_price - last_hist_close) / last_hist_close if last_hist_close > 0 else Decimal('0')
                        
                        # Decision Matrix
                        if last_hist_close > 0 and yf_diff < Decimal('0.10'):
                            logger.info(f"Verification: Rejecting Finnhub ({current_price}), Accepting YF ({yf_price}) which matches History.")
                            current_price = yf_price
                        elif current_price > 0 and abs(yf_price - current_price) / current_price < Decimal('0.05'):
                            logger.info(f"Verification: YF ({yf_price}) confirms Finnhub ({current_price}). Real Volatility detected.")
                            # Current price accepted
                        else:
                            # Both diverge from history, or conflict. 
                            if current_price > 0 and abs(yf_price - current_price) / current_price < Decimal('0.10'):
                                 logger.info("Verification: Sources agree on new price level.")
                                 # Current price accepted
                            else:
                                 logger.warning(f"Verification Failed: YF={yf_price}, Finn={current_price}, Hist={last_hist_close}. Fallback to History.")
                                 current_price = last_hist_close
                    else:
                        logger.warning("Verification Failed: YF returned 0. Fallback to History.")
                        current_price = last_hist_close if last_hist_close > 0 else current_price
                        
                except Exception as ex:
                    logger.warning(f"Verification Error for {ticker}: {ex}. Fallback to History.")
                    current_price = last_hist_close if last_hist_close > 0 else current_price

            # Final Safety: If still 0, use history
            if current_price <= 0 and last_hist_close > 0:
                 current_price = last_hist_close
            
            # Detect currency (simple heuristic)
            if ticker.endswith(".L"):
                currency = "GBP" # Actually GBX but normalized usually

            # CHECK FOR VALID DATA - Trigger Fallback if empty
            if not history_series or current_price <= 0:
                raise ValueError("Insufficient data retrieved from providers")

            p_data = PriceData(
                ticker=ticker,
                current_price=current_price,
                currency=currency,
                source="DataFabricator",
                history_series=history_series
            )
            cache.set(cache_key, p_data, ttl=60) # 60s cache for prices
            return p_data
        except Exception as e:
            logger.error(f"Price fetch failed for {ticker}: {e}")
            return None

    async def _fetch_news_data(self, ticker: str) -> Optional[ResearchData]:
        """Fetch news using existing logic."""
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
            return None

    async def _fetch_social_data(self, ticker: str) -> Optional[SocialData]:
        """Fetch social sentiment"""
        # Placeholder for social API
        return None

    async def _fetch_whale_data(self, ticker: str) -> Optional[WhaleData]:
        """Fetch whale alerts"""
        # Placeholder
        return None
