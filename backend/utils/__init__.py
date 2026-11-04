# Utils package
import math
import re
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
    "YOU", "THINK", "COMPARE", "VERSUS", "BETWEEN", "DEEP", "DIVE", "MORE", "SOME", "ANY",
    # Trading verbs and filler people shout in capitals
    "BUY", "SELL", "HOLD", "SHORT", "LONG", "SHOULD", "WOULD", "COULD", "NOW", "RIGHT",
    "STOP", "LIMIT", "ORDER", "TRADE", "SWING", "MARGIN", "THE", "AND", "FOR", "NOT",
    "CAN", "WILL", "HAVE", "ARE", "WAS", "WHY", "WHO", "WHICH", "PLEASE", "THANKS",
    # Common all-caps terms that are not tickers
    "ETF", "ETFS", "USD", "INR", "GBP", "EUR", "RSI", "SMA", "EMA", "MACD", "VAR", "ATR",
    "ADX", "OBV", "CCI", "OK", "AI", "IPO", "CEO", "CFO", "NSE", "BSE", "LSE", "NYSE",
    "SIP", "ISA", "SIPP", "KYC", "PAN", "EMI", "ROI", "EPS", "YTD", "ATH", "FII", "DII",
    "USA", "GDP", "CPI", "NAV", "AUM", "EBITDA", "FAQ", "ASAP", "FYI", "PDF", "FD", "INVEST",
}

# Ticker candidates must be written in capitals by the user. The base is 2-12
# characters (NSE symbols run long: BAJAJFINSV, HDFCBANK, RELIANCE), optionally
# followed by an exchange suffix like .L or .NS.
_DOLLAR_TICKER_RE = re.compile(r"\$([A-Za-z0-9][A-Za-z0-9&-]{0,11}(?:\.[A-Za-z]{1,2})?)")
_CAPS_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9$.])"
    r"([A-Z0-9][A-Z0-9&-]*(?:\.[A-Z]{1,2})?)"
    r"(?![A-Za-z0-9])"
)


def extract_ticker_from_text(text: str, find_last: bool = False) -> Optional[str]:
    """
    Heuristic ticker pick from a chat message.

    Only tokens the user typed in capitals count (TCS, RELIANCE, VOD.L, 3GLD),
    or a `$`-prefixed symbol. Ordinary words never qualify, whatever their
    length. Company names are not resolved here. The coordinator's TICKER
    output is the better signal and wins over this guess when they disagree.
    """
    if not text:
        return None

    dollar_match = _DOLLAR_TICKER_RE.search(text)
    if dollar_match:
        return dollar_match.group(1).upper().rstrip("-&")

    for match in _CAPS_TOKEN_RE.finditer(text):
        token = match.group(1).rstrip("-&")
        base, _, suffix = token.partition(".")
        if not any(c.isalpha() for c in base) or base.isdigit():
            continue
        # One leading digit at most (3GLD, 5QQQ); rules out 52WK, 30000RS.
        if base[0].isdigit() and not re.fullmatch(r"\d[A-Z][A-Z0-9&-]*", base):
            continue
        if len(base) > 12 or len(base) < (2 if suffix else 3):
            continue
        if base in TICKER_STOP_WORDS or token in TICKER_STOP_WORDS:
            continue
        return token
    return None

def sanitize_nan(obj: Any) -> Any:
    """
    Recursively walk through an object and replace NaN/Inf values with 0.0.
    Also converts Decimal to float for JSON compatibility with Swift client.
    """
    from decimal import Decimal
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return 0.0
        return obj
    elif isinstance(obj, Decimal):
        return float(obj)
    elif isinstance(obj, dict):
        return {k: sanitize_nan(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_nan(v) for v in obj]
    return obj

from .ane_detection import detect_ane_available
from .safe_python import SafePythonExecutor, run_safe_python, get_safe_executor
