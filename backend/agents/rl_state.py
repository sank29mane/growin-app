"""
RL State Fabricator - SOTA 2026 Edition
Fuses outputs from JMCE (Mu/Sigma) and TTM-R2 (Trends) into a normalized 
observation tensor for the RL Action Head.

Includes Institutional Rebalance detection (2:00 PM GMT Play), 
Volatility Tax sensors, and Semantic Fusion via CLaRa.
"""

import numpy as np
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Union
import logging

try:
    import mlx.core as mx
    HAS_MLX = True
except ImportError:
    mx = None
    HAS_MLX = False

# Import ClaraAgent for semantic fusion
from backend.agents.clara_agent import ClaraAgent

logger = logging.getLogger(__name__)

class RLStateFabricator:
    """
    Constructs the 'Global State' vector for the Reinforcement Learning policy.
    Optimized for LSE Leveraged ETFs (3x/5x).
    
    Updated for Semantic Fusion (Wave 2): 96-dim (64 quant + 32 semantic).
    """
    
    def __init__(self, n_assets: int = 10, state_dim: int = 96):
        self.n_assets = n_assets
        self.state_dim = state_dim
        self.history_buffer = [] # For temporal features
        self.clara_agent = ClaraAgent() # Lazy-loaded during first context call
        
    def fabricator_state(
        self, 
        jmce_mu: np.ndarray, 
        jmce_L: np.ndarray, 
        ttm_forecast: Dict[str, Any],
        portfolio_state: Dict[str, Any],
        market_metadata: Dict[str, Any],
        semantic_text: Optional[str] = None
    ) -> Union[np.ndarray, Any]:
        """
        Creates a normalized 96-dim state vector with fused semantic signal.
        
        State Space Mapping:
        - [0:N] Expected Returns (Mu)
        - [N:2N] Variance (Diag of LL^T)
        - [2N:3N] TTM Trend Confidence
        - [3N:3N+4] Portfolio Delta (Current vs Target)
        - [3N+4:3N+8] Time Features (Sine/Cosine of day, 2PM Indicator, Weekday)
        - [3N+8:64] Volatility Clustering (Regime)
        - [64:96] Semantic Fusion (CLaRa 32-dim Context)
        """
        # --- QUANT BLOCK (64-dim) ---
        
        # 1. Expected Returns (Mu) - Normalized and Padded
        mu_norm = np.clip(jmce_mu, -0.05, 0.05) * 20.0
        mu_feat = np.zeros(self.n_assets)
        n_mu = min(self.n_assets, len(mu_norm))
        mu_feat[:n_mu] = mu_norm[:n_mu]
        
        # 2. Extract Variance from Cholesky L (Diag of LL^T)
        var_feat = np.zeros(self.n_assets)
        actual_assets = min(self.n_assets, jmce_L.shape[0])
        for i in range(actual_assets):
            # Diag of LL^T is sum of squares of row i
            var_feat[i] = np.sum(jmce_L[i, :i+1]**2)
        
        # Normalize Variance (Log-scale usually works better for RL)
        var_feat = np.log1p(var_feat * 100.0)
        
        # 3. TTM Trend Logic
        # Extract the directional conviction from TTM-R2
        trend_feat = np.zeros(self.n_assets)
        if "forecast" in ttm_forecast and ttm_forecast["forecast"]:
            forecast_bars = ttm_forecast["forecast"]
            try:
                last_p = float(forecast_bars[-1].get("close", 0.0))
                # Use current price if provided in metadata, else first forecast bar
                curr_p = float(market_metadata.get("current_price", forecast_bars[0].get("close", 1.0)))
                
                if curr_p > 0:
                    net_return = (last_p - curr_p) / curr_p
                    # Scale to [-1, 1] (5% move = 1.0)
                    trend_val = np.clip(net_return * 20.0, -1.0, 1.0)
                    # Apply confidence weighting
                    conf = ttm_forecast.get("confidence", 0.8)
                    trend_feat.fill(trend_val * conf)
            except (IndexError, KeyError, ValueError, ZeroDivisionError):
                pass
            
        # 4. Portfolio Delta (Current vs Target)
        curr_w = portfolio_state.get('current_weights', np.zeros(4))
        tgt_w = portfolio_state.get('target_weights', np.zeros(4))
        
        curr_w_4 = np.zeros(4)
        tgt_w_4 = np.zeros(4)
        curr_w_4[:min(4, len(curr_w))] = curr_w[:min(4, len(curr_w))]
        tgt_w_4[:min(4, len(tgt_w))] = tgt_w[:min(4, len(tgt_w))]
        
        portfolio_delta = (tgt_w_4 - curr_w_4) * 10.0 # RL sensitivity scaling
        
        # 5. Institutional Rebalance Detection (2:00 PM GMT 'Smart Money')
        now_gmt = datetime.now(timezone.utc)
        is_rebalance_window = 1.0 if (now_gmt.hour == 14 and now_gmt.minute < 30) else 0.0
        
        # Temporal features
        day_minutes = now_gmt.hour * 60 + now_gmt.minute
        phi = 2.0 * np.pi * day_minutes / 1440.0
        sin_day = np.sin(phi)
        cos_day = np.cos(phi)
        is_weekday = 1.0 if now_gmt.weekday() < 5 else 0.0
        
        time_features = np.array([sin_day, cos_day, is_rebalance_window, is_weekday])
        
        # 6. Volatility Clustering (Regime)
        regime_data = market_metadata.get('regime', {})
        if isinstance(regime_data, dict):
            z_score = regime_data.get('z_score', 0.0)
            vol_score = regime_data.get('volatility_score', 0.20)
        else: # Handle RegimeState object
            z_score = getattr(regime_data, 'z_score', 0.0)
            vol_score = getattr(regime_data, 'volatility_score', 0.20)
            
        vol_regime = np.array([z_score, vol_score])
        
        # Build quant vector (target 64-dim)
        quant_vec = np.concatenate([
            mu_feat.flatten(),      # N
            var_feat.flatten(),     # N
            trend_feat.flatten(),   # N
            portfolio_delta,        # 4
            time_features,          # 4
            vol_regime              # 2
        ])
        
        # Pad/Clip quant block to exactly 64
        if len(quant_vec) < 64:
            quant_vec = np.pad(quant_vec, (0, 64 - len(quant_vec)))
        else:
            quant_vec = quant_vec[:64]

        # --- SEMANTIC BLOCK (32-dim) ---
        
        # 7. Semantic Fusion via CLaRa
        if semantic_text:
            # get_context_vector returns a 32-dim MLX array or NumPy array
            semantic_context = self.clara_agent.get_context_vector(semantic_text)
            if HAS_MLX and isinstance(semantic_context, mx.array):
                semantic_feat = np.array(semantic_context).flatten()
            else:
                semantic_feat = semantic_context.flatten()
        else:
            # Fallback to zero vector if no text provided
            semantic_feat = np.zeros(32, dtype=np.float32)

        # Ensure semantic feature is exactly 32
        if len(semantic_feat) < 32:
            semantic_feat = np.pad(semantic_feat, (0, 32 - len(semantic_feat)))
        else:
            semantic_feat = semantic_feat[:32]

        # --- FINAL FUSION (96-dim) ---
        state_vec = np.concatenate([quant_vec, semantic_feat]).astype(np.float32)
        
        # Ensure total state_dim is met
        if len(state_vec) < self.state_dim:
            state_vec = np.pad(state_vec, (0, self.state_dim - len(state_vec)))
        else:
            state_vec = state_vec[:self.state_dim]
            
        # 8. SOTA: MLX Normalization for Numerical Stability
        if HAS_MLX:
            mx_state = mx.array(state_vec)
            # Use mx.norm (L2) for stability
            norm_val = mx.linalg.norm(mx_state)
            if norm_val > 1e-8:
                mx_state = mx_state / norm_val
            return mx_state
            
        return state_vec

    def to_mlx(self, state_np: np.ndarray) -> Optional[Any]:
        if HAS_MLX:
            return mx.array(state_np)
        return None
