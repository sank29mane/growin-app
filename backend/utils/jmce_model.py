import numpy as np
try:
    import mlx.core as mx
    import mlx.nn as nn
    HAS_MLX = True
except ImportError:
    mx = None
    class DummyModule:
        def __init__(self, *args, **kwargs): pass
        def __call__(self, *args, **kwargs): pass
    class nn:
        Module = DummyModule
        @staticmethod
        def Linear(*args, **kwargs): return lambda x: x
    HAS_MLX = False

from typing import Tuple, Optional, Dict, Any, Union
from enum import Enum

class TimeResolution(Enum):
    DAILY = 'daily'
    INTRADAY_5MIN = '5min'
    INTRADAY_1MIN = '1min'

class NeuralJMCE(nn.Module):
    """
    Joint Mean-Covariance Estimator (JMCE) using a Transformer backbone.
    Optimized for Apple Silicon NPU via MLX.
    
    Predicts expected returns (mu) and the Cholesky factor (L) of the covariance matrix
    to ensure the resulting covariance is always Positive Definite.
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
        
        self.target_seq_len = seq_len
        if resolution == TimeResolution.INTRADAY_5MIN:
            self.target_seq_len = max(seq_len, 78)
        elif resolution == TimeResolution.INTRADAY_1MIN:
            self.target_seq_len = max(seq_len, 390)

        if HAS_MLX:
            self.input_proj = nn.Linear(n_assets, d_model)
            self.error_proj = nn.Linear(n_assets, d_model)
            self.pos_emb = mx.random.normal((self.target_seq_len, d_model)) * 0.02
            self.transformer = nn.TransformerEncoder(
                num_layers=n_layers,
                dims=d_model,
                num_heads=n_heads,
                mlp_dims=d_model * 4
            )
            self.mu_head = nn.Linear(d_model, n_assets)
            self.cholesky_head = nn.Linear(d_model, self.cholesky_size)
            self.velocity_head = nn.Linear(d_model, self.cholesky_size)
            self.fourier_phase_shift = mx.zeros((n_assets, 2))
        else:
            self.input_proj = None
            self.error_proj = None
            self.pos_emb = None
            self.transformer = None
            self.mu_head = None
            self.cholesky_head = None
            self.velocity_head = None
            self.fourier_phase_shift = None
        
        self._init_cholesky_logic(n_assets)

    def _init_cholesky_logic(self, n_assets: int):
        diag_indices = []
        idx_map = np.full((n_assets, n_assets), -1, dtype=np.int32)
        k = 0
        for i in range(n_assets):
            for j in range(i + 1):
                idx_map[i, j] = k
                if i == j:
                    diag_indices.append(k)
                k += 1
        self.diag_mask = np.zeros(self.cholesky_size, dtype=np.float32)
        self.diag_mask[diag_indices] = 1.0
        
        idx_map[idx_map == -1] = self.cholesky_size
        if HAS_MLX:
            self.diag_mask = mx.array(self.diag_mask)
            self.idx_map_mx = mx.array(idx_map, dtype=mx.uint32)
        else:
            self.idx_map_mx = None

    def _apply_fourier_shift(self, mu: 'mx.array') -> 'mx.array':
        shift = self.fourier_phase_shift
        sin_phi = mx.tanh(shift[:, 0]) * 0.1
        cos_phi = mx.sqrt(1.0 - sin_phi**2)
        return mu * cos_phi - sin_phi

    def __call__(
        self, 
        x: 'mx.array', 
        error_vector: Optional['mx.array'] = None,
        return_velocity: bool = False
    ) -> Tuple['mx.array', 'mx.array', Optional['mx.array']]:
        """
        Forward pass.
        Args:
            x: Return sequences of shape (batch, seq_len, n_assets)
            error_vector: Optional error sequences
            return_velocity: Whether to return covariance velocity
        Returns:
            mu: Expected returns (batch, n_assets)
            L: Cholesky factor (batch, n_assets, n_assets)
            V: Optional covariance velocity
        """
        B, S, N = x.shape
        x_lat = self.input_proj(x)
        if error_vector is not None:
            err_lat = self.error_proj(error_vector)
            x_lat = mx.concatenate([x_lat, err_lat], axis=1)
            x_lat = x_lat[:, :self.target_seq_len, :]
            x_lat = x_lat + self.pos_emb[:x_lat.shape[1]]
        else:
            x_lat = x_lat + self.pos_emb[:S]
        x_trans = self.transformer(x_lat, mask=None)
        x_final = x_trans[:, -1, :]
        mu = self.mu_head(x_final)
        mu = self._apply_fourier_shift(mu)
        L_flat = self.cholesky_head(x_final)
        L = self._build_cholesky(L_flat)
        V = None
        if return_velocity:
            V_flat = self.velocity_head(x_final)
            V = self._build_cholesky(V_flat)
        return mu, L, V

    def _build_cholesky(self, L_flat: 'mx.array') -> 'mx.array':
        """
        Reconstructs the lower-triangular L matrix and ensures the diagonal is positive.
        Sigma = L * L^T is guaranteed to be Positive Definite.
        """
        # Ensure diagonal elements are positive to guarantee PD covariance
        L_flat = L_flat * (1.0 - self.diag_mask) + mx.exp(L_flat) * self.diag_mask
        B = L_flat.shape[0]
        zeros = mx.zeros((B, 1))
        L_flat_padded = mx.concatenate([L_flat, zeros], axis=1)
        indices = self.idx_map_mx.astype(mx.int32)
        L = mx.take(L_flat_padded, indices, axis=1)
        return L

    def get_covariance(self, L: 'mx.array') -> 'mx.array':
        """Computes the covariance matrix Sigma = LL^T."""
        return mx.matmul(L, L.transpose(0, 2, 1))

class CoreMLJMCE:
    def __init__(self, model_path: str, n_assets: int = 50):
        from backend.coreml_inference import CoreMLRunner
        self.runner = CoreMLRunner(model_path)
        self.n_assets = n_assets
        self._initialized = self.runner.load(model_path)
    def __call__(self, x: np.ndarray, error_vector: Optional[np.ndarray] = None, return_velocity: bool = False) -> Tuple[np.ndarray, np.ndarray, Optional[np.ndarray]]:
        if not self._initialized:
            raise RuntimeError('CoreML JMCE model not initialized')
        inputs = {'returns': x.astype(np.float32)}
        if error_vector is not None:
            inputs['error_vector'] = error_vector.astype(np.float32)
        outputs = self.runner.predict(inputs)
        mu = outputs.get('mu')
        L = outputs.get('L')
        V = outputs.get('V') if return_velocity else None
        return mu, L, V

def get_jmce_model(n_assets: int = 50, use_ane: bool = True, resolution: TimeResolution = TimeResolution.DAILY) -> Any:
    from backend.app_logging import setup_logging
    logger = setup_logging('jmce_factory')
    if use_ane:
        import os
        model_name = f'jmce_{n_assets}_{resolution.value}.mlmodel'
        possible_paths = [os.path.join('models', 'coreml', model_name), os.path.join('backend', 'models', 'coreml', model_name)]
        for model_path in possible_paths:
            if os.path.exists(model_path) and os.path.getsize(model_path) > 0:
                try:
                    logger.info(f'🚀 Loading JMCE on ANE (NPU) via {model_path}')
                    return CoreMLJMCE(model_path, n_assets=n_assets)
                except Exception as e:
                    logger.warning(f'Failed to load ANE model at {model_path}: {e}')
    logger.info(f'⚡ Loading NeuralJMCE on GPU (MLX) [Resolution: {resolution.value}]')
    return NeuralJMCE(n_assets=n_assets, resolution=resolution)
