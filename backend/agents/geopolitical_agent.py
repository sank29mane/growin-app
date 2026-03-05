
import os
import asyncio
import logging
from typing import Dict, Any, List, Optional
from decimal import Decimal

from .base_agent import BaseAgent, AgentConfig, AgentResponse
from market_context import GeopoliticalData, GeopoliticalEvent
from utils.news_client import NewsDataIOClient
from utils.financial_math import create_decimal

logger = logging.getLogger(__name__)

class GeopoliticalAgent(BaseAgent):
    """
    Specialized agent for monitoring global geopolitical risks (GPR).
    Synthesizes macro-economic threats into a normalized GPR score.
    """

    def __init__(self, config: AgentConfig = None):
        if config is None:
            config = AgentConfig(
                name="GeopoliticalAgent",
                enabled=True,
                timeout=30.0,
                cache_ttl=3600  # Geopolitics changes slower than price (1 hour cache)
            )
        super().__init__(config)
        self.news_client = NewsDataIOClient()
        self.model_name = os.getenv("GEOPOLITICAL_MODEL", "granite-tiny")

    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Fetch geopolitical news and synthesize a risk score.
        """
        ticker = context.get("ticker", "GLOBAL")
        
        try:
            # 1. Fetch Geopolitical News
            # Queries: "Geopolitical Risk", "Global Conflict", "Trade War", "Sanctions"
            tasks = [
                self.news_client.fetch_latest_news("geopolitical risk", country="us,gb", category="politics"),
                self.news_client.fetch_latest_news("global conflict", country="us,gb", category="politics"),
                self._fetch_tavily_geopolitics()
            ]
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            articles = []
            for res in results:
                if isinstance(res, list):
                    articles.extend(res)
                elif isinstance(res, Exception):
                    logger.warning(f"Geopolitical news fetch failed: {res}")

            if not articles:
                return self._neutral_response()

            # 2. Synthesize Score via LLM
            geopolitical_data = await self._synthesize_risk(articles)
            
            # 3. Store in RAG (Step 3)
            await self._store_in_rag(geopolitical_data)

            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=geopolitical_data.model_dump(),
                latency_ms=0
            )

        except Exception as e:
            logger.error(f"Geopolitical analysis failed: {e}")
            return self._neutral_response(error=str(e))

    async def _fetch_tavily_geopolitics(self) -> List[Dict]:
        """Fetch broad geopolitical context from Tavily."""
        try:
            api_key = os.getenv("TAVILY_API_KEY")
            if not api_key: return []
            
            from tavily import TavilyClient
            tavily = TavilyClient(api_key=api_key)
            query = "current top 5 global geopolitical risks and their market impact March 2026"
            
            def fetch():
                return tavily.search(query=query, search_depth="advanced", max_results=5)
            
            response = await asyncio.to_thread(fetch)
            return [
                {
                    'title': r.get('title'),
                    'description': r.get('content') or r.get('snippet'),
                    'source': {'name': 'Tavily Geopolitics'},
                    'url': r.get('url')
                }
                for r in response.get('results', [])
            ]
        except Exception as e:
            logger.warning(f"Tavily geopolitics fetch failed: {e}")
            return []

    async def _synthesize_risk(self, articles: List[Dict]) -> GeopoliticalData:
        """Use LLM to score geopolitical risk based on news headlines."""
        try:
            from .llm_factory import LLMFactory
            llm = await LLMFactory.create_llm(self.model_name)
            
            # Prepare headlines for LLM
            headlines = "\n".join([f"- {a['title']}: {a.get('description', '')[:200]}" for a in articles[:10]])
            
            prompt = f"""
            Analyze the following geopolitical news headlines and synthesize a Geopolitical Risk (GPR) score.
            
            Headlines:
            {headlines}
            
            Return a JSON object with:
            - gpr_score: 0.0 (Peace/Stable) to 1.0 (Global Crisis/War). Baseline is 0.5.
            - global_sentiment_label: BULLISH, BEARISH, VOLATILE, CRISIS.
            - top_events: Array of objects with {{title, impact (HIGH/MEDIUM/LOW), region, description}}.
            - summary: A 2-sentence macro summary.
            
            JSON ONLY.
            """
            
            content = ""
            if hasattr(llm, "ainvoke"):
                from langchain_core.messages import HumanMessage
                resp = await llm.ainvoke([HumanMessage(content=prompt)])
                content = resp.content
            elif hasattr(llm, "chat"):
                resp = await llm.chat(input_text=prompt, temperature=0)
                content = resp.get("content", "")

            import json
            # Extract JSON from potential markdown
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            
            data = json.loads(content)
            
            events = []
            for e in data.get("top_events", []):
                events.append(GeopoliticalEvent(
                    title=e.get("title", "Unknown Event"),
                    impact=e.get("impact", "MEDIUM"),
                    region=e.get("region", "Global"),
                    description=e.get("description"),
                    url=None
                ))
            
            return GeopoliticalData(
                gpr_score=create_decimal(data.get("gpr_score", 0.5)),
                global_sentiment_label=data.get("global_sentiment_label", "NEUTRAL"),
                top_events=events,
                summary=data.get("summary", "No summary available.")
            )

        except Exception as e:
            logger.warning(f"LLM synthesis for GPR failed: {e}")
            return GeopoliticalData(gpr_score=Decimal('0.5'), global_sentiment_label="NEUTRAL", summary="Synthesis failed.")

    async def _store_in_rag(self, data: GeopoliticalData):
        """Store geopolitical events in RAG for timeline retrieval."""
        try:
            from app_context import state
            if state.rag_manager:
                for event in data.top_events:
                    metadata = {
                        "type": "geopolitical_event",
                        "impact": event.impact,
                        "region": event.region,
                        "gpr_score": float(data.gpr_score)
                    }
                    state.rag_manager.add_document(
                        content=f"[{event.region}] {event.title}: {event.description}",
                        metadata=metadata
                    )
        except Exception as e:
            logger.warning(f"Failed to store geopolitical event in RAG: {e}")

    def _neutral_response(self, error: str = None) -> AgentResponse:
        data = GeopoliticalData(
            gpr_score=Decimal('0.5'),
            global_sentiment_label="NEUTRAL",
            summary=f"Neutral baseline (Error: {error})" if error else "Neutral baseline."
        )
        return AgentResponse(
            agent_name=self.config.name,
            success=True,
            data=data.model_dump(),
            error=error,
            latency_ms=0
        )
