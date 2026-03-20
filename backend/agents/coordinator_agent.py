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
from status_manager import status_manager
from app_logging import correlation_id_ctx
from utils.audit_log import log_audit
from app_context import state
import asyncio
import logging
import re
import time
import json
import difflib
from typing import Dict, List, Any, Optional, Tuple
import os

logger = logging.getLogger(__name__)

COORDINATOR_SYSTEM_PROMPT = """You are the Growin Coordinator Agent.
Your job is to classify user intent and route queries to the correct specialist agents.

Specialist Agents:
- QuantAgent: Real-time indicators, price action, RSI, and technical signals.
- ForecastingAgent: Predictive price targets (24h/7d) using Neural JMCE.
- ResearchAgent: Web-scale news and sentiment analysis (via Tavily/NewsData).
- SocialAgent: X/Twitter sentiment and retail activity trends.
- WhaleAgent: Large trade detection and unusual volume monitoring.
- PortfolioAgent: Direct access to Trading 212 holdings and cash balances.
- GoalPlannerAgent: Strategic asset allocation and feasibility modeling.
- VisionAgent: Technical analysis of chart screenshots/images ("vision")

Available Intents:
- price_check (needs quant)
- market_analysis (needs quant, forecast, research, social, whale)
- portfolio_query (needs portfolio)
- goal_planning (needs goal_planner)
- intraday_trade (needs quant, forecast, research; implies 5min bars)
- swing_trade (needs quant, forecast, research; implies 1hour bars)
- conversational (general financial questions, definitions, greetings, or abstract strategy)

Output format:
INTENT: [intent_name]
TICKER: [symbol or NONE]
REASON: [short explanation]
"""

