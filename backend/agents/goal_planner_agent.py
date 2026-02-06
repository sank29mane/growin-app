"""
Goal Planner Agent - Portfolio Optimization & Feasibility Analysis
Calculates optimal asset allocations based on user goals using Sharpe ratio optimization.
"""

import numpy as np
from scipy.optimize import minimize
from typing import Dict, Any, List, Optional
import logging
import time
from .base_agent import BaseAgent, AgentConfig, AgentResponse
from market_context import GoalData
# Use lazy import for app_context to avoid circular deps if needed, or import here if safe
# form app_context import state (doing inside method to be safe as per original)

logger = logging.getLogger(__name__)

# Constants
RISK_FREE_RATE = 0.04
TRADING_DAYS = 252

class GoalPlannerAgent(BaseAgent):
    """
    Goal-Based Investment Planner Agent.
    Calculates optimal asset allocation and feasibility scores based on 
    target returns, duration, and risk profile.
    
    Phases:
    2A: Foundation (Optimizer + Monte Carlo with Mock data)
    2B: T212 Integration (Real instrument querying)
    """
    
    def __init__(self, config: Optional[AgentConfig] = None):
        if not config:
            config = AgentConfig(name="GoalPlannerAgent")
        super().__init__(config)
        
        # Risk profiles with target volatilities and expected return premiums
        self.risk_profiles = {
            "LOW": {
                "max_volatility": 0.07,
                "expected_return_premium": 0.02, # Spread over risk-free
                "preferred_sectors": ["Bonds", "Consumer Staples", "Utilities"]
            },
            "MEDIUM": {
                "max_volatility": 0.14,
                "expected_return_premium": 0.06,
                "preferred_sectors": ["Technology", "Healthcare", "S&P 500"]
            },
            "HIGH": {
                "max_volatility": 0.25,
                "expected_return_premium": 0.12,
                "preferred_sectors": ["Small Cap", "Crypto", "Emerging Markets"]
            },
            "AGGRESSIVE_PLUS": {
                "max_volatility": 0.45,  # Much higher tolerance
                "expected_return_premium": 0.20,
                "preferred_sectors": ["Tech", "Crypto", "Growth", "Momentum"],
                "strategy": "MOMENTUM_SORT"
            }
        }
        
        # Baseline Assets for Phase 2A (Mock data based on historical averages)
        # In Phase 2B, these will be replaced by real T212 instruments
        self.asset_universe = {
            "VTI": {"name": "Total Stock Market ETF", "return": 0.10, "vol": 0.18, "sector": "Stock", "momentum_score": 0.6},
            "BND": {"name": "Total Bond Market ETF", "return": 0.04, "vol": 0.06, "sector": "Bond", "momentum_score": 0.1},
            "VOO": {"name": "Vanguard S&P 500 ETF", "return": 0.11, "vol": 0.17, "sector": "Stock", "momentum_score": 0.7},
            "QQQ": {"name": "Invesco QQQ Trust", "return": 0.15, "vol": 0.22, "sector": "Tech", "momentum_score": 0.9},
            "VIG": {"name": "Dividend Appreciation ETF", "return": 0.09, "vol": 0.13, "sector": "Stock", "momentum_score": 0.4},
            "VXUS": {"name": "Total International Stock ETF", "return": 0.07, "vol": 0.19, "sector": "Stock", "momentum_score": 0.3},
            "GLD": {"name": "SPDR Gold Shares", "return": 0.06, "vol": 0.15, "sector": "Commodity", "momentum_score": 0.5},
            "BITO": {"name": "Bitcoin Strategy ETF", "return": 0.25, "vol": 0.60, "sector": "Crypto", "momentum_score": 0.95},
            "TQQQ": {"name": "ProShares UltraPro QQQ (3x)", "return": 0.40, "vol": 0.65, "sector": "Tech", "momentum_score": 0.92},
            "ARKK": {"name": "ARK Innovation ETF", "return": 0.25, "vol": 0.45, "sector": "Tech", "momentum_score": 0.85}
        }
        
    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Calculates a goal-based investment plan using REAL MARKET DATA via MCP.
        Context needs:
        - initial_capital: float
        - target_returns_percent: float
        - duration_years: float
        - risk_profile: str (LOW, MEDIUM, HIGH, AGGRESSIVE_PLUS)
        """
        start_time = time.time()
        
        try:
            # 1. Parse Inputs with defaults
            initial_capital = float(context.get("initial_capital", 1000.0))
            target_returns = float(context.get("target_returns_percent", 10.0)) / 100.0
            duration = float(context.get("duration_years", 3.0))
            risk_input = str(context.get("risk_profile", "MEDIUM")).upper()
            
            # Allow custom mapping or direct match
            risk_map = {
                "AGGRESSIVE+": "AGGRESSIVE_PLUS",
                "EXPRESSIVE": "AGGRESSIVE_PLUS",
                "GROWTH+": "AGGRESSIVE_PLUS"
            }
            risk_profile = risk_map.get(risk_input, risk_input)
            
            if risk_profile not in self.risk_profiles:
                risk_profile = "MEDIUM"
            
            logger.info(f"GoalPlannerAgent analysis for profile: {risk_profile}")

            # 2. Fetch Real Data (Phase 2B)
            # Try to fetch real metrics from T212/YFinance via MCP
            # Fallback to mock data if MCP is unavailable or fails
            real_universe = await self._fetch_real_data(list(self.asset_universe.keys()))
            
            # Use real data if available, otherwise mock
            universe_to_use = real_universe if real_universe else self.asset_universe
            if not real_universe:
                 logger.warning("Using MOCK data (MCP fetch failed or empty)")
            else:
                 logger.info(f"Using REAL data for {len(real_universe)} assets")
                
            # 3. Strategy Selection & Asset Filtering
            weights = {}
            expected_ret = 0.0
            expected_vol = 0.0
            sharpe = 0.0
            rebalance_info = {}
            
            if risk_profile == "AGGRESSIVE_PLUS":
                # --- MOMENTUM SORT STRATEGY ---
                # 100% Equity, Sorted by Momentum Score (Relative Strength), Weekly Rebalancing modeled
                
                # Filter for High Growth/Momentum Assets only (Exclude Bonds, Defenders)
                # Note: volatility check uses the 'vol' from new data
                candidates = [
                    k for k, v in universe_to_use.items() 
                    if v.get("sector") in ["Tech", "Crypto", "Stock"] and v.get("vol", 0) > 0.15
                ]
                
                # Sort by Momentum Score (simulating Relative Strength)
                # In real generic implementation, this would use 1-month / 3-month returns
                sorted_candidates = sorted(
                    candidates, 
                    key=lambda x: universe_to_use[x].get("momentum_score", 0), 
                    reverse=True
                )
                
                # Top 4 Assets get allocated (Concentrated portfolio)
                top_picks = sorted_candidates[:4]
                
                if not top_picks:
                     logger.warning("No suitable momentum candidates found!")
                     top_picks = candidates[:4] # Fallback
                
                # Allocation: Weighted by Momentum Score (simulating Kelly-like sizing)
                total_score = sum(universe_to_use[x].get("momentum_score", 0) for x in top_picks)
                weights_list = []
                
                if total_score == 0:
                    # Equal weight fallback
                    for x in top_picks:
                        w = 1.0 / len(top_picks) if len(top_picks) > 0 else 0
                        weights[x] = w
                        weights_list.append(w)
                else:
                    for x in top_picks:
                        w = universe_to_use[x]["momentum_score"] / total_score
                        weights[x] = w
                        weights_list.append(w)
                
                # Calculate Portfolio Metrics
                # Expected Return is weighted sum of asset returns
                # Volatility accounts for concentration risk (less diversification benefit)
                p_ret = sum(weights[x] * universe_to_use[x]["return"] for x in top_picks)
                
                # Simplified Volatility for Momentum (High due to correlation)
                # Assuming 0.7 average correlation for high momentum assets
                avg_vol = np.mean([universe_to_use[x]["vol"] for x in top_picks]) if top_picks else 0
                p_vol = avg_vol * 0.9 # Slight diversification benefit
                
                expected_ret = p_ret
                expected_vol = p_vol
                sharpe = (expected_ret - RISK_FREE_RATE) / (expected_vol + 1e-9)
                
                # Add rebalancing metadata - TRADING 212 PIE
                rebalance_info = {
                    "frequency": "WEEKLY",
                    "trigger": "Ranking Change",
                    "method": "Relative Strength Sort",
                    "action": "Update Pie Weights"
                }

            else:
                # --- STANDARD MPT OPTIMIZATION ---
                pool = list(universe_to_use.keys())
                if risk_profile == "LOW":
                    # Remove high vol assets
                    pool = [k for k in pool if universe_to_use[k]["vol"] < 0.20]
                
                # Extract matrices
                returns = np.array([universe_to_use[a]["return"] for a in pool])
                vols = np.array([universe_to_use[a]["vol"] for a in pool])
                
                # Simple Correlation Matrix
                # NOTE: This is a simplification ("Mock Correlation"). 
                # In a full-scale Prod env, we should fetch covariance matrices from a risk model (like Barra) or calculate from historical series.
                n = len(pool)
                corr_matrix = np.full((n, n), 0.4) 
                np.fill_diagonal(corr_matrix, 1.0)
                
                # Add specific correlations
                stock_indices = [i for i, a in enumerate(pool) if universe_to_use[a]["sector"] == "Stock"]
                bond_indices = [i for i, a in enumerate(pool) if universe_to_use[a]["sector"] == "Bond"]
                for s in stock_indices:
                    for b in bond_indices:
                        corr_matrix[s, b] = corr_matrix[b, s] = 0.1
                
                cov_matrix = np.outer(vols, vols) * corr_matrix
                
                # Optimize
                optimization_res = self._optimize_portfolio(returns, cov_matrix, target_returns, risk_profile)
                weights_arr = optimization_res["weights"]
                expected_ret = optimization_res["expected_return"]
                expected_vol = optimization_res["expected_volatility"]
                sharpe = optimization_res["sharpe_ratio"]
                
                # Convert array to dict
                for i, ticker in enumerate(pool):
                    if weights_arr[i] > 0.01:
                        weights[ticker] = float(weights_arr[i])
                        
                rebalance_info = {
                    "frequency": "QUARTERLY", 
                    "method": "Calendar", 
                    "action": "Rebalance Pie"
                }

            # 4. Monte Carlo Simulation
            mc_result = self._monte_carlo_simulation(
                initial_capital, expected_ret, expected_vol, duration, target_returns
            )
            
            # 5. Generate Growth Path for Charting
            growth_path = []
            for y in range(int(duration) + 1):
                # Expected value at year y: S0 * (1 + mu)^y
                # Target value at year y: S0 * (1 + target_mu)^y
                expected_v = initial_capital * (1 + expected_ret)**y
                target_v = initial_capital * (1 + target_returns)**y
                growth_path.append({
                    "year": float(y),
                    "value": float(round(expected_v, 2)),
                    "target": float(round(target_v, 2))
                })

            # 6. Format Output
            optimal_weights_formatted = {
                k: float(round(v, 3)) for k, v in weights.items()
            }
            
            # 7. Construct Response
            goal_data = GoalData(
                target_returns_percent=target_returns * 100.0,
                duration_years=duration,
                initial_capital=initial_capital,
                risk_profile=risk_profile,
                feasibility_score=float(mc_result["probability"]),
                optimal_weights=optimal_weights_formatted,
                expected_annual_return=float(expected_ret),
                expected_volatility=float(expected_vol),
                sharpe_ratio=float(sharpe),
                simulated_final_value_avg=float(mc_result["avg_final_value"]),
                probability_of_success=float(mc_result["probability"]),
                simulated_growth_path=growth_path,
                suggested_instruments=[
                    {
                        "ticker": t,
                        "name": universe_to_use[t]["name"],
                        "weight": optimal_weights_formatted[t],
                        "expected_return": universe_to_use[t]["return"],
                        "category": "Momentum" if risk_profile == "AGGRESSIVE_PLUS" else universe_to_use[t]["sector"]
                    } for t in optimal_weights_formatted
                ]
            )
            
            # Inject extra strategy details for AGGRESSIVE_PLUS & PIE INFO
            result_data = goal_data.model_dump()
            result_data["rebalancing_strategy"] = rebalance_info
            
            # Add Pie Implementation Plan
            result_data["implementation"] = {
                 "type": "TRADING212_PIE",
                 "name": f"{risk_profile} Goal Portfolio",
                 "icon": "Growth" if risk_profile in ["HIGH", "AGGRESSIVE_PLUS"] else "Safe",
                 "action": "CREATE_OR_UPDATE"
            }
            
            latency = (time.time() - start_time) * 1000
            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=result_data,
                latency_ms=latency
            )
            
        except Exception as e:
            logger.error(f"GoalPlannerAgent failed: {e}")
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error=str(e),
                latency_ms=0
            )

    async def _fetch_real_data(self, tickers: List[str]) -> Optional[Dict[str, Any]]:
        """
        Fetch real price history via Alpaca (primary) or MCP/YF (fallback) and calculate metrics.
        Returns a universe dict with real 'return' and 'vol' values.
        """
        try:
             # Use Alpaca for data fetching as per user request
            from data_engine import get_alpaca_client
            alpaca = get_alpaca_client()
            
            real_universe = {}
            
            # Helper to fetch single ticker via Alpaca
            async def fetch_ticker(t):
                try:
                    # Fetch 1 year history (approx 252 trading days + buffer)
                    # We need enough data for robust volatility calc
                    bars_data = await alpaca.get_historical_bars(t, timeframe="1Day", limit=300)
                    
                    if not bars_data or "bars" not in bars_data or not bars_data["bars"]:
                        # warning logged deeper if needed
                        return t, self.asset_universe.get(t)
                        
                    bars = bars_data["bars"]
                    if len(bars) < 30: # Need at least a month of data
                         return t, self.asset_universe.get(t)
                    
                    # Calculate Metrics
                    # 1. Prices
                    prices = [b['c'] for b in bars]
                    
                    # 2. Daily Returns
                    daily_returns = np.diff(prices) / prices[:-1]
                    
                    # 3. Annual Return (CAGR approx or simple 1y change)
                    # Simple 1-year change: (End - Start) / Start
                    # If we have less than 1y, strictly speaking we should annualize, but for now simple change
                    start_price = prices[0]
                    end_price = prices[-1]
                    total_ret = (end_price - start_price) / start_price if start_price > 0 else 0
                    
                    # Normalize to 1-year if data is short (e.g. IPO)
                    days_covered = len(bars)
                    if days_covered > 0 and days_covered < 200:
                         total_ret = total_ret * (TRADING_DAYS / days_covered)
                    
                    annual_ret = total_ret
                    
                    # 4. Volatility
                    daily_vol = np.std(daily_returns)
                    annual_vol = daily_vol * np.sqrt(TRADING_DAYS)
                    
                    # 5. Momentum Score (Sharpe-like: Return / Vol)
                    mom_score = 0
                    if annual_vol > 0:
                        mom_score = annual_ret / annual_vol
                    
                    return t, {
                        "name": self.asset_universe.get(t, {}).get("name", t),
                        "sector": self.asset_universe.get(t, {}).get("sector", "Unknown"),
                        "return": float(annual_ret),
                        "vol": float(annual_vol),
                        "momentum_score": float(mom_score)
                    }
                except Exception as ex:
                    logger.warning(f"Failed to fetch data for {t} via Alpaca: {ex}")
                    return t, self.asset_universe.get(t)

            # Gather all fetches
            import asyncio
            results = await asyncio.gather(*[fetch_ticker(t) for t in tickers])
            
            for t, data in results:
                if data:
                    real_universe[t] = data
            
            return real_universe
            
        except Exception as e:
            logger.error(f"Global real data fetch failed: {e}")
            return None

    def _optimize_portfolio(self, returns, cov_matrix, target_return, risk_profile):
        """
        Maximize Sharpe Ratio subject to risk constraints.
        Objective: Minimize negative Sharpe ratio.
        """
        n = len(returns)
        
        # Max volatility allowed for this profile
        profile_limit = self.risk_profiles.get(risk_profile, self.risk_profiles["MEDIUM"])
        max_vol = profile_limit["max_volatility"]
        
        def objective(weights):
            # We want to minimize negative Sharpe Ratio
            p_ret = np.dot(weights, returns)
            p_vol = np.sqrt(np.dot(weights.T, np.dot(cov_matrix, weights)))
            # Add small epsilon to avoid divide by zero
            return -(p_ret - RISK_FREE_RATE) / (p_vol + 1e-9)
        
        # Constraints
        constraints = [
            {'type': 'eq', 'fun': lambda w: np.sum(w) - 1.0}, # Weights sum to 1
            {'type': 'ineq', 'fun': lambda w: max_vol - np.sqrt(np.dot(w.T, np.dot(cov_matrix, w)))} # Volatility limit
        ]
        
        # Bounds: Long-only (min 0%, max 100% per asset)
        bounds = tuple((0, 1) for _ in range(n))
        
        # Initial guess: Equal weights
        init_guess = np.array([1.0/n] * n)
        
        res = minimize(objective, init_guess, method='SLSQP', bounds=bounds, constraints=constraints)
        
        if not res.success:
            logger.warning(f"Portfolio optimization did not converge: {res.message}")
            final_weights = init_guess
        else:
            final_weights = res.x
            
        p_ret = np.dot(final_weights, returns)
        p_vol = np.sqrt(np.dot(final_weights.T, np.dot(cov_matrix, final_weights)))
        sharpe = (p_ret - RISK_FREE_RATE) / (p_vol + 1e-9)
        
        return {
            "weights": final_weights,
            "expected_return": p_ret,
            "expected_volatility": p_vol,
            "sharpe_ratio": sharpe
        }

    def _monte_carlo_simulation(self, s0, mu, sigma, t, target_mu):
        """
        Run 10,000 simulations to calculate probability of success.
        Uses Geometric Brownian Motion.
        """
        num_sims = 10000
        
        # Formula: S_T = S_0 * exp((mu - 0.5*sigma^2)T + sigma*sqrt(T)*Z)
        z = np.random.standard_normal(num_sims)
        final_values = s0 * np.exp((mu - 0.5 * sigma**2) * t + sigma * np.sqrt(t) * z)
        
        # Target value calculation
        target_value = s0 * (1 + target_mu)**t
        
        successes = np.sum(final_values >= target_value)
        prob = successes / num_sims
        
        return {
            "probability": float(prob),
            "avg_final_value": float(np.mean(final_values)),
            "median_final_value": float(np.median(final_values))
        }
