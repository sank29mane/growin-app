"""
Coordinator Agent - Orchestrates all specialist agents
Static SOTA model that routes queries and aggregates results
"""

from .base_agent import BaseAgent, AgentResponse
from market_context import MarketContext
from . import QuantAgent, PortfolioAgent, ForecastingAgent, ResearchAgent, SocialAgent, WhaleAgent, GoalPlannerAgent, VisionAgent
from .decision_agent import DecisionAgent
from utils.ticker_utils import TickerResolver
from data_fabricator import DataFabricator
from typing import Dict, Any, List, Optional, Union
import asyncio
import logging
from utils.async_utils import run_with_timeout
import re
import json
import difflib
from typing import Dict, Any, List, Optional, Union, Tuple
import os
from backend.utils.async_utils import run_with_timeout

logger = logging.getLogger(__name__)

COORDINATOR_SYSTEM_PROMPT = """
You are the Coordinator Agent. Your job is to classify user queries and route them to specialist agents.
Analyze the user's request and return a JSON object with the intent and required agents.

Specialist Agents:
- QuantAgent: Technical analysis, RSI, MACD, Support/Resistance ("quant")
- PortfolioAgent: Portfolio holdings, performance, risk ("portfolio")
- ForecastingAgent: Price predictions, TTM-R2 ("forecast")
- ResearchAgent: News analysis, sentiment ("research")
- SocialAgent: Twitter/Reddit sentiment ("social")
- WhaleAgent: Institutional trade activity ("whale")
- GoalPlannerAgent: Financial goal implementation ("goal")
- VisionAgent: Technical chart pattern recognition ("vision")

JSON Output Format:
{
  "intent": "analytical" | "actionable",
  "ticker": "AAPL" | null,
  "required_agents": ["quant", "forecast"],
  "reasoning": "Explain why these agents are needed"
}
"""

