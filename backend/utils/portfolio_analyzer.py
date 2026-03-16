import numpy as np
try:
    import mlx.core as mx
    MLX_AVAILABLE = True
except ImportError:
    mx = None
    MLX_AVAILABLE = False
from scipy.optimize import minimize
from decimal import Decimal
from typing import Dict, List, Optional, Union, Any
from utils.jmce_model import NeuralJMCE, get_jmce_model, TimeResolution
from utils.risk_engine import RiskEngine
from utils.financial_math import create_decimal
from app_logging import setup_logging
from app_context import state

logger = setup_logging("portfolio_analyzer")

class PortfolioAnalyzer:
    """
    Institutional-grade portfolio optimizer using Neural JMCE and SLSQP.
    Implements 10% Position Cap and Dynamic Alpha Hurdle.
    
    SOTA 2026: Dynamically utilizes ANE (CoreML) or GPU (MLX) for NPU acceleration.
    """
    
    def __init__(self, model: Optional[Any] = None, n_assets: int = 50, resolution: TimeResolution = TimeResolution.DAILY):
        # Use state.ane_config to decide if we should use ANE
        use_ane = getattr(state.ane_config, 'enabled', True)
        self.model = model or get_jmce_model(n_assets=n_assets, use_ane=use_ane, resolution=resolution)
        self.risk_engine = RiskEngine()

    async def optimize_weights(
        self, 
        returns_history: np.ndarray, 
        macro_signals: Dict[str, Any], 
        persona: str = 'aggressive',
        rf_rate: float = 0.04
    ) -> List[Decimal]:
        """
        Optimizes portfolio weights based on regime-aware JMCE estimates.
        
        Args:
            returns_history: (seq_len, n_assets) array of historical returns.
            macro_signals: Dict from RegimeFetcher.
            persona: 'aggressive' (Max Sharpe) or 'defensive' (Min Vol).
            rf_rate: Annual risk-free rate.
            
        Returns:
            List[Decimal]: Optimal weights for each asset.
        """
        n_assets = returns_history.shape[1]
        seq_len = returns_history.shape[0]
        
        try:
            # 1. Forward Pass on NPU (ANE or GPU)
            # SOTA 2026: NeuralJMCE returns (mu, L, V)
            # Input shape: (seq_len, n_assets) or (1, seq_len, n_assets)
            
            if hasattr(self.model, "__call__"):
                if isinstance(self.model, NeuralJMCE):
                    # MLX Path (GPU)
                    x = mx.array(returns_history[np.newaxis, :, :].astype(np.float32))
                    mu_mx, L_mx, _ = self.model(x)
                    sigma_mx = self.model.get_covariance(L_mx)
                    
                    # Convert to numpy for SciPy
                    mu = np.array(mu_mx[0])
                    sigma = np.array(sigma_mx[0])
                else:
                    # CoreML Path (ANE) - Using duck typing for CoreMLJMCE
                    # Expects (seq_len, n_assets)
                    mu, L, _ = self.model(returns_history.astype(np.float32))
                    
                    # CoreML outputs are already numpy arrays
                    # We need to compute sigma = L * L^T if L is Cholesky
                    if mu is None or L is None:
                        raise RuntimeError("CoreML model returned None for mu or L")
                        
                    if L.ndim == 3: L = L[0] # remove batch if present
                    if mu.ndim == 2: mu = mu[0]
                    
                    sigma = np.matmul(L, L.T)
            else:
                raise RuntimeError("Invalid JMCE model loaded")
            
            # 2. Dynamic Hurdle (75 bps + Friction)
            # In a real scenario, friction would be asset-specific (e.g. T212 spreads/FX)
            # Here we apply a global hurdle as per SPEC
            hurdle = 0.0075 / 252 # Daily hurdle from 75bps annual
            mu_adj = mu - hurdle
            
            # Daily risk-free rate
            rf_daily = rf_rate / 252
            
            # 3. Objective Functions
            def objective(w):
                port_return = np.dot(w, mu_adj)
                # Portfolio variance: w^T * Sigma * w
                port_var = np.dot(w.T, np.dot(sigma, w))
                port_vol = np.sqrt(max(port_var, 1e-12))
                
                if persona == 'aggressive':
                    # Maximize Sharpe = Minimize -Sharpe
                    sharpe = (port_return - rf_daily) / (port_vol + 1e-9)
                    return -float(sharpe)
                else:
                    # Minimize Volatility
                    return float(port_vol)

            # 4. Constraints & Bounds
            # Constraint: Weights must sum to 1.0
            constraints = [
                {'type': 'eq', 'fun': lambda w: np.sum(w) - 1.0}
            ]
            
            # Hard 10% Position Cap as per SPEC
            bounds = [(0.0, 0.10) for _ in range(n_assets)]
            
            # 5. Optimization (SLSQP)
            # Initial guess: equal weights (capped at 10%)
            # If more than 10 assets, equal weights will be < 10%
            init_w = np.array([1.0 / n_assets] * n_assets)
            
            # Run optimizer
            res = minimize(
                objective, 
                init_w, 
                method='SLSQP', 
                bounds=bounds, 
                constraints=constraints,
                options={'maxiter': 100, 'ftol': 1e-6}
            )
            
            if not res.success:
                logger.warning(f"Optimization did not converge: {res.message}. Using equal weights.")
                # Ensure equal weights don't violate bounds if n_assets < 10
                # But for optimization, we usually have many assets.
                # If we have 5 assets, equal weights = 0.20 (violates 0.10 cap)
                # In that case, we can't sum to 1.0 with 0.10 cap.
                # The SPEC implies we have enough assets or allow cash.
                # For now, we assume n_assets >= 10 or weights sum to max possible.
                return [create_decimal(w) for w in init_w]
            
            final_weights = [create_decimal(w) for w in res.x]
            logger.info(f"Optimization successful ({persona}). Max weight: {max(res.x):.4f}")
            return final_weights
            
        except Exception as e:
            logger.error(f"Portfolio optimization failed: {e}", exc_info=True)
            # Fallback to equal weights
            return [create_decimal(1.0 / n_assets) for _ in range(n_assets)]

    async def get_covariance_velocity(self, returns_history: np.ndarray) -> Optional[float]:
        """
        SOTA 2026 Phase 30: Extract covariance velocity for high-velocity shifts.
        Uses M4 NPU (MLX/ANE) to detect rapid correlation changes.
        """
        try:
            if hasattr(self.model, "__call__"):
                if isinstance(self.model, NeuralJMCE):
                    x = mx.array(returns_history[np.newaxis, :, :].astype(np.float32))
                    _, _, V_mx = self.model(x, return_velocity=True)
                    if V_mx is not None:
                        # For single asset (N=1), V is (1, 1, 1). Return the scalar.
                        # For multiple assets, return Frobenius norm as a shift indicator.
                        if V_mx.shape[1] == 1:
                            return float(V_mx[0, 0, 0])
                        else:
                            # Use mx.linalg.norm if available, or manual Fro norm
                            v_np = np.array(V_mx[0])
                            return float(np.linalg.norm(v_np))
                else:
                    # CoreML Path (ANE)
                    _, _, V = self.model(returns_history.astype(np.float32), return_velocity=True)
                    if V is not None:
                        if V.ndim == 3: V = V[0]
                        return float(np.linalg.norm(V))
            return None
        except Exception as e:
            logger.warning(f"Failed to extract covariance velocity: {e}")
            return None
