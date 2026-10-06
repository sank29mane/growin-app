"""Server-owned, single-use critic bindings. Never trust user_context markers."""

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import weakref

from market_context import MarketContext
from model_registry import ModelRegistryError, ROLE_RISK_CRITIC


@dataclass
class _Binding:
    context: weakref.ReferenceType
    proposal_id: str
    content_hash: str
    stage: str = "reviewed"


_bindings: dict[int, _Binding] = {}


def _canonical(value):
    # Tag containers as well as decimals so a caller-supplied dictionary
    # cannot hash like a Decimal with the same serialized value.
    if isinstance(value, dict):
        return {"dict": [(key, _canonical(value[key])) for key in sorted(value)]}
    if isinstance(value, list):
        return {"list": [_canonical(item) for item in value]}
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("unsupported proposal value")


def proposal_identity(proposal: dict) -> tuple[str, str]:
    try:
        proposal_id = proposal["proposal_id"]
        if not isinstance(proposal_id, str) or not proposal_id:
            raise ValueError("missing proposal id")
        content = json.dumps(_canonical(proposal), separators=(",", ":"), allow_nan=False)
        return proposal_id, hashlib.sha256(content.encode()).hexdigest()
    except (KeyError, ValueError, TypeError):
        raise ModelRegistryError("RISK_REVIEW_REQUIRED", ROLE_RISK_CRITIC) from None


def clear_review(context: MarketContext) -> None:
    _bindings.pop(id(context), None)
    context.user_context.pop("risk_review_succeeded", None)


def _bind_successful_review(context: MarketContext, identity: tuple[str, str]) -> None:
    """Called only by RiskAgent after validating the critic's successful result."""
    key = id(context)
    reference = weakref.ref(context, lambda ref: _bindings.pop(key, None))
    _bindings[key] = _Binding(reference, *identity)


def require_review(context: MarketContext, proposal: dict, stage: str = "reviewed") -> None:
    binding = _bindings.get(id(context))
    if (binding is None or binding.context() is not context or binding.stage != stage
            or (binding.proposal_id, binding.content_hash) != proposal_identity(proposal)):
        raise ModelRegistryError("RISK_REVIEW_REQUIRED", ROLE_RISK_CRITIC)


def consume_registration(context: MarketContext, proposal: dict) -> None:
    require_review(context, proposal)
    _bindings[id(context)].stage = "registered"


def consume_broadcast(context: MarketContext, proposal: dict) -> None:
    require_review(context, proposal, "registered")
    clear_review(context)
