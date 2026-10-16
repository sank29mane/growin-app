"""
Live Trading Loop Component.
Integrates online feature engineering, Welford standardizer,
Numba GMM regime inference, MLX adapter hot-swapping, and dynamic risk scaling.
"""
import numpy as np
import logging
import asyncio
import numbers
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
    from backend.simulation.regime_severity import build_severity_map, validate_posterior
except ImportError:
    from simulation.regime_severity import build_severity_map, validate_posterior
    from simulation import (
        PreFlightDecision,
        PreFlightSimulator,
        RiskSwarmGate,
        TelemetryLogger,
    )

logger = logging.getLogger(__name__)

class InvalidTickError(ValueError):
    """A tick that cannot be classified. ``code`` is the stable reason the loop reports."""

    code = "TICK_INVALID"


# Upper bound on any quoted price the loop will classify. The dearest listed share trades near
# 7e5 and the largest quote scales (JPY, KRW, INR paise) stay under 1e8, so 1e12 leaves four
# orders of magnitude of headroom. The point is overflow: the volatility EMA squares a relative
# return, and a price near 1e308 gives an infinite variance that no later tick can unwind.
_MAX_TICK_PRICE = 1e12


def _validate_tick(mid_price: object, ask: object, bid: object) -> None:
    """Reject a tick that would poison the feature state or be sized on nonsense, before any update.

    Refused: a non-real value or a bool; NaN or infinity; a price at or below zero; a price
    at or above ``_MAX_TICK_PRICE``; a crossed book (bid above ask). The volatility tracker
    returns 0.0 on its very first update, which hides a NaN or infinite mid on a cold loop,
    and one such value poisons the EMA and Welford state for good, so the check comes first.
    A refused tick leaves every feature state untouched and the next good tick classifies
    normally. ``bid == ask`` (a zero spread) is accepted; only ``bid > ask`` is crossed.
    """

    for name, value in (("mid", mid_price), ("ask", ask), ("bid", bid)):
        if isinstance(value, bool) or not isinstance(value, numbers.Real):
            raise InvalidTickError(f"{name} price is not a real number")
        # Written as a positive range test so NaN (every comparison False) is refused too.
        if not (0 < value < _MAX_TICK_PRICE):
            raise InvalidTickError(f"{name} price is not finite and inside (0, {_MAX_TICK_PRICE:g})")
    if bid > ask:
        raise InvalidTickError("the book is crossed (bid above ask)")


