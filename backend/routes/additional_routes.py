"""
Additional Routes - Endpoints needed by Mac app that weren't migrated
These are stub implementations that need to be properly implemented
"""

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from app_context import state
import logging
import asyncio
import json
from trading212_mcp_server import normalize_ticker

logger = logging.getLogger(__name__)
router = APIRouter()

# Trade Stats Endpoint
@router.get("/api/trade/stats")
async def get_trade_stats():
    """Get current trade statistics (stub)"""
    return {
        "trades_today": 0,
        "daily_limit": 100,
        "trades_this_hour": 0,
        "hourly_limit": 20,
        "daily_utilization": 0
    }

# Chart Data Endpoint
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

        # CRITICAL FIX: Check if result is None (Finnhub not available)
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
            # Optimized: Vectorized conversion (approx 10x faster)
            db_data["timestamp"] = db_data["timestamp"].apply(lambda x: x.isoformat())
            db_data["volume"] = db_data["volume"].fillna(0).astype(int)
            points = db_data[["timestamp", "close", "high", "low", "open", "volume"]].to_dict("records")
            
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

    try:
        logger.info(f"Fetching chart for {ticker} using yfinance (market: {market})")
        data = await get_yfinance_chart_data(ticker, timeframe, limit, cache_key)
        
        if data:
            # 3. Store in AnalyticsDB for future use (Async/Fire-and-forget)
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

        if not data or len(data) < 5:
            logger.warning(f"Insufficient data from yfinance for {ticker}")
            data = []
            error_info = {
                "code": "INSUFFICIENT_DATA",
                "message": f"Not enough historical data for {ticker}",
                "provider_errors": ["yfinance: Insufficient data"],
                "fallback_used": False
            }
    except Exception as e:
        logger.error(f"yfinance failed for {ticker}: {e}")
        data = []
        error_info = {
            "code": "PROVIDER_FAILED",
            "message": "Chart data provider failed",
            "provider_errors": [f"yfinance: {str(e)}"],
            "fallback_used": False
        }

    # Currency Normalization using centralized utility
    if data:
        # Use CurrencyNormalizer to handle GBX -> GBP
        # We need to peek at the first point to guess currency if not explicit, 
        # but CurrencyNormalizer handles it by ticker suffix usually.
        # But wait, yfinance data for .L is in GBX (pence).
        # CurrencyNormalizer.normalize_price converts GBX -> GBP
        
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

        # FIXED: Use yfinance for all markets to avoid Finnhub 429 errors
        # Refresh every 60s (yfinance is delayed data anyway)
        refresh_interval = 60

        last_data = None

        while True:
            try:
                # Get updated chart data using yfinance
                chart_response = await get_chart_data(symbol, timeframe="1Day", limit=50)
                
                # CRITICAL FIX: Normalize response format to handle both dict and list
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

        # CRITICAL FIX: Check if result is a string (mock mode error)
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
        # Optimized: Vectorized conversion
        history = history.copy() # Work on copy to avoid setting on view
        history['timestamp'] = history.index
        history['timestamp'] = history['timestamp'].apply(lambda x: x.isoformat())
        history['close'] = history['Close'].round(2)
        history['high'] = history['High'].round(2)
        history['low'] = history['Low'].round(2)
        history['open'] = history['Open'].round(2)
        history['volume'] = history['Volume'].fillna(0).astype(int)

        points = history[['timestamp', 'close', 'high', 'low', 'open', 'volume']].to_dict('records')

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

# Quant Indicators Endpoint
@router.get("/api/quant/{symbol}/indicators")
async def get_quant_indicators(symbol: str, timeframe: str = "1Day"):
    """Get technical indicators for symbol (stub)"""
    logger.warning(f"Quant indicators called but not implemented: {symbol}")
    return {"error": "Quant indicators endpoint not yet implemented", "symbol": symbol}

