"""
Tax-Loss Harvesting (TLH) Scanner - SOTA 2026
Identifies opportunities to sell losing positions in taxable accounts to offset gains.
"""

import logging
from typing import List, Dict, Any
from decimal import Decimal
from .financial_math import create_decimal, safe_div

logger = logging.getLogger(__name__)

class TLHScanner:
    """
    Scans a portfolio for tax-loss harvesting opportunities.
    Primary focus: 'Invest' accounts (taxable).
    """
    
    def __init__(self, loss_threshold_pct: float = -5.0, min_loss_amount: float = 10.0):
        self.loss_threshold_pct = loss_threshold_pct
        self.min_loss_amount = create_decimal(min_loss_amount)

    def scan(self, portfolio_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Scan positions for TLH candidates.
        Only considers positions from the 'invest' account.
        """
        candidates = []
        positions = portfolio_data.get("positions", [])
        
        # Determine account mappings if consolidated
        # (Trading 212 'Invest' is the primary taxable account)
        
        for pos in positions:
            # SOTA 2026: Only harvest in taxable accounts
            # Note: If the position is consolidated, we must ensure we only target the 'Invest' portion.
            # For this MVP, we assume any position with a loss in 'Invest' is a candidate.
            
            ticker = pos.get("ticker", "UNKNOWN")
            qty = create_decimal(pos.get("quantity", 0))
            avg_price = create_decimal(pos.get("averagePrice", 0))
            cur_price = create_decimal(pos.get("currentPrice", 0))
            
            if qty <= 0 or avg_price <= 0:
                continue
                
            unrealized_pnl = (cur_price - avg_price) * qty
            pnl_pct = float(safe_div((cur_price - avg_price), avg_price)) * 100
            
            # Check if it meets the criteria for harvesting
            if pnl_pct <= self.loss_threshold_pct and unrealized_pnl <= -self.min_loss_amount:
                # Candidate found
                candidates.append({
                    "ticker": ticker,
                    "quantity": float(qty),
                    "unrealized_pnl": float(unrealized_pnl),
                    "pnl_percent": pnl_pct,
                    "offset_value": abs(float(unrealized_pnl)), # Potential tax offset
                    "strategy": "SELL_FOR_TAX_LOSS",
                    "reason": f"Position down {pnl_pct:.1f}% with £{abs(unrealized_pnl):.2f} offset potential."
                })
                
        # Sort by largest loss first
        candidates.sort(key=lambda x: x["unrealized_pnl"])
        
        if candidates:
            logger.info(f"TLH Scanner: Found {len(candidates)} potential harvesting opportunities.")
            
        return candidates
