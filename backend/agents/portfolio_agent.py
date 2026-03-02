"""
Portfolio Agent - Fetches portfolio data via MCP
"""

from typing import Dict, Any, Optional
import logging
import json

from .base_agent import BaseAgent, AgentConfig, AgentResponse
from market_context import PortfolioData
from app_context import state # Access global state
from utils.ticker_utils import normalize_ticker
from cache_manager import cache # Import global cache
import asyncio
import pandas as pd
import numpy as np
import yfinance as yf
from decimal import Decimal
from utils.financial_math import create_decimal
from error_resilience import CircuitBreaker

logger = logging.getLogger(__name__)


class PortfolioAgent(BaseAgent):
    """
    Agent for fetching and analyzing portfolio data.
    Uses Trading 212 MCP server.
    """
    
    def __init__(self, config: AgentConfig = None):
        if config is None:
            config = AgentConfig(
                name="PortfolioAgent",
                enabled=True,
                timeout=10.0,
                cache_ttl=60 # Cache for 1 min
            )
        super().__init__(config)
        # Use global MCP client
        self.mcp_client = state.mcp_client
        
        self.circuit_breaker = CircuitBreaker("mcp_portfolio", failure_threshold=3, recovery_timeout=30)

    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Fetch portfolio data.
        
        Context should include:
        - account_type: "invest" (default), "isa", or "all"
        """
        # Get requested account type (default to invest if not specified)
        # The Coordinator should pass this from the initial request
        requested_account = context.get("account_type", "invest")
        self.logger.info(f"Analyzing portfolio for account type: {requested_account}")
        
        try:
            from status_manager import status_manager
            status_manager.set_status("portfolio_agent", "running", f"Syncing {requested_account} holdings...")
            
            # Use a local timeout to prevent hanging.
            if not self.circuit_breaker.can_proceed():
                raise ValueError("MCP tool call failed or circuit breaker is OPEN")

            try:
                # Fallback to wait_for for compatibility with < Python 3.11
                if hasattr(asyncio, 'timeout'):
                    async with asyncio.timeout(15.0):
                        result = await self.mcp_client.call_tool(
                            "analyze_portfolio",
                            arguments={"account_type": requested_account}
                        )
                else:
                    result = await asyncio.wait_for(
                        self.mcp_client.call_tool(
                            "analyze_portfolio",
                            arguments={"account_type": requested_account}
                        ),
                        timeout=15.0
                    )
                self.circuit_breaker.record_success()
            except asyncio.TimeoutError:
                self.circuit_breaker.record_failure()
                raise ValueError("MCP tool call timed out")
            except Exception as e:
                self.circuit_breaker.record_failure()
                # Pass through the error string logic from earlier or standard RPC errors
                if "Unauthorized" in str(e) or "401" in str(e):
                     raise ValueError(f"Unauthorized: {e}")
                raise ValueError(f"MCP tool call failed: {e}")
            
            # The result is a list of TextContent objects
            # We assume the first one contains the JSON data
            if not result or not hasattr(result, 'content') or not result.content:
                 raise ValueError("Empty response from MCP tool")

            # Check if content is empty or the first item lacks 'text'
            first_content = result.content[0]
            if not hasattr(first_content, 'text') or not first_content.text:
                 raise ValueError("Invalid or empty text content from MCP tool")

            content = first_content.text
            try:
                data = json.loads(content)
            except json.JSONDecodeError as decode_err:
                self.logger.error(f"Failed to parse portfolio JSON. Raw content: {content[:200]}")
                raise ValueError(f"Invalid JSON response from portfolio tool: {decode_err}")
            
            # Check for error in tool response
            if "error" in data:
                self.logger.warning(f"Portfolio tool returned error: {data['error']}")
                return AgentResponse(
                    agent_name=self.config.name,
                    success=False, 
                    data=data,
                    error=data["error"],
                    latency_ms=0
                )

            # Check for partial errors in account summaries (e.g. 429 on all accounts)
            summary = data.get("summary", {})
            accounts = summary.get("accounts", {})
            if accounts:
                failed_accounts = [k for k, v in accounts.items() if v.get("status") == "error"]
                # If we tried to fetch accounts and ALL of them failed
                if len(failed_accounts) == len(accounts) and len(accounts) > 0:
                     first_error = accounts[failed_accounts[0]].get("error", "Unknown error")
                     self.logger.error(f"Portfolio analysis failed for all accounts: {first_error}")
                     return AgentResponse(
                        agent_name=self.config.name,
                        success=False,
                        data=data,
                        error=f"All accounts failed: {first_error}",
                        latency_ms=0
                     )
            
            # Process and validate data structure
            portfolio_data = self._process_portfolio_data(data)

            # --- CACHE UPDATE: Store as "current_portfolio" ---
            try:
                # Cache the structured PortfolioData object (or its dict representation)
                # We use a long TTL (e.g., 1 hour) because we will iteratively update it
                cache.set("current_portfolio", portfolio_data, ttl=3600)
                self.logger.info("PortfolioAgent: Updated 'current_portfolio' in global cache")
            except Exception as e:
                self.logger.warning(f"PortfolioAgent: Failed to update cache: {e}")
            # --------------------------------------------------
            
            # Fetch history for context (default 30 days) - TIMEOUT PROTECTED
            try:
                # Wrap history fetching in 5s timeout to prevent blocking core data delivery
                # History is "nice to have", positions/value are critical.
                async with asyncio.timeout(5.0):
                   history = await self._fetch_portfolio_history(portfolio_data)
                   portfolio_data.portfolio_history = history
            except asyncio.TimeoutError:
                self.logger.warning("Portfolio history fetch timed out (5s). Skipping history, returning core data.")
                # We do NOT fail the request; we just yield data without history
            except Exception as e:
                self.logger.warning(f"Failed to fetch portfolio history: {e}")

            # --- RAG INTEGRATION: Store Portfolio Snapshot ---
            try:
                from app_context import state
                from datetime import datetime
                if state.rag_manager:
                    # Create a qualitative snapshot
                    total_val = portfolio_data.total_value
                    pnl_pct = portfolio_data.pnl_percent * 100
                    top_pos = sorted(portfolio_data.positions, key=lambda x: x.get('currentValue', 0), reverse=True)[:3]
                    top_tickers = [p.get('ticker') for p in top_pos]
                    
                    snapshot_text = (
                        f"Portfolio Snapshot ({requested_account}): Value £{total_val:.2f}, "
                        f"Performance {pnl_pct:+.2f}%. "
                        f"Top Holdings: {', '.join(top_tickers)}. "
                        f"Cash: £{portfolio_data.cash_balance.get('free', 0):.2f}"
                    )
                    
                    state.rag_manager.add_document(
                        content=snapshot_text,
                        metadata={
                            "type": "portfolio_snapshot",
                            "account": requested_account,
                            "date": datetime.now().strftime("%Y-%m-%d"),
                            "timestamp": asyncio.get_event_loop().time()
                        }
                    )
                    self.logger.info("PortfolioAgent: Stored snapshot in RAG")
            except Exception as e:
                self.logger.warning(f"PortfolioAgent: Failed to store RAG snapshot: {e}")
            # -------------------------------------------------
            
            from status_manager import status_manager
            status_manager.set_status("portfolio_agent", "ready", f"Value: £{portfolio_data.total_value:,.0f}")
            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=portfolio_data.model_dump(),
                latency_ms=0
            )
            
        except Exception as e:
            self.logger.error(f"Portfolio analysis failed: {e}")
            error_msg = str(e)
            
            # Enrich error message for better UI handling
            if "401" in error_msg or "Unauthorized" in error_msg:
                error_msg = "Invalid API Credential"
            elif "Connection refused" in error_msg or "ClientConnectorError" in error_msg:
                error_msg = "Connection Refused (Is MCP running?)"
            
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error=error_msg,
                latency_ms=0
            )

    def get_cached_portfolio(self) -> Optional[PortfolioData]:
        """Retrieve the currently cached portfolio data."""
        return cache.get("current_portfolio")

    def update_local_portfolio(self, ticker: str, quantity: Any, price: Any, side: str):
        """
        Iteratively update the cached portfolio after a trade.
        This provides immediate feedback without waiting for a full T212 sync.
        """
        p_data = self.get_cached_portfolio()
        if not p_data:
            return

        try:
            # 1. Update Cash
            qty_dec = create_decimal(quantity)
            price_dec = create_decimal(price)
            cost = qty_dec * price_dec
            
            # Ensure we get Decimal from cash_balance
            current_cash = create_decimal(p_data.cash_balance.get("free", Decimal('0')))
            
            if side.upper() == "BUY":
                new_cash = current_cash - cost
                p_data.total_invested = create_decimal(p_data.total_invested) + cost
            else: # SELL
                new_cash = current_cash + cost
                p_data.total_invested = create_decimal(p_data.total_invested) - cost 

            p_data.cash_balance["free"] = new_cash
            p_data.cash_balance["total"] = new_cash # Simplified

            # 2. Update Positions
            ticker = normalize_ticker(ticker)
            found = False
            for pos in p_data.positions:
                if normalize_ticker(pos.get("ticker", "")) == ticker:
                    current_qty = create_decimal(pos.get("quantity", Decimal('0')))
                    new_qty = current_qty + qty_dec if side.upper() == "BUY" else current_qty - qty_dec
                    
                    pos["quantity"] = new_qty
                    pos["currentPrice"] = price_dec
                    pos["value"] = new_qty * price_dec
                    found = True
                    break
            
            if not found and side.upper() == "BUY":
                # Add new position
                new_pos = {
                    "ticker": ticker,
                    "quantity": qty_dec,
                    "averagePrice": price_dec,
                    "currentPrice": price_dec,
                    "ppl": Decimal('0'),
                    "fxPpl": Decimal('0'),
                    "initialFillDate": asyncio.get_event_loop().time(),
                    "frontend": "MANUAL_TRADE",
                    "maxBuy": qty_dec,
                    "maxSell": qty_dec,
                    "pieQuantity": Decimal('0')
                }
                p_data.positions.append(new_pos)

            # 3. Update Totals
            total_val = sum(create_decimal(p.get("quantity", Decimal('0'))) * create_decimal(p.get("currentPrice", Decimal('0'))) for p in p_data.positions)
            p_data.total_value = total_val + new_cash
            
            # Save back to cache
            cache.set("current_portfolio", p_data, ttl=3600)
            self.logger.info(f"PortfolioAgent: Iteratively updated portfolio for {side} {quantity} {ticker}")

        except Exception as e:
            self.logger.error(f"Failed to iteratively update portfolio: {e}")

    def _process_portfolio_data(self, data: Dict[str, Any]) -> PortfolioData:
        """Convert raw dictionary to PortfolioData object"""
        
        # Determine where the summary and accounts data live
        summary = data.get("summary", data)
        
        # Accounts might be at top level (old schema?) or inside summary (new schema)
        accounts = data.get("accounts")
        if not accounts and isinstance(summary, dict):
            accounts = summary.get("accounts")
            
        total_invested = create_decimal(summary.get("total_invested", Decimal('0')))
        pnl_percent = float(summary.get("total_pnl_percent", 0.0))
        
        # SAFETY CHECK: If invested amount is negligible, PnL% is noise. Reset to 0.
        if total_invested < Decimal('1.0'):
            pnl_percent = 0.0
            
        return PortfolioData(
            total_positions=summary.get("total_positions", 0),
            total_invested=total_invested,
            total_value=create_decimal(summary.get("current_value", Decimal('0'))),
            total_pnl=create_decimal(summary.get("total_pnl", Decimal('0'))),
            pnl_percent=pnl_percent,
            net_deposits=create_decimal(summary.get("net_deposits", Decimal('0'))),
            cash_balance={
                "total": create_decimal(summary.get("cash_balance", {}).get("total", Decimal('0'))),
                "free": create_decimal(summary.get("cash_balance", {}).get("free", Decimal('0')))
            },
            accounts=accounts,
            positions=data.get("positions", [])
        )

    async def _fetch_portfolio_history(self, p_data: PortfolioData, days: int = 30) -> list:
        """Synthetic history generator using consolidated holdings."""
        positions = p_data.positions
        free_cash = float(p_data.cash_balance.get("free", 0.0))
        
        if not positions:
            return []

        # Map ticker to total quantity across all accounts
        holdings = {}
        for p in positions:
            t = normalize_ticker(p.get("ticker", ""))
            q = float(p.get("quantity", 0))
            if t and q > 0:
                holdings[t] = holdings.get(t, 0.0) + q
        
        if not holdings:
            return []

        tickers = list(holdings.keys())
        period_map = {1: "1d", 7: "5d", 30: "1mo", 90: "3mo", 365: "1y"}
        period = period_map.get(days, "1mo")
        
        try:
            loop = asyncio.get_running_loop()
            tickers_str = " ".join(tickers)
            
            # Check cache first
            cache_key = f"portfolio_history_{tickers_str}_{period}"
            cached_data = cache.get(cache_key)
            
            if cached_data is not None:
                self.logger.info(f"Cache hit for portfolio history: {cache_key}")
                df = cached_data
            else:
                self.logger.info(f"Cache miss for portfolio history. Fetching from yfinance: {tickers_str}")
                # Download historical prices
                df = await loop.run_in_executor(None, lambda: yf.download(tickers_str, period=period, progress=False)['Close'])
                
                if not df.empty:
                    # Cache the result (TTL 1 hour)
                    cache.set(cache_key, df, ttl=3600)
            
            if df.empty:
                return []
            
            df = df.ffill().bfill()
            
            # Vectorized implementation
            if isinstance(df, pd.Series):
                ticker = list(holdings.keys())[0]
                df = df.to_frame(name=ticker)

            valid_holdings = {k: v for k, v in holdings.items() if k in df.columns}

            if not valid_holdings:
                 # Return free cash for all timestamps if no valid holdings match
                 portfolio_val = pd.Series(free_cash, index=df.index)
            else:
                quantities = pd.Series(valid_holdings)

                # Align df to quantities and ensure we work on a copy
                df_subset = df[quantities.index].copy()

                # Apply Pence conversion
                # Find columns ending with .L
                uk_tickers = [t for t in df_subset.columns if t.endswith(".L")]

                for t in uk_tickers:
                    try:
                        # Ensure numeric comparison - yfinance can return strings
                        series = pd.to_numeric(df_subset[t], errors='coerce')
                        mask = series > 500
                        # Apply transformation only where mask is true using numpy.where
                        df_subset[t] = np.where(mask, series / 100.0, series)
                    except Exception as e:
                        self.logger.warning(f"Currency conversion failed for {t}: {e}")

                # Calculate weighted sum
                portfolio_val = df_subset.dot(quantities)
                portfolio_val = portfolio_val + free_cash

            # Handle Nan/Inf
            portfolio_val = portfolio_val.replace([np.inf, -np.inf], np.nan).fillna(0.0)

            # Create result list
            history_points = [
                {
                    "timestamp": ts.isoformat(),
                    "total_value": round(float(v), 2)
                }
                for ts, v in zip(portfolio_val.index, portfolio_val.values)
            ]
            
            return history_points
        except Exception as e:
            self.logger.error(f"Error fetching synthetic history: {e}")
            return []
