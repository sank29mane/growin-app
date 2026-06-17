import os
import glob
import pandas as pd
import sys

# Add backend to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../backend')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.analytics_db import AnalyticsDB

def ingest_all_etfs(db_path="backend/data/analytics.duckdb", data_dir="data/etfs"):
    print(f"📁 Initializing Ingestion Pipeline (Database: {db_path})...")
    db = AnalyticsDB(db_path=db_path)
    
    # Find all CSV files in target dir
    csv_files = glob.glob(os.path.join(data_dir, "*.csv"))
    print(f"🔍 Found {len(csv_files)} CSV files in {data_dir}")
    
    total_inserted = 0
    tickers_ingested = []
    
    for csv_file in csv_files:
        filename = os.path.basename(csv_file)
        # Ticker format: {ticker}_5m.csv or {ticker}_10m.csv
        if "_5m.csv" in filename:
            ticker = filename.replace("_5m.csv", "")
            interval = "5m"
        elif "_10m.csv" in filename:
            ticker = filename.replace("_10m.csv", "")
            interval = "10m"
        else:
            # Skip other CSVs
            continue
            
        print(f"Processing {ticker} ({interval} interval)...")
        try:
            df = pd.read_csv(csv_file)
            if df.empty:
                print(f"  ⚠️ File is empty: {filename}")
                continue
                
            # Parse datetime column
            datetime_col = None
            for col in ['Datetime', 'datetime', 'Timestamp', 'timestamp']:
                if col in df.columns:
                    datetime_col = col
                    break
            
            if datetime_col:
                df['Datetime'] = pd.to_datetime(df[datetime_col])
                df.set_index('Datetime', inplace=True)
            else:
                # If first column is index/date
                df.iloc[:, 0] = pd.to_datetime(df.iloc[:, 0])
                df.set_index(df.columns[0], inplace=True)
                df.index.name = 'Datetime'
                
            # Drop rows with NaN in OHLCV
            df.dropna(subset=['Open', 'High', 'Low', 'Close'], inplace=True)
            
            # Resample to 10-minute bars
            df_10m = df.resample('10min').agg({
                'Open': 'first',
                'High': 'max',
                'Low': 'min',
                'Close': 'last',
                'Volume': 'sum'
            }).dropna()
            
            if df_10m.empty:
                print(f"  ⚠️ No data left after resampling for {ticker}")
                continue
                
            # Convert to list of dicts for bulk insert
            records = []
            for ts, row in df_10m.iterrows():
                records.append({
                    'timestamp': ts.isoformat(),
                    'open': float(row['Open']),
                    'high': float(row['High']),
                    'low': float(row['Low']),
                    'close': float(row['Close']),
                    'volume': int(row['Volume'])
                })
                
            inserted = db.bulk_insert_ohlcv(ticker, records, table_name='raw_market_data')
            total_inserted += inserted
            tickers_ingested.append(ticker)
            print(f"  ✅ Ingested {inserted} raw rows for {ticker}")
            
        except Exception as e:
            print(f"  ❌ Error processing {filename}: {e}")
            
    print(f"\n🎉 Ingestion Completed! Total rows inserted into raw_market_data: {total_inserted}")
    print(f"📈 Tickers loaded ({len(tickers_ingested)}): {', '.join(tickers_ingested[:10])}...")

if __name__ == "__main__":
    ingest_all_etfs()
