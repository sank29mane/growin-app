"""
LLM Factory - builds the chat model for a role.

Phase 67: a role resolves to one provider and one model from the model role
registry (``private/models.json``). Nothing here inspects a model or provider
name, probes a server, or falls back to another model. A role that is not
configured raises ``ModelRoleMissing``; a failing provider raises
``ProviderError``.
"""

import logging

from model_registry.provider import RoleChatModel, chat_model_for_role

logger = logging.getLogger(__name__)


class LLMFactory:
    """Factory for role-bound chat models."""

    @staticmethod
    def for_role(role: str) -> RoleChatModel:
        """Build the chat model for ``role`` from the active registry."""
        llm = chat_model_for_role(role)
        logger.info("LLM Factory: role %s bound to model %s", role, llm.model_id)
        return llm

    @staticmethod
    async def create_llm(role: str) -> RoleChatModel:
        """Async form of ``for_role`` for callers that already await a factory."""
        return LLMFactory.for_role(role)
