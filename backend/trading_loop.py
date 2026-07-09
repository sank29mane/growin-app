"""
Live Trading Loop Component.
Integrates online feature engineering, Welford standardizer,
Numba GMM regime inference, MLX adapter hot-swapping, and dynamic risk scaling.
"""
import numpy as np
import logging
import asyncio
from typing import Dict, Any, Optional

try:
    from backend.features.online_vol import OnlineVolatility
    from backend.features.online_spread import RelativeSpread
    from backend.features.welford import WelfordStandardizer
    from backend.coreml.fast_gmm import fast_gmm_predict_proba
    from backend.mlx.adapter_manager import MLXAdapterManager
except ImportError:
    from features.online_vol import OnlineVolatility
    from features.online_spread import RelativeSpread
    from features.welford import WelfordStandardizer
    from coreml.fast_gmm import fast_gmm_predict_proba
    from mlx.adapter_manager import MLXAdapterManager

try:
    from backend.simulation import (
        PreFlightDecision,
        PreFlightSimulator,
        RiskSwarmGate,
        TelemetryLogger,
    )
except ImportError:
    from simulation import (
        PreFlightDecision,
        PreFlightSimulator,
        RiskSwarmGate,
        TelemetryLogger,
    )

logger = logging.getLogger(__name__)

