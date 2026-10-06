"""The one OpenAI-compatible provider.

Every chat role goes through ``build_chat_model`` (LangChain) or
``role_magentic_model`` (magentic). Both take their endpoint, key and model from
a ``ResolvedRole`` and pass them explicitly, so ``OPENAI_*`` and ``MAGENTIC_*``
environment variables cannot redirect a role (P8). xAI, LM Studio and Ollama
differ only in ``base_url`` and ``api_key_env`` inside ``models.json``.

There is no fallback. A failed call raises ``ProviderError`` and nothing else is
tried: retries are off, and no other provider or model is consulted.

The ``kind`` dispatch below is the single place the code branches on provider
kind. Provider ids and model strings are never inspected.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Callable, List, Optional, Sequence

import httpx

from .errors import ModelRegistryError, ProviderError
from .loader import ResolvedRole, get_active_registry
from .schemas import KIND_OPENAI_COMPATIBLE

logger = logging.getLogger(__name__)


# --- error mapping ---------------------------------------------------------


def map_exception(exc: BaseException, role: str) -> BaseException:
    """Translate a transport or API failure into ``ProviderError``.

    Anything that is not a network or HTTP-status failure is returned
    unchanged: a bug in the caller must not be relabelled as a provider fault.
    """

    import openai

    if isinstance(exc, (ProviderError, ModelRegistryError)):
        return exc
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        if status in (401, 403):
            return ProviderError("PROVIDER_AUTH_FAILED", role)
        if status == 404:
            return ProviderError("PROVIDER_MODEL_NOT_FOUND", role)
        if status == 429:
            return ProviderError("PROVIDER_RATE_LIMITED", role)
        if status >= 500:
            return ProviderError("PROVIDER_SERVER_ERROR", role)
        return ProviderError("PROVIDER_REQUEST_REJECTED", role)
    if isinstance(exc, (openai.APITimeoutError, httpx.TimeoutException)):
        return ProviderError("PROVIDER_TIMEOUT", role)
    if isinstance(exc, (openai.APIConnectionError, httpx.TransportError)):
        return ProviderError("PROVIDER_UNREACHABLE", role)
    return exc


# --- LangChain path --------------------------------------------------------


class RoleChatModel:
    """A role-bound chat model with typed errors.

    Wraps one ``ChatOpenAI`` built from the role. ``ainvoke`` and ``astream``
    mirror the LangChain methods the agents already use.
    """

    def __init__(self, resolved: ResolvedRole, inner: Any) -> None:
        self.resolved = resolved
        self.role = resolved.role
        self.model_id = resolved.model
        self.inner = inner

    def __repr__(self) -> str:
        return f"RoleChatModel(role={self.role!r}, model={self.model_id!r})"

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        try:
            return await self.inner.ainvoke(messages, **kwargs)
        except Exception as exc:
            mapped = map_exception(exc, self.role)
            if mapped is exc:
                raise
            raise mapped from None

    async def astream(self, messages: Any, **kwargs: Any) -> AsyncIterator[Any]:
        try:
            async for chunk in self.inner.astream(messages, **kwargs):
                yield chunk
        except Exception as exc:
            mapped = map_exception(exc, self.role)
            if mapped is exc:
                raise
            raise mapped from None


def _require_key(resolved: ResolvedRole) -> str:
    """The key value, or "" for a keyless provider. A named but unset key fails."""

    if resolved.api_key_env is None:
        return ""
    key = resolved.api_key()
    if key is None:
        raise ProviderError("KEY_MISSING", resolved.role)
    return key


def _build_openai_compatible(
    resolved: ResolvedRole, http_client: Any = None
) -> RoleChatModel:
    from langchain_openai import ChatOpenAI
    from pydantic import SecretStr

    key = _require_key(resolved)
    params: dict[str, Any] = {
        "model": resolved.model,
        "base_url": resolved.base_url,
        # SecretStr("") reaches the client as an explicit empty key: it sends no
        # Authorization header and never falls back to OPENAI_API_KEY.
        "api_key": SecretStr(key),
        "timeout": resolved.timeout_s,
        "max_retries": 0,
    }
    if resolved.temperature is not None:
        params["temperature"] = resolved.temperature
    if resolved.max_tokens is not None:
        # langchain-openai renames max_tokens to max_completion_tokens, which
        # LM Studio and Ollama may ignore. extra_body sends the classic field
        # every OpenAI-compatible server accepts.
        params["extra_body"] = {"max_tokens": resolved.max_tokens}
    if resolved.top_p is not None:
        params["top_p"] = resolved.top_p
    if isinstance(http_client, httpx.AsyncClient):
        params["http_async_client"] = http_client
    elif isinstance(http_client, httpx.Client):
        params["http_client"] = http_client
    return RoleChatModel(resolved, ChatOpenAI(**params))


# The kind dispatch. Allowlisted by the static name-guessing test.
_CHAT_BUILDERS: dict[str, Callable[..., RoleChatModel]] = {
    KIND_OPENAI_COMPATIBLE: _build_openai_compatible,
}


def build_chat_model(resolved: ResolvedRole, *, http_client: Any = None) -> RoleChatModel:
    """Build the chat model for a resolved role.

    ``http_client`` is a test hook: an ``httpx.AsyncClient`` (async calls) or an
    ``httpx.Client`` (sync calls), typically with a ``MockTransport``.
    """

    builder = _CHAT_BUILDERS.get(resolved.kind)
    if builder is None:
        raise ProviderError("PROVIDER_KIND_UNSUPPORTED", resolved.role)
    return builder(resolved, http_client=http_client)


def chat_model_for_role(role: str, *, http_client: Any = None) -> RoleChatModel:
    """Resolve ``role`` in the active registry and build its chat model."""

    return build_chat_model(get_active_registry().resolve(role), http_client=http_client)


# --- magentic path ---------------------------------------------------------


def role_magentic_model(resolved: ResolvedRole) -> Any:
    """A magentic ``OpenaiChatModel`` bound to the role.

    magentic reads ``OPENAI_BASE_URL``, ``OPENAI_API_KEY`` and
    ``MAGENTIC_OPENAI_*`` only when it is not handed a model. Entering this
    model as a context manager (or passing it to a prompt function) bypasses all
    of them: endpoint, key and model id come from the role.
    """

    from magentic import OpenaiChatModel

    if resolved.kind != KIND_OPENAI_COMPATIBLE:
        raise ProviderError("PROVIDER_KIND_UNSUPPORTED", resolved.role)
    kwargs: dict[str, Any] = {
        "api_key": _require_key(resolved),
        "base_url": resolved.base_url,
    }
    if resolved.temperature is not None:
        kwargs["temperature"] = resolved.temperature
    if resolved.max_tokens is not None:
        kwargs["max_tokens"] = resolved.max_tokens
    return OpenaiChatModel(resolved.model, **kwargs)


def role_pydantic_ai_model(resolved: ResolvedRole) -> Any:
    """A pydantic-ai ``OpenAIModel`` bound to the role (the swarm uses this).

    The ``AsyncOpenAI`` client gets the endpoint and key explicitly, with
    retries off, so ``OPENAI_BASE_URL`` and ``OPENAI_API_KEY`` are never read.
    """

    import openai
    from pydantic_ai.models.openai import OpenAIModel
    from pydantic_ai.providers.openai import OpenAIProvider

    if resolved.kind != KIND_OPENAI_COMPATIBLE:
        raise ProviderError("PROVIDER_KIND_UNSUPPORTED", resolved.role)
    client = openai.AsyncOpenAI(
        base_url=resolved.base_url,
        api_key=_require_key(resolved),
        timeout=resolved.timeout_s,
        max_retries=0,
    )
    return OpenAIModel(resolved.model, provider=OpenAIProvider(openai_client=client))


async def run_magentic(role: str, func: Callable[..., Any], *args: Any) -> Any:
    """Run a magentic prompt function inside the model bound to ``role``.

    The role resolves first: a missing role raises ``ModelRoleMissing`` before
    any thread starts or request leaves the process. The prompt function runs in
    a worker thread because magentic's sync API blocks.
    """

    resolved = get_active_registry().resolve(role)
    model = role_magentic_model(resolved)

    def _call() -> Any:
        with model:
            return func(*args)

    try:
        return await asyncio.to_thread(_call)
    except Exception as exc:
        mapped = map_exception(exc, role)
        if mapped is exc:
            raise
        raise mapped from None


# --- message helpers -------------------------------------------------------


def image_message_content(text: str, images: Optional[Sequence[str]]) -> Any:
    """OpenAI vision content: text plus one ``image_url`` part per image.

    ``images`` are base64 strings or data URLs. With no images the text is
    returned as a plain string.
    """

    if not images:
        return text
    parts: List[dict[str, Any]] = [{"type": "text", "text": text}]
    for image in images:
        url = image if image.startswith("data:") else f"data:image/png;base64,{image}"
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts
