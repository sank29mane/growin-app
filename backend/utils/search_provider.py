import os
import logging
from typing import Optional
from plugins.search import SearchPlugin, TavilySearchPlugin

logger = logging.getLogger(__name__)

def get_search_plugin() -> Optional[SearchPlugin]:
    """Factory function to get the configured search plugin."""
    tavily_key = os.getenv("TAVILY_API_KEY")

    if tavily_key:
        return TavilySearchPlugin(api_key=tavily_key)

    logger.warning("No search provider API key found (e.g., TAVILY_API_KEY). Search functionality will be disabled.")
    return None
