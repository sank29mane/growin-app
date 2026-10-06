#!/usr/bin/env python3
"""
Trading 212 MCP Server
A read-only Model Context Protocol server for Trading 212 account data.

It exposes reads only. It has no order, cancel, pie-mutation or account-switch
tool, and its HTTP client can send nothing but GET (66-CONTEXT D-05, D-10, D-14).
Orders reach Trading 212 only through the typed execution boundary.
"""

import aiofiles
import asyncio
import base64
import json
import logging
import os
import sys
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import aiofiles
import httpx
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Resource, TextContent, Tool
from utils import sanitize_nan
from utils.process_guard import start_parent_watchdog
from shared_types import (
    SENSITIVE_TOOLS,
    Trading212EnvironmentError,
    require_trading212_environment,
)
from brokers.trading212.governor import Governor, endpoint_template

# Start watchdog immediately to ensure cleanup if parent dies
start_parent_watchdog()

# Constants
LIVE_API_BASE = "https://live.trading212.com/api/v0"
DEMO_API_BASE = "https://demo.trading212.com/api/v0"
STATE_FILE = ".state.json"
PRACTICE_CREDENTIAL_PREFIX = "TRADING212_PRACTICE_"

logger = logging.getLogger(__name__)


async def _load_state(filepath: str) -> Optional[Dict[str, Any]]:
    """Loads state from file asynchronously."""
    try:
        if os.path.exists(filepath):
            async with aiofiles.open(filepath, "r") as f:
                content = await f.read()
                return json.loads(content)
    except Exception:
        pass
    return None


