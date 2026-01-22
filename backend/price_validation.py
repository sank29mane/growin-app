"""
Price validation and variance checking across data sources.
Ensures price consistency before executing trades.
"""

from typing import Dict, Optional
from utils.currency_utils import DataSourceNormalizer
import asyncio
import logging

logger = logging.getLogger(__name__)

class PriceValidator:
    """
    Validates prices across multiple data sources before trade execution.
    Implements threshold checking and variance reporting.
    """
    
    # Threshold configuration
    DEFAULT_VARIANCE_THRESHOLD = 0.5  # 0.5% variance allowed
    WARNING_VARIANCE_THRESHOLD = 1.0  # Warn user if > 1%
    CRITICAL_VARIANCE_THRESHOLD = 3.0  # Block trade if > 3%
    
    @staticmethod
    async def fetch_multi_source_price(ticker: str) -> Dict[str, Optional[float]]:
        """
        Fetch price from all available sources.
        
        Returns:
            {
                "trading212": normalized_price,
                "alpaca": normalized_price,
                "yfinance": normalized_price,
                "currency": "GBP" or "USD"
            }
        """
        from data_engine import get_alpaca_client
        from trading212_mcp_server import normalize_ticker
        
        results = {
            "trading212": None,
            "alpaca": None,
            "yfinance": None,
            "currency": DataSourceNormalizer.get_currency_for_ticker(ticker)
        }
        
        # Normalize ticker for each source
        normalized_ticker = normalize_ticker(ticker)
        
        # Fetch from all sources in parallel
        async def fetch_alpaca():
            try:
                alpaca_client = get_alpaca_client()
                data = await alpaca_client.get_historical_bars(normalized_ticker, limit=1)
                if data and "bars" in data and data["bars"]:
                    raw_price = data["bars"][0]["c"]
                    results["alpaca"] = DataSourceNormalizer.normalize_alpaca_price(raw_price, normalized_ticker)
                    logger.info(f"Alpaca price for {ticker}: {results['alpaca']}")
            except Exception as e:
                logger.warning(f"Alpaca price fetch failed for {ticker}: {e}")
        
        async def fetch_yfinance():
            try:
                import yfinance as yf
                ticker_obj = yf.Ticker(normalized_ticker)
                info = await asyncio.to_thread(ticker_obj.history, period="1d", interval="1d")
                if not info.empty:
                    raw_price = float(info['Close'].iloc[-1])
                    results["yfinance"] = DataSourceNormalizer.normalize_yfinance_price(raw_price, normalized_ticker)
                    logger.info(f"yfinance price for {ticker}: {results['yfinance']}")
            except Exception as e:
                logger.warning(f"yfinance price fetch failed for {ticker}: {e}")
        
        async def fetch_trading212():
            try:
                # Trading212 price fetching would go here
                # For now, we'll leave it as None and rely on other sources
                # This can be implemented when MCP client is available in this context
                pass
            except Exception as e:
                logger.warning(f"Trading212 price fetch failed for {ticker}: {e}")
        
        # Execute all fetches concurrently
        await asyncio.gather(
            fetch_alpaca(),
            fetch_yfinance(),
            fetch_trading212(),
            return_exceptions=True
        )
        
        return results
    
    @staticmethod
    def calculate_variance(prices: Dict[str, Optional[float]]) -> Dict[str, any]:
        """
        Calculate price variance across sources.
        
        Returns:
            {
                "max_variance": 2.5,  # Percentage
                "mean_price": 150.25,
                "price_range": (149.00, 151.50),
                "outliers": ["trading212"],  # Sources with high variance
                "consensus_price": 150.25  # Recommended price to use
            }
        """
        # Filter out None values
        valid_prices = {k: v for k, v in prices.items() if v is not None and k != "currency"}
        
        if len(valid_prices) == 0:
            return {"error": "No valid prices available"}
        
        if len(valid_prices) == 1:
            source, price = list(valid_prices.items())[0]
            return {
                "max_variance": 0.0,
                "mean_price": price,
                "price_range": (price, price),
                "outliers": [],
                "consensus_price": price,
                "single_source": source
            }
        
        # Calculate statistics
        price_values = list(valid_prices.values())
        mean_price = sum(price_values) / len(price_values)
        min_price = min(price_values)
        max_price = max(price_values)
        
        # Calculate max variance from mean
        max_variance = max(
            abs(p - mean_price) / mean_price * 100 for p in price_values
        )
        
        # Identify outliers (>2% from mean)
        outliers = [
            source for source, price in valid_prices.items()
            if abs(price - mean_price) / mean_price * 100 > 2.0
        ]
        
        return {
            "max_variance": round(max_variance, 2),
            "mean_price": round(mean_price, 2),
            "price_range": (round(min_price, 2), round(max_price, 2)),
            "outliers": outliers,
            "consensus_price": round(mean_price, 2),  # Use mean as consensus
            "sources": valid_prices
        }
    
    @staticmethod
    async def validate_trade_price(
        ticker: str,
        intended_price: Optional[float] = None,
        threshold: float = None
    ) -> Dict[str, any]:
        """
        Validate price before trade execution.
        
        Args:
            ticker: Stock ticker
            intended_price: Price user/agent intends to trade at
            threshold: Max allowed variance percentage (default: 0.5%)
        
        Returns:
            {
                "valid": True/False,
                "action": "proceed" | "warn" | "block",
                "variance": 0.5,
                "recommended_price": 150.25,
                "message": "Price validation successful",
                "details": {...}
            }
        """
        if threshold is None:
            threshold = PriceValidator.DEFAULT_VARIANCE_THRESHOLD
        
        # Fetch prices from all sources
        prices = await PriceValidator.fetch_multi_source_price(ticker)
        variance_data = PriceValidator.calculate_variance(prices)
        
        if "error" in variance_data:
            return {
                "valid": False,
                "action": "block",
                "message": "Unable to validate price - no data sources available",
                "details": variance_data
            }
        
        max_variance = variance_data["max_variance"]
        consensus_price = variance_data["consensus_price"]
        
        # Determine action based on variance
        if max_variance <= PriceValidator.DEFAULT_VARIANCE_THRESHOLD:
            action = "proceed"
            message = f"Price validated. Variance {max_variance:.2f}% within acceptable range."
        elif max_variance <= PriceValidator.WARNING_VARIANCE_THRESHOLD:
            action = "warn"
            message = f"⚠️ Price variance {max_variance:.2f}% detected. Recommend review before proceeding."
        elif max_variance <= PriceValidator.CRITICAL_VARIANCE_THRESHOLD:
            action = "warn"
            message = f"⚠️ HIGH price variance {max_variance:.2f}%! Strongly recommend manual review."
        else:
            action = "block"
            message = f"🚫 CRITICAL price variance {max_variance:.2f}%! Trade blocked for safety."
        
        # If user provided intended price, validate against it too
        if intended_price:
            intended_variance = abs(intended_price - consensus_price) / consensus_price * 100
            if intended_variance > threshold:
                action = "warn"
                currency = prices.get("currency", "USD")
                symbol = "£" if currency == "GBP" else "$"
                message += f" Your intended price ({symbol}{intended_price:.2f}) differs {intended_variance:.2f}% from market consensus."
        
        return {
            "valid": action == "proceed",
            "action": action,
            "variance": max_variance,
            "recommended_price": consensus_price,
            "message": message,
            "details": {
                **variance_data,
                "prices": prices,
                "threshold": threshold
            }
        }
