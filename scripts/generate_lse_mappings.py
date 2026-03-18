"""
Generates the mapping for LSE Leveraged ETFs.
Fuses data from data/lse_leveraged_etfs.json and yfinance.
"""

import os
import json
import logging
import yfinance as yf
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
            
            # Simple leverage detection based on name/ticker
            leverage = 1.0
            if "3x" in long_name.lower() or ticker.startswith("3"):
                leverage = 3.0
            elif "2x" in long_name.lower() or ticker.startswith("2"):
                leverage = 2.0
            elif "5x" in long_name.lower() or ticker.startswith("5"):
                leverage = 5.0
            elif "inverse" in long_name.lower() or "short" in long_name.lower():
                leverage = -1.0
            
            mappings[ticker] = {
                "name": long_name,
                "leverage": leverage,
                "currency": info.get("currency", "GBX"),
                "base_asset": info.get("underlyingSymbol", ticker.split(".")[0])
            }
        except Exception as e:
            logger.warning(f"Failed to fetch info for {ticker}: {e}")
            mappings[ticker] = {
                "name": ticker,
                "leverage": 1.0,
                "currency": "GBX",
                "base_asset": ticker.split(".")[0]
            }
            
    with open(output_path, "w") as f:
        json.dump(mappings, f, indent=2)
        
    print(f"✅ Saved mappings to {output_path}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    generate_mappings()
