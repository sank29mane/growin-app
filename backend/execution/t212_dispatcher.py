"""Trading 212 compatibility dispatcher behind the canonical execution seam."""

import json
from typing import Any, Dict, Iterable

from .models import OrderAck, OrderIntent
from .service import BrokerExecutionError, BrokerOutcomeUnknownError


_FAILURE_STATUSES = {"ERROR", "FAILED", "FAILURE", "REJECTED", "BLOCKED"}


class Trading212Dispatcher:
    def __init__(self, mcp_client: Any):
        self._mcp_client = mcp_client

    async def dispatch(self, intent: OrderIntent) -> OrderAck:
        result = await self._mcp_client.call_tool(
            "place_market_order",
            {
                "ticker": intent.ticker,
                "quantity": float(intent.quantity),
                "order_type": intent.side.value,
            },
        )
        payload = _parse_mcp_result(result)
        order_id = _find_order_id(payload)
        if not order_id:
            raise BrokerOutcomeUnknownError(
                "Broker acknowledgement omitted an order id and requires reconciliation"
            )

        status = str(payload.get("status") or payload.get("Status") or "ACKNOWLEDGED")
        return OrderAck(
            proposal_id=intent.proposal_id,
            broker="trading212",
            broker_order_id=order_id,
            status=status,
            raw=payload,
        )


def _parse_mcp_result(result: Any) -> Dict[str, Any]:
    if getattr(result, "isError", False) or getattr(result, "is_error", False):
        raise BrokerExecutionError("MCP reported a broker execution error")

    content = getattr(result, "content", result)
    items: Iterable[Any] = content if isinstance(content, (list, tuple)) else [content]
    payloads = []
    for item in items:
        value = getattr(item, "text", item)
        if isinstance(value, dict):
            payloads.append(value)
            continue
        if not isinstance(value, str):
            continue
        text = value.strip()
        json_start = text.find("{")
        if json_start >= 0:
            try:
                parsed = json.loads(text[json_start:])
            except json.JSONDecodeError as exc:
                raise BrokerExecutionError("Broker returned malformed JSON") from exc
            if isinstance(parsed, dict):
                payloads.append(parsed)
                continue
        if any(marker in text.lower() for marker in ("error", "failed", "blocked", "rejected")):
            raise BrokerExecutionError("Broker rejected the order")

    if not payloads:
        raise BrokerExecutionError("Broker returned no structured acknowledgement")

    payload = payloads[0]
    error = payload.get("error") if "error" in payload else payload.get("Error")
    if error not in (None, "", False, 0):
        raise BrokerExecutionError("Broker rejected the order")
    success = payload.get("success") if "success" in payload else payload.get("Success")
    if success is False:
        raise BrokerExecutionError("Broker rejected the order")

    status = str(payload.get("status") or payload.get("Status") or "").upper()
    if status in _FAILURE_STATUSES:
        raise BrokerExecutionError("Broker rejected the order")
    return payload


def _find_order_id(payload: Dict[str, Any]) -> str:
    for key in ("orderId", "order_id", "id", "orderReference", "order_reference"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    success = payload.get("Success") or payload.get("success")
    if isinstance(success, dict):
        return _find_order_id(success)
    return ""
