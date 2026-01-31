"""
Chart Routes - Historical and Real-time Market Data
"""

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, BackgroundTasks
from app_context import state
import logging
import asyncio
import json
from trading212_mcp_server import normalize_ticker

logger = logging.getLogger(__name__)
router = APIRouter()

def detect_market(ticker: str) -> str:
    """
    Detect market for a ticker symbol.
    Returns 'UK' for LSE stocks, 'US' for US stocks, 'unknown' otherwise.
    """
    ticker = ticker.upper()

    # UK stocks: end with .L or have _EQ suffix from Trading212
    if ticker.endswith('.L') or '_EQ' in ticker:
        return 'UK'

    # Common UK ticker patterns (major UK stocks without .L)
    uk_tickers = {
        'LLOY', 'HSBA', 'BARC', 'BP', 'VOD', 'GSK', 'AZN', 'RIO', 'BHP',
        'ULVR', 'DGE', 'BT.A', 'NG.L', 'PRU', 'AV.L', 'RR.L', 'EXPN',
        'SSE', 'ITV', 'STAN', 'LGEN', 'AAL', 'CCL', 'CPG', 'EZJ', 'IAG',
        'BLND', 'INF', 'ABF', 'ADM', 'AHT', 'ANTO', 'AVST', 'BAES', 'BDEV',
        'BNZL', 'BRBY', 'CCH', 'CTEC', 'DCC', 'EDV', 'ENT', 'EXPN', 'FERG',
        'FLTR', 'FRES', 'GFS', 'GLEN', 'HIK', 'HWDN', 'ICG', 'IHG', 'III',
        'IMB', 'INCH', 'ITRK', 'JD.', 'KGF', 'LAND', 'LMP', 'LSEG', 'MKS',
        'MNDI', 'MRO', 'NG.', 'NXT', 'OCDO', 'PHNX', 'PSH', 'PSN', 'RDSB',
        'REL', 'RKT', 'RMG', 'RMV', 'RR.', 'RTO', 'SBRY', 'SDR', 'SGE',
        'SGRO', 'SHEL', 'SKG', 'SMDS', 'SMIN', 'SN.', 'SPX', 'TSCO', 'TW.',
        'ULVR', 'UU.', 'VOD', 'WPP'
    }

    if ticker in uk_tickers:
        return 'UK'

    # Default to US for most other tickers
    return 'US'


