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
from utils import sanitize_nan

# Constants
LIVE_API_BASE = "https://live.trading212.com/api/v0"
DEMO_API_BASE = "https://demo.trading212.com/api/v0"
STATE_FILE = ".state.json"

# Import centralized currency normalization
from utils.currency_utils import CurrencyNormalizer, normalize_all_positions, calculate_portfolio_value



def normalize_ticker(ticker: str) -> str:
    """
    SOTA Ticker Normalization: Resolves discrepancies between Trading212, 
    Yahoo Finance, Alpaca, and Finnhub.
    
    Tier 1 Resolution: Rule-based fast path.
    """
    if not ticker:
        return ""

    # 1. Basic Cleaning
    ticker = ticker.upper().strip().replace("$", "")
    
    # 2. Already Normalized (contains dot)
    if "." in ticker:
        return ticker

    # 3. Handle Platform-Specific Artifacts
    original = ticker
    # Strip T212 suffixes (handles multiple like _US_EQ)
    ticker = re.sub(r'(_EQ|_US|_BE|_DE|_GB|_FR|_NL|_ES|_IT)+$', '', ticker)
    ticker = ticker.replace("_", "") # Fallback for messy underscores
    
    # 4. SPECIAL MAPPINGS (SOTA curated list for T212 -> YFinance)
    # Map specifically known problematic tickers
    special_mappings = {
        "SSLNL": "SSLN", "SGLNL": "SGLN", "3GLD": "3GLD", "SGLN": "SGLN",
        "PHGP": "PHGP", "PHAU": "PHAU", "3LTS": "3LTS", "3USL": "3USL",
        "LLOY1": "LLOY", "VOD1": "VOD", "BARC1": "BARC", "TSCO1": "TSCO",
        "BPL1": "BP", "BPL": "BP", # BP.L
        "AZNL1": "AZN", "AZNL": "AZN", # Astrazeneca
        "SGLN1": "SGLN",
        "MAG5": "MAG5", "MAG5L": "MAG5",
        "MAG7": "MAG7", "MAG7L": "MAG7",
        "GLD3": "GLD3", 
        "3UKL": "3UKL", 
        "5QQQ": "5QQQ", 
        "TSL3": "TSL3", 
        "NVD3": "NVD3",
        "AVL": "AV",   # Aviva
        "UUL": "UU",   # United Utilities
        "BAL": "BA",   # BAE Systems (BA.L)
        "SLL": "SL",   # Standard Life / Segro? (Check context usually SL.L)
        "AU": "AUT",   # Auto Trader? Or Au (Gold)? Assuming AUT for AU.L usually.
        "REL": "REL",  # RELX (REL.L) - Keep as is
        "AAL": "AAL",  # Anglo American (AAL.L) - Keep as is
        "RBL": "RKT",  # Reckitt Benckiser
        "MICCL": "MICC", # Midwich Group (MICC.L)
    }
    
    if ticker in special_mappings:
        ticker = special_mappings[ticker]

    # 5. Suffix Protection for Leveraged Products & Extra 'L' Handling
    # Many UK tickers arrive with an extra 'L' (e.g., BARCL, SHELL, GSKL).
    # If len > 3 and ends in 'L', it's likely a suffix we should strip.
    is_leveraged_etp = ticker.endswith("1") and len(ticker) > 3
    
    # Check against common UK stock stems for "1" suffix
    if is_leveraged_etp:
        stems = ["LLOY", "BARC", "VOD", "HSBA", "TSCO", "BP", "AZN", "RR", "NG", "SGLN", "SSLN"]
        if any(ticker.startswith(stem) for stem in stems):
            ticker = ticker[:-1]
            
    # 6. Global Exchange Logic (UK vs US)
    us_exclusions = {
        # Tech & Growth
        "AAPL", "MSFT", "GOOG", "AMZN", "NVDA", "TSLA", "META", "NFLX",
        "AMD", "INTC", "PYPL", "ADBE", "CSCO", "PEP", "COST", "AVGO", "QCOM", "TXN",
        "ORCL", "CRM", "IBM", "UBER", "ABNB", "SNOW", "PLTR", "SQ", "SHOP", "SPOT",
        "GOOGL", # Explicitly exclude GOOGL

        # Financials
        "JPM", "BAC", "WFC", "C", "GS", "MS", "BLK", "AXP", "V", "MA", "COF", "USB",

        # Industrial & Auto
        "CAT", "DE", "GE", "GM", "F", "BA", "LMT", "RTX", "HON", "UPS", "FDX", "UNP", "MMM",

        # Consumer
        "WMT", "TGT", "HD", "LOW", "MCD", "SBUX", "NKE", "KO", "PEP", "PG", "CL", "MO", "PM", "DIS", "CMCSA",

        # Healthcare
        "JNJ", "PFE", "MRK", "ABBV", "LLY", "UNH", "CVS", "AMGN", "GILD", "BMY", "ISRG", "TMO", "ABT", "DHR",

        # Energy
        "XOM", "CVX", "COP", "SLB", "EOG", "OXY", "KMI", "HAL",

        # Telecom
        "T", "VZ", "TMUS",

        # ETFs
        "SPY", "QQQ", "DIA", "IWM", "IVV", "VOO", "VTI", "GLD", "SLV", "ARKK", "SMH", "XLF", "XLE", "XLK", "XLV",

        # Single Letter US Tickers
        "F", "T", "C", "V", "Z", "O", "D", "R", "K", "X", "S", "M", "A", "G", "U",

        # Popular Growth/Tech (4 letters)
        "SMCI", "RDDT", "ARM", "MSTR", "COIN", "PLTR", "SOFI", "AFRM", "HOOD",
        "DKNG", "RBLX", "PATH", "DDOG", "NET"
    }
    
    is_explicit_uk = "_EQ" in original and "_US" not in original
    is_explicit_us = "_US" in original
    is_likely_uk = (len(ticker) <= 5 or ticker.endswith("L")) and ticker not in us_exclusions and not is_explicit_us
    
    # Heuristic for stripping extra 'L' (e.g. BARCL -> BARC)
    # We apply this if it looks like a UK stock and satisfies length constraints.
    # Exclude typical 3-letter codes that are valid (like AAL, REL) unless mapped.
    if is_likely_uk and ticker.endswith("L") and len(ticker) > 3 and ticker not in us_exclusions:
        # Check if stripping L leaves a valid-looking numeric suffix or leveraged token?
        # Usually valid tickers are 3 or 4 chars. 
        # BARCL (5) -> BARC (4). OK.
        # SHELL (5) -> SHEL (4). OK.
        # GSKL (4) -> GSK (3). OK.
        # RELL (4) -> REL (3). OK.
        # Don't strip if it becomes too short (<2) or is in keep-list?
        # But we handle 3-letter via mappings mostly.
        # Safe heuristic: Strip L.
        ticker = ticker[:-1]

    # Leveraged ETPs (Granular detection)
    is_leveraged = bool(re.search(r'^(3|5|7)[A-Z]+', ticker)) or \
                    bool(re.search(r'[A-Z]+(2|3|5|7)$', ticker))
                    
    if is_explicit_uk or is_likely_uk or is_leveraged:
        # Ensure it doesn't already have .L (redundant check)
        if not ticker.endswith(".L") and "." not in ticker:
            return f"{ticker}.L"

    return ticker


