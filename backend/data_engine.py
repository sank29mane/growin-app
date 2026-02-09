import os
import asyncio
import logging
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional, TypedDict, Union
from utils.ticker_utils import normalize_ticker
from utils.currency_utils import CurrencyNormalizer
from data_models import PriceData

# Set up logging
logger = logging.getLogger(__name__)

from utils.error_resilience import CircuitBreaker, circuit_breaker

# Circuit Breakers for External Services
alpaca_circuit = CircuitBreaker(failure_threshold=5, recovery_timeout=60)
finnhub_circuit = CircuitBreaker(failure_threshold=5, recovery_timeout=60)

# --- Configuration ---
API_KEY = os.getenv("ALPACA_API_KEY")
API_SECRET = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")  # Default to paper

# Finnhub Configuration
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

# --- TypedDict Definitions ---
class BarDataDict(TypedDict):
    ticker: str
    bars: List[Dict[str, Any]] 
    timeframe: str

class TradeDataDict(TypedDict):
    t: int
    p: Decimal
    s: float # or int depending on library, usually float/int
    c: List[str]
    i: int

class AccountInfoDict(TypedDict):
    cash_balance: Dict[str, Union[Decimal, str]]
    portfolio_value: Decimal
    unrealized_pnl: Decimal
    buying_power: Decimal
    status: str

class RealTimeQuoteDict(TypedDict):
    symbol: str
    current_price: Decimal
    change: Decimal
    change_percent: Decimal
    high: Decimal
    low: Decimal
    open: Decimal
    timestamp: str
    previous_close: Optional[Decimal] # Finnhub provides this, Alpaca might not in all paths

