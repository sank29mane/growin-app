"""
Chart Routes - Historical and Real-time Market Data
"""

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from app_context import state
import logging
import asyncio
import json
from datetime import datetime, timezone
from utils.ticker_utils import normalize_ticker
from utils.currency_utils import CurrencyNormalizer

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
        'ULVR', 'DGE', 'BT.A', 'PRU', 'STAN', 'LGEN', 'AAL', 'CCL', 'CPG', 'EZJ', 'IAG',
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

    return 'US'


async def get_alpaca_chart_data(ticker: str, timeframe: str, limit: int, cache_key: str):
    """Fetch chart data using Alpaca API."""
    from cache_manager import cache
    from data_engine import get_alpaca_client

    try:
        alpaca = get_alpaca_client()
        if not alpaca:
            raise Exception("Alpaca client not available")

        result = await alpaca.get_historical_bars(ticker, timeframe=timeframe, limit=limit)

        if not result or not isinstance(result, dict) or "bars" not in result:
            raise Exception("No data returned from Alpaca")

        bar_list = result["bars"]
        points = []
        for bar_data in bar_list:
            try:
                timestamp_str = bar_data.get("timestamp") or datetime.fromtimestamp(bar_data["t"] / 1000, timezone.utc).isoformat()
                if not timestamp_str.endswith("Z") and "+" not in timestamp_str:
                    timestamp_str += "Z"

                points.append({
                    "timestamp": timestamp_str,
                    "close": round(float(bar_data.get("c", 0)), 2),
                    "high": round(float(bar_data.get("h", 0)), 2),
                    "low": round(float(bar_data.get("l", 0)), 2),
                    "open": round(float(bar_data.get("o", 0)), 2),
                    "volume": int(bar_data.get("v", 0))
                })
            except Exception:
                continue

        if not points or len(points) < 5:
             raise Exception("Insufficient data from Alpaca")

        points.sort(key=lambda x: x.get("timestamp", ""))
        cache.set(cache_key, points, ttl=600)
        return points
    except Exception as e:
        logger.warning(f"Alpaca chart data fetch failed for {ticker}: {e}")
        raise


