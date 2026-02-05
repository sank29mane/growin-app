"""
Ticker Normalization Utilities
Shared logic for resolving ticker discrepancies between T212, Yahoo Finance, and Alpaca.
"""

import re
from typing import Dict, List, Optional, Tuple

# Bolt Optimization: Import optional dependencies at module level to avoid repeated ImportErrors (PR #48)
try:
    import growin_core
    GROWIN_CORE_AVAILABLE = True
except ImportError:
    growin_core = None
    GROWIN_CORE_AVAILABLE = False

# --- Ticker Normalization Constants ---

SPECIAL_MAPPINGS = {
    "SSLNL": "SSLN", "SGLNL": "SGLN", "3GLD": "3GLD", "SGLN": "SGLN",
    "PHGP": "PHGP", "PHAU": "PHAU", "3LTS": "3LTS", "3USL": "3USL",
    "LLOY1": "LLOY", "VOD1": "VOD", "BARC1": "BARC", "TSCO1": "TSCO",
    "BPL1": "BP", "BPL": "BP", # BP.L
    "AZNL1": "AZN", "AZNL": "AZN", # Astrazeneca
    "SGLN1": "SGLN",
    "MAG5": "MAG5", "MAG5L": "MAG5",
    "MAG7": "MAG7", "MAG7L": "MAG7",
    "GLD3": "GLD3",
    "3UKL": "3UKL",
    "5QQQ": "5QQQ",
    "TSL3": "TSL3",
    "NVD3": "NVD3",
    "AVL": "AV",   # Aviva
    "UUL": "UU",   # United Utilities
    "BAL": "BA",   # BAE Systems (BA.L)
    "SLL": "SL",   # Standard Life / Segro? (Check context usually SL.L)
    "AU": "AUT",   # Auto Trader? Or Au (Gold)? Assuming AUT for AU.L usually.
    "REL": "REL",  # RELX (REL.L) - Keep as is
    "AAL": "AAL",  # Anglo American (AAL.L) - Keep as is
    "RBL": "RKT",  # Reckitt Benckiser
    "MICCL": "MICC", # Midwich Group (MICC.L)
}

US_EXCLUSIONS = {
    # Tech & Growth
    "AAPL", "MSFT", "GOOG", "AMZN", "NVDA", "TSLA", "META", "NFLX",
    "AMD", "INTC", "PYPL", "ADBE", "CSCO", "PEP", "COST", "AVGO", "QCOM", "TXN",
    "ORCL", "CRM", "IBM", "UBER", "ABNB", "SNOW", "PLTR", "SQ", "SHOP", "SPOT",
    "GOOGL", # Explicitly exclude GOOGL
    # New additions from PR #37
    "SMCI", "MSTR", "COIN", "HOOD", "ARM", "DKNG", "SOFI", "MARA", "RIOT",
    "CRWD", "PANW", "NET", "DDOG", "ZS", "TEAM", "MDB", "OKTA", "DOCU",

    # Financials
    "JPM", "BAC", "WFC", "C", "GS", "MS", "BLK", "AXP", "V", "MA", "COF", "USB",

    # Industrial & Auto
    "CAT", "DE", "GE", "GM", "F", "BA", "LMT", "RTX", "HON", "UPS", "FDX", "UNP", "MMM",

    # Consumer
    "WMT", "TGT", "HD", "LOW", "MCD", "SBUX", "NKE", "KO", "PEP", "PG", "CL", "MO", "PM", "DIS", "CMCSA",

    # Healthcare
    "JNJ", "PFE", "MRK", "ABBV", "LLY", "UNH", "CVS", "AMGN", "GILD", "BMY", "ISRG", "TMO", "ABT", "DHR",

    # Energy
    "XOM", "CVX", "COP", "SLB", "EOG", "OXY", "KMI", "HAL",

    # Telecom
    "T", "VZ", "TMUS",

    # ETFs
    "SPY", "QQQ", "DIA", "IWM", "IVV", "VOO", "VTI", "GLD", "SLV", "ARKK", "SMH", "XLF", "XLE", "XLK", "XLV",

    # Single Letter US Tickers
    "F", "T", "C", "V", "Z", "O", "D", "R", "K", "X", "S", "M", "A", "G"
}

LEVERAGED_STEMS = tuple(["LLOY", "BARC", "VOD", "HSBA", "TSCO", "BP", "AZN", "RR", "NG", "SGLN", "SSLN"])

def normalize_ticker(ticker: str) -> str:
    """
    SOTA Ticker Normalization: Resolves discrepancies between Trading212, 
    Yahoo Finance, Alpaca, and Finnhub via Rust-optimized core.
    """
    if GROWIN_CORE_AVAILABLE:
        try:
            return growin_core.normalize_ticker(ticker)
        except Exception:
            # Fallback if Rust binding fails even if module exists
            pass

    # Fallback to robust Python logic if Rust fails or is missing
    if not ticker:
        return ""

    # 1. Basic Cleaning
    ticker = ticker.upper().strip().replace("$", "")
    
    # 2. Already Normalized (contains dot)
    if "." in ticker:
        return ticker

    # 3. Handle Platform-Specific Artifacts
    original = ticker
    # Strip T212 suffixes (handles multiple like _US_EQ)
    ticker = re.sub(r'(_EQ|_US|_BE|_DE|_GB|_FR|_NL|_ES|_IT)+$', '', ticker)
    ticker = ticker.replace("_", "") # Fallback for messy underscores
    
    # 4. SPECIAL MAPPINGS (SOTA curated list for T212 -> YFinance)
    if ticker in SPECIAL_MAPPINGS:
        ticker = SPECIAL_MAPPINGS[ticker]

    # 5. Suffix Protection for Leveraged Products & Extra 'L' Handling
    # Many UK tickers arrive with an extra 'L' (e.g., BARCL, SHELL, GSKL).
    # If len > 3 and ends in 'L', it's likely a suffix we should strip.
    is_leveraged_etp = ticker.endswith("1") and len(ticker) > 3
    
    # Check against common UK stock stems for "1" suffix
    if is_leveraged_etp:
        if ticker.startswith(LEVERAGED_STEMS):
            ticker = ticker[:-1]
            
    # 6. Global Exchange Logic (UK vs US)
    is_explicit_uk = "_EQ" in original and "_US" not in original
    is_likely_uk = (len(ticker) <= 5 or ticker.endswith("L")) and ticker not in US_EXCLUSIONS
    
    # Heuristic for stripping extra 'L' (e.g. BARCL -> BARC)
    if is_likely_uk and ticker.endswith("L") and len(ticker) > 3 and ticker not in US_EXCLUSIONS:
        # Safe heuristic: Strip L.
        ticker = ticker[:-1]

    # Leveraged ETPs (Granular detection)
    is_leveraged = bool(re.search(r'^(3|5|7)[A-Z]+', ticker)) or \
                    bool(re.search(r'[A-Z]+(2|3|5|7)$', ticker))

    if is_explicit_uk or is_likely_uk or is_leveraged:
        # Ensure it doesn't already have .L (redundant check)
        if not ticker.endswith(".L") and "." not in ticker:
            return f"{ticker}.L"

    return ticker
