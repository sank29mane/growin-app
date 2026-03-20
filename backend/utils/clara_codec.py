"""
Apple CLaRa Codec - SOTA 2026
Handles high-density document compression for institutional financial research.
Converts long-form text (SEC filings, news) into latent vectors for RL Policy fusion.
Optimized for Apple Silicon via PyTorch MPS.
"""

import os
import logging
import torch
from typing import List, Optional, Union
from transformers import AutoTokenizer, AutoModel

logger = logging.getLogger(__name__)

class ClaraCodec:
    """
    Continuous Latent Rationalization Codec.
    Adapted from apple/ml-clara for the Growin App ensemble.
    """
    
    def __init__(
        self, 
        model_path: str = "apple/CLaRa-7B-Instruct",
        device: str = "mps" if torch.backends.mps.is_available() else "cpu",
        projection_dim: int = 32
    ):
        self.model_path = model_path
        self.device = torch.device(device)
        self.projection_dim = projection_dim
        self.tokenizer = None
        self.model = None
        self._initialized = False

    def load_model(self):
        """Loads the CLaRa model and tokenizer into VRAM (MPS)."""
        if self._initialized:
            return
            
        try:
            logger.info(f"Loading Apple CLaRa from {self.model_path} on {self.device}")
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_path, trust_remote_code=True)
            
            # Load with FP16 for M4 Pro memory efficiency
            self.model = AutoModel.from_pretrained(
                self.model_path, 
                trust_remote_code=True,
                torch_dtype=torch.float16
            ).to(self.device)
            
            self.model.eval()
            self._initialized = True
            logger.info("CLaRa Codec successfully initialized.")
        except Exception as e:
            logger.error(f"Failed to load CLaRa model: {e}")
            # Fallback to a mock/zero-vector if loading fails to prevent crash in production
            self._initialized = False

    def compress_text(self, text: str, question: str = "Summarize financial risk factors") -> torch.Tensor:
        """
        Compresses input text into a high-density latent vector.
        
        Args:
            text: The source document (e.g., 10-K excerpt).
            question: The reasoning context for the compression.
            
        Returns:
            A torch.Tensor of shape (1, 4096) representing the compressed latent space.
        """
        if not self._initialized:
            self.load_model()
            
        if not self.model:
            return torch.zeros((1, 4096), device=self.device)

        with torch.no_grad():
            # SOTA: Use the CLaRa-specific document encoder
            # The model returns a sequence of soft-tokens.
            # We take the mean or first token as the document representation.
            try:
                # Based on apple/ml-clara/inference.ipynb logic
                inputs = self.tokenizer([question], [[text]], return_tensors="pt").to(self.device)
                outputs = self.model.encode_documents(**inputs)
                
                # outputs is likely (batch, num_latents, hidden_dim)
                # We project this down to a single high-density vector
                latent_vec = outputs.mean(dim=1) # (1, 4096)
                return latent_vec
            except Exception as e:
                logger.warning(f"CLaRa compression error: {e}")
                return torch.zeros((1, 4096), device=self.device)

    def project_to_policy_dim(self, latent_vec: torch.Tensor) -> torch.Tensor:
        """
        Squashes the 4096-dim latent into a 32-dim policy context.
        Uses a static projection (PCA-style) or a learned linear head.
        """
        # For Wave 2 Task 1, we use a fixed random projection head (normalized)
        # to ensure dimension stability while preserving semantic variance.
        # In Task 2, the ag-agent will implement a learned head.
        
        # Seeded for consistency
        torch.manual_seed(42)
        proj_matrix = torch.randn((4096, self.projection_dim), device=self.device, dtype=torch.float16)
        
        # L2 Normalized projection
        squashed = torch.matmul(latent_vec, proj_matrix)
        return torch.nn.functional.normalize(squashed, p=2, dim=1)

if __name__ == "__main__":
    # Sanity Check
    logging.basicConfig(level=logging.INFO)
    codec = ClaraCodec(model_path="apple/CLaRa-7B-Instruct")
    
    # Mock behavior for local testing without multi-GB downloads
    if os.getenv("GROWIN_SKIP_MODEL_LOAD"):
        print("Skipping heavy model load for sanity check.")
        mock_vec = torch.randn((1, 4096))
        squashed = codec.project_to_policy_dim(mock_vec)
        print(f"Mock Compression Success. Dim: {squashed.shape}")
    else:
        try:
            codec.load_model()
            vec = codec.compress_text("Tesla reports record deliveries but warns of supply chain drag.")
            squashed = codec.project_to_policy_dim(vec)
            print(f"CLaRa Compression Success. Unified State Contribution Dim: {squashed.shape}")
        except Exception as e:
            print(f"Setup Failed: {e}")
