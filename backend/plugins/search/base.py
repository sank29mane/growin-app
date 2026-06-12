from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

@dataclass
class SearchResult:
    title: str
    content: str
    url: str
    source: str = "SearchPlugin"

class SearchPlugin(ABC):
    @abstractmethod
    async def search(self, query: str, max_results: int = 5, include_domains: Optional[List[str]] = None, search_depth: str = "advanced") -> List[SearchResult]:
        pass
