"""Utility for image preprocessing for VLM consumption."""
import io
import logging
from typing import Tuple, Union, Optional
from PIL import Image

logger = logging.getLogger(__name__)

def prepare_vlm_image(
    image_path_or_bytes: Union[str, bytes],
    max_size: int = 1024,
    normalize: bool = True
) -> Tuple[Image.Image, Tuple[int, int]]:
    """
    Load, resize (preserving aspect ratio), and optionally normalize an image for VLM.
    
    Args:
        image_path_or_bytes: Path to image file or raw bytes.
        max_size: Maximum dimension (width or height) to resize to.
        normalize: Whether to perform normalization (currently returns PIL Image).
        
    Returns:
        Tuple of (Processed PIL Image, Original dimensions (width, height))
    """
    try:
        if isinstance(image_path_or_bytes, bytes):
            img = Image.open(io.BytesIO(image_path_or_bytes))
        else:
            img = Image.open(image_path_or_bytes)
            
        # Ensure RGB
        if img.mode != "RGB":
            img = img.convert("RGB")
            
        original_size = img.size
        width, height = original_size
        
        # Calculate new dimensions preserving aspect ratio
        if max(width, height) > max_size:
            if width > height:
                new_width = max_size
                new_height = int(height * (max_size / width))
            else:
                new_height = max_size
                new_width = int(width * (max_size / height))
                
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            logger.debug(f"Resized image from {original_size} to {img.size}")
        
        return img, original_size
        
    except Exception as e:
        logger.error(f"Failed to prepare VLM image: {e}")
        raise

def get_image_bytes(img: Image.Image, format: str = "JPEG") -> bytes:
    """Convert PIL Image to bytes."""
    buf = io.BytesIO()
    img.save(buf, format=format)
    return buf.getvalue()
