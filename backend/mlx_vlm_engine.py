"""MLX-powered VLM inference engine for Apple Silicon."""
import logging
import asyncio
from typing import Optional, Any, Dict, List, Tuple, Union
import mlx.core as mx
from PIL import Image

from mlx_engine import get_memory_info, MEMORY_WARNING_THRESHOLD

logger = logging.getLogger(__name__)

class MLXVLMInferenceEngine:
    """
    Handles MLX VLM model loading and inference.
    
    Optimized for:
    - Qwen2.5-VL series
    - Metal acceleration on Apple Silicon
    - Efficient image token handling
    """
    
    def __init__(self):
        self.model = None
        self.processor = None
        self.current_model_path: Optional[str] = None
        self._loading = False
        
    def load_model(self, model_path: str = "mlx-community/Qwen2.5-VL-7B-Instruct-4bit") -> bool:
        """
        Load a VLM model using mlx-vlm.
        """
        if self._loading:
            return False
            
        self._loading = True
        try:
            from mlx_vlm import load
            
            # Check memory
            mem_info = get_memory_info()
            if mem_info["warning"]:
                logger.warning(f"High memory usage: {mem_info['used_percent']}%")
                
            logger.info(f"Loading MLX VLM model: {model_path}")
            
            # Unload previous
            if self.model is not None:
                self.unload()
                
            self.model, self.processor = load(model_path)
            self.current_model_path = model_path
            
            # Warmup
            self._warmup()
            
            logger.info(f"Successfully loaded VLM: {model_path}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to load VLM {model_path}: {e}")
            self.model = None
            self.processor = None
            return False
        finally:
            self._loading = False

    def _warmup(self):
        """Warm up the model parameters."""
        try:
            mx.async_eval(self.model.parameters())
            # For VLM, we might want a simple image + text warmup if possible,
            # but parameter eval is a good start.
            mx.eval(self.model.parameters())
        except Exception as e:
            logger.warning(f"VLM warmup failed: {e}")

    async def generate(
        self,
        image: Union[Image.Image, List[Image.Image]],
        prompt: str,
        max_tokens: int = 512,
        temperature: float = 0.0,
        verbose: bool = False
    ) -> str:
        """
        Generate text response for given image(s) and prompt.

        Args:
            image: PIL Image or list of PIL Images
            prompt: Text prompt
            max_tokens: Max tokens to generate
            temperature: Sampling temperature

        Returns:
            Generated text response
        """
        if self.model is None or self.processor is None:
            raise RuntimeError("VLM model not loaded. Call load_model() first.")
            
        try:
            from mlx_vlm import generate
            from mlx_vlm.utils import prepare_inputs
            
            if not isinstance(image, list):
                images = [image]
            else:
                images = image
                
            logger.info(f"VLM generating for {len(images)} image(s) with prompt: {prompt[:50]}...")
            
            # Wrap blocking generate in thread
            response = await asyncio.to_thread(
                generate,
                self.model,
                self.processor,
                prompt=prompt,
                image=images,
                max_tokens=max_tokens,
                temperature=temperature,
                verbose=verbose
            )
            
            return response
            
        except Exception as e:
            logger.error(f"VLM generation error: {e}")
            raise

    def unload(self):
        """Free VLM from memory."""
        if self.model is not None:
            del self.model
            del self.processor
            self.model = None
            self.processor = None
            self.current_model_path = None
            mx.metal.clear_cache()
            logger.info("VLM unloaded")

    def is_loaded(self) -> bool:
        return self.model is not None

# Global singleton
_vlm_engine: Optional[MLXVLMInferenceEngine] = None

def get_vlm_engine() -> MLXVLMInferenceEngine:
    global _vlm_engine
    if _vlm_engine is None:
        _vlm_engine = MLXVLMInferenceEngine()
    return _vlm_engine
