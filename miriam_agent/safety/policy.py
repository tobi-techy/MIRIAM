import logging
from typing import Any

logger = logging.getLogger(__name__)

class SafetyPolicy:
    """Safety and policy enforcement for Miriam Financial Agent."""

    def __init__(self):
        # Initialize policy rules
        self.money_movement_limits = self._load_money_movement_limits()
        self.suspicious_patterns = self._load_suspicious_patterns()
        self.approval_workflow = self._load_approval_workflow()
        self.risk_scores = {}
        self.blocked_addresses = set()
        self.whitelisted_addresses = set()

    def _load_money_movement_limits(self) -> dict[str, Any]:
        """Load money movement limits from configuration."""
        return {
            "daily_limit": 10000.0,
            "transaction_limit": 5000.0,
            "high_risk_daily_limit": 1000.0,
            "high_risk_transaction_limit": 500.0,
            "blocked_categories": ["scam", "fraud", "illegal"],
        }

    def _load_suspicious_patterns(self) -> list[dict[str, Any]]:
        """Load suspicious activity patterns."""
        return [
            {
                "name": "rapid_large_transactions",
                "pattern": "multiple_large_transfers_short_time",
                "threshold": 3,
                "timeframe": "hour",
            },
            {
                "name": "unusual_recipients",
                "pattern": "new_beneficiary_unusual_amount",
                "threshold": 2500.0,
                "timeframe": "day",
            },
            {
                "name": "speed_money",
                "pattern": "immediate_large_transfer",
                "threshold": 5000.0,
                "timeframe": "hour",
            },
            {
                "name": "round_number_amounts",
                "pattern": "multiple_round_number_transfers",
                "threshold": 5,
                "timeframe": "day",
            },
        ]

    def _load_approval_workflow(self) -> dict[str, Any]:
        """Load approval workflow configuration."""
        return {
            "required_for_high_risk": True,
            "required_for_large_amounts": True,
            "required_for_new_recipients": False,
            "auto_approve_below_threshold": 100.0,
            "require_multiple_approvals_above": 5000.0,
        }

    async def validate_action(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user_id: str,
        financial_profile: Any,
    ) -> bool:
        """Validate if an action is safe to execute."""
        try:
            # Check if action is in allowlist
            if not await self._is_action_allowed(tool_name):
                logger.warning(
                    "Action not in allowlist",
                    tool_name=tool_name,
                    user_id=user_id,
                )
                return False

            # Check safety rules
            if not await self._check_safety_rules(
                tool_name, arguments, user_id, financial_profile
            ):
                return False

            # Check risk level
            risk_level = await self._assess_action_risk(
                tool_name, arguments, user_id, financial_profile
            )

            # Check if action requires approval
            requires_approval = await self._requires_approval(
                tool_name, arguments, user_id, financial_profile, risk_level
            )

            if requires_approval:
                logger.info(
                    "Action requires approval",
                    tool_name=tool_name,
                    user_id=user_id,
                    risk_level=risk_level,
                )
                # In a real implementation, we would wait for user approval here
                # For now, we'll log the approval requirement
                await self._log_approval_required(
                    tool_name, arguments, user_id, financial_profile, risk_level
                )

            # Check if action is blocked
            if await self._is_action_blocked(
                tool_name, arguments, user_id, financial_profile
            ):
                logger.error(
                    "Action is blocked",
                    tool_name=tool_name,
                    user_id=user_id,
                )
                return False

            return True

        except Exception as e:
            logger.error(
                "Error validating action",
                tool_name=tool_name,
                user_id=user_id,
                error=str(e),
                exc_info=True,
            )
            return False

    async def _is_action_allowed(self, tool_name: str) -> bool:
        """Check if action is in the allowlist."""
        # Define allowed actions
        allowed_actions = {
            "analyze_portfolio",
            "generate_budget_plan",
            "analyze_transaction",
            "get_financial_advice",
            "get_balance",
            "get_transaction_history",
            "transfer_funds",
            "withdraw_funds",
            "deposit_funds",
            "execute_strategy",
            "get_payment_status",
        }

        return tool_name in allowed_actions

    async def _check_safety_rules(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user_id: str,
        financial_profile: Any,
    ) -> bool:
        """Check if action complies with safety rules."""
        try:
            # Check for suspicious patterns
            if await self._detect_suspicious_patterns(
                arguments, user_id, financial_profile
            ):
                logger.warning(
                    "Suspicious pattern detected",
                    tool_name=tool_name,
                    user_id=user_id,
                )
                return False

            # Check limits
            if await self._check_limits(arguments, user_id, financial_profile):
                logger.warning(
                    "Limit exceeded",
                    tool_name=tool_name,
                    user_id=user_id,
                )
                return False

            # Check blocked categories
            if await self._check_blocked_categories(arguments):
                logger.warning(
                    "Blocked category detected",
                    tool_name=tool_name,
                    user_id=user_id,
                )
                return False

            # Check time-based restrictions
            if await self._check_time_based_restrictions(arguments, user_id):
                logger.warning(
                    "Time-based restriction violated",
                    tool_name=tool_name,
                    user_id=user_id,
                )
                return False

            return True

        except Exception as e:
            logger.error(
                "Error checking safety rules",
                tool_name=tool_name,
                error=str(e),
                exc_info=True,
            )
            return False

    async def _detect_suspicious_patterns(
        self,
        arguments: dict[str, Any],
        user_id: str,
        financial_profile: Any,
    ) -> bool:
        """Detect suspicious activity patterns."""
        try:
            # Get user's recent activity
            recent_activities = await self._get_recent_activities(user_id)

            # Check each pattern
            for pattern in self.suspicious_patterns:
                if await self._check_pattern(
                    pattern, arguments, recent_activities
                ):
                    return True

            return False

        except Exception as e:
            logger.error(
                "Error detecting suspicious patterns",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _check_pattern(
        self,
        pattern: dict[str, Any],
        arguments: dict[str, Any],
        recent_activities: list[dict[str, Any]],
    ) -> bool:
        """Check if a specific pattern is detected."""
        pattern_type = pattern["pattern"]

        if pattern_type == "multiple_large_transfers_short_time":
            return self._check_multiple_large_transfers(
                pattern, arguments, recent_activities
            )

        elif pattern_type == "new_beneficiary_unusual_amount":
            return self._check_new_beneficiary_unusual_amount(
                pattern, arguments, recent_activities
            )

        elif pattern_type == "immediate_large_transfer":
            return self._check_immediate_large_transfer(
                pattern, arguments, recent_activities
            )

        elif pattern_type == "multiple_round_number_transfers":
            return self._check_multiple_round_number_transfers(
                pattern, arguments, recent_activities
            )

        return False

    async def _check_multiple_large_transfers(
        self,
        pattern: dict[str, Any],
        arguments: dict[str, Any],
        recent_activities: list[dict[str, Any]],
    ) -> bool:
        """Check for multiple large transfers in short time."""
        try:
            # Get transfers in the timeframe
            threshold = pattern["threshold"]

            recent_transfers = [
                activity
                for activity in recent_activities
                if activity.get("type") == "transfer"
                and activity.get("amount", 0) >= threshold
            ]

            # Check if there are enough transfers in the timeframe
            return len(recent_transfers) >= threshold

        except Exception as e:
            logger.error(
                "Error checking multiple large transfers",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _check_new_beneficiary_unusual_amount(
        self,
        pattern: dict[str, Any],
        arguments: dict[str, Any],
        recent_activities: list[dict[str, Any]],
    ) -> bool:
        """Check for new beneficiary with unusual amount."""
        try:
            threshold = pattern["threshold"]
            amount = arguments.get("amount", 0)

            if amount < threshold:
                return False

            # Check if beneficiary is new
            beneficiary = arguments.get("destination_account")
            if not beneficiary:
                return False

            # Check if beneficiary has been used before
            previous_transfers = [
                activity
                for activity in recent_activities
                if activity.get("type") == "transfer"
                and activity.get("beneficiary") == beneficiary
            ]

            return len(previous_transfers) == 0

        except Exception as e:
            logger.error(
                "Error checking new beneficiary unusual amount",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _check_immediate_large_transfer(
        self,
        pattern: dict[str, Any],
        arguments: dict[str, Any],
        recent_activities: list[dict[str, Any]],
    ) -> bool:
        """Check for immediate large transfer."""
        try:
            threshold = pattern["threshold"]

            amount = arguments.get("amount", 0)
            if amount < threshold:
                return False

            # Check if transfer was initiated very recently
            # This would require timestamp checking
            # For now, we'll use a simplified check
            recent_transfers = [
                activity
                for activity in recent_activities
                if activity.get("type") == "transfer"
                and activity.get("amount", 0) >= threshold
            ]

            return len(recent_transfers) > 0

        except Exception as e:
            logger.error(
                "Error checking immediate large transfer",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _check_multiple_round_number_transfers(
        self,
        pattern: dict[str, Any],
        arguments: dict[str, Any],
        recent_activities: list[dict[str, Any]],
    ) -> bool:
        """Check for multiple round number transfers."""
        try:
            threshold = pattern["threshold"]

            # Count round number transfers
            round_number_transfers = [
                activity
                for activity in recent_activities
                if activity.get("type") == "transfer"
                and self._is_round_number(activity.get("amount", 0))
            ]

            return len(round_number_transfers) >= threshold

        except Exception as e:
            logger.error(
                "Error checking multiple round number transfers",
                error=str(e),
                exc_info=True,
            )
            return False

    def _is_round_number(self, amount: float) -> bool:
        """Check if amount is a round number."""
        return amount == int(amount) and (amount % 100 == 0 or amount % 1000 == 0)

    async def _check_limits(
        self,
        arguments: dict[str, Any],
        user_id: str,
        financial_profile: Any,
    ) -> bool:
        """Check if action exceeds limits."""
        try:
            amount = arguments.get("amount", 0)

            # Get user's risk level
            risk_level = await self._get_user_risk_level(user_id)

            # Set limits based on risk level
            if risk_level == "high":
                transaction_limit = self.money_movement_limits[
                    "high_risk_transaction_limit"
                ]
            else:
                transaction_limit = self.money_movement_limits["transaction_limit"]

            # Check transaction limit
            if amount > transaction_limit:
                return False

            # Check daily limit (simplified - would need actual daily tracking)
            # This would require checking user's daily total

            return True

        except Exception as e:
            logger.error(
                "Error checking limits",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _check_blocked_categories(
        self, arguments: dict[str, Any]
    ) -> bool:
        """Check if action is in blocked categories."""
        try:
            # Get description or category from arguments
            description = arguments.get("description", "")
            category = arguments.get("category", "")

            # Check against blocked categories
            for blocked_category in self.money_movement_limits["blocked_categories"]:
                if blocked_category.lower() in description.lower():
                    return True
                if blocked_category.lower() == category.lower():
                    return True

            return False

        except Exception as e:
            logger.error(
                "Error checking blocked categories",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _check_time_based_restrictions(
        self, arguments: dict[str, Any], user_id: str
    ) -> bool:
        """Check time-based restrictions."""
        try:
            # Check if action is allowed during restricted hours
            # This would require timezone handling
            # For now, we'll return False (no restrictions)
            return False

        except Exception as e:
            logger.error(
                "Error checking time-based restrictions",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _assess_action_risk(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user_id: str,
        financial_profile: Any,
    ) -> str:
        """Assess risk level of action."""
        try:
            risk_score = 0.0

            # Base risk based on tool
            tool_risk = self._get_tool_risk_level(tool_name)
            risk_score += tool_risk

            # Additional risk based on arguments
            amount = arguments.get("amount", 0)
            if amount > 0:
                if amount > 10000:
                    risk_score += 0.3
                elif amount > 5000:
                    risk_score += 0.2
                elif amount > 1000:
                    risk_score += 0.1

            # Risk based on user profile
            user_risk = await self._get_user_risk_level(user_id)
            risk_score += self._risk_level_to_score(user_risk)

            # Determine final risk level
            if risk_score >= 0.8:
                return "critical"
            elif risk_score >= 0.6:
                return "high"
            elif risk_score >= 0.4:
                return "medium"
            else:
                return "low"

        except Exception as e:
            logger.error(
                "Error assessing action risk",
                error=str(e),
                exc_info=True,
            )
            return "low"

    async def _requires_approval(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user_id: str,
        financial_profile: Any,
        risk_level: str,
    ) -> bool:
        """Check if action requires approval."""
        try:
            # Check if action is in approval workflow
            if tool_name in self.approval_workflow.get("require_approval", []):
                return True

            # Check risk level
            if risk_level in ["high", "critical"] and self.approval_workflow.get(
                "required_for_high_risk", False
            ):
                return True

            # Check amount
            amount = arguments.get("amount", 0)
            if amount > self.approval_workflow.get("required_for_large_amounts", 0):
                return True

            # Check if auto-approval is possible
            if amount <= self.approval_workflow.get(
                "auto_approve_below_threshold", 0
            ):
                return False

            # Default to requiring approval for money movement
            if tool_name in [
                "transfer_funds",
                "withdraw_funds",
                "deposit_funds",
                "execute_strategy",
            ]:
                return True

            return False

        except Exception as e:
            logger.error(
                "Error checking if approval required",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _is_action_blocked(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user_id: str,
        financial_profile: Any,
    ) -> bool:
        """Check if action is blocked."""
        try:
            # Check if user is blocked
            if user_id in self.blocked_addresses:
                return True

            # Check if action is globally blocked
            blocked_tools = self.approval_workflow.get("blocked_tools", [])
            if tool_name in blocked_tools:
                return True

            # Check for suspicious activity
            if await self._is_user_suspicious(user_id):
                return True

            # Check for failed attempts
            if await self._has_failed_attempts(user_id):
                return True

            return False

        except Exception as e:
            logger.error(
                "Error checking if action is blocked",
                error=str(e),
                exc_info=True,
            )
            return False

    def _get_tool_risk_level(self, tool_name: str) -> float:
        """Get risk level for a tool."""
        tool_risks = {
            "analyze_portfolio": 0.1,
            "generate_budget_plan": 0.1,
            "analyze_transaction": 0.2,
            "get_financial_advice": 0.1,
            "get_balance": 0.1,
            "get_transaction_history": 0.2,
            "transfer_funds": 0.8,
            "withdraw_funds": 0.7,
            "deposit_funds": 0.6,
            "execute_strategy": 0.9,
            "get_payment_status": 0.3,
        }

        return tool_risks.get(tool_name, 0.5)

    async def _get_user_risk_level(self, user_id: str) -> str:
        """Get user's risk level."""
        try:
            # Get user profile
            # This would require database access
            # For now, return default
            return "medium"

        except Exception as e:
            logger.error(
                "Error getting user risk level",
                error=str(e),
                exc_info=True,
            )
            return "medium"

    def _risk_level_to_score(self, risk_level: str) -> float:
        """Convert risk level to score."""
        risk_scores = {
            "low": 0.1,
            "medium": 0.4,
            "high": 0.7,
            "critical": 0.9,
        }

        return risk_scores.get(risk_level, 0.5)

    async def _get_recent_activities(
        self, user_id: str, timeframe_hours: int = 24
    ) -> list[dict[str, Any]]:
        """Get user's recent activities."""
        try:
            # This would query the database for recent user activities
            # For now, return empty list
            return []

        except Exception as e:
            logger.error(
                "Error getting recent activities",
                error=str(e),
                exc_info=True,
            )
            return []

    async def _is_user_suspicious(self, user_id: str) -> bool:
        """Check if user is suspicious."""
        try:
            # Check against suspicious patterns
            recent_activities = await self._get_recent_activities(user_id)

            for pattern in self.suspicious_patterns:
                if await self._check_pattern(
                    pattern, {}, recent_activities
                ):
                    return True

            return False

        except Exception as e:
            logger.error(
                "Error checking if user is suspicious",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _has_failed_attempts(self, user_id: str) -> bool:
        """Check if user has failed attempts."""
        try:
            # This would check for failed authentication attempts
            # For now, return False
            return False

        except Exception as e:
            logger.error(
                "Error checking failed attempts",
                error=str(e),
                exc_info=True,
            )
            return False

    async def _log_approval_required(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        user_id: str,
        financial_profile: Any,
        risk_level: str,
    ) -> None:
        """Log approval requirement."""
        try:
            # Audit-log the approval
            logger.info(
                "Action requires approval logged",
                user_id=user_id,
                action=tool_name,
                risk_level=risk_level,
            )

        except Exception as e:
            logger.error(
                "Error logging approval requirement",
                error=str(e),
                exc_info=True,
            )

    async def add_blocked_address(self, address: str) -> None:
        """Add an address to blocked list."""
        try:
            self.blocked_addresses.add(address)
            logger.info("Address blocked", address=address)
        except Exception as e:
            logger.error(
                "Error blocking address",
                error=str(e),
                exc_info=True,
            )

    async def remove_blocked_address(self, address: str) -> None:
        """Remove an address from blocked list."""
        try:
            self.blocked_addresses.discard(address)
            logger.info("Address unblocked", address=address)
        except Exception as e:
            logger.error(
                "Error unblocking address",
                error=str(e),
                exc_info=True,
            )

    async def add_whitelisted_address(self, address: str) -> None:
        """Add an address to whitelisted list."""
        try:
            self.whitelisted_addresses.add(address)
            logger.info("Address whitelisted", address=address)
        except Exception as e:
            logger.error(
                "Error whitelisting address",
                error=str(e),
                exc_info=True,
            )

    async def is_address_whitelisted(self, address: str) -> bool:
        """Check if address is whitelisted."""
        return address in self.whitelisted_addresses
