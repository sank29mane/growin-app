import time
import numpy as np
from typing import Dict, Any, Optional
from backend.simulation.models import MarketImpactModel

class PreFlightSimulator:
    """
    Stateless in-memory pre-flight simulator engine.
    Computes expected fill prices including non-linear market impact, and tracks drawdowns.
    """
    def __init__(self, impact_exponent: float = 0.5):
        self.impact_model = MarketImpactModel(impact_exponent=impact_exponent)

    def simulate_execution(
        self,
        order_side: str,
        order_qty: float,
        tick_window: Dict[str, Any],
        portfolio_state: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Simulate trade execution using a stateless, thread-safe projection.
        
        Parameters:
            order_side (str): "BUY" or "SELL".
            order_qty (float): Proposed order quantity.
            tick_window (dict): Contains NumPy arrays for 'timestamps', 'bid', 'ask', 'spread',
                                'bid_depth', and 'ask_depth'.
            portfolio_state (dict): Current portfolio values (e.g. 'equity' and 'peak_equity').
            
        Returns:
            dict: Telemetry with simulated_fill_price, simulator_drawdown_pct, and latency_ms.
        """
        start_time = time.perf_counter()

        # Compute mid price from the last tick in the window
        bid_series = tick_window["bid"]
        ask_series = tick_window["ask"]
        
        if len(bid_series) == 0 or len(ask_series) == 0:
            raise ValueError("Empty tick window provided to simulator.")

        last_bid = float(bid_series[-1])
        last_ask = float(ask_series[-1])
        mid_price = (last_bid + last_ask) / 2.0

        # Extract current depth level
        bid_depth = tick_window.get("bid_depth")
        ask_depth = tick_window.get("ask_depth")

        # If depths are lists or timeseries, get the last step
        if bid_depth is not None and len(bid_depth) > 0:
            if isinstance(bid_depth, list) or (isinstance(bid_depth, np.ndarray) and bid_depth.ndim > 2):
                bid_depth = bid_depth[-1]
        if ask_depth is not None and len(ask_depth) > 0:
            if isinstance(ask_depth, list) or (isinstance(ask_depth, np.ndarray) and ask_depth.ndim > 2):
                ask_depth = ask_depth[-1]

        # Calculate slippage
        order_side_upper = order_side.upper()
        if order_side_upper == "BUY":
            trade_size = order_qty
        elif order_side_upper == "SELL":
            trade_size = -order_qty
        else:
            trade_size = 0.0

        slippage = 0.0
        if order_qty > 0.0 and trade_size != 0.0:
            slippage = self.impact_model.calculate_slippage(
                trade_size=trade_size,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                mid_price=mid_price
            )

        if order_side_upper == "BUY":
            simulated_fill_price = mid_price + slippage
        elif order_side_upper == "SELL":
            simulated_fill_price = mid_price - slippage
        else:
            simulated_fill_price = mid_price

        # Calculate drawdown from portfolio_state
        current_equity = float(portfolio_state.get("equity") if portfolio_state.get("equity") is not None else portfolio_state.get("current_val", 10000.0))
        peak_equity = float(portfolio_state.get("peak_equity") if portfolio_state.get("peak_equity") is not None else portfolio_state.get("peak_val", current_equity))
        
        # Ensure peak equity is at least current equity
        peak_equity = max(peak_equity, current_equity)
        
        if peak_equity > 0.0:
            simulator_drawdown_pct = (peak_equity - current_equity) / peak_equity
        else:
            simulator_drawdown_pct = 0.0

        end_time = time.perf_counter()
        latency_ms = (end_time - start_time) * 1000.0

        return {
            "simulated_fill_price": simulated_fill_price,
            "simulator_drawdown_pct": simulator_drawdown_pct,
            "latency_ms": latency_ms
        }
