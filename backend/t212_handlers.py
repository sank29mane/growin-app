"""
Trading 212 Tool Handlers
Separated logic for handling MCP tool calls to reduce complexity.
"""

import asyncio
import json
import sys
from typing import Any, Dict, List
from datetime import datetime
import pandas as pd
import yfinance as yf
from mcp.types import TextContent

from utils import sanitize_nan
from utils.currency_utils import normalize_all_positions, calculate_portfolio_value
from utils.ticker_utils import normalize_ticker

# Type checking import
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    pass

async def handle_analyze_portfolio(
    arguments: Dict[str, Any],
    active_account_type: str,
    get_clients_func,
    clients_dict: Dict[str, Any]
) -> List[TextContent]:
    """Handle the analyze_portfolio tool call."""
    requested_account = arguments.get("account_type")
    if not requested_account:
        requested_account = active_account_type
    else:
        requested_account = requested_account.lower()

    # Determine which clients to query
    if requested_account == "all":
        all_clients = get_clients_func()
    elif requested_account in ["invest", "isa"]:
        client = clients_dict.get(requested_account)
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
        print(f"Warning: Could not fetch instruments for metadata: {e}", file=sys.stderr)

    # Define helper for parallel execution
    async def process_account(acc_type, c_instance):
        try:
            positions, cash_info = await asyncio.gather(
                c_instance.get_all_positions(), c_instance.get_account_cash()
            )

            # ✅ NORMALIZE POSITIONS
            position_objs = normalize_all_positions(positions, instrument_metadata)

            # Use objects for calculation before dumping (cleaner)
            acc_current = float(calculate_portfolio_value(position_objs))

            # Convert to dicts for JSON serialization and legacy key access downstream
            positions = [p.model_dump(by_alias=True) for p in position_objs]

            # Calculate account totals (using normalized values)
            acc_invested = sum(
                float(pos.get("averagePrice", 0)) * float(pos.get("quantity", 0))
                for pos in positions
            )

            # Market value is already calculated in Position object
            # acc_current = calculate_portfolio_value(positions) # Replaced above

            acc_pnl = sum(
                float(pos.get("unrealizedPnl", 0)) for pos in positions
            )

            # Tag positions
            for p in positions:
                ticker = p.get("ticker")
                p["account_type"] = acc_type
                if ticker in instrument_metadata:
                    p["name"] = instrument_metadata[ticker]["name"]

            return {
                "type": acc_type,
                "success": True,
                "positions": positions,
                "invested": acc_invested,
                "current": acc_current,
                "pnl": acc_pnl,
                "cash": cash_info,
            }
        except Exception as e:
            print(f"Error fetching data for {acc_type}: {e}", file=sys.stderr)
            return {
                "type": acc_type,
                "success": False,
                "error": str(e)
            }

    # execute in parallel
    tasks = [process_account(acc_type, client) for acc_type, client in all_clients.items()]
    results = await asyncio.gather(*tasks)

    # Aggregate results
    for res in results:
        acc_type = res["type"]
        if res["success"]:
            total_invested += res["invested"]
            total_current += res["current"]
            total_pnl += res["pnl"]
            total_cash += res["cash"].get("total", 0)
            free_cash += res["cash"].get("free", 0)
            all_positions.extend(res["positions"])

            account_summaries[acc_type] = {
                "total_invested": round(res["invested"], 2),
                "current_value": round(res["current"], 2),
                "total_pnl": round(res["pnl"], 2),
                "total_pnl_percent": round(
                    (res["pnl"] / res["invested"]) if res["invested"] > 0 else 0,
                    4
                ),
                "cash_balance": res["cash"],
                "status": "success",
            }
        else:
            account_summaries[acc_type] = {"status": "error", "error": res["error"]}

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
            type="text", text=json.dumps(sanitize_nan(analysis), default=str, separators=( ",", ":"))
        )
    ]

async def handle_market_order(
    arguments: Dict[str, Any],
    client: Any # Trading212Client
) -> List[TextContent]:
    """Handle place_market_order with price validation."""
    # --- Price Validation ---
    try:
        from price_validation import PriceValidator
        validation = await PriceValidator.validate_trade_price(arguments["ticker"])
        if validation["action"] == "block":
                return [
                TextContent(
                    type="text",
                    text=json.dumps({
                        "error": "Trade blocked due to price variance > 3%",
                        "details": validation["message"],
                        "recommended_price": validation.get("recommended_price")
                    })
                )
                ]
    except Exception as e:
        print(f"Warning: Price validation skipped/failed: {e}", file=sys.stderr)
    # ------------------------

    result = await client.place_market_order(
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

async def handle_get_price_history(
    arguments: Dict[str, Any]
) -> List[TextContent]:
    """Handle get_price_history using yfinance."""
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

async def handle_get_current_price(arguments: Dict[str, Any]) -> List[TextContent]:
    ticker = normalize_ticker(arguments.get("ticker"))
    price_data = {}
    source = "Trading 212"

    try:
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