# Forecast Endpoint
@router.get("/api/forecast/{symbol}")
async def get_forecast(symbol: str, steps: int = 96):
    """Get price forecast for symbol (stub)"""
    logger.warning(f"Forecast endpoint called but not implemented: {symbol}")
    return {"error": "Forecast endpoint not yet implemented", "symbol": symbol}

# MCP Servers Management (different from /mcp/status)
@router.get("/mcp/servers")
async def get_mcp_servers():
    """Get MCP servers list"""
    try:
        servers = state.chat_manager.get_mcp_servers(sanitize=True)
        return {"servers": servers}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/mcp/servers/add")
async def add_mcp_server(server_data: dict):
    """Add new MCP server"""
    try:
        state.chat_manager.add_mcp_server(
            name=server_data.get("name"),
            server_type=server_data.get("type"),
            command=server_data.get("command"),
            args=server_data.get("args", []),
            env=server_data.get("env", {}),
            url=server_data.get("url")
        )
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/mcp/servers/{server_name}")
async def delete_mcp_server(server_name: str):
    """Delete MCP server"""
    try:
        # TODO: Implement delete_mcp_server in ChatManager
        logger.warning(f"Delete MCP server not implemented: {server_name}")
        return {"status": "not_implemented"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# MLX Models Management
@router.get("/api/models/mlx")
async def get_mlx_models():
    """Get list of MLX models"""
    logger.warning("MLX models endpoint called but not implemented")
    return {"models": [], "note": "MLX model management not yet implemented"}

@router.post("/api/models/mlx/download")
async def download_mlx_model(model_data: dict):
    """Download MLX model from HuggingFace"""
    logger.warning(f"MLX download called but not implemented: {model_data}")
    return {"status": "not_implemented", "message": "MLX download endpoint not yet implemented"}

# HuggingFace Model Search
@router.get("/models/hf/search")
async def search_hf_models(query: str, sort: str = "downloads", direction: str = "desc", limit: int = 20):
    """Search HuggingFace models"""
    logger.warning(f"HF search called but not implemented: {query}")
    return {"models": [], "note": "HuggingFace search not yet implemented"}

# Models Available (legacy endpoint, redirect to /api/models/available)
@router.get("/models/available")
async def get_models_available_legacy():
    """Legacy endpoint - redirects to /api/models/available"""
    from model_config import DECISION_MODELS, COORDINATOR_MODELS
    return {
        "decision_models": [
            {"name": name, **info}
            for name, info in DECISION_MODELS.items()
        ],
        "coordinator_models": [
            {"name": name, **info}
            for name, info in COORDINATOR_MODELS.items()
        ]
    }

# Debug Endpoints
@router.post("/debug/clear-caches")
async def clear_caches():
    """Clear all caches"""
    from cache_manager import cache
    cache.clear()
    logger.info("Cache clear requested")
    return {"status": "success", "message": "Caches cleared"}

@router.get("/debug/logs")
async def get_logs():
    """Get recent server logs"""
    from app_logging import get_recent_logs
    return {"logs": get_recent_logs()}

@router.get("/api/system/status")
async def get_system_status():
    """
    Get aggregated health and architectural status of the system.
    """
    import time
    import resource
    import threading
    
    uptime = time.time() - state.start_time
    
    # Get memory usage in MB
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # On macOS ru_maxrss is in bytes, on Linux it is in KB
    import platform
    if platform.system() == 'Darwin':
        usage_mb = usage / (1024 * 1024)
    else:
        usage_mb = usage / 1024

    # Get MCP status
    mcp_connected = state.mcp_client.session is not None
    mcp_servers = state.chat_manager.get_mcp_servers()
    
    # Get Agent status (minimal for summary)
    agent_status = "online"
    
    return {
        "uptime": round(uptime, 0),
        "uptime_str": time.strftime("%H:%M:%S", time.gmtime(uptime)),
        "memory_mb": round(usage_mb, 2),
        "active_threads": threading.active_count(),
        "mcp": {
            "connected": mcp_connected,
            "servers_count": len(mcp_servers)
        },
        "agents": {
            "status": agent_status
        },
        "status": "healthy"
    }
