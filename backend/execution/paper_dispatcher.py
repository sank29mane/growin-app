from execution.models import OrderAck

class PaperDispatcher:
    async def dispatch(self, intent):
        return OrderAck(
            proposal_id=intent.proposal_id,
            broker_order_id="paper-" + intent.proposal_id,
            status="SUBMITTED"
        )
