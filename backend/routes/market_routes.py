"""
Market Routes - Portfolio and Market Data
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from typing import Optional
from app_context import state
from cache_manager import cache
from trading212_mcp_server import normalize_ticker
from market_context import GoalData
import logging
import asyncio
import numpy as np
from utils import sanitize_nan

logger = logging.getLogger(__name__)
router = APIRouter()

# Global lock for portfolio fetching to prevent race conditions
portfolio_lock = asyncio.Lock()

@router.post("/api/goal/plan")
async def create_goal_plan(context: dict):
    """
    Generate a comprehensive goal-based investment plan.
    
    Args:
        context: Dictionary containing:
            - initial_capital (float)
            - target_returns_percent (float)
            - duration_years (float)
            - risk_profile (str: LOW, MEDIUM, HIGH, AGGRESSIVE_PLUS)
            
    Returns:
        Expert investment plan with asset allocation, strategy, and Trading 212 Pie implementation details.
    """
    from agents.goal_planner_agent import GoalPlannerAgent, AgentConfig
    
    try:
        # Create ad-hoc specialist
        agent = GoalPlannerAgent()
        
        # Execute analysis
        response = await agent.analyze(context)
        
        if not response.success:
             raise HTTPException(status_code=500, detail=response.error)
             
        return response.data
        
    except Exception as e:
        logger.error(f"Goal planning failed: {e}")
        # Sentinel: Sanitized error message
        raise HTTPException(status_code=500, detail="Internal Server Error")

@router.post("/api/goal/execute")
async def execute_goal_plan(plan: dict):
    """
    Execute a goal plan by creating or updating a Trading 212 Pie.
    
    Args:
        plan: The 'implementation' dictionary from the goal plan response.
              Must contain: 'type': 'TRADING212_PIE', 'name', 'action', and weights logic.
              Actually, the UI should probably pass the full 'goal_data' or specialized params.
              Let's accept the raw 'implementation' block + 'weights'.
    
    Returns:
        Status of the Pie execution.
    """
    try:
        from app_context import state
        
        # Validation
        if not state.mcp_client.session:
             raise HTTPException(status_code=503, detail="Trading 212 connection not active")
             
        impl = plan.get("implementation", {})
        if impl.get("type") != "TRADING212_PIE":
             raise HTTPException(status_code=400, detail="Invalid execution type. Only TRADING212_PIE supported.")
        
        # Extract weights from plan (suggested_instruments)
        instruments = plan.get("suggested_instruments", [])
        if not instruments:
             raise HTTPException(status_code=400, detail="No instruments found in plan")
             
        pie_payload = {
            "name": impl.get("name", "Goal Portfolio"),
            "instruments": {i["ticker"]: i["weight"] for i in instruments}
        }
        
        logger.info(f"Executing Pie Creation: {pie_payload['name']}")
        
        # Call MCP Tool
        # We use 'update_pie' with 'create' action (or infer from action)
        # Note: MCP update_pie tool signature needs: action, pie_id (optional), name (optional), weights (optional)
        # Since we are creating from scratch usually, we map CREATE_OR_UPDATE to CREATE if ID missing
        
        result = await state.mcp_client.call_tool(
            "update_pie", 
            {
                "action": "create", # Force create for new goals for now
                "pie_name": pie_payload["name"],
                "weights": pie_payload["instruments"]
            }
        )
        
        return {"status": "success", "result": str(result), "pie_name": pie_payload["name"]}

    except Exception as e:
        logger.error(f"Goal execution failed: {e}")
        # Sentinel: Sanitized error message
        raise HTTPException(status_code=500, detail="Internal Server Error")

@router.get("/portfolio/live")
async def get_live_portfolio(account_type: Optional[str] = None):
    """
    Get current live portfolio data from Trading212.
    
    Args:
        account_type: Filter by account ("invest", "isa", or "all"). Uses active account if not specified.
    
    Uses PortfolioAgent to fetch real-time positions, valuations, and P&L.
    Results are cached for 60 seconds to reduce API calls.
    Implements request coalescence using asyncio.Lock to prevent rate limit issues.
    
    Returns:
        Portfolio data with positions, total_value, total_pnl, and cash_balance
        
    Raises:
        HTTPException: If portfolio fetch fails or MCP not connected
    """
    from app_context import account_context
    account_type = account_context.get_account_or_default(account_type)
    
    cache_key = f"portfolio_live_{account_type}"
    
    # 1. First check: Fast return if cached
    cached_data = cache.get(cache_key)
    if cached_data:
        return cached_data

    # 2. Acquire lock to prevent race conditions (Request Coalescence)
    async with portfolio_lock:
        # 3. Second check: verify if cache was populated while waiting for lock
        cached_data = cache.get(cache_key)
        if cached_data:
            return cached_data

        try:
            from agents import PortfolioAgent
            
            # SOTA: Use specialized PortfolioAgent directly
            agent = PortfolioAgent()
            
            # Pass account_type to agent
            response = await agent.analyze({"account_type": account_type})
            
            if not response.success:
                raise RuntimeError(f"Portfolio analysis failed: {response.error}")
                
            data = response.data or {}
            
            # Save snapshot using ChatManager
            if data:
                state.chat_manager.save_portfolio_snapshot(
                    total_value=data.get("total_value", 0),
                    total_pnl=data.get("total_pnl", 0),
                    cash_balance=data.get("cash_balance", {}).get("total", 0) if isinstance(data.get("cash_balance"), dict) else data.get("cash_balance", 0),
                    positions=data.get("positions", []),
                )

                # Update cache (60 seconds TTL) with account-specific key
                cache.set(cache_key, data, ttl=60)

            return sanitize_nan(data)
        except Exception as e:
            logger.error(f"Error fetching portfolio: {e}")
            # Sentinel: Sanitized error message
            raise HTTPException(status_code=500, detail="Internal Server Error")

@router.get("/portfolio/history")
async def get_portfolio_history(days: int = 30, account_type: Optional[str] = None):
    """
    Get historical portfolio data.
    
    Args:
        days: Number of days of history (default 30)
        account_type: Filter by account ("invest", "isa", or "all"). Uses active account if not specified.
    
    Returns historical portfolio values for charting.
    """
    from app_context import account_context
    account_type = account_context.get_account_or_default(account_type)
    try:
        # 1. Get current portfolio positions for the specified account
        portfolio_data = await get_live_portfolio(account_type=account_type)
        
        positions = portfolio_data.get("positions", [])
        summary = portfolio_data.get("summary", {})
        free_cash = summary.get("cash_balance", {}).get("free", 0)
        
        if not positions:
             return state.chat_manager.get_portfolio_history(days=days)

        # 2. Fetch history for each position
        
        # Map ticker to quantity
        holdings = {}
        for p in positions:
            t = normalize_ticker(p.get("ticker", ""))
            q = p.get("quantity", 0)
            if t and q > 0:
                holdings[t] = holdings.get(t, 0) + q
                
        if not holdings:
             return state.chat_manager.get_portfolio_history(days=days)

        # 3. Fetch historical bars for all tickers
        import yfinance as yf
        import pandas as pd
        
        tickers = list(holdings.keys())
        
        # Determine period
        period_map = {1: "1d", 7: "5d", 30: "1mo", 90: "3mo", 365: "1y", 3650: "max"}
        period = period_map.get(days, "1mo")
        
        # Download in bulk
        tickers_str = " ".join(tickers)
        
        try:
            # Run in executor to not block
            loop = asyncio.get_running_loop()
            df = await loop.run_in_executor(None, lambda: yf.download(tickers_str, period=period, progress=False)['Close'])
            
            if df.empty:
                 return state.chat_manager.get_portfolio_history(days=days)
                 
            # Reshape logic (same as original)
            if isinstance(df, pd.Series):
                 df = df.to_frame(name=tickers[0])
            elif len(tickers) == 1 and isinstance(df, pd.DataFrame):
                 if isinstance(df.columns, pd.MultiIndex):
                     df = df.xs('Close', axis=1, level=0, drop_level=True)

                # Calculate total value per timestamp
            history_points = []
            df = df.ffill().bfill()
            
            # Vectorized calculation (Bolt Optimization: replaces slow iterrows loop)
            total_value_series = pd.Series(float(free_cash), index=df.index)
            
            for ticker, quantity in holdings.items():
                if ticker not in df.columns:
                    continue
                    
                price_series = df[ticker].copy()

                # Currency conversion heuristic for LSE (pence to pounds)
                if ticker.endswith(".L"):
                    price_series = np.where(price_series > 500, price_series / 100.0, price_series)
                    
                total_value_series += price_series * quantity

            # Filter out NaNs/Infs
            mask = ~total_value_series.isna() & ~np.isinf(total_value_series)
            valid_series = total_value_series[mask]

            history_points = [
                {
                    "timestamp": timestamp.isoformat(),
                    "total_value": round(float(val), 2),
                    "total_pnl": 0,
                    "cash_balance": round(float(free_cash), 2)
                }
                for timestamp, val in valid_series.items()
            ]
            
            logger.info(f"Generated {len(history_points)} portfolio history points")
            return sanitize_nan(history_points)

        except Exception as e:
            logger.error(f"Synthetic history failed: {e}")
            return state.chat_manager.get_portfolio_history(days=days)
            
    except Exception as e:
        logger.error(f"History error: {e}")
        return state.chat_manager.get_portfolio_history(days=days)

@router.get("/api/search")
async def search_symbols(query: str):
    """
    Search for stock symbols using Trading212 MCP.
    
    Searches across available instruments and returns matching tickers
    with normalized symbols (e.g., removes _US_EQ suffix).
    
    Args:
        query: Search term (ticker or company name)
        
    Returns:
        List of matching instruments with ticker and name
        
    Raises:
        Returns empty list if MCP not connected or search fails
    """
    if not query:
        return []

    try:
        if state.mcp_client.session:
            result = await state.mcp_client.call_tool("search_instruments", {"query": query})

            if (
                hasattr(result, "content")
                and isinstance(result.content, list)
                and len(result.content) > 0
            ):
                text_content = result.content[0]
                if hasattr(text_content, "text"):
                    import json
                    try:
                        raw_results = json.loads(text_content.text)
                        for item in raw_results:
                            if "ticker" in item:
                                item["ticker"] = normalize_ticker(item["ticker"])
                        return raw_results
                    except json.JSONDecodeError:
                        return []

            return []
        else:
            common_tickers = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META", "NFLX"]
            return [{"ticker": t, "name": t} for t in common_tickers if query.upper() in t]
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []

@router.get("/account/active")
async def get_active_account():
    """Get the currently active account type."""
    from app_context import account_context
    return {"account_type": account_context.get_active_account()}

@router.post("/account/active")
async def set_active_account(request: dict):
    """
    Set the active account type.
    Called automatically when user switches accounts in portfolio view.
    """
    from app_context import account_context
    account_type = request.get("account_type")
    
    if not account_type:
        raise HTTPException(status_code=400, detail="account_type is required")
    
    try:
        account_context.set_active_account(account_type)
        logger.info(f"Active account changed to: {account_type}")
        return {"status": "success", "account_type": account_type}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/cache/clear")
async def clear_cache():
    """
    Clear all cached data.
    
    Useful when switching accounts or after configuration changes.
    
    Returns:
        Success status
    """
    cache.clear()
    logger.info("Cache cleared successfully")
    return {"status": "success", "message": "Cache cleared"}

@router.get("/api/analysis/{ticker}")
async def get_technical_analysis(ticker: str, timeframe: str = "1Month"):
    """
    Get comprehensive technical analysis + TTM forecast for a ticker.
    
    Returns:
    - AI analysis summary (trend, support/resistance, TTM forecast)
    - Algo signals (RSI, MACD, Bollinger Bands)
    - Timestamp
    
    Cached for 30 seconds to reduce compute.
    """
    from datetime import datetime
    
    cache_key = f"analysis_{ticker}_{timeframe}"
    cached = cache.get(cache_key)
    if cached:
        return cached
    
    try:
        from agents.quant_agent import QuantAgent
        from agents.forecasting_agent import ForecastingAgent
        from data_engine import get_alpaca_client
        
        # Fetch OHLCV data
        alpaca = get_alpaca_client()
        
        # Map timeframe to limit
        # SOTA: We need at least 512 bars for the TTM-R2 model to trigger!
        limit_map = {
            "1Day": 512, "1Week": 512, "1Month": 512,
            "3Month": 512, "1Year": 512, "Max": 1000
        }
        limit = limit_map.get(timeframe, 512)
        
        result = await alpaca.get_historical_bars(ticker, timeframe=timeframe, limit=limit)
        bars = result.get("bars", [])
        
        if not bars or len(bars) < 50:
            return {
                "ai_analysis": "Insufficient data for analysis.",
                "algo_signals": "Need at least 50 data points.",
                "last_updated": datetime.now().isoformat()
            }
        
        # Run QuantAgent and ForecastingAgent in parallel with timeouts
        quant = QuantAgent()
        forecast_agent = ForecastingAgent()
        
        async def run_quant():
            try:
                return await asyncio.wait_for(
                    quant.execute({
                        "ticker": ticker,
                        "ohlcv_data": bars
                    }),
                    timeout=10.0
                )
            except asyncio.TimeoutError:
                logger.warning(f"QuantAgent timeout for {ticker}")
                return None
            except Exception as e:
                logger.error(f"QuantAgent error for {ticker}: {e}")
                return None
        
        async def run_forecast():
            try:
                return await asyncio.wait_for(
                    forecast_agent.execute({
                        "ticker": ticker,
                        "ohlcv_data": bars,
                        "days": 7,  # Default horizon
                        "timeframe": timeframe
                    }),
                    timeout=10.0
                )
            except asyncio.TimeoutError:
                logger.warning(f"ForecastingAgent timeout for {ticker}")
                return None
            except Exception as e:
                logger.error(f"ForecastingAgent error for {ticker}: {e}")
                return None
        
        # Execute both agents in parallel
        results = await asyncio.gather(run_quant(), run_forecast(), return_exceptions=True)
        quant_response = results[0] if not isinstance(results[0], Exception) else None
        forecast_response = results[1] if not isinstance(results[1], Exception) else None
        
        # Handle errors gracefully
        quant_data = {}
        if quant_response and hasattr(quant_response, 'success') and quant_response.success:
            quant_data = quant_response.data or {}
        
        forecast_data = {}
        if forecast_response and hasattr(forecast_response, 'success') and forecast_response.success:
            forecast_data = forecast_response.data or {}
        
        # Generate AI Analysis text (combines technical + forecast)
        try:
            ai_analysis = _generate_ai_analysis_text(quant_data, forecast_data, bars)
        except Exception as e:
            logger.error(f"Error generating AI analysis: {e}")
            ai_analysis = "Analysis generation failed."
        
        # Generate Algo Signals text (technical indicators only)
        try:
            algo_signals = _generate_algo_signals_text(quant_data)
        except Exception as e:
            logger.error(f"Error generating algo signals: {e}")
            algo_signals = "Signals generation failed."
        
        result = {
            "ai_analysis": ai_analysis,
            "algo_signals": algo_signals,
            "last_updated": datetime.now().isoformat(),
            "raw_quant": quant_data,  # For debugging
            "raw_forecast": forecast_data  # For debugging
        }
        
        # Cache for 30 seconds
        cache.set(cache_key, result, ttl=30)
        
        return sanitize_nan(result)
        
    except Exception as e:
        logger.error(f"Analysis error for {ticker}: {e}")
        return {
            "ai_analysis": f"Error analyzing {ticker}.",
            # Sentinel: Sanitized error message
            "algo_signals": "Analysis unavailable due to internal error.",
            "last_updated": datetime.now().isoformat()
        }

def _generate_ai_analysis_text(quant_data: dict, forecast_data: dict, bars: list) -> str:
    """Generate human-readable AI analysis from quant + forecast data."""
    parts = []
    
    # 1. Current trend from quant data
    support = quant_data.get("support_level") or 0
    resistance = quant_data.get("resistance_level") or 0
    
    if not bars or len(bars) < 21:
        return "Insufficient data for trend analysis."
    
    current_price = bars[-1]['c']
    
    # Determine trend (compare to 20 bars ago)
    if len(bars) >= 21:
        past_price = bars[-21]['c']
        if current_price > past_price:
            trend = "bullish"
        elif current_price < past_price:
            trend = "bearish"
        else:
            trend = "neutral"
    else:
        trend = "neutral"
    
    parts.append(f"Trend identifies as {trend} on current timeframe.")
    
    # 2. Support/Resistance levels
    # Normalize if needed (heuristic: if > 500, likely pence)
    if resistance > 500: resistance = resistance / 100.0
    if support > 500: support = support / 100.0
    if current_price > 500: current_price = current_price / 100.0
    
    if resistance and resistance > current_price:
        distance = ((resistance - current_price) / current_price) * 100
        parts.append(f"Resistance at £{resistance:.2f} ({distance:.1f}% above).")
    
    if support and support > 0 and support < current_price:
        distance = ((current_price - support) / current_price) * 100
        parts.append(f"Support at £{support:.2f} ({distance:.1f}% below).")
    
    # 3. Forecast (if available)
    if forecast_data:
        forecast_24h = forecast_data.get("forecast_24h")
        confidence = forecast_data.get("confidence", "MEDIUM")
        
        # Normalize forecast if needed
        if forecast_24h and forecast_24h > 500:
             forecast_24h = forecast_24h / 100.0
        
        if forecast_24h:
            change_pct = ((forecast_24h - current_price) / current_price) * 100
            direction = "up" if change_pct > 0 else "down"
            parts.append(f"Forecast predicts {direction} {abs(change_pct):.1f}% in 24h ({confidence} confidence).")
    
    return " ".join(parts)

def _generate_algo_signals_text(quant_data: dict) -> str:
    """Generate algo signals text from technical indicators."""
    if not quant_data:
        return "Technical indicators unavailable."
    
    parts = []
    
    # RSI signal
    rsi = quant_data.get("rsi") or 50
    if rsi < 30:
        parts.append(f"RSI oversold at {rsi:.1f}.")
    elif rsi > 70:
        parts.append(f"RSI overbought at {rsi:.1f}.")
    else:
        parts.append(f"RSI neutral at {rsi:.1f}.")
    
    # MACD signal (handle null values)
    macd_data = quant_data.get("macd")
    if macd_data and isinstance(macd_data, dict):
        macd_value = macd_data.get("value") or 0
        macd_signal = macd_data.get("signal") or 0
        
        if macd_value and macd_signal:
            if macd_value > macd_signal:
                if macd_value > 0:
                    parts.append("MACD bullish crossover confirmed.")
                else:
                    parts.append("MACD showing early bullish divergence.")
            elif macd_value < macd_signal:
                if macd_value < 0:
                    parts.append("MACD bearish crossover confirmed.")
                else:
                    parts.append("MACD showing early bearish divergence.")
            else:
                parts.append("MACD crossover imminent.")
        else:
            parts.append("MACD data incomplete.")
    else:
        parts.append("MACD unavailable.")
    
    return " ".join(parts)
