"""
Regime Agent - Volatility Clustering for Market Character Detection
Categorizes market state into CALM, DYNAMIC, or CRISIS regimes.
Crucial for scaling RL rebalancing frequency and 'Volatility Tax' calculation.
"""

import numpy as np
from typing import Dict, Any, List, Optional, Union
import logging
import time
from pydantic import BaseModel

try:
    import mlx.core as mx
    HAS_MLX = True
except ImportError:
    mx = None
    HAS_MLX = False

logger = logging.getLogger(__name__)

class RegimeState(BaseModel):
    label: str # CALM, DYNAMIC, CRISIS
    volatility_score: float # Annualized volatility estimate or PC1 Vol proxy
    spectral_radius: float # SOTA 2026: Covariance topology metric (Largest Eigenvalue)
    z_score: float # Deviation from historical mean
    timestamp: float

class RegimeAgent:
    """
    Detects market regimes using Rolling Volatility Z-Scores and JMCE Covariance Topology.
    Optimized for LSE Leveraged ETFs where 'Mean Reversion' vs 'Trend'
    characters shift rapidly.
    """
    
    def __init__(self, window_short: int = 20, window_long: int = 252):
        self.window_short = window_short # 20 days for current regime
        self.window_long = window_long   # 252 days for baseline
        
    def detect_regime(self, returns: np.ndarray) -> RegimeState:
        """
        Baseline Method: Calculate Realized Volatility Z-Score.
        Input: Array of log-returns.
        Output: RegimeState dataclass.
        """
        if len(returns) < self.window_short:
            return RegimeState(
                label='CALM', 
                volatility_score=0.0, 
                spectral_radius=0.0, 
                z_score=0.0, 
                timestamp=time.time()
            )
            
        # 1. Calculate Realized Volatility
        # SOTA: Annualized standard deviation of log returns
        # LSE specific: 252 trading days, 78 5-min bars per day
        current_vol = np.std(returns[-self.window_short:]) * np.sqrt(252 * 78)
        
        # 2. Historical Baseline
        if len(returns) >= self.window_long:
            historical_vols = []
            for i in range(self.window_long, len(returns), self.window_short):
                v = np.std(returns[i-self.window_short:i]) * np.sqrt(252 * 78)
                historical_vols.append(v)
            
            mean_vol = np.mean(historical_vols)
            std_vol = np.std(historical_vols)
            
            z_score = (current_vol - mean_vol) / std_vol if std_vol > 0 else 0.0
        else:
            # Fallback to simple thresholds if historical data is thin
            z_score = 0.0
            mean_vol = 0.20 # 20% default assumption
            
        # 3. Categorize with SOTA 2026 Thresholds
        if z_score > 2.0 or (z_score == 0.0 and current_vol > 0.60):
            label = 'CRISIS'
        elif z_score > 1.0 or (z_score == 0.0 and current_vol > 0.40):
            label = 'DYNAMIC'
        else:
            label = 'CALM'
            
        logger.info(f'Regime Detected (Z-Score): {label} (Vol: {current_vol:.2f}, Z: {z_score:.2f})')
        
        # Spectral radius proxy for traditional returns (approx variance)
        # spectral_radius = variance = (vol / annualization_factor)^2
        spectral_radius = (current_vol / np.sqrt(252 * 78))**2
        
        return RegimeState(
            label=label,
            volatility_score=float(current_vol),
            spectral_radius=float(spectral_radius),
            z_score=float(z_score),
            timestamp=time.time()
        )

    def detect_regime_jmce(self, L: Any) -> RegimeState:
        """
        SOTA 2026: Predictive Regime Detection using JMCE Covariance Topology.
        Input: Cholesky factor 'L' from NeuralJMCE (B, N, N) or (N, N).
        """
        # 1. Calculate Sigma: Sigma = LL^T
        if HAS_MLX and hasattr(L, 'device'):
            # L is MLX array
            if len(L.shape) == 3:
                L_mat = L[0] # Take first batch
            else:
                L_mat = L
            sigma = mx.matmul(L_mat, L_mat.transpose())
            sigma_np = np.array(sigma)
        elif hasattr(L, 'transpose'):
            # L is NumPy array
            if len(L.shape) == 3:
                L_mat = L[0]
            else:
                L_mat = L
            sigma_np = L_mat @ L_mat.T
        else:
            logger.error('Invalid L factor passed to RegimeAgent. Expected MLX/NumPy array.')
            return RegimeState(
                label='CALM', 
                volatility_score=0.0, 
                spectral_radius=0.0, 
                z_score=0.0, 
                timestamp=time.time()
            )

        # 2. Extract Spectral Radius (Largest Eigenvalue)
        # SOTA: The largest eigenvalue of the covariance matrix captures 
        # the variance of the first principal component (PC1).
        # A spike indicates systemic risk/contagion.
        # For symmetric PSD matrices, this is equivalent to the spectral norm (2-norm).
        try:
            spectral_radius = float(np.linalg.norm(sigma_np, ord=2))
        except Exception as e:
            logger.error(f'Failed to calculate spectral radius: {e}')
            spectral_radius = 0.0
        
        # 3. Define SOTA 2026 Regime Thresholds
        # Calibration for LSE Leveraged ETFs (3x/5x) using JMCE predicted covariance:
        # CALM: Low Spectral Radius (Stable markets, base volatility)
        # DYNAMIC: Expansion/Trend (Active movement, rising eigenvalues)
        # CRISIS: Contagion/Extreme Volatility (Spectral Radius Spike / Systemic Panic)
        
        if spectral_radius > 0.08:
            label = 'CRISIS'
        elif spectral_radius > 0.02:
            label = 'DYNAMIC'
        else:
            label = 'CALM'
            
        logger.info(f'JMCE Predictive Regime: {label} (Spectral Radius: {spectral_radius:.6f})')
        
        # volatility_score is a proxy for the standard deviation of PC1
        # spectral_radius = variance, sqrt(spectral_radius) = standard deviation (volatility)
        vol_score = np.sqrt(spectral_radius)
        
        return RegimeState(
            label=label,
            volatility_score=float(vol_score),
            spectral_radius=float(spectral_radius),
            z_score=0.0, # Not applicable for direct JMCE eigenvalues
            timestamp=time.time()
        )