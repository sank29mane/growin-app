"""
Live Trading Loop Component.
Integrates online feature engineering, Welford standardizer,
Numba GMM regime inference, MLX adapter hot-swapping, and dynamic risk scaling.
"""
import numpy as np
import logging
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
        leverage_coefficients: Optional[Dict[int, float]] = None
    ):
        """
        Initialize the live trading loop.
        
        Args:
            model_manager: Pre-configured MLXAdapterManager for hot-swapping QLoRA adapters
            gmm_params: Dictionary of parameters loaded from the trained GMM model
            alpha: EMA volatility smoothing parameter
            leverage_coefficients: Risk scaling coefficients mapping regime_id -> weight (default: {0: 1.0, 1: 0.5, 2: 0.1, 3: 0.05})
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
