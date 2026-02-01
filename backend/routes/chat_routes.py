"""
Chat Routes - Conversation and Message Handling
"""

from fastapi import APIRouter, HTTPException, Depends
from typing import Optional, Dict, Any
from pydantic import BaseModel
from app_context import state, ChatMessage, AnalyzeRequest, AgentResponse
from rate_limiter import default_limiter
from utils import extract_ticker_from_text, sanitize_nan
import logging
import traceback
from datetime import datetime

# Constants
TITLE_UPDATE_INTERVAL = 6  # Update title every 6 messages

logger = logging.getLogger(__name__)
router = APIRouter()

async def update_conversation_title_if_needed(conversation_id: str, model_name: Optional[str] = None):
    """
    Update conversation title iteratively based on conversation growth.
    Updates title when conversation reaches milestones or grows significantly.
    """
    try:
        chat_manager = state.chat_manager
        history = chat_manager.get_conversation_history(conversation_id)

        if not history or len(history) < 4:
            return  # Not enough context yet

        # Get current title and check if it needs updating
        current_title = chat_manager.get_conversation_title(conversation_id)

        # Update if:
        # 1. No title set yet, or
        # 2. Title is generic/default, or
        # 3. Conversation has grown significantly (every 6 messages)
        should_update = (
            not current_title or
            current_title in ["New Conversation", "Untitled Conversation"] or
            "Chat" in current_title or
            len(history) % TITLE_UPDATE_INTERVAL == 0  # Update every N messages
        )

        if should_update:
            # Generate new title with full context
            title_result = await generate_conversation_title(conversation_id, model_name)
            logger.info(f"Updated conversation {conversation_id} title to: {title_result.get('title')}")

    except Exception as e:
        logger.warning(f"Failed to update conversation title: {e}")
        # Don't fail the main chat flow if title update fails

@router.post("/api/chat/message")
async def chat_message(request: ChatMessage, _=Depends(default_limiter.check)):
    """
    Chat endpoint using NEW Hybrid Architecture:
    Coordinator → Specialists → Decision Agent
    """
    try:
        from agents.coordinator_agent import CoordinatorAgent
        from agents.decision_agent import DecisionAgent
        
        chat_manager = state.chat_manager
        conversation_id = request.conversation_id or chat_manager.create_conversation()

        # Save user message
        chat_manager.save_message(
            conversation_id=conversation_id,
            role="user",
            content=request.message,
        )

        # Extract ticker from message
        ticker = extract_ticker_from_text(request.message)
        
        # Load history for context
        history = chat_manager.load_history(conversation_id, limit=6)
        
        # Phase 1: Coordinator orchestrates specialists
        coordinator = CoordinatorAgent(state.mcp_client, model_name=request.coordinator_model or "granite-tiny")
        market_context = await coordinator.process_query(
            query=request.message,
            ticker=ticker,
            account_type=request.account_type,  # Pass account context
            history=history
        )
        
        market_context.user_context["history"] = history
        
        # Phase 2: Decision Agent makes final recommendation
        decision_agent = DecisionAgent(
            model_name=request.model_name or "native-mlx",
            api_keys=request.api_keys
        )
        response = await decision_agent.make_decision(market_context, request.message)

        # Generate timestamp and ensure ISO format
        timestamp = datetime.utcnow().isoformat() + "Z"

        # Save assistant reply
        chat_manager.save_message(
            conversation_id=conversation_id,
            role="assistant",
            content=response,
            tool_calls=[],
            agent_name="DecisionAgent",
            model_name=request.model_name,
        )

        # Iteratively update conversation title based on growing context
        await update_conversation_title_if_needed(conversation_id, request.model_name)

        return {
            "conversation_id": conversation_id,
            "agent": "DecisionAgent",
            "response": response,
            "tool_calls": None, # Explicitly null to prevent frontend from treating it as a tool/search result
            "timestamp": timestamp,
            "model_name": request.model_name,
            "coordinator_model": request.coordinator_model,
            "data": sanitize_nan(market_context.model_dump() if hasattr(market_context, "model_dump") else market_context.dict())
        }
    except Exception as e:
        logger.error(f"Chat error: {str(e)}\n{traceback.format_exc()}")
        
        # Check for specific error types to provide safe feedback
        if "LM Studio" in str(e) or "Connection" in type(e).__name__:
            error_detail = "LLM Connection Error: Please ensure LM Studio/Ollama is running."
        elif "native-mlx" in str(e).lower():
            error_detail = "MLX Model Error: Check model path and hardware compatibility."
        else:
            # Mask all other internal errors to prevent leakage
            error_detail = "Internal Server Error"
        
        raise HTTPException(
            status_code=500, 
            detail=error_detail
        )

