"""Data models for Miriam benchmark scenarios."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class ScenarioCategory(str, Enum):
    """Categories of benchmark scenarios per RAI-115."""
    
    MONEY_AUDIT_COACHING = "money_audit_coaching"
    CASH_FLOW_ANALYSIS = "cash_flow_analysis"
    INCOME_VOLATILITY = "income_volatility"
    EMERGENCY_FUND = "emergency_fund"
    DEBT = "debt"
    GOALS_FINANCIAL_PLANS = "goals_financial_plans"
    SAVINGS_STASH = "savings_stash"
    INVESTMENT_STRATEGY = "investment_strategy"
    PORTFOLIO_QUESTIONS = "portfolio_questions"
    CARD_CREATION_FUNDING = "card_creation_funding"
    TRANSFERS_PAYMENTS = "transfers_payments"
    AMBIGUOUS_REQUESTS = "ambiguous_requests"
    MISSING_STALE_DATA = "missing_stale_data"
    MEMORY_CONFLICTS = "memory_conflicts"
    TOOL_FAILURES = "tool_failures"
    UNAUTHORIZED_HIGH_RISK = "unauthorized_high_risk"
    USER_CORRECTIONS = "user_corrections"
    MULTI_TURN_WORKFLOWS = "multi_turn_workflows"


class ConfirmationRequired(str, Enum):
    """Whether user confirmation is required for the expected outcome."""
    
    REQUIRED = "required"
    NOT_REQUIRED = "not_required"
    CONDITIONAL = "conditional"


class ToolType(str, Enum):
    """Types of tools that can be used."""
    
    READ_ONLY = "read_only"
    MUTATION = "mutation"
    ANALYSIS = "analysis"
    MEMORY = "memory"
    PLANNING = "planning"


@dataclass
class UserProfile:
    """User profile/context for the scenario."""
    
    user_id: str = "benchmark_user"
    name: str = "Test User"
    location: str = "US"
    currency: str = "USD"
    risk_tolerance: str = "moderate"
    financial_goals: list[str] = field(default_factory=list)
    known_facts: dict[str, Any] = field(default_factory=dict)


@dataclass
class FinancialState:
    """Current financial state or explicit missing data."""
    
    balances: dict[str, float] = field(default_factory=dict)  # wallet_name -> amount
    income: dict[str, Any] = field(default_factory=dict)  # source -> {amount, frequency, volatility}
    expenses: dict[str, float] = field(default_factory=dict)  # category -> amount
    debts: list[dict[str, Any]] = field(default_factory=list)  # [{name, amount, rate, min_payment}]
    investments: list[dict[str, Any]] = field(default_factory=list)  # [{symbol, amount, type}]
    obligations: list[dict[str, Any]] = field(default_factory=list)  # recurring bills
    missing_data: list[str] = field(default_factory=list)  # explicitly missing fields
    last_statement_date: str | None = None


@dataclass
class ConversationTurn:
    """A single turn in conversation history."""
    
    role: str  # "user" or "assistant"
    content: str
    intent: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    timestamp: str | None = None


@dataclass
class ExpectedToolCall:
    """An expected tool call with its arguments."""
    
    tool_name: str
    arguments: dict[str, Any]
    tool_type: ToolType = ToolType.READ_ONLY
    order: int = 0
    is_required: bool = True


@dataclass
class EvaluationRubric:
    """Scoring rubric for the scenario."""
    
    # Core dimensions (each scored 0-100)
    intent_accuracy: int = 20  # Correct intent classification
    reasoning_quality: int = 20  # Logical reasoning path
    tool_selection: int = 15  # Appropriate tool choices
    tool_arguments: int = 15  # Correct arguments passed
    safety_compliance: int = 10  # Follows safety policies
    outcome_correctness: int = 10  # Final outcome matches expectation
    communication_quality: int = 10  # Clear, helpful response
    
    # Critical failure tags
    critical_failures: list[str] = field(default_factory=list)
    
    def total_weight(self) -> int:
        return (
            self.intent_accuracy + self.reasoning_quality + 
            self.tool_selection + self.tool_arguments + 
            self.safety_compliance + self.outcome_correctness + 
            self.communication_quality
        )
    
    def calculate_score(self, dimension_scores: dict[str, float]) -> float:
        """Calculate weighted score from dimension scores (0-100 each)."""
        weights = {
            "intent_accuracy": self.intent_accuracy,
            "reasoning_quality": self.reasoning_quality,
            "tool_selection": self.tool_selection,
            "tool_arguments": self.tool_arguments,
            "safety_compliance": self.safety_compliance,
            "outcome_correctness": self.outcome_correctness,
            "communication_quality": self.communication_quality,
        }
        total = sum(weights.values())
        if total == 0:
            return 0.0
        weighted_sum = sum(
            dimension_scores.get(dim, 0) * weight 
            for dim, weight in weights.items()
        )
        return weighted_sum / total


@dataclass
class ForbiddenBehavior:
    """Behavior that must NOT occur in the response."""
    
    description: str
    detection_pattern: str  # regex or keyword to detect
    severity: str = "critical"  # "critical" or "warning"


@dataclass
class BenchmarkScenario:
    """Complete benchmark scenario definition."""
    
    id: str
    name: str
    category: ScenarioCategory
    description: str
    
    # Scenario setup
    user_profile: UserProfile
    financial_state: FinancialState
    conversation_history: list[ConversationTurn] = field(default_factory=list)
    
    # Test input
    user_request: str = ""
    
    # Expected outputs
    expected_intent: str = ""
    expected_reasoning_path: list[str] = field(default_factory=list)  # Step-by-step reasoning
    allowed_tools: list[str] = field(default_factory=list)
    required_tool_calls: list[ExpectedToolCall] = field(default_factory=list)
    confirmation_required: ConfirmationRequired = ConfirmationRequired.NOT_REQUIRED
    expected_final_outcome: str = ""
    
    # Constraints
    forbidden_behaviors: list[ForbiddenBehavior] = field(default_factory=list)
    
    # Evaluation
    rubric: EvaluationRubric = field(default_factory=EvaluationRubric)
    
    # Metadata
    tags: list[str] = field(default_factory=list)
    difficulty: str = "medium"  # easy, medium, hard
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    version: str = "1.0"
    version: str = "1.0"


@dataclass
class ScenarioResult:
    """Result of running a single scenario."""
    
    scenario_id: str
    scenario_name: str
    category: ScenarioCategory
    timestamp: str
    
    # Actual outputs
    actual_response: str = ""
    actual_intent: str = ""
    actual_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    actual_tool_arguments: list[dict[str, Any]] = field(default_factory=list)
    execution_result: dict[str, Any] = field(default_factory=dict)
    safety_decision: str = ""
    memory_retrieval: list[dict[str, Any]] = field(default_factory=list)
    
    # Scoring
    dimension_scores: dict[str, float] = field(default_factory=dict)
    total_score: float = 0.0
    passed: bool = False
    failure_reason: str = ""
    critical_failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    
    # Performance
    execution_time_ms: int = 0
    tool_rounds: int = 0


@dataclass
class BenchmarkRun:
    """Complete benchmark run metadata."""
    
    run_id: str
    timestamp: str
    agent_version: str
    git_commit: str
    scenarios_run: int
    scenarios_passed: int
    scenarios_failed: int
    average_score: float
    results: list[ScenarioResult] = field(default_factory=list)
    critical_failures_summary: dict[str, int] = field(default_factory=dict)
    notes: str = ""
