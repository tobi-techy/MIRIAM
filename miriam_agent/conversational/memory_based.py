import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
from collections import deque

from miriam_agent.database.models import User, FinancialProfile
from miriam_agent.memory import MemoryStore
from miriam_agent.vector import VectorStore

logger = logging.getLogger(__name__)

class ConversationContext:
    """Memory-based conversational context for Miriam Financial Agent."""

    def __init__(
        self,
        memory_store: MemoryStore,
        vector_store: VectorStore,
        user_id: str,
        financial_profile: FinancialProfile,
    ):
        self.memory_store = memory_store
        self.vector_store = vector_store
        self.user_id = user_id
        self.financial_profile = financial_profile

        # Conversation history with memory
        self.short_term_memory = deque(maxlen=50)  # Last 50 messages
        self.long_term_memory = []  # Important patterns and preferences
        self.context_buffer = {}

        # Topic tracking
        self.current_topics = set()
        self.topic_sentiments = {}
        self.topic_contexts = {}

        # State tracking
        self.user_state = {
            "last_intent": None,
            "current_goals": [],
            "active_plans": [],
            "financial_interests": [],
            "risk_tolerance": financial_profile.risk_tolerance,
            "knowledge_level": "intermediate",
        }

    async def update_context(
        self,
        message: str,
        role: str,
        intent: Optional[Dict[str, Any]] = None,
        entities: Optional[Dict[str, Any]] = None,
        sentiment: Optional[str] = None,
    ) -> None:
        """Update conversation context with new message."""
        try:
            # Add to short-term memory
            message_context = {
                "role": role,
                "content": message,
                "timestamp": datetime.utcnow().isoformat(),
                "intent": intent,
                "entities": entities,
                "sentiment": sentiment,
            }

            self.short_term_memory.append(message_context)

            # Update user state based on message
            await self._update_user_state(message, role, intent, entities)

            # Extract and store long-term memories
            await self._extract_long_term_memories(message, role)

            # Update topic tracking
            await self._update_topic_tracking(message, role, intent, sentiment)

            # Store context in memory store
            await self.memory_store.store_interaction(
                user_id=self.user_id,
                role=role,
                content=message,
                metadata={
                    "intent": intent,
                    "entities": entities,
                    "sentiment": sentiment,
                    "context_buffer": self.context_buffer,
                },
            )

        except Exception as e:
            logger.error(
                "Error updating context",
                error=str(e),
                exc_info=True,
            )
            raise

    async def _update_user_state(
        self,
        message: str,
        role: str,
        intent: Optional[Dict[str, Any]],
        entities: Optional[Dict[str, Any]],
    ) -> None:
        """Update user state based on message."""
        if role != "user":
            return

        # Update last intent
        if intent:
            self.user_state["last_intent"] = intent

        # Update goals from entities
        if entities:
            if "goal" in entities:
                goal = entities["goal"]
                if goal not in self.user_state["current_goals"]:
                    self.user_state["current_goals"].append(goal)

            if "plan" in entities:
                plan = entities["plan"]
                if plan not in self.user_state["active_plans"]:
                    self.user_state["active_plans"].append(plan)

            if "financial_interest" in entities:
                interest = entities["financial_interest"]
                if interest not in self.user_state["financial_interests"]:
                    self.user_state["financial_interests"].append(interest)

        # Update knowledge level based on complexity
        if self._is_complex_message(message):
            self.user_state["knowledge_level"] = "advanced"

    def _is_complex_message(self, message: str) -> bool:
        """Check if message is complex."""
        # Simple heuristic: long messages, technical terms, multiple clauses
        if len(message.split()) > 30:
            return True

        technical_terms = [
            "investment",
            "portfolio",
            "asset allocation",
            "diversification",
            "risk management",
            "beta",
            "alpha",
            "correlation",
        ]

        message_lower = message.lower()
        if any(term in message_lower for term in technical_terms):
            return True

        # Multiple clauses (comma separated)
        if message.count(",") > 2:
            return True

        return False

    async def _extract_long_term_memories(
        self, message: str, role: str
    ):
        """Extract long-term memories from conversation."""
        try:
            if role != "user":
                return

            # Extract preferences
            preferences = await self._extract_preferences(message)
            if preferences:
                for key, value in preferences.items():
                    await self._store_preference(key, value)

            # Extract financial goals
            goals = await self._extract_goals(message)
            if goals:
                for goal in goals:
                    await self._store_goal(goal)

            # Extract patterns
            patterns = await self._extract_patterns(message)
            if patterns:
                for pattern in patterns:
                    await self._store_pattern(pattern)

        except Exception as e:
            logger.error(
                "Error extracting long-term memories",
                error=str(e),
                exc_info=True,
            )

    async def _extract_preferences(self, message: str) -> Dict[str, Any]:
        """Extract preferences from message."""
        preferences = {}

        # Simple keyword-based extraction
        message_lower = message.lower()

        if "conservative" in message_lower:
            preferences["risk_tolerance"] = "conservative"

        if "aggressive" in message_lower:
            preferences["risk_tolerance"] = "aggressive"

        if "tax" in message_lower:
            preferences["tax_optimization"] = True

        if "retirement" in message_lower:
            preferences["retirement_focus"] = True

        if "dividend" in message_lower:
            preferences["income_focus"] = True

        return preferences

    async def _extract_goals(self, message: str) -> List[str]:
        """Extract goals from message."""
        goals = []

        # Simple keyword-based extraction
        message_lower = message.lower()

        goal_keywords = [
            ("save for", "savings"),
            ("retire", "retirement"),
            ("buy house", "home_purchase"),
            ("invest in", "investment"),
            ("build wealth", "wealth_building"),
            ("generate income", "income_generation"),
        ]

        for keyword, goal_type in goal_keywords:
            if keyword in message_lower:
                goals.append(goal_type)

        return goals

    async def _extract_patterns(self, message: str) -> List[Dict[str, Any]]:
        """Extract patterns from message."""
        patterns = []

        # Extract amount patterns
        import re

        amount_patterns = [
            r"\$(\d+)",
            r"\€(\d+)",
            r"\£(\d+)",
            r"(\d+) dollars",
            r"(\d+) euros",
            r"(\d+) pounds",
        ]

        for pattern in amount_patterns:
            matches = re.findall(pattern, message)
            for match in matches:
                patterns.append(
                    {
                        "type": "amount",
                        "value": float(match),
                        "context": "financial",
                    }
                )

        # Extract time patterns
        time_patterns = [
            r"next (\d+) (day|week|month|year)",
            r"in (\d+) (day|week|month|year)",
            r"by (\d+) (day|week|month|year)",
        ]

        for pattern in time_patterns:
            matches = re.findall(pattern, message, re.IGNORECASE)
            for match in matches:
                patterns.append(
                    {
                        "type": "time",
                        "value": int(match[0]),
                        "unit": match[1],
                        "context": "timeline",
                    }
                )

        return patterns

    async def _store_preference(self, key: str, value: Any) -> None:
        """Store a preference."""
        await self.memory_store.store_memory(
            user_id=self.user_id,
            memory_type="preference",
            content=f"{key}: {value}",
            metadata={"key": key, "value": value},
        )

    async def _store_goal(self, goal: str) -> None:
        """Store a goal."""
        await self.memory_store.store_memory(
            user_id=self.user_id,
            memory_type="goal",
            content=f"goal: {goal}",
            metadata={"goal": goal},
        )

    async def _store_pattern(self, pattern: Dict[str, Any]) -> None:
        """Store a pattern."""
        await self.memory_store.store_memory(
            user_id=self.user_id,
            memory_type="pattern",
            content=f"pattern: {pattern}",
            metadata={"pattern": pattern},
        )

    async def _update_topic_tracking(
        self,
        message: str,
        role: str,
        intent: Optional[Dict[str, Any]],
        sentiment: Optional[str],
    ) -> None:
        """Update topic tracking."""
        if role != "user":
            return

        # Extract topics from intent or message
        topics = []

        if intent:
            topics.append(intent.get("type", "general"))

        # Add additional topics based on message content
        message_lower = message.lower()

        topic_keywords = {
            "budget": ["budget", "spending", "expense"],
            "investment": ["invest", "stock", "bond", "market"],
            "retirement": ["retirement", "pension", "401k"],
            "tax": ["tax", "deduction", "credit"],
            "insurance": ["insurance", "coverage", "policy"],
            "real_estate": ["real estate", "property", "house"],
            "crypto": ["crypto", "bitcoin", "ethereum", "digital currency"],
        }

        for topic, keywords in topic_keywords.items():
            if any(keyword in message_lower for keyword in keywords):
                topics.append(topic)

        # Update topic tracking
        for topic in topics:
            self.current_topics.add(topic)

            if sentiment:
                if topic not in self.topic_sentiments:
                    self.topic_sentiments[topic] = {}
                self.topic_sentiments[topic][sentiment] = (
                    self.topic_sentiments[topic].get(sentiment, 0) + 1
                )

            if intent:
                if topic not in self.topic_contexts:
                    self.topic_contexts[topic] = []
                self.topic_contexts[topic].append(
                    {
                        "message": message,
                        "intent": intent,
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                )

    async def get_context_summary(self) -> Dict[str, Any]:
        """Get summary of current context."""
        try:
            # Get recent memories
            recent_memories = await self.memory_store.get_recent_interactions(
                self.user_id, limit=10
            )

            # Get user preferences
            preferences = await self.memory_store.get_user_preferences(
                self.user_id
            )

            # Get goals
            goals = await self.memory_store.get_user_goals(self.user_id)

            # Analyze topics
            topic_analysis = {}
            for topic in self.current_topics:
                if topic in self.topic_sentiments:
                    sentiments = self.topic_sentiments[topic]
                    dominant_sentiment = max(sentiments.items(), key=lambda x: x[1])

                    topic_analysis[topic] = {
                        "sentiment": dominant_sentiment[0],
                        "frequency": dominant_sentiment[1],
                        "context_count": len(self.topic_contexts.get(topic, [])),
                    }

            return {
                "user_state": self.user_state,
                "current_topics": list(self.current_topics),
                "topic_analysis": topic_analysis,
                "recent_memories": [
                    {
                        "content": memory.content,
                        "type": memory.type,
                        "metadata": memory.metadata,
                    }
                    for memory in recent_memories
                ],
                "preferences": [
                    {
                        "content": pref.content,
                        "metadata": pref.metadata,
                    }
                    for pref in preferences
                ],
                "goals": [
                    {
                        "content": goal.content,
                        "metadata": goal.metadata,
                    }
                    for goal in goals
                ],
            }

        except Exception as e:
            logger.error(
                "Error getting context summary",
                error=str(e),
                exc_info=True,
            )
            return {
                "user_state": self.user_state,
                "current_topics": list(self.current_topics),
                "topic_analysis": {},
                "recent_memories": [],
                "preferences": [],
                "goals": [],
            }

    async def generate_context_for_prompt(
        self, max_tokens: int = 1000
    ) -> str:
        """Generate context string for prompt."""
        try:
            # Get context summary
            context = await self.get_context_summary()

            # Format as readable context
            context_parts = []

            # User state
            context_parts.append("User Profile:")
            context_parts.append(f"- Knowledge level: {context['user_state']['knowledge_level']}")
            context_parts.append(f"- Risk tolerance: {context['user_state']['risk_tolerance']}")
            context_parts.append(
                f"- Current goals: {', '.join(context['user_state']['current_goals'])}"
            )
            context_parts.append(
                f"- Active plans: {', '.join(context['user_state']['active_plans'])}"
            )

            # Topics
            if context["current_topics"]:
                context_parts.append(f"\nCurrent topics: {', '.join(context['current_topics'])}")

            # Topic analysis
            if context["topic_analysis"]:
                context_parts.append("\nTopic analysis:")
                for topic, analysis in context["topic_analysis"].items():
                    context_parts.append(
                        f"- {topic}: {analysis['sentiment']} (frequency: {analysis['frequency']})"
                    )

            # Preferences
            if context["preferences"]:
                context_parts.append("\nUser preferences:")
                for pref in context["preferences"][-3:]:  # Last 3 preferences
                    metadata = pref.get("metadata", {})
                    if "key" in metadata:
                        context_parts.append(f"- {metadata['key']}: {pref['content']}")

            # Goals
            if context["goals"]:
                context_parts.append("\nUser goals:")
                for goal in context["goals"][-3:]:  # Last 3 goals
                    metadata = goal.get("metadata", {})
                    if "goal" in metadata:
                        context_parts.append(f"- {metadata['goal']}")

            # Recent memories
            if context["recent_memories"]:
                context_parts.append("\nRecent conversations:")
                for memory in context["recent_memories"][-3:]:  # Last 3 memories
                    context_parts.append(f"- {memory['content'][:100]}...")

            # Combine into a single string
            context_text = "\n".join(context_parts)

            # Truncate if too long
            if len(context_text) > max_tokens:
                context_text = context_text[:max_tokens] + "..."

            return context_text

        except Exception as e:
            logger.error(
                "Error generating context for prompt",
                error=str(e),
                exc_info=True,
            )
            return "Context not available"

    async def clear_old_context(self, days_old: int = 30) -> None:
        """Clear old context from memory."""
        try:
            cutoff_date = datetime.utcnow() - timedelta(days=days_old)

            # Clear old memories
            memories = await self.memory_store.retrieve_memory(
                self.user_id, limit=1000
            )

            for memory in memories:
                if memory.created_at < cutoff_date:
                    await self.memory_store.delete_memory(memory.id)

            # Clear old interaction contexts
            # This would require a separate table for interaction contexts
            # For now, just reset some in-memory context
            self.short_term_memory.clear()

            logger.info(
                "Old context cleared",
                user_id=self.user_id,
                days_old=days_old,
            )

        except Exception as e:
            logger.error(
                "Error clearing old context",
                error=str(e),
                exc_info=True,
            )
