from typing import Dict, List, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBearer

from miriam_agent.api.dependencies import get_current_user, get_memory_store, get_financial_intelligence
from miriam_agent.agents.financial import FinancialAgent, AgentConfig
from miriam_agent.database.models import User, FinancialProfile
from miriam_agent.integrations.grpc_client import GrpcPaymentClient
from miriam_agent.safety.audit import AuditSystem
from miriam_agent.safety.policy import SafetyPolicy
from miriam_agent.safety.validator import InputValidator

router = APIRouter()
security = HTTPBearer()

@router.post("/chat")
async def chat_with_agent(
    request: Dict[str, Any],
    user: User = Depends(get_current_user),
    memory_store = Depends(get_memory_store),
    financial_intelligence = Depends(get_financial_intelligence),
):
    """Chat with the financial agent."""
    try:
        message = request.get("message", "")
        conversation_id = request.get("conversation_id")

        if not message:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Message cannot be empty",
            )

        # Get user's financial profile
        financial_profile = await memory_store.get_financial_profile(user.id)
        if not financial_profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Financial profile not found. Please set up your financial profile.",
            )

        # Initialize safety policy and validator
        safety_policy = SafetyPolicy()
        validator = InputValidator()

        # Validate input
        is_valid, validation_errors = await validator.validate_user_input(
            {"message": message}, "chat"
        )
        if not is_valid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Validation errors: {', '.join(validation_errors)}",
            )

        # Initialize gRPC payment client
        grpc_endpoint = memory_store.get_config().get("grpc_endpoint", "localhost:50051")
        payment_client = GrpcPaymentClient(grpc_endpoint)

        # Create agent configuration
        agent_config = AgentConfig(
            name="financial_agent",
            model="gpt-4",
            temperature=0.7,
            system_prompt="You are Miriam, a professional financial advisor with a conversational, Ramit Sethi-style approach. You help users understand their financial situation, create plans, and make smart decisions. You always put safety first and require explicit approval for money actions.",
        )

        # Initialize financial agent
        financial_agent = FinancialAgent(
            config=agent_config,
            safety_policy=safety_policy,
            memory_store=memory_store,
            financial_profile=financial_profile,
            financial_intelligence=financial_intelligence,
            payment_client=payment_client,
        )

        # Process message
        result = await financial_agent.process_message(message, conversation_id)

        # Log the interaction
        await memory_store.store_interaction(
            user_id=user.id,
            role="user",
            content=message,
            conversation_id=result["conversation_id"],
            metadata={"timestamp": result["metadata"]["processed_at"]},
        )

        # Get conversation history for context
        conversation_history = await memory_store.get_conversation_history(
            result["conversation_id"]
        )

        # Return response
        return {
            "response": result["response"],
            "conversation_id": result["conversation_id"],
            "conversation_history": conversation_history,
            "metadata": result["metadata"],
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error processing chat request: {str(e)}",
        )

@router.get("/conversations")
async def get_user_conversations(
    user: User = Depends(get_current_user),
    memory_store = Depends(get_memory_store),
):
    """Get user's conversations."""
    try:
        conversations = await memory_store.get_conversations(user.id)

        return {
            "conversations": [
                {
                    "id": conv.id,
                    "title": conv.title,
                    "created_at": conv.created_at.isoformat(),
                    "updated_at": conv.updated_at.isoformat(),
                }
                for conv in conversations
            ]
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error getting conversations: {str(e)}",
        )

@router.post("/conversations")
async def create_conversation(
    request: Dict[str, Any],
    user: User = Depends(get_current_user),
    memory_store = Depends(get_memory_store),
):
    """Create a new conversation."""
    try:
        title = request.get("title", "New Conversation")

        conversation = await memory_store.create_conversation(user.id, title)

        return {
            "conversation": {
                "id": conversation.id,
                "title": conversation.title,
                "created_at": conversation.created_at.isoformat(),
                "updated_at": conversation.updated_at.isoformat(),
            }
        }

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating conversation: {str(e)}",
        )

@router.get("/conversations/{conversation_id}")
async def get_conversation_messages(
    conversation_id: str,
    user: User = Depends(get_current_user),
    memory_store = Depends(get_memory_store),
):
    """Get messages for a specific conversation."""
    try:
        # Check if conversation belongs to user
        conversations = await memory_store.get_conversations(user.id)
        conversation_owners = [conv.id for conv in conversations]

        if conversation_id not in conversation_owners:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found",
            )

        messages = await memory_store.get_conversation_messages(conversation_id)

        return {
            "messages": [
                {
                    "id": msg.id,
                    "role": msg.role,
                    "content": msg.content,
                    "metadata": msg.metadata,
                    "created_at": msg.created_at.isoformat(),
                }
                for msg in messages
            ]
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error getting conversation messages: {str(e)}",
        )

@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "miriam-agent-chat"}
