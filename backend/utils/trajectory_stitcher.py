"""
Trajectory Stitcher - Coherent reasoning synthesis for MAS.
Merges disparate specialist signals into a unified chronological narrative.
"""

import logging
from typing import List, Dict, Any, Optional
from backend.market_context import MarketContext

logger = logging.getLogger(__name__)

class TrajectoryStitcher:
    """
    Stitches multiple agent trajectories into a single linear timeline.
    Prevents fragmented responses by identifying logical dependencies.
    """
    
    @staticmethod
    def stitch(context: MarketContext) -> str:
        """
        Produce a coherent reasoning narrative from context data.
        """
        segments = []
        
        # 1. Market Foundation
        if context.price:
            segments.append(f"Market opened with {context.ticker} at {context.price.currency}{context.price.current_price:.2f}.")
            
        # 2. Technical Intelligence (Quant)
        if context.quant:
            q = context.quant
            signal_val = q.signal.value if hasattr(q.signal, 'value') else q.signal
            segments.append(f"Technical analysis identifies a {signal_val} signal, with RSI at {q.rsi:.1f} and established support at {q.support_level:.2f}.")
            
        # 3. Institutional & Sentiment Layer (Research/Whale)
        if context.research:
            r = context.research
            segments.append(f"Institutional sentiment is {r.sentiment_label} ({r.sentiment_score:.2f}), driven by recent {len(r.articles)} regulatory and news catalysts.")
            
        if context.whale:
            w = context.whale
            if w.sentiment_impact != "NEUTRAL":
                segments.append(f"Institutional block trades (Whale Watch) show a {w.sentiment_impact} bias, confirming smart-money alignment.")
        
        # 4. Forward Projection (Forecast)
        if context.forecast:
            f = context.forecast
            segments.append(f"Predictive modeling projects a {f.trend} trajectory over the next 24h, targeting ${f.forecast_24h:.2f} with {f.confidence} confidence.")
            
        # 5. Risk & Liquidity (SOTA 2026 Phase 28)
        if context.risk_governance:
            rg = context.risk_governance
            if rg.vix_level:
                segments.append(f"Macro volatility (VIX: {rg.vix_level:.1f}) and systemic risk ({rg.systemic_risk_level}) establish the risk baseline.")
            if rg.slippage_bps:
                segments.append(f"Execution liquidity is {rg.liquidity_status}, with estimated slippage impact of {rg.slippage_bps:.1f} bps.")

        # 6. Synthesis
        if not segments:
            return "No specialist signals available for stitching."
            
        stitched = " ".join(segments)
        return stitched
