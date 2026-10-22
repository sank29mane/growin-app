"""The one OpenAI-compatible provider.

Every chat role goes through ``build_chat_model`` (LangChain), ``run_structured``
(one plain call, the wire schema in the prompt, a strict parse) or ``role_pydantic_ai_model``. Each takes its
endpoint, key and model from a ``ResolvedRole`` and passes them explicitly, so
``OPENAI_*`` and ``MAGENTIC_*`` environment variables cannot redirect a role (P8). xAI, LM Studio and Ollama
differ only in ``base_url`` and ``api_key_env`` inside ``models.json``.

There is no fallback. A failed call raises ``ProviderError`` and nothing else is
tried: retries are off, and no other provider or model is consulted.

Every reply names the model that produced it. A server that answers a different
model than the one asked for (LM Studio serves its loaded model for an unknown
id) is a ``MODEL_MISMATCH``, and the reply is never used.

The ``kind`` dispatch below is the single place the code branches on provider
kind. Provider ids and model strings are never inspected.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, AsyncIterator, Callable, List, Optional, Sequence, Type, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .errors import ModelRegistryError, ProviderError
from .loader import ResolvedRole, get_active_registry
from .schemas import KIND_OPENAI_COMPATIBLE, ROLE_RISK_CRITIC
from .wire_schema import WireReplyError, wire_spec

logger = logging.getLogger(__name__)

_ModelT = TypeVar("_ModelT", bound=BaseModel)


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


def check_served_model(role: str, requested: str, served: Any) -> None:
    """Raise ``MODEL_MISMATCH`` unless the reply names exactly the requested model.

    The comparison is exact string equality: the registry id is what the operator
    pinned, and every runtime in the 2026-10 evidence reports that same id back.
    No case folding, prefix stripping or alias table, so nothing is loosened
    beyond what was observed. A reply with no model name is a mismatch too.

    Residual: on the LangChain ``ainvoke`` path ``langchain_openai`` fills
    ``response_metadata["model_name"]`` with the requested id when the server
    omits ``model``, so "no model name" cannot fire there. LM Studio always sends
    ``model``; ``run_structured`` reads the raw completion and has no such gap.
    """

    if not isinstance(served, str) or served != requested:
        raise ProviderError("MODEL_MISMATCH", role)


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
            reply = await self.inner.ainvoke(messages, **kwargs)
            metadata = getattr(reply, "response_metadata", None) or {}
            check_served_model(self.role, self.model_id, metadata.get("model_name"))
            return reply
        except Exception as exc:
            mapped = map_exception(exc, self.role)
            if mapped is exc:
                raise
            raise mapped from None

    async def astream(self, messages: Any, **kwargs: Any) -> AsyncIterator[Any]:
        try:
            named = False
            async for chunk in self.inner.astream(messages, **kwargs):
                # LangChain puts the served model on the finish chunk only, so
                # earlier tokens have already gone out. The check still runs before
                # the finish chunk is passed on, and a stream that never names its
                # model is a mismatch too.
                served = (getattr(chunk, "response_metadata", None) or {}).get("model_name")
                if served is not None:
                    check_served_model(self.role, self.model_id, served)
                    named = True
                yield chunk
            if not named:
                raise ProviderError("MODEL_MISMATCH", self.role)
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


# --- structured output path ------------------------------------------------


_SCHEMA_INSTRUCTION = (
    "Answer with a single JSON object matching this JSON schema and nothing else.\n"
    "JSON schema:\n"
)


def _with_schema_instruction(
    role: str, messages: Sequence[dict[str, Any]], schema_text: str
) -> list[dict[str, Any]]:
    """Copies of ``messages`` with the schema instruction appended to the last user message.

    The caller's messages are not changed. No user message at all is a caller bug and
    is refused before any request leaves the process.
    """

    copies = [dict(m) for m in messages]
    for index in range(len(copies) - 1, -1, -1):
        if copies[index].get("role") != "user":
            continue
        addition = "\n\n" + _SCHEMA_INSTRUCTION + schema_text
        content = copies[index].get("content")
        if isinstance(content, str):
            copies[index]["content"] = content + addition
        elif isinstance(content, list):
            copies[index]["content"] = [*content, {"type": "text", "text": addition.lstrip("\n")}]
        else:
            break
        return copies
    raise ProviderError("PROVIDER_REQUEST_REJECTED", role)


# The one rule for fenced output: the whole content is a single ```json block.
# Lowercase tag, a newline after it and before the closing fence, nothing outside.
_WHOLE_FENCE = re.compile(r"```json\n(.*)\n```", re.DOTALL)


def _unwrap_whole_fence(content: str) -> str:
    """The inner text when ``content`` is exactly one whole-content json fence, else ``content``.

    Local servers sometimes wrap a correct answer in ```json ... ```. This accepts
    that and nothing else: a full match (no prose before, no text after, no
    trailing newline), one block (the inner text holds no further fence) and the
    lowercase tag. The inner text is then strict-validated like any other reply.
    """

    match = _WHOLE_FENCE.fullmatch(content)
    if match is None or "```" in match.group(1):
        return content
    return match.group(1)


def _has_reasoning_evidence(completion: Any, message: Any) -> bool:
    """True when the server reports that the model reasoned.

    ``completion`` is the raw JSON body of the reply (a dict), never the SDK model: the
    SDK coerces ``"7"``, ``true`` and ``1.0`` into the positive int a typed field wants,
    which would turn garbage into evidence. Evidence is a raw JSON integer (not a bool)
    above zero in ``usage.completion_tokens_details.reasoning_tokens``, or a non-blank
    ``reasoning_content`` / ``reasoning`` on the message. Anything else (a string, a
    bool, a float, null, a negative, a missing field) is no evidence, so the caller
    that needs it fails closed.
    """

    usage = completion.get("usage") if isinstance(completion, dict) else None
    details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
    tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if type(tokens) is int and tokens > 0:
        return True
    for name in ("reasoning_content", "reasoning"):
        text = getattr(message, name, None)
        if isinstance(text, str) and text.strip():
            return True
    return False


async def run_structured(
    role: str,
    messages: Sequence[dict[str, Any]],
    schema: Type[_ModelT],
) -> _ModelT:
    """One chat call whose answer must be a ``schema`` instance.

    The request carries no ``response_format``, no ``tools`` and no ``tool_choice``.
    On LM Studio, constrained decoding switches a reasoning model's thinking off or
    leaves it with no room to answer, so the schema goes in the prompt instead: one
    fixed instruction and the JSON text of the strict wire schema
    (``wire_schema.wire_spec``) are appended to the last user message.

    The role resolves first: a missing role raises before any request leaves the
    process. Transport and API errors map through ``map_exception``. The reply is
    accepted only if it names the requested model, its ``finish_reason`` is
    ``"stop"`` (a reply cut off at the token limit is never parsed), and its content
    is strict JSON (optionally as one whole-content ```json fence, see
    ``_unwrap_whole_fence``) that matches the wire schema exactly and then
    validates, strictly, against the real ``schema``. For the risk critic the reply must also
    show that the model reasoned (``REASONING_ABSENT``): a verdict from a model that
    did not think is the no-think failure the critic evals recorded. Anything else
    raises ``ProviderError`` (``MODEL_MISMATCH``, ``STRUCTURED_OUTPUT_INVALID`` or
    ``REASONING_ABSENT``). There is no other lenient parse, no retry and no second model.
    """

    import openai

    resolved = get_active_registry().resolve(role)
    if resolved.kind != KIND_OPENAI_COMPATIBLE:
        raise ProviderError("PROVIDER_KIND_UNSUPPORTED", role)
    key = _require_key(resolved)
    spec = wire_spec(schema)
    request: dict[str, Any] = {
        "model": resolved.model,
        "messages": _with_schema_instruction(role, messages, json.dumps(spec.schema)),
    }
    if resolved.temperature is not None:
        request["temperature"] = resolved.temperature
    if resolved.max_tokens is not None:
        request["max_tokens"] = resolved.max_tokens
    if resolved.top_p is not None:
        request["top_p"] = resolved.top_p

    client = openai.AsyncOpenAI(
        base_url=resolved.base_url,
        api_key=key,
        timeout=resolved.timeout_s,
        max_retries=0,
    )
    try:
        try:
            raw = await client.chat.completions.with_raw_response.create(**request)
            completion = raw.parse()
            wire = raw.http_response.json() if role == ROLE_RISK_CRITIC else None
        finally:
            await client.close()
        check_served_model(role, resolved.model, getattr(completion, "model", None))
        choices = completion.choices or []
        if not choices or choices[0].finish_reason != "stop":
            raise ProviderError("STRUCTURED_OUTPUT_INVALID", role)
        message = choices[0].message
        if role == ROLE_RISK_CRITIC and not _has_reasoning_evidence(wire, message):
            raise ProviderError("REASONING_ABSENT", role)
        content = message.content
        if not isinstance(content, str) or not content:
            raise ProviderError("STRUCTURED_OUTPUT_INVALID", role)
        try:
            return schema.model_validate_json(spec.to_native_json(_unwrap_whole_fence(content)), strict=True)
        except (ValidationError, WireReplyError):
            # The model's text is never put in the error: it can echo the prompt.
            raise ProviderError("STRUCTURED_OUTPUT_INVALID", role) from None
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
