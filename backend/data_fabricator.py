"""
Data Fabrication Engine
Centralized, deterministic data fetching module to replace ad-hoc agent fetching.
Prevents "God Object" Coordinator by isolating IO logic here.
"""

import asyncio
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

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
        
    async def fabricate_context(self, intent: str, ticker: Optional[str], account_type: Optional[str], user_settings: Dict[str, Any] = {}) -> MarketContext:
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
        """Fetch OHLCV and current price with robust normalization"""
        try:
            from utils.data_frayer import get_data_frayer
            frayer = get_data_frayer()
            
            # Use SOTA Data Fraying to combine all providers
            frayed_bars = await frayer.fetch_frayed_bars(ticker, limit=1000)
            
            # Extract last close and history
            last_hist_close = 0.0
            history_series = []
            
            if frayed_bars:
                last_hist_close = float(frayed_bars[-1]["c"])
                history_series = [
                    TimeSeriesItem(
                        timestamp=int(b['t']),
                        open=float(b['o']),
                        high=float(b['h']),
                        low=float(b['l']),
                        close=float(b['c']),
                        volume=float(b['v'])
                    ) for b in frayed_bars
                ]

            # Fetch current quote from primary source
            quote_result = await self.finnhub.get_real_time_quote(ticker)
            current_price = 0.0
            currency = "USD"
            
            if quote_result and "current_price" in quote_result and quote_result["current_price"] > 0:
                 current_price = float(quote_result["current_price"])

            # --- SOTA DATA VALIDATION LOGIC ---
            # Compare Real-Time Quote vs Historical Close to detect anomalies (Unit mismatch, API errors)
            needs_verification = False
            
            if last_hist_close > 0 and current_price > 0:
                diff_pct = abs(current_price - last_hist_close) / last_hist_close
                
                # Check for Pence/Pound mismatch (approx 100x factor)
                if 90 < (current_price / last_hist_close) < 110:
                    logger.warning(f"Data Mismatch (GBX/GBP): History={last_hist_close}, Curr={current_price}. Treating as GBX->GBP adjustment.")
                    current_price /= 100.0  # Normalize to match history (assuming history is valid base)
                    
                elif 0.009 < (current_price / last_hist_close) < 0.011:
                     logger.warning(f"Data Mismatch (GBP/GBX): History={last_hist_close}, Curr={current_price}. Treating as GBP->GBX adjustment.")
                     current_price *= 100.0
                     
                elif diff_pct > 0.20: # >20% unexplained gap
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
                        
                    yf_price = await asyncio.to_thread(fetch_yf_price)
                    
                    if yf_price > 0:
                        # Verify against History
                        yf_diff = abs(yf_price - last_hist_close) / last_hist_close if last_hist_close > 0 else 0.0
                        
                        # Decision Matrix
                        if last_hist_close > 0 and yf_diff < 0.10:
                            logger.info(f"Verification: Rejecting Finnhub ({current_price}), Accepting YF ({yf_price}) which matches History.")
                            current_price = yf_price
                        elif abs(yf_price - current_price) / current_price < 0.05:
                            logger.info(f"Verification: YF ({yf_price}) confirms Finnhub ({current_price}). Real Volatility detected.")
                            # Current price accepted
                        else:
                            # Both diverge from history, or conflict. 
                            # If they agree with each other (even if far from history), take them?
                            # If they disagree, fallback to HISTORY.
                            if abs(yf_price - current_price) / current_price < 0.10:
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

            return PriceData(
                ticker=ticker,
                current_price=float(current_price),
                currency=currency,
                source="DataFabricator",
                history_series=history_series
            )
        except Exception as e:
            logger.error(f"Price fetch failed for {ticker}: {e}")
            # FALBACK: Return empty PriceData instead of None to prevent crashes
            return PriceData(
                ticker=ticker,
                current_price=0.0,
                currency="USD",
                source="error_fallback",
                history_series=[]
            )

    async def _fetch_news_data(self, ticker: str) -> Optional[ResearchData]:
        """
        Fetch news using existing logic (migrated from ResearchAgent).
        For now, we'll keep the actual API call logic in the existing helper or mock it, 
        but in a full migration, this would call NewsData.io directly.
        """
        try:
            # Placeholder: In a real refactor, we'd move the NewsDataIOClient usage here.
            # Using a basic stub that would be populated by the actual API
            # For Phase 1, we might return None and let the ResearchAgent fallback run?
            # No, the goal is centralized fetching.
            
            # TODO: Import NewsDataIOClient here when migrated
            return None 
            
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
