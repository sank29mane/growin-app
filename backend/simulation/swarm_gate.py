import logging

logger = logging.getLogger(__name__)

class RiskSwarmGate:
    """
    Capital scaling risk gate that dynamically queries the database for multipliers
    based on GMM regime IDs, and enforces hard limits on spreads.
    """
    def evaluate(
        self,
        simulated_fill_price: float,
        trade_size: float,
        regime_id: int,
        current_spread_pct: float,
        db_connection
    ) -> float:
        """
        Evaluate the capital scaling policy for a trade.
        
        Parameters:
            simulated_fill_price (float): Estimated fill price including slippage.
            trade_size (float): The proposed trade quantity.
            regime_id (int): The GMM regime ID.
            current_spread_pct (float): Current relative spread (e.g. 0.02 = 2.0%).
            db_connection: A database connection (SQLite or DuckDB).
            
        Returns:
            float: Scaled trade size. Returns 0.0 if spread exceeds 5.0% or query fails.
        """
        # Safety Guard: spread > 5.0% of mid-price -> block execution
        if current_spread_pct > 0.05:
            logger.warning(
                f"Trade blocked by RiskSwarmGate: current spread {current_spread_pct * 100:.2f}% "
                f"exceeds the 5.0% safety threshold."
            )
            return 0.0

        if db_connection is None:
            logger.error("RiskSwarmGate database connection is None. Blocking trade for safety.")
            return 0.0

        try:
            cursor = db_connection.cursor()
            cursor.execute(
                "SELECT scale_multiplier FROM scaling_policies WHERE regime_id = ?",
                (int(regime_id),)
            )
            row = cursor.fetchone()
            if row is not None:
                scale_multiplier = float(row[0])
            else:
                logger.warning(
                    f"Regime ID {regime_id} not found in scaling_policies table. "
                    f"Blocking trade for safety."
                )
                scale_multiplier = 0.0
        except Exception as e:
            logger.error(
                f"Error querying scaling policy for regime {regime_id}: {e}. "
                f"Blocking trade for safety."
            )
            return 0.0

        scaled_size = trade_size * scale_multiplier
        logger.info(
            f"RiskSwarmGate evaluation: regime={regime_id}, spread={current_spread_pct*100:.2f}%, "
            f"multiplier={scale_multiplier:.2f}, original_size={trade_size}, scaled_size={scaled_size}"
        )
        return scaled_size
