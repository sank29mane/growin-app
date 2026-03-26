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
    with open(config_path, "r") as f:
        data = json.load(f)
    return data.get("tickers", [])

def convert_yf_json_to_csv(json_data, filename):
    try:
        result = json_data['chart']['result'][0]
        timestamps = result['timestamp']
        quote = result['indicators']['quote'][0]
        
        df = pd.DataFrame({
            'Datetime': pd.to_datetime(timestamps, unit='s'),
            'Open': quote['open'],
            'High': quote['high'],
            'Low': quote['low'],
            'Close': quote['close'],
            'Volume': quote['volume']
        })
        
        df.set_index('Datetime', inplace=True)
        df.dropna(inplace=True)
        df.to_csv(filename)
        return len(df)
    except Exception as e:
        print(f"Error parsing JSON: {e}")
        return 0

def fetch_data(test_mode=False):
    tickers = load_tickers()
    if test_mode:
        print("🧪 Test mode enabled. Fetching data for first 3 tickers only.")
        tickers = tickers[:3]
        
    print(f"📥 Fetching 5-minute intraday data for {len(tickers)} tickers using Browser Automation...")
    os.makedirs("data/etfs", exist_ok=True)
    saved_count = 0
    
    with sync_playwright() as p:
        # Use a headed browser to look as human as possible to Cloudflare/Yahoo
        browser = p.chromium.launch(headless=True) 
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        
        # Prime the session by visiting the main page and accepting cookies if prompted
        print("Priming session on finance.yahoo.com...")
        page.goto("https://finance.yahoo.com/", wait_until="commit")
        time.sleep(2)
        
        for ticker in tickers:
            print(f"Fetching {ticker}...")
            # Yahoo Finance internal API URL for charts
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?region=US&lang=en-US&includePrePost=false&interval=5m&useYfid=true&range=5d"
            
            try:
                page.goto(url, wait_until="domcontentloaded")
                content = page.locator("body").inner_text()
                
                try:
                    data = json.loads(content)
                    if "chart" in data and data["chart"]["result"] is not None:
                        filename = f"data/etfs/{ticker}_5m.csv"
                        rows = convert_yf_json_to_csv(data, filename)
                        if rows > 0:
                            saved_count += 1
                            print(f"  ✅ Saved {filename} ({rows} rows)")
                        else:
                            print(f"  ⚠️ No valid data found in JSON for {ticker}")
                    else:
                        print(f"  ⚠️ Invalid chart data returned for {ticker}")
                except json.JSONDecodeError:
                    print(f"  ❌ Failed to decode JSON for {ticker}. IP might be soft-blocked.")
                    
            except Exception as e:
                print(f"  ❌ Browser navigation error for {ticker}: {e}")
                
            time.sleep(2.5) # Be gentle on the API
            
        browser.close()
            
    print(f"✅ Successfully saved {saved_count}/{len(tickers)} ETF datasets via Browser Automation.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch LSE Leveraged ETF Data via Browser API")
    parser.add_argument("--test-mode", action="store_true", help="Run in test mode (only fetch a few tickers)")
    args = parser.parse_args()
    
    fetch_data(test_mode=args.test_mode)
