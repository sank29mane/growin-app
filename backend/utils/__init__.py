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
    "MONTH", "WEEK", "DAY", "HOUR", "MIN", "SEC", "ALL", "NONE", "NULL", "TRUE", "FALSE",
    "THINK", "VERY", "SOME", "MANY", "COULD", "WOULD", "SHOULD", "THEIR", "THERE", "THESE",
    "IS", "ARE", "WAS", "WERE", "BE", "BEEN", "BEING", "FOR", "AND", "BUT", "OR", "YET", "SO",
    "PLEASE", "DO", "CAN", "THANKS", "THANK", "YOU", "ME", "MY", "I", "WE", "US", "OUR", "IN", "TO"
}

def extract_ticker_from_text(text: str, find_last: bool = False) -> Optional[str]:
    """
    Extract ticker from text using enhanced regex analysis.
    Supports $TICKER, TICKER.L, and alphanumeric symbols like 3GLD.
    """
    if not text:
        return None
        
    import re
    
    # 1. Check for $TICKER format (Strongest signal)
    dollar_match = re.search(r'\$([A-Z0-9.]{2,10})\b', text.upper())
    if dollar_match:
        return dollar_match.group(1)
        
    # 2. Extract potential candidates (words containing uppercase letters and numbers/dots)
    # We look for words that are 2-8 chars, start with a letter/number, and aren't all digits
    candidates = re.findall(r'\b([A-Z0-9.]{2,8})\b', text.upper())
    
    # We want to find the LAST candidate that isn't a stop word (for history resolution)
    # but for general extraction we find the FIRST.
    # To support the Mixed test, we'll return all and let the caller decide?
    # No, the utility should ideally return the most likely one.
    
    # For extraction, return matches that aren't stop words and aren't pure numbers
    valid_candidates = []
    for cand in candidates:
        # Filter out pure numbers
        if cand.isdigit():
            continue
        # Filter out stop words
        if cand in TICKER_STOP_WORDS:
            continue
        # Length check (2-6 usually, up to 8 for UK/Leveraged)
        if len(cand) < 2:
            continue
        valid_candidates.append(cand)
        
    if valid_candidates:
        return valid_candidates[-1] if find_last else valid_candidates[0]
        
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