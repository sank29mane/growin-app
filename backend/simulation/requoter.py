import asyncio
import logging

logger = logging.getLogger(__name__)

class AdaptiveReQuoter:
    def __init__(self, vol_tracker, alpaca_client, current_regime, interval: float = 5.0):
        self.vol_tracker = vol_tracker
        self.alpaca_client = alpaca_client
        self.current_regime = current_regime
        self.interval = interval
        self.active_orders = {}

    async def run_polling_loop(self):
        while True:
            try:
                await asyncio.wait_for(self.poll(), timeout=self.interval)
                await asyncio.sleep(self.interval)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                logger.info("AdaptiveReQuoter polling loop cancelled or timed out. Engaging kill switch.")
                await self.kill_switch()
                break
            except Exception as e:
                logger.error(f"Error in polling loop: {e}")
                await asyncio.sleep(self.interval)

    async def kill_switch(self):
        for order_id in list(self.active_orders.keys()):
            try:
                await self.alpaca_client.cancel_order(order_id)
                logger.info(f"Cancelled order {order_id} via kill switch")
            except Exception as e:
                logger.error(f"Failed to cancel order {order_id}: {e}")
        self.active_orders.clear()

    def calculate_collar(self, mid_price: float, spread_base: float, current_volatility: float):
        regime_multiplier = self.get_regime_multiplier(self.current_regime)
        margin = spread_base + (current_volatility * regime_multiplier)
        return mid_price - margin, mid_price + margin

    def get_regime_multiplier(self, regime: str) -> float:
        multipliers = {
            "low_vol": 1.0,
            "normal": 1.5,
            "high_vol": 2.0,
            "extreme": 3.0
        }
        return multipliers.get(regime, 1.5)

    async def poll(self):
        # To be implemented in next tasks
        pass
