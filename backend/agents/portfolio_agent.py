"""
Portfolio Agent - Fetches portfolio data via MCP
"""

from typing import Dict, Any, Optional, List
import logging
import json

import asyncio
from utils.async_utils import run_with_timeout
import pandas as pd
import numpy as np
import yfinance as yf
from decimal import Decimal
from pydantic import BaseModel, Field
from magentic import prompt as mag_prompt

from .base_agent import BaseAgent, AgentConfig, AgentResponse
from market_context import PortfolioData
from app_context import state # Access global state
from utils.ticker_utils import normalize_ticker
from cache_manager import cache # Import global cache
from utils.financial_math import create_decimal
from utils.portfolio_analyzer import PortfolioAnalyzer
from resilience import get_circuit_breaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)

class PortfolioAnalysis(BaseModel):
    """Structured qualitative analysis of portfolio health."""
    diversification_score: float = Field(..., ge=0, le=10, description="Score from 0 (Concentrated) to 10 (Ideal)")
    risk_concentration: List[str] = Field(default_factory=list, description="List of sectors or tickers with high risk")
    rebalance_needed: bool = Field(..., description="Whether a rebalance is recommended")
    summary_insight: str = Field(..., description="Key qualitative insight for the user")

@mag_prompt(
    "Perform a qualitative analysis of the current portfolio status.\n"
    "Portfolio Summary: {summary}\n"
    "Top Holdings: {holdings}\n"
    "Analyze diversification and potential risks, then return a structured PortfolioAnalysis."
)
def analyze_portfolio_quality(summary: str, holdings: str) -> PortfolioAnalysis:
    ...

