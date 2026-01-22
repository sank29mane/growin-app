"""
Portfolio Agent - Fetches portfolio data via MCP
"""

from typing import Dict, Any
import logging
import json

from base_agent import BaseAgent, AgentConfig, AgentResponse
from market_context import PortfolioData
from mcp_client import Trading212MCPClient
from app_context import state # Access global state
from trading212_mcp_server import normalize_ticker
import asyncio
import pandas as pd
import yfinance as yf

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
            # Call MCP tool
            # The MCP server's analyze_portfolio tool expects "account_type" argument
            result = await self.mcp_client.call_tool(
                "analyze_portfolio", 
                arguments={"account_type": requested_account}
            )
            
            # The result is a list of TextContent objects
            # We assume the first one contains the JSON data
            if not result.content:
                 raise ValueError("Empty response from MCP tool")

            content = result.content[0].text
            data = json.loads(content)
            
            # Check for error in tool response
            if "error" in data:
                self.logger.warning(f"Portfolio tool returned error: {data['error']}")
                # If valid error (e.g. no ISA account), return specific error structure
                # This allows the LLM to know it failed but gracefully handling it
                return AgentResponse(
                    agent_name=self.config.name,
                    success=False, # Mark as failed so Decision Agent knows
                    data=data,  # Pass result anyway as it might have partial data
                    error=data["error"],
                    latency_ms=0
                )
            
            # Process and validate data structure
            portfolio_data = self._process_portfolio_data(data)
            
            # Fetch history for context (default 30 days)
            try:
                history = await self._fetch_portfolio_history(portfolio_data)
                portfolio_data.portfolio_history = history
            except Exception as e:
                self.logger.warning(f"Failed to fetch portfolio history: {e}")
            
            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=portfolio_data.model_dump(),
                latency_ms=0
            )
            
        except Exception as e:
            self.logger.error(f"Portfolio analysis failed: {e}")
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error=str(e),
                latency_ms=0
            )

    def _process_portfolio_data(self, data: Dict[str, Any]) -> PortfolioData:
        """Convert raw dictionary to PortfolioData object"""
        
        # Determine where the summary and accounts data live
        summary = data.get("summary", data)
        
        # Accounts might be at top level (old schema?) or inside summary (new schema)
        accounts = data.get("accounts")
        if not accounts and isinstance(summary, dict):
            accounts = summary.get("accounts")
            
        return PortfolioData(
            total_positions=summary.get("total_positions", 0),
            total_invested=summary.get("total_invested", 0.0),
            total_value=summary.get("current_value", 0.0), # Schema mapping
            total_pnl=summary.get("total_pnl", 0.0),
            pnl_percent=summary.get("total_pnl_percent", 0.0),
            cash_balance=summary.get("cash_balance", {"total": 0.0, "free": 0.0}),
            accounts=accounts,
            positions=data.get("positions", [])
        )

    async def _fetch_portfolio_history(self, p_data: PortfolioData, days: int = 30) -> list:
        """Synthetic history generator using current holdings and yfinance"""
        positions = p_data.positions
        free_cash = p_data.cash_balance.get("free", 0.0) if isinstance(p_data.cash_balance, dict) else 0.0
        
        if not positions:
            return []

        # Map ticker to quantity
        holdings = {}
        for p in positions:
            t = normalize_ticker(p.get("ticker", ""))
            q = p.get("quantity", 0)
            if t and q > 0:
                holdings[t] = holdings.get(t, 0) + q
        
        if not holdings:
            return []

        tickers = list(holdings.keys())
        period_map = {1: "1d", 7: "5d", 30: "1mo", 90: "3mo", 365: "1y"}
        period = period_map.get(days, "1mo")
        
        try:
            loop = asyncio.get_running_loop()
            tickers_str = " ".join(tickers)
            
            # Download historical prices
            df = await loop.run_in_executor(None, lambda: yf.download(tickers_str, period=period, progress=False)['Close'])
            
            if df.empty:
                return []
            
            df = df.ffill().bfill()
            history_points = []
            
            for timestamp, row in df.iterrows():
                try:
                    total_value = free_cash
                    for ticker, quantity in holdings.items():
                        price = 0
                        if isinstance(df, pd.Series):
                            price = row
                        elif ticker in row:
                            price = row[ticker]
                        else:
                            continue
                        
                        # Pence to pounds conversion for UK stocks
                        if ticker.endswith(".L") and price > 500:
                            price = price / 100.0
                            
                        total_value += price * quantity
                    
                    import math
                    if math.isnan(total_value) or math.isinf(total_value):
                        total_value = 0.0

                    history_points.append({
                        "timestamp": timestamp.isoformat(),
                        "total_value": round(float(total_value), 2)
                    })
                except Exception:
                    continue
            
            return history_points
        except Exception as e:
            self.logger.error(f"Error fetching synthetic history: {e}")
            return []
