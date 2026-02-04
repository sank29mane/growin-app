
import asyncio
import json
import re
import difflib
import sys
import os

# Updated normalization function to test it locally
# Add backend directory to path so we can import modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading212_mcp_server import normalize_ticker

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
    # New cases for fixing the extra "L" issue
    ("BARCL", "BARC.L"),
    ("SHELL", "SHEL.L"),
    ("GSKL", "GSK.L"),
    ("RELL", "REL.L"),
    ("LLOYL", "LLOY.L"),
    ("TSCOL", "TSCO.L"),
    ("AZNL", "AZN.L"),
    ("BNZLL", "BNZL.L"), # Bunzl
    ("MNGL", "MNG.L"),   # M&G
    ("RBL", "RKT.L"),    # Reckitt Benckiser
    ("LGENL", "LGEN.L"), # Legal & General
    ("PHNXL", "PHNX.L"), # Phoenix Group
    ("MICCL", "MICC.L"), # Midwich Group
    ("SLL", "SL.L"),
    ("AIEL", "AIE.L"),
    ("CCHL", "CCH.L"),   # Coca Cola HBC? 
    ("BAL", "BA.L"),
    ("MONYL", "MONY.L"),
    ("BPL", "BP.L"),
    ("UUL", "UU.L"),
    ("ABFL", "ABF.L"),   # Associated British Foods
    ("GLENL", "GLEN.L"), # Glencore
    ("BATSL", "BATS.L"), # British American Tobacco
    ("TATEL", "TATE.L"), # Tate & Lyle
    ("GAWL", "GAW.L"),   # Games Workshop
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