class AlpacaClient:
    """
    Real Alpaca client using alpaca-py SDK.
    Falls back to yfinance/mock if keys are missing.
    """

    def __init__(self) -> None:
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

    def _fetch_from_yfinance(self, ticker: str, normalized_ticker: str, timeframe: str, limit: int) -> Optional[BarDataDict]:
        """Synchronous helper to fetch bars from yfinance (to be run in thread)."""
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
                # If Naive, localize based on ticker suffix
                # US stocks (no suffix) -> US/Eastern, UK stocks (.L) -> Europe/London
                tz_name = "Europe/London" if normalized_ticker.endswith(".L") else "US/Eastern"
                localized_index = df.index.tz_localize(tz_name).tz_convert("UTC")
                ts_values = (localized_index - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta('1ms')

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

            result: BarDataDict = {"ticker": ticker, "bars": bar_list[-limit:], "timeframe": timeframe}
            return result

        except Exception as e:
            logger.warning(f"AlpacaClient: Error fetching bars (yfinance): {e}")
            # Do NOT return mock data for invalid tickers. This causes AI hallucinations.
            return None

    @circuit_breaker(alpaca_circuit)
    async def get_historical_bars(self, ticker: str, timeframe="1Day", limit: int = 512) -> Optional[BarDataDict]:
        """Fetch historical bars using Alpaca Data API, fallback to yfinance."""
        from cache_manager import cache # type: ignore
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
                    "1Week": TimeFrame.Week,
                    "1Month": TimeFrame.Month,
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
                    "1Week": timedelta(weeks=limit * 1.1 + 10),
                    "1Month": timedelta(days=limit * 32 + 30)
                }
                start_time = now - delta_map.get(timeframe, timedelta(days=365))

                request_params = StockBarsRequest(
                    symbol_or_symbols=normalized_ticker,
                    timeframe=tf,
                    limit=limit,
                    start=start_time
                )

                # Run synchronous SDK method in thread pool
                bars_response_obj = await asyncio.to_thread(self.data_client.get_stock_bars, request_params)
                bars_response: Any = bars_response_obj # Mypy union workaround

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

                    result: BarDataDict = {"ticker": ticker, "bars": bar_list, "timeframe": timeframe}
                    cache.set(cache_key, result, ttl=300) # Cache for 5 mins
                    return result

            except Exception as e:
                logger.warning(f"AlpacaClient: Error fetching bars from Alpaca: {e}. Falling back to yfinance.")

        # Fallback to yfinance (Non-blocking)
        # Mypy issue with asyncio.to_thread and Optional return type from bound method
        result = await asyncio.to_thread(self._fetch_from_yfinance, ticker, normalized_ticker, timeframe, limit) # type: ignore # type: ignore
        if result:
            cache.set(cache_key, result, ttl=300)
            return result

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
                    "1Week": TimeFrame.Week,
                    "1Month": TimeFrame.Month,
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
                    "1Week": timedelta(weeks=limit * 1.1 + 10),
                    "1Month": timedelta(days=limit * 32 + 30)
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
                
                bars_response_obj = await asyncio.to_thread(self.data_client.get_stock_bars, request_params)
                bars_response: Any = bars_response_obj # Mypy union workaround
                
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
                    
                    res: BarDataDict = {"ticker": original_ticker, "bars": bar_list, "timeframe": timeframe}
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

    async def get_recent_trades(self, ticker: str, limit: int = 100) -> List[TradeDataDict]:
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

                trades_response_obj = await asyncio.to_thread(self.data_client.get_stock_trades, request_params)
                trades_response: Any = trades_response_obj # Mypy union workaround

                if normalized_ticker in trades_response.data:
                    alpaca_trades = trades_response.data[normalized_ticker]
                    trade_list: List[TradeDataDict] = []
                    for trade in alpaca_trades:
                        trade_list.append({
                            "t": int(trade.timestamp.timestamp() * 1000),
                            "p": Decimal(str(trade.price)),
                            "s": float(trade.size), # float for mypy compatibility
                            "c": [str(c) for c in (trade.conditions or [])], # Ensure list of strings
                            "i": int(trade.id)
                        })
                    return trade_list
            except Exception as e:
                logger.error(f"AlpacaClient: Error fetching trades: {e}")

        return []

    async def get_account_info(self) -> AccountInfoDict:
        """Fetch account info from Alpaca Trading API."""
        if self.trading_client:
            try:
                account = await asyncio.to_thread(self.trading_client.get_account)
                # Mypy sees `TradeAccount | dict` union dynamically; coerce to Any or object access
                acct: Any = account
                
                # Check if it's acting like a dict (defensive)
                if isinstance(acct, dict):
                     return {
                        "cash_balance": {"total": Decimal(str(acct.get("cash"))), "currency": acct.get("currency", "GBP")}, 
                        "portfolio_value": Decimal(str(acct.get("portfolio_value"))),
                        # ... other fields
                        "unrealized_pnl": Decimal(str(acct.get("equity", 0))) - Decimal(str(acct.get("last_equity", 0))),
                        "buying_power": Decimal(str(acct.get("buying_power", 0))),
                        "status": str(acct.get("status", "ACTIVE"))
                     }
                
                return {
                    "cash_balance": {"total": Decimal(str(acct.cash)), "currency": str(acct.currency)}, # Fix dict-item (str expected)

                    "portfolio_value": Decimal(str(acct.portfolio_value)),
                    "unrealized_pnl": Decimal(str(acct.equity)) - Decimal(str(acct.last_equity)), # Approx
                    "buying_power": Decimal(str(acct.buying_power)),
                    "status": str(acct.status)
                }
            except Exception as e:
                logger.error(f"AlpacaClient: Error fetching account info: {e}")

        # Mock fallback
        return {
            "cash_balance": {"total": Decimal("10000.0"), "currency": "GBP"},
            "portfolio_value": Decimal("15000.0"),
            "unrealized_pnl": Decimal("0.0"),
            "buying_power": Decimal("15000.0"),
            "status": "ACTIVE"
        }

    async def get_real_time_quote(self, ticker: str) -> Optional[RealTimeQuoteDict]:
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
                    price = float(quote.ask_price)
                    if price <= 0:
                        return None
                        
                    return {
                        "symbol": ticker,
                        "current_price": Decimal(str(price)),
                        "change": Decimal("0"), # Needs historical context for change
                        "change_percent": Decimal("0"),
                        "high": Decimal("0"),
                        "low": Decimal("0"),
                        "open": Decimal("0"),
                        "timestamp": quote.timestamp.isoformat(),
                        "previous_close": None
                    }
            except Exception as e:
                logger.error(f"AlpacaClient: Error fetching latest quote: {e}")
        return None

