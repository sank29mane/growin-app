"""
Price validation and variance checking across data sources.
Ensures price consistency before executing trades.
"""

from typing import Dict, Optional, Any
from decimal import Decimal
from utils.currency_utils import DataSourceNormalizer
import asyncio
import logging

logger = logging.getLogger(__name__)

class PriceValidator:
    """
    Validates prices across multiple data sources before trade execution.
    Implements threshold checking and variance reporting using Decimal for precision.
    """

    # Threshold configuration
    DEFAULT_VARIANCE_THRESHOLD = Decimal('0.5')  # 0.5% variance allowed
    WARNING_VARIANCE_THRESHOLD = Decimal('1.0')  # Warn user if > 1%
    CRITICAL_VARIANCE_THRESHOLD = Decimal('3.0')  # Block trade if > 3%

    @staticmethod
    async def fetch_multi_source_price(ticker: str) -> Dict[str, Any]:
        """
        Fetch price from all available sources.

        Returns:
            {
                "trading212": Decimal('150.25'),
                "alpaca": Decimal('150.22'),
                "yfinance": Decimal('150.20'),
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
                # data_engine's get_historical_bars now returns dict with 'bars' list of dicts (from PriceData.model_dump)
                # values are Decimal objects
                data = await alpaca_client.get_historical_bars(normalized_ticker, limit=1)
                if data and "bars" in data and data["bars"]:
                    # PriceData dump has 'close' as Decimal (if not json dump mode)
                    raw_price = data["bars"][0]["close"]
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
    def calculate_variance(prices: Dict[str, Any]) -> Dict[str, Any]:
        """
        Calculate price variance across sources.
        """
        # Filter out None values and currency
        valid_prices = {k: v for k, v in prices.items() if v is not None and k != "currency"}

        if len(valid_prices) == 0:
            return {"error": "No valid prices available"}

        if len(valid_prices) == 1:
            source, price = list(valid_prices.items())[0]
            price_dec = Decimal(str(price))
            return {
                "max_variance": Decimal('0.0'),
                "mean_price": price_dec,
                "price_range": (price_dec, price_dec),
                "outliers": [],
                "consensus_price": price_dec,
                "single_source": source
            }

        # Calculate statistics
        price_values = [Decimal(str(v)) for v in valid_prices.values()]

        # Calculate mean
        total = sum(price_values)
        count = Decimal(len(price_values))
        mean_price = total / count

        min_price = min(price_values)
        max_price = max(price_values)

        # Calculate max variance from mean in percentage
        # variance = abs(price - mean) / mean * 100
        variances = [(abs(p - mean_price) / mean_price * Decimal('100.0')) for p in price_values]
        max_variance = max(variances)

        # Identify outliers (>2% from mean)
        outlier_threshold = Decimal('2.0')
        outliers = [
            source for source, price in valid_prices.items()
            if (abs(Decimal(str(price)) - mean_price) / mean_price * Decimal('100.0')) > outlier_threshold
        ]

        # Round for display
        return {
            "max_variance": max_variance.quantize(Decimal("0.01")),
            "mean_price": mean_price.quantize(Decimal("0.01")),
            "price_range": (min_price.quantize(Decimal("0.01")), max_price.quantize(Decimal("0.01"))),
            "outliers": outliers,
            "consensus_price": mean_price.quantize(Decimal("0.01")),  # Use mean as consensus
            "sources": valid_prices
        }

    @staticmethod
    async def validate_trade_price(
        ticker: str,
        intended_price: Optional[float] = None,
        threshold: float = None
    ) -> Dict[str, Any]:
        """
        Validate price before trade execution.
        """
        d_threshold = Decimal(str(threshold)) if threshold is not None else PriceValidator.DEFAULT_VARIANCE_THRESHOLD

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
            message = f"Price validated. Variance {max_variance}% within acceptable range."
        elif max_variance <= PriceValidator.WARNING_VARIANCE_THRESHOLD:
            action = "warn"
            message = f"⚠️ Price variance {max_variance}% detected. Recommend review before proceeding."
        elif max_variance <= PriceValidator.CRITICAL_VARIANCE_THRESHOLD:
            action = "warn"
            message = f"⚠️ HIGH price variance {max_variance}%! Strongly recommend manual review."
        else:
            action = "block"
            message = f"🚫 CRITICAL price variance {max_variance}%! Trade blocked for safety."

        # If user provided intended price, validate against it too
        if intended_price:
            d_intended = Decimal(str(intended_price))
            intended_variance = abs(d_intended - consensus_price) / consensus_price * Decimal('100.0')
            if intended_variance > d_threshold:
                action = "warn"
                currency = prices.get("currency", "USD")
                symbol = "£" if currency == "GBP" else "$"
                message += f" Your intended price ({symbol}{d_intended:.2f}) differs {intended_variance:.2f}% from market consensus."

        return {
            "valid": action == "proceed",
            "action": action,
            "variance": float(max_variance), # Convert to float for JSON response compatibility if needed
            "recommended_price": float(consensus_price), # Convert to float for JSON response
            "message": message,
            "details": {
                **variance_data,
                "prices": prices,
                "threshold": float(d_threshold)
            }
        }
