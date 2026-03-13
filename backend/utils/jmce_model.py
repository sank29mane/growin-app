try:
    import mlx.core as mx
    import mlx.nn as nn
    HAS_MLX = True
except ImportError:
    mx = None
    # Provide a dummy nn.Module for inheritance if MLX is missing
    class DummyModule:
        def __init__(self, *args, **kwargs): pass
        def __call__(self, *args, **kwargs): pass
    class nn:
        Module = DummyModule
        @staticmethod
        def Linear(*args, **kwargs): return lambda x: x
    HAS_MLX = False
import numpy as np
from typing import Tuple, Optional

from enum import Enum
from typing import Tuple, Optional, Dict, Any

class TimeResolution(Enum):
    DAILY = "daily"
    INTRADAY_5MIN = "5min"
    INTRADAY_1MIN = "1min"

class NeuralJMCE(nn.Module):
    """
    Joint Mean-Covariance Estimator (JMCE) using a Transformer backbone.
    Optimized for Apple Silicon NPU via MLX.
    
    Predicts expected returns (mu) and the Cholesky factor (L) of the covariance matrix
    to ensure the resulting covariance is always Positive Definite.
    
    SOTA 2026 Phase 30: Added High-Velocity Intraday (1Min/5Min) resolution support.
    """
    def __init__(
        self,
        n_assets: int = 50,
        d_model: int = 128,
        n_layers: int = 3,
        n_heads: int = 4,
        seq_len: int = 180,
        resolution: TimeResolution = TimeResolution.DAILY
    ):
        super().__init__()
        self.n_assets = n_assets
        self.d_model = d_model
        self.resolution = resolution
        self.cholesky_size = (n_assets * (n_assets + 1)) // 2
        
        # SOTA 2026: Resolution-aware sequence padding/slicing
        # Daily: 180 days, 5Min: 78 intervals (1 day), 1Min: 390 intervals (1 day)
        self.target_seq_len = seq_len
        if resolution == TimeResolution.INTRADAY_5MIN:
            self.target_seq_len = max(seq_len, 78)
        elif resolution == TimeResolution.INTRADAY_1MIN:
            self.target_seq_len = max(seq_len, 390)

        # Input projection: daily returns for all assets -> d_model latent space
        if HAS_MLX:
            self.input_proj = nn.Linear(n_assets, d_model)
            
            # Positional encoding for sequence length (learned)
            # We use the target_seq_len to ensure we can handle full intraday days
            self.pos_emb = mx.random.normal((self.target_seq_len, d_model)) * 0.02
            
            # Transformer Backbone for sequence processing
            # Captures regime-aware dependencies and correlations over time
            self.transformer = nn.TransformerEncoder(
                num_layers=n_layers,
                dims=d_model,
                num_heads=n_heads,
                mlp_dims=d_model * 4
            )
            
            # Head for Expected Returns (mu)
            self.mu_head = nn.Linear(d_model, n_assets)
            
            # Head for Cholesky factor (L_flat)
            self.cholesky_head = nn.Linear(d_model, self.cholesky_size)
            
            # SOTA Phase 30: Velocity Head for Tick Covariance Shift Detection
            # Predicts the rate of change of the Cholesky factor
            self.velocity_head = nn.Linear(d_model, self.cholesky_size)
        else:
            self.input_proj = None
            self.pos_emb = None
            self.transformer = None
            self.mu_head = None
            self.cholesky_head = None
            self.velocity_head = None
        
        # Precompute indexing logic for Cholesky reconstruction
        self._init_cholesky_logic(n_assets)

    def _init_cholesky_logic(self, n_assets: int):
        """Precomputes indices and masks for efficient Cholesky reconstruction."""
        diag_indices = []
        idx_map = np.full((n_assets, n_assets), -1, dtype=np.int32)
        k = 0
        for i in range(n_assets):
            for j in range(i + 1):
                idx_map[i, j] = k
                if i == j:
                    diag_indices.append(k)
                k += 1
        
        # Mask for diagonal elements to apply exp()
        self.diag_mask = np.zeros(self.cholesky_size, dtype=np.float32)
        self.diag_mask[diag_indices] = 1.0
        self.diag_mask = mx.array(self.diag_mask)
        
        # Index map for gathering flat values into (N, N) matrix
        # Upper triangle will point to a 'zero' padding element
        idx_map[idx_map == -1] = self.cholesky_size
        self.idx_map_mx = mx.array(idx_map, dtype=mx.uint32)

    def __call__(self, x: mx.array, return_velocity: bool = False) -> Tuple[mx.array, mx.array, Optional[mx.array]]:
        """
        Forward pass.
        Args:
            x: Return sequences of shape (batch, seq_len, n_assets)
            return_velocity: Whether to return covariance velocity (Phase 30)
        Returns:
            mu: Expected returns (batch, n_assets)
            L: Cholesky factor (batch, n_assets, n_assets)
            V: Optional Covariance Velocity (batch, n_assets, n_assets)
        """
        B, S, N = x.shape
        # 1. Project to latent space
        x = self.input_proj(x)
        
        # 2. Add Positional Embedding (clipped to actual sequence length)
        x = x + self.pos_emb[:S]
        
        # 3. Backbone processing
        x = self.transformer(x, mask=None)
        
        # 4. Pooling (Last token representation)
        x_final = x[:, -1, :]
        
        # 5. Predict parameters
        mu = self.mu_head(x_final)
        L_flat = self.cholesky_head(x_final)
        
        # 6. Reconstruct valid Cholesky factor
        L = self._build_cholesky(L_flat)
        
        # 7. SOTA Phase 30: Velocity extraction
        V = None
        if return_velocity:
            V_flat = self.velocity_head(x_final)
            V = self._build_cholesky(V_flat) # Re-use building logic
            
        return mu, L, V

    def _build_cholesky(self, L_flat: mx.array) -> mx.array:
        """
        Reconstructs the lower-triangular L matrix and ensures the diagonal is positive.
        Sigma = L * L^T is guaranteed to be Positive Definite.
        """
        # Ensure diagonal elements are positive to guarantee PD covariance
        L_flat = L_flat * (1.0 - self.diag_mask) + mx.exp(L_flat) * self.diag_mask
        
        # Padding for upper triangle (zeros)
        B = L_flat.shape[0]
        zeros = mx.zeros((B, 1))
        L_flat_padded = mx.concatenate([L_flat, zeros], axis=1)
        
        # SOTA: Explicitly cast indices to int32 before take() to satisfy Metal kernels
        indices = self.idx_map_mx.astype(mx.int32)
        
        # Gather into (B, N, N) matrix using mx.take for safe indexing
        L = mx.take(L_flat_padded, indices, axis=1)
        return L

    def get_covariance(self, L: mx.array) -> mx.array:
        """Computes the covariance matrix Sigma = LL^T."""
        return mx.matmul(L, L.transpose(0, 2, 1))

