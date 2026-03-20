"""
Clara Agent - Semantic Fusion for RL Policy
Manages high-density latent memories using Apple CLaRa Codec.
Reduces 4096-dim latent vectors to 32-dim policy context via MLX Linear Layer.
"""

import logging
import numpy as np
from typing import Optional, Union

try:
    import mlx.core as mx
    import mlx.nn as nn
    HAS_MLX = True
except ImportError:
    mx = None
    nn = None
    HAS_MLX = False

from backend.utils.clara_codec import ClaraCodec

logger = logging.getLogger(__name__)

# Conditional base class
BaseModule = nn.Module if HAS_MLX else object

class ProjectionHead(BaseModule):
    """
    Learned Projection Head to reduce high-density latent memories.
    Reduces 4096-dim (CLaRa) vectors to 32-dim policy context.
    """
    def __init__(self, input_dim: int = 4096, output_dim: int = 32):
        if HAS_MLX:
            super().__init__()
            # MLX Linear layer
            self.proj = nn.Linear(input_dim, output_dim)
        else:
            self.proj = None
        
    def __call__(self, x):
        if not HAS_MLX:
            return x
        # Project and normalize for policy stability
        x = self.proj(x)
        # Normalize (L2)
        norm = mx.linalg.norm(x, keepdims=True)
        return x / (norm + 1e-8)

class ClaraAgent:
    """
    Agent responsible for Semantic Fusion.
    Converts financial text into high-density latent signals for the RL State.
    """
    def __init__(self, model_path: str = "apple/CLaRa-7B-Instruct"):
        self.codec = ClaraCodec(model_path=model_path)
        self.projection_head = ProjectionHead(input_dim=4096, output_dim=32)
        self._initialized = False

    def load(self):
        """Lazy loader for the heavy CLaRa model."""
        if not self._initialized:
            try:
                self.codec.load_model()
                self._initialized = True
            except Exception as e:
                logger.error(f"Failed to load ClaraAgent: {e}")

    def get_context_vector(self, text: str, question: str = "Summarize financial risk factors"):
        """
        Compresses text and projects it to a 32-dim semantic vector.
        
        Returns:
            A 32-dim MLX array (if MLX available) or NumPy array.
        """
        try:
            # 1. Compress text to 4096-dim latent vector (PyTorch Tensor)
            # This handles model loading internally if not initialized
            latent_tensor = self.codec.compress_text(text, question)
            
            # 2. Convert to NumPy for bridge
            latent_np = latent_tensor.detach().cpu().numpy().astype(np.float32)
            
            # 3. Project to 32-dim using MLX Head
            if HAS_MLX:
                # Ensure it is (1, 4096)
                if len(latent_np.shape) == 1:
                    latent_np = latent_np[np.newaxis, :]
                
                latent_mx = mx.array(latent_np)
                context_vec = self.projection_head(latent_mx)
                return context_vec
            else:
                # Fallback: Simple random projection if MLX is missing
                np.random.seed(42)
                proj = np.random.randn(4096, 32).astype(np.float32)
                # Ensure it is (1, 4096)
                if len(latent_np.shape) == 1:
                    latent_np = latent_np[np.newaxis, :]
                context_vec = latent_np @ proj
                norm = np.linalg.norm(context_vec, axis=-1, keepdims=True)
                return context_vec / (norm + 1e-8)
                
        except Exception as e:
            logger.error(f"ClaraAgent context vector error: {e}")
            if HAS_MLX:
                return mx.zeros((1, 32))
            return np.zeros((1, 32), dtype=np.float32)

if __name__ == "__main__":
    # Sanity Check
    logging.basicConfig(level=logging.INFO)
    agent = ClaraAgent()
    
    # Mock text
    test_text = "NVIDIA reports 200% growth in data center revenue but warns of export curbs."
    
    # Test with mock behavior if model loading skipped
    import os
    os.environ["GROWIN_SKIP_MODEL_LOAD"] = "1"
    
    vec = agent.get_context_vector(test_text)
    print(f"Semantic Vector Shape: {vec.shape}")
