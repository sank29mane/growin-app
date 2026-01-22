# Utils package
import math
from typing import Optional, Any

def extract_ticker_from_text(text: str) -> Optional[str]:
    """
    Extract ticker from text using simple word analysis.
    Looks for 3-5 letter alphabetic words.
    """
    words = text.upper().split()
    for word in words:
        if len(word) >= 3 and len(word) <= 5 and word.isalpha():
            return word
    return None

def sanitize_nan(obj: Any) -> Any:
    """
    Recursively walk through an object and replace NaN/Inf values with 0.0.
    This ensures JSON compatibility for FastAPI responses.
    """
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return 0.0
        return obj
    elif isinstance(obj, dict):
        return {k: sanitize_nan(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_nan(v) for v in obj]
    return obj

from .ane_detection import detect_ane_available
from .safe_python import SafePythonExecutor, run_safe_python, get_safe_executor