class CoreMLJMCE:
    """
    Core ML wrapper for NeuralJMCE to utilize the ANE (Neural Engine).
    Bridges the high-velocity intraday requirements to dedicated NPU hardware.
    """
    def __init__(self, model_path: str, n_assets: int = 50):
        from backend.coreml_inference import CoreMLRunner
        self.runner = CoreMLRunner(model_path)
        self.n_assets = n_assets
        self._initialized = self.runner.load(model_path)
        
    def __call__(self, x: np.ndarray, return_velocity: bool = False) -> Tuple[np.ndarray, np.ndarray, Optional[np.ndarray]]:
        """
        Forward pass on ANE.
        Args:
            x: Return sequences (seq_len, n_assets)
        """
        if not self._initialized:
            raise RuntimeError("CoreML JMCE model not initialized or ANE unavailable")
            
        # CoreML model expects specific input keys
        # We assume the model was exported with 'returns' as input
        # and 'mu', 'L', 'V' as outputs
        inputs = {"returns": x.astype(np.float32)}
        outputs = self.runner.predict(inputs)
        
        mu = outputs.get("mu")
        L = outputs.get("L")
        V = outputs.get("V") if return_velocity else None
        
        return mu, L, V

def get_jmce_model(
    n_assets: int = 50, 
    use_ane: bool = True, 
    resolution: TimeResolution = TimeResolution.DAILY
) -> Any:
    """
    Factory to retrieve the best available JMCE implementation.
    Prioritizes ANE (CoreML) > GPU (MLX).
    """
    from backend.app_logging import setup_logging
    logger = setup_logging("jmce_factory")

    if use_ane:
        import os
        # Check multiple possible locations for pre-compiled CoreML models
        model_name = f"jmce_{n_assets}_{resolution.value}.mlmodel"
        possible_paths = [
            os.path.join("models", "coreml", model_name),
            os.path.join("backend", "models", "coreml", model_name),
            os.path.join("..", "models", "coreml", model_name)
        ]
        
        for model_path in possible_paths:
            if os.path.exists(model_path) and os.path.getsize(model_path) > 0:
                try:
                    logger.info(f"🚀 Loading JMCE on ANE (NPU) via {model_path}")
                    return CoreMLJMCE(model_path, n_assets=n_assets)
                except Exception as e:
                    logger.warning(f"Failed to load ANE model at {model_path}, trying next location: {e}")
        
        logger.warning(f"No valid CoreML model found for {model_name}. NPU acceleration (ANE) will not be used for JMCE.")
    
    # Default to MLX (GPU)
    logger.info(f"⚡ Loading NeuralJMCE on GPU (MLX) [Resolution: {resolution.value}]")
    return NeuralJMCE(n_assets=n_assets, resolution=resolution)