class Cache:
    """Simple in-memory cache with TTL."""

    def __init__(self, ttl_seconds: int = 3600):
        self._cache: Dict[str, Any] = {}
        self._timestamps: Dict[str, float] = {}
        self.ttl_seconds = ttl_seconds

    def get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            if time.time() - self._timestamps[key] < self.ttl_seconds:
                return self._cache[key]
            else:
                del self._cache[key]
                del self._timestamps[key]
        return None

    def set(self, key: str, value: Any):
        self._cache[key] = value
        self._timestamps[key] = time.time()


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
        self.cache = Cache()

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

        data = await self._request("GET", "equity/metadata/exchanges")
        self.cache.set(cache_key, data)
        return data

    async def get_instruments(self) -> list:
        """Get list of all tradable instruments."""
        cache_key = "instruments"
        cached = self.cache.get(cache_key)
        if cached:
            return cached

        data = await self._request("GET", "equity/metadata/instruments")
        self.cache.set(cache_key, data)
        return data

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
    return json.dumps(data, separators=(",", ":"))


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
                "properties": {
                    "ticker": {"type": "string", "description": "Ticker symbol"}
                },
                "required": ["ticker"],
            },
        ),
        Tool(
            name="calculate_technical_indicators",
            description="Calculate technical indicators (SMA, RSI, MACD, Bollinger Bands) for a ticker",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "Ticker symbol"},
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
                "properties": {
                    "ticker": {"type": "string", "description": "Ticker symbol"}
                },
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
            # Get requested account type (default to active account)
            requested_account = arguments.get("account_type")
            if not requested_account:
                requested_account = active_account_type
            else:
                requested_account = requested_account.lower()

            # Determine which clients to query
            if requested_account == "all":
                all_clients = get_clients()
            elif requested_account in ["invest", "isa"]:
                client = clients.get(requested_account)
                if not client:
                    return [
                        TextContent(
                            type="text",
                            text=json.dumps(
                                {
                                    "error": f"No client available for {requested_account.upper()} account. Please configure API keys.",
                                    "requested_account": requested_account,
                                    "summary": {
                                        "total_positions": 0,
                                        "total_invested": 0.0,
                                        "current_value": 0.0,
                                        "total_pnl": 0.0,
                                        "total_pnl_percent": 0.0,
                                        "cash_balance": {"total": 0.0, "free": 0.0},
                                    },
                                    "positions": [],
                                }
                            ),
                        )
                    ]
                all_clients = {requested_account: client}
            else:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": f"Invalid account_type: {requested_account}. Must be 'invest', 'isa', or 'all'."
                            }
                        ),
                    )
                ]

            total_invested = 0
            total_current = 0
            total_pnl = 0
            total_cash = 0
            free_cash = 0
            all_positions = []
            account_summaries = {}

            # Fetch instruments for name and currency mapping
            instrument_metadata = {}
            try:
                # Use first available client to fetch instruments
                first_client = list(all_clients.values())[0] if all_clients else None
                if first_client:
                    instruments = await first_client.get_instruments()
                    for inst in instruments:
                        ticker = inst.get("ticker")
                        if ticker:
                            instrument_metadata[ticker] = {
                                "name": inst.get("name"),
                                "currency": inst.get("currencyCode"),
                            }
            except Exception as e:
                print(
                    f"Warning: Could not fetch instruments for metadata: {e}",
                    file=sys.stderr,
                )

            for acc_type, c_instance in all_clients.items():
                try:
                    positions, cash_info = await asyncio.gather(
                        c_instance.get_all_positions(), c_instance.get_account_cash()
                    )

                    # ✅ NORMALIZE ALL POSITIONS with proper currency handling
                    positions = normalize_all_positions(positions, instrument_metadata)

                    # Calculate account totals using GBP-normalized values
                    acc_invested = sum(
                        pos.get("averagePriceGBP", pos.get("averagePrice", 0))
                        * pos.get("quantity", 0)
                        for pos in positions
                    )
                    acc_current = calculate_portfolio_value(positions)
                    acc_pnl = sum(
                        pos.get("pplGBP", pos.get("ppl", 0)) for pos in positions
                    )

                    total_invested += acc_invested
                    total_current += acc_current
                    total_pnl += acc_pnl
                    total_cash += cash_info.get("total", 0)
                    free_cash += cash_info.get("free", 0)

                    # Tag positions with account type and enrich with names
                    for p in positions:
                        ticker = p.get("ticker")
                        p["account_type"] = acc_type
                        if ticker in instrument_metadata:
                            p["name"] = instrument_metadata[ticker]["name"]
                    all_positions.extend(positions)

                    account_summaries[acc_type] = {
                        "total_invested": round(acc_invested, 2),
                        "current_value": round(acc_current, 2),
                        "total_pnl": round(acc_pnl, 2),
                        "total_pnl_percent": round(
                            (acc_pnl / acc_invested) if acc_invested > 0 else 0, 4
                        ),
                        "cash_balance": cash_info,
                        "status": "success",
                    }
                except Exception as e:
                    print(f"Error fetching data for {acc_type}: {e}", file=sys.stderr)
                    account_summaries[acc_type] = {"status": "error", "error": str(e)}

            analysis = {
                "requested_account": requested_account,
                "active_account_type": active_account_type,
                "summary": {
                    "total_positions": len(all_positions),
                    "total_invested": round(total_invested, 2),
                    "current_value": round(total_current, 2),
                    "total_pnl": round(total_pnl, 2),
                    "total_pnl_percent": round(
                        (total_pnl / total_invested * 100) if total_invested > 0 else 0,
                        2,
                    ),
                    "net_deposits": round(total_current - total_pnl, 2),
                    "cash_balance": {
                        "total": round(total_cash, 2),
                        "free": round(free_cash, 2),
                    },
                    "accounts": account_summaries,
                },
                "positions": all_positions,
            }

            return [
                TextContent(
                    type="text", text=json.dumps(sanitize_nan(analysis), separators=(",", ":"))
                )
            ]

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
                                text=json.dumps(sanitize_nan(position), separators=(",", ":")),
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
            result = await c.place_market_order(
                ticker=arguments["ticker"],
                quantity=arguments["quantity"],
                order_type=arguments["order_type"],
            )
            return [
                TextContent(
                    type="text",
                    text=f"Market order placed successfully:\n{json.dumps(result, separators=(',', ':'))}",
                )
            ]

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
                        sanitize_nan(results[:20]), separators=(",", ":")
                    ),  # Limit to top 20 results
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
            instruments = await c.get_all_instruments()
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
                    type="text", text=json.dumps(sanitize_nan(metrics), separators=(",", ":"))
                )
            ]

        elif name == "get_all_pies":
            pies = await c.get_all_pies()
            return [
                TextContent(type="text", text=json.dumps(pies, separators=(",", ":")))
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
                )
            ]

        elif name == "get_price_history":
            ticker = normalize_ticker(arguments["ticker"])
            start_date = arguments.get("start_date")
            end_date = arguments.get("end_date")
            period = arguments.get("period", "3mo")
            interval = arguments.get("interval", "1d")

            # Run in executor to avoid blocking async loop since yfinance is sync
            loop = asyncio.get_running_loop()

            def fetch_history():
                stock = yf.Ticker(ticker)

                # Use custom date range if provided, otherwise use period
                if start_date:
                    # If end_date not provided, use today
                    end = end_date if end_date else datetime.now().strftime("%Y-%m-%d")
                    return stock.history(start=start_date, end=end, interval=interval)
                else:
                    return stock.history(period=period, interval=interval)

            hist = await loop.run_in_executor(None, fetch_history)

            if hist.empty:
                return [
                    TextContent(
                        type="text", text=f"No historical data found for {ticker}"
                    )
                ]

            # Calculate statistics
            hist_copy = hist.copy()
            hist_copy["Daily_Change"] = hist_copy["Close"].diff()
            hist_copy["Daily_Change_Pct"] = hist_copy["Close"].pct_change() * 100

            # Overall statistics
            start_price = hist_copy["Close"].iloc[0]
            end_price = hist_copy["Close"].iloc[-1]
            total_change = end_price - start_price
            total_change_pct = (total_change / start_price) * 100

            # Up vs Down days
            up_days = (hist_copy["Daily_Change"] > 0).sum()
            down_days = (hist_copy["Daily_Change"] < 0).sum()
            unchanged_days = (hist_copy["Daily_Change"] == 0).sum()

            # High/Low
            high_price = hist_copy["High"].max()
            low_price = hist_copy["Low"].min()
            high_date = hist_copy["High"].idxmax()
            low_date = hist_copy["Low"].idxmin()

            # Average daily change
            avg_daily_change = hist_copy["Daily_Change"].mean()
            avg_daily_change_pct = hist_copy["Daily_Change_Pct"].mean()

            # Volatility (standard deviation of daily returns)
            volatility = hist_copy["Daily_Change_Pct"].std()

            # Prepare data for output
            hist_copy = hist_copy.reset_index()
            hist_copy["Date"] = hist_copy["Date"].apply(
                lambda x: x.strftime("%Y-%m-%d") if hasattr(x, "strftime") else str(x)
            )

            # Round numerical values for cleaner output
            for col in [
                "Open",
                "High",
                "Low",
                "Close",
                "Daily_Change",
                "Daily_Change_Pct",
            ]:
                if col in hist_copy.columns:
                    hist_copy[col] = hist_copy[col].round(2)

            # Get key days data
            first_day = hist_copy.iloc[0]
            last_day = hist_copy.iloc[-1]

            # Limit daily data to first 5, last 5, and any significant days
            total_days = len(hist_copy)
            if total_days > 20:
                # Show first 5, last 5, plus high/low days
                first_5 = hist_copy.head(5)
                last_5 = hist_copy.tail(5)
                price_data_sample = pd.concat([first_5, last_5]).drop_duplicates()
                price_data_note = f"Showing first 5 and last 5 days. Full dataset has {total_days} days."
            else:
                price_data_sample = hist_copy
                price_data_note = f"Complete dataset with {total_days} days."

            result = {
                "ticker": ticker,
                "period": {
                    "start_date": first_day["Date"],
                    "end_date": last_day["Date"],
                    "total_trading_days": total_days,
                },
                "key_prices": {
                    "first_day": {
                        "date": first_day["Date"],
                        "open": round(first_day["Open"], 2),
                        "close": round(first_day["Close"], 2),
                    },
                    "last_day": {
                        "date": last_day["Date"],
                        "open": round(last_day["Open"], 2),
                        "close": round(last_day["Close"], 2),
                    },
                    "period_high": {
                        "price": round(high_price, 2),
                        "date": high_date.strftime("%Y-%m-%d")
                        if hasattr(high_date, "strftime")
                        else str(high_date),
                    },
                    "period_low": {
                        "price": round(low_price, 2),
                        "date": low_date.strftime("%Y-%m-%d")
                        if hasattr(low_date, "strftime")
                        else str(low_date),
                    },
                },
                "performance": {
                    "total_change": round(total_change, 2),
                    "total_change_percent": round(total_change_pct, 2),
                    "avg_daily_change": round(avg_daily_change, 2),
                    "avg_daily_change_percent": round(avg_daily_change_pct, 2),
                    "volatility_percent": round(volatility, 2),
                },
                "daily_movement": {
                    "up_days": int(up_days),
                    "down_days": int(down_days),
                    "unchanged_days": int(unchanged_days),
                    "up_down_ratio": round(up_days / down_days, 2)
                    if down_days > 0
                    else None,
                },
                "price_data_info": price_data_note,
                "price_data": price_data_sample[
                    [
                        "Date",
                        "Open",
                        "High",
                        "Low",
                        "Close",
                        "Volume",
                        "Daily_Change",
                        "Daily_Change_Pct",
                    ]
                ].to_dict(orient="records"),
            }

            return [
                TextContent(type="text", text=json.dumps(result, indent=2, default=str))
            ]

        elif name == "get_ticker_analysis":
            ticker = normalize_ticker(arguments["ticker"])

            loop = asyncio.get_running_loop()

            def fetch_info():
                stock = yf.Ticker(ticker)
                return stock.info

            info = await loop.run_in_executor(None, fetch_info)

            keys_to_keep = [
                "sector",
                "industry",
                "marketCap",
                "forwardPE",
                "trailingPE",
                "dividendYield",
                "fiftyTwoWeekHigh",
                "fiftyTwoWeekLow",
                "averageVolume",
                "currentPrice",
                "targetMeanPrice",
                "recommendationKey",
                "ebitda",
                "debtToEquity",
                "returnOnEquity",
                "freeCashflow",
                "beta",
                "shortName",
                "longName",
                "currency",
            ]

            filtered_info = {k: v for k, v in info.items() if k in keys_to_keep}

            return [TextContent(type="text", text=json.dumps(filtered_info, indent=2))]

        elif name == "calculate_technical_indicators":
            ticker = normalize_ticker(arguments["ticker"])
            period = arguments.get("period", "1y")
            interval = arguments.get("interval", "1d")

            loop = asyncio.get_running_loop()

            def calc_indicators():
                stock = yf.Ticker(ticker)
                df = stock.history(period=period, interval=interval)

                if df.empty:
                    return None

                # SMA
                df["SMA_50"] = df["Close"].rolling(window=50).mean()
                df["SMA_200"] = df["Close"].rolling(window=200).mean()

                # RSI
                delta = df["Close"].diff()
                gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                rs = gain / loss
                df["RSI"] = 100 - (100 / (1 + rs))

                # MACD
                exp1 = df["Close"].ewm(span=12, adjust=False).mean()
                exp2 = df["Close"].ewm(span=26, adjust=False).mean()
                df["MACD"] = exp1 - exp2
                df["Signal_Line"] = df["MACD"].ewm(span=9, adjust=False).mean()

                # Bollinger Bands
                df["BB_Middle"] = df["Close"].rolling(window=20).mean()
                std_dev = df["Close"].rolling(window=20).std()
                df["BB_Upper"] = df["BB_Middle"] + (std_dev * 2)
                df["BB_Lower"] = df["BB_Middle"] - (std_dev * 2)

                return df

            df = await loop.run_in_executor(None, calc_indicators)

            if df is None or df.empty:
                return [
                    TextContent(
                        type="text", text=f"No historical data found for {ticker}"
                    )
                ]

            latest = df.iloc[-10:].copy()
            latest = latest.reset_index()
            latest["Date"] = latest["Date"].apply(
                lambda x: x.isoformat() if hasattr(x, "isoformat") else str(x)
            )

            cols = [
                "Date",
                "Close",
                "Volume",
                "SMA_50",
                "SMA_200",
                "RSI",
                "MACD",
                "Signal_Line",
                "BB_Upper",
                "BB_Lower",
            ]
            cols = [c for c in cols if c in latest.columns]

            result = latest[cols].to_dict(orient="records")

            summary = {
                "ticker": ticker,
                "latest_indicators": result[-1],
                "recent_trend": result,
            }

            return [
                TextContent(
                    type="text", text=json.dumps(summary, indent=2, default=str)
                )
            ]

        elif name == "get_current_price":
            ticker = normalize_ticker(arguments.get("ticker"))
            price_data = {}
            source = "Trading 212"

            # Try Trading 212 via instrument search/metadata
            try:
                # We reuse get_instruments to find the ticker and its details including price if available?
                # Actually get_instruments usually returns metadata.
                # Use get_price_history(1d)? No that's history.
                # Use search_instruments?
                # T212 API doesn't have a direct "get price" for non-owned assets except in metadata if available.
                # But we can try to find it in the "instruments" list if we cached it, or just use YFinance as primary for "Market Data" if T212 is restricted.
                # However, let's assume we want to use YFinance as fallback or primary for price check of unowned assets.

                # Let's try YFinance directly as T212 API is limited for unowned realtime data without stream.
                source = "Yahoo Finance"

                loop = asyncio.get_running_loop()

                def fetch_price_sync(ticker_symbol):
                    stock = yf.Ticker(ticker_symbol)
                    # fast info
                    info = stock.fast_info
                    current_price = info.last_price
                    currency = info.currency
                    if current_price is None:
                        # Fallback to history
                        hist = stock.history(period="1d")
                        if not hist.empty:
                            current_price = hist["Close"].iloc[-1]
                    return current_price, currency

                current_price, currency = await loop.run_in_executor(None, fetch_price_sync, ticker)

                if current_price:
                    price_data = {
                        "ticker": ticker,
                        "price": current_price,
                        "currency": currency,
                        "source": source,
                    }
                else:
                    raise ValueError("Price not found")

            except Exception as e:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "error": f"Failed to fetch price for {ticker}: {e}",
                                "source": source,
                            }
                        ),
                    )
                ]

            return [TextContent(type="text", text=json.dumps(price_data, indent=2))]

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        return [TextContent(type="text", text=f"Error executing {name}: {str(e)}")]


