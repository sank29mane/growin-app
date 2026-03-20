# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "playwright",
#     "pandas",
# ]
# ///

import os
import json
import time
import argparse
import pandas as pd
from playwright.sync_api import sync_playwright

def load_tickers(config_path="data/lse_leveraged_etfs.json"):
    if not os.path.exists(config_path):
        return []
    with open(config_path, "r") as f:
        data = json.load(f)
    return data.get("tickers", [])

def convert_yf_json_to_csv(json_data, filename):
    try:
        if not json_data or "chart" not in json_data or not json_data["chart"]["result"]:
            return 0
            
        result = json_data["chart"]["result"][0]
        timestamps = result.get("timestamp")
        if not timestamps:
            return 0
            
        quote = result["indicators"]["quote"][0]
        
        df = pd.DataFrame({
            "Datetime": pd.to_datetime(timestamps, unit="s"),
            "Open": quote.get("open"),
            "High": quote.get("high"),
            "Low": quote.get("low"),
            "Close": quote.get("close"),
            "Volume": quote.get("volume")
        })
        
        df.set_index("Datetime", inplace=True)
        df.dropna(inplace=True)
        df.to_csv(filename)
        return len(df)
    except Exception as e:
        print(f"  Error parsing JSON for {filename}: {e}")
        return 0

def fetch_data(test_mode=False):
    tickers = load_tickers()
    if test_mode:
        print("🧪 Test mode enabled. Fetching data for first 3 tickers only.")
        tickers = tickers[:3]
        
    if not tickers:
        print("❌ No tickers found to fetch.")
        return

    print(f"📥 Fetching 5-minute intraday data for {len(tickers)} tickers using Browser Automation...")
    os.makedirs("data/etfs", exist_ok=True)
    saved_count = 0
    
    # We will also collect metadata for the mappings
    metadata_map = {}
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True) 
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        print("Priming session on finance.yahoo.com...")
        try:
            page.goto("https://finance.yahoo.com/", wait_until="commit", timeout=30000)
            time.sleep(2)
        except Exception as e:
            print(f"⚠️ Warning: Session priming failed: {e}")
        
        for ticker in tickers:
            print(f"Fetching {ticker}...")
            # Yahoo Finance internal API URL for charts
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?region=US&lang=en-US&includePrePost=false&interval=5m&useYfid=true&range=5d"
            
            try:
                # Use a slightly longer timeout and wait for network idle if possible
                response = page.goto(url, wait_until="domcontentloaded", timeout=20000)
                
                # Robustly extract JSON content
                # Sometimes its wrapped in <pre> or other tags
                content = page.locator("body").inner_text()
                
                # Clean up content if it contains non-JSON stuff
                content = content.strip()
                if not content.startswith("{"):
                    # Try to find the first { and last }
                    start = content.find("{")
                    end = content.rfind("}")
                    if start != -1 and end != -1:
                        content = content[start:end+1]

                try:
                    data = json.loads(content)
                    if "chart" in data and data["chart"]["result"] is not None:
                        result = data["chart"]["result"][0]
                        meta = result.get("meta", {})
                        
                        # Store metadata for generate_lse_mappings.py to use or for us to save
                        metadata_map[ticker] = {
                            "name": meta.get("longName", ticker),
                            "currency": meta.get("currency", "GBX"),
                            "exchange": meta.get("exchangeName", "LSE"),
                            "symbol": ticker
                        }
                        
                        filename = f"data/etfs/{ticker}_5m.csv"
                        rows = convert_yf_json_to_csv(data, filename)
                        if rows > 0:
                            saved_count += 1
                            print(f"  ✅ Saved {filename} ({rows} rows). Currency: {meta.get('currency')}")
                        else:
                            print(f"  ⚠️ No price data found for {ticker}")
                    else:
                        error = data.get("chart", {}).get("error", {}).get("description", "Unknown error")
                        print(f"  ⚠️ API Error for {ticker}: {error}")
                except json.JSONDecodeError:
                    print(f"  ❌ Failed to decode JSON for {ticker}. Content length: {len(content)}")
                    # Save error state for debugging
                    with open(f"data/etfs/{ticker}_error.txt", "w") as f:
                        f.write(content)
                    
            except Exception as e:
                print(f"  ❌ Browser navigation error for {ticker}: {e}")
                
            time.sleep(1.5) # Reduced sleep but still gentle
            
        browser.close()
    
    # Save the collected metadata for generate_lse_mappings.py
    if metadata_map:
        with open("data/etfs/scraper_metadata.json", "w") as f:
            json.dump(metadata_map, f, indent=2)
            
    print(f"✅ Successfully saved {saved_count}/{len(tickers)} ETF datasets.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch LSE Leveraged ETF Data via Browser API")
    parser.add_argument("--test-mode", action="store_true", help="Run in test mode (only fetch a few tickers)")
    args = parser.parse_args()
    
    fetch_data(test_mode=args.test_mode)
