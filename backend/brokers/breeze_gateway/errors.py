"""Errors of the Mac relay client (contract growin-relay/1, C6 and C9).

One RelayError subclass per server code, plus the client-side errors. The text
of any RelayError is the code and the request id and nothing else: never a
header, a body, a parameter value, a token or a URL.
"""

from __future__ import annotations


class RelayError(Exception):
    """A failure talking to the relay. `str(err)` is `<code> (request <id>)`."""

    code: str = "RELAY_ERROR"

    def __init__(
        self,
        code: str | None = None,
        *,
        http_status: int | None = None,
        reason: str | None = None,
        breeze_status: int | None = None,
        retryable: bool = False,
        retry_after_s: int | None = None,
        request_id: str | None = None,
    ) -> None:
        self.code = code if code is not None else type(self).code
        self.http_status = http_status
        self.reason = reason
        self.breeze_status = breeze_status
        self.retryable = retryable
        self.retry_after_s = retry_after_s
        self.request_id = request_id
        super().__init__(self.code, request_id)

    def __str__(self) -> str:
        return f"{self.code} (request {self.request_id})"

    __repr__ = __str__


# ------------------------------------------------------- server codes (C6)


class Unauthorized(RelayError):
    code = "UNAUTHORIZED"


class NoSession(RelayError):
    code = "NO_SESSION"


class SessionExpired(RelayError):
    code = "SESSION_EXPIRED"


class EgressMismatch(RelayError):
    code = "EGRESS_MISMATCH"


class BudgetUnavailable(RelayError):
    code = "BUDGET_UNAVAILABLE"


class BudgetExhausted(RelayError):
    code = "BUDGET_EXHAUSTED"


class UpstreamError(RelayError):
    code = "UPSTREAM_ERROR"


class UpstreamTimeout(RelayError):
    code = "UPSTREAM_TIMEOUT"


class RelayValidationError(RelayError):
    code = "VALIDATION"


class LoginAttemptInvalid(RelayError):
    code = "LOGIN_ATTEMPT_INVALID"


class IdentityMismatch(RelayError):
    code = "IDENTITY_MISMATCH"


class ForbiddenClient(RelayError):
    code = "FORBIDDEN_CLIENT"


class ContractMismatch(RelayError):
    code = "CONTRACT_MISMATCH"


class NotFound(RelayError):
    code = "NOT_FOUND"


class MethodNotAllowed(RelayError):
    code = "METHOD_NOT_ALLOWED"


class InternalRelayError(RelayError):
    code = "INTERNAL"


CODE_TO_CLASS: dict[str, type[RelayError]] = {
    cls.code: cls
    for cls in (
        Unauthorized,
        NoSession,
        SessionExpired,
        EgressMismatch,
        BudgetUnavailable,
        BudgetExhausted,
        UpstreamError,
        UpstreamTimeout,
        RelayValidationError,
        LoginAttemptInvalid,
        IdentityMismatch,
        ForbiddenClient,
        ContractMismatch,
        NotFound,
        MethodNotAllowed,
        InternalRelayError,
    )
}


# ------------------------------------------------------------ client side


class RelayContractMismatch(RelayError):
    """The response carried no contract header, or a different value. Never retried."""

    code = "RELAY_CONTRACT_MISMATCH"


class RelayHashMismatch(RelayError):
    """The body does not match X-Growin-Upstream-Sha256."""

    code = "RELAY_HASH_MISMATCH"


class RelayProtocolError(RelayError):
    """Missing or malformed header, bad JSON, redirect, oversize body or unknown code.

    `reason` is a short closed word such as `missing_header`; it never holds a value.
    """

    code = "RELAY_PROTOCOL_ERROR"


class RelayUnavailable(RelayError):
    """Tunnel child gone, connect error or client timeout. The caller decides about a retry."""

    code = "RELAY_UNAVAILABLE"


class RelayArgumentError(ValueError):
    """A caller argument failed validation before anything was sent.

    The text names the parameter only, never its value.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"invalid_argument: {name}")


class TunnelError(Exception):
    """The IAP tunnel would not start. `str(err)` is the code only, never gcloud output."""

    def __init__(self, code: str, *, returncode: int | None = None) -> None:
        self.code = code
        self.returncode = returncode
        super().__init__(code)

    def __str__(self) -> str:
        return self.code