async def get_finnhub_chart_data(ticker: str, timeframe: str, limit: int, cache_key: str):
    """Fetch chart data using Finnhub API for UK stocks."""
    from cache_manager import cache
    from data_engine import get_finnhub_client

    try:
        finnhub = get_finnhub_client()
        if not finnhub:
            raise Exception("Finnhub client not available")

        # Get historical bars with proper error checking
        result = await finnhub.get_historical_bars(ticker, timeframe=timeframe, limit=limit)

        if not result:
            raise Exception("Finnhub returned no data")

        # Handle FinnhubClient response format: {"ticker": ticker, "bars": bar_list, "timeframe": timeframe}
        if isinstance(result, dict) and "bars" in result:
            bar_list = result["bars"]
        else:
            raise Exception(f"Unexpected Finnhub response format: {type(result)}")

        # Format results with proper type checking
        points = []
        for bar_data in bar_list:
            try:
                # Handle timestamp conversion for different formats
                timestamp_str = ""
                # Use the pre-calculated ISO string from data_engine
                if "timestamp" in bar_data and isinstance(bar_data["timestamp"], str):
                    timestamp_str = bar_data["timestamp"]
                elif "t" in bar_data:
                    # Unix timestamp in milliseconds
                    import datetime
                    ts_ms = bar_data["t"]
                    if isinstance(ts_ms, (int, float)):
                        # Use proper UTC
                        ts = datetime.datetime.fromtimestamp(ts_ms / 1000, datetime.timezone.utc)
                        timestamp_str = ts.isoformat()
                    else:
                        timestamp_str = str(ts_ms)

                # Ensure 'Z' suffix for UTC if missing and no offset
                if timestamp_str and not timestamp_str.endswith("Z") and "+" not in timestamp_str and "-" not in timestamp_str[-6:]:
                      timestamp_str += "Z"

                points.append({
                    "timestamp": timestamp_str,
                    "close": round(float(bar_data.get("close", bar_data.get("c", 0))), 2),
                    "high": round(float(bar_data.get("high", bar_data.get("h", 0))), 2),
                    "low": round(float(bar_data.get("low", bar_data.get("l", 0))), 2),
                    "open": round(float(bar_data.get("open", bar_data.get("o", 0))), 2),
                    "volume": int(bar_data.get("volume", bar_data.get("v", 0)))
                })
            except (AttributeError, ValueError, TypeError, KeyError) as e:
                logger.warning(f"Error processing bar data {bar_data}: {e}")
                continue

        if not points or len(points) < 5:
              logger.warning("Finnhub returned insufficient data points (<5). Triggering fallback.")
              raise Exception("Insufficient data from Finnhub")

        if not points:
            raise Exception("No valid bar data could be processed")

        # Sort by timestamp (oldest first)
        points.sort(key=lambda x: x.get("timestamp", ""))

        # Cache for 30 seconds (Finnhub data is very fresh, but cache briefly for performance)
        cache.set(cache_key, points, ttl=30)

        return points

    except Exception as e:
        error_msg = str(e)
        
        # Check if it's a 403 error (access denied) - expected for some symbols
        if "403" in error_msg or "access" in error_msg.lower():
            # Silently fail for 403s - fallback will handle it
            raise Exception("Finnhub access denied")
        
        # Log other unexpected errors
        logger.error(f"Finnhub chart data fetch failed: {e}")
        raise


