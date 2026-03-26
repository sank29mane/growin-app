"""
Orchestrator Agent - Unified Routing, Coordination, and Decision Making.
SOTA 2026: Flattened architecture for reduced latency and improved coherence.
"""

import asyncio
import logging
import json
import re
import uuid
import time
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from .base_agent import BaseAgent, AgentResponse
from .coordinator_agent import COORDINATOR_SYSTEM_PROMPT
from .decision_agent import DecisionAgent
from .messenger import AgentMessage, get_messenger
from market_context import MarketContext
from data_fabricator import DataFabricator
from status_manager import status_manager
from app_logging import correlation_id_ctx
from utils.audit_log import log_audit
from .llm_factory import LLMFactory
from price_validation import PriceValidator

# Import specialist agents (as used in CoordinatorAgent)
from . import QuantAgent, PortfolioAgent, ForecastingAgent, ResearchAgent, SocialAgent, WhaleAgent, GoalPlannerAgent

logger = logging.getLogger(__name__)

class OrchestratorAgent:
    """
    Unified Orchestrator - Combines routing and reasoning into a single lifecycle.
    
    Architecture Evolution:
    - Reduced inter-agent hops (from 2+ to 1)
    - Consolidated state management
    - Unified telemetry stream
    - Support for 8-bit AFFINE local inference optimization
    """
    
    def __init__(self, mcp_client=None, chat_manager=None, model_name: str = "native-mlx", api_keys: Optional[Dict[str, str]] = None):
        from app_context import state
        self.model_name = model_name
        self.mcp_client = mcp_client or state.mcp_client
        self.chat_manager = chat_manager or state.chat_manager
        self.api_keys = api_keys or {}
        
        # Core Components
        self.data_fabricator = DataFabricator()
        self.messenger = get_messenger()
        
        # Specialists
        self.quant_agent = QuantAgent()
        self.portfolio_agent = PortfolioAgent()
        self.forecasting_agent = ForecastingAgent()
        self.research_agent = ResearchAgent()
        self.social_agent = SocialAgent()
        self.whale_agent = WhaleAgent()
        self.goal_planner_agent = GoalPlannerAgent()

        # Risk Agent (Critic)
        from .risk_agent import RiskAgent
        self.risk_agent = RiskAgent(model_name=model_name)

        # Decision logic container (reusing DecisionAgent's internal logic)
        self.decision_engine = DecisionAgent(model_name=model_name, api_keys=api_keys, mcp_client=self.mcp_client)
        
        self.routing_llm = None
        self._initialized = False
        
        logger.info(f"OrchestratorAgent initialized with model: {model_name}")

    async def _initialize(self):
        """Initialize LLMs and providers"""
        if self._initialized:
            return
            
        # Initialize routing LLM (lightweight)
        # Using granite-tiny as default for routing to minimize latency
        self.routing_llm = await LLMFactory.create_llm("granite-tiny")
        
        # Initialize decision engine (reasoning model)
        await self.decision_engine._initialize_llm()
        
        self._initialized = True

    async def _classify_intent(self, query: str) -> Dict[str, Any]:
        """Classify user intent using routing LLM (reused from Coordinator)"""
        if not self.routing_llm:
            await self._initialize()
            
        clean_query = query.strip()[:500]
        prompt = f"""You are the Orchestrator.
Route queries to specialist agents.
Format your response EXACTLY as:
INTENT: [intent_name]
TICKER: [symbol or NONE]
REASON: [short explanation]

Intents:
- price_check (needs quant)
- market_analysis (needs quant, forecast, research, social)
- portfolio_query (needs portfolio)
- goal_planning (needs goal_planner)
- intraday_trade (needs quant, forecast, research; implies 5min bars)
- swing_trade (needs quant, forecast, research; implies 1hour bars)
- conversational (general financial questions, definitions, greetings, or abstract strategy)

Query: "{clean_query}"
"""

        try:
            # Simple invocation (reusing Coordinator's logic)
            from langchain_core.messages import HumanMessage
            response = await self.routing_llm.ainvoke([HumanMessage(content=prompt)])
            content = response.content if hasattr(response, 'content') else str(response)

            intent_match = re.search(r'INTENT:\s*(\w+)', content, re.IGNORECASE)
            ticker_match = re.search(r'TICKER:\s*([A-Z0-9.]+)', content, re.IGNORECASE)
            
            intent_type = intent_match.group(1).lower() if (intent_match and intent_match.group(1)) else "market_analysis"
            ticker = ticker_match.group(1).upper() if ticker_match and ticker_match.group(1) and "NONE" not in ticker_match.group(1).upper() else None
            
            # Hard overrides to protect against LLM misclassification
            q_lower = query.lower()
            if "portfolio" in q_lower:
                intent_type = "portfolio_query"
            
            # Conversational/Educational triggers
            if any(w in q_lower for w in ["what is", "how does", "tell me about", "hello", "hi", "how are you", "who are you"]):
                intent_type = "conversational"
                
            if ticker in ["ISA", "INVEST", "MY", "DEEP", "DIVE", "MORE", "SOME", "RSI", "MACD"]:
                ticker = None
            
            # If it's a "how is it doing" question without a ticker, we might be talking about a portfolio
            if "how is it doing" in q_lower or "performance" in q_lower:
                if not ticker:
                    intent_type = "portfolio_query"
                    
            needs_map = {
                "price_check": ["quant"],
                "market_analysis": ["quant", "forecast", "research", "social", "whale", "portfolio"],
                "portfolio_query": ["portfolio", "quant", "forecast"],
                "goal_planning": ["goal_planner", "portfolio"],
                "intraday_trade": ["quant", "forecast", "research", "whale"],
                "swing_trade": ["quant", "forecast", "research", "whale"],
                "conversational": [],
                "educational": []
            }
            
            needs = needs_map.get(intent_type, ["quant", "forecast"])
            
            return {
                "type": intent_type,
                "needs": needs,
                "primary_ticker": ticker,
                "reason": "Unified Routing"
            }
        except Exception as e:
            logger.error(f"Orchestrator routing failed: {e}")
            return {"type": "market_analysis", "needs": ["quant", "portfolio"], "primary_ticker": None, "reason": "Routing Fallback"}

    async def run(self, query: str, conversation_id: Optional[str] = None, history: List[Dict] = [], ticker: Optional[str] = None, account_type: Optional[str] = None) -> Dict[str, Any]:
        """
        Execute full Orchestrator lifecycle: Routing -> Collection -> Reasoning.
        """
        await self._initialize()
        
        # 1. Setup Correlation & Telemetry
        c_id = correlation_id_ctx.get() or str(uuid.uuid4())
        correlation_id_ctx.set(c_id)
        
        status_manager.set_status("orchestrator", "working", f"Processing: '{query[:30]}...'", model=self.model_name)
        
        await self.messenger.send_message(AgentMessage(
            sender="OrchestratorAgent",
            recipient="broadcast",
            subject="agent_started",
            payload={"agent": "OrchestratorAgent", "query": query},
            correlation_id=c_id
        ))

        # 2. Routing Phase
        status_manager.set_status("orchestrator", "working", "Classifying Intent...")
        intent_info = await self._classify_intent(query)
        # Context bridging for "Deep Dive" or follow-ups without explicit tickers
        if not ticker and history:
            from utils import extract_ticker_from_text
            for i, msg in enumerate(reversed(history)):
                if i >= 5: # Short-circuit to optimize performance
                    break
                # SOTA 2026: Only inherit context from User Messages to avoid hallucinating tickers from AI headers (e.g. ACE)
                if msg.get("role") != "user":
                    continue
                    
                content = msg.get("content", "")
                found = extract_ticker_from_text(content)
                if found and found not in ["ISA", "INVEST", "MY", "DEEP", "DIVE", "MORE", "SOME", "RSI"]:
                    ticker = found
                    break
                elif "portfolio" in content.lower():
                    intent_info["type"] = "portfolio_query"
                    break
                    
        await self.messenger.send_message(AgentMessage(
            sender="OrchestratorAgent",
            recipient="broadcast",
            subject="intent_classified",
            payload=intent_info,
            correlation_id=c_id
        ))
        
        # 3. Data Fabrication (Market Context)
        status_manager.set_status("orchestrator", "working", "Fabricating Context...")
        
        # SOTA 2026: Historical Alpha Context
        from analytics_db import get_analytics_db
        db = get_analytics_db()
        historical_alpha = db.get_agent_alpha_metrics(ticker)
        
        from agents.decision_agent import DecisionAgent
        detected_account = account_type
        if not detected_account or detected_account == "all":
            detected_account = DecisionAgent()._detect_account_mentions(query)
            
        context = await self.data_fabricator.fabricate_context(
            intent=intent_info["type"],
            ticker=ticker,
            account_type=detected_account,
            user_settings={}
        )
        context.routing_reason = intent_info["reason"]
        context.user_context["history"] = history
        context.user_context["historical_alpha"] = historical_alpha

        await self.messenger.send_message(AgentMessage(
            sender="OrchestratorAgent",
            recipient="broadcast",
            subject="context_fabricated",
            payload={"ticker": ticker, "intent": intent_info["type"]},
            correlation_id=c_id
        ))

        # 4. Parallel Specialist Execution (The Swarm)
        needs = intent_info["needs"]
        tasks = []
        
        # Dynamic Weighting: Adjust specialist priority based on alpha
        # (Agents with higher historical alpha are executed first or given more resource)
        # This is a logical prioritization; execution remains parallel
        
        await self.messenger.send_message(AgentMessage(
            sender="OrchestratorAgent",
            recipient="broadcast",
            subject="swarm_started",
            payload={"agents": needs, "alpha_context": historical_alpha.get("specialists", {})},
            correlation_id=c_id
        ))
        
        # Preparation for parallel tasks
        if "quant" in needs and context.ticker:
            ohlcv = []
            if context.price and context.price.history_series:
                ohlcv = [{
                    't': b.timestamp, 'o': b.open, 'h': b.high, 'l': b.low, 'c': b.close, 'v': b.volume
                } for b in context.price.history_series]
            tasks.append(self._run_specialist(self.quant_agent, {"ticker": context.ticker, "ohlcv_data": ohlcv, "intent": intent_info["type"]}))
            
        if "forecast" in needs and context.ticker:
            ohlcv = []
            if context.price and context.price.history_series:
                ohlcv = [{
                    't': b.timestamp, 'o': b.open, 'h': b.high, 'l': b.low, 'c': b.close, 'v': b.volume
                } for b in context.price.history_series]
            tasks.append(self._run_specialist(self.forecasting_agent, {"ticker": context.ticker, "ohlcv_data": ohlcv, "days": 5}))
            
        if "research" in needs:
            tasks.append(self._run_specialist(self.research_agent, {"ticker": context.ticker}))
            
        if "portfolio" in needs:
            tasks.append(self._run_specialist(self.portfolio_agent, {"account_type": detected_account}))

        if tasks:
            status_manager.set_status("orchestrator", "working", f"Executing Swarm ({len(tasks)} agents)...")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for res in results:
                if isinstance(res, AgentResponse) and res.success:
                    await self._merge_result(context, res)

        # 5. Reasoning Phase (Decision)
        status_manager.set_status("orchestrator", "working", "Synthesizing Recommendation...")
        
        await self.messenger.send_message(AgentMessage(
            sender="OrchestratorAgent",
            recipient="broadcast",
            subject="reasoning_started",
            payload={"model": self.model_name},
            correlation_id=c_id
        ))
        
        # SOTA 2026: Trajectory Stitching
        from utils.trajectory_stitcher import TrajectoryStitcher
        stitched_narrative = TrajectoryStitcher.stitch(context)
        
        # SOTA 2026: Tax-Loss Harvesting Intelligence
        from utils.tlh_scanner import TLHScanner
        tlh_candidates = []
        if context.portfolio:
            tlh_candidates = TLHScanner().scan(context.portfolio.model_dump())
            context.user_context["tlh_opportunities"] = tlh_candidates

        # Initial Thesis (Weighted by Alpha)
        # We pass the alpha metrics directly into the prompt to bias the LLM synthesis
        alpha_prompt = f"\n[HISTORICAL ALPHA CONTEXT]\n{json.dumps(historical_alpha.get('specialists', {}))}\n"
        tlh_prompt = f"\n[TAX-LOSS HARVESTING OPPORTUNITIES]\n{json.dumps(tlh_candidates)}\n"
        full_query = query + alpha_prompt + tlh_prompt + f"\n[STITCHED NARRATIVE]\n{stitched_narrative}\n"
        
        decision_result = await self.decision_engine.make_decision(context, full_query)
        recommendation = decision_result.get("content", "")
        
        # SOTA 2026 Phase 30: Emit Rebalance Proposal for HITL UI
        if "pending_proposal" in context.user_context:
            proposal = context.user_context["pending_proposal"]
            await self.messenger.send_message(AgentMessage(
                sender="OrchestratorAgent",
                recipient="broadcast",
                subject="rebalance_proposal",
                payload=proposal,
                correlation_id=c_id
            ))
            # Also append manual approval tag to text
            recommendation += f"\n\n[ACTION_REQUIRED:APPROVE_TRADE({proposal.get('proposal_id')})]"

        # --- SOTA 2026: ADVERSARIAL DEBATE LOOP ---
        if context.intent in ["conversational", "educational"]:
            return {"content": recommendation, "response_id": decision_result.get("response_id"), "context": context}

        import os
        debate_trace = []
        max_debate_turns = 0 if os.getenv("USE_SHADOW_LLM") == "1" else 1 # Skip rebuttal in shadow mode definition
        
        # ACE Evaluator
        from .ace_evaluator import ACEEvaluator
        ace_evaluator = ACEEvaluator()
        
        for turn in range(max_debate_turns + 1):
            status_manager.set_status("orchestrator", "working", f"Risk Review (Turn {turn+1})...")
            
            await self.messenger.send_message(AgentMessage(
                sender="OrchestratorAgent",
                recipient="broadcast",
                subject="risk_review_started",
                payload={"model": self.risk_agent.model_name, "turn": turn},
                correlation_id=c_id
            ))
            
            risk_review = await self.risk_agent.review(context, recommendation)
            debate_trace.append({"turn": turn, "status": risk_review.get("status"), "refutation": risk_review.get("debate_refutation")})
            
            if risk_review.get("status") == "APPROVED" or turn >= max_debate_turns:
                break
            
            # If FLAGGED/BLOCKED and we have turns left - REBUTTAL
            status_manager.set_status("orchestrator", "working", "Adversarial Rebuttal in progress...")
            rebuttal_prompt = f"""
            The Risk Agent (The Contrarian) challenged your strategy:
            "{risk_review.get('debate_refutation')}"
            
            Provide a refined strategy that addresses this critique or provide evidence why the critique is invalid.
            Stitched Context: {stitched_narrative}
            """
            # Use decision engine to generate rebuttal
            rebuttal_result = await self.decision_engine.generate_response(rebuttal_prompt)
            recommendation = rebuttal_result

        # Calculate final ACE Score using dedicated component
        ace_score = ace_evaluator.calculate_score(debate_trace, risk_review.get("status"))
        robustness_label = ace_evaluator.get_robustness_label(ace_score)

        context.user_context["risk_review"] = risk_review
        context.user_context["debate_trace"] = debate_trace
        context.user_context["ace_score"] = ace_score
        context.user_context["robustness_label"] = robustness_label
        
        # SOTA 2026: Final Output Formatting
        header = f"### Strategic Recommendation (ACE: {ace_score:.2f} - {robustness_label})\n"
        if risk_review.get("status") in ["FLAGGED", "BLOCKED"]:
            warning = f"\n\n⚠️ **ADVERSARIAL WARNING**: {risk_review.get('risk_assessment')}"
            if risk_review.get("requires_hitl"):
                warning += "\n\n[ACTION_REQUIRED:TRADE_APPROVAL]"
            recommendation = header + recommendation + warning
        else:
            recommendation = header + recommendation + "\n\n✅ *Strategy verified through adversarial debate.*"
        
        # 6. Finalization
        status_manager.set_status("orchestrator", "ready", "Response complete")
        
        await self.messenger.send_message(AgentMessage(
            sender="OrchestratorAgent",
            recipient="broadcast",
            subject="agent_complete",
            payload={"agent": "OrchestratorAgent", "success": True},
            correlation_id=c_id
        ))
        
        # SOTA 2026: Async Alpha Audit
        async def delayed_alpha():
            await asyncio.sleep(2) # Brief delay
            db.calculate_agent_alpha(c_id)
        
        asyncio.create_task(delayed_alpha())
        
        return {
            "content": recommendation,
            "response_id": decision_result.get("response_id"),
            "context": context
        }

    async def _run_specialist(self, agent: BaseAgent, input_data: Dict[str, Any], suppress_events: bool = False) -> AgentResponse:
        """Run a specialist with unified telemetry"""
        c_id = correlation_id_ctx.get()
        agent_name = agent.config.name
        
        if not suppress_events:
            await self.messenger.send_message(AgentMessage(
                sender="OrchestratorAgent",
                recipient="broadcast",
                subject="agent_started",
                payload={"agent": agent_name, "ticker": input_data.get("ticker")},
                correlation_id=c_id
            ))
        
        try:
            result = await agent.execute(input_data)
            
            if not suppress_events:
                await self.messenger.send_message(AgentMessage(
                    sender="OrchestratorAgent",
                    recipient="broadcast",
                    subject="agent_complete",
                    payload={"agent": agent_name, "success": result.success, "latency": result.latency_ms},
                    correlation_id=c_id
                ))
            return result
        except Exception as e:
            logger.error(f"Orchestrator specialist failed ({agent_name}): {e}")
            return AgentResponse(agent_name=agent_name, success=False, data={}, error=str(e))

    async def _merge_result(self, context: MarketContext, result: AgentResponse):
        """Merge specialist data into context"""
        from market_context import ForecastData, QuantData, PortfolioData, ResearchData, SocialData, WhaleData
        data = result.data
        name = result.agent_name
        
        if name == "QuantAgent": context.quant = QuantData(**data)
        elif name == "ForecastingAgent": context.forecast = ForecastData(**data)
        elif name == "PortfolioAgent": context.portfolio = PortfolioData(**data)
        elif name == "ResearchAgent": context.research = ResearchData(**data)
        elif name == "SocialAgent": context.social = SocialData(**data)
        elif name == "WhaleAgent": context.whale = WhaleData(**data)

    async def run_stream(self, query: str, conversation_id: Optional[str] = None, history: List[Dict] = [], ticker: Optional[str] = None, account_type: Optional[str] = None):
        """Streaming variant of Orchestrator lifecycle"""
        await self._initialize()
        c_id = correlation_id_ctx.get() or str(uuid.uuid4())
        correlation_id_ctx.set(c_id)
        
        # 1. Routing & Context (Fast)
        intent_info = await self._classify_intent(query)
        if not ticker: 
            ticker = intent_info.get("primary_ticker")
            
        # Context bridging for "Deep Dive" or follow-ups without explicit tickers
        if not ticker and history:
            from utils import extract_ticker_from_text
            for i, msg in enumerate(reversed(history)):
                if i >= 5: # Short-circuit to optimize performance
                    break
                # SOTA 2026: Only inherit context from User Messages to avoid hallucinating tickers from AI headers (e.g. ACE)
                if msg.get("role") != "user":
                    continue
                    
                content = msg.get("content", "")
                found = extract_ticker_from_text(content)
                if found and found not in ["ISA", "INVEST", "MY", "DEEP", "DIVE", "MORE", "SOME", "RSI"]:
                    ticker = found
                    break
                elif "portfolio" in content.lower():
                    intent_info["type"] = "portfolio_query"
                    break
        
        from agents.decision_agent import DecisionAgent
        detected_account = account_type
        if not detected_account or detected_account == "all":
            detected_account = DecisionAgent()._detect_account_mentions(query)
            
        context = await self.data_fabricator.fabricate_context(
            intent=intent_info["type"],
            ticker=ticker,
            account_type=detected_account
        )
        context.user_context["history"] = history
        
        # 2. Parallel Collection (The Swarm)
        needs = intent_info["needs"]
        
        # SOTA: Hardening - Give DataFabricator a moment if the stream is starting very fast
        if ticker:
            await asyncio.sleep(0.5)

        tasks = []

        if "quant" in needs and ticker:
            ohlcv = []
            if context.price and context.price.history_series:
                ohlcv = [{
                    't': b.timestamp, 'o': b.open, 'h': b.high, 'l': b.low, 'c': b.close, 'v': b.volume
                } for b in context.price.history_series]
            # Resilient Trigger: QuantAgent will now try its own resolve if ohlcv is empty
            tasks.append(self._run_specialist(self.quant_agent, {"ticker": ticker, "ohlcv_data": ohlcv, "intent": intent_info["type"]}))

        if "forecast" in needs and ticker:
            ohlcv = []
            if context.price and context.price.history_series:
                ohlcv = [{
                    't': b.timestamp, 'o': b.open, 'h': b.high, 'l': b.low, 'c': b.close, 'v': b.volume
                } for b in context.price.history_series]
            tasks.append(self._run_specialist(self.forecasting_agent, {"ticker": ticker, "ohlcv_data": ohlcv, "days": 5}))

            
        if "research" in needs:
            tasks.append(self._run_specialist(self.research_agent, {"ticker": context.ticker}))
            
        if "portfolio" in needs:
            tasks.append(self._run_specialist(self.portfolio_agent, {"account_type": detected_account}))
        
        if "social" in needs:
            tasks.append(self._run_specialist(self.social_agent, {"ticker": context.ticker}))
            
        if "whale" in needs:
            tasks.append(self._run_specialist(self.whale_agent, {"ticker": context.ticker}))
            
        if "goal_planner" in needs:
            tasks.append(self._run_specialist(self.goal_planner_agent, {}))

        if tasks:
            status_manager.set_status("orchestrator", "working", f"Executing Swarm ({len(tasks)} agents)...")
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, AgentResponse) and res.success:
                    await self._merge_result(context, res)

        # SOTA 2026 Phase 25: Liquidity Calculation (Post-Specialist)
        if ticker and context.price and context.price.history_series:
            ohlcv_raw = [{
                't': b.timestamp, 'o': b.open, 'h': b.high, 'l': b.low, 'c': b.close, 'v': b.volume
            } for b in context.price.history_series]
            adv = self.quant_agent.engine.calculate_adv_30d(ohlcv_raw)
            if context.risk_governance:
                context.risk_governance.adv_30d = adv
            else:
                from market_context import RiskGovernanceData
                context.risk_governance = RiskGovernanceData(adv_30d=adv)
        
        # 3. Stream Reasoning
        full_response = ""
        async for chunk in self.decision_engine.make_decision_stream(context, query):
            full_response += chunk
            yield chunk

        # 4. Governance Phase (Risk Review)
        if context.intent in ["conversational", "educational"]:
             # Yield final context for route handler metadata
             from pydantic import BaseModel
             class FinalEvent(BaseModel):
                 market_context: MarketContext
             
             yield FinalEvent(market_context=context)
             return

        status_manager.set_status("orchestrator", "working", "Performing Risk Review (Critic Pattern)...")
        risk_review = await self.risk_agent.review(context, full_response)
        context.user_context["risk_review"] = risk_review
        
        # SOTA 2026: Append Risk Warning to stream if FLAGGED or BLOCKED
        if risk_review.get("status") in ["FLAGGED", "BLOCKED"]:
            warning = f"\n\n⚠️ **RISK WARNING**: {risk_review.get('risk_assessment')}"
            if risk_review.get("requires_hitl"):
                warning += "\n\n[ACTION_REQUIRED:TRADE_APPROVAL]"
            yield warning

        # Yield final context for route handler metadata
        from pydantic import BaseModel
        class FinalEvent(BaseModel):
            market_context: MarketContext
        
        yield FinalEvent(market_context=context)