class LiveTradingLoop:
    """
    Main live integration loop component coordinating market tick ingestion,
    real-time volatility/spread updates, Welford scaling, JIT regime prediction,
    and adaptive risk constraints.
    """
    def __init__(
        self,
        model_manager: MLXAdapterManager,
        gmm_params: Dict[str, np.ndarray],
        alpha: float = 0.05,
        leverage_coefficients: Optional[Dict[int, float]] = None,
        telemetry_db_path: str = "simulation_telemetry.db"
    ):
        """
        Initialize the live trading loop.
        
        Args:
            model_manager: Pre-configured MLXAdapterManager for hot-swapping QLoRA adapters
            gmm_params: Dictionary of parameters loaded from the trained GMM model
            alpha: EMA volatility smoothing parameter
            leverage_coefficients: Risk scaling coefficients mapping regime_id -> weight (default: {0: 1.0, 1: 0.5, 2: 0.1, 3: 0.05})
            telemetry_db_path: Path to the SQLite telemetry database file
        """
        self.model_manager = model_manager
        self.gmm_params = gmm_params
        
        # Instantiate fast online features
        self.vol_tracker = OnlineVolatility(alpha=alpha)
        self.spread_tracker = RelativeSpread()
        
        # Instantiate online standardizers
        self.vol_standardizer = WelfordStandardizer()
        self.spread_standardizer = WelfordStandardizer()
        
        # Warm-start standardizers with offline statistics if available
        if "scaler_mean" in gmm_params and "scaler_var" in gmm_params:
            self.vol_standardizer.initialize_state(
                mean=float(gmm_params["scaler_mean"][0]),
                variance=float(gmm_params["scaler_var"][0])
            )
            self.spread_standardizer.initialize_state(
                mean=float(gmm_params["scaler_mean"][1]),
                variance=float(gmm_params["scaler_var"][1])
            )
            
        # Extract model arrays
        self.weights = np.ascontiguousarray(gmm_params["weights"])
        self.means = np.ascontiguousarray(gmm_params["means"])
        self.precisions_cholesky = np.ascontiguousarray(gmm_params["precisions_cholesky"])
        
        # Live state tracking
        self.current_regime: int = -1
        
        # Default leverage coefficients up to K=4 regimes to handle trained GMM models
        default_coefs = {0: 1.0, 1: 0.5, 2: 0.1, 3: 0.05}
        self.leverage_coefficients = leverage_coefficients or default_coefs
        self.risk_leverage_coefficient: float = 1.0

        # Instantiate simulation objects
        self.simulator = PreFlightSimulator()
        self.swarm_gate = RiskSwarmGate()
        self.telemetry_logger = TelemetryLogger(db_path=telemetry_db_path)

    def process_tick(self, mid_price: float, ask: float, bid: float) -> Dict[str, Any]:
        """
        Accepts a tick update and executes the entire synchronous processing pipeline:
        1. Dynamically compute relative spreads and running EMA volatility.
        2. Run Z-score scaling using Welford parameters.
        3. Run Numba fast GMM probability predictor.
        4. Trigger the MLX adapter hot-swapper when a regime change is identified.
        5. Apply profitability constraints to scale risk leverage coefficients.
        
        Args:
            mid_price (float): Current mid price of the asset
            ask (float): Current ask price of the asset
            bid (float): Current bid price of the asset
            
        Returns:
            dict: Pipeline telemetry for verification and monitoring
        """
        # 1. Dynamically compute relative spreads and running EMA volatility
        vol = self.vol_tracker.update(mid_price)
        spread = self.spread_tracker.update(bid, ask)
        
        # 2. Run Z-score scaling using Welford parameters (accumulate running stats)
        self.vol_standardizer.update(vol)
        self.spread_standardizer.update(spread)
        
        # Prepare 1D feature array
        x = np.array([vol, spread], dtype=np.float64)
        
        # Construct scaling mean and var arrays. Force minimum variance of 1e-12 to prevent div by zero.
        scaler_mean = np.array([self.vol_standardizer.mean, self.spread_standardizer.mean], dtype=np.float64)
        scaler_var = np.array([
            max(self.vol_standardizer.variance, 1e-12),
            max(self.spread_standardizer.variance, 1e-12)
        ], dtype=np.float64)
        
        # 3. Run Numba fast GMM probability predictor
        probabilities = fast_gmm_predict_proba(
            x,
            self.weights,
            self.means,
            self.precisions_cholesky,
            scaler_mean,
            scaler_var
        )
        
        dominant_regime = int(np.argmax(probabilities))
        
        # 4. Trigger the MLX adapter hot-swapper when a regime change is identified
        # Map the dominant regime to preloaded adapter IDs to handle mismatched counts (e.g. K=4 GMM, 3 adapters)
        available_adapters = list(self.model_manager.preloaded_weights.keys())
        if available_adapters:
            adapter_id = min(dominant_regime, max(available_adapters))
        else:
            adapter_id = dominant_regime
            
        regime_changed = False
        if dominant_regime != self.current_regime:
            swap_success = self.model_manager.swap_adapter(adapter_id)
            if swap_success:
                self.current_regime = dominant_regime
                regime_changed = True
            else:
                logger.error(f"Failed to hot-swap model adapter to {adapter_id} for regime {dominant_regime}")
                
        # 5. Apply profitability constraints: scale risk leverage based on regime probabilities
        self.risk_leverage_coefficient = float(
            sum(
                probabilities[k] * self.leverage_coefficients.get(k, 0.05 if k >= 2 else 1.0)
                for k in range(len(probabilities))
            )
        )
        
        return {
            "volatility": vol,
            "spread": spread,
            "probabilities": probabilities.tolist(),
            "dominant_regime": dominant_regime,
            "regime_changed": regime_changed,
            "risk_leverage_coefficient": self.risk_leverage_coefficient,
            "active_adapter_id": adapter_id
        }

    async def execute_order_pre_flight(
        self,
        order_side: str,
        order_qty: float,
        tick_window: Dict[str, Any],
        portfolio_state: Dict[str, Any],
        db_connection,
        broker_dispatch_coro,
        drawdown_limit: float = 0.10,
        reoptimize_callback = None
    ) -> Optional[PreFlightDecision]:
        """
        Intercepts order generation to run pre-flight simulation and risk swarm gate checks.
        If approved, dispatches the order to the broker and logs post-fill telemetry.
        
        Parameters:
            order_side (str): "BUY" or "SELL".
            order_qty (float): Proposed order size.
            tick_window (dict): Sliding window of ticks.
            portfolio_state (dict): Current portfolio metrics.
            db_connection: Database connection to query scaling policies.
            broker_dispatch_coro: A coroutine that executes broker order placement and returns
                                  actual fill info (e.g. {"actual_fill_price": float}).
            drawdown_limit (float): Limit for triggering re-optimization routines.
            reoptimize_callback (callable): Optional callback for re-optimization.
            
        Returns:
            PreFlightDecision: The pre-flight evaluation results.
        """
        loop = asyncio.get_running_loop()
        
        # 1. Execute simulator in thread pool to avoid GIL blocks on async loop
        sim_res = await loop.run_in_executor(
            None,
            self.simulator.simulate_execution,
            order_side,
            order_qty,
            tick_window,
            portfolio_state
        )
        
        simulated_fill_price = sim_res["simulated_fill_price"]
        simulator_drawdown_pct = sim_res["simulator_drawdown_pct"]
        
        # 2. Get current regime and spread pct
        regime_id = self.current_regime
        if regime_id == -1:
            regime_id = 0  # Default to base regime if none detected yet
            
        # Get relative spread from last tick in window, or default to current spread tracker value
        if "spread" in tick_window and len(tick_window["spread"]) > 0:
            current_spread_pct = float(tick_window["spread"][-1])
        else:
            current_spread_pct = self.spread_tracker.spread

        # 3. Call RiskSwarmGate to get scaled size
        scaled_size = self.swarm_gate.evaluate(
            simulated_fill_price=simulated_fill_price,
            trade_size=order_qty,
            regime_id=regime_id,
            current_spread_pct=current_spread_pct,
            db_connection=db_connection
        )
        
        approved = scaled_size > 0.0
        
        decision = PreFlightDecision(
            approved=approved,
            simulated_fill_price=simulated_fill_price,
            scaled_size=scaled_size,
            regime_id=regime_id,
            latency_ms=sim_res["latency_ms"],
            simulator_drawdown_pct=simulator_drawdown_pct
        )
        
        # 4. Trigger re-optimization routines if drawdown limit exceeded
        if simulator_drawdown_pct > drawdown_limit:
            logger.warning(
                f"Simulator drawdown {simulator_drawdown_pct * 100:.2f}% "
                f"exceeds limit {drawdown_limit * 100:.2f}%. Triggering re-optimization."
            )
            if reoptimize_callback is not None:
                if asyncio.iscoroutinefunction(reoptimize_callback):
                    await reoptimize_callback()
                else:
                    reoptimize_callback()

        # 5. Bypass broker dispatch if scaled size is 0
        if not approved:
            logger.warning(f"Pre-flight trade blocked or scaled to 0. Bypassing broker dispatch.")
            return decision

        # 6. Dispatch order to broker
        try:
            broker_fill = await broker_dispatch_coro(scaled_size, simulated_fill_price)
            actual_fill_price = broker_fill["actual_fill_price"]
        except Exception as e:
            logger.error(f"Broker dispatch failed: {e}")
            return decision

        # 7. Post-fill telemetry logging
        # Compute metrics
        slippage_error_bps = abs(actual_fill_price - simulated_fill_price) / simulated_fill_price * 10000.0
        actual_deviation_bps = abs(actual_fill_price - simulated_fill_price) / simulated_fill_price * 10000.0
        size_reduction_pct = (order_qty - scaled_size) / order_qty if order_qty > 0.0 else 0.0
        gate_blocks_count = 1 if scaled_size < order_qty else 0

        self.telemetry_logger.log_cycle(
            simulation_latency_ms=decision.latency_ms,
            slippage_error_bps=slippage_error_bps,
            gate_blocks_count=gate_blocks_count,
            active_regime_distribution=regime_id,
            tick_window=tick_window,
            actual_deviation_bps=actual_deviation_bps,
            size_reduction_pct=size_reduction_pct
        )

        return decision

