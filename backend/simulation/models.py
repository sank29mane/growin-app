import numpy as np

class MarketImpactModel:
    """
    Estimates non-linear slippage and spread expansion based on L2 depth and trade size.
    Uses vectorized NumPy operations to keep execution time under sub-millisecond level.
    """
    def __init__(self, impact_exponent: float = 0.5):
        self.impact_exponent = impact_exponent

    def calculate_slippage(
        self,
        trade_size: float,
        bid_depth: np.ndarray,
        ask_depth: np.ndarray,
        mid_price: float
    ) -> float:
        """
        Estimate slippage (absolute difference between execution price and mid price).
        
        Parameters:
            trade_size (float): The proposed trade quantity (positive for buy, negative for sell).
            bid_depth (np.ndarray): 1D array of sizes or 2D array of [price, size] on bid side.
            ask_depth (np.ndarray): 1D array of sizes or 2D array of [price, size] on ask side.
            mid_price (float): Current asset mid-price.
            
        Returns:
            float: Estimated slippage.
        """
        abs_size = abs(trade_size)
        if abs_size <= 1e-8:
            return 0.0

        is_buy = trade_size > 0
        depth = ask_depth if is_buy else bid_depth

        if depth is None or len(depth) == 0:
            # Fallback to standard square-root market impact model if depth is unavailable
            return float(mid_price * 0.0002 * (abs_size ** self.impact_exponent))

        # Check if depth is 2D [[price, size], ...]
        if depth.ndim == 2 and depth.shape[1] == 2:
            prices = depth[:, 0]
            sizes = depth[:, 1]
        else:
            # Handle 1D array of sizes, projecting prices relative to mid price
            sizes = depth.astype(np.float64)
            n_levels = len(sizes)
            step_pct = 0.001  # 0.1% price step per level
            if is_buy:
                prices = mid_price * (1.0 + step_pct * (np.arange(n_levels) + 1.0))
            else:
                prices = mid_price * (1.0 - step_pct * (np.arange(n_levels) + 1.0))

        # Vectorized walk of the book to find average fill price
        accumulated_qty = 0.0
        weighted_price_sum = 0.0
        remaining_qty = abs_size

        valid_mask = sizes > 0
        valid_sizes = sizes[valid_mask]
        valid_prices = prices[valid_mask]

        if len(valid_sizes) > 0:
            cum_sizes = np.cumsum(valid_sizes)
            idx = np.searchsorted(cum_sizes, abs_size)

            if idx == 0:
                fill_qty = min(abs_size, valid_sizes[0])
                weighted_price_sum = float(fill_qty * valid_prices[0])
                accumulated_qty = float(fill_qty)
                remaining_qty = abs_size - accumulated_qty
            else:
                num_full_levels = min(idx, len(valid_sizes))
                accumulated_qty = float(cum_sizes[num_full_levels - 1])
                weighted_price_sum = float(np.dot(valid_sizes[:num_full_levels], valid_prices[:num_full_levels]))
                remaining_qty = abs_size - accumulated_qty

                if idx < len(valid_sizes) and remaining_qty > 1e-8:
                    fill_qty = min(remaining_qty, valid_sizes[idx])
                    weighted_price_sum += float(fill_qty * valid_prices[idx])
                    accumulated_qty += float(fill_qty)
                    remaining_qty -= float(fill_qty)

        if accumulated_qty < abs_size:
            # Trade size exceeds available depth; apply penalty on remaining size
            # Extract last_price from original unmasked prices array per instructions
            last_price = prices[-1] if len(prices) > 0 else mid_price
            penalty_pct = 0.05  # 5% penalty for illiquidity exceedance
            penalty_price = last_price * (1.0 + penalty_pct if is_buy else 1.0 - penalty_pct)
            weighted_price_sum += remaining_qty * penalty_price
            accumulated_qty += remaining_qty

        avg_fill_price = weighted_price_sum / abs_size
        slippage = abs(avg_fill_price - mid_price)

        # Vectorized spread expansion penalty proportional to volume size relative to total book volume
        total_depth_volume = np.sum(sizes)
        if total_depth_volume > 0:
            ratio = abs_size / total_depth_volume
            spread_expansion = mid_price * 0.001 * (ratio ** 2)
            slippage += spread_expansion

        return float(slippage)
