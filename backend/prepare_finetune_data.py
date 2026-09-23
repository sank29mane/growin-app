import pandas as pd
import yfinance as yf
import os

os.makedirs('data', exist_ok=True)
tickers = ["RELIANCE.NS", "TCS.NS", "TATAPOWER.NS", "SUZLON.NS", "RVNL.NS"]

for ticker in tickers:
    df = yf.download(ticker, period="5d", interval="5m", progress=False)
    if not df.empty:
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]

        # Format for Kronos
        df['timestamps'] = df.index.strftime('%Y/%m/%d %H:%M')
        df['amount'] = 0 # Dummy amount

        df = df[['timestamps', 'open', 'close', 'high', 'low', 'volume', 'amount']]
        df.to_csv(f"data/{ticker.replace('.NS', '')}_5min.csv", index=False)
        print(f"Saved {ticker} data.")