# Global clients map
clients: Dict[str, Trading212Client] = {}
credentials: dict = {}
active_account_type: str = "invest"


def get_clients() -> Dict[str, Trading212Client]:
    """Get all initialized clients."""
    return {k: v for k, v in clients.items() if v is not None}


def get_active_client() -> Trading212Client:
    """Get the currently active client for trading operations."""
    global active_account_type
    c = clients.get(active_account_type)
    if not c:
        # Fallback to any available client
        available = get_clients()
        if not available:
            raise ValueError(
                "No Trading 212 clients initialized. Please set API credentials."
            )
        return list(available.values())[0]
    return c


async def main():
    """Main entry point for the MCP server."""
    global clients, credentials

    # Load environment variables from .env file
    load_dotenv()

    def get_env_var(name: str) -> Optional[str]:
        val = os.getenv(name)
        return val if val and val.strip() else None

    # Get API credentials from environment variables
    # Try specific keys first, fall back to generic key
    generic_key = get_env_var("TRADING212_API_KEY")
    invest_key = get_env_var("TRADING212_API_KEY_INVEST") or generic_key
    isa_key = get_env_var("TRADING212_API_KEY_ISA")

    generic_secret = get_env_var("TRADING212_API_SECRET")
    invest_secret = get_env_var("TRADING212_API_SECRET_INVEST") or generic_secret
    isa_secret = get_env_var("TRADING212_API_SECRET_ISA") or generic_secret

    use_demo = (get_env_var("TRADING212_USE_DEMO") or "false").lower() == "true"

    if not invest_key and not isa_key:
        print(
            "Warning: No Trading 212 API credentials found in environment. Please configure at runtime via 'switch_account' tool.",
            file=sys.stderr,
        )

    if not invest_secret and not isa_secret:
        print(
            "Warning: No Trading 212 API secrets found in environment.", file=sys.stderr
        )

    # Store credentials
    credentials = {
        "invest": {"key": invest_key, "secret": invest_secret},
        "isa": {"key": isa_key, "secret": isa_secret},
        "use_demo": use_demo,
    }

    print("Initializing Trading 212 accounts...", file=sys.stderr)
    
    # DEDUPLICATION FIX: If both keys are the same, only create one client
    # This prevents double-counting when querying "all" accounts
    if invest_key and isa_key and invest_key == isa_key:
        print("Note: Same API key for Invest and ISA. Using single client.", file=sys.stderr)
        clients["invest"] = Trading212Client(invest_key, invest_secret or "", use_demo)
        # Point ISA to the same client to avoid duplicate queries
        clients["isa"] = clients["invest"]
    else:
        if invest_key:
            clients["invest"] = Trading212Client(invest_key, invest_secret or "", use_demo)
        if isa_key:
            clients["isa"] = Trading212Client(isa_key, isa_secret or "", use_demo)

    global active_account_type
    active_account_type = (
        "invest" if "invest" in clients else ("isa" if "isa" in clients else "invest")
    )

    # Try to load state for active account
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                state = json.load(f)
                saved_type = state.get("account_type")
                if saved_type in credentials:
                    active_account_type = saved_type
    except Exception as e:
        print(f"Warning: Failed to load state: {e}", file=sys.stderr)

    print(f"Active account set to {active_account_type.upper()}", file=sys.stderr)

    # Run the server
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
