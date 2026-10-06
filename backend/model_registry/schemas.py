"""Schema v1 for ``private/models.json``.

The file maps roles to providers and models. Nothing in this module names a
vendor or a model: provider ids are labels chosen by the operator, and the
code reads only ``kind``.
"""

from __future__ import annotations

from typing import Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

# Provider kinds. ``openai_compatible`` speaks the OpenAI chat completions wire
# format and serves xAI, LM Studio, Ollama and any other compatible endpoint.
# ``hf_local`` is an in-process Hugging Face checkpoint and exists for the
# forecaster only.
KIND_OPENAI_COMPATIBLE = "openai_compatible"
KIND_HF_LOCAL = "hf_local"

# Roles. The forecaster is a time-series checkpoint; every other role is a chat
# model reached through an ``openai_compatible`` provider.
ROLE_COORDINATOR = "coordinator"
ROLE_DECISION = "decision"
ROLE_RESEARCH = "research"
ROLE_RISK_CRITIC = "risk_critic"
ROLE_MATH_CODEGEN = "math_codegen"
ROLE_FORECASTER = "forecaster"

CHAT_ROLES = (
    ROLE_COORDINATOR,
    ROLE_DECISION,
    ROLE_RESEARCH,
    ROLE_RISK_CRITIC,
    ROLE_MATH_CODEGEN,
)
ROLE_NAMES = CHAT_ROLES + (ROLE_FORECASTER,)

# Roles that must name a temperature. The code carries no sampling defaults, so
# an unset temperature would mean "whatever the server picks" for the roles that
# decide and review trades. Such a role fails closed at resolve time.
TEMPERATURE_REQUIRED_ROLES = frozenset({ROLE_DECISION, ROLE_RISK_CRITIC})

# The only legal kind per role class. Looked up, never compared by value.
CHAT_ROLE_KINDS = frozenset({KIND_OPENAI_COMPATIBLE})
FORECASTER_ROLE_KINDS = frozenset({KIND_HF_LOCAL})

# Provider fields that would carry a key value. Any of them is a schema error:
# keys are named by environment variable only.
INLINE_KEY_FIELDS = frozenset(
    {"api_key", "apikey", "key", "secret", "token", "password", "authorization", "bearer"}
)

PROVIDER_ID_PATTERN = r"^[a-z][a-z0-9_-]{0,31}$"
API_KEY_ENV_PATTERN = r"^[A-Z][A-Z0-9_]*$"
MAX_MODEL_ID_CHARS = 256


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["openai_compatible", "hf_local"]
    base_url: Optional[str] = Field(default=None, max_length=512)
    api_key_env: Optional[str] = Field(default=None, max_length=128)
    timeout_s: float = Field(default=60.0, gt=0, le=600)


class RoleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(pattern=PROVIDER_ID_PATTERN)
    model: str = Field(min_length=1, max_length=MAX_MODEL_ID_CHARS)
    revision: Optional[str] = Field(default=None, min_length=1, max_length=128)
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    max_tokens: Optional[int] = Field(default=None, gt=0, le=1_000_000)
    top_p: Optional[float] = Field(default=None, gt=0, le=1)
    # Capability fields. Code reads these, never the model string.
    image_prefix: Optional[str] = Field(default=None, min_length=1, max_length=64)
    compact_prompt: bool = False


class RegistryFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int
    providers: Dict[str, ProviderConfig]
    roles: Dict[str, RoleConfig]
