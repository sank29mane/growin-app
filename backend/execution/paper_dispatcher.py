"""Deterministic, local-only acknowledgement for paper orders."""

import hashlib
import json
from typing import Any, Dict, Tuple

from .models import OrderAck, OrderIntent, OrderMode
from .service import BrokerExecutionError


class PaperDispatcher:
    """Acknowledge an immutable order intent without contacting a broker."""

    async def dispatch(self, intent: OrderIntent) -> OrderAck:
        if intent.mode is not OrderMode.PAPER:
            raise BrokerExecutionError("Paper dispatcher refuses non-paper intent")
        identity, identity_source = _identity_material(intent)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()

        return OrderAck(
            proposal_id=intent.proposal_id,
            broker="paper",
            broker_order_id=f"paper-{digest[:32]}",
            status="ACKNOWLEDGED",
            raw={
                "execution_mode": "paper",
                "identity_source": identity_source,
                "identity_digest": f"sha256:{digest}",
            },
        )


def _identity_material(intent: OrderIntent) -> Tuple[str, str]:
    client_order_id = getattr(intent, "client_order_id", "")
    if client_order_id:
        return str(client_order_id), "client_order_id"

    canonical_intent = _canonical_intent(intent)
    fallback = {
        "proposal_id": str(intent.proposal_id),
        "intent": canonical_intent,
    }
    return _canonical_json(fallback), "canonical_intent"


def _canonical_intent(intent: OrderIntent) -> Dict[str, Any]:
    model_dump = getattr(intent, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", exclude_none=False)
    return dict(vars(intent))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