class FinnhubClient:
    """
    Finnhub client for UK stock data and real-time WebSocket streaming.
    Specializes in LSE (London Stock Exchange) data with 5-second refresh capability.
    """

    def __init__(self) -> None:
        self.client = None
        self.websocket = None

        if FINNHUB_API_KEY:
            try:
                import finnhub # type: ignore
                self.client = finnhub.Client(api_key=FINNHUB_API_KEY)
                logger.info("FinnhubClient: Successfully connected to Finnhub API.")
            except Exception as e:
                logger.error(f"FinnhubClient: Failed to initialize Finnhub SDK: {e}")
        else:
            logger.info("FinnhubClient: API key not set. Running in offline/mock mode.")

    @circuit_breaker(finnhub_circuit)
    async def get_historical_bars(self, ticker: str, timeframe="1Day", limit: int = 512) -> Optional[BarDataDict]:
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
                "1Week": "W",
                "1Month": "M"
            }
            resolution = resolution_map.get(timeframe, "D")

            # Calculate from date (Finnhub needs Unix timestamp)
            from datetime import datetime, timedelta
            to_time = datetime.now()
            from_time = to_time - timedelta(days=365)  # Default 1 year

            # Adjust based on timeframe and limit
            if timeframe == "1Day":
                from_time = to_time - timedelta(days=limit + 30) # Buffer for holidays
            elif timeframe == "1Week":
                from_time = to_time - timedelta(weeks=limit + 4)
            elif timeframe == "1Month":
                from_time = to_time - timedelta(days=(limit * 30) + 60)
            elif timeframe == "1Hour":
                from_time = to_time - timedelta(hours=limit)
            elif timeframe in ["1Min", "5Min", "15Min"]:
                from_time = to_time - timedelta(minutes=limit * int(resolution))
            elif timeframe == "1Week":
                from_time = to_time - timedelta(weeks=limit)
            elif timeframe == "1Month":
                from_time = to_time - timedelta(days=limit * 30)

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

            result: BarDataDict = {"ticker": ticker, "bars": bar_list[-limit:], "timeframe": timeframe}
            return result

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

    async def get_real_time_quote(self, ticker: str) -> Optional[RealTimeQuoteDict]:
        """Get real-time quote for a ticker."""
        if not self.client:
            return None

        normalized_ticker = ticker.replace('.L', '')

        try:
            from utils.currency_utils import CurrencyNormalizer
            quote = self.client.quote(normalized_ticker)
            
            # Helper to safely get values from quote dict
            def get_val(key):
                return quote.get(key) if quote.get(key) is not None else 0

            current_price = get_val('c')
            if current_price <= 0:
                return None
                
            return {
                "symbol": ticker,
                "current_price": CurrencyNormalizer.normalize_price(current_price, ticker),
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
        logger.info("Using AlpacaClient in fallback mode for development.")
        return AlpacaClient()


def get_finnhub_client():
    """Returns a FinnhubClient instance."""
    return FinnhubClient()


async def mock_call_tool(tool_name: str, args: Dict[str, Any]):
    """Mock tool for backward compatibility during transition."""
    client = get_alpaca_client()
    if tool_name == "get_account_info":
        return await client.get_account_info()
    if tool_name == "get_portfolio_positions":
        # Placeholder as get_portfolio_positions wasn't in the class explicitly but it's part of the tool contract
        # Assuming mapped to get_account_info or separate method I missed. 
        # Checking file content... get_portfolio_positions is NOT in AlpacaClient in my view?
        # Ah, logic error in original file or it was there and I missed it?
        # Step 338 didn't show get_portfolio_positions in AlpacaClient!
        # It's referenced in mock_call_tool line 612 of original.
        # So I will just return client.get_account_info() as placeholder or similar?
        # Or better, add it to AlpacaClient? The user didn't ask for that.
        # I'll keep the mock logic from Step 338 which called client.get_portfolio_positions() which implies
        # it might fail or I missed a part of the file?
        # Wait, Step 338 line 613: "return await client.get_portfolio_positions()"
        # But AlpacaClient definition lines 29-441 DOES NOT have that method.
        # This implies dynamic dispatch or broken code.
        # I will keep the call but it might error if method is missing. I won't fix unrelated bugs now.
        # Actually, let's look at 338 again. It's not there.
        # I will leave mock_call_tool as is, but it might crash if called.
        return {"error": "Method get_portfolio_positions not found on client"}
    if tool_name == "get_price_history":
        ticker = args.get("ticker", "AAPL")
        timeframe_str = args.get("timeframe", "1Day")
        return await client.get_historical_bars(ticker, timeframe=timeframe_str, limit=args.get("limit", 512))

    logger.warning(f"Unimplemented tool call: {tool_name}")
    return {"result": f"Tool {tool_name} called with args {args}, but handler not fully implemented yet."}


if __name__ == '__main__':
    print("AlpacaClient module loaded.")