# Module-level circuit breaker to persist state across agent instantiations (SOTA 2026 Resilience API)
portfolio_circuit_breaker = get_circuit_breaker("portfolio", failure_threshold=3, recovery_timeout=30.0)

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
        self.circuit_breaker = portfolio_circuit_breaker
        
    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Fetch portfolio data.
        
        Context should include:
        - account_type: "invest" (default), "isa", or "all"
        """
        requested_account = context.get("account_type", "invest")
        self.logger.info(f"Analyzing portfolio for account type: {requested_account}")
        
        try:
            from status_manager import status_manager
            status_manager.set_status("portfolio_agent", "running", f"Syncing {requested_account} holdings...")

            # SOTA 2026: Consolidated Multi-Account Fetch with Enhanced Timeouts and Circuit Breaker
            if requested_account == "all":
                # Parallel fetch for ISA and Invest
                async def fetch_all():
                    tasks = [
                        self.mcp_client.call_tool("analyze_portfolio", arguments={"account_type": "invest"}),
                        self.mcp_client.call_tool("analyze_portfolio", arguments={"account_type": "isa"})
                    ]
                    res = await run_with_timeout(asyncio.gather(*tasks, return_exceptions=True), 15.0)

                    # Ensure CircuitBreaker records failure if both fetches fail
                    any_success = any(not isinstance(r, Exception) for r in res)
                    if not any_success and res:
                        raise ValueError(f"All account fetches failed: {res[0]}")

                    return res
                        
                try:
                    results = await self.circuit_breaker.call(fetch_all)
                            
                except asyncio.TimeoutError as e:
                    raise ValueError("Multi-account fetch timed out") from e
                except CircuitBreakerOpenException:
                    logger.error("Portfolio sync skipped: circuit breaker is OPEN")
                    return AgentResponse(
                        agent_name=self.config.name,
                        success=False,
                        data={},
                        error="MCP tool call failed or circuit breaker is OPEN",
                        latency_ms=0
                    )
                except Exception as e:
                    raise ValueError(f"Multi-account fetch failed: {e}") from e
                
                raw_data_list = []
                for res in results:
                    if isinstance(res, Exception):
                        self.logger.error(f"Account fetch failed: {res}")
                        continue
                    if res and hasattr(res, 'content') and res.content:
                        raw_data_list.append(json.loads(res.content[0].text))
                
                if not raw_data_list:
                    raise ValueError("Failed to fetch data from any accounts")
                
                # Consolidate raw data
                consolidated_data = self._consolidate_accounts(raw_data_list)
                portfolio_data = self._process_portfolio_data(consolidated_data)
            else:
                # Single account fetch with timeout
                async def fetch_single():
                    return await run_with_timeout(
                        self.mcp_client.call_tool(
                            "analyze_portfolio",
                            arguments={"account_type": requested_account}
                        ),
                        15.0
                    )

                try:
                    result = await self.circuit_breaker.call(fetch_single)
                except asyncio.TimeoutError as e:
                    raise ValueError(f"Portfolio fetch for {requested_account} timed out") from e
                except CircuitBreakerOpenException:
                    logger.error("Portfolio sync skipped: circuit breaker is OPEN")
                    return AgentResponse(
                        agent_name=self.config.name,
                        success=False,
                        data={},
                        error="MCP tool call failed or circuit breaker is OPEN",
                        latency_ms=0
                    )
                except Exception as e:
                    if "Unauthorized" in str(e) or "401" in str(e):
                        raise ValueError(f"Unauthorized: {e}") from e
                    raise ValueError(f"Portfolio fetch failed: {e}") from e

                if not result or not hasattr(result, 'content') or not result.content:
                    raise ValueError("Empty response from MCP tool")

                data = json.loads(result.content[0].text)

                if "error" in data:
                    raise ValueError(f"Portfolio tool returned error: {data['error']}")

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
                history = await run_with_timeout(self._fetch_portfolio_history(portfolio_data), 5.0)
                portfolio_data.portfolio_history = history
            except asyncio.TimeoutError:
                self.logger.warning("Portfolio history fetch timed out (5s). Skipping history, returning core data.")
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

    def _consolidate_accounts(self, data_list: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Merge multiple account data dictionaries into a single consolidated view."""
        consolidated = {
            "summary": {
                "total_positions": 0,
                "total_invested": 0.0,
                "current_value": 0.0,
                "total_pnl": 0.0,
                "total_pnl_percent": 0.0,
                "net_deposits": 0.0,
                "cash_balance": {"total": 0.0, "free": 0.0},
                "accounts": {}
            },
            "positions": []
        }
        
        pos_map = {} # Ticker -> Position
        
        for data in data_list:
            acc_summary = data.get("summary", {})
            acc_name = acc_summary.get("account_type", "unknown")
            
            # Update summary totals
            consolidated["summary"]["total_invested"] += float(acc_summary.get("total_invested", 0))
            consolidated["summary"]["current_value"] += float(acc_summary.get("current_value", 0))
            consolidated["summary"]["total_pnl"] += float(acc_summary.get("total_pnl", 0))
            consolidated["summary"]["net_deposits"] += float(acc_summary.get("net_deposits", 0))
            consolidated["summary"]["cash_balance"]["total"] += float(acc_summary.get("cash_balance", {}).get("total", 0))
            consolidated["summary"]["cash_balance"]["free"] += float(acc_summary.get("cash_balance", {}).get("free", 0))
            
            # Record individual account status
            consolidated["summary"]["accounts"][acc_name] = acc_summary
            
            # Merge positions
            for pos in data.get("positions", []):
                ticker = pos.get("ticker")
                if ticker in pos_map:
                    # Merge existing position
                    existing = pos_map[ticker]
                    # Update quantity and calculate new weighted average price
                    q1 = float(existing.get("quantity", 0))
                    p1 = float(existing.get("averagePrice", 0))
                    q2 = float(pos.get("quantity", 0))
                    p2 = float(pos.get("averagePrice", 0))
                    
                    new_q = q1 + q2
                    if new_q > 0:
                        new_avg = ((q1 * p1) + (q2 * p2)) / new_q
                        existing["averagePrice"] = new_avg
                        existing["quantity"] = new_q
                        existing["value"] = float(existing.get("value", 0)) + float(pos.get("value", 0))
                        existing["ppl"] = float(existing.get("ppl", 0)) + float(pos.get("ppl", 0))
                else:
                    pos_map[ticker] = pos.copy()
        
        consolidated["positions"] = list(pos_map.values())
        consolidated["summary"]["total_positions"] = len(consolidated["positions"])
        
        # Calculate overall PnL percent
        if consolidated["summary"]["total_invested"] > 0:
            consolidated["summary"]["total_pnl_percent"] = (consolidated["summary"]["total_pnl"] / consolidated["summary"]["total_invested"])
            
        return consolidated

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
        """Synthetic history generator using consolidated holdings via PortfolioAnalyzer."""
        positions = p_data.positions
        free_cash = float(p_data.cash_balance.get("free", 0.0))
        
        if not positions:
            return []

        # Map ticker to total quantity
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
            
            # 1. Fetch historical prices
            cache_key = f"market_prices_{tickers_str}_{period}"
            cached_prices = cache.get(cache_key)
            
            if cached_prices:
                market_prices_df = cached_prices
            else:
                market_prices_df = await loop.run_in_executor(None, lambda: yf.download(tickers_str, period=period, progress=False)['Close'])
                if not market_prices_df.empty:
                    cache.set(cache_key, market_prices_df, ttl=3600)

            if market_prices_df.empty:
                return []

            # 2. Format for PortfolioAnalyzer
            # market_data dict: {ticker: [{'t': timestamp, 'c': price}, ...]}
            market_data = {}
            if isinstance(market_prices_df, pd.Series):
                # Single ticker case
                t = tickers[0]
                market_data[t] = [{"t": ts.value // 10**6, "c": val} for ts, val in market_prices_df.items()]
            else:
                for t in tickers:
                    if t in market_prices_df.columns:
                        market_data[t] = [{"t": ts.value // 10**6, "c": val} for ts, val in market_prices_df[t].items()]

            # 3. Generate Backcast History
            analyzer = PortfolioAnalyzer()
            backcast_positions = [{"ticker": t, "qty": q} for t, q in holdings.items()]
            history_df = analyzer.generate_backcast_history(backcast_positions, market_data)
            
            if history_df.empty:
                return []
                
            # Add free cash
            history_df["total_value"] += free_cash

            # 4. Format for response
            history_points = [
                {
                    "timestamp": ts.isoformat(),
                    "total_value": round(float(v), 2)
                }
                for ts, v in zip(history_df.index, history_df["total_value"].values)
            ]
            
            return history_points
        except Exception as e:
            self.logger.error(f"Error fetching synthetic history via analyzer: {e}")
            return []
