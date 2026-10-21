"""Mac side of the Breeze gateway: the one RelayClient for contract growin-relay/1.

All Breeze traffic goes through the VM relay over an IAP tunnel this package
starts itself. The vendor Breeze SDK must never be imported on the Mac, and no
Breeze credential is stored here: the relay token is the relay's own bearer for
one session and the only secret the Mac keeps.
"""

from .client import (
    CONTRACT,
    LoginAttempt,
    RelayBudget,
    RelayClient,
    RelayHealth,
    SessionStatus,
    UpstreamPayload,
)
from .errors import (
    BudgetExhausted,
    BudgetUnavailable,
    CODE_TO_CLASS,
    ContractMismatch,
    EgressMismatch,
    ForbiddenClient,
    IdentityMismatch,
    InternalRelayError,
    LoginAttemptInvalid,
    MethodNotAllowed,
    NoSession,
    NotFound,
    RelayArgumentError,
    RelayContractMismatch,
    RelayError,
    RelayHashMismatch,
    RelayProtocolError,
    RelayUnavailable,
    RelayValidationError,
    SessionExpired,
    TunnelError,
    Unauthorized,
    UpstreamError,
    UpstreamTimeout,
)
from .token_store import MemoryTokenStore, StoredToken, TokenStore

__all__ = [
    "BudgetExhausted",
    "BudgetUnavailable",
    "CODE_TO_CLASS",
    "CONTRACT",
    "ContractMismatch",
    "EgressMismatch",
    "ForbiddenClient",
    "IdentityMismatch",
    "InternalRelayError",
    "LoginAttempt",
    "LoginAttemptInvalid",
    "MemoryTokenStore",
    "MethodNotAllowed",
    "NoSession",
    "NotFound",
    "RelayArgumentError",
    "RelayBudget",
    "RelayClient",
    "RelayContractMismatch",
    "RelayError",
    "RelayHashMismatch",
    "RelayHealth",
    "RelayProtocolError",
    "RelayUnavailable",
    "RelayValidationError",
    "SessionExpired",
    "SessionStatus",
    "StoredToken",
    "TokenStore",
    "TunnelError",
    "Unauthorized",
    "UpstreamError",
    "UpstreamTimeout",
    "UpstreamPayload",
]
