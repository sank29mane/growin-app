"""
Generates the mapping for LSE Leveraged ETFs.
Fuses data from data/lse_leveraged_etfs.json and yfinance.
"""

import os
import json
import logging
import yfinance as yf
import re
import requests
from tqdm import tqdm

logger = logging.getLogger(__name__)

def generate_mappings():
    config_path = "data/lse_leveraged_etfs.json"
    output_path = "data/etfs/lse_leveraged.json"
    
    if not os.path.exists(config_path):
        logger.error(f"Config file {config_path} not found.")
        return
        
    with open(config_path, "r") as f:
        config = json.load(f)
        
    tickers = config.get("tickers", [])
    mappings = {}
    
    print(f"Generating mappings for {len(tickers)} tickers...")
    os.makedirs("data/etfs", exist_ok=True)
    
    for ticker in tqdm(tickers):
        try:
            t = yf.Ticker(ticker)
            info = t.info
            long_name = info.get("longName", ticker)
            
            # Enhanced leverage detection
            leverage = 1.0
            
            # Check for 'short' or 'inverse' first to set sign
            is_inverse = any(word in long_name.lower() for word in ["inverse", "short", "bear"])
            is_inverse = is_inverse or ticker.endswith("S.L") or "LQQS" in ticker
            
            # Extract number (e.g., 3x, 5x, or leading digit)
            num_match = re.search(r'([235])x', long_name.lower())
            if not num_match:
                # Fallback to leading digit if name is sparse
                num_match = re.match(r'^([235])', ticker)
            
            if num_match:
                leverage = float(num_match.group(1))
            
            if is_inverse:
                leverage = -abs(leverage)
            
            # Special case for known tickers if detection fails
            if "NVD3" in ticker: leverage = 3.0
            if "NVDS" in ticker: leverage = -1.0
            if "5LUK" in ticker: leverage = 5.0
            
            mappings[ticker] = {
                "name": long_name,
                "leverage": leverage,
                "currency": info.get("currency", "GBX"),
                "base_asset": info.get("underlyingSymbol", ticker.split(".")[0])
            }
        except (Exception, requests.exceptions.ConnectionError) as e:
            logger.warning(f"Failed to fetch info for {ticker} (using fallback): {e}")
            
            # Smart Fallback for LSE Tickers
            leverage = 1.0
            is_inverse = ticker.endswith("S.L") or "LQQS" in ticker or "NVDS" in ticker
            
            num_match = re.match(r'^([235])', ticker)
            if num_match:
                leverage = float(num_match.group(1))
            
            if "NVD3" in ticker: leverage = 3.0
            if "5LUK" in ticker: leverage = 5.0
            
            if is_inverse:
                leverage = -abs(leverage)
                
            mappings[ticker] = {
                "name": ticker,
                "leverage": leverage,
                "currency": "GBX",
                "base_asset": ticker.split(".")[0]
            }
            
    with open(output_path, "w") as f:
        json.dump(mappings, f, indent=2)
        
    print(f"✅ Saved mappings to {output_path}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    generate_mappings()