@router.post("/agent/analyze", response_model=AgentResponse)
async def analyze_portfolio(request: AnalyzeRequest):
    """
    Analyze query using Coordinator + Decision Agent architecture.
    
    This endpoint orchestrates specialist agents and provides a final recommendation.
    Used by the iOS app for ad-hoc queries without full conversation context.
    
    Args:
        request: AnalyzeRequest with query string and optional model_name
        
    Returns:
        AgentResponse with final_answer and messages list
        
    Raises:
        HTTPException: If MCP not connected or analysis fails
    """
    if not state.mcp_client.session:
        raise HTTPException(status_code=503, detail="MCP Server not connected")

    try:
        from agents.coordinator_agent import CoordinatorAgent
        from agents.decision_agent import DecisionAgent
        
        # Extract ticker from query (simple approach)
        ticker = None
        if request.query:
            words = request.query.upper().split()
            for word in words:
                if len(word) >= 3 and len(word) <= 5 and word.isalpha():
                    ticker = word
                    break
        
        # 1. Coordinate: Gather data from specialists
        coordinator = CoordinatorAgent(state.mcp_client, model_name=request.coordinator_model or "granite-tiny")
        market_context = await coordinator.process_query(
            query=request.query,
            ticker=ticker,
            account_type=request.account_type  # Pass account context
        )
        
        # 2. Decide: Generate final recommendation
        decision_agent = DecisionAgent(
            model_name=request.model_name or "native-mlx",
            api_keys=request.api_keys
        )
        result = await decision_agent.make_decision(market_context, request.query)
        
        return {"messages": [], "final_answer": result}
    except Exception as e:
        logger.error(f"Analysis failed: {e}")
        # Sentinel: Sanitized error message
        raise HTTPException(status_code=500, detail="Internal Server Error")


@router.get("/conversations")
async def list_conversations():
    """
    List all conversations for the current user.
    
    Returns:
        List of conversation objects with id, title, and timestamps
        
    Raises:
        HTTPException: If database query fails
    """
    try:
        return state.chat_manager.list_conversations()
    except Exception as e:
        logger.error(f"Error listing conversations: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal Server Error")