async def get_yfinance_chart_data(ticker: str, timeframe: str, limit: int, cache_key: str):
    """Fallback chart data using yfinance."""
    from cache_manager import cache
    import yfinance as yf
    import pandas as pd

    timeframe_normalized = timeframe.lower()
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
        loop = asyncio.get_running_loop()
        def fetch_yf():
            t = yf.Ticker(ticker)
            return t.history(period=period, interval=interval)

        history = await loop.run_in_executor(None, fetch_yf)
        if history.empty:
            return []

        history = history.copy()
        history['timestamp'] = history.index.map(lambda x: x.isoformat())
        history['close'] = history['Close'].round(2)
        history['high'] = history['High'].round(2)
        history['low'] = history['Low'].round(2)
        history['open'] = history['Open'].round(2)
        history['volume'] = history['Volume'].fillna(0).astype(int)

        points = history[['timestamp', 'close', 'high', 'low', 'open', 'volume']].to_dict('records')
        
        if len(points) > limit:
            step = max(1, len(points) // limit)
            points = points[::step]

        cache.set(cache_key, points, ttl=300)
        return points
    except Exception as e:
        logger.error(f"yfinance chart data fetch failed for {ticker}: {e}")
        raise


@router.get("/api/chart/{symbol}")
async def get_chart_data(symbol: str, timeframe: str = "1Day", limit: int = 500):
    """
    Unified Chart Data Endpoint.
    Routes between Cache, Alpaca, yfinance, and AnalyticsDB.
    """
    from cache_manager import cache
    from analytics_db import get_analytics_db
    
    ticker = normalize_ticker(symbol)
    market = detect_market(ticker)
    
    # Ensure .L for UK stocks in yfinance/analytics
    if market == 'UK' and not ticker.endswith('.L'):
        ticker = f"{ticker}.L"

    # 1. Persistent Cache Check (AnalyticsDB)
    analytics = get_analytics_db()
    if timeframe in ["1Year", "Max", "3Month"]:
        try:
            db_data = analytics.get_recent_ohlcv(ticker, limit=limit)
            if db_data is not None and not db_data.empty:
                db_data["timestamp"] = db_data["timestamp"].apply(lambda x: x.isoformat())
                points = db_data[["timestamp", "close", "high", "low", "open", "volume"]].to_dict("records")
                
                # Normalize currency on read
                for p in points:
                    p["close"] = CurrencyNormalizer.normalize_price(p["close"], ticker)
                    p["high"] = CurrencyNormalizer.normalize_price(p["high"], ticker)
                    p["low"] = CurrencyNormalizer.normalize_price(p["low"], ticker)
                    p["open"] = CurrencyNormalizer.normalize_price(p["open"], ticker)
                
                return {
                    "data": points,
                    "metadata": {"market": market, "currency": "GBP" if market == "UK" else "USD", "ticker": ticker, "provider": "AnalyticsDB"}
                }
        except Exception as e:
            logger.warning(f"AnalyticsDB lookup failed: {e}")

    # 2. Memory Cache Check
    cache_key = f"chart_{ticker}_{timeframe.lower()}"
    cached = cache.get(cache_key)
    if cached:
        return {"data": cached, "metadata": {"market": market, "currency": "GBP" if market == "UK" else "USD", "ticker": ticker, "provider": "Cache"}}

    # 3. Fetch from Providers
    data = []
    provider = "yfinance"
    
    if market == 'US':
        try:
            data = await get_alpaca_chart_data(ticker, timeframe, limit, cache_key)
            provider = "Alpaca"
        except Exception:
            pass # Fallback to yfinance

    if not data:
        try:
            data = await get_yfinance_chart_data(ticker, timeframe, limit, cache_key)
            provider = "yfinance"
        except Exception:
            pass

    # 4. Last Resort: Synthetic
    if not data:
        try:
            from utils.mock_data import generate_synthetic_chart_data
            data = generate_synthetic_chart_data(ticker, timeframe, limit)
            provider = "Synthetic"
        except Exception:
            raise HTTPException(status_code=404, detail=f"No data found for {symbol}")

    # 5. Normalize & Cache to DB
    for p in data:
        p["close"] = CurrencyNormalizer.normalize_price(p["close"], ticker)
        p["high"] = CurrencyNormalizer.normalize_price(p["high"], ticker)
        p["low"] = CurrencyNormalizer.normalize_price(p["low"], ticker)
        p["open"] = CurrencyNormalizer.normalize_price(p["open"], ticker)

    try:
        analytics_data = [{"t": p["timestamp"], "o": p["open"], "h": p["high"], "l": p["low"], "c": p["close"], "v": p["volume"]} for p in data]
        analytics.bulk_insert_ohlcv(ticker, analytics_data)
    except Exception as e:
        logger.warning(f"Failed to cache to AnalyticsDB: {e}")

    return {
        "data": data,
        "metadata": {
            "market": market,
            "currency": "GBP" if market == "UK" else "USD",
            "symbol": "£" if market == "UK" else "$",
            "ticker": ticker,
            "provider": provider
        }
    }


@router.websocket("/ws/chart/{symbol}")
async def websocket_chart_data(websocket: WebSocket, symbol: str):
    """WebSocket for real-time updates"""
    await websocket.accept()
    try:
        while True:
            chart_response = await get_chart_data(symbol, timeframe="1Day", limit=50)
            await websocket.send_json({
                "type": "chart_update",
                "symbol": symbol,
                "data": chart_response.get("data", []),
                "metadata": chart_response.get("metadata", {}),
                "timestamp": asyncio.get_event_loop().time()
            })
            await asyncio.sleep(60)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"WebSocket error for {symbol}: {e}")