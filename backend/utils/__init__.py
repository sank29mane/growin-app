# Utils package
import math
from typing import Optional, Any

# Common words to exclude from ticker extraction
TICKER_STOP_WORDS = {
    "ANALYZE", "CHECK", "PRICE", "STOCK", "SHARE", "ABOUT", "WHAT", "HOW", "WHEN",
    "PORTFOLIO", "ANALYSIS", "MARKET", "GENERAL", "REPORT", "SUMMARY", "UPDATE",
    "SHOW", "TELL", "GIVE", "FIND", "THIS", "THAT", "WITH", "FROM", "YOUR", "THEY",
    "DOES", "WANT", "NEED", "LIKE", "LOOK", "STOC", "DATA", "REAL", "USER", "SURE",
    "HELP", "LIST", "TYPE", "CODE", "READ", "FILE", "VIEW", "EDIT", "TOOL", "CALL",
    "NAME", "ARGS", "INFO", "API", "LOAD", "SAVE", "BEST", "GOOD", "TIME", "YEAR",
    "MONTH", "WEEK", "DAY", "HOUR", "MIN", "SEC", "ALL", "NONE", "NULL", "TRUE", "FALSE"
}

def extract_ticker_from_text(text: str) -> Optional[str]:
    """
    Extract ticker from text using simple word analysis.
    Looks for 3-5 letter alphabetic words.
    """
    if not text:
        return None
        
    # Check for $TICKER format first
    import re
    dollar_match = re.search(r'\$([A-Z]{2,6})', text.upper())
    if dollar_match:
        return dollar_match.group(1)

    words = text.upper().split()
    for word in words:
        # Clean word from punctuation
        clean_word = "".join(ch for ch in word if ch.isalnum())
        if len(clean_word) >= 3 and len(clean_word) <= 5 and clean_word.isalpha():
            if clean_word not in TICKER_STOP_WORDS:
                return clean_word
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