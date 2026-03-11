try:
    import mlx.core as mx
    import mlx.nn as nn
except ImportError:
    mx = None
    nn = None
import numpy as np
from typing import Tuple, Optional, Any

class DummyModule:
    pass

class NeuralJMCE(nn.Module if nn else DummyModule):
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
        seq_len: int = 180
    ):
        super().__init__()
        self.n_assets = n_assets
        self.d_model = d_model
        self.cholesky_size = (n_assets * (n_assets + 1)) // 2
        
        # Input projection: daily returns for all assets -> d_model latent space
        self.input_proj = nn.Linear(n_assets, d_model)
        
        # Positional encoding for sequence length (learned)
        self.pos_emb = mx.random.normal((seq_len, d_model)) * 0.02
        
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
        self.idx_map_mx = mx.array(idx_map)

    def __call__(self, x: Any) -> Tuple[Any, Any]:
        """
        Forward pass.
        Args:
            x: Return sequences of shape (batch, seq_len, n_assets)
        Returns:
            mu: Expected returns (batch, n_assets)
            L: Cholesky factor (batch, n_assets, n_assets)
        """
        B, S, N = x.shape
        if N != self.n_assets:
            # Handle mismatch or padding if necessary. 
            # For now, we enforce consistency or expect the user to slice.
            pass
            
        # 1. Project to latent space
        x = self.input_proj(x)
        
        # 2. Add Positional Embedding
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
        
        return mu, L

    def _build_cholesky(self, L_flat: Any) -> Any:
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
        
        # Gather into (B, N, N) matrix
        L = L_flat_padded[:, self.idx_map_mx]
        return L

    def get_covariance(self, L: Any) -> Any:
        """Computes the covariance matrix Sigma = LL^T."""
        return mx.matmul(L, L.transpose(0, 2, 1))
