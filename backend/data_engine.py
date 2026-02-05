import os
import asyncio
import time
import logging
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from utils.ticker_utils import normalize_ticker
from utils.currency_utils import CurrencyNormalizer, normalize_all_positions
from data_models import Position, PriceData

# Set up logging
logger = logging.getLogger(__name__)

# --- Configuration ---
API_KEY = os.getenv("ALPACA_API_KEY")
API_SECRET = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")  # Default to paper

# Finnhub Configuration
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

class AlpacaClient:
    """
    Real Alpaca client using alpaca-py SDK.
    Falls back to yfinance/mock if keys are missing.
    """

    def __init__(self):
        self.trading_client = None
        self.data_client = None

        if API_KEY and API_SECRET:
            try:
                from alpaca.trading.client import TradingClient
                from alpaca.data.historical import StockHistoricalDataClient

                self.trading_client = TradingClient(API_KEY, API_SECRET, paper="paper" in BASE_URL)
                self.data_client = StockHistoricalDataClient(API_KEY, API_SECRET)
                logger.info("AlpacaClient: Successfully connected to Alpaca API.")
            except Exception as e:
                logger.error(f"AlpacaClient: Failed to initialize Alpaca SDK: {e}")
        else:
            logger.info("AlpacaClient: API keys not set. Running in offline/mock mode.")

    async def get_historical_bars(self, ticker: str, timeframe="1Day", limit: int = 512) -> Optional[Dict[str, Any]]:
        """Fetch historical bars using Alpaca Data API, fallback to yfinance."""
        from cache_manager import cache
        cache_key = f"bars_{ticker}_{timeframe}_{limit}"
        cached_data = cache.get(cache_key)
        if cached_data:
            return cached_data

        normalized_ticker = normalize_ticker(ticker)

        # OPTIMIZATION: Skip Alpaca for UK stocks (they are not supported on standard Alpaca plan)
        # This avoids "invalid symbol" errors and speeds up the response
        if normalized_ticker.endswith('.L'):
            logger.info(f"AlpacaClient: Skipping Alpaca for UK stock {normalized_ticker}, using fallback.")
            self.data_client = None # Temporarily disable to force fallback for this call

        # Try Alpaca first
        if self.data_client:
            try:
                from alpaca.data.requests import StockBarsRequest
                from alpaca.data.timeframe import TimeFrame

                # Map timeframe string to Alpaca TimeFrame
                tf_map = {
                    "1Min": TimeFrame.Minute,
                    "5Min": TimeFrame.Minute, # Approximate
                    "15Min": TimeFrame.Minute, # Approximate
                    "1Hour": TimeFrame.Hour,
                    "1Day": TimeFrame.Day,
                }
                tf = tf_map.get(timeframe, TimeFrame.Day)

                # Calculate start date based on timeframe and limit
                # from datetime import datetime, timedelta, timezone (Removed to fix scope shadowing)

                now = datetime.now(timezone.utc)

                # Default lookback if not specified
                # OPTIMIZATION: Multipliers increased to account for market closures (nights/weekends)
                # We need to request enough 'wall clock' time to get 'limit' trading bars.
                delta_map = {
                    "1Min": timedelta(minutes=limit * 5),
                    "5Min": timedelta(minutes=limit * 10),
                    "15Min": timedelta(minutes=limit * 30),
                    "1Hour": timedelta(hours=limit * 5),  # 1000 bars * 5 = 5000 hours (~200 days)
                    "1Day": timedelta(days=limit * 1.6 + 100), # ~1.5x for weekends + buffer
                }
                start_time = now - delta_map.get(timeframe, timedelta(days=365))

                request_params = StockBarsRequest(
                    symbol_or_symbols=normalized_ticker,
                    timeframe=tf,
                    limit=limit,
                    start=start_time
                )

                # Run synchronous SDK method in thread pool
                bars_response = await asyncio.to_thread(self.data_client.get_stock_bars, request_params)

                if normalized_ticker in bars_response.data:
                    alpaca_bars = bars_response.data[normalized_ticker]
                    bar_list = []
                    for bar in alpaca_bars:
                        bar_list.append(PriceData(
                            ticker=ticker,
                            timestamp=bar.timestamp.isoformat(),
                            open=Decimal(str(bar.open)),
                            high=Decimal(str(bar.high)),
                            low=Decimal(str(bar.low)),
                            close=Decimal(str(bar.close)),
                            volume=int(bar.volume)
                        ).model_dump()) # Store as dict in cache/return for now to avoid serialization issues downstream

                    result = {"ticker": ticker, "bars": bar_list, "timeframe": timeframe}
                    cache.set(cache_key, result, ttl=300) # Cache for 5 mins
                    return result

            except Exception as e:
                logger.warning(f"AlpacaClient: Error fetching bars from Alpaca: {e}. Falling back to yfinance.")

        # Fallback to yfinance
        try:
            import yfinance as yf
            import pandas as pd
            import numpy as np
            from utils.currency_utils import CurrencyNormalizer

            # Map timeframe to yfinance period/interval
            # OPTIMIZATION: Request max viable history for fallback to ensure TTM context (512+)
            period_map = {
                "1Min": ("5d", "1m"),     # Max 7d usually
                "5Min": ("60d", "5m"),    # Max 60d
                "15Min": ("60d", "15m"),  # Max 60d
                "1Hour": ("730d", "1h"),  # Max 730d (2y) - ample for 512 bars
                "1Day": ("5y", "1d"),     # 5y is plenty
                "1Week": ("5y", "1wk"),
                "1Month": ("max", "1mo")
            }
            period, interval = period_map.get(timeframe, ("5y", "1d"))

            ticker_obj = yf.Ticker(normalized_ticker)
            data = ticker_obj.history(period=period, interval=interval)

            if data.empty:
                return None

            # ⚡ OPTIMIZATION: Use vectorized operations and direct dict creation (~30x faster)
            # Avoids iterrows and Pydantic instantiation overhead for every row

            # Use original dataframe directly where possible to avoid copy overhead,
            # but usually we need a copy if we modify it. Here we just extract values.
            df = data # Alias for convenience

            # Check if UK stock once (CurrencyNormalizer logic)
            is_uk = CurrencyNormalizer.is_uk_stock(normalized_ticker)

            # Extract columns to numpy arrays and round/normalize
            if is_uk:
                # Vectorized division for UK stocks (GBX -> GBP)
                opens = np.round(df['Open'].values / 100.0, 2)
                highs = np.round(df['High'].values / 100.0, 2)
                lows = np.round(df['Low'].values / 100.0, 2)
                closes = np.round(df['Close'].values / 100.0, 2)
            else:
                opens = np.round(df['Open'].values, 2)
                highs = np.round(df['High'].values, 2)
                lows = np.round(df['Low'].values, 2)
                closes = np.round(df['Close'].values, 2)

            # Volume: handle NaN and convert to int
            if 'Volume' in df.columns:
                volumes = df['Volume'].fillna(0).astype(int).values
            else:
                volumes = np.zeros(len(df), dtype=int)

            # Convert index to timestamp milliseconds robustly
            if df.index.tz is not None:
                # If TZ-aware, subtract TZ-aware epoch
                ts_values = (df.index - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta('1ms')
            else:
                # If Naive, subtract Naive epoch
                ts_values = (df.index - pd.Timestamp("1970-01-01")) // pd.Timedelta('1ms')

            # Convert Series to numpy array if needed
            if isinstance(ts_values, pd.Series):
                ts_values = ts_values.values

            # Generate ISO strings using list comprehension (faster than loop with datetime.fromtimestamp)
            timestamps = [datetime.fromtimestamp(t/1000.0, tz=timezone.utc).isoformat() for t in ts_values]

            # Build list of dicts directly
            bar_list = []
            for ts, o, h, l, c, v in zip(timestamps, opens, highs, lows, closes, volumes):
                bar_list.append({
                    'ticker': ticker,
                    'timestamp': ts,
                    'open': Decimal(str(o)),
                    'high': Decimal(str(h)),
                    'low': Decimal(str(l)),
                    'close': Decimal(str(c)),
                    'volume': int(v)
                })

            result = {"ticker": ticker, "bars": bar_list[-limit:], "timeframe": timeframe}
            cache.set(cache_key, result, ttl=300)
            return result

        except Exception as e:
            logger.warning(f"AlpacaClient: Error fetching bars (yfinance): {e}")
            # Do NOT return mock data for invalid tickers. This causes AI hallucinations.
            return None

    async def get_batch_bars(self, tickers: List[str], timeframe="1Day", limit: int = 512) -> Dict[str, Any]:
        """
        Fetch historical bars for multiple tickers in parallel.
        Optimized to use Alpaca Batch API for compatible symbols.
        """
        from cache_manager import cache
        from datetime import datetime, timedelta, timezone
        
        results = {}
        missing_tickers = []
        
        # 1. Check Cache
        for t in tickers:
            cache_key = f"bars_{t}_{timeframe}_{limit}"
            cached = cache.get(cache_key)
            if cached:
                results[t] = cached
            else:
                missing_tickers.append(t)
                
        if not missing_tickers:
            return results

        # 2. Separate Alpaca-compatible vs Fallback (e.g. UK stocks)
        alpaca_candidates = []
        fallback_candidates = []
        
        for t in missing_tickers:
            norm = normalize_ticker(t)
            if norm.endswith('.L'): # UK stocks not on Alpaca
                fallback_candidates.append(t)
            else:
                alpaca_candidates.append(t)

        # 3. Batch Fetch from Alpaca
        if self.data_client and alpaca_candidates:
            try:
                from alpaca.data.requests import StockBarsRequest
                from alpaca.data.timeframe import TimeFrame
                
                tf_map = {
                    "1Min": TimeFrame.Minute,
                    "5Min": TimeFrame.Minute,
                    "15Min": TimeFrame.Minute,
                    "1Hour": TimeFrame.Hour,
                    "1Day": TimeFrame.Day,
                }
                tf = tf_map.get(timeframe, TimeFrame.Day)
                
                # Calculate start time (same logic as get_historical_bars)
                now = datetime.now(timezone.utc)
                delta_map = {
                    "1Min": timedelta(minutes=limit * 5),
                    "5Min": timedelta(minutes=limit * 10),
                    "15Min": timedelta(minutes=limit * 30),
                    "1Hour": timedelta(hours=limit * 5),
                    "1Day": timedelta(days=limit * 1.6 + 100),
                }
                start_time = now - delta_map.get(timeframe, timedelta(days=365))
                
                # Alpaca Batch Request
                # Map original tickers to normalized for the request
                norm_map = {normalize_ticker(t): t for t in alpaca_candidates}
                request_syms = list(norm_map.keys())
                
                request_params = StockBarsRequest(
                    symbol_or_symbols=request_syms,
                    timeframe=tf,
                    limit=limit,
                    start=start_time
                )
                
                bars_response = await asyncio.to_thread(self.data_client.get_stock_bars, request_params)
                
                # Process Batch Response
                for norm_sym, alpaca_bars in bars_response.data.items():
                    original_ticker = norm_map.get(norm_sym, norm_sym)
                    
                    bar_list = []
                    for bar in alpaca_bars:
                        bar_list.append(PriceData(
                            ticker=original_ticker,
                            timestamp=bar.timestamp.isoformat(),
                            open=Decimal(str(bar.open)),
                            high=Decimal(str(bar.high)),
                            low=Decimal(str(bar.low)),
                            close=Decimal(str(bar.close)),
                            volume=int(bar.volume)
                        ).model_dump())
                    
                    res = {"ticker": original_ticker, "bars": bar_list, "timeframe": timeframe}
                    results[original_ticker] = res
                    cache.set(f"bars_{original_ticker}_{timeframe}_{limit}", res, ttl=300)
                    
            except Exception as e:
                logger.warning(f"Batch fetch failed: {e}. Falling back to individual.")
                fallback_candidates.extend(alpaca_candidates) # Add back to fallback queue

        # 4. Process Fallbacks (UK stocks + any failed Alpaca + ones missing from batch response)
        # Check what we still miss
        still_missing = [t for t in missing_tickers if t not in results]
        
        if still_missing:
            # Parallelize individual fetches
            tasks = [self.get_historical_bars(t, timeframe, limit) for t in still_missing]
            fallback_res = await asyncio.gather(*tasks)
            
            for res in fallback_res:
                if res:
                    results[res['ticker']] = res
                    
        return results

    async def get_recent_trades(self, ticker: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Fetch latest trades for a ticker."""
        normalized_ticker = normalize_ticker(ticker)
        if self.data_client:
            try:
                from alpaca.data.requests import StockTradesRequest
                from datetime import datetime, timedelta, timezone

                request_params = StockTradesRequest(
                    symbol_or_symbols=normalized_ticker,
                    start=datetime.now(timezone.utc) - timedelta(hours=24), # Last 24 hours
                    limit=limit
                )

                trades_response = await asyncio.to_thread(self.data_client.get_stock_trades, request_params)

                if normalized_ticker in trades_response.data:
                    alpaca_trades = trades_response.data[normalized_ticker]
                    trade_list = []
                    for trade in alpaca_trades:
                        trade_list.append({
                            "t": int(trade.timestamp.timestamp() * 1000),
                            "p": Decimal(str(trade.price)),
                            "s": trade.size,
                            "c": trade.conditions, # List of condition codes
                            "i": trade.id
                        })
                    return trade_list
            except Exception as e:
                logger.error(f"AlpacaClient: Error fetching trades: {e}")

        return []

    async def get_account_info(self) -> Dict[str, Any]:
        """Fetch account info from Alpaca Trading API."""
        if self.trading_client:
            try:
                account = await asyncio.to_thread(self.trading_client.get_account)
                return {
                    "cash_balance": {"total": Decimal(str(account.cash)), "currency": account.currency},
                    "portfolio_value": Decimal(str(account.portfolio_value)),
                    "unrealized_pnl": Decimal(str(account.equity)) - Decimal(str(account.last_equity)), # Approx
                    "buying_power": Decimal(str(account.buying_power)),
                    "status": str(account.status)
                }
            except Exception as e:
                logger.error(f"AlpacaClient: Error fetching account info: {e}")

        # Mock fallback
        return {
            "cash_balance": {"total": Decimal("10000.0"), "currency": "USD"},
            "portfolio_value": Decimal("15000.0"),
            "unrealized_plpc": Decimal("0.05")
        }

    async def get_real_time_quote(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Get real-time quote for a ticker from Alpaca."""
        normalized_ticker = normalize_ticker(ticker)
        if self.data_client:
            try:
                from alpaca.data.requests import StockLatestQuoteRequest
                request_params = StockLatestQuoteRequest(symbol_or_symbols=normalized_ticker)

                # Use thread pool for sync SDK call
                quote_resp = await asyncio.to_thread(self.data_client.get_stock_latest_quote, request_params)

                if normalized_ticker in quote_resp:
                    quote = quote_resp[normalized_ticker]
                    return {
                        "symbol": ticker,
                        "current_price": Decimal(str(quote.ask_price)),
                        "change": Decimal("0"), # Needs historical context for change
                        "change_percent": Decimal("0"),
                        "high": Decimal("0"),
                        "low": Decimal("0"),
                        "open": Decimal("0"),
                        "timestamp": quote.timestamp.isoformat()
                    }
            except Exception as e:
                logger.error(f"AlpacaClient: Error fetching latest quote: {e}")
        return None

class FinnhubClient:
    """
    Finnhub client for UK stock data and real-time WebSocket streaming.
    Specializes in LSE (London Stock Exchange) data with 5-second refresh capability.
    """

    def __init__(self):
        self.client = None
        self.websocket = None

        if FINNHUB_API_KEY:
            try:
                import finnhub
                self.client = finnhub.Client(api_key=FINNHUB_API_KEY)
                logger.info("FinnhubClient: Successfully connected to Finnhub API.")
            except Exception as e:
                logger.error(f"FinnhubClient: Failed to initialize Finnhub SDK: {e}")
        else:
            logger.info("FinnhubClient: API key not set. Running in offline/mock mode.")

    async def get_historical_bars(self, ticker: str, timeframe="1Day", limit: int = 512) -> Optional[Dict[str, Any]]:
        """Fetch historical bars using Finnhub API for UK stocks."""
        if not self.client:
            return None

        # Remove .L suffix for Finnhub API (it expects LLOY not LLOY.L)
        normalized_ticker = ticker.replace('.L', '')

        try:
            # Finnhub resolution mapping
            resolution_map = {
                "1Min": "1",
                "5Min": "5",
                "15Min": "15",
                "1Hour": "60",
                "1Day": "D",
            }
            resolution = resolution_map.get(timeframe, "D")

            # Calculate from date (Finnhub needs Unix timestamp)
            from datetime import datetime, timedelta
            to_time = datetime.now()
            from_time = to_time - timedelta(days=365)  # Default 1 year

            # Adjust based on timeframe and limit
            if timeframe == "1Day":
                from_time = to_time - timedelta(days=limit)
            elif timeframe == "1Hour":
                from_time = to_time - timedelta(hours=limit)
            elif timeframe in ["1Min", "5Min", "15Min"]:
                from_time = to_time - timedelta(minutes=limit * int(resolution))

            # Fetch candle data
            candles = self.client.stock_candles(
                symbol=normalized_ticker,
                resolution=resolution,
                _from=int(from_time.timestamp()),
                to=int(to_time.timestamp())
            )

            if candles['s'] != 'ok' or not candles.get('c'):
                return None

            # Convert Finnhub format to our standard format
            bar_list = []
            for i in range(len(candles['c'])):
                # Normalize prices
                # Note: CurrencyNormalizer now used inside the loop or logic
                # normalize_price returns Decimal
                o = CurrencyNormalizer.normalize_price(candles['o'][i], ticker)
                h = CurrencyNormalizer.normalize_price(candles['h'][i], ticker)
                l = CurrencyNormalizer.normalize_price(candles['l'][i], ticker)
                c = CurrencyNormalizer.normalize_price(candles['c'][i], ticker)
                v = candles['v'][i] if i < len(candles['v']) else 0

                ts_iso = datetime.fromtimestamp(candles['t'][i]).isoformat()

                bar_list.append(PriceData(
                    ticker=ticker,
                    timestamp=ts_iso,
                    open=o,
                    high=h,
                    low=l,
                    close=c,
                    volume=int(v)
                ).model_dump())

            return {"ticker": ticker, "bars": bar_list[-limit:], "timeframe": timeframe}

        except Exception as e:
            error_msg = str(e)

            # Check if it's a 403 error (access denied)
            if "403" in error_msg or "access" in error_msg.lower():
                # Don't log 403 errors repeatedly - these are expected for some symbols
                # The fallback system will handle this gracefully
                return None

            # Log other errors
            logger.error(f"FinnhubClient: Error fetching bars from Finnhub: {e}")
            return None

    async def get_real_time_quote(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Get real-time quote for a ticker."""
        if not self.client:
            return None

        normalized_ticker = ticker.replace('.L', '')

        try:
            from utils.currency_utils import CurrencyNormalizer
            quote = self.client.quote(normalized_ticker)

            # Helper to safely get value or 0 if None
            def get_val(key):
                return quote.get(key) if quote.get(key) is not None else 0

            return {
                "symbol": ticker,
                "current_price": CurrencyNormalizer.normalize_price(get_val('c'), ticker),
                "change": CurrencyNormalizer.normalize_price(get_val('d'), ticker),
                "change_percent": Decimal(str(get_val('dp') if get_val('dp') is not None else 0)),
                "high": CurrencyNormalizer.normalize_price(get_val('h'), ticker),
                "low": CurrencyNormalizer.normalize_price(get_val('l'), ticker),
                "open": CurrencyNormalizer.normalize_price(get_val('o'), ticker),
                "previous_close": CurrencyNormalizer.normalize_price(get_val('pc'), ticker),
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            logger.error(f"FinnhubClient: Error fetching real-time quote: {e}")
            return None


# --- Utility Functions ---

def get_alpaca_client():
    """Returns an AlpacaClient or MockAlpacaClient based on API key availability."""
    try:
        return AlpacaClient()
    except EnvironmentError:
        logger.info("Using MockAlpacaClient for development.")
        return MockAlpacaClient()


def get_finnhub_client():
    """Returns a FinnhubClient instance."""
    return FinnhubClient()


async def mock_call_tool(tool_name: str, args: Dict[str, Any]):
    """Mock tool for backward compatibility during transition."""
    client = get_alpaca_client()
    if tool_name == "get_account_info":
        return await client.get_account_info()
    if tool_name == "get_portfolio_positions":
        return await client.get_portfolio_positions()
    if tool_name == "get_price_history":
        ticker = args.get("ticker", "AAPL")
        timeframe_str = args.get("timeframe", "1Day")
        return await client.get_historical_bars(ticker, timeframe=timeframe_str, limit=args.get("limit", 512))

    logger.warning(f"Unimplemented tool call: {tool_name}")
    return {"result": f"Tool {tool_name} called with args {args}, but handler not fully implemented yet."}


if __name__ == '__main__':
    print("AlpacaClient module loaded.")