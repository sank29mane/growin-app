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
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Resource, TextContent, Tool
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
from utils.currency_utils import CurrencyNormalizer, normalize_all_positions, calculate_portfolio_value
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
    # Standard RSI uses Wilder's Smoothing (alpha=1/14), but we use SMA (rolling mean)
    # to maintain compatibility with previous implementation logic.
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
    # Optimize: reuse rolling object for mean and std
    roller_20 = df["Close"].rolling(window=20)
    df["BB_Middle"] = roller_20.mean()
    std_dev = roller_20.std()

    df["BB_Upper"] = df["BB_Middle"] + (std_dev * 2)
    df["BB_Lower"] = df["BB_Middle"] - (std_dev * 2)

    return df


class FileCache:
    """
    Persistent cache with TTL and disk storage.
    Optimized for minimizing API calls for heavy static data (metadata).
    """

    def __init__(self, filename: str = ".t212_cache.json", ttl_seconds: int = 3600):
        self.filename = filename
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[str, Any] = {}
        self._timestamps: Dict[str, float] = {}
        self._load_from_disk()

    def _load_from_disk(self):
        """Load cache from disk on initialization."""
        if os.path.exists(self.filename):
            try:
                with open(self.filename, 'r') as f:
                    data = json.load(f)
                    self._cache = data.get('cache', {})
                    self._timestamps = data.get('timestamps', {})
                # Clean expired items immediately on load
                self._cleanup_expired()
            except Exception as e:
                print(f"Warning: Failed to load cache from {self.filename}: {e}", file=sys.stderr)

    def _save_to_disk(self):
        """Save current cache state to disk."""
        try:
            with open(self.filename, 'w') as f:
                json.dump({
                    'cache': self._cache,
                    'timestamps': self._timestamps
                }, f)
        except Exception as e:
            print(f"Warning: Failed to save cache to {self.filename}: {e}", file=sys.stderr)

    def _cleanup_expired(self):
        """Remove expired items."""
        now = time.time()
        expired = [k for k, ts in self._timestamps.items() if now - ts > self.ttl_seconds]
        for k in expired:
            del self._cache[k]
            del self._timestamps[k]
        if expired:
            self._save_to_disk()

    def get(self, key: str) -> Optional[Any]:
        """Get value if not expired."""
        value, is_expired = self.get_with_expiry_status(key)
        if not is_expired:
            return value
        return None

    def get_with_expiry_status(self, key: str) -> tuple[Optional[Any], bool]:
        """
        Get value and expiration status. 
        Returns (value, is_expired). 
        If key missing, returns (None, True).
        """
        if key in self._cache:
            is_expired = (time.time() - self._timestamps[key] > self.ttl_seconds)
            if is_expired:
                # We do NOT auto-delete here anymore, to allow stale fallback.
                # Cleanup happens in _cleanup_expired() or manual maintenance.
                pass
            return self._cache[key], is_expired
        return None, True

    def set(self, key: str, value: Any, custom_ttl: Optional[int] = None):
        """
        Set value in cache. 
        
        Args:
            key: Cache key
            value: Value to store
            custom_ttl: Optional custom TTL for this specific item (overrides default)
        """
        self._cache[key] = value
        # Use custom TTL logic if needed, but here we just store timestamp. 
        # For simplicity, if we want longer TTLs for specific items, we might need a metadata dict.
        # But for now, we'll stick to the global logic or just rely on the fact that metadata is the main use case.
        # WAIT: The requirement is aggressive caching for metadata (24h) vs short for others.
        # Let's simple check the key name or passed arg?
        # The simplest way is to rely on the class TTL, or allow passing it.
        # But our storage only stores 'timestamps' (creation time).
        # We need to store expiry time effectively if we want per-item TTL.
        
        # IMPROVED LOGIC: Store expiry time directly.
        ttl = custom_ttl if custom_ttl is not None else self.ttl_seconds
        
        # We'll store the *expiration timestamp*, not the creation timestamp, to handle variable TTLs.
        # But to be backward compatible with the '_load' logic above which expects 'timestamps' to be creation time?
        # Let's refactor slightly to be clean.
        self._timestamps[key] = time.time() # This is creation time
        
        # Currently the get() method uses self.ttl_seconds global. 
        # If we want per-item custom TTL, we should store it.
        # Hook: wrapper wrapper.
        # Let's just update the file structure to support it?
        # For minimal disruption, let's just make the default TTL huge (24h) if we instantiate it that way,
        # OR, since we replace the class, we can do whatever we want.
        
        # Let's use a `ttl_map` if we want valid variance, OR just assume this cache instance is used for Metadata primarily.
        # The 'Trading212Client' creates `self.cache = Cache()`.
        # I will change that instantiation too.
        
        self._save_to_disk()

    # Re-implementing get/set to handle per-item TTL properly would be best.
    # But let's stick to the interface.
    # To support the "aggressive metadata caching", I will change the default TTL in usage to 24h.



