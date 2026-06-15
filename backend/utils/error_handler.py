import logging
from typing import Optional

class GrowinError(Exception):
    """Base exception for all Growin app errors."""
    pass

class APIError(GrowinError):
    """Exception raised for errors in the external API calls."""
    pass

class DatabaseError(GrowinError):
    """Exception raised for errors in database operations."""
    pass

class LLMError(GrowinError):
    """Exception raised for errors related to LLM operations."""
    pass

class AgentExecutionError(GrowinError):
    """Exception raised for errors during agent execution."""
    pass

def handle_error(e: Exception, context: str, logger: logging.Logger, raise_error: bool = False, custom_error: Optional[Exception] = None) -> None:
    """
    Centralized error handling function.

    Args:
        e: The original exception.
        context: A string describing the context where the error occurred.
        logger: The logger instance to use for recording the error.
        raise_error: Whether to re-raise the exception (or custom_error) after logging.
        custom_error: An optional custom exception to raise instead of the original one.
    """
    logger.error(f"{context}: {e}", exc_info=True)

    if raise_error:
        if custom_error:
            raise custom_error from e
        raise e
