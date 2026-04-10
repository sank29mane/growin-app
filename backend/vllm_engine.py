"""VLLM-powered inference engine for Apple Silicon optimized models using vllm-mlx."""
import logging
import asyncio
from typing import Optional, AsyncIterator, Any, Dict, List, Union

try:
    from vllm_mlx.engine.batched import BatchedEngine
    from vllm_mlx.scheduler import SchedulerConfig
    HAS_VLLM_MLX = True
except ImportError:
    HAS_VLLM_MLX = False

logger = logging.getLogger(__name__)

class VLLMInferenceEngine:
    """
    Wrapper for vllm-mlx BatchedEngine.
    Provides a consistent interface for high-throughput local inference.
    
    Optimized for:
    - Gemma 4, Nemotron 3 MoE models
    - Continuous batching (PagedAttention)
    - Apple Silicon Unified Memory
    """
    
    def __init__(self):
        self.engine: Optional[Any] = None
        self.current_model_path: Optional[str] = None
        self._loading = False

    async def load_model(self, model_path: str, max_num_seqs: int = 16, **kwargs) -> bool:
        """
        Load a model into the batched engine.
        
        Args:
            model_path: Path to the model or HuggingFace ID.
            max_num_seqs: Maximum number of concurrent sequences for continuous batching.
            **kwargs: Additional arguments for BatchedEngine (e.g., force_mllm=False).
        """
        if not HAS_VLLM_MLX:
            logger.error("vllm-mlx not installed. Cannot load VLLM engine.")
            return False

        if self._loading:
            return False
            
        self._loading = True
        try:
            logger.info(f"🚀 Loading vllm-mlx BatchedEngine: {model_path}")
            
            # SOTA 2026: Enable PagedAttention and memory-aware caching
            scheduler_config = SchedulerConfig(
                max_num_seqs=max_num_seqs,
                use_paged_cache=True,  # Requested PagedAttention
                use_memory_aware_cache=True,
                cache_memory_percent=0.25  # Use 25% of available RAM for KV cache
            )
            
            # Initialize engine
            self.engine = BatchedEngine(
                model_name=model_path,
                scheduler_config=scheduler_config,
                trust_remote_code=True,
                **kwargs
            )
            
            # Start engine (loads model)
            await self.engine.start()
            self.current_model_path = model_path
            
            logger.info(f"✅ vLLM Engine started: {model_path}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to load vLLM {model_path}: {e}")
            self.engine = None
            self.current_model_path = None
            return False
        finally:
            self._loading = False

    async def generate(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.7,
        top_p: float = 0.9,
    ) -> str:
        """
        Generate text response.
        """
        if self.engine is None:
            raise RuntimeError("vLLM model not loaded. Call load_model() first.")
            
        try:
            output = await self.engine.generate(
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p
            )
            return output.text
        except Exception as e:
            logger.error(f"vLLM generate error: {e}")
            raise

    async def stream_generate(
        self,
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.7,
        top_p: float = 0.9,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Stream text response.
        Yields dict with 'text' (new) and 'tokens' (total).
        """
        if self.engine is None:
            raise RuntimeError("vLLM model not loaded. Call load_model() first.")
            
        try:
            async for output in self.engine.stream_generate(
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p
            ):
                yield {
                    "text": output.new_text,
                    "tokens": output.completion_tokens,
                    "finished": output.finished
                }
        except Exception as e:
            logger.error(f"vLLM stream_generate error: {e}")
            raise

    async def stop(self):
        """Stop the engine and release resources."""
        if self.engine:
            await self.engine.stop()
            self.engine = None
            self.current_model_path = None
            logger.info("❄️ vLLM Engine stopped.")

    def is_loaded(self) -> bool:
        return self.engine is not None and getattr(self.engine, "_loaded", False)

# Global singleton
_vllm_engine: Optional[VLLMInferenceEngine] = None

def get_vllm_engine() -> VLLMInferenceEngine:
    global _vllm_engine
    if _vllm_engine is None:
        _vllm_engine = VLLMInferenceEngine()
    return _vllm_engine