# Import centralized currency normalization
from utils.currency_utils import normalize_all_positions
from utils.ticker_utils import normalize_ticker
from t212_handlers import (
    handle_analyze_portfolio,
    handle_get_price_history,
    handle_get_current_price,
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
        self._lock = None
        self._load_from_disk()

    def _load_from_disk(self):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(asyncio.to_thread(self._sync_load_from_disk))
        except RuntimeError:
            self._sync_load_from_disk()

    def _sync_load_from_disk(self):
        if os.path.exists(self.filename):
            try:
                with open(self.filename, "r") as f:
                    data = json.load(f)
                    self._cache = data.get("cache", {})
                    self._timestamps = data.get("timestamps", {})

                now = time.time()
                expired = [
                    k for k, ts in self._timestamps.items() if now - ts > self.ttl_seconds
                ]
                for k in expired:
                    del self._cache[k]
                    del self._timestamps[k]
                if expired:
                    tmp_filename = self.filename + ".tmp"
                    with open(tmp_filename, "w") as f:
                        json.dump(
                            {"cache": self._cache, "timestamps": self._timestamps}, f
                        )
                    os.replace(tmp_filename, self.filename)
            except Exception as e:
                print(
                    f"Warning: Failed to load cache from {self.filename}: {e}",
                    file=sys.stderr,
                )

    async def _save_to_disk(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            try:
                tmp_filename = self.filename + ".tmp"
                async with aiofiles.open(tmp_filename, "w") as f:
                    data = json.dumps(
                        {"cache": self._cache, "timestamps": self._timestamps}
                    )
                    await f.write(data)
                os.replace(tmp_filename, self.filename)
            except Exception as e:
                print(
                    f"Warning: Failed to save cache to {self.filename}: {e}",
                    file=sys.stderr,
                )

    async def _cleanup_expired(self):
        now = time.time()
        expired = [
            k for k, ts in self._timestamps.items() if now - ts > self.ttl_seconds
        ]
        for k in expired:
            del self._cache[k]
            del self._timestamps[k]
        if expired:
            await self._save_to_disk()

    def get(self, key: str) -> Optional[Any]:
        value, is_expired = self.get_with_expiry_status(key)
        if not is_expired:
            return value
        return None

    def get_with_expiry_status(self, key: str) -> tuple[Optional[Any], bool]:
        if key in self._cache:
            is_expired = time.time() - self._timestamps[key] > self.ttl_seconds
            return self._cache[key], is_expired
        return None, True

    async def set(self, key: str, value: Any, custom_ttl: Optional[int] = None):
        self._cache[key] = value
        self._timestamps[key] = time.time()
        await self._save_to_disk()


READ_METHOD = "GET"


async def _refuse_non_get_before_sending(request: httpx.Request) -> None:
    """httpx request hook: nothing but GET leaves this process, on any host.

    The live host gets the same rule as the demo host: it is read through this
    client and never written (operator rule for 66-02). Because the hook sits on
    the client itself, it also refuses a call that bypasses ``_request``.
    """

    if request.method != READ_METHOD:
        raise PermissionError(
            "Trading 212 broker mutation blocked by read-only transport: "
            f"{request.method} {request.url.host}"
        )


_MISSING = object()


def _present(source: Dict[str, Any], *path: str) -> Any:
    """Return source[path...] or the ``_MISSING`` marker; never a default value."""

    node: Any = source
    for step in path:
        if not isinstance(node, dict) or step not in node or node[step] is None:
            return _MISSING
        node = node[step]
    return node


def _legacy(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    """Build a legacy-shaped dict; a key with no v0 source is omitted, never 0 (D-10d)."""

    return {key: value for key, value in pairs if value is not _MISSING}


def normalize_account_info(summary: Dict[str, Any]) -> Dict[str, Any]:
    """v0 ``equity/account/summary`` to the keys the old ``account/info`` carried."""

    return _legacy(
        [
            ("id", _present(summary, "id")),
            ("currencyCode", _present(summary, "currency")),
        ]
    )


def normalize_account_cash(summary: Dict[str, Any]) -> Dict[str, Any]:
    """v0 ``equity/account/summary`` to the keys the old ``account/cash`` carried."""

    return _legacy(
        [
            ("free", _present(summary, "cash", "availableToTrade")),
            ("total", _present(summary, "totalValue")),
            ("invested", _present(summary, "investments", "totalCost")),
            ("ppl", _present(summary, "investments", "unrealizedProfitLoss")),
            ("result", _present(summary, "investments", "realizedProfitLoss")),
            ("pieCash", _present(summary, "cash", "inPies")),
            ("blocked", _present(summary, "cash", "reservedForOrders")),
        ]
    )


def normalize_position(position: Dict[str, Any]) -> Dict[str, Any]:
    """v0 ``equity/positions`` item to the keys the old ``equity/portfolio`` item carried.

    ``maxBuy`` has no v0 source and is never emitted.
    """

    return _legacy(
        [
            ("ticker", _present(position, "instrument", "ticker")),
            ("quantity", _present(position, "quantity")),
            ("averagePrice", _present(position, "averagePricePaid")),
            ("currentPrice", _present(position, "currentPrice")),
            ("ppl", _present(position, "walletImpact", "unrealizedProfitLoss")),
            ("fxPpl", _present(position, "walletImpact", "fxImpact")),
            ("initialFillDate", _present(position, "createdAt")),
            ("pieQuantity", _present(position, "quantityInPies")),
            ("maxSell", _present(position, "quantityAvailableForTrading")),
            ("currency", _present(position, "instrument", "currency")),
        ]
    )


class Trading212Client:
    """Read-only client for Trading 212 account data.

    It has no method that writes, and ``_request`` refuses every method but GET
    before any network use. Every read acquires its endpoint's governor slot.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        use_demo: bool,
        *,
        governor: Optional[Governor] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        if not api_key or not api_secret:
            raise ValueError(
                "Trading 212 needs both an API key and an API secret (HTTP Basic); "
                "no client was built."
            )
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = DEMO_API_BASE if use_demo else LIVE_API_BASE
        self.governor = governor if governor is not None else Governor()

        credentials = f"{api_key}:{api_secret}"
        encoded_credentials = base64.b64encode(credentials.encode("utf-8")).decode(
            "utf-8"
        )
        self.auth_header = f"Basic {encoded_credentials}"

        self.client = httpx.AsyncClient(
            headers={
                "Authorization": self.auth_header,
                "Content-Type": "application/json",
            },
            timeout=30.0,
            follow_redirects=False,
            transport=transport,
            event_hooks={"request": [_refuse_non_get_before_sending]},
        )
        self.cache = FileCache(ttl_seconds=86400)

    async def close(self):
        await self.client.aclose()

    async def _request(self, method: str, endpoint: str, **kwargs) -> Any:
        """GET ``endpoint`` once, or once more after a single 429 (D-10a, D-14).

        A timeout, a connection error and any 4xx or 5xx other than a first 429
        raise at once with no further attempt.
        """

        if method.upper() != READ_METHOD:
            raise PermissionError(
                "Trading 212 broker mutation blocked by read-only transport"
            )
        if any(name in kwargs for name in ("json", "data", "content", "files")):
            raise PermissionError("Trading 212 reads carry no request body")

        url = f"{self.base_url}/{endpoint}"
        for attempt in (0, 1):
            key = await self.governor.acquire(READ_METHOD, endpoint)
            response = await self.client.request(READ_METHOD, url, **kwargs)
            self.governor.observe(key, response.headers)
            if response.status_code == 429 and attempt == 0:
                wait = self.governor.hold_after_throttle(key, response.headers)
                logger.warning(
                    "T212 API 429 on %s: waiting %.1fs for the rate limit reset, then one retry",
                    endpoint_template(READ_METHOD, endpoint),
                    wait,
                )
                continue
            response.raise_for_status()
            if response.content:
                return response.json()
            return {}

    async def get_account_summary(self) -> dict:
        return await self._request("GET", "equity/account/summary")

    async def get_account_info(self) -> dict:
        return normalize_account_info(await self.get_account_summary())

    async def get_account_cash(self) -> dict:
        return normalize_account_cash(await self.get_account_summary())

    async def get_all_positions(self) -> list:
        positions = await self._request("GET", "equity/positions")
        return [normalize_position(item) for item in positions]

    async def get_position_by_ticker(self, ticker: str) -> dict:
        positions = await self._request(
            "GET", f"equity/positions?{urlencode({'ticker': ticker})}"
        )
        return normalize_position(positions[0]) if positions else {}

    async def get_all_orders(self) -> list:
        return await self._request("GET", "equity/orders")

    async def get_order_by_id(self, order_id: str) -> dict:
        return await self._request("GET", f"equity/orders/{order_id}")

    async def get_historical_orders(
        self, cursor: Optional[int] = None, limit: int = 50
    ) -> dict:
        params = {"limit": min(limit, 50)}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", f"equity/history/orders?{urlencode(params)}")

    async def get_dividends(
        self, cursor: Optional[int] = None, limit: int = 50
    ) -> dict:
        params = {"limit": min(limit, 50)}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET", f"equity/history/dividends?{urlencode(params)}"
        )

    async def get_transactions(
        self, cursor: Optional[int] = None, limit: int = 50
    ) -> dict:
        params = {"limit": min(limit, 50)}
        if cursor:
            params["cursor"] = cursor
        return await self._request(
            "GET", f"equity/history/transactions?{urlencode(params)}"
        )

    async def get_instruments(self) -> list:
        cache_key = "instruments"
        cached = self.cache.get(cache_key)
        if cached:
            return cached
        try:
            data = await self._request("GET", "equity/metadata/instruments")
            await self.cache.set(cache_key, data)
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
            await self.cache.set(cache_key, data)
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


app = Server("trading212-mcp-server")


@app.list_resources()
async def list_resources() -> list[Resource]:
    return [
        Resource(
            uri="trading212://account/info",
            name="Account Info",
            mimeType="application/json",
        ),
        Resource(
            uri="trading212://account/cash",
            name="Account Cash",
            mimeType="application/json",
        ),
        Resource(
            uri="trading212://portfolio/positions",
            name="Portfolio Positions",
            mimeType="application/json",
        ),
        Resource(
            uri="trading212://orders/pending",
            name="Pending Orders",
            mimeType="application/json",
        ),
        Resource(
            uri="trading212://instruments/all",
            name="All Instruments",
            mimeType="application/json",
        ),
        Resource(
            uri="trading212://exchanges/all",
            name="All Exchanges",
            mimeType="application/json",
        ),
        Resource(
            uri="trading212://pies/all",
            name="Investment Pies",
            mimeType="application/json",
        ),
        Resource(
            uri="trading212://history/orders",
            name="Historical Orders",
            mimeType="application/json",
        ),
        Resource(
            uri="trading212://history/dividends",
            name="Dividend History",
            mimeType="application/json",
        ),
        Resource(
            uri="trading212://history/transactions",
            name="Transaction History",
            mimeType="application/json",
        ),
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
    return json.dumps(data, separators=(",", ":"))


@app.list_tools()
async def list_tools() -> list[Tool]:
    tools = [
        Tool(
            name="analyze_portfolio",
            description="Analyze portfolio",
            inputSchema={
                "type": "object",
                "properties": {
                    "account_type": {"type": "string", "enum": ["invest", "isa", "all"]}
                },
            },
        ),
        Tool(
            name="get_position_details",
            description="Get position details",
            inputSchema={
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        ),
        Tool(
            name="search_instruments",
            description="Search instruments",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        Tool(
            name="get_historical_performance",
            description="Get performance",
            inputSchema={"type": "object", "properties": {"limit": {"type": "number"}}},
        ),
        Tool(
            name="calculate_portfolio_metrics",
            description="Calculate portfolio metrics",
            inputSchema={"type": "object"},
        ),
        Tool(
            name="get_all_pies",
            description="Get all pies",
            inputSchema={"type": "object"},
        ),
        Tool(
            name="get_pie_details",
            description="Get pie details",
            inputSchema={
                "type": "object",
                "properties": {"pie_id": {"type": "number"}},
                "required": ["pie_id"],
            },
        ),
        Tool(
            name="get_price_history",
            description="Get price history",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "start_date": {"type": "string"},
                    "end_date": {"type": "string"},
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
                    },
                },
                "required": ["ticker"],
            },
        ),
        Tool(
            name="get_ticker_analysis",
            description="Get ticker analysis",
            inputSchema={
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        ),
        Tool(
            name="calculate_technical_indicators",
            description="Calculate technical indicators",
            inputSchema={
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "period": {
                        "type": "string",
                        "enum": ["3mo", "6mo", "1y", "2y", "5y"],
                    },
                    "interval": {"type": "string", "enum": ["1d", "1wk", "1mo"]},
                },
                "required": ["ticker"],
            },
        ),
        Tool(
            name="get_current_price",
            description="Get current price",
            inputSchema={
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        ),
    ]
    return tools


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> list[TextContent]:
    # No mutation tool exists in this server. A name from the sensitive list is
    # refused outright, whatever the environment says (defence in depth, D-05).
    if name in SENSITIVE_TOOLS:
        raise PermissionError(f"Trading 212 mutation tool blocked in read-only mode: {name}")

    c = get_active_client()

    try:
        if name == "analyze_portfolio":
            return await handle_analyze_portfolio(
                arguments, active_account_type, get_clients, clients
            )

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
                        return [
                            TextContent(
                                type="text",
                                text=json.dumps(
                                    sanitize_nan(position), separators=(",", ":")
                                ),
                            )
                        ]
                except Exception:
                    continue
            return [TextContent(type="text", text=f"Position {ticker} not found.")]

        elif name == "search_instruments":
            query = arguments["query"].upper()
            instruments = await c.get_instruments()
            results = [
                inst
                for inst in instruments
                if query in inst.get("ticker", "").upper()
                or query in inst.get("name", "").upper()
            ]
            return [
                TextContent(
                    type="text",
                    text=json.dumps(sanitize_nan(results[:20]), separators=(",", ":")),
                )
            ]

        elif name == "get_historical_performance":
            limit = arguments.get("limit", 50)
            history = await c.get_historical_orders(limit=min(limit, 50))
            return [
                TextContent(
                    type="text", text=json.dumps(sanitize_nan(history), indent=2)
                )
            ]

        elif name == "calculate_portfolio_metrics":
            positions, cash = await asyncio.gather(
                c.get_all_positions(), c.get_account_cash()
            )
            instruments = await c.get_instruments()
            metadata_cache = {i.get("ticker"): i for i in instruments}
            positions = normalize_all_positions(positions, metadata_cache)
            total_value = sum(
                pos.get("currentPrice", 0) * pos.get("quantity", 0) for pos in positions
            )
            total_cost = sum(
                pos.get("averagePrice", 0) * pos.get("quantity", 0) for pos in positions
            )
            total_pnl = sum(pos.get("ppl", 0) for pos in positions)
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
                    type="text",
                    text=json.dumps(sanitize_nan(metrics), separators=(",", ":")),
                )
            ]

        elif name == "get_all_pies":
            pies = await c.get_all_pies()
            return [
                TextContent(type="text", text=json.dumps(pies, separators=(",", ":")))
            ]

        elif name == "get_pie_details":
            pie = await c.get_pie(arguments["pie_id"])
            return [TextContent(type="text", text=json.dumps(pie, indent=2))]

        elif name == "get_ticker_analysis":
            ticker = normalize_ticker(arguments["ticker"])
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, lambda: yf.Ticker(ticker).info)
            keys = [
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
            filtered = {k: v for k, v in info.items() if k in keys}
            return [TextContent(type="text", text=json.dumps(filtered, indent=2))]

        elif name == "calculate_technical_indicators":
            ticker = normalize_ticker(arguments["ticker"])
            period, interval = (
                arguments.get("period", "1y"),
                arguments.get("interval", "1d"),
            )
            loop = asyncio.get_running_loop()
            df = await loop.run_in_executor(
                None,
                lambda: _compute_indicators(
                    yf.Ticker(ticker).history(period=period, interval=interval)
                ),
            )
            if df is None or df.empty:
                return [TextContent(type="text", text=f"No data for {ticker}")]
            latest = df.iloc[-10:].copy().reset_index()
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

        else:
            raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        return [
            TextContent(
                type="text", text=json.dumps({"error": str(e), "success": False})
            )
        ]


clients: Dict[str, Trading212Client] = {}
active_account_type: str = "invest"
startup_error: Optional[str] = None


def get_clients() -> Dict[str, Trading212Client]:
    return {k: v for k, v in clients.items() if v is not None}


def get_active_client() -> Trading212Client:
    global active_account_type
    c = clients.get(active_account_type)
    if not c:
        available = get_clients()
        if not available:
            raise ValueError(startup_error or "No Trading 212 clients initialized.")
        return list(available.values())[0]
    return c


def drop_practice_credentials(environ: Dict[str, str]) -> list[str]:
    """Remove ``TRADING212_PRACTICE_*`` from ``environ`` and return the names removed.

    The practice account belongs to the execution adapter alone (D-07). This
    server must not hold its keys even if a ``.env`` file loaded here names them.
    """

    names = [n for n in list(environ) if n.upper().startswith(PRACTICE_CREDENTIAL_PREFIX)]
    for name in names:
        del environ[name]
    return names


def build_clients(
    environ: Dict[str, str],
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> Dict[str, Trading212Client]:
    """Build read-only clients from the environment, or raise.

    Raises ``Trading212EnvironmentError`` unless TRADING212_USE_DEMO is exactly
    ``true`` or ``false`` (no default, D-10b). An account whose key has no
    secret gets no client: HTTP Basic needs both and there is no bare-key
    fallback.
    """

    use_demo = require_trading212_environment(environ) == "demo"

    def get_env_var(name: str) -> Optional[str]:
        val = environ.get(name)
        return val if val and val.strip() else None

    generic_key = get_env_var("TRADING212_API_KEY")
    invest_key = get_env_var("TRADING212_API_KEY_INVEST") or generic_key
    isa_key = get_env_var("TRADING212_API_KEY_ISA")

    generic_secret = get_env_var("TRADING212_API_SECRET")
    invest_secret = get_env_var("TRADING212_API_SECRET_INVEST") or generic_secret
    isa_secret = get_env_var("TRADING212_API_SECRET_ISA") or generic_secret

    built: Dict[str, Trading212Client] = {}

    def build(account: str, key: Optional[str], secret: Optional[str]):
        if not key:
            return None
        if not secret:
            logger.error(
                "Trading 212 %s account: key is set without a secret; no client built",
                account.upper(),
            )
            return None
        return Trading212Client(key, secret, use_demo, transport=transport)

    if invest_key and isa_key and invest_key == isa_key:
        shared = build("invest", invest_key, invest_secret)
        if shared:
            built["invest"] = shared
            built["isa"] = shared
    else:
        invest_client = build("invest", invest_key, invest_secret)
        if invest_client:
            built["invest"] = invest_client
        isa_client = build("isa", isa_key, isa_secret)
        if isa_client:
            built["isa"] = isa_client
    return built


async def main():
    global clients, active_account_type, startup_error
    load_dotenv()
    drop_practice_credentials(os.environ)

    try:
        clients = build_clients(os.environ)
        startup_error = None
        environment = require_trading212_environment(os.environ)
        if environment == "demo":
            print("Trading 212: Using DEMO environment (reads only).", file=sys.stderr)
        else:
            print(
                "Trading 212: Using LIVE environment (GET reads only).", file=sys.stderr
            )
    except Trading212EnvironmentError as error:
        clients = {}
        startup_error = str(error)
        print(f"Trading 212: no client built. {error}", file=sys.stderr)

    active_account_type = (
        "invest" if "invest" in clients else ("isa" if "isa" in clients else "invest")
    )

    state_data = await _load_state(STATE_FILE)
    if state_data:
        saved_type = state_data.get("account_type")
        if saved_type in ("invest", "isa"):
            active_account_type = saved_type

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
