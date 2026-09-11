"""Core data models for Miriam Financial Agent."""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class TransactionType(str, Enum):
    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"


class IntentType(str, Enum):
    ANALYSIS = "analysis"
    PLANNING = "planning"
    TRANSACTION = "transaction"
    ADVICE = "advice"
    QUESTION = "question"
    GREETING = "greeting"


class FinancialGoal(BaseModel):
    id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:8])
    name: str
    target_amount: float
    current_amount: float = 0.0
    deadline: Optional[datetime] = None
    priority: str = "medium"
    status: str = "active"


class InvestmentStrategy(BaseModel):
    id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:8])
    name: str
    strategy_type: str
    risk_level: RiskLevel = RiskLevel.MEDIUM
    allocation: Dict[str, float] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    active: bool = True


class PaymentRequest(BaseModel):
    action: str  # transfer_funds, withdraw_funds, deposit_funds
    amount: float
    recipient_id: Optional[str] = None
    source_id: Optional[str] = None
    currency: str = "USD"
    description: Optional[str] = None
    idempotency_key: Optional[str] = None


class PaymentResult(BaseModel):
    success: bool
    transaction_id: Optional[str] = None
    amount: float = 0.0
    fee: float = 0.0
    status: str = "pending"
    message: Optional[str] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class UserProfile(BaseModel):
    user_id: str
    name: str
    email: str
    risk_tolerance: RiskLevel = RiskLevel.MEDIUM
    monthly_income: float = 0.0
    current_savings: float = 0.0
    financial_goals: List[FinancialGoal] = Field(default_factory=list)
    preferences: Dict[str, Any] = Field(default_factory=dict)