async def get_alpaca_chart_data(ticker: str, timeframe: str, limit: int, cache_key: str):
    """Fetch chart data using Alpaca API for superior quality."""
    from cache_manager import cache
    from data_engine import get_alpaca_client

    try:
        alpaca = get_alpaca_client()
        if not alpaca:
            raise Exception("Alpaca client not available")

        # Get historical bars with proper error checking
        result = await alpaca.get_historical_bars(ticker, timeframe=timeframe, limit=limit)

        # Check if result is a string (mock mode error)
        if isinstance(result, str):
            raise Exception(f"Alpaca mock mode error: {result}")

        if not result:
            raise Exception("No data returned from Alpaca")

        # Handle AlpacaClient response format: {"ticker": ticker, "bars": bar_list, "timeframe": timeframe}
        if isinstance(result, dict) and "bars" in result:
            bar_list = result["bars"]
        elif hasattr(result, '__iter__') and not isinstance(result, (str, dict)):
            # Direct iterable of bars
            bar_list = result
        else:
            raise Exception(f"Unexpected Alpaca response format: {type(result)}")

        # Format results with proper type checking
        points = []
        for bar_data in bar_list:
            try:
                # Check if it's a bar object with attributes (Alpaca SDK format)
                if hasattr(bar_data, 'timestamp') and hasattr(bar_data, 'close'):
                    # Alpaca bar object format
                    timestamp = bar_data.timestamp
                    points.append({
                        "timestamp": timestamp.isoformat() if hasattr(timestamp, 'isoformat') else str(timestamp),
                        "close": round(float(bar_data.close), 2),
                        "high": round(float(bar_data.high), 2),
                        "low": round(float(bar_data.low), 2),
                        "open": round(float(bar_data.open), 2),
                        "volume": int(getattr(bar_data, 'volume', 0))
                    })
                if isinstance(bar_data, dict):
                    # Handle timestamp conversion for different formats
                    timestamp_str = ""
                    # Prioritize the pre-calculated ISO string from data_engine
                    if "timestamp" in bar_data and isinstance(bar_data["timestamp"], str):
                        timestamp_str = bar_data["timestamp"]
                    elif "timestamp" in bar_data:
                         # Alpaca object timestamp
                        ts = bar_data["timestamp"]
                        if hasattr(ts, 'isoformat'):
                            timestamp_str = ts.isoformat()
                        else:
                            timestamp_str = str(ts)
                    elif "t" in bar_data:
                        # Unix timestamp in milliseconds
                        import datetime
                        ts_ms = bar_data["t"]
                        if isinstance(ts_ms, (int, float)):
                            # Use proper UTC
                            ts = datetime.datetime.fromtimestamp(ts_ms / 1000, datetime.timezone.utc)
                            timestamp_str = ts.isoformat()
                        else:
                            timestamp_str = str(ts_ms)
                    
                    # Ensure 'Z' suffix for UTC if missing and no offset
                    if timestamp_str and not timestamp_str.endswith("Z") and "+" not in timestamp_str and "-" not in timestamp_str[-6:]:
                         timestamp_str += "Z"

                    points.append({
                        "timestamp": timestamp_str,
                        "close": round(float(bar_data.get("close", bar_data.get("c", 0))), 2),
                        "high": round(float(bar_data.get("high", bar_data.get("h", 0))), 2),
                        "low": round(float(bar_data.get("low", bar_data.get("l", 0))), 2),
                        "open": round(float(bar_data.get("open", bar_data.get("o", 0))), 2),
                        "volume": int(bar_data.get("volume", bar_data.get("v", 0)))
                    })
                else:
                    logger.warning(f"Skipping unknown bar data type: {type(bar_data)}")
                    continue

            except (AttributeError, ValueError, TypeError, KeyError) as e:
                logger.warning(f"Error processing bar data {bar_data}: {e}")
                continue

        if not points or len(points) < 5:
             logger.warning("Alpaca returned insufficient data points (<5). Triggering fallback.")
             raise Exception("Insufficient data from Alpaca")

        if not points:
            raise Exception("No valid bar data could be processed")

        # Sort by timestamp (oldest first)
        points.sort(key=lambda x: x.get("timestamp", ""))

        # Cache for 10 minutes (Alpaca data is fresher)
        cache.set(cache_key, points, ttl=600)

        return points

    except Exception as e:
        logger.error(f"Alpaca chart data fetch failed: {e}")
        raise


async def get_yfinance_chart_data(ticker: str, timeframe: str, limit: int, cache_key: str):
    """Fallback chart data using yfinance."""
    from cache_manager import cache
    import yfinance as yf
    import pandas as pd
    import asyncio

    # Normalize timeframe to lowercase for consistent mapping
    # Swift app sends "1Day", "1Week", etc.
    timeframe_normalized = timeframe.lower()
    
    # Map timeframe to yfinance period and interval
    timeframe_map = {
        "1day": ("1d", "5m"),
        "1week": ("5d", "30m"),
        "1month": ("1mo", "1h"),
        "3month": ("3mo", "1d"),
        "1year": ("1y", "1d"),
        "max": ("max", "1wk")
    }

    period, interval = timeframe_map.get(timeframe_normalized, ("1mo", "1h"))

    try:
        # Fetch data in thread pool to avoid blocking FastAPI
        loop = asyncio.get_running_loop()
        def f():
            t = yf.Ticker(ticker)
            return t.history(period=period, interval=interval)

        history = await loop.run_in_executor(None, f)

        if history.empty:
            return []

        # Format results
        points = []
        for timestamp, row in history.iterrows():
            points.append({
                "timestamp": timestamp.isoformat(),
                "close": round(float(row["Close"]), 2),
                "high": round(float(row["High"]), 2),
                "low": round(float(row["Low"]), 2),
                "open": round(float(row["Open"]), 2),
                "volume": int(row["Volume"])
            })

        # Apply limit reduction if too many points
        if len(points) > limit:
            step = len(points) // limit
            points = points[::step]

        # Cache for 5 minutes
        cache.set(cache_key, points, ttl=300)

        return points

    except Exception as e:
        logger.error(f"yfinance chart data fetch failed: {e}")
        raise


