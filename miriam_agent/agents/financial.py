import json
from typing import Any, Dict, List, Optional

from miriam_agent.agents.base import BaseAgent, AgentConfig
from miriam_agent.core.exceptions import AgentError, FinancialError
from miriam_agent.core.models import FinancialGoal, Transaction
from miriam_agent.database.models import FinancialProfile
from miriam_agent.financial.intelligence import FinancialIntelligence
from miriam_agent.integrations.grpc_client import GrpcPaymentClient
from miriam_agent.safety.policy import SafetyPolicy

class FinancialAgent(BaseAgent):
    """Financial agent that handles financial conversations and actions."""

    def __init__(
        self,
        config: AgentConfig,
        safety_policy: SafetyPolicy,
        memory_store: Any,
        financial_profile: FinancialProfile,
        financial_intelligence: FinancialIntelligence,
        payment_client: GrpcPaymentClient,
    ):
        super().__init__(config, safety_policy, memory_store, financial_profile)
        self.financial_intelligence = financial_intelligence
        self.payment_client = payment_client

    async def _plan_response(self, message: str) -> Dict[str, Any]:
        """Plan response using financial intelligence."""
        try:
            # Analyze the message to understand intent
            intent = await self.financial_intelligence.analyze_intent(
                message, self.financial_profile
            )

            # Determine if this requires tool use
            tool_calls = await self._determine_tools_needed(
                intent, message
            )

            # Generate reasoning for the plan
            reasoning = await self._generate_reasoning(
                intent, tool_calls, message
            )

            return {
                "intent": intent,
                "tool_calls": tool_calls,
                "reasoning": reasoning,
                "requires_approval": self._requires_approval(
                    intent, tool_calls
                ),
            }

        except Exception as e:
            self.logger.error(
                "Error planning response",
                error=str(e),
                exc_info=True,
            )
            raise AgentError(f"Failed to plan response: {str(e)}")

    async def _determine_tools_needed(
        self, intent: Dict[str, Any], message: str
    ) -> List[Dict[str, Any]]:
        """Determine which tools are needed to fulfill the intent."""
        tool_calls = []

        # Map intents to tools
        intent_type = intent.get("type", "general")

        if intent_type == "analysis":
            tool_calls.append({
                "name": "analyze_portfolio",
                "arguments": {
                    "user_id": self.state.user_id,
                    "period": intent.get("timeframe", "month"),
                },
            })

        elif intent_type == "planning":
            tool_calls.append({
                "name": "generate_budget_plan",
                "arguments": {
                    "user_id": self.state.user_id,
                    "goal": intent.get("goal", "balance"),
                },
            })

        elif intent_type == "transaction":
            tool_calls.append({
                "name": "analyze_transaction",
                "arguments": {
                    "description": message,
                    "amount": self._extract_amount(message),
                },
            })

        elif intent_type == "advice":
            tool_calls.append({
                "name": "get_financial_advice",
                "arguments": {
                    "user_id": self.state.user_id,
                    "context": intent.get("context", "general"),
                },
            })

        return tool_calls

    async def _generate_reasoning(
        self,
        intent: Dict[str, Any],
        tool_calls: List[Dict[str, Any]],
        message: str,
    ) -> str:
        """Generate reasoning for the planned actions."""
        if not tool_calls:
            return f"I'll help you with: {intent.get('type', 'general')} analysis based on your financial situation."

        reasoning = f"I need to analyze your request and gather relevant financial data. "
        reasoning += f"The intent appears to be: {intent.get('description', 'general')}. "

        if len(tool_calls) == 1:
            tool = tool_calls[0]
            reasoning += f"I'll use the {tool['name']} tool to provide you with insights."
        else:
            reasoning += f"I'll use multiple tools: {', '.join([t['name'] for t in tool_calls])}."

        return reasoning

    def _requires_approval(
        self, intent: Dict[str, Any], tool_calls: List[Dict[str, Any]]
    ) -> bool:
        """Determine if user approval is required for the planned actions."""
        # Check if intent type requires approval
        if intent.get("risk_level", "low") in ["high", "critical"]:
            return True

        # Check if tool calls involve money movement
        money_movement_tools = [
            "transfer_funds",
            "withdraw_funds",
            "deposit_funds",
            "execute_strategy",
        ]

        for tool_call in tool_calls:
            if tool_call["name"] in money_movement_tools:
                return True

        return False

    def _extract_amount(self, message: str) -> Optional[float]:
        """Extract amount from message if present."""
        import re

        amount_pattern = r"\$?\s*(\d+(?:\.\d{2})?)"
        match = re.search(amount_pattern, message)

        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass

        return None

    async def _execute_action_plan(
        self, action_plan: Dict[str, Any]
    ) -> List[Any]:
        """Execute the planned actions."""
        tool_calls = action_plan.get("tool_calls", [])
        tool_results = []

        for tool_call in tool_calls:
            try:
                # Validate safety first
                is_safe = await self._validate_safety(tool_call)
                if not is_safe:
                    self.logger.warning(
                        "Action failed safety validation",
                        tool_name=tool_call["name"],
                        arguments=tool_call["arguments"],
                    )
                    tool_results.append(
                        {
                            "tool_name": tool_call["name"],
                            "error": "Action failed safety validation",
                            "type": "error",
                        }
                    )
                    continue

                # Check if approval is required
                if action_plan.get("requires_approval", False):
                    self.logger.info(
                        "Action requires user approval",
                        tool_name=tool_call["name"],
                        arguments=tool_call["arguments"],
                    )
                    # In a real implementation, we would wait for user approval here
                    # For now, we'll simulate approval for testing
                    tool_result = await self._execute_tool_safely(
                        tool_call["name"], tool_call["arguments"]
                    )
                    if not tool_result.get("error"):
                        tool_result["approval_required"] = True
                else:
                    tool_result = await self._execute_tool_safely(
                        tool_call["name"], tool_call["arguments"]
                    )

                tool_results.append(tool_result)

            except Exception as e:
                self.logger.error(
                    "Error executing tool",
                    tool_name=tool_call["name"],
                    error=str(e),
                    exc_info=True,
                )
                tool_results.append(
                    {
                        "tool_name": tool_call["name"],
                        "error": str(e),
                        "type": "error",
                    }
                )

        return tool_results

    async def _execute_tool_safely(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute a tool safely with error handling."""
        try:
            # Route to appropriate tool execution method
            if tool_name == "analyze_portfolio":
                result = await self.financial_intelligence.analyze_portfolio(
                    arguments["user_id"], arguments.get("period", "month")
                )

            elif tool_name == "generate_budget_plan":
                result = await self.financial_intelligence.generate_budget_plan(
                    arguments["user_id"], arguments.get("goal", "balance")
                )

            elif tool_name == "analyze_transaction":
                result = await self.financial_intelligence.analyze_transaction(
                    arguments["description"], arguments.get("amount")
                )

            elif tool_name == "get_financial_advice":
                result = await self.financial_intelligence.get_financial_advice(
                    arguments["user_id"], arguments.get("context", "general")
                )

            else:
                # Check if it's a payment-related tool
                if tool_name in [
                    "transfer_funds",
                    "withdraw_funds",
                    "deposit_funds",
                    "execute_strategy",
                ]:
                    result = await self.payment_client.execute_payment_action(
                        tool_name, arguments
                    )
                else:
                    raise FinancialError(f"Unknown tool: {tool_name}")

            return {"result": result, "tool_name": tool_name, "type": "success"}

        except Exception as e:
            self.logger.error(
                "Tool execution failed",
                tool_name=tool_name,
                error=str(e),
                exc_info=True,
            )
            return {"error": str(e), "tool_name": tool_name, "type": "error"}

    async def _generate_response(
        self,
        message: str,
        action_plan: Dict[str, Any],
        tool_results: List[Dict[str, Any]],
    ) -> str:
        """Generate final response based on action results."""
        tool_calls = action_plan.get("tool_calls", [])

        if not tool_calls:
            return "I understand you want to discuss your finances. Could you provide more specific details about what you'd like to analyze or plan for?"

        # Analyze tool results
        successful_results = [
            r for r in tool_results if r.get("type") == "success"
        ]
        error_results = [r for r in tool_results if r.get("type") == "error"]

        response = "Based on my analysis, here are my insights:\n\n"

        if successful_results:
            for result in successful_results:
                tool_name = result["tool_name"]

                if tool_name == "analyze_portfolio":
                    response += self._format_portfolio_analysis(result["result"])

                elif tool_name == "generate_budget_plan":
                    response += self._format_budget_plan(result["result"])

                elif tool_name == "analyze_transaction":
                    response += self._format_transaction_analysis(result["result"])

                elif tool_name == "get_financial_advice":
                    response += self._format_financial_advice(result["result"])

                elif "approval_required" in result:
                    response += f"\n⚠️ **Action Requires Your Approval**: I recommend proceeding with the {tool_name.replace('_', ' ')} as requested, but this action will need your explicit approval before execution.\n"

        if error_results:
            response += "\n⚠️ I encountered some issues:\n"
            for error_result in error_results:
                response += f"- {error_result['error']}\n"

        # Add proactive suggestions
        response += "\n---\nWould you like me to:\n"
        response += "- Create a more detailed financial plan\n"
        response += "- Set up automated monitoring for your accounts\n"
        response += "- Help you understand your current spending patterns\n"
        response += "- Review your investment strategy\n"

        return response

    def _format_portfolio_analysis(self, analysis: Dict[str, Any]) -> str:
        """Format portfolio analysis for display."""
        response = "**Portfolio Analysis:**\n"

        if "holdings" in analysis:
            response += f"- Total holdings: {len(analysis['holdings'])} positions\n"

        if "performance" in analysis:
            perf = analysis["performance"]
            response += f"- Period performance: {perf.get('total_return', 'N/A')}%\n"

        response += "\n"
        return response

    def _format_budget_plan(self, plan: Dict[str, Any]) -> str:
        """Format budget plan for display."""
        response = "**Budget Plan:**\n"

        if "categories" in plan:
            for category, allocation in plan["categories"].items():
                response += f"- {category}: {allocation}%\n"

        if "monthly_target" in plan:
            response += f"- Monthly target: ${plan['monthly_target']}\n"

        response += "\n"
        return response

    def _format_transaction_analysis(self, analysis: Dict[str, Any]) -> str:
        """Format transaction analysis for display."""
        response = "**Transaction Analysis:**\n"

        if "category" in analysis:
            response += f"- Category: {analysis['category']}\n"

        if "amount" in analysis:
            response += f"- Amount: ${analysis['amount']}\n"

        if "confidence" in analysis:
            response += f"- Confidence: {analysis['confidence'] * 100:.1f}%\n"

        response += "\n"
        return response

    def _format_financial_advice(self, advice: Dict[str, Any]) -> str:
        """Format financial advice for display."""
        response = "**Financial Advice:**\n"

        if "recommendations" in advice:
            for i, recommendation in enumerate(
                advice["recommendations"], 1
            ):
                response += f"{i}. {recommendation}\n"

        if "priority" in advice:
            response += f"\n**Priority:** {advice['priority']}\n"

        response += "\n"
        return response