def _failure_reason(exc: BaseException) -> str:
    """A stable label for a pipeline failure; never raises, even if ``exc.code`` does."""

    try:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and code:
            return code
    except Exception:  # noqa: BLE001 - a hostile ``code`` property must not escape
        pass
    return type(exc).__name__


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
        leverage_by_severity_rank: Optional[Dict[int, float]] = None,
        telemetry_db_path: str = "simulation_telemetry.db",
        alpaca_client: Any = None
    ):
        """
        Initialize the live trading loop.
        
        Args:
            model_manager: Pre-configured MLXAdapterManager for hot-swapping QLoRA adapters
            gmm_params: Dictionary of parameters loaded from the trained GMM model
            alpha: EMA volatility smoothing parameter
            leverage_by_severity_rank: Optional override of risk scaling weights keyed by
                severity rank (0 = calmest), never by raw component id. The default is the
                model's own policy table (calm 1.0, 0.5, 0.1, crisis 0.05 for K=4).
            telemetry_db_path: Path to the SQLite telemetry database file
        """
        try:
            from backend.simulation.requoter import AdaptiveReQuoter
        except ImportError:
            from simulation.requoter import AdaptiveReQuoter

        self.model_manager = model_manager
        self.gmm_params = gmm_params
        self.alpaca_client = alpaca_client
        
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
        
        # Raw component ids are arbitrary. The severity map orders this model's components
        # (raising for an unsupported K, a bad covariance or scaler, or tied scores), and
        # every consumer below reads it: leverage, adapters, collar and the size policy.
        self.severity_map = build_severity_map(gmm_params)
        count = self.severity_map.component_count
        if leverage_by_severity_rank is None:
            self.leverage_coefficients = self.severity_map.size_multipliers_by_id()
        else:
            if set(leverage_by_severity_rank) != set(range(count)):
                raise ValueError("leverage_by_severity_rank must have exactly one weight per severity rank")
            self.leverage_coefficients = {
                raw: float(leverage_by_severity_rank[self.severity_map.rank(raw)])
                for raw in range(count)
            }
        self.risk_leverage_coefficient: float = 1.0

        # Instantiate simulation objects
        self.simulator = PreFlightSimulator()
        self.swarm_gate = RiskSwarmGate()
        self.telemetry_logger = TelemetryLogger(db_path=telemetry_db_path)
        
        self.requoter = AdaptiveReQuoter(
            vol_tracker=self.vol_tracker,
            alpaca_client=self.alpaca_client,
            current_regime=self.current_regime,
            severity_map=self.severity_map,
        )
        self.requoter_task = None

    def start(self):
        if self.requoter_task is None:
            self.requoter_task = asyncio.create_task(self.requoter.run_polling_loop())

    def stop(self):
        if self.requoter_task:
            self.requoter_task.cancel()
            self.requoter_task = None

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
        # The whole classification pipeline (feature updates, posterior, adapter lookups, swap
        # and leverage) sits in one try. A tick that cannot be turned into a regime (a missing
        # price, a bad posterior, an unreadable adapter slot) leaves no usable classification,
        # so any exception invalidates the regime. Without this the previous regime and its
        # leverage stayed in place and later orders were sized by a stale classification.
        vol: Optional[float] = None
        spread: Optional[float] = None
        try:
            # 0. Refuse a bad tick before any feature state moves (see _validate_tick).
            _validate_tick(mid_price, ask, bid)

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
            # A NaN or malformed posterior has an argmax of 0, which would silently pick
            # whichever component is numbered 0, so it is validated before it is read.
            probabilities = validate_posterior(probabilities, self.severity_map.component_count)
            dominant_regime = int(np.argmax(probabilities))
            # The adapter follows the component's severity rank (K=4 GMM, 3 adapters: the two
            # most severe ranks share the last adapter), never the raw component id.
            available_adapters = list(self.model_manager.preloaded_weights.keys())
            adapter_id = self.severity_map.adapter_id(dominant_regime, available_adapters)

            # 4. Trigger the MLX adapter hot-swapper when a regime change is identified
            regime_changed = False
            if dominant_regime != self.current_regime:
                try:
                    swap_success = bool(self.model_manager.swap_adapter(adapter_id))
                except Exception as exc:  # noqa: BLE001 - a failing adapter swap is a failed swap
                    logger.error(f"Adapter swap raised {type(exc).__name__} for regime {dominant_regime}")
                    swap_success = False
                if swap_success:
                    self.current_regime = dominant_regime
                    self.requoter.current_regime = dominant_regime
                    regime_changed = True
                else:
                    # The new regime is known but its adapter is not in place. Keeping the previous
                    # (possibly calmer) regime would size orders for the wrong market, so the
                    # regime is invalidated instead.
                    logger.error(f"Failed to hot-swap model adapter to {adapter_id} for regime {dominant_regime}")
                    return self._invalidate_regime(vol, spread, "ADAPTER_SWAP_FAILED")

            # 5. Apply profitability constraints: scale risk leverage based on regime probabilities
            self.risk_leverage_coefficient = float(
                sum(
                    probabilities[k] * self.leverage_coefficients[k]
                    for k in range(len(probabilities))
                )
            )

            return {
                "volatility": vol,
                "spread": spread,
                "probabilities": probabilities.tolist(),
                "dominant_regime": dominant_regime,
                "regime_changed": regime_changed,
                "regime_valid": True,
                "risk_leverage_coefficient": self.risk_leverage_coefficient,
                "active_adapter_id": adapter_id
            }
        except Exception as exc:  # noqa: BLE001 - any pipeline failure invalidates the regime
            # Invalidate first. Naming the reason can itself fail (an exception whose ``code``
            # raises), and that must never leave the stale regime usable.
            return self._invalidate_regime(vol, spread, exc)

    def _invalidate_regime(
        self, vol: Optional[float], spread: Optional[float], reason: "str | BaseException"
    ) -> Dict[str, Any]:
        """No usable regime: block sizing and dispatch until the next successful classification.

        The state is cleared before anything else, and nothing after it may raise: naming an
        exception (its ``code`` property) or logging can fail, and a failure there must not
        leave the previous regime in place.
        """

        self.current_regime = -1
        self.requoter.current_regime = -1
        self.risk_leverage_coefficient = 0.0
        if isinstance(reason, BaseException):
            reason = _failure_reason(reason)
        try:
            logger.error(f"Regime invalidated ({reason}); orders are blocked until the next good classification.")
        except Exception:  # noqa: BLE001 - the regime is already invalid; logging cannot undo that
            pass
        return {
            "volatility": vol,
            "spread": spread,
            "probabilities": [],
            "dominant_regime": -1,
            "regime_changed": False,
            "regime_valid": False,
            "invalid_reason": reason,
            "risk_leverage_coefficient": 0.0,
            "active_adapter_id": None,
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
            # No regime detected yet. A raw id of 0 is not a neutral default (it is an
            # arbitrary component), so an unclassified order is blocked, not sized.
            logger.warning("Pre-flight trade blocked: no regime has been classified yet.")
            return PreFlightDecision(
                approved=False,
                simulated_fill_price=simulated_fill_price,
                scaled_size=0.0,
                regime_id=-1,
                latency_ms=sim_res["latency_ms"],
                simulator_drawdown_pct=simulator_drawdown_pct,
            )

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
            db_connection=db_connection,
            severity_map=self.severity_map,
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