@router.get("/api/chart/{symbol}")
async def get_chart_data(symbol: str, timeframe: str = "1Day", limit: int = 500):
    """
    Get historical chart data using market-based routing with AnalyticsDB caching.
    
    Flow:
    1. Check AnalyticsDB (fastest)
    2. Check Memory Cache
    3. Fetch from yfinance (fallback)
    4. Store in AnalyticsDB
    """
    from cache_manager import cache
    from trading212_mcp_server import normalize_ticker
    from utils.currency_utils import CurrencyNormalizer
    from analytics_db import get_analytics_db
    
    # Initialize services
    analytics = get_analytics_db()
    ticker = normalize_ticker(symbol)
    market = detect_market(ticker)

    # For UK markets, ensure .L suffix for yfinance compatibility
    if market == 'UK' and not ticker.endswith('.L'):
        ticker = f"{ticker}.L"

    # 1. Try AnalyticsDB first (Persistent OLAP Cache)
    # Only suitable for historical queries, not real-time intraday if not updated
    if timeframe in ["1Year", "Max", "3Month"]:
        db_data = analytics.get_recent_ohlcv(ticker, limit=limit)
        if db_data is not None and not db_data.empty:
            logger.info(f"✅ Served {len(db_data)} points from AnalyticsDB for {ticker}")
            # Convert to dict format
            points = []
            for _, row in db_data.iterrows():
                points.append({
                    "timestamp": row["timestamp"].isoformat(),
                    "close": row["close"],
                    "high": row["high"],
                    "low": row["low"],
                    "open": row["open"],
                    "volume": int(row["volume"])
                })
            
            # AnalyticsDB stores raw values, so we still need to check currency normalization
            # But usually we store normalized values? Let's normalize on read to be safe.
            normalized_points = []
            for point in points:
                # Normalize currency (GBX -> GBP)
                p = point.copy()
                p["close"] = CurrencyNormalizer.normalize_price(point["close"], ticker)
                p["high"] = CurrencyNormalizer.normalize_price(point["high"], ticker)
                p["low"] = CurrencyNormalizer.normalize_price(point["low"], ticker)
                p["open"] = CurrencyNormalizer.normalize_price(point["open"], ticker)
                normalized_points.append(p)
                
            currency = "GBP" if market == "UK" else "USD"
            return {
                "data": normalized_points,
                "metadata": {
                    "market": market,
                    "currency": currency,
                    "symbol": "£" if market == "UK" else "$",
                    "ticker": ticker,
                    "provider": "AnalyticsDB"
                }
            }

    cache_key = f"chart_{ticker}_{timeframe.lower()}_{market.lower()}"

    # 2. Check Memory Cache
    cached_data = cache.get(cache_key)
    if cached_data:
        return cached_data

    actual_provider = "yfinance"
    error_info = None
    data = []

    # 3. Fetch from Primary Provider (Alpaca for US, Finnhub for UK would be ideal but Finnhub is rate limited)
    # We strictly use Alpaca for US stocks as it's the official high-quality provider.
    if market == 'US':
        try:
            logger.info(f"Fetching chart for {ticker} using Alpaca")
            data = await get_alpaca_chart_data(ticker, timeframe, limit, cache_key)
            if data:
                actual_provider = "Alpaca"
        except Exception as e:
            logger.warning(f"Alpaca failed for {ticker}, falling back to yfinance: {e}")
            # Fallthrough to yfinance

    # 4. Fallback to yfinance (if Alpaca failed or not US)
    if not data:
        try:
            logger.info(f"Fetching chart for {ticker} using yfinance (market: {market})")
            data = await get_yfinance_chart_data(ticker, timeframe, limit, cache_key)
        except Exception as e:
             logger.error(f"yfinance failed for {ticker}: {e}")
             if not error_info: # preserve previous error if any? No, yfinance is the last line of defense.
                 error_info = {
                    "code": "PROVIDER_FAILED",
                    "message": "All chart data providers failed",
                    "provider_errors": [f"yfinance: {str(e)}"],
                    "fallback_used": True
                 }

    # 5. Last Resort: Synthetic Data (for offline/dev mode)
    if not data:
        logger.warning(f"All providers failed for {ticker}. Generating synthetic data.")
        try:
            from utils.mock_data import generate_synthetic_chart_data
            data = generate_synthetic_chart_data(ticker, timeframe, limit)
            actual_provider = "Synthetic (Dev Mode)"
            error_info = None # Clear error info since we have data now
        except Exception as e:
            logger.error(f"Failed to generate synthetic data: {e}")

    if data:
        # 5. Store in AnalyticsDB (Async/Fire-and-forget)
        try:
            # Prepare data for DuckDB (rename keys to match schema)
            analytics_data = []
            for p in data:
                analytics_data.append({
                    "t": p["timestamp"], # Will be parsed by bulk_insert
                    "o": p["open"],
                    "h": p["high"],
                    "l": p["low"],
                    "c": p["close"],
                    "v": p["volume"]
                })
            # Don't await, just run
            analytics.bulk_insert_ohlcv(ticker, analytics_data)
        except Exception as e:
            logger.warning(f"Failed to cache to AnalyticsDB: {e}")

    if not data and not error_info:
        logger.warning(f"Insufficient data for {ticker}")
        error_info = {
            "code": "INSUFFICIENT_DATA",
            "message": f"Not enough historical data for {ticker}",
            "provider_errors": ["Insufficient data found"],
            "fallback_used": True
        }

    # Currency Normalization using centralized utility
    if data:
        for point in data:
            point["close"] = CurrencyNormalizer.normalize_price(point["close"], ticker)
            point["high"] = CurrencyNormalizer.normalize_price(point["high"], ticker)
            point["low"] = CurrencyNormalizer.normalize_price(point["low"], ticker)
            point["open"] = CurrencyNormalizer.normalize_price(point["open"], ticker)

    # Return data with market and currency metadata
    currency = "GBP" if market == "UK" else "USD"
    response = {
        "data": data,
        "metadata": {
            "market": market,
            "currency": currency,
            "symbol": "£" if market == "UK" else "$",
            "ticker": ticker,
            "provider": actual_provider
        }
    }
    
    if error_info:
        response["error"] = error_info
    
    return response


