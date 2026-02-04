import duckdb
import pandas as pd
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


class AnalyticsDB:
    """
    DuckDB-powered analytics engine for high-performance OLAP queries.
    
    Use Cases:
    - Time-series aggregations (20-50x faster than SQLite)
    - Complex analytical queries on historical OHLCV data
    - Portfolio performance analytics
    - Bulk data operations
    
    Architecture:
    - Columnar storage for fast scans
    - Vectorized execution
    - Multi-threaded query processing
    - In-memory or persistent mode
    """
    
    def __init__(self, db_path: str = ":memory:"):
        """
        Initialize DuckDB analytics database.
        
        Args:
            db_path: Path to database file or ":memory:" for in-memory
        """
        self.conn = duckdb.connect(db_path)
        self._init_schema()
        logger.info(f"✅ AnalyticsDB initialized (mode: {'memory' if db_path == ':memory:' else 'persistent'})")
    
    def _init_schema(self):
        """Create optimized schema for time-series analytics"""
        # OHLCV historical data table
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS ohlcv_history (
                ticker VARCHAR NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                volume BIGINT,
                PRIMARY KEY (ticker, timestamp)
            )
        """)
        
        # Create index for fast queries
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ticker_time 
            ON ohlcv_history(ticker, timestamp DESC)
        """)
        
        logger.info("Analytics schema initialized")
    
    def bulk_insert_ohlcv(self, ticker: str, data: List[Dict[str, Any]]) -> int:
        """
        Fast bulk insert for OHLCV data.
        Uses DuckDB's native DataFrame integration for maximum speed.
        
        Args:
            ticker: Stock ticker symbol
            data: List of OHLCV dicts with keys: o, h, l, c, v, t OR timestamp, open, high, low, close, volume
        
        Returns:
            Number of rows inserted
        """
        if not data:
            return 0
        
        try:
            # OPTIMIZATION: Vectorized normalization (~100x faster than iteration)
            df = pd.DataFrame(data)

            # 1. Map short keys to long keys
            col_map = {
                'o': 'open',
                'h': 'high',
                'l': 'low',
                'c': 'close',
                'v': 'volume',
                't': 'timestamp'
            }
            rename_dict = {}
            for short, long in col_map.items():
                if short in df.columns and long not in df.columns:
                    rename_dict[short] = long
                elif short in df.columns and long in df.columns:
                    # Coalesce: fill NaN in long with value from short
                    df[long] = df[long].fillna(df[short])

            if rename_dict:
                df.rename(columns=rename_dict, inplace=True)

            # 2. Ensure all required columns exist and fill defaults
            required_cols = ['timestamp', 'open', 'high', 'low', 'close', 'volume']
            for col in required_cols:
                if col not in df.columns:
                    df[col] = 0.0 if col != 'timestamp' else None

            # 3. Vectorized timestamp conversion
            if pd.api.types.is_numeric_dtype(df['timestamp']):
                new_ts = pd.Series(index=df.index, dtype='datetime64[ns]')
                numerics = pd.to_numeric(df['timestamp'], errors='coerce')
                
                # Heuristic: > 2e9 implies milliseconds
                mask_ms = numerics > 2_000_000_000

                if mask_ms.any():
                    new_ts.loc[mask_ms] = pd.to_datetime(numerics.loc[mask_ms], unit='ms')

                if (~mask_ms).any():
                    new_ts.loc[~mask_ms] = pd.to_datetime(numerics.loc[~mask_ms], unit='s')

                df['timestamp'] = new_ts
            else:
                # Handle ISO strings or mixed
                df['timestamp'] = pd.to_datetime(df['timestamp'])

            df['ticker'] = ticker
            
            # Bulk insert using DuckDB's fast path
            # Explicitly select columns in schema order to avoid misalignment
            self.conn.execute("""
                INSERT INTO ohlcv_history 
                SELECT ticker, timestamp, open, high, low, close, volume 
                FROM df
                ON CONFLICT (ticker, timestamp) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume
            """)
            
            rows_inserted = len(df)
            logger.info(f"📊 Inserted {rows_inserted} rows for {ticker}")
            return rows_inserted
            
        except Exception as e:
            logger.error(f"Bulk insert failed: {e}")
            return 0
    
    def get_aggregated_stats(
        self, 
        ticker: str, 
        window_days: int = 30
    ) -> Optional[pd.DataFrame]:
        """
        Lightning-fast aggregated statistics for a ticker.
        
        Args:
            ticker: Stock ticker symbol
            window_days: Time window in days
        
        Returns:
            DataFrame with aggregated statistics
        """
        try:
            result = self.conn.execute(f"""
                SELECT 
                    ticker,
                    COUNT(*) as data_points,
                    AVG(close) as avg_price,
                    STDDEV(close) as volatility,
                    MIN(close) as min_price,
                    MAX(close) as max_price,
                    MAX(high) - MIN(low) as price_range,
                    SUM(volume) as total_volume,
                    AVG(volume) as avg_volume,
                    (MAX(close) - MIN(close)) / MIN(close) * 100 as price_change_pct
                FROM ohlcv_history
                WHERE ticker = ? 
                  AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '{window_days}' DAY
                GROUP BY ticker
            """, (ticker,)).fetchdf()
            
            return result if not result.empty else None
            
        except Exception as e:
            logger.error(f"Aggregation query failed: {e}")
            return None
    
    def get_recent_ohlcv(
        self, 
        ticker: str, 
        limit: int = 100
    ) -> Optional[pd.DataFrame]:
        """
        Get recent OHLCV data for a ticker.
        
        Args:
            ticker: Stock ticker symbol
            limit: Number of most recent rows to fetch
        
        Returns:
            DataFrame with OHLCV data
        """
        try:
            result = self.conn.execute("""
                SELECT 
                    timestamp,
                    open,
                    high,
                    low,
                    close,
                    volume
                FROM ohlcv_history
                WHERE ticker = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (ticker, limit)).fetchdf()
            
            return result if not result.empty else None
            
        except Exception as e:
            logger.error(f"Query failed: {e}")
            return None
    
    def calculate_volatility(
        self, 
        ticker: str, 
        window_days: int = 30
    ) -> Optional[float]:
        """
        Calculate annualized volatility for a ticker.
        
        Args:
            ticker: Stock ticker symbol
            window_days: Time window in days
        
        Returns:
            Annualized volatility (standard deviation of returns)
        """
        try:
            result = self.conn.execute(f"""
                WITH daily_returns AS (
                    SELECT 
                        ticker,
                        close,
                        LAG(close) OVER (ORDER BY timestamp) as prev_close,
                        (close - LAG(close) OVER (ORDER BY timestamp)) / 
                            LAG(close) OVER (ORDER BY timestamp) as daily_return
                    FROM ohlcv_history
                    WHERE ticker = ?
                      AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '{window_days}' DAY
                    ORDER BY timestamp
                )
                SELECT 
                    STDDEV(daily_return) * SQRT(252) as annualized_volatility
                FROM daily_returns
                WHERE daily_return IS NOT NULL
            """, (ticker,)).fetchone()
            
            return result[0] if result and result[0] is not None else None
            
        except Exception as e:
            logger.error(f"Volatility calculation failed: {e}")
            return None
    
    def get_price_trend(
        self, 
        ticker: str, 
        window_days: int = 30
    ) -> Optional[str]:
        """
        Determine price trend (bullish/bearish/neutral).
        
        Args:
            ticker: Stock ticker symbol
            window_days: Time window in days
        
        Returns:
            Trend description: 'bullish', 'bearish', or 'neutral'
        """
        try:
            result = self.conn.execute(f"""
                WITH price_data AS (
                    SELECT 
                        close,
                        ROW_NUMBER() OVER (ORDER BY timestamp) as rn,
                        COUNT(*) OVER () as total
                    FROM ohlcv_history
                    WHERE ticker = ?
                      AND timestamp >= CURRENT_TIMESTAMP - INTERVAL '{window_days}' DAY
                )
                SELECT 
                    AVG(CASE WHEN rn <= total/2 THEN close END) as first_half_avg,
                    AVG(CASE WHEN rn > total/2 THEN close END) as second_half_avg
                FROM price_data
            """, (ticker,)).fetchone()
            
            if not result or None in result:
                return 'neutral'
            
            first_avg, second_avg = result
            change_pct = ((second_avg - first_avg) / first_avg) * 100
            
            if change_pct > 2:
                return 'bullish'
            elif change_pct < -2:
                return 'bearish'
            else:
                return 'neutral'
                
        except Exception as e:
            logger.error(f"Trend calculation failed: {e}")
            return 'neutral'
    
    def close(self):
        """Close database connection"""
        self.conn.close()
        logger.info("AnalyticsDB connection closed")


# --- Utility Functions ---

_analytics_instance = None

def get_analytics_db(db_path: str = ":memory:"):
    """
    Singleton pattern for AnalyticsDB.
    
    Args:
        db_path: Path to database file or ":memory:"
    
    Returns:
        AnalyticsDB instance
    """
    global _analytics_instance
    if _analytics_instance is None:
        _analytics_instance = AnalyticsDB(db_path)
    return _analytics_instance


if __name__ == '__main__':
    # Test
    db = get_analytics_db()
    print("AnalyticsDB ready for OLAP queries")
