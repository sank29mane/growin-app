import os
import asyncio
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from trading212_mcp_server import normalize_ticker

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
        normalized_ticker = normalize_ticker(ticker)
        
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
                from datetime import datetime, timedelta, timezone
                
                now = datetime.now(timezone.utc)
                
                # Default lookback if not specified
                delta_map = {
                    "1Min": timedelta(minutes=limit * 2),
                    "5Min": timedelta(minutes=limit * 10),
                    "15Min": timedelta(minutes=limit * 30),
                    "1Hour": timedelta(hours=limit * 2),
                    "1Day": timedelta(days=limit + 100), # Add buffer
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
                        bar_list.append({
                            "t": int(bar.timestamp.timestamp() * 1000),
                            "o": bar.open,
                            "h": bar.high,
                            "l": bar.low,
                            "c": bar.close,
                            "v": bar.volume,
                            "timestamp": bar.timestamp.isoformat() # Add explicit ISO string for easier parsing
                        })
                    
                    return {"ticker": ticker, "bars": bar_list, "timeframe": timeframe}
                    
            except Exception as e:
                logger.warning(f"AlpacaClient: Error fetching bars from Alpaca: {e}. Falling back to yfinance.")

        # Fallback to yfinance
        try:
            import yfinance as yf
            import pandas as pd
            from utils.currency_utils import CurrencyNormalizer

            # Map timeframe to yfinance period/interval
            period_map = {
                "1Min": ("1d", "1m"),
                "5Min": ("1d", "5m"),
                "15Min": ("1d", "15m"),
                "1Hour": ("1mo", "1h"),
                "1Day": ("1y", "1d"),
            }
            period, interval = period_map.get(timeframe, ("1y", "1d"))

            ticker_obj = yf.Ticker(normalized_ticker)
            data = ticker_obj.history(period=period, interval=interval)

            if data.empty:
                return None

            bar_list = []
            for idx, row in data.iterrows():
                # NORMALIZE PRICE: Convert GBX to GBP if it's a UK stock
                open_val = CurrencyNormalizer.normalize_price(float(row['Open']), normalized_ticker)
                high_val = CurrencyNormalizer.normalize_price(float(row['High']), normalized_ticker)
                low_val = CurrencyNormalizer.normalize_price(float(row['Low']), normalized_ticker)
                close_val = CurrencyNormalizer.normalize_price(float(row['Close']), normalized_ticker)
                
                bar_list.append({
                    "t": int(pd.to_datetime(idx).timestamp() * 1000),
                    "o": open_val,
                    "h": high_val,
                    "l": low_val,
                    "c": close_val,
                    "v": int(row['Volume']),
                })

            return {"ticker": ticker, "bars": bar_list[-limit:], "timeframe": timeframe}

        except Exception as e:
            logger.warning(f"AlpacaClient: Error fetching bars (yfinance): {e}")
            # Return mock data instead of None
            import random
            from datetime import datetime, timedelta

            bar_list = []
            base_price = 150.0 + random.uniform(-50, 50)  # Random base price
            current_time = datetime.now()

            for i in range(min(limit, 30)):  # Generate up to 30 days of mock data
                # Generate realistic price movement
                daily_change = random.uniform(-3.0, 3.0)
                price = base_price + daily_change + (i * 0.1)  # Slight trend

                bar_list.append({
                    "t": int((current_time - timedelta(days=29-i)).timestamp() * 1000),
                    "o": round(price + random.uniform(-1.0, 1.0), 2),
                    "h": round(price + random.uniform(0.5, 2.0), 2),
                    "l": round(price - random.uniform(0.5, 2.0), 2),
                    "c": round(price, 2),
                    "v": random.randint(1000000, 50000000)
                })

            logger.info("AlpacaClient: Using generated mock data")
            return {"ticker": ticker, "bars": bar_list, "timeframe": timeframe}

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
                            "p": trade.price,
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
                    "cash_balance": {"total": float(account.cash), "currency": account.currency},
                    "portfolio_value": float(account.portfolio_value),
                    "unrealized_pnl": float(account.equity) - float(account.last_equity), # Approx
                    "buying_power": float(account.buying_power),
                    "status": str(account.status)
                }
            except Exception as e:
                logger.error(f"AlpacaClient: Error fetching account info: {e}")
        
        # Mock fallback
        return {
            "cash_balance": {"total": 10000.0, "currency": "USD"},
            "portfolio_value": 15000.0,
            "unrealized_plpc": 0.05
        }

    async def get_portfolio_positions(self) -> List[Dict[str, Any]]:
        """Fetch open positions from Alpaca Trading API."""
        if self.trading_client:
            try:
                positions = await asyncio.to_thread(self.trading_client.get_all_positions)
                result = []
                for pos in positions:
                    result.append({
                        "symbol": pos.symbol,
                        "qty": float(pos.qty),
                        "current_price": float(pos.current_price),
                        "market_value": float(pos.market_value),
                        "unrealized_pnl": float(pos.unrealized_pl),
                        "unrealized_plpc": float(pos.unrealized_plpc)
                    })
                return result
            except Exception as e:
                logger.error(f"AlpacaClient: Error fetching positions: {e}")

        # Mock fallback
        return [
            {"symbol": "AAPL", "qty": 10, "current_price": 180.0, "market_value": 1800.0},
            {"symbol": "MSFT", "qty": 5, "current_price": 300.0, "market_value": 1500.0}
        ]


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
            from currency_utils import CurrencyNormalizer
            bar_list = []
            for i in range(len(candles['c'])):
                bar_list.append({
                    "t": candles['t'][i] * 1000,  # Convert to milliseconds
                    "o": CurrencyNormalizer.normalize_price(candles['o'][i], ticker),
                    "h": CurrencyNormalizer.normalize_price(candles['h'][i], ticker),
                    "l": CurrencyNormalizer.normalize_price(candles['l'][i], ticker),
                    "c": CurrencyNormalizer.normalize_price(candles['c'][i], ticker),
                    "v": candles['v'][i] if i < len(candles['v']) else 0,
                    "timestamp": datetime.fromtimestamp(candles['t'][i]).isoformat()
                })

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
            from currency_utils import CurrencyNormalizer
            quote = self.client.quote(normalized_ticker)
            return {
                "symbol": ticker,
                "current_price": CurrencyNormalizer.normalize_price(quote.get('c', 0), ticker),
                "change": CurrencyNormalizer.normalize_price(quote.get('d', 0), ticker),
                "change_percent": quote.get('dp', 0),
                "high": CurrencyNormalizer.normalize_price(quote.get('h', 0), ticker),
                "low": CurrencyNormalizer.normalize_price(quote.get('l', 0), ticker),
                "open": CurrencyNormalizer.normalize_price(quote.get('o', 0), ticker),
                "previous_close": CurrencyNormalizer.normalize_price(quote.get('pc', 0), ticker),
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