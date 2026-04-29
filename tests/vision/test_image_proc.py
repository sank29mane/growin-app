"""Tests for image preprocessing utility."""
import io
import pytest
from PIL import Image
from backend.utils.image_proc import prepare_vlm_image, prepare_vlm_image_async

def create_test_image(width: int, height: int) -> bytes:
    """Create a test image and return as bytes."""
    img = Image.new("RGB", (width, height), color="red")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()

def test_prepare_vlm_image_resize():
    """Test that image is resized while preserving aspect ratio."""
    width, height = 2000, 1000
    img_bytes = create_test_image(width, height)
    
    max_size = 1024
    processed_img, original_size = prepare_vlm_image(img_bytes, max_size=max_size)
    
    assert original_size == (width, height)
    assert max(processed_img.size) == max_size
    
    # Check aspect ratio preservation: 2000/1000 = 2.0
    # 1024 / new_height = 2.0 => new_height = 512
    assert processed_img.size == (1024, 512)

def test_prepare_vlm_image_no_resize():
    """Test that small images are not resized."""
    width, height = 500, 400
    img_bytes = create_test_image(width, height)
    
    processed_img, original_size = prepare_vlm_image(img_bytes, max_size=1024)
    
    assert original_size == (width, height)
    assert processed_img.size == (width, height)

def test_prepare_vlm_image_mode_conversion():
    """Test that images are converted to RGB."""
    # Create RGBA image
    img = Image.new("RGBA", (100, 100), color=(255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    img_bytes = buf.getvalue()
    
    processed_img, _ = prepare_vlm_image(img_bytes)
    assert processed_img.mode == "RGB"

@pytest.mark.asyncio
async def test_prepare_vlm_image_async():
    """Test the asynchronous wrapper."""
    width, height = 2000, 1000
    img_bytes = create_test_image(width, height)

    max_size = 1024
    processed_img, original_size = await prepare_vlm_image_async(img_bytes, max_size=max_size)

    assert original_size == (width, height)
    assert max(processed_img.size) == max_size
    assert processed_img.size == (1024, 512)
