import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON as SQLAlchemyJSON
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, declarative_base, mapped_column, relationship

Base = declarative_base()


class User(Base):
    """User model."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    preferences: Mapped[dict[str, Any]] = mapped_column(SQLAlchemyJSON, default=dict)

    # Relationships
    financial_profile = relationship("FinancialProfile", back_populates="user")
    conversations = relationship("Conversation", back_populates="user")
    memory_entries = relationship("MemoryEntry", back_populates="user")
    audit_logs = relationship("AuditLog", back_populates="user")


class FinancialProfile(Base):
    """Financial profile model."""

    __tablename__ = "financial_profiles"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), nullable=False
    )
    monthly_income: Mapped[float] = mapped_column(Float, nullable=False)
    current_savings: Mapped[float] = mapped_column(Float, default=0.0)
    risk_tolerance: Mapped[str] = mapped_column(String, default="medium")
    investment_goals: Mapped[list[Any]] = mapped_column(SQLAlchemyJSON, default=list)
    financial_goals: Mapped[list[Any]] = mapped_column(SQLAlchemyJSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    user = relationship("User", back_populates="financial_profile")
    transactions = relationship("Transaction", back_populates="financial_profile")
    investments = relationship("Investment", back_populates="financial_profile")
    budgets = relationship("Budget", back_populates="financial_profile")


class Transaction(Base):
    """Transaction model."""

    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    financial_profile_id: Mapped[str] = mapped_column(
        String, ForeignKey("financial_profiles.id"), nullable=False
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)  # income, expense, transfer
    transaction_date: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        "metadata", SQLAlchemyJSON, default=dict
    )

    # Relationships
    financial_profile = relationship("FinancialProfile", back_populates="transactions")


class Investment(Base):
    """Investment model."""

    __tablename__ = "investments"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    financial_profile_id: Mapped[str] = mapped_column(
        String, ForeignKey("financial_profiles.id"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    purchase_price: Mapped[float] = mapped_column(Float, nullable=False)
    current_price: Mapped[float] = mapped_column(Float, nullable=False)
    purchase_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    current_value: Mapped[float] = mapped_column(Float, nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    financial_profile = relationship("FinancialProfile", back_populates="investments")


class Budget(Base):
    """Budget model."""

    __tablename__ = "budgets"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    financial_profile_id: Mapped[str] = mapped_column(
        String, ForeignKey("financial_profiles.id"), nullable=False
    )
    category: Mapped[str] = mapped_column(String, nullable=False)
    monthly_limit: Mapped[float] = mapped_column(Float, nullable=False)
    current_spend: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    financial_profile = relationship("FinancialProfile", back_populates="budgets")


class Conversation(Base):
    """Conversation model."""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    user = relationship("User", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation")


class Message(Base):
    """Message model."""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    conversation_id: Mapped[str] = mapped_column(
        String, ForeignKey("conversations.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String, nullable=False)  # user, assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        "metadata", SQLAlchemyJSON, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    conversation = relationship("Conversation", back_populates="messages")


class MemoryEntry(Base):
    """Memory entry model."""

    __tablename__ = "memory_entries"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)  # conversation, financial, preference
    content: Mapped[str] = mapped_column(Text, nullable=False)
    extra_data: Mapped[dict[str, Any]] = mapped_column(
        "metadata", SQLAlchemyJSON, default=dict
    )
    embedding: Mapped[dict[str, Any] | None] = mapped_column(
        SQLAlchemyJSON, nullable=True
    )  # For vector search
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    # Relationships
    user = relationship("User", back_populates="memory_entries")


class AuditLog(Base):
    """Audit log model."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    resource: Mapped[str] = mapped_column(String, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(SQLAlchemyJSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="audit_logs")


class ToolUsage(Base):
    """Tool usage log model."""

    __tablename__ = "tool_usage"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), nullable=False)
    tool_name: Mapped[str] = mapped_column(String, nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(SQLAlchemyJSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(SQLAlchemyJSON, default=dict)
    execution_time: Mapped[float] = mapped_column(
        Float, nullable=False
    )  # in seconds
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)