@router.get("/conversations/{conversation_id}/history")
async def get_conversation_history(conversation_id: str):
    """
    Get message history for a specific conversation.
    
    Args:
        conversation_id: UUID of the conversation
        
    Returns:
        List of messages with role, content, and timestamps
        
    Raises:
        HTTPException: If conversation not found or query fails
    """
    try:
        return state.chat_manager.load_history(conversation_id)
    except Exception as e:
        logger.error(f"Error loading history for {conversation_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal Server Error")


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    """
    Permanently delete a conversation and all its messages.
    
    Args:
        conversation_id: UUID of the conversation to delete
        
    Returns:
        Success status
        
    Raises:
        HTTPException: If deletion fails
    """
    try:
        state.chat_manager.delete_conversation(conversation_id)
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error deleting conversation {conversation_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal Server Error")


@router.delete("/conversations/{conversation_id}/clear")
async def clear_conversation(conversation_id: str):
    """
    Clear all messages from a conversation but keep the conversation.
    
    Args:
        conversation_id: UUID of the conversation to clear
        
    Returns:
        Success status
        
    Raises:
        HTTPException: If clear operation fails
    """
    try:
        state.chat_manager.clear_conversation(conversation_id)
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error clearing conversation {conversation_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal Server Error")


async def generate_conversation_title(conversation_id: str, model_name: Optional[str] = None):
    """
    Generate a concise AI-powered title for a conversation.
    """
    try:
        from utils.text_processing import extract_title_from_text
        
        chat_manager = state.chat_manager
        history = chat_manager.get_conversation_history(conversation_id)
        if not history:
            return {"title": "New Conversation"}

        # Build concise summary (limit to last 4 messages for speed)
        # Focus on user messages as they define the topic best
        messages_text = ""
        for msg in history[-4:]:
            role = "User" if msg['role'] == "user" else "Assistant"
            content = msg['content'][:250]  # Truncate long messages
            messages_text += f"{role}: {content}\n"

        prompt = (
            "Create a very short, specific title (3-5 words) for this conversation based on the user's intent. "
            "Examples: 'Apple Stock Analysis', 'Portfolio Risk Review', 'Tech Sector Trends'. "
            "Do NOT use 'Chat', 'Conversation', 'Regarding', or generic terms. "
            "If a ticker symbol is mentioned (like AAPL, TSLA), INCLUDE it. "
            "Output ONLY the title text. No thinking, no quotes, no preamble.\n\n"
            f"Conversation:\n{messages_text}\n\nTitle:"
        )

        from agents.decision_agent import DecisionAgent
        agent = DecisionAgent(model_name=model_name or "native-mlx")
        response = await agent.generate_response(prompt)

        # Use utility for robust extraction
        title = extract_title_from_text(str(response))

        chat_manager.update_conversation_title(conversation_id, title)
        return {"title": title}
    except Exception as e:
        logger.error(f"Title generation error: {e}")
        return {"title": "Financial Analysis"}

@router.post("/conversations/{conversation_id}/generate-title")
async def generate_conversation_title_endpoint(conversation_id: str, model_name: Optional[str] = None):
    """Endpoint wrapper for title generation"""
    return await generate_conversation_title(conversation_id, model_name)

class IngestRequest(BaseModel):
    content: str
    metadata: Dict[str, Any] = {}

@router.post("/api/knowledge/ingest")
async def ingest_knowledge(request: IngestRequest):
    """
    Manually ingest knowledge into the RAG system.
    Useful for users to save strategy notes, specific news, or goals.
    """
    try:
        from app_context import state
        if not state.rag_manager:
            raise HTTPException(status_code=503, detail="RAG system not initialized")
            
        # Enforce metadata defaults
        meta = request.metadata
        meta["type"] = meta.get("type", "manual_entry")
        meta["timestamp"] = datetime.now().isoformat()
        
        state.rag_manager.add_document(content=request.content, metadata=meta)
        
        return {"status": "success", "message": "Knowledge ingested successfully"}
    except Exception as e:
        logger.error(f"Ingestion failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal Server Error")

@router.get("/api/models/lmstudio")
async def list_lmstudio_models():
    """List available models from LM Studio."""
    try:
        from app_context import state
        from lm_studio_client import LMStudioClient
        
        client = state.lm_studio_client or LMStudioClient()
        
        # Check connection first
        if not await client.check_connection():
            return {"models": [], "error": "LM Studio not running"}
            
        # Get models (filtering out embeddings)
        models = await client.list_models()
        llm_models = [
            m["id"] for m in models 
            if "embed" not in m["id"].lower() 
            and "nomic" not in m["id"].lower()
            and "bert" not in m["id"].lower()
        ]
        
        return {"models": llm_models}
    except Exception as e:
        logger.error(f"Failed to list LM Studio models: {e}", exc_info=True)
        return {"models": [], "error": "Internal Server Error"}
