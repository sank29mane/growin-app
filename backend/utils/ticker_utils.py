"""
Ticker Normalization Utilities
Shared logic for resolving ticker discrepancies between T212, Yahoo Finance, and Alpaca.
"""

import re

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

UK_COMMON_STEMS = tuple(["LLOY", "BARC", "VOD", "HSBA", "TSCO", "BP", "AZN", "RR", "NG", "SGLN", "SSLN", "GSK", "SHELL", "BATS", "AHT", "NWG", "GLEN"])

def normalize_ticker(ticker: str) -> str:
    """
    SOTA Ticker Normalization: Resolves discrepancies between Trading212, 
    Yahoo Finance, Alpaca, and Finnhub via Rust-optimized core.
    """
    # Dynamic check for growin_core to support test mocking
    import sys
    g_core = sys.modules.get("growin_core")
    if g_core and hasattr(g_core, "normalize_ticker"):
        try:
            return g_core.normalize_ticker(ticker)
        except Exception:
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
        if ticker.startswith(UK_COMMON_STEMS):
            ticker = ticker[:-1]
            
    # 6. Global Exchange Logic (UK vs US)
    is_explicit_uk = "_EQ" in original and "_US" not in original
    
    # Improved Likelihood check:
    # 1. Must NOT be in US Exclusions
    # 2. Must either be short (<= 3 chars) OR end in L (with reasonable length)
    is_likely_uk = (len(ticker) <= 3 or (len(ticker) <= 5 and ticker.endswith("L"))) and ticker not in US_EXCLUSIONS
    
    # SOTA Fix: SMCI and other 4-char US tickers should NOT be likely UK
    if len(ticker) == 4:
        is_likely_uk = False

    # Force likelihood for known UK stems (LLOY, BARC, TSCO, etc)
    if any(ticker == stem or ticker == f"{stem}1" or ticker == f"{stem}L" for stem in UK_COMMON_STEMS):
        is_likely_uk = True

    # Heuristic for stripping extra 'L' (e.g. BARCL -> BARC)
    if is_likely_uk and ticker.endswith("L") and len(ticker) > 3 and ticker not in US_EXCLUSIONS:
        # Safe heuristic: Strip L if it's likely a UK suffix artifact
        ticker = ticker[:-1]

    # Leveraged ETPs (Granular detection - must have a digit and be short)
    is_leveraged = (bool(re.search(r'^[357][A-Z]+', ticker)) or \
                    bool(re.search(r'^[A-Z]+[2357]$', ticker))) and len(ticker) <= 5

    # FINAL DECISION: Should we add .L?
    if (is_explicit_uk or is_likely_uk or is_leveraged) and ticker not in US_EXCLUSIONS:
        # Ensure it doesn't already have .L (redundant check)
        if not ticker.endswith(".L") and "." not in ticker:
            return f"{ticker}.L"

    return ticker
