"""
Chat Routes - Conversation and Message Handling
"""

from fastapi import APIRouter, HTTPException, Depends
from typing import Optional
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

        # Save assistant reply
        chat_manager.save_message(
            conversation_id=conversation_id,
            role="assistant:DecisionAgent",
            content=response,
            tool_calls=[],
            agent_name="DecisionAgent",
            model_name=request.model_name,
        )

        # Iteratively update conversation title based on growing context
        await update_conversation_title_if_needed(conversation_id, request.model_name)

        timestamp = chat_manager.conn.execute(
            "SELECT timestamp FROM messages WHERE conversation_id = ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()[0]

        return {
            "conversation_id": conversation_id,
            "agent": "DecisionAgent",
            "response": response,
            "tool_calls": [],
            "timestamp": timestamp,
            "model_name": request.model_name,
            "coordinator_model": request.coordinator_model,
            "data": sanitize_nan(market_context.model_dump())
        }
    except Exception as e:
        logger.error(f"Chat error: {e}\n{traceback.format_exc()}")
        # Sentinel: Sanitized error message to prevent leaking internal details
        raise HTTPException(
            status_code=500, 
            detail="Internal Server Error"
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
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


async def generate_conversation_title(conversation_id: str, model_name: Optional[str] = None):
    """
    Generate a concise AI-powered title for a conversation.
    Strips reasoning/think blocks from LLM output.
    """
    import re
    
    try:
        chat_manager = state.chat_manager
        history = chat_manager.get_conversation_history(conversation_id)
        if not history:
            return {"title": "New Conversation"}

        # Build concise summary (limit to last 4 messages for speed)
        messages_text = ""
        for msg in history[-4:]:
            role = "User" if msg['role'] == "user" else "Assistant"
            content = msg['content'][:200]  # Truncate long messages
            messages_text += f"{role}: {content}\n"

        prompt = (
            "Create a 3-6 word title for this conversation. "
            "Include ticker symbols if mentioned. "
            "Output ONLY the title, nothing else.\n\n"
            f"{messages_text}\n\nTitle:"
        )

        from agents.decision_agent import DecisionAgent
        agent = DecisionAgent(model_name=model_name or "native-mlx")
        response = await agent.generate_response(prompt)

        # AGGRESSIVE CLEANING: Strip ALL common reasoning/think blocks
        title = str(response)
        
        # 1. Strip <think>...</think> (including incomplete tags)
        title = re.sub(r'<think>.*?</think>', '', title, flags=re.DOTALL | re.IGNORECASE)
        title = re.sub(r'<think>.*', '', title, flags=re.DOTALL | re.IGNORECASE)
        title = re.sub(r'.*?</think>', '', title, flags=re.DOTALL | re.IGNORECASE)
        
        # 2. Strip common reasoning markers
        title = re.sub(r'\*\*thinking\*\*.*?\*\*', '', title, flags=re.DOTALL | re.IGNORECASE)
        title = re.sub(r'thinking:.*?\n', '', title, flags=re.IGNORECASE)
        
        # 3. Strip meta-instrution bleed (e.g. "We must output only...")
        title = re.sub(r'we (must|need|should).*?(?=\n|$)', '', title, flags=re.IGNORECASE)
        title = re.sub(r'I (will|am going to).*?(?=\n|$)', '', title, flags=re.IGNORECASE)
        title = re.sub(r'Let me.*?(?=\n|$)', '', title, flags=re.IGNORECASE)
        title = re.sub(r'The title should.*?(?=\n|$)', '', title, flags=re.IGNORECASE)
        
        # 4. Strip quotes and surrounding artifacts before splitting
        title = title.strip().strip('"').strip("'").strip('`')
        
        # 5. Extract first "real" line that isn't empty or meta
        lines = [l.strip() for l in title.split('\n') if l.strip()]
        
        # Filter out lines that look like meta-instructions
        clean_lines = []
        for l in lines:
            l_lower = l.lower()
            if any(x in l_lower for x in ["output only", "3-6 word", "title:", "user:", "assistant:"]):
                continue
            if len(l) > 1:
                clean_lines.append(l)
        
        title = clean_lines[0] if clean_lines else "Financial Conversation"
        
        # Final formatting
        title = title.strip().strip('"').strip("'").strip('*').strip()
        
        # Limit length
        if len(title) > 40:
            title = title[:37] + "..."
        if len(title) < 2:
            title = "Financial Conversation"

        chat_manager.update_conversation_title(conversation_id, title)
        return {"title": title}
    except Exception as e:
        logger.error(f"Title generation error: {e}")
        return {"title": "Financial Conversation"}

@router.post("/conversations/{conversation_id}/generate-title")
async def generate_conversation_title_endpoint(conversation_id: str, model_name: Optional[str] = None):
    """Endpoint wrapper for title generation"""
    return await generate_conversation_title(conversation_id, model_name)
