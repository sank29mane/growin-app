#!/usr/bin/env python3
"""
Trading 212 MCP Server
A Model Context Protocol server for Trading 212 API integration.
Provides comprehensive access to account data, portfolio management, and trading operations.
"""

import asyncio
import base64
import json
import os
import sys
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Resource, TextContent, Tool
from utils import sanitize_nan
from utils.process_guard import start_parent_watchdog

# Start watchdog immediately to ensure cleanup if parent dies
start_parent_watchdog()

# Constants
LIVE_API_BASE = "https://live.trading212.com/api/v0"
DEMO_API_BASE = "https://demo.trading212.com/api/v0"
STATE_FILE = ".state.json"

# Import centralized currency normalization
from utils.currency_utils import normalize_all_positions
from utils.ticker_utils import normalize_ticker
from t212_handlers import (
    handle_analyze_portfolio,
    handle_market_order,
    handle_get_price_history,
    handle_get_current_price
)

def _compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Helper to compute technical indicators efficiently."""
    if df.empty:
        return df

    # SMA
    df["SMA_50"] = df["Close"].rolling(window=50).mean()
    df["SMA_200"] = df["Close"].rolling(window=200).mean()

    # RSI
    delta = df["Close"].diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)

    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()

    rs = avg_gain / avg_loss
    df["RSI"] = 100 - (100 / (1 + rs))

    # MACD
    exp1 = df["Close"].ewm(span=12, adjust=False).mean()
    exp2 = df["Close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = exp1 - exp2
    df["Signal_Line"] = df["MACD"].ewm(span=9, adjust=False).mean()

    # Bollinger Bands
    roller_20 = df["Close"].rolling(window=20)
    df["BB_Middle"] = roller_20.mean()
    std_dev = roller_20.std()

    df["BB_Upper"] = df["BB_Middle"] + (std_dev * 2)
    df["BB_Lower"] = df["BB_Middle"] - (std_dev * 2)

    return df


class FileCache:
    """Persistent cache with TTL and disk storage."""

    def __init__(self, filename: str = ".t212_cache.json", ttl_seconds: int = 3600):
        self.filename = filename
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[str, Any] = {}
        self._timestamps: Dict[str, float] = {}
        self._load_from_disk()

    def _load_from_disk(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    data = json.load(f)
                    self._cache = data.get('cache', {})
                    self._timestamps = data.get('timestamps', {})
                self._cleanup_expired()
            except Exception as e:
                print(f"Warning: Failed to load cache from {self.filename}: {e}", file=sys.stderr)

    def _save_to_disk(self):
        try:
            with open(self.filename, 'w') as f:
                json.dump({
                    'cache': self._cache,
                    'timestamps': self._timestamps
                }, f)
        except Exception as e:
            print(f"Warning: Failed to save cache to {self.filename}: {e}", file=sys.stderr)

    def _cleanup_expired(self):
        now = time.time()
        expired = [k for k, ts in self._timestamps.items() if now - ts > self.ttl_seconds]
        for k in expired:
            del self._cache[k]
            del self._timestamps[k]
        if expired:
            self._save_to_disk()

    def get(self, key: str) -> Optional[Any]:
        value, is_expired = self.get_with_expiry_status(key)
        if not is_expired:
            return value
        return None

    def get_with_expiry_status(self, key: str) -> tuple[Optional[Any], bool]:
        if key in self._cache:
            is_expired = (time.time() - self._timestamps[key] > self.ttl_seconds)
            return self._cache[key], is_expired
        return None, True

    def set(self, key: str, value: Any, custom_ttl: Optional[int] = None):
        self._cache[key] = value
        self._timestamps[key] = time.time()
        self._save_to_disk()


class Trading212Client:
    """Client for Trading 212 API operations."""

    def __init__(self, api_key: str, api_secret: str, use_demo: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = DEMO_API_BASE if use_demo else LIVE_API_BASE

        if api_secret:
            credentials = f"{api_key}:{api_secret}"
            encoded_credentials = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")
            self.auth_header = f"Basic {encoded_credentials}"
        else:
            self.auth_header = api_key

        self.client = httpx.AsyncClient(
            headers={
                "Authorization": self.auth_header,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        self.cache = FileCache(ttl_seconds=86400)

    async def close(self):
        await self.client.aclose()

    async def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        url = f"{self.base_url}/{endpoint}"
        max_retries = 3
        base_delay = 1.0

        for attempt in range(max_retries + 1):
            try:
                response = await self.client.request(method, url, **kwargs)
                response.raise_for_status()
                if response.content:
                    return response.json()
                return {}
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < max_retries:
                    delay = base_delay * (2**attempt)
                    await asyncio.sleep(delay)
                    continue
                raise
            except Exception:
                if attempt < max_retries:
                    delay = base_delay * (2**attempt)
                    await asyncio.sleep(delay)
                    continue
                raise

    async def get_account_info(self) -> dict:
        return await self._request("GET", "equity/account/info")

    async def get_account_cash(self) -> dict:
        return await self._request("GET", "equity/account/cash")

    async def get_all_positions(self) -> list:
        return await self._request("GET", "equity/portfolio")

    async def get_position_by_ticker(self, ticker: str) -> dict:
        return await self._request("GET", f"equity/portfolio/{ticker}")

    async def get_all_orders(self) -> list:
        return await self._request("GET", "equity/orders")

    async def get_order_by_id(self, order_id: str) -> dict:
        return await self._request("GET", f"equity/orders/{order_id}")

    async def place_market_order(self, ticker: str, quantity: float, order_type: str = "BUY") -> dict:
        adjusted_quantity = quantity if order_type.upper() == "BUY" else -abs(quantity)
        payload = {"ticker": ticker, "quantity": adjusted_quantity}
        return await self._request("POST", "equity/orders/market", json=payload)

    async def place_limit_order(self, ticker: str, quantity: float, limit_price: float, order_type: str = "BUY", time_validity: str = "DAY") -> dict:
        adjusted_quantity = quantity if order_type.upper() == "BUY" else -abs(quantity)
        api_time_validity = "GOOD_TILL_CANCEL" if time_validity.upper() == "GTC" else time_validity
        payload = {"ticker": ticker, "quantity": adjusted_quantity, "limitPrice": limit_price, "timeValidity": api_time_validity}
        return await self._request("POST", "equity/orders/limit", json=payload)

    async def place_stop_order(self, ticker: str, quantity: float, stop_price: float, order_type: str = "BUY", time_validity: str = "DAY") -> dict:
        adjusted_quantity = quantity if order_type.upper() == "BUY" else -abs(quantity)
        api_time_validity = "GOOD_TILL_CANCEL" if time_validity.upper() == "GTC" else time_validity
        payload = {"ticker": ticker, "quantity": adjusted_quantity, "stopPrice": stop_price, "timeValidity": api_time_validity}
        return await self._request("POST", "equity/orders/stop", json=payload)

    async def place_stop_limit_order(self, ticker: str, quantity: float, limit_price: float, stop_price: float, order_type: str = "BUY", time_validity: str = "DAY") -> dict:
        adjusted_quantity = quantity if order_type.upper() == "BUY" else -abs(quantity)
        api_time_validity = "GOOD_TILL_CANCEL" if time_validity.upper() == "GTC" else time_validity
        payload = {"ticker": ticker, "quantity": adjusted_quantity, "limitPrice": limit_price, "stopPrice": stop_price, "timeValidity": api_time_validity}
        return await self._request("POST", "equity/orders/stop_limit", json=payload)

    async def cancel_order(self, order_id: str) -> dict:
        return await self._request("DELETE", f"equity/orders/{order_id}")

    async def get_historical_orders(self, cursor: Optional[int] = None, limit: int = 50) -> dict:
        params = {"limit": min(limit, 50)}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", f"equity/history/orders?{urlencode(params)}")

    async def get_dividends(self, cursor: Optional[int] = None, limit: int = 50) -> dict:
        params = {"limit": min(limit, 50)}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", f"equity/history/dividends?{urlencode(params)}")

    async def get_transactions(self, cursor: Optional[int] = None, limit: int = 50) -> dict:
        params = {"limit": min(limit, 50)}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", f"equity/history/transactions?{urlencode(params)}")

    async def get_instruments(self) -> list:
        cache_key = "instruments"
        cached = self.cache.get(cache_key)
        if cached:
            return cached
        try:
            data = await self._request("GET", "equity/metadata/instruments")
            self.cache.set(cache_key, data)
            return data
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                stale_data, _ = self.cache.get_with_expiry_status(cache_key)
                if stale_data:
                    return stale_data
            raise

    async def get_exchanges(self) -> list:
        cache_key = "exchanges"
        cached = self.cache.get(cache_key)
        if cached:
            return cached
        try:
            data = await self._request("GET", "equity/metadata/exchanges")
            self.cache.set(cache_key, data)
            return data
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                stale_data, _ = self.cache.get_with_expiry_status(cache_key)
                if stale_data:
                    return stale_data
            raise

    async def get_all_pies(self) -> list:
        return await self._request("GET", "equity/pies")

    async def get_pie(self, pie_id: int) -> dict:
        return await self._request("GET", f"equity/pies/{pie_id}")

    async def create_pie(self, name: str, icon: str, instruments: list) -> dict:
        payload = {"name": name, "icon": icon, "instruments": instruments}
        return await self._request("POST", "equity/pies", json=payload)

    async def update_pie(self, pie_id: int, name: str, icon: str, instruments: list) -> dict:
        payload = {"name": name, "icon": icon, "instruments": instruments}
        return await self._request("POST", f"equity/pies/{pie_id}", json=payload)

    async def delete_pie(self, pie_id: int) -> dict:
        return await self._request("DELETE", f"equity/pies/{pie_id}")


app = Server("trading212-mcp-server")

@app.list_resources()
async def list_resources() -> list[Resource]:
    return [
        Resource(uri="trading212://account/info", name="Account Info", mimeType="application/json"),
        Resource(uri="trading212://account/cash", name="Account Cash", mimeType="application/json"),
        Resource(uri="trading212://portfolio/positions", name="Portfolio Positions", mimeType="application/json"),
        Resource(uri="trading212://orders/pending", name="Pending Orders", mimeType="application/json"),
        Resource(uri="trading212://instruments/all", name="All Instruments", mimeType="application/json"),
        Resource(uri="trading212://exchanges/all", name="All Exchanges", mimeType="application/json"),
        Resource(uri="trading212://pies/all", name="Investment Pies", mimeType="application/json"),
        Resource(uri="trading212://history/orders", name="Historical Orders", mimeType="application/json"),
        Resource(uri="trading212://history/dividends", name="Dividend History", mimeType="application/json"),
        Resource(uri="trading212://history/transactions", name="Transaction History", mimeType="application/json"),
    ]

@app.read_resource()
async def read_resource(uri: str) -> str:
    c = get_active_client()
    resource_map = {
        "trading212://account/info": c.get_account_info,
        "trading212://account/cash": c.get_account_cash,
        "trading212://portfolio/positions": c.get_all_positions,
        "trading212://orders/pending": c.get_all_orders,
        "trading212://instruments/all": c.get_instruments,
        "trading212://exchanges/all": c.get_exchanges,
        "trading212://pies/all": c.get_all_pies,
        "trading212://history/orders": lambda: c.get_historical_orders(limit=50),
        "trading212://history/dividends": lambda: c.get_dividends(limit=50),
        "trading212://history/transactions": lambda: c.get_transactions(limit=50),
    }
    if uri not in resource_map:
        raise ValueError(f"Unknown resource: {uri}")
    data = await resource_map[uri]()
    return json.dumps(data, separators=( ",", ":"))

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="analyze_portfolio", description="Analyze portfolio", inputSchema={"type": "object", "properties": {"account_type": {"type": "string", "enum": ["invest", "isa", "all"]}}}),
        Tool(name="get_position_details", description="Get position details", inputSchema={"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}),
        Tool(name="place_market_order", description="Place market order", inputSchema={"type": "object", "properties": {"ticker": {"type": "string"}, "quantity": {"type": "number"}, "order_type": {"type": "string", "enum": ["BUY", "SELL"]}}, "required": ["ticker", "quantity", "order_type"]}),
        Tool(name="place_limit_order", description="Place limit order", inputSchema={"type": "object", "properties": {"ticker": {"type": "string"}, "quantity": {"type": "number"}, "limit_price": {"type": "number"}, "order_type": {"type": "string", "enum": ["BUY", "SELL"]}, "time_validity": {"type": "string", "enum": ["DAY", "GTC"]}}, "required": ["ticker", "quantity", "limit_price", "order_type"]}),
        Tool(name="place_stop_order", description="Place stop order", inputSchema={"type": "object", "properties": {"ticker": {"type": "string"}, "quantity": {"type": "number"}, "stop_price": {"type": "number"}, "order_type": {"type": "string", "enum": ["BUY", "SELL"]}, "time_validity": {"type": "string", "enum": ["DAY", "GTC"]}}, "required": ["ticker", "quantity", "stop_price", "order_type"]}),
        Tool(name="place_stop_limit_order", description="Place stop-limit order", inputSchema={"type": "object", "properties": {"ticker": {"type": "string"}, "quantity": {"type": "number"}, "stop_price": {"type": "number"}, "limit_price": {"type": "number"}, "order_type": {"type": "string", "enum": ["BUY", "SELL"]}, "time_validity": {"type": "string", "enum": ["DAY", "GTC"]}}, "required": ["ticker", "quantity", "stop_price", "limit_price", "order_type"]}),
        Tool(name="cancel_order", description="Cancel order", inputSchema={"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}),
        Tool(name="search_instruments", description="Search instruments", inputSchema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}),
        Tool(name="get_historical_performance", description="Get performance", inputSchema={"type": "object", "properties": {"limit": {"type": "number"}}}),
        Tool(name="calculate_portfolio_metrics", description="Calculate portfolio metrics", inputSchema={"type": "object"}),
        Tool(name="get_all_pies", description="Get all pies", inputSchema={"type": "object"}),
        Tool(name="get_pie_details", description="Get pie details", inputSchema={"type": "object", "properties": {"pie_id": {"type": "number"}}, "required": ["pie_id"]}),
        Tool(name="create_investment_pie", description="Create pie", inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "icon": {"type": "string"}, "instruments": {"type": "array", "items": {"type": "object", "properties": {"ticker": {"type": "string"}, "targetShare": {"type": "number"}}}}}, "required": ["name", "icon", "instruments"]}),
        Tool(name="update_investment_pie", description="Update pie", inputSchema={"type": "object", "properties": {"pie_id": {"type": "number"}, "name": {"type": "string"}, "icon": {"type": "string"}, "instruments": {"type": "array", "items": {"type": "object", "properties": {"ticker": {"type": "string"}, "targetShare": {"type": "number"}}}}}, "required": ["pie_id", "name", "icon", "instruments"]}),
        Tool(name="delete_investment_pie", description="Delete pie", inputSchema={"type": "object", "properties": {"pie_id": {"type": "number"}}, "required": ["pie_id"]}),
        Tool(name="switch_account", description="Switch account", inputSchema={"type": "object", "properties": {"account_type": {"type": "string", "enum": ["invest", "isa"]}, "key": {"type": "string"}, "secret": {"type": "string"}}, "required": ["account_type"]}),
        Tool(name="get_price_history", description="Get price history", inputSchema={"type": "object", "properties": {"ticker": {"type": "string"}, "start_date": {"type": "string"}, "end_date": {"type": "string"}, "period": {"type": "string", "enum": ["1d", "5d", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"]}, "interval": {"type": "string", "enum": ["1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1wk", "1mo", "3mo"]}}, "required": ["ticker"]}),
        Tool(name="get_ticker_analysis", description="Get ticker analysis", inputSchema={"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}),
        Tool(name="calculate_technical_indicators", description="Calculate technical indicators", inputSchema={"type": "object", "properties": {"ticker": {"type": "string"}, "period": {"type": "string", "enum": ["3mo", "6mo", "1y", "2y", "5y"]}, "interval": {"type": "string", "enum": ["1d", "1wk", "1mo"]}}, "required": ["ticker"]}),
        Tool(name="get_current_price", description="Get current price", inputSchema={"type": "object", "properties": {"ticker": {"type": "string"}}, "required": ["ticker"]}),
    ]

@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    global active_account_type
    c = get_active_client()
    try:
        if name == "analyze_portfolio":
            return await handle_analyze_portfolio(arguments, active_account_type, get_clients, clients)
        
        elif name == "place_market_order":
            return await handle_market_order(arguments, c)
        
        elif name == "get_price_history":
            return await handle_get_price_history(arguments)
        
        elif name == "get_current_price":
            return await handle_get_current_price(arguments)
        
        elif name == "get_position_details":
            ticker = arguments["ticker"].upper()
            all_clients = get_clients()
            for acc_type, client in all_clients.items():
                try:
                    position = await client.get_position_by_ticker(ticker)
                    if position:
                        position["account_type"] = acc_type
                        return [TextContent(type="text", text=json.dumps(sanitize_nan(position), separators=(",", ":")))]
                except Exception:
                    continue
            return [TextContent(type="text", text=f"Position {ticker} not found.")]
        
        elif name == "place_limit_order":
            result = await c.place_limit_order(
                ticker=arguments["ticker"],
                quantity=arguments["quantity"],
                limit_price=arguments["limit_price"],
                order_type=arguments["order_type"],
                time_validity=arguments.get("time_validity", "DAY")
            )
            return [TextContent(type="text", text=f"Limit order placed:\n{json.dumps(result, indent=2)}")]
        
        elif name == "place_stop_order":
            result = await c.place_stop_order(
                ticker=arguments["ticker"],
                quantity=arguments["quantity"],
                stop_price=arguments["stop_price"],
                order_type=arguments["order_type"],
                time_validity=arguments.get("time_validity", "DAY")
            )
            return [TextContent(type="text", text=f"Stop order placed:\n{json.dumps(result, indent=2)}")]
        
        elif name == "place_stop_limit_order":
            result = await c.place_stop_limit_order(
                ticker=arguments["ticker"],
                quantity=arguments["quantity"],
                stop_price=arguments["stop_price"],
                limit_price=arguments["limit_price"],
                order_type=arguments["order_type"],
                time_validity=arguments.get("time_validity", "DAY")
            )
            return [TextContent(type="text", text=f"Stop-limit order placed:\n{json.dumps(result, indent=2)}")]
        
        elif name == "cancel_order":
            result = await c.cancel_order(arguments["order_id"])
            return [TextContent(type="text", text=f"Order cancelled:\n{json.dumps(result, indent=2)}")]
        
        elif name == "search_instruments":
            query = arguments["query"].upper()
            instruments = await c.get_instruments()
            results = [inst for inst in instruments if query in inst.get("ticker", "").upper() or query in inst.get("name", "").upper()]
            return [TextContent(type="text", text=json.dumps(sanitize_nan(results[:20]), separators=( ",", ":")))]
        
        elif name == "get_historical_performance":
            limit = arguments.get("limit", 50)
            history = await c.get_historical_orders(limit=min(limit, 50))
            return [TextContent(type="text", text=json.dumps(sanitize_nan(history), indent=2))]
        
        elif name == "calculate_portfolio_metrics":
            positions, cash = await asyncio.gather(c.get_all_positions(), c.get_account_cash())
            instruments = await c.get_instruments()
            metadata_cache = {i.get("ticker"): i for i in instruments}
            positions = normalize_all_positions(positions, metadata_cache)
            total_value = sum(pos.get("currentPrice", 0) * pos.get("quantity", 0) for pos in positions)
            total_cost = sum(pos.get("averagePrice", 0) * pos.get("quantity", 0) for pos in positions)
            total_pnl = sum(pos.get("ppl", 0) for pos in positions)
            sorted_by_pnl = sorted(positions, key=lambda x: x.get("ppl", 0), reverse=True)
            metrics = {
                "portfolio_value": round(total_value, 2),
                "total_invested": round(total_cost, 2),
                "total_pnl": round(total_pnl, 2),
                "pnl_percentage": round((total_pnl / total_cost * 100) if total_cost > 0 else 0, 2),
                "cash_balance": cash,
                "number_of_positions": len(positions),
                "top_performers": sorted_by_pnl[:5],
                "worst_performers": sorted_by_pnl[-5:] if len(sorted_by_pnl) > 5 else [],
            }
            return [TextContent(type="text", text=json.dumps(sanitize_nan(metrics), separators=( ",", ":")))]
        
        elif name == "get_all_pies":
            pies = await c.get_all_pies()
            return [TextContent(type="text", text=json.dumps(pies, separators=( ",", ":")))]
        
        elif name == "get_pie_details":
            pie = await c.get_pie(arguments["pie_id"])
            return [TextContent(type="text", text=json.dumps(pie, indent=2))]
        
        elif name == "create_investment_pie":
            result = await c.create_pie(name=arguments["name"], icon=arguments["icon"], instruments=arguments["instruments"])
            return [TextContent(type="text", text=f"Pie created:\n{json.dumps(result, indent=2)}")]
        
        elif name == "update_investment_pie":
            result = await c.update_pie(pie_id=arguments["pie_id"], name=arguments["name"], icon=arguments["icon"], instruments=arguments["instruments"])
            return [TextContent(type="text", text=f"Pie updated:\n{json.dumps(result, indent=2)}")]
        
        elif name == "delete_investment_pie":
            result = await c.delete_pie(arguments["pie_id"])
            return [TextContent(type="text", text=f"Pie deleted:\n{json.dumps(result, indent=2)}")]
        
        elif name == "switch_account":
            account_type = arguments["account_type"].lower()
            new_key, new_secret = arguments.get("key"), arguments.get("secret")
            if account_type not in ["invest", "isa"]:
                raise ValueError("Invalid account type.")
            active_account_type = account_type
            if new_key:
                if account_type not in credentials:
                    credentials[account_type] = {}
                credentials[account_type]["key"] = new_key
                if new_secret is not None:
                    credentials[account_type]["secret"] = new_secret
                if clients.get(account_type):
                    await clients[account_type].close()
                clients[account_type] = Trading212Client(new_key, new_secret or "", credentials.get("use_demo", False))
            
            if not clients.get(account_type):
                raise ValueError(f"No API key found for {account_type.upper()}.")
            
            try:
                with open(STATE_FILE, "w") as f:
                    json.dump({"account_type": active_account_type}, f)
            except Exception:
                pass
            return [TextContent(type="text", text=f"Switched to {account_type.upper()}.")]
        
        elif name == "get_ticker_analysis":
            ticker = normalize_ticker(arguments["ticker"])
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, lambda: yf.Ticker(ticker).info)
            keys = ["sector", "industry", "marketCap", "forwardPE", "trailingPE", "dividendYield", "fiftyTwoWeekHigh", "fiftyTwoWeekLow", "averageVolume", "currentPrice", "targetMeanPrice", "recommendationKey", "ebitda", "debtToEquity", "returnOnEquity", "freeCashflow", "beta", "shortName", "longName", "currency"]
            filtered = {k: v for k, v in info.items() if k in keys}
            return [TextContent(type="text", text=json.dumps(filtered, indent=2))]
        
        elif name == "calculate_technical_indicators":
            ticker = normalize_ticker(arguments["ticker"])
            period, interval = arguments.get("period", "1y"), arguments.get("interval", "1d")
            loop = asyncio.get_running_loop()
            df = await loop.run_in_executor(None, lambda: _compute_indicators(yf.Ticker(ticker).history(period=period, interval=interval)))
            if df is None or df.empty:
                return [TextContent(type="text", text=f"No data for {ticker}")]
            latest = df.iloc[-10:].copy().reset_index()
            latest["Date"] = latest["Date"].apply(lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x))
            cols = ["Date", "Close", "Volume", "SMA_50", "SMA_200", "RSI", "MACD", "Signal_Line", "BB_Upper", "BB_Lower"]
            cols = [c for c in cols if c in latest.columns]
            result = latest[cols].to_dict(orient="records")
            summary = {"ticker": ticker, "latest_indicators": result[-1], "recent_trend": result}
            return [TextContent(type="text", text=json.dumps(summary, indent=2, default=str))]
        
        else:
            raise ValueError(f"Unknown tool: {name}")
            
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e), "success": False}))]

clients: Dict[str, Trading212Client] = {}
credentials: dict = {}
active_account_type: str = "invest"

def get_clients() -> Dict[str, Trading212Client]:
    return {k: v for k, v in clients.items() if v is not None}

def get_active_client() -> Trading212Client:
    global active_account_type
    c = clients.get(active_account_type)
    if not c:
        available = get_clients()
        if not available:
            raise ValueError("No Trading 212 clients initialized.")
        return list(available.values())[0]
    return c

async def main():
    global clients, credentials
    load_dotenv()
    def get_env_var(name: str) -> Optional[str]:
        val = os.getenv(name)
        return val if val and val.strip() else None
    
    generic_key = get_env_var("TRADING212_API_KEY")
    invest_key = get_env_var("TRADING212_API_KEY_INVEST") or generic_key
    isa_key = get_env_var("TRADING212_API_KEY_ISA")
    
    generic_secret = get_env_var("TRADING212_API_SECRET")
    invest_secret = get_env_var("TRADING212_API_SECRET_INVEST") or generic_secret
    isa_secret = get_env_var("TRADING212_API_SECRET_ISA") or generic_secret
    
    use_demo = (get_env_var("TRADING212_USE_DEMO") or "false").lower() == "true"
    
    credentials = {
        "invest": {"key": invest_key, "secret": invest_secret},
        "isa": {"key": isa_key, "secret": isa_secret},
        "use_demo": use_demo
    }
    
    if invest_key and isa_key and invest_key == isa_key:
        clients["invest"] = Trading212Client(invest_key, invest_secret or "", use_demo)
        clients["isa"] = clients["invest"]
    else:
        if invest_key:
            clients["invest"] = Trading212Client(invest_key, invest_secret or "", use_demo)
        if isa_key:
            clients["isa"] = Trading212Client(isa_key, isa_secret or "", use_demo)
            
    global active_account_type
    active_account_type = "invest" if "invest" in clients else ("isa" if "isa" in clients else "invest")
    
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                state_data = json.load(f)
                saved_type = state_data.get("account_type")
                if saved_type in credentials:
                    active_account_type = saved_type
    except Exception:
        pass
        
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())

if __name__ == "__main__":
    asyncio.run(main())