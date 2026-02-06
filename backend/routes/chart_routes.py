"""
Chart Routes - Historical and Real-time Market Data
"""

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
import logging
import asyncio
from datetime import datetime, timezone
from utils.ticker_utils import normalize_ticker
from utils.currency_utils import CurrencyNormalizer

logger = logging.getLogger(__name__)
router = APIRouter()

# Common UK ticker patterns (major UK stocks without .L)
# Hoisted to module level for performance optimization (PR #38)
UK_TICKERS = {
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

def detect_market(ticker: str) -> str:
    """
    Detect market for a ticker symbol.
    Returns 'UK' for LSE stocks, 'US' for US stocks, 'unknown' otherwise.
    """
    ticker = ticker.upper()

    # UK stocks: end with .L or have _EQ suffix from Trading212
    if ticker.endswith('.L') or '_EQ' in ticker:
        return 'UK'

    if ticker in UK_TICKERS:
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

                # Normalize currency AT SOURCE
                close_val = float(CurrencyNormalizer.normalize_price(bar_data.get("c", bar_data.get("close", 0)), ticker))
                high_val = float(CurrencyNormalizer.normalize_price(bar_data.get("h", bar_data.get("high", 0)), ticker))
                low_val = float(CurrencyNormalizer.normalize_price(bar_data.get("l", bar_data.get("low", 0)), ticker))
                open_val = float(CurrencyNormalizer.normalize_price(bar_data.get("o", bar_data.get("open", 0)), ticker))
                volume_val = int(bar_data.get("v", bar_data.get("volume", 0)))

                points.append({
                    "timestamp": timestamp_str,
                    "close": close_val,
                    "high": high_val,
                    "low": low_val,
                    "open": open_val,
                    "volume": volume_val,
                    "c": close_val,
                    "h": high_val,
                    "l": low_val,
                    "o": open_val,
                    "v": volume_val
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
        
        # Add short keys
        history['c'] = history['close']
        history['h'] = history['high']
        history['l'] = history['low']
        history['o'] = history['open']
        history['v'] = history['volume']

        raw_points = history[['timestamp', 'close', 'high', 'low', 'open', 'volume', 'c', 'h', 'l', 'o', 'v']].to_dict('records')

        # Normalize currency BEFORE caching
        points = []
        for p in raw_points:
            p_norm = p.copy()
            close_val = float(CurrencyNormalizer.normalize_price(p.get("close", p.get("c", 0)), ticker))
            p_norm["close"] = close_val
            p_norm["c"] = close_val
            p_norm["high"] = float(CurrencyNormalizer.normalize_price(p.get("high", p.get("h", 0)), ticker))
            p_norm["h"] = p_norm["high"]
            p_norm["low"] = float(CurrencyNormalizer.normalize_price(p.get("low", p.get("l", 0)), ticker))
            p_norm["l"] = p_norm["low"]
            p_norm["open"] = float(CurrencyNormalizer.normalize_price(p.get("open", p.get("o", 0)), ticker))
            p_norm["o"] = p_norm["open"]
            points.append(p_norm)

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

                # No normalization here: we store normalized data in AnalyticsDB
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

    # 3. Fetch from Providers based on Market
    # STRICT POLICY: Use real data sources only. No synthetic fallback.
    from data_engine import get_finnhub_client
    
    data = []
    provider = "Unknown"
    
    if market == 'UK':
        # UK Stocks: Try Finnhub first, then yfinance
        finnhub = get_finnhub_client()
        if finnhub and finnhub.client:
            try:
                result = await finnhub.get_historical_bars(ticker, timeframe, limit)
                if result and result.get("bars"):
                    data = _convert_bars_to_chart_format(result["bars"], ticker)
                    provider = "Finnhub"
                    cache.set(cache_key, data, ttl=600)
            except Exception as e:
                logger.warning(f"Finnhub fetch failed for {ticker}: {e}")
        
        # Fallback to yfinance for UK stocks
        if not data:
            try:
                data = await get_yfinance_chart_data(ticker, timeframe, limit, cache_key)
                provider = "yfinance"
            except Exception as e:
                logger.warning(f"yfinance UK fallback failed for {ticker}: {e}")
    else:
        # US Stocks: Try Alpaca first, then yfinance
        try:
            data = await get_alpaca_chart_data(ticker, timeframe, limit, cache_key)
            provider = "Alpaca"
        except Exception:
            pass  # Fallback to yfinance
        
        if not data:
            try:
                data = await get_yfinance_chart_data(ticker, timeframe, limit, cache_key)
                provider = "yfinance"
            except Exception as e:
                logger.warning(f"yfinance US fallback failed for {ticker}: {e}")

    # STRICT: No synthetic data. Return 404 if no real data available.
    if not data:
        raise HTTPException(status_code=404, detail=f"No market data available for {symbol}. Check ticker symbol or try again later.")

    # 4. Final Processing & Persistent Backup
    try:
        if provider not in ["AnalyticsDB", "Cache"]:
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


def _convert_bars_to_chart_format(bars: list, ticker: str) -> list:
    """Convert data_engine bar format to chart route format."""
    points = []
    for bar in bars:
        ts = bar.get("timestamp", "")
        if not ts.endswith("Z") and "+" not in ts:
            ts += "Z"
        
        points.append({
            "timestamp": ts,
            "close": float(bar.get("close", 0)),
            "high": float(bar.get("high", 0)),
            "low": float(bar.get("low", 0)),
            "open": float(bar.get("open", 0)),
            "volume": int(bar.get("volume", 0)),
            "c": float(bar.get("close", 0)),
            "h": float(bar.get("high", 0)),
            "l": float(bar.get("low", 0)),
            "o": float(bar.get("open", 0)),
            "v": int(bar.get("volume", 0))
        })
    return points


@router.websocket("/ws/chart/{symbol}")
async def websocket_chart_data(websocket: WebSocket, symbol: str):
    """WebSocket for high-frequency real-time price ticks (10s refresh)"""
    from data_engine import get_alpaca_client, get_finnhub_client

    await websocket.accept()
    ticker = normalize_ticker(symbol)
    market = detect_market(ticker)

    alpaca = get_alpaca_client()
    finnhub = get_finnhub_client()

    try:
        # Initial full data burst
        chart_response = await get_chart_data(symbol, timeframe="1Day", limit=100)
        await websocket.send_json({
            "type": "chart_init",
            "symbol": symbol,
            "data": chart_response.get("data", []),
            "metadata": chart_response.get("metadata", {})
        })

        while True:
            # High-frequency ticks
            quote = None
            if market == "UK":
                quote = await finnhub.get_real_time_quote(ticker)
            else:
                quote = await alpaca.get_real_time_quote(ticker)

            if quote:
                await websocket.send_json({
                    "type": "realtime_quote",
                    "symbol": symbol,
                    "data": {
                        "current_price": float(quote["current_price"]),
                        "change": float(quote["change"]),
                        "change_percent": float(quote["change_percent"]),
                        "timestamp": quote["timestamp"]
                    }
                })

                # Also send a tick that can be appended to charts
                await websocket.send_json({
                    "type": "chart_tick",
                    "symbol": symbol,
                    "tick": {
                        "timestamp": quote["timestamp"],
                        "close": float(quote["current_price"]),
                        "volume": 0 # Trades not usually in simple quote
                    }
                })

            await asyncio.sleep(10) # 10s is a good balance for paper/free tiers
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for {symbol}")
    except Exception as e:
        logger.error(f"WebSocket error for {symbol}: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except: pass