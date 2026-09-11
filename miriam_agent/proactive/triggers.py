import asyncio
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta

from miriam_agent.database.models import User, FinancialProfile
from miriam_agent.memory import MemoryStore

logger = logging.getLogger(__name__)

class ProactiveFeatures:
    """Proactive features for Miriam Financial Agent."""

    def __init__(
        self,
        memory_store: MemoryStore,
        user_id: str,
        financial_profile: FinancialProfile,
    ):
        self.memory_store = memory_store
        self.user_id = user_id
        self.financial_profile = financial_profile

        # Trigger configuration
        self.triggers = self._load_triggers()
        self.trigger_history = []
        self.last_check = datetime.utcnow()

    def _load_triggers(self) -> List[Dict[str, Any]]:
        """Load proactive triggers from configuration."""
        return [
            {
                "name": "budget_review",
                "condition": "monthly_budget_exceeded",
                "description": "Review budget when monthly spending exceeds budget by 20%",
                "priority": "high",
                "frequency": "monthly",
            },
            {
                "name": "investment_alert",
                "condition": "market_volatility",
                "description": "Alert user about market volatility",
                "priority": "medium",
                "frequency": "daily",
            },
            {
                "name": "goal_progress",
                "condition": "goal_progress_milestone",
                "description": "Celebrate goal progress milestones",
                "priority": "low",
                "frequency": "weekly",
            },
            {
                "name": "spending_anomaly",
                "condition": "unusual_spending_pattern",
                "description": "Alert on unusual spending patterns",
                "priority": "high",
                "frequency": "continuous",
            },
            {
                "name": "financial_health",
                "condition": "financial_health_decline",
                "description": "Monitor and alert on financial health decline",
                "priority": "medium",
                "frequency": "weekly",
            },
            {
                "name": "cash_flow_alert",
                "condition": "negative_cash_flow",
                "description": "Alert on negative cash flow",
                "priority": "high",
                "frequency": "daily",
            },
        ]

    async def check_proactive_triggers(self) -> List[Dict[str, Any]]:
        """Check for proactive triggers to fire."""
        try:
            triggered_actions = []

            # Check each trigger
            for trigger in self.triggers:
                if await self._should_trigger_fire(trigger):
                    action = await self._fire_trigger(trigger)
                    if action:
                        triggered_actions.append(action)

            # Update trigger history
            self.trigger_history.append(
                {
                    "timestamp": datetime.utcnow().isoformat(),
                    "triggered_actions": triggered_actions,
                }
            )

            return triggered_actions

        except Exception as e:
            logger.error(
                "Error checking proactive triggers",
                error=str(e),
                exc_info=True,
            )
            return []

    async def _should_trigger_fire(self, trigger: Dict[str, Any]) -> bool:
        """Check if a trigger should fire based on current conditions."""
        try:
            condition = trigger["condition"]

            if condition == "monthly_budget_exceeded":
                return await self._check_monthly_budget_exceeded()

            elif condition == "market_volatility":
                return await self._check_market_volatility()

            elif condition == "goal_progress_milestone":
                return await self._check_goal_progress_milestone()

            elif condition == "unusual_spending_pattern":
                return await self._check_unusual_spending_pattern()

            elif condition == "financial_health_decline":
                return await self._check_financial_health_decline()

            elif condition == "negative_cash_flow":
                return await self._check_negative_cash_flow()

            return False

        except Exception as e:
            logger.error(
                "Error checking trigger condition",
                condition=trigger["condition"],
                error=str(e),
                exc_info=True,
            )
            return False

    async def _check_monthly_budget_exceeded(self) -> bool:
        """Check if monthly budget has been exceeded."""
        try:
            # Get current month expenses and budget
            current_month = datetime.utcnow().replace(day=1).strftime("%Y-%m-%d")
            expenses = await self._get_monthly_expenses(current_month)

            # Get budget limits
            budgets = await self.memory_store.get_user_goals(self.user_id)

            for budget in budgets:
                metadata = budget.metadata
                if metadata.get("type") == "budget":
                    monthly_limit = metadata.get("monthly_limit", 0)
                    if expenses > (monthly_limit * 1.2):  # 20% over budget
                        return True

            return False

        except Exception as e:
            logger.error(
                "Error checking monthly budget exceeded",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _check_market_volatility(self) -> bool:
        """Check if market volatility warrants an alert."""
        try:
            # Get user's investments
            investments = await self.memory_store.get_user_goals(self.user_id)

            # Check if user has investments
            if not investments:
                return False

            # Simple volatility check based on user's investment pattern
            # In production, this would use real market data
            volatility_score = await self._calculate_volatility_score()

            return volatility_score > 0.5  # High volatility threshold

        except Exception as e:
            logger.error(
                "Error checking market volatility",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _check_goal_progress_milestone(self) -> bool:
        """Check if user has reached a goal progress milestone."""
        try:
            # Get user's goals
            goals = await self.memory_store.get_user_goals(self.user_id)

            for goal in goals:
                metadata = goal.metadata
                if metadata.get("type") == "financial_goal":
                    # Check if goal progress has reached a milestone
                    current_progress = metadata.get("current_progress", 0)
                    target = metadata.get("target", 1)

                    if target > 0:
                        progress_percentage = current_progress / target * 100

                        # Check for milestone thresholds
                        milestones = [25, 50, 75, 100]
                        for milestone in milestones:
                            if progress_percentage >= milestone:
                                # Check if this milestone has been achieved before
                                if not await self._has_achieved_milestone(
                                    goal.id, milestone
                                ):
                                    return True

            return False

        except Exception as e:
            logger.error(
                "Error checking goal progress milestone",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _check_unusual_spending_pattern(self) -> bool:
        """Check for unusual spending patterns."""
        try:
            # Get recent spending patterns
            recent_patterns = await self._get_recent_spending_patterns()

            # Compare with user's historical spending
            historical_patterns = await self._get_historical_spending_patterns()

            # Detect anomalies
            anomalies = await self._detect_spending_anomalies(
                recent_patterns, historical_patterns
            )

            return len(anomalies) > 0

        except Exception as e:
            logger.error(
                "Error checking unusual spending pattern",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _check_financial_health_decline(self) -> bool:
        """Check if financial health is declining."""
        try:
            # Get current financial health score
            current_score = await self._calculate_financial_health_score()

            # Get historical scores
            historical_scores = await self._get_historical_financial_scores()

            if not historical_scores:
                return False

            # Check if current score is declining
            previous_score = historical_scores[-1]
            decline = previous_score - current_score

            return decline > 5  # More than 5 points decline

        except Exception as e:
            logger.error(
                "Error checking financial health decline",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _check_negative_cash_flow(self) -> bool:
        """Check for negative cash flow."""
        try:
            # Get current cash flow
            cash_flow = await self._get_current_cash_flow()

            return cash_flow < 0

        except Exception as e:
            logger.error(
                "Error checking negative cash flow",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _fire_trigger(self, trigger: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Fire a proactive trigger and generate action."""
        try:
            # Generate trigger-specific action
            action = {
                "trigger_name": trigger["name"],
                "condition": trigger["condition"],
                "description": trigger["description"],
                "priority": trigger["priority"],
                "timestamp": datetime.utcnow().isoformat(),
                "message": await self._generate_trigger_message(trigger),
                "action_type": "alert",
            }

            # Log the action
            logger.info(
                "Proactive trigger fired",
                trigger_name=trigger["name"],
                user_id=self.user_id,
                message=action["message"],
            )

            # Store in memory
            await self.memory_store.store_memory(
                user_id=self.user_id,
                memory_type="proactive_action",
                content=f"Triggered: {trigger['name']} - {action['message']}",
                metadata={
                    "trigger_name": trigger["name"],
                    "condition": trigger["condition"],
                    "priority": trigger["priority"],
                    "timestamp": action["timestamp"],
                },
            )

            return action

        except Exception as e:
            logger.error(
                "Error firing trigger",
                trigger_name=trigger["name"],
                error=str(e),
                exc_info=True,
            )
            return None

    async def _generate_trigger_message(self, trigger: Dict[str, Any]) -> str:
        """Generate a user-friendly message for a triggered action."""
        try:
            messages = {
                "budget_review": "Your monthly budget has been exceeded by 20%! Would you like me to help you adjust your spending plan?",
                "investment_alert": "Market volatility detected. Your portfolio has experienced significant fluctuations. Let's review your investment strategy.",
                "goal_progress_milestone": "Great progress on your financial goals! You've reached a new milestone. Let's discuss next steps.",
                "spending_anomaly": "I noticed an unusual spending pattern in your recent transactions. Would you like me to investigate?",
                "financial_health_decline": "Your financial health score has declined recently. Let's review your current financial situation and identify areas for improvement.",
                "cash_flow_alert": "You currently have a negative cash flow. This means your expenses are exceeding your income. Let's discuss strategies to improve your cash situation.",
            }

            return messages.get(
                trigger["condition"], f"Proactive alert: {trigger['description']}"
            )

        except Exception as e:
            logger.error(
                "Error generating trigger message",
                error=str(e),
                exc_info=True,
            )
            return f"Proactive alert: {trigger['description']}"

    async def _calculate_volatility_score(self) -> float:
        """Calculate portfolio volatility score."""
        try:
            # Simple volatility calculation based on user's investments
            # In production, this would use real market data and volatility models

            investments = await self.memory_store.get_user_goals(self.user_id)

            if not investments:
                return 0.0

            # Calculate based on investment diversity and recent performance
            volatility_score = 0.0

            for investment in investments:
                metadata = investment.metadata
                if metadata.get("type") == "investment":
                    # Check if investment has been recently volatile
                    recent_performance = metadata.get("recent_performance", 0)
                    volatility_score += abs(recent_performance) / 100

            # Normalize to 0-1 range
            max_volatility = len(investments) * 1.0
            return min(volatility_score / max_volatility if max_volatility > 0 else 0.0, 1.0)

        except Exception as e:
            logger.error(
                "Error calculating volatility score",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    async def _has_achieved_milestone(
        self, goal_id: str, milestone: int
    ) -> bool:
        """Check if a goal milestone has already been achieved."""
        try:
            # Check if this milestone has been recorded before
            goal_memories = await self.memory_store.retrieve_memory(
                self.user_id, limit=100
            )

            for memory in goal_memories:
                metadata = memory.metadata
                if (
                    metadata.get("type") == "milestone"
                    and metadata.get("goal_id") == goal_id
                    and metadata.get("milestone") == milestone
                ):
                    return True

            return False

        except Exception as e:
            logger.error(
                "Error checking milestone achievement",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _get_monthly_expenses(self, month: str) -> float:
        """Get monthly expenses."""
        try:
            # This would query the database for expenses in the specified month
            # For now, return a default value
            return 0.0

        except Exception as e:
            logger.error(
                "Error getting monthly expenses",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    async def _get_recent_spending_patterns(self) -> List[Dict[str, Any]]:
        """Get recent spending patterns."""
        try:
            # This would analyze recent transactions
            # For now, return empty list
            return []

        except Exception as e:
            logger.error(
                "Error getting recent spending patterns",
                error=str(e),
                exc_info=True,
            )
            return []

    async def _get_historical_spending_patterns(self) -> List[Dict[str, Any]]:
        """Get historical spending patterns."""
        try:
            # This would analyze historical transaction data
            # For now, return empty list
            return []

        except Exception as e:
            logger.error(
                "Error getting historical spending patterns",
                error=str(e),
                exc_info=True,
            )
            return []

    async def _detect_spending_anomalies(
        self, recent_patterns: List[Dict[str, Any]], historical_patterns: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Detect spending anomalies."""
        try:
            anomalies = []

            # Compare recent and historical patterns
            for pattern in recent_patterns:
                # Find similar pattern in historical data
                similar_patterns = [
                    hist
                    for hist in historical_patterns
                    if self._is_similar_spending_pattern(pattern, hist)
                ]

                if not similar_patterns:
                    anomalies.append(
                        {
                            "pattern": pattern,
                            "type": "unusual",
                            "reason": "No similar historical pattern found",
                        }
                    )

            return anomalies

        except Exception as e:
            logger.error(
                "Error detecting spending anomalies",
                error=str(e),
                exc_info=True,
            )
            return []

    def _is_similar_spending_pattern(
        self, pattern1: Dict[str, Any], pattern2: Dict[str, Any]
    ) -> bool:
        """Check if two spending patterns are similar."""
        # Simple similarity check based on amount, category, and timing
        try:
            return (
                pattern1.get("category") == pattern2.get("category")
                and abs(
                    pattern1.get("amount", 0) - pattern2.get("amount", 0)
                )
                / max(pattern2.get("amount", 1), 1)
                < 0.2  # Within 20% of amount
            )
        except Exception:
            return False

    async def _calculate_financial_health_score(self) -> float:
        """Calculate current financial health score."""
        try:
            # Simple financial health score calculation
            # In production, this would use more sophisticated metrics

            score = 100.0

            # Deduct points for various issues
            # Check cash flow
            cash_flow = await self._get_current_cash_flow()
            if cash_flow < 0:
                score -= 20

            # Check savings rate
            savings_rate = await self._get_savings_rate()
            if savings_rate < 0.1:  # Less than 10% savings rate
                score -= 10

            # Check debt level
            debt_level = await self._get_debt_level()
            if debt_level > 0.3:  # More than 30% debt-to-income
                score -= 15

            # Check budget adherence
            budget_adherence = await self._get_budget_adherence()
            if budget_adherence < 0.8:  # Less than 80% budget adherence
                score -= 10

            return max(0.0, score)

        except Exception as e:
            logger.error(
                "Error calculating financial health score",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    async def _get_current_cash_flow(self) -> float:
        """Get current cash flow."""
        try:
            # This would calculate current cash flow
            # For now, return a default value
            return 0.0

        except Exception as e:
            logger.error(
                "Error getting current cash flow",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    async def _get_savings_rate(self) -> float:
        """Get savings rate."""
        try:
            # Calculate savings rate
            # This would use income and savings data
            # For now, return a default value
            return 0.15

        except Exception as e:
            logger.error(
                "Error getting savings rate",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    async def _get_debt_level(self) -> float:
        """Get debt level (debt-to-income ratio)."""
        try:
            # Calculate debt-to-income ratio
            # This would use income and debt data
            # For now, return a default value
            return 0.25

        except Exception as e:
            logger.error(
                "Error getting debt level",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    async def _get_budget_adherence(self) -> float:
        """Get budget adherence rate."""
        try:
            # Calculate budget adherence
            # This would compare actual spending to budget
            # For now, return a default value
            return 0.85

        except Exception as e:
            logger.error(
                "Error getting budget adherence",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    async def _get_historical_financial_scores(self) -> List[float]:
        """Get historical financial scores."""
        try:
            # Get historical scores from memory
            # This would store daily scores
            # For now, return empty list
            return []

        except Exception as e:
            logger.error(
                "Error getting historical financial scores",
                error=str(e),
                exc_info=True,
            )
            return []

    async def schedule_proactive_actions(self) -> List[Dict[str, Any]]:
        """Schedule proactive actions for the current day."""
        try:
            # Check for proactive triggers
            triggered_actions = await self.check_proactive_triggers()

            # Schedule actions
            scheduled_actions = []

            for action in triggered_actions:
                scheduled_action = {
                    "action": action,
                    "scheduled_time": datetime.utcnow().isoformat(),
                    "priority": action["priority"],
                    "status": "scheduled",
                }
                scheduled_actions.append(scheduled_action)

            logger.info(
                "Proactive actions scheduled",
                user_id=self.user_id,
                action_count=len(scheduled_actions),
            )

            return scheduled_actions

        except Exception as e:
            logger.error(
                "Error scheduling proactive actions",
                error=str(e),
                exc_info=True,
            )
            return []