class CoordinatorAgent(BaseAgent):
    """
    Coordinator Agent - The 'Dispatcher' of the agent swarm.
    Static SOTA model that routes queries and aggregates results.
    """
    
    def __init__(self, config: Optional[Any] = None, model_name: str = "native-mlx", api_keys: Optional[Dict[str, str]] = None):
        super().__init__(config or BaseAgent.default_config("CoordinatorAgent"))
        self.model_name = model_name
        self.api_keys = api_keys or {}
        self.data_fabricator = DataFabricator()
        self.mcp_client = state.mcp_client
        
        # Specialist registry
        self.quant_agent = QuantAgent()
        self.portfolio_agent = PortfolioAgent()
        self.forecasting_agent = ForecastingAgent()
        self.research_agent = ResearchAgent()
        self.social_agent = SocialAgent()
        self.whale_agent = WhaleAgent()
        self.goal_planner_agent = GoalPlannerAgent()
        self.vision_agent = VisionAgent()
        
        self._llm = None
        self._initialized = False

    async def _initialize_llm(self):
        if self._initialized: return
        from .llm_factory import LLMFactory
        # We use a fast model for coordination
        self._llm = await LLMFactory.create_llm("granite-tiny")
        self._initialized = True

    async def _classify_intent(self, query: str) -> Dict[str, Any]:
        """Classify user intent using fast model"""
        await self._initialize_llm()
        
        from langchain_core.messages import HumanMessage
        response = await self._llm.ainvoke([HumanMessage(content=COORDINATOR_SYSTEM_PROMPT + f"\nQuery: {query}")])
        content = response.content if hasattr(response, 'content') else str(response)
        
        # Regex extraction
        intent_match = re.search(r'INTENT:\s*(\w+)', content, re.IGNORECASE)
        ticker_match = re.search(r'TICKER:\s*([A-Z0-9.]+)', content, re.IGNORECASE)
        reason_match = re.search(r'REASON:\s*(.+)', content, re.IGNORECASE)
        
        intent_type = intent_match.group(1).lower() if (intent_match and intent_match.group(1)) else "market_analysis"
        ticker = ticker_match.group(1).upper() if ticker_match and ticker_match.group(1) and "NONE" not in ticker_match.group(1).upper() else None
        
        # Hard overrides
        q_lower = query.lower()
        if "portfolio" in q_lower:
            intent_type = "portfolio_query"
            if ticker in ["ISA", "INVEST", "MY"]: ticker = None
            
        if any(w in q_lower for w in ["what is", "how does", "tell me about", "hello", "hi", "how are you"]):
            intent_type = "conversational"
            
        if ticker in ["RSI", "MACD", "DEEP", "DIVE", "MORE"]:
            ticker = None

        needs_map = {
            "price_check": ["quant"],
            "market_analysis": ["quant", "forecast", "research", "social", "whale", "portfolio"],
            "portfolio_query": ["portfolio", "quant", "forecast"],
            "goal_planning": ["goal_planner", "portfolio"],
            "intraday_trade": ["quant", "forecast", "research", "whale"],
            "swing_trade": ["quant", "forecast", "research", "whale"],
            "conversational": []
        }
        
        return {
            "type": intent_type,
            "primary_ticker": ticker,
            "needs": needs_map.get(intent_type, ["quant", "forecast"]),
            "reason": reason_match.group(1) if reason_match else "Direct Routing"
        }

    async def process_query(self, query: str, history: List[Dict] = [], ticker: Optional[str] = None, account_type: Optional[str] = None) -> MarketContext:
        """
        Main entry point for swarm execution.
        """
        status_manager.set_status("coordinator", "working", "Analyzing request...")
        
        # 1. Intent & Context Extraction
        intent = await self._classify_intent(query)
        
        # Inherit ticker if not provided and in history
        resolver = TickerResolver()
        if not ticker:
            ticker = intent.get("primary_ticker")
            if not ticker and history:
                # SOTA 2026: Only inherit context from User Messages to avoid hallucinating tickers from AI headers (e.g. ACE)
                for msg in reversed(history):
                    if msg.get("role") != "user":
                        continue
                    content = msg.get("content", "")
                    if not content:
                        continue
                    extracted = resolver.extract(content)
                    if extracted:
                        ticker = extracted[0]
                        break
        
        # --- NEW INTRADAY RESOLVER ---
        # SOTA Phase 30: If it's an intraday intent, we prioritize specific ticker resolution
        if intent["type"] == "intraday_trade" and not ticker:
             # Look specifically for most recent tickers discussed
             pass 

        # Final Ticker Normalization via Resolver
        if ticker:
            original_ticker = ticker
            try:
                from trading212_mcp_server import normalize_ticker
                ticker = normalize_ticker(ticker)
            except Exception:
                ticker = resolver.normalize(ticker)

            if ticker != original_ticker:
                 logger.info(f"Ticker normalized (Resolver): {original_ticker} -> {ticker}")

        # 2. CENTRALIZED DATA FABRICATION
        status_manager.set_status("coordinator", "working", "Fabricating Market Context...")
        
        context = await self.data_fabricator.fabricate_context(
            intent=intent.get("type", "analytical"),
            ticker=ticker,
            account_type=account_type,
            user_settings={}
        )
        
        # Inject Intent metadata
        context.routing_reason = intent.get("reason", "")
        if account_type:
            context.user_context["account_type"] = account_type

        # --- SKILL INJECTION ---
        # Coordinator also benefits from skills (e.g. knowing 'tax' queries need different handling)
        from utils.skill_loader import get_skill_loader
        skills_text = get_skill_loader().get_relevant_skills(query)
        if skills_text:
             context.user_context["expert_skills"] = skills_text
        # -----------------------
        
        # COORDINATOR FIX: Ticker normalization
        if context.ticker:
            original_ticker = context.ticker

            # Centralized SOTA ticker normalization
            normalized = TickerResolver().normalize(context.ticker)
            if normalized and normalized != original_ticker:
                logger.info(f"Coordinator: Normalized ticker '{original_ticker}' -> '{normalized}'")
                context.ticker = normalized

        # 3. PARALLEL AGENT_EXECUTION (Pure Processors)
        needs = intent.get("needs", [])
        specialist_tasks = []
        
        # Broadcast intent to the agent bus (SOTA Decentralized Path)
        from .messenger import AgentMessage
        from .governance import get_governance
        from app_logging import correlation_id_ctx
        
        c_id = correlation_id_ctx.get()
        broadcast_msg = AgentMessage(
            sender="CoordinatorAgent",
            recipient="broadcast",
            subject="intent_classified",
            payload=intent,
            correlation_id=c_id
        )
        # In a real swarm, this would be handled by the message broker
        
        # -----------------------
        # SPECIALIST EXECUTION
        # -----------------------
        if "quant" in needs and context.ticker:
            ohlcv = []
            if context.price and context.price.history_series:
                ohlcv = [{
                    't': b.timestamp, 'o': b.open, 'h': b.high, 'l': b.low, 'c': b.close, 'v': b.volume
                } for b in context.price.history_series]
            specialist_tasks.append(self._run_specialist(self.quant_agent, {"ticker": context.ticker, "ohlcv_data": ohlcv, "intent": intent["type"]}))
            
        if "forecast" in needs and context.ticker:
            ohlcv = []
            if context.price and context.price.history_series:
                ohlcv = [{
                    't': b.timestamp, 'o': b.open, 'h': b.high, 'l': b.low, 'c': b.close, 'v': b.volume
                } for b in context.price.history_series]
            # Forecasting defaults to 5 days daily for swing, or special logic for intraday
            specialist_tasks.append(self._run_specialist(self.forecasting_agent, {"ticker": context.ticker, "ohlcv_data": ohlcv, "days": 5}))
            
        if "research" in needs:
            specialist_tasks.append(self._run_specialist(self.research_agent, {"ticker": context.ticker}))
            
        if "portfolio" in needs:
            specialist_tasks.append(self._run_specialist(self.portfolio_agent, {"account_type": account_type}))
            
        if "social" in needs:
            specialist_tasks.append(self._run_specialist(self.social_agent, {"ticker": context.ticker}))
            
        if "whale" in needs:
            specialist_tasks.append(self._run_specialist(self.whale_agent, {"ticker": context.ticker}))
            
        if "goal_planner" in needs:
            specialist_tasks.append(self._run_specialist(self.goal_planner_agent, {}))

        # VisionAgent: Trigger if image exists in context or query suggests visual analysis
        if "vision" in needs or "image" in context.user_context:
             specialist_tasks.append(self._run_specialist(self.vision_agent, {"ticker": context.ticker, "image": context.user_context.get("image")}))

        if specialist_tasks:
            status_manager.set_status("coordinator", "working", f"Executing swarm ({len(specialist_tasks)} agents)...")
            results = await asyncio.gather(*specialist_tasks, return_exceptions=True)
            
            for res in results:
                if isinstance(res, AgentResponse) and res.success:
                    self._merge_result(context, res)
                elif isinstance(res, Exception):
                    logger.error(f"Swarm task exception: {res}")

        # 4. DECISION SYNTHESIS (Consensus)
        status_manager.set_status("coordinator", "working", "DecisionAgent Reasoning...")
        
        # SOTA 2026: Reasoning Trace export happens within make_decision
        decision_result = await DecisionAgent(model_name=self.model_name).make_decision(context, query)
        
        context.user_context["final_answer"] = decision_result
        
        status_manager.set_status("coordinator", "ready", "Swarm analysis complete")
        
        # Log Audit
        log_audit(
            action="COORDINATOR_PROCESS",
            actor="CoordinatorAgent",
            details={
                "query": query,
                "ticker": ticker,
                "intent": intent["type"],
                "agents_executed": list(context.agents_executed),
                "agents_failed": list(context.agents_failed)
            }
        )
        
        return context

    async def _run_specialist(self, agent: BaseAgent, context: Dict[str, Any]) -> AgentResponse:
        """Run a specialist agent with error handling and timeout"""
        from status_manager import status_manager
        from .messenger import AgentMessage, get_messenger
        from app_logging import correlation_id_ctx
        
        agent_key = (agent.config.name or "").lower().replace("agent", "_agent")
        c_id = correlation_id_ctx.get()
        messenger = get_messenger()
        
        status_manager.set_status(agent_key, "working", "Executing task...")
        
        # Emit telemetry: Start
        start_msg = AgentMessage(
            sender="CoordinatorAgent",
            recipient=agent.config.name,
            subject="agent_started",
            payload={"agent": agent.config.name, "ticker": context.get("ticker")},
            correlation_id=c_id
        )
        await messenger.send_message(start_msg)
        
        result = None
        start_time = time.time()
        try:
            # 15s timeout per specialist to prevent hanging
            if hasattr(asyncio, 'timeout'):
                async with asyncio.timeout(15.0):
                    result = await agent.execute(context)
            else:
                result = await asyncio.wait_for(agent.execute(context), timeout=15.0)
                
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
                            
                            context = context.copy()
                            context["ticker"] = new_ticker
                            # Retry with new ticker
                            try:
                                retry_result = await agent.execute(context)
                            except Exception as e:
                                retry_result = AgentResponse(
                                    agent_name=agent.config.name,
                                    success=False,
                                    data={},
                                    error=str(e),
                                    latency_ms=0
                                )

                            if retry_result.success:
                                status_manager.set_status(agent_key, "ready", "Resolved via Tier 2")
                                result = retry_result
                            else:
                                result = retry_result # Update original result for Tier 3 fallthrough

                    if not result.success:
                        # Tier 3: LLM Self-Correction fallback (Reasoning)
                        logger.info(f"Coordinator: Escalating Ticker Resolution to Tier 3 (LLM) for {agent.config.name}")
                        status_manager.set_status(agent_key, "working", "Attempting LLM self-healing (Tier 3)...")

                        # Propagate updated context (with corrected ticker from T2) to T3
                        fixed_result = await self._handle_specialist_error(agent, context, result.error)
                        if fixed_result:
                            logger.info(f"Coordinator Tier 3: Successfully healed error for {agent.config.name}")
                            status_manager.set_status(agent_key, "ready", "Fixed via Coordinator Self-Healing")
                            result = fixed_result

        except asyncio.TimeoutError:
            error_msg = "Timout after 15s"
            logger.warning(f"Specialist {agent.config.name} timed out")
            result = AgentResponse(
                agent_name=agent.config.name,
                success=False,
                data={},
                error=error_msg,
                latency_ms=15000
            )
        except Exception as e:
            logger.error(f"Specialist {agent.config.name} failed: {e}")
            result = AgentResponse(
                agent_name=agent.config.name,
                success=False,
                data={},
                error=str(e),
                latency_ms=0
            )
        
        # --- FINAL TELEMETRY AND STATUS (Ensures it runs even on timeout/exception) ---
        status_label = "ready" if result.success else "error"
        status_desc = "Task complete" if result.success else f"Failed: {result.error}"
        status_manager.set_status(agent_key, status_label, status_desc)
        
        # Emit telemetry: Success/Complete
        complete_msg = AgentMessage(
            sender=agent.config.name,
            recipient="CoordinatorAgent",
            subject="agent_complete",
            payload={
                "agent": agent.config.name,
                "success": result.success,
                "latency_ms": result.latency_ms or int((time.time() - start_time) * 1000),
                "ticker": context.get("ticker"),
                "error": result.error if not result.success else None
            },
            correlation_id=c_id
        )
        await messenger.send_message(complete_msg)
        
        return result

    def _merge_result(self, context: MarketContext, result: AgentResponse):
        """Aggregate specialist results into unified context"""
        from market_context import ForecastData, QuantData, PortfolioData, ResearchData, SocialData, WhaleData, GoalData, VisionData
        data = result.data
        name = result.agent_name
        
        if name == "QuantAgent": context.quant = QuantData(**data)
        elif name == "ForecastingAgent": context.forecast = ForecastData(**data)
        elif name == "PortfolioAgent": context.portfolio = PortfolioData(**data)
        elif name == "ResearchAgent": context.research = ResearchData(**data)
        elif name == "SocialAgent": context.social = SocialData(**data)
        elif name == "WhaleAgent": context.whale = WhaleData(**data)
        elif name == "GoalPlannerAgent": context.goal = GoalData(**data)
        elif name == "VisionAgent": context.vision = VisionData(**data)
        
        context.agents_executed.add(name)


    async def _resolve_ticker_via_search(self, term: str) -> Optional[str]:
        """Tier 2: Use TickerResolver search to find correct ticker for a name/term"""
        try:
            # SOTA 2026: Try MCP search if available, fallback to Resolver
            search_result = []
            if self.mcp_client:
                try:
                    if hasattr(asyncio, 'timeout'):
                        async with asyncio.timeout(10.0):
                            search_result = await self.mcp_client.call_tool("search_instruments", {"query": term})
                    else:
                        search_result = await asyncio.wait_for(
                            self.mcp_client.call_tool("search_instruments", {"query": term}),
                            timeout=10.0
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
                best_match = max(candidates, key=lambda x: max(x["search_score"], x["name_score"]))
                highest_score = max(best_match["search_score"], best_match["name_score"])
                
                if highest_score > 0.6: # Confidence threshold
                    found_ticker = best_match["ticker"]
                    normalized = TickerResolver().normalize(found_ticker)
                    logger.info(f"Coordinator Tier 2: Found best match '{best_match['name']}' ({found_ticker}) score={highest_score:.2f} -> {normalized}")
                    return normalized
                    
        except asyncio.TimeoutError:
            logger.warning(f"Ticker search discovery timed out for {term}")
        except Exception as e:
            logger.warning(f"Coordinator Tier 2 search failed for '{term}': {e}")
            
        return None

    async def _handle_specialist_error(self, agent: BaseAgent, context: Dict[str, Any], error: str) -> Optional[AgentResponse]:
        """Tier 3: Attempt to resolve errors using reasoning + Docker Sandbox execution"""
        await self._initialize_llm()
        from langchain_core.messages import HumanMessage
        
        prompt = f"""Specialist {agent.config.name} failed with error: "{error}"
        Input Context: {json.dumps(context)}
        
        Suggest a fix. If it's a ticker issue, provide the correct ticker.
        Return ONLY a JSON block with the fix:
        {{
          "reasoning": "Explain the fix",
          "fixed_ticker": "SYMBOL",
          "code": "import json\\n# ... fix logic if needed ...\\nprint(json.dumps(new_context))"
        }}
        
        If fixed via ticker only, set "fixed_ticker". If complex context fix needed, provide "code" for Docker Sandbox.
        """
        try:
            response = await self._llm.ainvoke([HumanMessage(content=prompt)])
            content = response.content if hasattr(response, 'content') else str(response)
            
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if not match:
                return None
                
            fix_data = json.loads(match.group(), strict=False)
            
            # Simple Ticker Fix
            if fix_data.get("fixed_ticker"):
                new_ticker = fix_data["fixed_ticker"].upper()
                logger.info(f"Coordinator Tier 3: Retrying with agent-suggested ticker: {new_ticker}")
                context_copy = context.copy()
                context_copy["ticker"] = new_ticker
                return await agent.execute(context_copy)
            
            # Complex Docker Sandbox Fix
            python_code = fix_data.get("code")
            if python_code and self.mcp_client:
                logger.info(f"Coordinator Tier 3: Delegating context fix to Docker Sandbox...")
                try:
                    if hasattr(asyncio, 'timeout'):
                        async with asyncio.timeout(30.0):
                            exec_result = await self.mcp_client.call_tool("docker_run_python", {"script": python_code})
                    else:
                        exec_result = await asyncio.wait_for(
                            self.mcp_client.call_tool("docker_run_python", {"script": python_code}),
                            timeout=30.0
                        )
                    
                    if exec_result and hasattr(exec_result, 'content'):
                        output_text = exec_result.content[0].text
                        import ast
                        exec_res_dict = ast.literal_eval(output_text)
                        
                        if exec_res_dict.get("exit_code") == 0:
                            stdout = exec_res_dict.get("stdout", "")
                            new_context = json.loads(stdout)
                            logger.info(f"Coordinator Tier 3: Retry with fixed context from sandbox: {new_context}")
                            return await agent.execute(new_context)
                except Exception as e:
                    logger.error(f"Docker Sandbox fix failed: {e}")
                        
        except Exception as e:
            logger.warning(f"Tier 3 self-correction failed: {e}")
            
        return None