class CoordinatorAgent(BaseAgent):
    """
    CoordinatorAgent: The brain of the swarm.
    Routes queries to specialist agents and aggregates their data.
    """
    
    def __init__(self, config=None, llm=None, mcp_client=None, **kwargs):
        # Support cases where mcp_client is passed as the first positional arg instead of config
        if config is not None and not hasattr(config, 'name') and hasattr(config, 'sessions'):
            mcp_client = config
            config = None

        from .base_agent import AgentConfig
        config = config or AgentConfig(
            name="CoordinatorAgent",
            description="Swarm Orchestrator",
            enabled=True,
            timeout=10.0,
            cache_ttl=300
        )
        super().__init__(config)
        self.llm = llm
        self.mcp_client = mcp_client
        self.specialists: Dict[str, BaseAgent] = {}
        
    def register_specialists(self, agents: Dict[str, BaseAgent]):
        """Register specialist agents for routing"""
        self.specialists = agents

    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        1. Classifies the query using LLM
        2. Routes to required specialists
        3. Aggregates results into MarketContext
        """
        query = context.get("query", "")
        if not query:
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                error="No query provided in context"
            )

        # 1. Classification & Routing
        routing_decision = await self._get_routing_decision(query)
        intent = routing_decision.get("intent", "analytical")
        ticker = routing_decision.get("ticker")
        required_agents = routing_decision.get("required_agents", [])
        
        # SOTA 2026: Automatic ticker extraction if LLM missed it but it was in context
        if not ticker and context.get("ticker"):
            ticker = context.get("ticker")
            
        # Update context with routing info
        context["intent"] = intent
        if ticker:
            context["ticker"] = ticker

        # Initialize MarketContext
        market_context = MarketContext(
            query=query,
            intent=intent,
            ticker=ticker,
            user_context=context.get("user_context", {}),
            reasoning=routing_decision.get("reasoning")
        )

        # 2. Parallel Execution of Specialists
        tasks = []
        c_id = context.get("correlation_id", "swarm-" + os.urandom(4).hex())
        
        for agent_id in required_agents:
            agent = self.specialists.get(agent_id)
            if agent:
                agent_context = context.copy()
                agent_context["correlation_id"] = c_id
                tasks.append(self._execute_specialist(agent, agent_context))
            else:
                logger.warning(f"Coordinator: Requested agent '{agent_id}' not found")

        if not tasks:
            # Default to Research if no agents identified (Safety fallback)
            research_agent = self.specialists.get("research")
            if research_agent:
                tasks.append(self._execute_specialist(research_agent, context))

        results = await asyncio.gather(*tasks)

        # 3. Aggregation
        for result in results:
            if result:
                await self._merge_result_into_context(market_context, result)
                market_context.add_agent_result(
                    result.agent_name, 
                    result.success, 
                    result.latency_ms,
                    result.telemetry
                )

        return AgentResponse(
            agent_name=self.config.name,
            success=market_context.is_complete(),
            data=market_context.model_dump(),
            metadata={
                "routing": routing_decision,
                "correlation_id": c_id
            }
        )

    async def _get_routing_decision(self, query: str) -> Dict[str, Any]:
        """Ask LLM to route the query"""
        if not self.llm:
            # Fallback for no LLM (Test mode)
            return {"intent": "analytical", "required_agents": ["research"], "ticker": None}

        try:
            from langchain_core.messages import HumanMessage
            response = await self.llm.ainvoke([HumanMessage(content=query)])
            content = response.content if hasattr(response, 'content') else str(response)
            
            # Extract JSON
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                return json.loads(match.group())
        except Exception as e:
            logger.error(f"Coordinator routing failed: {e}")
            
        return {"intent": "analytical", "required_agents": ["research"], "ticker": None}

    async def _execute_specialist(self, agent: BaseAgent, context: Dict[str, Any]) -> Optional[AgentResponse]:
        """Execute a single specialist with status tracking and self-correction"""
        from .status_manager import status_manager
        from .messenger import AgentMessage, get_messenger
        
        agent_key = f"{agent.config.name}-{context.get('ticker', 'global')}"
        status_manager.set_status(agent_key, "working", f"Executing {agent.config.name}...")
        
        messenger = get_messenger()
        c_id = context.get("correlation_id")
        
        # Emit telemetry: Started
        start_msg = AgentMessage(
            sender="CoordinatorAgent",
            recipient=agent.config.name,
            subject="agent_started",
            payload={"agent": agent.config.name, "ticker": context.get("ticker")},
            correlation_id=c_id
        )
        await messenger.send_message(start_msg)
        
        try:
            # 15s timeout per specialist to prevent hanging
            result = await run_with_timeout(agent.execute(context), 15.0)

            # COORDINATOR SELF-CORRECTION: Try to fix if it's a known data issue
            if not result.success and result.error:
                error_msg = (result.error or "").lower()

                # Trigger resolution if ticker not found or delisted (Tier 2 Escalation)
                if any(x in error_msg for x in ["not found", "ticker", "delisted", "no data", "404"]):
                    logger.info(f"Coordinator: Escalating Ticker Resolution to Tier 2 (Search) for {agent.config.name}: {result.error}")
                    status_manager.set_status(agent_key, "working", "Escalating Ticker Resolution (Tier 2)...")

                    if "ticker" in context:
                        ticker = context["ticker"]
                        status_manager.set_status(agent_key, "working", f"Searching for correct ticker for '{ticker}'...")

                        new_ticker = await self._resolve_ticker_via_search(ticker)

                        if new_ticker and new_ticker != ticker:
                            logger.info(f"Coordinator Tier 2: Success! Resolved {ticker} -> {new_ticker}")
                            status_manager.set_status(agent_key, "working", f"Retrying with resolved ticker: {new_ticker}")

                            new_context = context.copy()
                            new_context["ticker"] = new_ticker
                            # Retry with new ticker
                            retry_result = await agent.execute(new_context)
                            if retry_result.success:
                                status_manager.set_status(agent_key, "ready", "Resolved via Tier 2")
                                result = retry_result
                            else:
                                result = retry_result # Update original result for Tier 3 fallthrough

                if not result.success:
                    # Tier 3: LLM Self-Correction fallback (Reasoning)
                    logger.info(f"Coordinator: Escalating Ticker Resolution to Tier 3 (LLM) for {agent.config.name}")
                    status_manager.set_status(agent_key, "working", "Attempting LLM self-healing (Tier 3)...")

                    fixed_result = await self._handle_specialist_error(agent, context, result.error)
                    if fixed_result:
                        logger.info(f"Coordinator Tier 3: Successfully healed error for {agent.config.name}")
                        status_manager.set_status(agent_key, "ready", "Fixed via Coordinator Self-Healing")
                        result = fixed_result

            status_manager.set_status(agent_key, "ready", "Task complete")

            # Emit telemetry: Success/Complete
            complete_msg = AgentMessage(
                sender=agent.config.name,
                recipient="CoordinatorAgent",
                subject="agent_complete",
                payload={
                    "agent": agent.config.name,
                    "success": result.success,
                    "latency_ms": result.latency_ms,
                    "ticker": context.get("ticker"),
                    "error": result.error if not result.success else None
                },
                correlation_id=c_id
            )
            await messenger.send_message(complete_msg)
            return result
        except asyncio.TimeoutError:
            error_msg = "Timout after 15s"
            logger.warning(f"Specialist {agent.config.name} timed out")
            status_manager.set_status(agent_key, "error", "Timed out")
            return AgentResponse(
                agent_name=agent.config.name,
                success=False,
                error=error_msg
            )
        except Exception as e:
            logger.error(f"Coordinator: Error executing specialist {agent.config.name}: {e}")
            status_manager.set_status(agent_key, "error", str(e))
            return AgentResponse(
                agent_name=agent.config.name,
                success=False,
                error=str(e)
            )

    async def _resolve_ticker_via_search(self, term: str) -> Optional[str]:
        """
        Tier 2: Search-Augmented Discovery.
        Uses Trading212 search tool and string similarity (Levenshtein/Difflib)
        to verify the best match for ambiguous symbols or names.
        """
        try:
            logger.info(f"Coordinator Tier 2: Searching for correct ticker matching '{term}'")
            # SOTA 2026: Try MCP search if available, fallback to Resolver
            search_result = []
            if self.mcp_client:
                try:
                    search_result = await run_with_timeout(
                        self.mcp_client.call_tool("search_instruments", {"query": term}),
                        10.0
                    )
                    
                    if hasattr(search_result, 'content'):
                        content = search_result.content
                        if isinstance(content, list) and len(content) > 0:
                            text = content[0].text if hasattr(content[0], 'text') else str(content[0])
                            search_result = json.loads(text)
                    elif isinstance(search_result, str):
                        search_result = json.loads(search_result)
                except Exception as e:
                    logger.warning(f"MCP search failed, falling back to local resolver: {e}")
                    resolver = TickerResolver()
                    search_result = await resolver.search(term)
            else:
                resolver = TickerResolver()
                search_result = await resolver.search(term)

            # Check if search_result is wrapped in TextContent or JSON string
            if hasattr(search_result, 'content'):
                content = search_result.content
                if isinstance(content, list) and len(content) > 0:
                    text = content[0].text if hasattr(content[0], 'text') else str(content[0])
                    search_result = json.loads(text)
            elif isinstance(search_result, str):
                search_result = json.loads(search_result)

            if search_result and isinstance(search_result, list) and len(search_result) > 0:
                # 1. Normalize candidates and prepare for comparison
                candidates = []
                for res in search_result:
                    ticker = res.get("ticker", "")
                    name = res.get("name", "")
                    if ticker:
                        candidates.append({
                            "ticker": ticker,
                            "name": name,
                            "search_score": difflib.SequenceMatcher(None, term.upper(), ticker.upper()).ratio(),
                            "name_score": difflib.SequenceMatcher(None, term.lower(), name.lower()).ratio()
                        })
                
                # 2. Find best match based on similarity
                # Priority: Exact Ticker Match > Ticker Similarity > Name Similarity
                best_match = None
                highest_score = 0.0

                for cand in candidates:
                    # Multi-factor score
                    score = max(cand["search_score"], cand["name_score"])

                    # Exact ticker match boost
                    if cand["ticker"].upper() == term.upper():
                        score = 1.0

                    if score > highest_score:
                        highest_score = score
                        best_match = cand
                
                # 3. Commit match if it meets threshold (e.g., > 0.6 similarity)
                if best_match and highest_score > 0.6:
                    found_ticker = best_match["ticker"]

                    # Normalize the found ticker via Resolver
                    normalized = TickerResolver().normalize(found_ticker)

                    logger.info(f"Coordinator Tier 2: Found best match '{best_match['name']}' ({found_ticker}) score={highest_score:.2f} -> {normalized}")
                    return normalized
                else:
                    logger.warning(f"Coordinator Tier 2: No match exceeded similarity threshold (max={highest_score:.2f})")
                    
        except asyncio.TimeoutError:
            logger.warning(f"Ticker search discovery timed out for {term}")
        except Exception as e:
            logger.warning(f"Ticker search discovery failed for {term}: {e}")

        return None

    async def _handle_specialist_error(self, agent: BaseAgent, context: Dict[str, Any], error: str) -> Optional[AgentResponse]:
        """Tier 3: Attempt to fix a specialist error using LLM reasoning (Self-Healing via Docker Sandbox)"""
        if not self.llm:
            return None

        prompt = f"""
        Agent {agent.config.name} failed with error: "{error}" while processing {context}.
        The error suggests a ticker or data mapping issue.
        Can we fix this by modifying the input context?
        If it's a Ticker Not Found/Delisted error, try adding/removing .L or correcting the symbol.
        Leveraged stocks often have digits (MAG5, GLD3) - ensure they are preserved.

        Write a Python script that prints the JSON of the modified 'new_context'.
        The script will run in an ISOLATED Docker container.
        
        Input Context:
        {json.dumps(context)}
        
        Return ONLY a JSON block with the script:
        {{
          "reasoning": "Explain the fix",
          "code": "import json\\ncontext = {json.dumps(context)}\\n# ... fix logic ...\\nprint(json.dumps(context))"
        }}
        """
        try:
            from langchain_core.messages import HumanMessage
            response = await self.llm.ainvoke([HumanMessage(content=prompt)])
            content = response.content if hasattr(response, 'content') else str(response)
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                try:
                    fix_data = json.loads(match.group(), strict=False)
                    python_code = fix_data.get("code", "")
                except json.JSONDecodeError as je:
                    logger.warning(f"Coordinator: JSON decode failed for fix: {je}")
                    return None

                if python_code:
                    logger.info(f"Coordinator: Delegating fix to Docker Sandbox...")
                    # SOTA 2026: Use Docker MCP for isolation
                    try:
                        # Call docker_run_python tool
                        # We assume the tool is registered in the MCP client available to the coordinator
                        result = await run_with_timeout(
                            self.mcp_client.call_tool("docker_run_python", {"script": python_code}),
                            30.0
                        )
                        
                        # Parse the output from the tool
                        if result and hasattr(result, 'content') and result.content:
                            output_text = result.content[0].text
                            # The tool returns a string rep of a dict: {'stdout': '...', ...}
                            # We need to parse that string safely
                            import ast
                            exec_res = ast.literal_eval(output_text)

                            if isinstance(exec_res, dict) and exec_res.get("exit_code") == 0:
                                stdout = exec_res.get("stdout", "")
                                new_context = json.loads(stdout)
                                logger.info(f"Coordinator retry for {agent.config.name} with fixed context: {new_context}")
                                return await agent.execute(new_context)
                            elif isinstance(exec_res, dict):
                                # If it's a dict but we don't have exit code 0 or maybe it's just the context dict directly (like in the original code before merge)
                                # Let's support both formats
                                for k, v in exec_res.items():
                                    context[k] = v
                                logger.info(f"Coordinator: Auto-fix successful. Updated keys: {list(exec_res.keys())}")
                                return await agent.execute(context)
                            else:
                                logger.warning(f"Docker execution failed: {exec_res}")
                    except asyncio.TimeoutError:
                        logger.error(f"Docker execution fix timed out")
                    except Exception as e:
                        logger.error(f"Failed to execute Docker fix: {e}")
                        
        except Exception as e:
            logger.warning(f"Self-correction failed: {e}")

        return None

    async def _merge_result_into_context(self, context: MarketContext, result: AgentResponse):
        """Merge specialist result into market context"""
        from market_context import ForecastData, QuantData, PortfolioData, ResearchData, SocialData, WhaleData

        data = result.data
        agent_name = result.agent_name

        if agent_name == "ForecastingAgent":
            context.forecast = ForecastData(**data)
        elif agent_name == "QuantAgent":
            context.quant = QuantData(**data)
        elif agent_name == "PortfolioAgent":
            context.portfolio = PortfolioData(**data)
        elif agent_name == "ResearchAgent":
            context.research = ResearchData(**data)
        elif agent_name == "SocialAgent":
            context.social = SocialData(**data)
        elif agent_name == "WhaleAgent":
            context.whale = WhaleData(**data)
        elif agent_name == "VisionAgent":
            from market_context import VisionData
            context.vision = VisionData(**data)
        elif agent_name == "GoalPlannerAgent":
            # Note: GoalData must be imported if not already in context, but dynamic import ok
            from market_context import GoalData
            context.goal = GoalData(**data)

    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        # Coordinator Agent acts as a router, `execute` is the main entry point.
        # This satisfies the BaseAgent abstract method requirement.
        return AgentResponse(
            agent_name=self.config.name,
            success=True,
            data={},
            latency_ms=0
        )