class Trading212Client:
    """Client for Trading 212 API operations."""

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    def __init__(self, api_key: str, api_secret: str, use_demo: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = DEMO_API_BASE if use_demo else LIVE_API_BASE

        # Determine authorization header
        if api_secret:
            # Some wrappers might use Basic Auth
            credentials = f"{api_key}:{api_secret}"
            encoded_credentials = base64.b64encode(credentials.encode("utf-8")).decode(
                "utf-8"
            )
            self.auth_header = f"Basic {encoded_credentials}"
        else:
            # Official Trading 212 API uses the API key directly in the Authorization header
            self.auth_header = api_key

        self.client = httpx.AsyncClient(
            headers={
                "Authorization": self.auth_header,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )
        self.cache = FileCache(ttl_seconds=86400) # 24h persistent cache for metadata

    async def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Make an API request with retry logic for rate limits."""
        url = f"{self.base_url}/{endpoint}"
        max_retries = 3
        base_delay = 1.0  # seconds

        for attempt in range(max_retries + 1):
            try:
                response = await self.client.request(method, url, **kwargs)
                response.raise_for_status()

                if response.content:
                    return response.json()
                return {}

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    if attempt < max_retries:
                        # Exponential backoff
                        delay = base_delay * (2**attempt)
                        print(
                            f"Rate limit hit, retrying in {delay} seconds... (attempt {attempt + 1}/{max_retries + 1})",
                            file=sys.stderr,
                        )
                        await asyncio.sleep(delay)
                        continue
                    else:
                        print(
                            f"Rate limit persisted after {max_retries + 1} attempts",
                            file=sys.stderr,
                        )
                        raise
                else:
                    raise
            except Exception as e:
                if attempt < max_retries:
                    delay = base_delay * (2**attempt)
                    print(
                        f"Request failed with {type(e).__name__}, retrying in {delay} seconds... (attempt {attempt + 1}/{max_retries + 1})",
                        file=sys.stderr,
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    raise

    # Account Data Methods
    async def get_account_info(self) -> dict:
        """Get account information."""
        return await self._request("GET", "equity/account/info")

    async def get_account_cash(self) -> dict:
        """Get account cash balance."""
        return await self._request("GET", "equity/account/cash")

    # Portfolio Methods
    async def get_all_positions(self) -> list:
        """Fetch all open positions."""
        return await self._request("GET", "equity/portfolio")

    async def get_position_by_ticker(self, ticker: str) -> dict:
        """
        Fetch a specific position by ticker.

        Args:
            ticker: The instrument ticker symbol
        """
        return await self._request("GET", f"equity/portfolio/{ticker}")

    # Order Methods
    async def get_all_orders(self) -> list:
        """Get all pending orders."""
        return await self._request("GET", "equity/orders")

    async def get_order_by_id(self, order_id: str) -> dict:
        """
        Get order details by ID.

        Args:
            order_id: The order ID
        """
        return await self._request("GET", f"equity/orders/{order_id}")

    async def place_market_order(
        self, ticker: str, quantity: float, order_type: str = "BUY"
    ) -> dict:
        """
        Place a market order.

        Args:
            ticker: Instrument ticker
            quantity: Number of shares/units (positive for BUY, negative for SELL)
            order_type: BUY or SELL
        """
        # Trading212 API uses positive quantities for BUY and negative for SELL
        adjusted_quantity = quantity if order_type.upper() == "BUY" else -abs(quantity)

        payload = {"ticker": ticker, "quantity": adjusted_quantity}
        return await self._request("POST", "equity/orders/market", json=payload)

    async def place_limit_order(
        self,
        ticker: str,
        quantity: float,
        limit_price: float,
        order_type: str = "BUY",
        time_validity: str = "DAY",
    ) -> dict:
        """
        Place a limit order.

        Args:
            ticker: Instrument ticker
            quantity: Number of shares/units
            limit_price: Limit price
            order_type: BUY or SELL
            time_validity: DAY or GOOD_TILL_CANCEL
        """
        # Trading212 API uses positive quantities for BUY and negative for SELL
        adjusted_quantity = quantity if order_type.upper() == "BUY" else -abs(quantity)
        # Convert GTC to GOOD_TILL_CANCEL for API compatibility
        api_time_validity = (
            "GOOD_TILL_CANCEL" if time_validity.upper() == "GTC" else time_validity
        )

        payload = {
            "ticker": ticker,
            "quantity": adjusted_quantity,
            "limitPrice": limit_price,
            "timeValidity": api_time_validity,
        }
        return await self._request("POST", "equity/orders/limit", json=payload)

    async def place_stop_order(
        self,
        ticker: str,
        quantity: float,
        stop_price: float,
        order_type: str = "BUY",
        time_validity: str = "DAY",
    ) -> dict:
        """
        Place a stop order.

        Args:
            ticker: Instrument ticker
            quantity: Number of shares/units
            stop_price: Stop price
            order_type: BUY or SELL
            time_validity: DAY or GOOD_TILL_CANCEL
        """
        # Trading212 API uses positive quantities for BUY and negative for SELL
        adjusted_quantity = quantity if order_type.upper() == "BUY" else -abs(quantity)
        # Convert GTC to GOOD_TILL_CANCEL for API compatibility
        api_time_validity = (
            "GOOD_TILL_CANCEL" if time_validity.upper() == "GTC" else time_validity
        )

        payload = {
            "ticker": ticker,
            "quantity": adjusted_quantity,
            "stopPrice": stop_price,
            "timeValidity": api_time_validity,
        }
        return await self._request("POST", "equity/orders/stop", json=payload)

    async def place_stop_limit_order(
        self,
        ticker: str,
        quantity: float,
        limit_price: float,
        stop_price: float,
        order_type: str = "BUY",
        time_validity: str = "DAY",
    ) -> dict:
        """
        Place a stop-limit order.

        Args:
            ticker: Instrument ticker
            quantity: Number of shares/units
            limit_price: Limit price
            stop_price: Stop price
            order_type: BUY or SELL
            time_validity: DAY or GOOD_TILL_CANCEL
        """
        # Trading212 API uses positive quantities for BUY and negative for SELL
        adjusted_quantity = quantity if order_type.upper() == "BUY" else -abs(quantity)
        # Convert GTC to GOOD_TILL_CANCEL for API compatibility
        api_time_validity = (
            "GOOD_TILL_CANCEL" if time_validity.upper() == "GTC" else time_validity
        )

        payload = {
            "ticker": ticker,
            "quantity": adjusted_quantity,
            "limitPrice": limit_price,
            "stopPrice": stop_price,
            "timeValidity": api_time_validity,
        }
        return await self._request("POST", "equity/orders/stop_limit", json=payload)

    async def cancel_order(self, order_id: str) -> dict:
        """
        Cancel a pending order.

        Args:
            order_id: The order ID to cancel
        """
        return await self._request("DELETE", f"equity/orders/{order_id}")

    # Historical Data Methods
    async def get_historical_orders(
        self, cursor: Optional[int] = None, limit: int = 50
    ) -> dict:
        """
        Get historical order data.

        Args:
            cursor: Pagination cursor
            limit: Number of results (max 50)
        """
        params = {"limit": min(limit, 50)}
        if cursor:
            params["cursor"] = cursor

        query_string = urlencode(params)
        return await self._request("GET", f"equity/history/orders?{query_string}")

    async def get_dividends(
        self, cursor: Optional[int] = None, limit: int = 50
    ) -> dict:
        """
        Get paid out dividends.

        Args:
            cursor: Pagination cursor
            limit: Number of results (max 50)
        """
        params = {"limit": min(limit, 50)}
        if cursor:
            params["cursor"] = cursor

        query_string = urlencode(params)
        return await self._request("GET", f"equity/history/dividends?{query_string}")

    async def get_transactions(
        self, cursor: Optional[int] = None, limit: int = 50
    ) -> dict:
        """
        Get transaction list.

        Args:
            cursor: Pagination cursor
            limit: Number of results (max 50)
        """
        params = {"limit": min(limit, 50)}
        if cursor:
            params["cursor"] = cursor

        query_string = urlencode(params)
        return await self._request("GET", f"equity/history/transactions?{query_string}")

    # Instruments Metadata Methods
    async def get_exchanges(self) -> list:
        """Get list of all exchanges."""
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
                # Try fallback to stale
                stale_data, _ = self.cache.get_with_expiry_status(cache_key)
                if stale_data:
                    print(f"Warning: Rate limit hit. Exposing STALE exchanges data (fallback).", file=sys.stderr)
                    return stale_data
            raise

    async def get_instruments(self) -> list:
        """Get list of all tradable instruments."""
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
                # Try fallback to stale
                stale_data, _ = self.cache.get_with_expiry_status(cache_key)
                if stale_data:
                    print(f"Warning: Rate limit hit. Exposing STALE instruments data (fallback).", file=sys.stderr)
                    return stale_data
            raise

    # Pies Methods
    async def get_all_pies(self) -> list:
        """Fetch all investment pies."""
        return await self._request("GET", "equity/pies")

    async def get_pie(self, pie_id: int) -> dict:
        """
        Fetch a specific pie.

        Args:
            pie_id: The pie ID
        """
        return await self._request("GET", f"equity/pies/{pie_id}")

    async def create_pie(self, name: str, icon: str, instruments: list) -> dict:
        """
        Create a new pie.

        Args:
            name: Pie name
            icon: Icon name
            instruments: List of instruments with tickers and target shares
        """
        payload = {"name": name, "icon": icon, "instruments": instruments}
        return await self._request("POST", "equity/pies", json=payload)

    async def update_pie(
        self, pie_id: int, name: str, icon: str, instruments: list
    ) -> dict:
        """
        Update an existing pie.

        Args:
            pie_id: The pie ID
            name: New pie name
            icon: New icon name
            instruments: Updated list of instruments
        """
        payload = {"name": name, "icon": icon, "instruments": instruments}
        return await self._request("POST", f"equity/pies/{pie_id}", json=payload)

    async def delete_pie(self, pie_id: int) -> dict:
        """
        Delete a pie.

        Args:
            pie_id: The pie ID to delete
        """
        return await self._request("DELETE", f"equity/pies/{pie_id}")

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


# Initialize MCP Server
app = Server("trading212-mcp-server")


@app.list_resources()
async def list_resources() -> list[Resource]:
    """List available Trading 212 resources."""
    return [
        Resource(
            uri="trading212://account/info",
            name="Account Information",
            mimeType="application/json",
            description="Complete account information including currency and account type",
        ),
        Resource(
            uri="trading212://account/cash",
            name="Account Cash Balance",
            mimeType="application/json",
            description="Current cash balance, blocked funds, and available cash",
        ),
        Resource(
            uri="trading212://portfolio/positions",
            name="All Portfolio Positions",
            mimeType="application/json",
            description="All open positions with current values and P&L",
        ),
        Resource(
            uri="trading212://orders/pending",
            name="Pending Orders",
            mimeType="application/json",
            description="All pending orders (limit, stop, stop-limit)",
        ),
        Resource(
            uri="trading212://instruments/all",
            name="All Instruments",
            mimeType="application/json",
            description="List of all tradable instruments with metadata",
        ),
        Resource(
            uri="trading212://exchanges/all",
            name="All Exchanges",
            mimeType="application/json",
            description="List of all available exchanges",
        ),
        Resource(
            uri="trading212://pies/all",
            name="Investment Pies",
            mimeType="application/json",
            description="All investment pies with their allocations",
        ),
        Resource(
            uri="trading212://history/orders",
            name="Historical Orders",
            mimeType="application/json",
            description="Historical executed orders",
        ),
        Resource(
            uri="trading212://history/dividends",
            name="Dividend History",
            mimeType="application/json",
            description="All paid out dividends",
        ),
        Resource(
            uri="trading212://history/transactions",
            name="Transaction History",
            mimeType="application/json",
            description="Complete transaction history",
        ),
    ]


@app.read_resource()
async def read_resource(uri: str) -> str:
    """Read a Trading 212 resource."""
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
    """List available Trading 212 tools."""
    return [
        # Account Analysis Tools
        Tool(
            name="analyze_portfolio",
            description="Analyze portfolio including positions, P&L, diversification. Optionally filter by account type (invest/isa) or get all accounts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "account_type": {
                        "type": "string",
                        "enum": ["invest", "isa", "all"],
                        "description": "Filter by account type. 'all' returns combined data from both accounts. If not specified, uses the currently active account.",
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="get_position_details",
            description="Get detailed information about a specific position by ticker symbol",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "The ticker symbol (e.g., AAPL, TSLA)",
                    }
                },
                "required": ["ticker"],
            },
        ),
        # Trading Tools
        Tool(
            name="place_market_order",
            description="Place a market order to buy or sell immediately at current market price",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Instrument ticker symbol",
                    },
                    "quantity": {
                        "type": "number",
                        "description": "Number of shares/units to trade",
                    },
                    "order_type": {
                        "type": "string",
                        "enum": ["BUY", "SELL"],
                        "description": "Whether to buy or sell",
                    },
                },
                "required": ["ticker", "quantity", "order_type"],
            },
        ),
        Tool(
            name="place_limit_order",
            description="Place a limit order to buy or sell at a specific price or better",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Instrument ticker symbol",
                    },
                    "quantity": {
                        "type": "number",
                        "description": "Number of shares/units to trade",
                    },
                    "limit_price": {
                        "type": "number",
                        "description": "Maximum price to pay (BUY) or minimum price to accept (SELL)",
                    },
                    "order_type": {
                        "type": "string",
                        "enum": ["BUY", "SELL"],
                        "description": "Whether to buy or sell",
                    },
                    "time_validity": {
                        "type": "string",
                        "enum": ["DAY", "GTC"],
                        "description": "Order validity: DAY (expires end of day) or GTC (Good Till Cancelled)",
                        "default": "DAY",
                    },
                },
                "required": ["ticker", "quantity", "limit_price", "order_type"],
            },
        ),
        Tool(
            name="place_stop_order",
            description="Place a stop order that triggers a market order when price reaches stop price",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Instrument ticker symbol",
                    },
                    "quantity": {
                        "type": "number",
                        "description": "Number of shares/units to trade",
                    },
                    "stop_price": {
                        "type": "number",
                        "description": "Price that triggers the order",
                    },
                    "order_type": {
                        "type": "string",
                        "enum": ["BUY", "SELL"],
                        "description": "Whether to buy or sell",
                    },
                    "time_validity": {
                        "type": "string",
                        "enum": ["DAY", "GTC"],
                        "description": "Order validity",
                        "default": "DAY",
                    },
                },
                "required": ["ticker", "quantity", "stop_price", "order_type"],
            },
        ),
        Tool(
            name="place_stop_limit_order",
            description="Place a stop-limit order that triggers a limit order when stop price is reached",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Instrument ticker symbol",
                    },
                    "quantity": {
                        "type": "number",
                        "description": "Number of shares/units to trade",
                    },
                    "stop_price": {
                        "type": "number",
                        "description": "Price that triggers the limit order",
                    },
                    "limit_price": {
                        "type": "number",
                        "description": "Limit price for the triggered order",
                    },
                    "order_type": {
                        "type": "string",
                        "enum": ["BUY", "SELL"],
                        "description": "Whether to buy or sell",
                    },
                    "time_validity": {
                        "type": "string",
                        "enum": ["DAY", "GTC"],
                        "description": "Order validity",
                        "default": "DAY",
                    },
                },
                "required": [
                    "ticker",
                    "quantity",
                    "stop_price",
                    "limit_price",
                    "order_type",
                ],
            },
        ),
        Tool(
            name="cancel_order",
            description="Cancel a pending order by order ID",
            inputSchema={
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "The ID of the order to cancel",
                    }
                },
                "required": ["order_id"],
            },
        ),
        # Analysis and Research Tools
        Tool(
            name="search_instruments",
            description="Search for tradable instruments by name or ticker",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (ticker or company name)",
                    }
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_historical_performance",
            description="Analyze historical trading performance including win rate and average returns",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "number",
                        "description": "Number of historical orders to analyze (max 50)",
                        "default": 50,
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="calculate_portfolio_metrics",
            description="Calculate comprehensive portfolio metrics including total value, P&L, sector allocation, and risk indicators",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        # Pie Management Tools
        Tool(
            name="get_all_pies",
            description="Fetch all investment pies associated with the account",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_pie_details",
            description="Get detailed information about an investment pie",
            inputSchema={
                "type": "object",
                "properties": {
                    "pie_id": {"type": "number", "description": "The pie ID"}
                },
                "required": ["pie_id"],
            },
        ),
        Tool(
            name="create_investment_pie",
            description="Create a new investment pie with specified allocations",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the pie"},
                    "icon": {"type": "string", "description": "Icon name for the pie"},
                    "instruments": {
                        "type": "array",
                        "description": "Array of instruments with ticker and targetShare percentage",
                        "items": {
                            "type": "object",
                            "properties": {
                                "ticker": {"type": "string"},
                                "targetShare": {"type": "number"},
                            },
                        },
                    },
                },
                "required": ["name", "icon", "instruments"],
            },
        ),
        Tool(
            name="update_investment_pie",
            description="Update an existing investment pie (e.g. rebalance weights)",
            inputSchema={
                "type": "object",
                "properties": {
                    "pie_id": {"type": "number", "description": "The pie ID"},
                    "name": {"type": "string", "description": "New pie name"},
                    "icon": {"type": "string", "description": "New icon name"},
                    "instruments": {
                        "type": "array",
                        "description": "Updated array of instruments with ticker and targetShare",
                        "items": {
                            "type": "object",
                            "properties": {
                                "ticker": {"type": "string"},
                                "targetShare": {"type": "number"},
                            },
                        },
                    },
                },
                "required": ["pie_id", "name", "icon", "instruments"],
            },
        ),
        Tool(
            name="delete_investment_pie",
            description="Delete an investment pie",
            inputSchema={
                "type": "object",
                "properties": {
                    "pie_id": {"type": "number", "description": "The ID of the pie to delete"}
                },
                "required": ["pie_id"],
            },
        ),
        # Account Management Tools
        Tool(
            name="switch_account",
            description="Switch between Invest and ISA accounts and optionally update credentials",
            inputSchema={
                "type": "object",
                "properties": {
                    "account_type": {
                        "type": "string",
                        "enum": ["invest", "isa"],
                        "description": "The account type to switch to",
                    },
                    "key": {
                        "type": "string",
                        "description": "Optional: New API key for this account type",
                    },
                    "secret": {
                        "type": "string",
                        "description": "Optional: New API secret for this account type",
                    },
                },
                "required": ["account_type"],
            },
        ),
        # Market Data & Analytics (Powered by yfinance)
        Tool(
            name="get_price_history",
            description="Fetch historical price candles (OHLCV) for a ticker with optional custom date range. Provides detailed statistics including ups/downs, price changes, and volatility metrics.",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {
                        "type": "string",
                        "description": "Ticker symbol (e.g., AAPL, TSLA, SGLN.L for London stocks)",
                    },
                    "start_date": {
                        "type": "string",
                        "description": "Start date in YYYY-MM-DD format (e.g., 2024-12-01). If provided, end_date must also be provided.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date in YYYY-MM-DD format (e.g., 2024-12-20). Defaults to today if start_date is provided.",
                    },
                    "period": {
                        "type": "string",
                        "enum": [
                            "1d",
                            "5d",
                            "3mo",
                            "6mo",
                            "1y",
                            "2y",
                            "5y",
                            "10y",
                            "ytd",
                            "max",
                        ],
                        "description": "Data period (used only if start_date is not provided)",
                        "default": "3mo",
                    },
                    "interval": {
                        "type": "string",
                        "enum": [
                            "1m",
                            "2m",
                            "5m",
                            "15m",
                            "30m",
                            "60m",
                            "90m",
                            "1h",
                            "1d",
                            "5d",
                            "1wk",
                            "1mo",
                            "3mo",
                        ],
                        "description": "Data interval",
                        "default": "1d",
                    },
                },
                "required": ["ticker"],
            },
        ),
        Tool(
            name="get_ticker_analysis",
            description="Get detailed analysis, metadata, and financial ratios for a ticker",
            inputSchema={
                "type": "object",
                "properties": {"ticker": {"type": "string", "description": "Ticker symbol"}},
                "required": ["ticker"],
            },
        ),
        Tool(
            name="calculate_technical_indicators",
            description="Calculate technical indicators (SMA, RSI, MACD, Bollinger Bands) for a ticker",
            inputSchema={
                "type": "object",
                "properties": {"ticker": {"type": "string", "description": "Ticker symbol"}},
                "period": {
                    "type": "string",
                    "enum": ["3mo", "6mo", "1y", "2y", "5y"],
                    "description": "Historical data period to use for calculation",
                    "default": "1y",
                },
                "interval": {
                    "type": "string",
                    "enum": ["1d", "1wk", "1mo"],
                    "description": "Data interval",
                    "default": "1d",
                },
            },
            "required": ["ticker"],
            },
        ),
        Tool(
            name="get_current_price",
            description="Get the current real-time or delayed price for a ticker. Tries Trading 212 first, falls back to Yahoo Finance.",
            inputSchema={
                "type": "object",
                "properties": {"ticker": {"type": "string", "description": "Ticker symbol"}},
                "required": ["ticker"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    """Execute a Trading 212 tool."""
    global active_account_type
    c = get_active_client()

    try:
        if name == "analyze_portfolio":
            return await handle_analyze_portfolio(
                arguments,
                active_account_type,
                get_clients,
                clients
            )

        elif name == "get_position_details":
            ticker = arguments["ticker"].upper()
            all_clients = get_clients()

            for acc_type, c in all_clients.items():
                try:
                    position = await c.get_position_by_ticker(ticker)
                    if position:
                        position["account_type"] = acc_type
                        return [
                            TextContent(
                                type="text",
                                text=json.dumps(sanitize_nan(position), separators=( ",", ":"))
                            )
                        ]
                except:
                    continue

            return [
                TextContent(
                    type="text", text=f"Position {ticker} not found in any account."
                )
            ]

        elif name == "place_market_order":
            return await handle_market_order(arguments, c)

        elif name == "place_limit_order":
            result = await c.place_limit_order(
                ticker=arguments["ticker"],
                quantity=arguments["quantity"],
                limit_price=arguments["limit_price"],
                order_type=arguments["order_type"],
                time_validity=arguments.get("time_validity", "DAY"),
            )
            return [
                TextContent(
                    type="text",
                    text=f"Limit order placed successfully:\n{json.dumps(result, indent=2)}",
                )
            ]

        elif name == "place_stop_order":
            result = await c.place_stop_order(
                ticker=arguments["ticker"],
                quantity=arguments["quantity"],
                stop_price=arguments["stop_price"],
                order_type=arguments["order_type"],
                time_validity=arguments.get("time_validity", "DAY"),
            )
            return [
                TextContent(
                    type="text",
                    text=f"Stop order placed successfully:\n{json.dumps(result, indent=2)}",
                )
            ]

        elif name == "place_stop_limit_order":
            result = await c.place_stop_limit_order(
                ticker=arguments["ticker"],
                quantity=arguments["quantity"],
                stop_price=arguments["stop_price"],
                limit_price=arguments["limit_price"],
                order_type=arguments["order_type"],
                time_validity=arguments.get("time_validity", "DAY"),
            )
            return [
                TextContent(
                    type="text",
                    text=f"Stop-limit order placed successfully:\n{json.dumps(result, indent=2)}",
                )
            ]

        elif name == "cancel_order":
            result = await c.cancel_order(arguments["order_id"])
            return [
                TextContent(
                    type="text",
                    text=f"Order cancelled successfully:\n{json.dumps(result, indent=2)}",
                )
            ]

        elif name == "search_instruments":
            query = arguments["query"].upper()
            instruments = await c.get_instruments()

            # Search by ticker or name
            results = [
                inst
                for inst in instruments
                if query in inst.get("ticker", "").upper()
                or query in inst.get("name", "").upper()
            ]

            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        sanitize_nan(results[:20]), separators=( ",", ":")) # Limit to top 20 results
                )
            ]

        elif name == "get_historical_performance":
            limit = arguments.get("limit", 50)
            history = await c.get_historical_orders(limit=min(limit, 50))

            orders = history.get("items", [])
            total_orders = len(orders)

            # Basic performance metrics
            metrics = {"total_orders": total_orders, "orders": orders}

            return [TextContent(type="text", text=json.dumps(sanitize_nan(metrics), indent=2))]

        elif name == "calculate_portfolio_metrics":
            positions, cash = await asyncio.gather(
                c.get_all_positions(), c.get_account_cash()
            )

            # Fetch metadata for normalization
            instruments = await c.get_instruments()
            metadata_cache = {i.get("ticker"): i for i in instruments}

            # Normalize positions
            positions = normalize_all_positions(positions, metadata_cache)

            # Calculate comprehensive metrics (prices already in GBP)
            total_value = sum(
                pos.get("currentPrice", 0) * pos.get("quantity", 0) for pos in positions
            )
            total_cost = sum(
                pos.get("averagePrice", 0) * pos.get("quantity", 0) for pos in positions
            )
            total_pnl = sum(pos.get("ppl", 0) for pos in positions)

            # Find biggest winners and losers
            sorted_by_pnl = sorted(
                positions, key=lambda x: x.get("ppl", 0), reverse=True
            )

            metrics = {
                "portfolio_value": round(total_value, 2),
                "total_invested": round(total_cost, 2),
                "total_pnl": round(total_pnl, 2),
                "pnl_percentage": round(
                    (total_pnl / total_cost * 100) if total_cost > 0 else 0, 2
                ),
                "cash_balance": cash,
                "number_of_positions": len(positions),
                "top_performers": sorted_by_pnl[:5],
                "worst_performers": sorted_by_pnl[-5:]
                if len(sorted_by_pnl) > 5
                else [],
            }

            return [
                TextContent(
                    type="text", text=json.dumps(sanitize_nan(metrics), separators=( ",", ":"))
                )
            ]

        elif name == "get_all_pies":
            pies = await c.get_all_pies()
            return [
                TextContent(type="text", text=json.dumps(pies, separators=( ",", ":")))
            ]

        elif name == "get_pie_details":
            pie_id = arguments["pie_id"]
            pie = await c.get_pie(pie_id)
            return [TextContent(type="text", text=json.dumps(pie, indent=2))]

        elif name == "create_investment_pie":
            result = await c.create_pie(
                name=arguments["name"],
                icon=arguments["icon"],
                instruments=arguments["instruments"],
            )
            return [
                TextContent(
                    type="text",
                    text=f"Investment pie created successfully:\n{json.dumps(result, indent=2)}",
                )
            ]

        elif name == "update_investment_pie":
            result = await c.update_pie(
                pie_id=arguments["pie_id"],
                name=arguments["name"],
                icon=arguments["icon"],
                instruments=arguments["instruments"],
            )
            return [
                TextContent(
                    type="text",
                    text=f"Investment pie updated successfully:\n{json.dumps(result, indent=2)}",
                )
            ]

        elif name == "delete_investment_pie":
            result = await c.delete_pie(pie_id=arguments["pie_id"])
            return [
                TextContent(
                    type="text",
                    text=f"Investment pie deleted successfully:\n{json.dumps(result, indent=2)}",
                )
            ]

        elif name == "switch_account":
            account_type = arguments["account_type"].lower()
            new_key = arguments.get("key")
            new_secret = arguments.get("secret")

            if account_type not in ["invest", "isa"]:
                raise ValueError("Invalid account type. Must be 'invest' or 'isa'.")

            active_account_type = account_type

            # Update/Initialize client if credentials provided
            if new_key:
                if account_type not in credentials:
                    credentials[account_type] = {}
                credentials[account_type]["key"] = new_key
                if new_secret is not None:
                    credentials[account_type]["secret"] = new_secret

                # Close old if exists
                if clients.get(account_type):
                    await clients[account_type].close()

                clients[account_type] = Trading212Client(
                    new_key, new_secret or "", credentials.get("use_demo", False)
                )

            # If no key provided but client doesn't exist, try using stored credentials
            if not clients.get(account_type) and credentials.get(account_type, {}).get(
                "key"
            ):
                creds = credentials[account_type]
                clients[account_type] = Trading212Client(
                    creds["key"],
                    creds.get("secret", ""),
                    credentials.get("use_demo", False),
                )

            if not clients.get(account_type):
                raise ValueError(
                    f"No API key found for {account_type.upper()} account. Please provide it."
                )

            # Save state
            try:
                with open(STATE_FILE, "w") as f:
                    json.dump({"account_type": active_account_type}, f)
            except Exception as e:
                print(f"Warning: Failed to save state: {e}", file=sys.stderr)

            return [
                TextContent(
                    type="text",
                    text=f"Successfully switched active account to {account_type.upper()}.",