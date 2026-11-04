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
    "NEWS", "URGENT", "LTP", "HELLO",
    # Common all-caps terms that are not tickers
    "ETF", "ETFS", "USD", "INR", "GBP", "EUR", "RSI", "SMA", "EMA", "MACD", "VAR", "ATR",
    "ADX", "OBV", "CCI", "OK", "AI", "IPO", "CEO", "CFO", "NSE", "BSE", "LSE", "NYSE",
    "SIP", "ISA", "SIPP", "KYC", "PAN", "EMI", "ROI", "EPS", "YTD", "ATH", "FII", "DII",
    "USA", "GDP", "CPI", "NAV", "AUM", "EBITDA", "FAQ", "ASAP", "FYI", "PDF", "FD", "INVEST",
    # Ampersand abbreviations. `&` stays legal in a symbol because NSE lists M&M, so
    # these are rejected by name rather than by the character.
    "P&L", "M&A", "R&D", "Q&A", "S&P",
}

# Ticker candidates: the base is 2-12 characters (NSE symbols run long: BAJAJFINSV,
# HDFCBANK, RELIANCE), optionally followed by an exchange suffix like .L or .NS.
_DOLLAR_TICKER_RE = re.compile(r"(?<![A-Za-z0-9$])\$([A-Za-z0-9][A-Za-z0-9&.-]*)")
_CAPS_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9$.])"
    r"([A-Z0-9][A-Z0-9&-]*(?:\.[A-Z]{1,2})?)"
    r"(?![A-Za-z0-9])"
)


def _validated_symbol(token: str, *, min_base: int, check_stop: bool) -> Optional[str]:
    """
    The one place a candidate symbol is judged. Returns the upper-cased symbol, or
    None. Rules: a whole token (nothing is truncated), a base of at most 12 chars
    that starts with a letter (or one digit then letters, as in 3GLD), not all
    digits, an optional 1-2 letter exchange suffix, and optionally not a stop word.
    """
    token = token.upper().rstrip(".-&")
    base, dot, suffix = token.partition(".")
    if dot and not re.fullmatch(r"[A-Z]{1,2}", suffix):
        return None
    if not re.fullmatch(r"[A-Z0-9&-]+", base) or base.isdigit():
        return None
    if base[0].isdigit():
        if len(base) < 4 or not re.fullmatch(r"\d[A-Z][A-Z0-9&-]*", base):
            return None
    elif not base[0].isalpha():
        return None
    if len(base) > 12 or len(base) < (min(min_base, 2) if suffix else min_base):
        return None
    if check_stop and (base in TICKER_STOP_WORDS or token in TICKER_STOP_WORDS):
        return None
    return token


def validate_ticker_symbol(token: Optional[str]) -> Optional[str]:
    """Judge a symbol that came from somewhere else (the coordinator's TICKER).
    Same rules as the chat heuristic, except a 2-letter base is allowed (GE, HP)."""
    if not token:
        return None
    return _validated_symbol(token, min_base=2, check_stop=True)


def extract_dollar_ticker(text: str) -> Optional[str]:
    """The first valid `$SYMBOL` in the text. Money like `$5000` or `$ABCDEFGHIJKLM` is skipped."""
    for match in _DOLLAR_TICKER_RE.finditer(text or ""):
        symbol = _validated_symbol(match.group(1), min_base=2, check_stop=False)
        if symbol:
            return symbol
    return None


def extract_ticker_from_text(text: str, find_last: bool = False) -> Optional[str]:
    """
    Heuristic ticker pick from a chat message.

    A `$`-prefixed symbol counts, and so does a token the user typed in capitals
    (TCS, RELIANCE, VOD.L, 3GLD). Ordinary words never qualify, whatever their
    length. Company names are not resolved here. The coordinator's TICKER output is
    the better signal and wins over this guess when they disagree.
    """
    if not text:
        return None

    dollar = extract_dollar_ticker(text)
    if dollar:
        return dollar

    for match in _CAPS_TOKEN_RE.finditer(text):
        symbol = _validated_symbol(match.group(1), min_base=3, check_stop=True)
        if symbol:
            return symbol
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
