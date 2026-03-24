"""
Ticker Normalization Utilities
Shared logic for resolving ticker discrepancies between T212, Yahoo Finance, and Alpaca.
"""

import re
from typing import Optional

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
    "3GLDL": "3GLD", "3GLDL_EQ": "3GLD",
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
    "PHNXL": "PHNX", # Phoenix Group
    "BT": "BT-A", # BT Group (BT-A.L)
    "NG": "NG", # National Grid
    "JII": "JII", # JPMorgan Indian Investment Trust
    "RBS": "NWG", # NatWest Group
    "BTL": "BT-A",
}

US_EXCLUSIONS = {
    # Tech & Growth
    "AAPL", "MSFT", "GOOG", "AMZN", "NVDA", "TSLA", "META", "NFLX",
    "AMD", "INTC", "PYPL", "ADBE", "CSCO", "PEP", "COST", "AVGO", "QCOM", "TXN",
    "ORCL", "CRM", "IBM", "UBER", "ABNB", "SNOW", "PLTR", "SQ", "SHOP", "SPOT",
    "GOOGL", "AU", # Anglogold Ashanti (US)
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

UK_COMMON_STEMS = tuple(["LLOY", "BARC", "VOD", "HSBA", "TSCO", "BP", "AZN", "RR", "NG", "SGLN", "SSLN", "GSK", "SHELL", "BATS", "AHT", "NWG", "GLEN", "PHNX", "BT-A", "JII", "ABF", "SSE", "RIO", "REL", "ULVR", "SHEL", "LQQ3", "3QQQ", "3LUS", "3SPY", "LQQS", "3ULS"])

def normalize_ticker(ticker: str) -> str:
    """
    SOTA Ticker Normalization: Resolves discrepancies between Trading212, 
    Yahoo Finance, Alpaca, and Finnhub via Rust-optimized core.
    
    Now a wrapper for TickerResolver.normalize for backward compatibility.
    """
    return TickerResolver().normalize(ticker)

class TickerResolver:
    """
    Unified Service for Ticker Normalization, Extraction, and Validation.
    Centralizes US/UK/Leveraged logic and tiered resolution.
    """
    
    def __init__(self):
        self.special_mappings = SPECIAL_MAPPINGS
        self.us_exclusions = US_EXCLUSIONS
        self.uk_stems = UK_COMMON_STEMS
        self._cache = {} # Simple in-memory cache for this instance

    def normalize(self, ticker: str) -> str:
        """
        Normalizes a ticker symbol to a standard format (e.g., adding .L for UK stocks).
        Preferentially uses the Rust core implementation if available.
        """
        if not ticker:
            return ""

        # 1. Dynamic check for growin_core
        import sys
        g_core = sys.modules.get("growin_core")
        if g_core and hasattr(g_core, "normalize_ticker"):
            try:
                return g_core.normalize_ticker(ticker)
            except Exception:
                pass

        # 2. Basic Cleaning
        ticker = ticker.upper().strip().replace("$", "")
        
        # 3. Already Normalized (contains dot)
        if "." in ticker:
            return ticker

        # 4. Handle Platform-Specific Artifacts
        original = ticker
        # Strip T212 suffixes (handles multiple like _US_EQ) - Case Insensitive
        ticker = re.sub(r'(_EQ|_US|_BE|_DE|_GB|_FR|_NL|_ES|_IT)+$', '', ticker, flags=re.IGNORECASE)
        ticker = ticker.replace("_", "") # Fallback for messy underscores
        
        # 5. SPECIAL MAPPINGS
        if ticker.upper() in self.special_mappings:
            ticker = self.special_mappings[ticker.upper()]
        
        # Ensure base ticker is uppercase after stripping artifacts but BEFORE mapping/exclusion checks
        ticker = ticker.upper()

        # 6. Global Exchange Logic (UK vs US)
        is_explicit_uk = "_EQ" in original.upper() and "_US" not in original.upper()
        
        # Improved Likelihood check (from PR #146):
        # 1. Must NOT be in US Exclusions
        # 2. Must either be short (<= 3 chars) OR end in L (with reasonable length)
        is_likely_uk = (len(ticker) <= 3 or (len(ticker) <= 5 and ticker.endswith("L"))) and ticker not in self.us_exclusions
        
        # SOTA Fix: SMCI and other 4-char US tickers should NOT be likely UK
        if len(ticker) == 4 and not ticker.endswith("L"):
            is_likely_uk = False

        # Force likelihood for known UK stems (LLOY, BARC, TSCO, etc)
        if any(ticker == stem or ticker == f"{stem}1" or ticker == f"{stem}L" for stem in self.uk_stems):
            is_likely_uk = True

        # Heuristic for stripping extra 'L' (e.g. BARCL -> BARC)
        if is_likely_uk and ticker.endswith("L") and len(ticker) > 3 and ticker not in self.us_exclusions:
            ticker = ticker[:-1]

        # Leveraged ETPs (Granular detection - must have a digit and be short)
        is_leveraged = (bool(re.search(r'^[357][A-Z]+', ticker)) or \
                        bool(re.search(r'^[A-Z]+[2357]$', ticker))) and len(ticker) <= 5

        # FINAL DECISION: Should we add .L?
        if (is_explicit_uk or is_likely_uk or is_leveraged) and ticker not in self.us_exclusions:
            if not ticker.endswith(".L") and "." not in ticker:
                return f"{ticker}.L"

        return ticker

    def extract(self, text: str) -> list[str]:
        """
        NLP-lite ticker extraction from natural language strings.
        Supports common patterns like "Compare AAPL and MSFT" or "Check Tesla (TSLA)".
        """
        # Patterns: 
        # 1. Standard UPPERCASE tickers (2-5 chars)
        # 2. Tickers in parentheses: (AAPL)
        # 3. Alphanumeric tickers: 3GLD, 5QQQ
        
        # Basic regex for potential tickers
        potential_tickers = re.findall(r'\b[A-Z0-9]{1,5}(?:\.[A-L])?\b', text)
        
        # Filter and normalize
        results = []
        for t in potential_tickers:
            norm = self.normalize(t)
            if norm and norm not in results:
                # Basic heuristic: ignore if it's a very short common word
                if len(t) == 1 and t not in ["F", "T", "C", "V", "Z", "O", "D", "R", "K", "X", "S", "M", "A", "G"]:
                    continue
                results.append(norm)
                
        return results

    async def resolve(self, query: str) -> Optional[str]:
        """
        Tiered Resolution: 
        1. Exact Match Cache
        2. Normalize & Validate
        3. Provider Search
        """
        # 1. Quick normalization
        ticker = self.normalize(query)
        if not ticker:
            return None
            
        # 2. Check Cache
        if ticker in self._cache:
            return self._cache[ticker]
            
        # 3. Extraction if query is a sentence
        if " " in query:
            extracted = self.extract(query)
            if extracted:
                return extracted[0] # Return first match
                
        return ticker

    async def search(self, query: str) -> list[dict]:
        """
        Search for potential tickers matching a query string.
        Returns a list of matching candidates for the Coordinator Tier 2.
        """
        # SOTA 2026: Basic implementation for local fallback.
        # Real search usually happens via MCP/Finnhub.
        ticker = await self.resolve(query)
        if ticker:
            return [{"ticker": ticker, "name": query}]
        return []
