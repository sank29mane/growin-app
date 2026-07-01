#!/usr/bin/env python3
"""
Offline GMM Training Engine for Market Regime Classification.

This script fetches historical volatility and spread data, normalizes them,
fits Gaussian Mixture Models for K ∈ [2, 3, 4], selects the optimal number
of regimes (K) based on Bayesian Information Criterion (BIC), and serializes
the trained GMM model parameters.
"""

import os
import sys
import argparse
import logging
import joblib
import numpy as np
import pandas as pd
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler

# Ensure backend is in the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../backend')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.analytics_db import AnalyticsDB

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def extract_training_data(db_path: str, ticker: str = None) -> pd.DataFrame:
    """
    Extract historical volatility and spread features from DuckDB.
    If features are not yet calculated, triggers their ingestion.
    """
    try:
        db = AnalyticsDB(db_path=db_path)
        
        # Check if there is data in market_features
        test_query = "SELECT COUNT(*) FROM market_features"
        try:
            count = db.execute(test_query).fetchone()[0]
        except Exception:
            count = 0
            
        if count == 0:
            logger.info("Market features table is empty. Attempting to generate features from ohlcv_history...")
            # Get unique tickers
            tickers_query = "SELECT DISTINCT ticker FROM ohlcv_history"
            tickers = [r[0] for r in db.execute(tickers_query).fetchall()]
            if not tickers:
                raise ValueError("No historical OHLCV data found in ohlcv_history to generate features.")
            for t in tickers:
                db.calculate_and_ingest_features(t)
                
        # Build query
        query = """
            SELECT timestamp, ticker, rolling_volatility, rolling_spread
            FROM market_features
        """
        params = []
        if ticker:
            query += " WHERE ticker = ?"
            params.append(ticker)
        query += " ORDER BY timestamp ASC"
        
        rows = db.execute(query, params).fetchall()
        if not rows:
            raise ValueError(f"No feature records found in market_features{' for ticker ' + ticker if ticker else ''}.")
            
        df = pd.DataFrame(rows, columns=['timestamp', 'ticker', 'volatility', 'spread'])
    except Exception as e:
        if "Serialization Error" in str(e) or "expected end of object" in str(e):
            logger.warning("DuckDB database file version mismatch. Falling back to reading from exported CSV...")
            csv_path = os.path.join(os.path.dirname(db_path), "ohlcv_history.csv")
            if not os.path.exists(csv_path):
                raise ValueError("DuckDB mismatch encountered, and ohlcv_history.csv is not available.")
            
            # Load CSV into an in-memory DuckDB connection
            db = AnalyticsDB(db_path=":memory:")
            raw_df = pd.read_csv(csv_path)
            # convert timestamp column to datetime
            raw_df['timestamp'] = pd.to_datetime(raw_df['timestamp'])
            db.bulk_insert_ohlcv("AAPL", raw_df[raw_df['ticker'] == 'AAPL'].to_dict('records'))
            db.bulk_insert_ohlcv("TSLA", raw_df[raw_df['ticker'] == 'TSLA'].to_dict('records'))
            db.bulk_insert_ohlcv("NVDA", raw_df[raw_df['ticker'] == 'NVDA'].to_dict('records'))
            db.bulk_insert_ohlcv("MMM", raw_df[raw_df['ticker'] == 'MMM'].to_dict('records'))
            
            # Re-fetch from the newly populated in-memory DuckDB
            query = """
                SELECT timestamp, ticker, rolling_volatility, rolling_spread
                FROM market_features
            """
            params = []
            if ticker:
                query += " WHERE ticker = ?"
                params.append(ticker)
            query += " ORDER BY timestamp ASC"
            rows = db.execute(query, params).fetchall()
            df = pd.DataFrame(rows, columns=['timestamp', 'ticker', 'volatility', 'spread'])
        else:
            raise e
            
    logger.info(f"Successfully extracted {len(df)} rows of training features.")
    return df


def fit_optimal_gmm(features: np.ndarray, min_k: int = 2, max_k: int = 4, reg_covar: float = 1e-6) -> tuple[GaussianMixture, int, dict]:
    """
    Fit multiple GMMs and select the optimal model minimizing BIC.
    
    Parameters:
        features: Normalized 2D array of shape (N, 2) [Volatility, Spread]
        min_k: Minimum number of clusters (regimes)
        max_k: Maximum number of clusters (regimes)
        reg_covar: Non-negative regularization added to the diagonal of covariance.
        
    Returns:
        tuple: (best_fitted_gmm_model, best_k, bic_scores)
    """
    best_bic = float('inf')
    best_gmm = None
    best_k = min_k
    bic_scores = {}
    
    for k in range(min_k, max_k + 1):
        gmm = GaussianMixture(
            n_components=k,
            covariance_type='full',
            random_state=42,
            max_iter=200,
            n_init=5,
            reg_covar=reg_covar
        )
        gmm.fit(features)
        
        # Verify convergence and degenerate covariance check
        if not gmm.converged_:
            logger.warning(f"GMM with K={k} did not converge!")
            continue
            
        # Check for degenerate covariance matrices (near-zero eigenvalues/determinant)
        degenerate = False
        for cov in gmm.covariances_:
            eigenvals = np.linalg.eigvalsh(cov)
            if np.any(eigenvals <= 1e-7):
                degenerate = True
                logger.warning(f"GMM K={k} has degenerate covariance matrix: eigenvalues={eigenvals}")
                break
                
        if degenerate:
            continue
            
        bic = gmm.bic(features)
        bic_scores[k] = bic
        logger.info(f"GMM K={k} -> BIC: {bic:.4f}, Converged: {gmm.converged_}")
        
        if bic < best_bic:
            best_bic = bic
            best_gmm = gmm
            best_k = k
            
    if best_gmm is None:
        raise ValueError("Failed to fit any valid, converged, non-degenerate GMM models.")
        
    logger.info(f"Selected GMM with K={best_k} (BIC: {best_bic:.4f}) as the optimal model.")
    return best_gmm, best_k, bic_scores


def main():
    parser = argparse.ArgumentParser(description="Train GMM regime classifier offline.")
    parser.add_argument("--db-path", type=str, default="backend/data/analytics.duckdb", help="Path to DuckDB file")
    parser.add_argument("--ticker", type=str, default=None, help="Filter by ticker")
    parser.add_argument("--output", type=str, default="models/gmm_regime.joblib", help="Output path for serialized model")
    args = parser.parse_args()
    
    try:
        # 1. Ingest/Extract data
        df = extract_training_data(args.db_path, args.ticker)
        
        # 2. Extract features
        X = df[['volatility', 'spread']].values
        
        # 3. Standardize features
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        
        # 4. Find optimal GMM
        model, best_k, bic_scores = fit_optimal_gmm(X_scaled)
        
        # 5. Save model and Z-score scaling parameters
        output_dir = os.path.dirname(args.output)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            
        payload = {
            "model": model,
            "scaler": scaler,
            "best_k": best_k,
            "bic_scores": bic_scores,
            "means": model.means_,
            "covariances": model.covariances_,
            "weights": model.weights_,
            # Save precomputed values for online JIT path
            "precisions_cholesky": model.precisions_cholesky_,
            "scaler_mean": scaler.mean_,
            "scaler_scale": scaler.scale_
        }
        
        joblib.dump(payload, args.output)
        logger.info(f"✅ GMM Model payload successfully serialized to {args.output}")
        
    except Exception as e:
        logger.error(f"Failed to train GMM regime model: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
