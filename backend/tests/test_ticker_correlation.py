
import asyncio
import json
import re
import difflib
import sys
import os

# Updated normalization function to test it locally
def normalize_ticker(ticker: str) -> str:
    """SOTA Ticker Normalization: Tested locally for correlation analysis."""
    if not ticker: return ""
    ticker = ticker.upper().strip().replace("$", "")
    if "." in ticker: return ticker
    original = ticker
    # Strip T212 suffixes (handles multiple like _US_EQ)
    ticker = re.sub(r'(_EQ|_US|_BE|_DE|_GB|_FR|_NL|_ES|_IT)+$', '', ticker)
    ticker = ticker.replace("_", "")
    
    special_mappings = {
        "SSLNL": "SSLN", "SGLNL": "SGLN", "3GLD": "3GLD", "SGLN": "SGLN",
        "LLOY1": "LLOY", "VOD1": "VOD", "BARC1": "BARC", "TSCO1": "TSCO",
        "BPL1": "BP", "AZNL1": "AZN", "SGLN1": "SGLN", "MAG5": "MAG5", "MAG7": "MAG7",
        "GLD3": "GLD3", "3UKL": "3UKL", "5QQQ": "5QQQ", "TSL3": "TSL3", "NVD3": "NVD3",
    }
    if ticker in special_mappings: ticker = special_mappings[ticker]

    if ticker.endswith("1") and len(ticker) > 3:
        stems = ["LLOY", "BARC", "VOD", "HSBA", "TSCO", "BP", "AZN", "RR", "NG", "SGLN", "SSLN"]
        if any(ticker.startswith(stem) for stem in stems):
            ticker = ticker[:-1]

    us_exclusions = {
        "AAPL", "MSFT", "GOOG", "AMZN", "NVDA", "TSLA", "META", "NFLX",
        "SPY", "QQQ", "DIA", "IWM", "IVV", "VOO", "VTI", "GLD", "SLV", "ARKK", "SMH",
        "AMD", "INTC", "PYPL", "ADBE", "CSCO", "PEP", "COST", "AVGO", "QCOM", "TXN"
    }
    
    is_explicit_uk = "_EQ" in original and "_US" not in original
    is_likely_uk = (len(ticker) <= 4 or ticker.endswith("L")) and ticker not in us_exclusions
    is_leveraged = bool(re.search(r'^(3|5|7)[A-Z]+', ticker)) or bool(re.search(r'[A-Z]+(2|3|5|7)$', ticker))
                    
    if is_explicit_uk or is_likely_uk or is_leveraged:
        if not ticker.endswith(".L") and "." not in ticker:
            return f"{ticker}.L"
    return ticker

# Correlation Test Cases
test_cases = [
    ("AAPL_US_EQ", "AAPL"),
    ("LLOY_EQ", "LLOY.L"),
    ("SGLN1_EQ", "SGLN.L"),
    ("MAG7_EQ", "MAG7.L"),
    ("3GLD_EQ", "3GLD.L"),
    ("TSCO_EQ", "TSCO.L"),
    ("VOD1", "VOD.L"),
    ("PHAU", "PHAU.L"),
    ("MAG5", "MAG5.L"),
    ("5QQQ", "5QQQ.L"),
    ("BPL1", "BP.L"),
    ("AZNL1", "AZN.L"),
    ("$MSFT", "MSFT"),
    ("lloy", "LLOY.L"),
    ("BARC.L", "BARC.L"),
]

def run_correlation_test():
    print("=== Ticker Correlation Analysis (v2) ===")
    print(f"{'Source Ticker':<15} | {'Normalized (Unified)':<20} | {'Status'}")
    print("-" * 50)
    
    all_passed = True
    for src, expected in test_cases:
        norm = normalize_ticker(src)
        status = "✅" if norm == expected else f"❌ (Expected {expected})"
        if norm != expected: all_passed = False
        print(f"{src:<15} | {norm:<20} | {status}")
    
    if all_passed:
        print("\nSUCCESS: All ticker correlations passed.")
    else:
        print("\nFAILURE: Some ticker correlations failed.")

if __name__ == "__main__":
    run_correlation_test()
