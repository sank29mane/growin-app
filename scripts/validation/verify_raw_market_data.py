import os
import sys
import duckdb

# Add backend to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../backend')))

def verify():
    db_path = "backend/data/analytics.duckdb"
    print(f"🔍 Validating DuckDB raw_market_data table in {db_path}...")
    conn = duckdb.connect(db_path)
    
    # Check tables
    tables = [t[0] for t in conn.execute("show tables").fetchall()]
    print(f"Available tables: {tables}")
    if "raw_market_data" not in tables:
        print("❌ raw_market_data table is missing!")
        return False
        
    # Total row count
    row_count = conn.execute("select count(*) from raw_market_data").fetchone()[0]
    print(f"Total rows in raw_market_data: {row_count}")
    if row_count == 0:
        print("❌ raw_market_data is empty!")
        return False
        
    # Get distinct tickers and row count
    tickers_data = conn.execute("select ticker, count(*), min(timestamp), max(timestamp) from raw_market_data group by ticker order by count(*) desc").fetchall()
    print(f"\nIngested Tickers Count: {len(tickers_data)}")
    print("Top 10 tickers with most data points:")
    for t, cnt, min_t, max_t in tickers_data[:10]:
        print(f"  - {t}: {cnt} rows | Range: {min_t} to {max_t}")
        
    # Check for null values
    null_checks = conn.execute("""
        select 
            sum(case when open is null then 1 else 0 end) as null_open,
            sum(case when close is null then 1 else 0 end) as null_close,
            sum(case when timestamp is null then 1 else 0 end) as null_ts
        from raw_market_data
    """).fetchone()
    print(f"\nNull check results: Open Nulls={null_checks[0]}, Close Nulls={null_checks[1]}, Timestamp Nulls={null_checks[2]}")
    
    if any(null_checks):
        print("❌ Null values found in raw_market_data!")
        return False
        
    print("\n✅ Verification Successful: raw_market_data contains valid ETF records with complete timestamps.")
    return True

if __name__ == "__main__":
    success = verify()
    sys.exit(0 if success else 1)