@router.websocket("/ws/chart/{symbol}")
async def websocket_chart_data(websocket: WebSocket, symbol: str):
    """
    WebSocket endpoint for real-time chart data updates.
    Uses yfinance for all stocks (more reliable, no rate limits).
    """
    await websocket.accept()

    try:
        # Detect market and set up appropriate client
        market = detect_market(symbol)
        ticker = normalize_ticker(symbol)

        # Refresh every 60s (yfinance is delayed data anyway)
        refresh_interval = 60

        last_data = None

        while True:
            try:
                # Get updated chart data using yfinance
                chart_response = await get_chart_data(symbol, timeframe="1Day", limit=50)
                
                # Normalize response format to handle both dict and list
                from error_resilience import normalize_response_format
                normalized = normalize_response_format(chart_response)
                chart_data = normalized.get("data", [])
                metadata = normalized.get("metadata", {})

                # Only send if data has changed
                if chart_data != last_data and chart_data:
                    await websocket.send_json({
                        "type": "chart_update",
                        "symbol": symbol,
                        "data": chart_data,
                        "metadata": metadata,
                        "timestamp": asyncio.get_event_loop().time()
                    })
                    last_data = chart_data

            except Exception as e:
                logger.error(f"Error in WebSocket chart update for {symbol}: {e}")
                await websocket.send_json({
                    "type": "error",
                    "message": "Chart data temporarily unavailable",
                    "symbol": symbol,
                    "fallback": True
                })

            # Wait for next update
            await asyncio.sleep(refresh_interval)

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for {symbol}")
    except Exception as e:
        logger.error(f"WebSocket error for {symbol}: {e}")
