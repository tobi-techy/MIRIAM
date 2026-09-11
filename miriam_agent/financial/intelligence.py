import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from miriam_agent.core.exceptions import FinancialError
from miriam_agent.core.models import FinancialGoal, InvestmentStrategy

logger = logging.getLogger(__name__)

class FinancialIntelligence:
    """Financial intelligence and analysis module for Miriam Financial Agent."""

    def __init__(self, memory_store: Any):
        self.memory_store = memory_store
        self.llm_cache = {}
        self.risk_cache = {}
        self.pattern_cache = {}

    async def analyze_intent(
        self, message: str, financial_profile: Any
    ) -> Dict[str, Any]:
        """Analyze user message to determine intent."""
        try:
            # Simple intent detection based on keywords
            intent_keywords = {
                "analysis": ["analyze", "check", "review", "show", "view", "see"],
                "planning": [
                    "plan",
                    "create",
                    "set up",
                    "establish",
                    "develop",
                    "design",
                ],
                "transaction": [
                    "transfer",
                    "send",
                    "receive",
                    "pay",
                    "spend",
                    "earn",
                ],
                "advice": [
                    "advice",
                    "suggest",
                    "recommend",
                    "help",
                    "guide",
                    "tell me",
                ],
                "alert": [
                    "alert",
                    "notify",
                    "warn",
                    "important",
                    "urgent",
                    "attention",
                ],
            }

            message_lower = message.lower()
            detected_intent = "general"

            for intent, keywords in intent_keywords.items():
                if any(keyword in message_lower for keyword in keywords):
                    detected_intent = intent
                    break

            # Extract additional context
            timeframe = self._extract_timeframe(message)
            target_amount = self._extract_amount(message)
            categories = self._extract_categories(message)

            return {
                "type": detected_intent,
                "description": self._generate_intent_description(
                    detected_intent, message
                ),
                "timeframe": timeframe,
                "target_amount": target_amount,
                "categories": categories,
                "urgency": self._assess_urgency(message),
                "risk_level": self._assess_risk_level(message, financial_profile),
            }

        except Exception as e:
            logger.error(
                "Error analyzing intent",
                error=str(e),
                exc_info=True,
            )
            raise FinancialError(f"Failed to analyze intent: {str(e)}")

    def _extract_timeframe(self, message: str) -> Optional[str]:
        """Extract timeframe from message."""
        timeframes = {
            "today": "day",
            "tomorrow": "day",
            "this week": "week",
            "this month": "month",
            "this year": "year",
            "next week": "week",
            "next month": "month",
        }

        message_lower = message.lower()
        for phrase, timeframe in timeframes.items():
            if phrase in message_lower:
                return timeframe

        return None

    def _extract_amount(self, message: str) -> Optional[float]:
        """Extract monetary amount from message."""
        import re

        # Match patterns like $100, 100 USD, 100.50, etc.
        amount_pattern = r"\$?\s*(\d+(?:\.\d{2})?)\s*(?:USD|dollars?|USD)?"
        match = re.search(amount_pattern, message)

        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass

        return None

    def _extract_categories(self, message: str) -> List[str]:
        """Extract financial categories from message."""
        categories = {
            "food": ["food", "groceries", "dining", "restaurant", "meal"],
            "transport": [
                "transport",
                "car",
                "gas",
                "fuel",
                "ride",
                "taxi",
                "uber",
            ],
            "housing": ["housing", "rent", "mortgage", "utilities"],
            "entertainment": [
                "entertainment",
                "movies",
                "show",
                "game",
                "streaming",
            ],
            "shopping": [
                "shopping",
                "clothes",
                "purchase",
                "buy",
                "online",
            ],
            "health": [
                "health",
                "medical",
                "doctor",
                "pharmacy",
                "prescription",
            ],
            "savings": ["savings", "saving", "invest", "investment"],
        }

        message_lower = message.lower()
        detected_categories = []

        for category, keywords in categories.items():
            if any(keyword in message_lower for keyword in keywords):
                detected_categories.append(category)

        return detected_categories

    def _generate_intent_description(
        self, intent_type: str, message: str
    ) -> str:
        """Generate description of the detected intent."""
        descriptions = {
            "analysis": "User wants to analyze their financial situation or data",
            "planning": "User wants to create or modify financial plans",
            "transaction": "User wants to perform or review transactions",
            "advice": "User is seeking financial advice or recommendations",
            "alert": "User wants to be alerted about important financial matters",
            "general": "User is asking general financial questions",
        }

        base_description = descriptions.get(
            intent_type, "User has a financial inquiry"
        )

        # Add context from message
        if "budget" in message.lower():
            return f"{base_description} related to budgeting"

        if "investment" in message.lower():
            return f"{base_description} related to investments"

        if "debt" in message.lower():
            return f"{base_description} related to debt management"

        return base_description

    def _assess_urgency(self, message: str) -> str:
        """Assess urgency level of the message."""
        urgent_words = [
            "urgent",
            "asap",
            "immediately",
            "right now",
            "today",
            "critical",
            "emergency",
            "important",
            "time sensitive",
        ]

        message_lower = message.lower()
        if any(word in message_lower for word in urgent_words):
            return "high"

        # Check for negative words that might indicate urgency
        if any(
            word in message_lower
            for word in ["problem", "issue", "error", "wrong", "mistake"]
        ):
            return "medium"

        return "low"

    def _assess_risk_level(
        self, message: str, financial_profile: Any
    ) -> str:
        """Assess risk level of the requested action."""
        # Check message for high-risk keywords
        high_risk_words = [
            "large amount",
            "significant",
            "substantial",
            "major",
            "big",
            "high value",
            "expensive",
            "large transaction",
        ]

        message_lower = message.lower()
        if any(word in message_lower for word in high_risk_words):
            return "high"

        # Check financial profile for risk indicators
        if financial_profile and hasattr(financial_profile, "risk_tolerance"):
            if financial_profile.risk_tolerance.lower() == "high":
                return "medium"

        return "low"

    async def analyze_portfolio(
        self, user_id: str, period: str = "month"
    ) -> Dict[str, Any]:
        """Analyze user's investment portfolio."""
        try:
            # Get portfolio data from memory store
            portfolio_data = await self.memory_store.get_portfolio_data(
                user_id, period
            )

            if not portfolio_data:
                return {
                    "error": "No portfolio data found",
                    "suggestions": "Please connect your accounts to analyze portfolio",
                }

            # Calculate performance metrics
            performance = self._calculate_portfolio_performance(
                portfolio_data, period
            )

            # Identify risk factors
            risk_factors = self._identify_risk_factors(portfolio_data)

            # Generate recommendations
            recommendations = await self._generate_portfolio_recommendations(
                portfolio_data, performance, risk_factors
            )

            return {
                "performance": performance,
                "risk_factors": risk_factors,
                "recommendations": recommendations,
                "diversification_score": self._calculate_diversification_score(
                    portfolio_data
                ),
            }

        except Exception as e:
            logger.error(
                "Error analyzing portfolio",
                error=str(e),
                exc_info=True,
            )
            raise FinancialError(f"Failed to analyze portfolio: {str(e)}")

    def _calculate_portfolio_performance(
        self, portfolio_data: Dict[str, Any], period: str
    ) -> Dict[str, Any]:
        """Calculate portfolio performance metrics."""
        try:
            # Calculate returns
            total_value = sum(
                holding["value"] for holding in portfolio_data.get("holdings", [])
            )
            cost_basis = sum(
                holding["cost_basis"]
                for holding in portfolio_data.get("holdings", [])
            )

            total_return = total_value - cost_basis
            return_percentage = (
                (total_return / cost_basis * 100) if cost_basis > 0 else 0
            )

            # Calculate volatility (simplified)
            returns = self._calculate_daily_returns(portfolio_data)
            volatility = np.std(returns) * np.sqrt(252) if returns else 0

            # Calculate Sharpe ratio (simplified)
            risk_free_rate = 0.02  # 2% risk-free rate
            excess_returns = returns - risk_free_rate if len(returns) > 0 else []
            sharpe_ratio = (
                (np.mean(excess_returns) / np.std(returns))
                if returns and np.std(returns) > 0
                else 0
            )

            return {
                "total_value": total_value,
                "cost_basis": cost_basis,
                "total_return": total_return,
                "return_percentage": return_percentage,
                "volatility": volatility,
                "sharpe_ratio": sharpe_ratio,
                "period": period,
            }

        except Exception as e:
            logger.error(
                "Error calculating portfolio performance",
                error=str(e),
                exc_info=True,
            )
            return {"error": str(e)}

    def _identify_risk_factors(
        self, portfolio_data: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Identify risk factors in the portfolio."""
        risk_factors = []

        try:
            holdings = portfolio_data.get("holdings", [])

            # Check concentration risk
            if holdings:
                total_value = sum(holding["value"] for holding in holdings)
                if total_value > 0:
                    max_position = max(
                        (holding["value"] / total_value)
                        for holding in holdings
                    )
                    if max_position > 0.5:
                        risk_factors.append(
                            {
                                "type": "concentration",
                                "severity": "high",
                                "description": f"Largest position represents {max_position * 100:.1f}% of portfolio",
                            }
                        )

            # Check sector concentration
            sectors = {}
            for holding in holdings:
                sector = holding.get("sector", "unknown")
                sectors[sector] = sectors.get(sector, 0) + holding["value"]

            if sectors:
                total_value = sum(sectors.values())
                for sector, value in sectors.items():
                    if value / total_value > 0.4:
                        risk_factors.append(
                            {
                                "type": "sector_concentration",
                                "severity": "medium",
                                "description": f"{sector} represents {(value / total_value) * 100:.1f}% of portfolio",
                            }
                        )

            # Check cash position
            cash_percentage = self._get_cash_percentage(portfolio_data)
            if cash_percentage > 0.2:
                risk_factors.append(
                    {
                        "type": "low_return_assets",
                        "severity": "low",
                        "description": f"Cash and cash equivalents represent {cash_percentage * 100:.1f}% of portfolio",
                    }
                )

        except Exception as e:
            logger.error(
                "Error identifying risk factors",
                error=str(e),
                exc_info=True,
            )

        return risk_factors

    async def _generate_portfolio_recommendations(
        self,
        portfolio_data: Dict[str, Any],
        performance: Dict[str, Any],
        risk_factors: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Generate portfolio recommendations."""
        recommendations = []

        try:
            # Diversification recommendation
            diversification_score = self._calculate_diversification_score(
                portfolio_data
            )
            if diversification_score < 0.7:
                recommendations.append(
                    {
                        "type": "diversification",
                        "priority": "high",
                        "description": "Consider diversifying your portfolio across different asset classes and sectors",
                        "expected_impact": "Reduced risk, potentially better risk-adjusted returns",
                    }
                )

            # Rebalancing recommendation
            if performance.get("return_percentage", 0) > 20:
                recommendations.append(
                    {
                        "type": "rebalancing",
                        "priority": "medium",
                        "description": "Portfolio has experienced significant gains. Consider rebalancing to maintain target allocation",
                        "expected_impact": "Maintained risk profile and locked in gains",
                    }
                )

            # Risk management recommendation
            high_risk_factors = [
                factor for factor in risk_factors if factor.get("severity") == "high"
            ]
            if high_risk_factors:
                recommendations.append(
                    {
                        "type": "risk_management",
                        "priority": "high",
                        "description": "High risk factors detected. Consider implementing risk mitigation strategies",
                        "expected_impact": "Reduced portfolio volatility and potential losses",
                    }
                )

            # Income generation recommendation
            cash_percentage = self._get_cash_percentage(portfolio_data)
            if cash_percentage < 0.1:
                recommendations.append(
                    {
                        "type": "income_generation",
                        "priority": "low",
                        "description": "Consider allocating a portion of your portfolio to income-generating assets",
                        "expected_impact": "Additional cash flow and diversification",
                    }
                )

            # Default recommendation if no specific recommendations
            if not recommendations:
                recommendations.append(
                    {
                        "type": "maintenance",
                        "priority": "low",
                        "description": "Your portfolio looks well-balanced. Continue with regular monitoring and contributions",
                        "expected_impact": "Maintained financial goals on track",
                    }
                )

        except Exception as e:
            logger.error(
                "Error generating portfolio recommendations",
                error=str(e),
                exc_info=True,
            )

        return recommendations

    def _calculate_diversification_score(self, portfolio_data: Dict[str, Any]) -> float:
        """Calculate portfolio diversification score."""
        try:
            holdings = portfolio_data.get("holdings", [])

            if not holdings:
                return 0.0

            # Calculate Herfindahl-Hirschman Index (HHI)
            total_value = sum(holding["value"] for holding in holdings)
            if total_value <= 0:
                return 0.0

            hhi = sum(
                (holding["value"] / total_value) ** 2
                for holding in holdings
            )

            # Convert HHI to diversification score (0-1, where 1 is most diversified)
            diversification_score = max(0, 1 - hhi)

            return diversification_score

        except Exception as e:
            logger.error(
                "Error calculating diversification score",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    def _get_cash_percentage(self, portfolio_data: Dict[str, Any]) -> float:
        """Get percentage of portfolio in cash."""
        try:
            holdings = portfolio_data.get("holdings", [])

            if not holdings:
                return 0.0

            total_value = sum(holding["value"] for holding in holdings)
            if total_value <= 0:
                return 0.0

            cash_value = sum(
                holding["value"]
                for holding in holdings
                if holding.get("type", "") == "cash"
            )

            return cash_value / total_value

        except Exception as e:
            logger.error(
                "Error calculating cash percentage",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    async def generate_budget_plan(
        self, user_id: str, goal: str = "balance"
    ) -> Dict[str, Any]:
        """Generate a budget plan for the user."""
        try:
            # Get user's financial data
            income_data = await self.memory_store.get_income_data(user_id)
            expense_data = await self.memory_store.get_expense_data(user_id)

            # Calculate monthly income
            monthly_income = self._calculate_monthly_income(income_data)

            # Calculate monthly expenses
            monthly_expenses = self._calculate_monthly_expenses(expense_data)

            # Determine savings target
            savings_target = self._calculate_savings_target(
                monthly_income, goal
            )

            # Allocate budget to categories
            budget_allocation = self._allocate_budget(
                monthly_income, expense_data, savings_target
            )

            return {
                "monthly_income": monthly_income,
                "monthly_expenses": monthly_expenses,
                "savings_target": savings_target,
                "budget_allocation": budget_allocation,
                "net_cash_flow": monthly_income - monthly_expenses - savings_target,
                "recommendations": await self._generate_budget_recommendations(
                    budget_allocation, expense_data
                ),
            }

        except Exception as e:
            logger.error(
                "Error generating budget plan",
                error=str(e),
                exc_info=True,
            )
            raise FinancialError(f"Failed to generate budget plan: {str(e)}")

    def _calculate_monthly_income(self, income_data: Dict[str, Any]) -> float:
        """Calculate monthly income from income data."""
        try:
            monthly_income = 0.0

            # Sum regular income
            for income in income_data.get("regular_income", []):
                amount = income.get("amount", 0)
                frequency = income.get("frequency", "monthly")

                if frequency == "monthly":
                    monthly_income += amount
                elif frequency == "weekly":
                    monthly_income += amount * 4.33
                elif frequency == "biweekly":
                    monthly_income += amount * 2.17
                elif frequency == "quarterly":
                    monthly_income += amount / 3
                elif frequency == "annual":
                    monthly_income += amount / 12

            # Add bonus/income
            for income in income_data.get("bonus_income", []):
                monthly_income += income.get("monthly_amount", 0)

            return monthly_income

        except Exception as e:
            logger.error(
                "Error calculating monthly income",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    def _calculate_monthly_expenses(
        self, expense_data: Dict[str, Any]
    ) -> Dict[str, float]:
        """Calculate monthly expenses by category."""
        try:
            monthly_expenses = {}

            # Average expenses by category
            for category, expenses in expense_data.get(
                "category_expenses", {}
            ).items():
                monthly_expenses[category] = sum(
                    expense.get("monthly_amount", 0) for expense in expenses
                )

            return monthly_expenses

        except Exception as e:
            logger.error(
                "Error calculating monthly expenses",
                error=str(e),
                exc_info=True,
            )
            return {}

    def _calculate_savings_target(
        self, monthly_income: float, goal: str
    ) -> float:
        """Calculate monthly savings target based on goal."""
        try:
            if goal == "zero_based":
                return 0.0
            elif goal == "emergency_fund":
                return monthly_income * 0.20  # 20% of income
            elif goal == "retirement":
                return monthly_income * 0.15  # 15% of income
            elif goal == "goal_based":
                return monthly_income * 0.10  # 10% of income
            elif goal == "debt_paydown":
                return min(monthly_income * 0.20, 1000)  # Up to $1000 or 20% of income
            else:  # balance or default
                return monthly_income * 0.10  # 10% of income

        except Exception as e:
            logger.error(
                "Error calculating savings target",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    def _allocate_budget(
        self,
        monthly_income: float,
        expense_data: Dict[str, Any],
        savings_target: float,
    ) -> Dict[str, float]:
        """Allocate budget to categories based on expenses and income."""
        try:
            # Get current expense percentages
            expense_categories = expense_data.get("category_expenses", {})

            # Calculate total monthly expenses
            total_expenses = sum(
                category_data["monthly_amount"]
                for category_data in expense_categories.values()
            )

            # Allocate based on current spending patterns
            budget_allocation = {}

            if total_expenses > 0 and monthly_income > 0:
                for category, category_data in expense_categories.items():
                    monthly_amount = category_data["monthly_amount"]
                    percentage = (monthly_amount / monthly_income) * 100

                    budget_allocation[category] = {
                        "monthly_amount": monthly_amount,
                        "percentage": percentage,
                    }

            # Add savings category
            savings_percentage = (savings_target / monthly_income) * 100
            budget_allocation["savings"] = {
                "monthly_amount": savings_target,
                "percentage": savings_percentage,
            }

            return budget_allocation

        except Exception as e:
            logger.error(
                "Error allocating budget",
                error=str(e),
                exc_info=True,
            )
            return {}

    async def _generate_budget_recommendations(
        self,
        budget_allocation: Dict[str, Any],
        expense_data: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Generate budget recommendations."""
        recommendations = []

        try:
            # Check for overspending categories
            for category, allocation in budget_allocation.items():
                if category != "savings":
                    # Compare with recommended percentages
                    recommended_percentages = {
                        "food": 10,
                        "transport": 15,
                        "housing": 30,
                        "entertainment": 10,
                        "shopping": 10,
                        "health": 5,
                        "utilities": 5,
                    }

                    recommended = recommended_percentages.get(category, 10)
                    current_percentage = allocation["percentage"]

                    if current_percentage > recommended + 5:
                        recommendations.append(
                            {
                                "type": "reduce_spending",
                                "category": category,
                                "priority": "high",
                                "description": f"You're spending {current_percentage:.1f}% of your income on {category}, which is significantly higher than the recommended {recommended}%",
                                "suggestion": f"Consider reducing {category} expenses by {current_percentage - recommended:.1f}% points",
                            }
                        )

            # Check savings rate
            savings_percentage = budget_allocation.get("savings", {}).get(
                "percentage", 0
            )
            if savings_percentage < 10:
                recommendations.append(
                    {
                        "type": "increase_savings",
                        "priority": "medium",
                        "description": f"Your savings rate is {savings_percentage:.1f}%, below the recommended minimum of 10%",
                        "suggestion": "Increase your savings rate to build a financial safety net and achieve your goals faster",
                    }
                )

            if not recommendations:
                recommendations.append(
                    {
                        "type": "maintain_good_habits",
                        "priority": "low",
                        "description": "Your budget allocation looks good! You're following recommended spending patterns",
                        "suggestion": "Continue monitoring your spending and consider optimizing your savings rate",
                    }
                )

        except Exception as e:
            logger.error(
                "Error generating budget recommendations",
                error=str(e),
                exc_info=True,
            )

        return recommendations

    async def analyze_transaction(
        self, description: str, amount: Optional[float] = None
    ) -> Dict[str, Any]:
        """Analyze a single transaction."""
        try:
            # Categorize transaction
            category = self._categorize_transaction(description, amount)

            # Identify transaction type
            transaction_type = self._identify_transaction_type(
                description, amount
            )

            # Generate insights
            insights = await self._generate_transaction_insights(
                description, category, transaction_type, amount
            )

            return {
                "description": description,
                "amount": amount,
                "category": category,
                "type": transaction_type,
                "insights": insights,
                "confidence": self._calculate_confidence(
                    description, category, transaction_type
                ),
            }

        except Exception as e:
            logger.error(
                "Error analyzing transaction",
                error=str(e),
                exc_info=True,
            )
            raise FinancialError(f"Failed to analyze transaction: {str(e)}")

    def _categorize_transaction(
        self, description: str, amount: Optional[float]
    ) -> str:
        """Categorize a transaction based on description and amount."""
        description_lower = description.lower()

        # Check for specific keywords
        if any(keyword in description_lower for keyword in ["grocery", "food", "meal", "eat"]):
            return "food"
        elif any(keyword in description_lower for keyword in ["transport", "car", "gas", "uber", "taxi", "ride"]):
            return "transport"
        elif any(keyword in description_lower for keyword in ["rent", "mortgage", "housing", "apartment"]):
            return "housing"
        elif any(keyword in description_lower for keyword in ["movie", "cinema", "entertain", "netflix", "show"]):
            return "entertainment"
        elif any(keyword in description_lower for keyword in ["shop", "purchase", "buy", "amazon", "mall"]):
            return "shopping"
        elif any(keyword in description_lower for keyword in ["medical", "doctor", "hospital", "pharmacy", "health"]):
            return "health"
        elif any(keyword in description_lower for keyword in ["save", "saving", "invest", "savings"]):
            return "savings"
        elif amount and amount > 1000:
            return "large_purchase"
        else:
            return "other"

    def _identify_transaction_type(
        self, description: str, amount: Optional[float]
    ) -> str:
        """Identify transaction type (expense, income, transfer)."""
        description_lower = description.lower()

        # Check for income keywords
        income_keywords = ["pay", "salary", "income", "bonus", "refund", "interest"]
        if any(keyword in description_lower for keyword in income_keywords):
            return "income"

        # Check for transfer keywords
        transfer_keywords = ["transfer", "wire", "move", "shift"]
        if any(keyword in description_lower for keyword in transfer_keywords):
            return "transfer"

        # Default to expense
        return "expense"

    async def _generate_transaction_insights(
        self,
        description: str,
        category: str,
        transaction_type: str,
        amount: Optional[float],
    ) -> List[str]:
        """Generate insights for a transaction."""
        insights = []

        try:
            if transaction_type == "expense":
                if amount and amount > 100:
                    insights.append(
                        f"Large expense ({amount:.2f}) in {category} category. Consider reviewing budget allocation."
                    )

                if category == "food" and amount and amount > 50:
                    insights.append(
                        "Food expense is higher than typical. Consider meal planning to reduce costs."
                    )

                if category == "entertainment" and amount and amount > 30:
                    insights.append(
                        "Entertainment expense is above average. Consider free alternatives."
                    )

            elif transaction_type == "income":
                if amount and amount > 5000:
                    insights.append(
                        f"Significant income ({amount:.2f}). Consider increasing savings or investments."
                    )

            # Add category-specific insights
            if category == "shopping" and amount and amount > 200:
                insights.append(
                    "Shopping expense is high this period. Review needs vs wants."
                )

            if not insights:
                insights.append(
                    f"Transaction categorized as {category} {transaction_type}. Track this category for better financial awareness."
                )

        except Exception as e:
            logger.error(
                "Error generating transaction insights",
                error=str(e),
                exc_info=True,
            )

        return insights

    def _calculate_confidence(
        self, description: str, category: str, transaction_type: str
    ) -> float:
        """Calculate confidence level for transaction analysis."""
        confidence = 0.5  # Base confidence

        # Increase confidence based on specific keywords
        if any(
            keyword in description.lower() for keyword in ["grocery", "restaurant", "uber"]
        ):
            confidence += 0.2

        if transaction_type != "other":
            confidence += 0.2

        # Reduce confidence for vague descriptions
        if len(description.split()) < 3:
            confidence -= 0.1

        return max(0.1, min(1.0, confidence))

    async def get_financial_advice(
        self, user_id: str, context: str = "general"
    ) -> Dict[str, Any]:
        """Get personalized financial advice."""
        try:
            # Get user financial profile
            profile = await self.memory_store.get_financial_profile(user_id)

            # Get current financial situation
            portfolio_data = await self.memory_store.get_portfolio_data(
                user_id, "month"
            )
            income_data = await self.memory_store.get_income_data(user_id)
            expense_data = await self.memory_store.get_expense_data(user_id)

            # Generate advice based on context
            advice = {
                "recommendations": [],
                "priority": "low",
                "next_steps": [],
            }

            # Generate advice based on context
            if context == "budgeting":
                advice = await self._generate_budgeting_advice(
                    profile, income_data, expense_data
                )
            elif context == "investing":
                advice = await self._generate_investing_advice(
                    profile, portfolio_data
                )
            elif context == "debt":
                advice = await self._generate_debt_advice(profile, income_data, expense_data)
            else:
                advice = await self._generate_general_advice(profile, portfolio_data, income_data, expense_data)

            return advice

        except Exception as e:
            logger.error(
                "Error generating financial advice",
                error=str(e),
                exc_info=True,
            )
            raise FinancialError(f"Failed to generate financial advice: {str(e)}")

    async def _generate_budgeting_advice(
        self, profile: Any, income_data: Dict[str, Any], expense_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate budgeting-specific advice."""
        recommendations = []
        next_steps = []

        try:
            # Calculate current budget metrics
            monthly_income = self._calculate_monthly_income(income_data)
            monthly_expenses = self._calculate_monthly_expenses(expense_data)
            savings_rate = (
                (monthly_income - monthly_expenses) / monthly_income * 100
                if monthly_income > 0
                else 0
            )

            # Generate recommendations
            if savings_rate < 10:
                recommendations.append(
                    f"Your savings rate is {savings_rate:.1f}%, below the recommended minimum of 10%. Increase savings by setting up automatic transfers."
                )
                next_steps.append("Set up an automatic savings transfer of 10% of your income")

            # Check for overspending categories
            expense_categories = expense_data.get("category_expenses", {})
            for category, category_data in expense_categories.items():
                monthly_amount = category_data["monthly_amount"]
                percentage = (monthly_amount / monthly_income) * 100

                if percentage > 20:
                    recommendations.append(
                        f"You're spending {percentage:.1f}% of your income on {category}. Consider reducing this category."
                    )
                    next_steps.append(f"Create a 20% budget limit for {category} expenses")

            # Default advice if no specific recommendations
            if not recommendations:
                recommendations.append(
                    f"Your budget looks good with a savings rate of {savings_rate:.1f}%. Continue monitoring your expenses and consider automating your savings."
                )
                next_steps.append("Set up automatic savings to maintain your savings rate")

        except Exception as e:
            logger.error(
                "Error generating budgeting advice",
                error=str(e),
                exc_info=True,
            )

        return {
            "recommendations": recommendations,
            "priority": "high" if len(recommendations) > 2 else "medium",
            "next_steps": next_steps,
        }

    async def _generate_investing_advice(
        self, profile: Any, portfolio_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate investing-specific advice."""
        recommendations = []
        next_steps = []

        try:
            # Calculate diversification score
            diversification_score = self._calculate_diversification_score(
                portfolio_data
            )

            if diversification_score < 0.7:
                recommendations.append(
                    f"Your portfolio has a low diversification score of {diversification_score:.2f}. Consider diversifying across different asset classes and sectors."
                )
                next_steps.append("Rebalance your portfolio to achieve better diversification")

            # Get performance metrics
            performance = self._calculate_portfolio_performance(
                portfolio_data, "month"
            )

            if performance.get("sharpe_ratio", 0) < 1.0:
                recommendations.append(
                    f"Your portfolio's risk-adjusted return (Sharpe ratio) is {performance.get('sharpe_ratio', 0):.2f}, below the ideal of 1.0. Consider adjusting your asset allocation."
                )
                next_steps.append("Review and optimize your asset allocation for better risk-adjusted returns")

            # Default advice
            if not recommendations:
                recommendations.append(
                    f"Your investment portfolio shows good diversification and performance. Continue with your current strategy and consider tax-loss harvesting if you have losses."
                )
                next_steps.append("Continue monitoring your investments and consider annual tax-loss harvesting")

        except Exception as e:
            logger.error(
                "Error generating investing advice",
                error=str(e),
                exc_info=True,
            )

        return {
            "recommendations": recommendations,
            "priority": "high" if len(recommendations) > 2 else "medium",
            "next_steps": next_steps,
        }

    async def _generate_debt_advice(
        self, profile: Any, income_data: Dict[str, Any], expense_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate debt-specific advice."""
        recommendations = []
        next_steps = []

        try:
            # Calculate debt-to-income ratio
            monthly_income = self._calculate_monthly_income(income_data)
            monthly_expenses = self._calculate_monthly_expenses(expense_data)

            # Get debt information from profile
            debt_info = getattr(profile, "debt_info", {})
            total_debt = sum(
                debt.get("amount", 0) for debt in debt_info.get("debts", [])
            )
            monthly_debt_payment = sum(
                debt.get("monthly_payment", 0)
                for debt in debt_info.get("debts", [])
            )

            debt_to_income = (
                (monthly_debt_payment / monthly_income * 100)
                if monthly_income > 0
                else 0
            )

            if debt_to_income > 40:
                recommendations.append(
                    f"Your debt-to-income ratio is {debt_to_income:.1f}%, which is above the recommended 40%. Consider debt consolidation or refinancing."
                )
                next_steps.append("Research debt consolidation options to lower your interest rates")

            # Find highest interest debt
            highest_interest_debt = max(
                debt_info.get("debts", []),
                key=lambda d: d.get("interest_rate", 0),
                default=None,
            )

            if highest_interest_debt:
                recommendations.append(
                    f"You have debt with {highest_interest_debt.get('interest_rate', 0):.1f}% interest rate. Consider paying this off first (avalanche method)."
                )
                next_steps.append("Create a debt avalanche payment plan to eliminate high-interest debt")

            # Default advice
            if not recommendations:
                recommendations.append(
                    "You're managing your debt well with a reasonable debt-to-income ratio. Continue making regular payments and consider building an emergency fund."
                )
                next_steps.append("Build an emergency fund to avoid new debt in case of unexpected expenses")

        except Exception as e:
            logger.error(
                "Error generating debt advice",
                error=str(e),
                exc_info=True,
            )

        return {
            "recommendations": recommendations,
            "priority": "high" if len(recommendations) > 2 else "medium",
            "next_steps": next_steps,
        }

    async def _generate_general_advice(
        self,
        profile: Any,
        portfolio_data: Dict[str, Any],
        income_data: Dict[str, Any],
        expense_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Generate general financial advice."""
        recommendations = []
        next_steps = []

        try:
            # Calculate overall financial health score
            health_score = self._calculate_financial_health_score(
                profile, portfolio_data, income_data, expense_data
            )

            if health_score < 70:
                recommendations.append(
                    f"Your overall financial health score is {health_score:.1f}/100. Consider focusing on improving your financial situation."
                )
                next_steps.append("Create a comprehensive financial plan to improve your overall financial health")

            # Check for emergency fund
            if not getattr(profile, "has_emergency_fund", False):
                recommendations.append(
                    "You don't have an emergency fund. Build one with 3-6 months of expenses saved."
                )
                next_steps.append("Start building an emergency fund by setting up automatic savings")

            # Check retirement planning
            if not getattr(profile, "has_retirement_plan", False):
                recommendations.append(
                    "You don't have a retirement plan yet. Start contributing to a retirement account as early as possible."
                )
                next_steps.append("Open a retirement account and set up automatic contributions")

            # Default advice
            if not recommendations:
                recommendations.append(
                    "Your financial situation looks good! Continue with your current financial habits and consider exploring investment opportunities to grow your wealth."
                )
                next_steps.append("Consider exploring investment options to grow your wealth further")

        except Exception as e:
            logger.error(
                "Error generating general advice",
                error=str(e),
                exc_info=True,
            )

        return {
            "recommendations": recommendations,
            "priority": "high" if len(recommendations) > 2 else "medium",
            "next_steps": next_steps,
        }

    def _calculate_financial_health_score(
        self,
        profile: Any,
        portfolio_data: Dict[str, Any],
        income_data: Dict[str, Any],
        expense_data: Dict[str, Any],
    ) -> float:
        """Calculate overall financial health score (0-100)."""
        try:
            score = 0.0

            # Calculate savings rate (0-40 points)
            monthly_income = self._calculate_monthly_income(income_data)
            monthly_expenses = self._calculate_monthly_expenses(expense_data)
            savings_rate = (
                (monthly_income - monthly_expenses) / monthly_income * 100
                if monthly_income > 0
                else 0
            )
            score += min(savings_rate * 0.4, 40)

            # Calculate diversification score (0-20 points)
            diversification_score = self._calculate_diversification_score(
                portfolio_data
            )
            score += diversification_score * 20

            # Check for emergency fund (0-20 points)
            if getattr(profile, "has_emergency_fund", False):
                emergency_fund_months = getattr(profile, "emergency_fund_months", 0)
                score += min(emergency_fund_months * 5, 20)

            # Check for retirement planning (0-20 points)
            if getattr(profile, "has_retirement_plan", False):
                retirement_score = getattr(profile, "retirement_score", 0)
                score += min(retirement_score * 2, 20)

            # Check debt level (0-20 points)
            debt_info = getattr(profile, "debt_info", {})
            total_debt = sum(
                debt.get("amount", 0) for debt in debt_info.get("debts", [])
            )
            debt_to_income = (
                (total_debt / (monthly_income * 10)) * 100
                if monthly_income > 0
                else 0
            )
            score += max(0, 100 - debt_to_income) * 0.2

            return score

        except Exception as e:
            logger.error(
                "Error calculating financial health score",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    async def _calculate_daily_returns(
        self, portfolio_data: Dict[str, Any]
    ) -> List[float]:
        """Calculate daily returns from portfolio data."""
        try:
            # This is a simplified calculation
            # In a real implementation, you would get actual historical price data

            returns = []

            # Get historical price data for each holding
            for holding in portfolio_data.get("holdings", []):
                # This would normally fetch actual price data
                # For now, use a simple random return for demonstration
                daily_return = np.random.normal(0.001, 0.02)  # 0.1% mean, 2% volatility
                returns.append(daily_return)

            return returns

        except Exception as e:
            logger.error(
                "Error calculating daily returns",
                error=str(e),
                exc_info=True,
            )
            return []

    async def generate_strategy(
        self, user_id: str, goal: str, timeframe: str = "long_term"
    ):
        """Generate a financial strategy for the user."""
        try:
            # Get user financial profile
            profile = await self.memory_store.get_financial_profile(user_id)

            # Generate strategy based on goal
            strategy = self._generate_strategy_for_goal(goal, profile, timeframe)

            # Add execution steps
            strategy["execution_steps"] = await self._generate_execution_steps(
                strategy, profile
            )

            return strategy

        except Exception as e:
            logger.error(
                "Error generating strategy",
                error=str(e),
                exc_info=True,
            )
            raise FinancialError(f"Failed to generate strategy: {str(e)}")

    def _generate_strategy_for_goal(
        self, goal: str, profile: Any, timeframe: str
    ) -> Dict[str, Any]:
        """Generate a strategy based on goal."""
        strategies = {
            "retirement": self._generate_retirement_strategy(profile, timeframe),
            "home_purchase": self._generate_home_purchase_strategy(
                profile, timeframe
            ),
            "emergency_fund": self._generate_emergency_fund_strategy(
                profile, timeframe
            ),
            "debt_paydown": self._generate_debt_paydown_strategy(
                profile, timeframe
            ),
            "wealth_building": self._generate_wealth_building_strategy(
                profile, timeframe
            ),
            "income_generation": self._generate_income_generation_strategy(
                profile, timeframe
            ),
        }

        return strategies.get(goal, self._generate_generic_strategy(profile, timeframe))

    def _generate_retirement_strategy(
        self, profile: Any, timeframe: str
    ) -> Dict[str, Any]:
        """Generate retirement strategy."""
        return {
            "goal_type": "retirement",
            "target_amount": self._calculate_retirement_target(profile, timeframe),
            "allocation": {
                "stocks": 0.7,
                "bonds": 0.2,
                "cash": 0.1,
            },
            "contributions": {
                "monthly": self._calculate_monthly_retirement_contribution(profile),
                "annual_increase": 0.03,
            },
            "risk_level": "medium_to_high",
            "timeline_years": self._calculate_retirement_timeline(profile),
        }

    def _generate_home_purchase_strategy(
        self, profile: Any, timeframe: str
    ) -> Dict[str, Any]:
        """Generate home purchase strategy."""
        return {
            "goal_type": "home_purchase",
            "down_payment_target": self._calculate_home_down_payment_target(profile),
            "savings_plan": {
                "monthly_savings": self._calculate_monthly_home_savings(profile),
                "timeline_months": self._calculate_home_purchase_timeline(profile),
            },
            "credit_score_target": 720,
            "debt_to_income_ratio": 0.36,
        }

    def _generate_emergency_fund_strategy(
        self, profile: Any, timeframe: str
    ) -> Dict[str, Any]:
        """Generate emergency fund strategy."""
        return {
            "goal_type": "emergency_fund",
            "target_months": 6,
            "monthly_savings": self._calculate_monthly_emergency_savings(profile),
            "timeline_months": 12,
            "fund_placement": "high_yield_savings",
        }

    def _generate_debt_paydown_strategy(
        self, profile: Any, timeframe: str
    ) -> Dict[str, Any]:
        """Generate debt paydown strategy."""
        debt_info = getattr(profile, "debt_info", {})

        return {
            "goal_type": "debt_paydown",
            "total_debt": sum(
                debt.get("amount", 0) for debt in debt_info.get("debts", [])
            ),
            "strategy": "avalanche",
            "highest_interest_debt": self._get_highest_interest_debt(debt_info),
            "monthly_payment": self._calculate_monthly_debt_payment(profile),
            "timeline_months": self._calculate_debt_paydown_timeline(profile),
        }

    def _generate_wealth_building_strategy(
        self, profile: Any, timeframe: str
    ) -> Dict[str, Any]:
        """Generate wealth building strategy."""
        return {
            "goal_type": "wealth_building",
            "target_amount": self._calculate_wealth_building_target(profile),
            "investment_strategy": {
                "equity_percentage": 0.7,
                "bond_percentage": 0.2,
                "alternative_percentage": 0.1,
            },
            "monthly_contribution": self._calculate_monthly_wealth_contribution(
                profile
            ),
            "expected_return": 0.08,
        }

    def _generate_income_generation_strategy(
        self, profile: Any, timeframe: str
    ) -> Dict[str, Any]:
        """Generate income generation strategy."""
        return {
            "goal_type": "income_generation",
            "target_monthly_income": self._calculate_income_generation_target(profile),
            "strategies": ["dividend_investments", "rental_properties", "business_investments"],
            "initial_investment": self._calculate_initial_income_investment(profile),
        }

    def _generate_generic_strategy(
        self, profile: Any, timeframe: str
    ) -> Dict[str, Any]:
        """Generate generic strategy."""
        return {
            "goal_type": "general",
            "focus_areas": ["budgeting", "savings", "debt_management"],
            "priority": "medium",
        }

    async def _generate_execution_steps(
        self, strategy: Dict[str, Any], profile: Any
    ) -> List[Dict[str, Any]]:
        """Generate execution steps for a strategy."""
        steps = []

        try:
            goal_type = strategy.get("goal_type")

            if goal_type == "retirement":
                steps = await self._generate_retirement_steps(strategy, profile)
            elif goal_type == "home_purchase":
                steps = await self._generate_home_purchase_steps(strategy, profile)
            elif goal_type == "emergency_fund":
                steps = await self._generate_emergency_fund_steps(strategy, profile)
            elif goal_type == "debt_paydown":
                steps = await self._generate_debt_paydown_steps(strategy, profile)
            elif goal_type == "wealth_building":
                steps = await self._generate_wealth_building_steps(strategy, profile)
            elif goal_type == "income_generation":
                steps = await self._generate_income_generation_steps(strategy, profile)

            return steps

        except Exception as e:
            logger.error(
                "Error generating execution steps",
                error=str(e),
                exc_info=True,
            )
            return []

    async def _generate_retirement_steps(
        self, strategy: Dict[str, Any], profile: Any
    ) -> List[Dict[str, Any]]:
        """Generate retirement execution steps."""
        steps = []

        try:
            monthly_contribution = strategy.get("contributions", {}).get("monthly", 0)
            equity_allocation = strategy.get("allocation", {}).get("stocks", 0)

            steps = [
                {
                    "step": 1,
                    "description": f"Set up automatic monthly contribution of ${monthly_contribution:.2f}",
                    "priority": "high",
                    "estimated_time": "immediate",
                    "action": "setup_automatic_transfer",
                },
                {
                    "step": 2,
                    "description": f"Diversify investments with {equity_allocation * 100:.0f}% in equities",
                    "priority": "medium",
                    "estimated_time": "1-2 weeks",
                    "action": "rebalance_portfolio",
                },
                {
                    "step": 3,
                    "description": "Review and adjust contributions annually",
                    "priority": "medium",
                    "estimated_time": "annually",
                    "action": "annual_review",
                },
            ]

        except Exception as e:
            logger.error(
                "Error generating retirement steps",
                error=str(e),
                exc_info=True,
            )

        return steps

    async def _generate_home_purchase_steps(
        self, strategy: Dict[str, Any], profile: Any
    ) -> List[Dict[str, Any]]:
        """Generate home purchase execution steps."""
        steps = []

        try:
            monthly_savings = strategy.get("savings_plan", {}).get("monthly_savings", 0)

            steps = [
                {
                    "step": 1,
                    "description": f"Start monthly savings plan of ${monthly_savings:.2f}",
                    "priority": "high",
                    "estimated_time": "immediate",
                    "action": "setup_savings_plan",
                },
                {
                    "step": 2,
                    "description": "Monitor credit score and work toward 720+",
                    "priority": "medium",
                    "estimated_time": "3-6 months",
                    "action": "credit_improvement",
                },
                {
                    "step": 3,
                    "description": "Save for down payment and pre-approval",
                    "priority": "medium",
                    "estimated_time": "6-12 months",
                    "action": "home_preparation",
                },
            ]

        except Exception as e:
            logger.error(
                "Error generating home purchase steps",
                error=str(e),
                exc_info=True,
            )

        return steps

    async def _generate_emergency_fund_steps(
        self, strategy: Dict[str, Any], profile: Any
    ) -> List[Dict[str, Any]]:
        """Generate emergency fund execution steps."""
        steps = []

        try:
            monthly_savings = strategy.get("monthly_savings", 0)
            timeline_months = strategy.get("timeline_months", 0)

            steps = [
                {
                    "step": 1,
                    "description": f"Set up emergency fund savings of ${monthly_savings:.2f} per month",
                    "priority": "high",
                    "estimated_time": "immediate",
                    "action": "setup_emergency_fund",
                },
                {
                    "step": 2,
                    "description": f"Build fund over {timeline_months} months to reach target",
                    "priority": "medium",
                    "estimated_time": f"{timeline_months} months",
                    "action": "accumulate_fund",
                },
                {
                    "step": 3,
                    "description": "Place funds in high-yield savings account",
                    "priority": "medium",
                    "estimated_time": "1 week",
                    "action": "fund_placement",
                },
            ]

        except Exception as e:
            logger.error(
                "Error generating emergency fund steps",
                error=str(e),
                exc_info=True,
            )

        return steps

    async def _generate_debt_paydown_steps(
        self, strategy: Dict[str, Any], profile: Any
    ) -> List[Dict[str, Any]]:
        """Generate debt paydown execution steps."""
        steps = []

        try:
            monthly_payment = strategy.get("monthly_payment", 0)
            highest_debt = strategy.get("highest_interest_debt", {})

            steps = [
                {
                    "step": 1,
                    "description": f"Implement debt avalanche method with ${monthly_payment:.2f} monthly payment",
                    "priority": "high",
                    "estimated_time": "immediate",
                    "action": "setup_avalanche_plan",
                },
                {
                    "step": 2,
                    "description": f"Prioritize paying off {highest_debt.get('name', 'high-interest debt')}",
                    "priority": "high",
                    "estimated_time": "3-12 months",
                    "action": "prioritize_debt",
                },
                {
                    "step": 3,
                    "description": "Celebrate milestones and adjust strategy as needed",
                    "priority": "medium",
                    "estimated_time": "ongoing",
                    "action": "milestone_tracking",
                },
            ]

        except Exception as e:
            logger.error(
                "Error generating debt paydown steps",
                error=str(e),
                exc_info=True,
            )

        return steps

    async def _generate_wealth_building_steps(
        self, strategy: Dict[str, Any], profile: Any
    ) -> List[Dict[str, Any]]:
        """Generate wealth building execution steps."""
        steps = []

        try:
            monthly_contribution = strategy.get("monthly_contribution", 0)
            expected_return = strategy.get("expected_return", 0.08)

            steps = [
                {
                    "step": 1,
                    "description": f"Set up automatic monthly investment of ${monthly_contribution:.2f}",
                    "priority": "high",
                    "estimated_time": "immediate",
                    "action": "setup_auto_investment",
                },
                {
                    "step": 2,
                    "description": f"Invest in diversified portfolio with expected {expected_return * 100:.1f}% annual return",
                    "priority": "medium",
                    "estimated_time": "1-2 weeks",
                    "action": "portfolio_setup",
                },
                {
                    "step": 3,
                    "description": "Review investment performance and rebalance annually",
                    "priority": "medium",
                    "estimated_time": "annually",
                    "action": "annual_review",
                },
            ]

        except Exception as e:
            logger.error(
                "Error generating wealth building steps",
                error=str(e),
                exc_info=True,
            )

        return steps

    async def _generate_income_generation_steps(
        self, strategy: Dict[str, Any], profile: Any
    ) -> List[Dict[str, Any]]:
        """Generate income generation execution steps."""
        steps = []

        try:
            initial_investment = strategy.get("initial_investment", 0)

            steps = [
                {
                    "step": 1,
                    "description": f"Allocate initial investment of ${initial_investment:.2f}",
                    "priority": "high",
                    "estimated_time": "immediate",
                    "action": "setup_income_investments",
                },
                {
                    "step": 2,
                    "description": "Research and select income-generating investments",
                    "priority": "medium",
                    "estimated_time": "2-4 weeks",
                    "action": "investment_research",
                },
                {
                    "step": 3,
                    "description": "Monitor income streams and reinvest profits",
                    "priority": "medium",
                    "estimated_time": "ongoing",
                    "action": "income_monitoring",
                },
            ]

        except Exception as e:
            logger.error(
                "Error generating income generation steps",
                error=str(e),
                exc_info=True,
            )

        return steps

    # Helper methods for strategy calculations

    def _calculate_retirement_target(self, profile: Any, timeframe: str) -> float:
        """Calculate retirement target amount."""
        try:
            current_age = getattr(profile, "current_age", 30)
            retirement_age = getattr(profile, "retirement_age", 65)
            years_in_retirement = getattr(profile, "years_in_retirement", 30)
            annual_expenses = getattr(profile, "annual_expenses", 50000)

            timeline_years = retirement_age - current_age

            # Simple calculation: need 25x annual expenses for retirement
            target_amount = annual_expenses * 25

            return target_amount

        except Exception as e:
            logger.error(
                "Error calculating retirement target",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    def _calculate_monthly_retirement_contribution(self, profile: Any) -> float:
        """Calculate monthly retirement contribution."""
        try:
            monthly_income = getattr(profile, "monthly_income", 10000)
            current_age = getattr(profile, "current_age", 30)
            retirement_age = getattr(profile, "retirement_age", 65)

            # Rule of thumb: save 15% of pre-tax income
            contribution_rate = 0.15

            # Adjust based on age (lower if closer to retirement)
            years_to_retirement = retirement_age - current_age
            if years_to_retirement < 20:
                contribution_rate += 0.05
            elif years_to_retirement < 30:
                contribution_rate += 0.03

            return monthly_income * contribution_rate

        except Exception as e:
            logger.error(
                "Error calculating monthly retirement contribution",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    def _calculate_retirement_timeline(self, profile: Any) -> int:
        """Calculate retirement timeline in years."""
        try:
            current_age = getattr(profile, "current_age", 30)
            retirement_age = getattr(profile, "retirement_age", 65)

            return retirement_age - current_age

        except Exception as e:
            logger.error(
                "Error calculating retirement timeline",
                error=str(e),
                exc_info=True,
            )
            return 0

    def _calculate_home_down_payment_target(self, profile: Any) -> float:
        """Calculate home down payment target."""
        try:
            current_savings = getattr(profile, "current_savings", 0)
            target_down_payment_percentage = 0.20  # 20% down payment

            # Estimate home price based on income
            monthly_income = getattr(profile, "monthly_income", 10000)
            estimated_home_price = monthly_income * 120  # Assume 10x monthly income

            target_amount = estimated_home_price * target_down_payment_percentage

            # If already have savings, reduce target
            if current_savings > 0:
                target_amount = max(0, target_amount - current_savings)

            return target_amount

        except Exception as e:
            logger.error(
                "Error calculating home down payment target",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    def _calculate_monthly_home_savings(self, profile: Any) -> float:
        """Calculate monthly home savings."""
        try:
            monthly_income = getattr(profile, "monthly_income", 10000)
            current_savings = getattr(profile, "current_savings", 0)
            target_down_payment = self._calculate_home_down_payment_target(profile)

            if target_down_payment <= 0:
                return 0.0

            # Calculate timeline based on timeframe (default 5 years)
            timeline_years = getattr(profile, "home_purchase_timeline_years", 5)
            timeline_months = timeline_years * 12

            monthly_savings = target_down_payment / timeline_months

            # Ensure it's reasonable (not more than 50% of income)
            max_reasonable_savings = monthly_income * 0.5
            return min(monthly_savings, max_reasonable_savings)

        except Exception as e:
            logger.error(
                "Error calculating monthly home savings",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    def _calculate_home_purchase_timeline(self, profile: Any) -> int:
        """Calculate home purchase timeline."""
        try:
            return getattr(profile, "home_purchase_timeline_years", 5) * 12

        except Exception as e:
            logger.error(
                "Error calculating home purchase timeline",
                error=str(e),
                exc_info=True,
            )
            return 60

    def _calculate_monthly_emergency_savings(self, profile: Any) -> float:
        """Calculate monthly emergency fund savings."""
        try:
            monthly_expenses = getattr(profile, "monthly_expenses", 4000)
            target_months = getattr(profile, "emergency_fund_months", 6)

            # Calculate total target
            target_amount = monthly_expenses * target_months

            # Calculate timeline (default 12 months)
            timeline_months = 12

            monthly_savings = target_amount / timeline_months

            return monthly_savings

        except Exception as e:
            logger.error(
                "Error calculating monthly emergency savings",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    def _get_highest_interest_debt(self, debt_info: Dict[str, Any]) -> Dict[str, Any]:
        """Get highest interest debt."""
        try:
            debts = debt_info.get("debts", [])

            if not debts:
                return {}

            return max(debts, key=lambda d: d.get("interest_rate", 0))

        except Exception as e:
            logger.error(
                "Error getting highest interest debt",
                error=str(e),
                exc_info=True,
            )
            return {}

    def _calculate_monthly_debt_payment(self, profile: Any) -> float:
        """Calculate monthly debt payment."""
        try:
            debt_info = getattr(profile, "debt_info", {})
            debts = debt_info.get("debts", [])

            # Calculate minimum payment (1% of balance + interest)
            total_payment = 0.0
            for debt in debts:
                balance = debt.get("balance", 0)
                interest_rate = debt.get("interest_rate", 0)

                minimum_payment = max(balance * 0.01, balance * interest_rate / 12)
                total_payment += minimum_payment

            return total_payment

        except Exception as e:
            logger.error(
                "Error calculating monthly debt payment",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    def _calculate_debt_paydown_timeline(self, profile: Any) -> int:
        """Calculate debt paydown timeline."""
        try:
            debt_info = getattr(profile, "debt_info", {})
            total_debt = sum(
                debt.get("balance", 0) for debt in debt_info.get("debts", [])
            )
            monthly_payment = self._calculate_monthly_debt_payment(profile)

            if monthly_payment <= 0:
                return 0

            timeline_months = total_debt / monthly_payment

            return int(timeline_months)

        except Exception as e:
            logger.error(
                "Error calculating debt paydown timeline",
                error=str(e),
                exc_info=True,
            )
            return 0

    def _calculate_wealth_building_target(self, profile: Any) -> float:
        """Calculate wealth building target."""
        try:
            monthly_income = getattr(profile, "monthly_income", 10000)
            current_age = getattr(profile, "current_age", 30)
            retirement_age = getattr(profile, "retirement_age", 65)

            # Simple target: 10x annual income by retirement
            years_to_retirement = retirement_age - current_age
            target_amount = monthly_income * 12 * 10

            return target_amount

        except Exception as e:
            logger.error(
                "Error calculating wealth building target",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    def _calculate_monthly_wealth_contribution(self, profile: Any) -> float:
        """Calculate monthly wealth building contribution."""
        try:
            monthly_income = getattr(profile, "monthly_income", 10000)

            # Save 20% of income for wealth building
            return monthly_income * 0.20

        except Exception as e:
            logger.error(
                "Error calculating monthly wealth contribution",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    def _calculate_income_generation_target(self, profile: Any) -> float:
        """Calculate income generation target."""
        try:
            monthly_income = getattr(profile, "monthly_income", 10000)

            # Target: replace 50% of current income
            return monthly_income * 0.5

        except Exception as e:
            logger.error(
                "Error calculating income generation target",
                error=str(e),
                exc_info=True,
            )
            return 0.0

    def _calculate_initial_income_investment(self, profile: Any) -> float:
        """Calculate initial income investment."""
        try:
            monthly_income = getattr(profile, "monthly_income", 10000)

            # Need at least $50,000 to generate meaningful passive income
            return max(50000, monthly_income * 12 * 2)

        except Exception as e:
            logger.error(
                "Error calculating initial income investment",
                error=str(e),
                exc_info=True,
            )
            return 50000

    def __del__(self):
        """Destructor to clean up resources."""
        self.llm_cache.clear()
        self.risk_cache.clear()
        self.pattern_cache.clear()
