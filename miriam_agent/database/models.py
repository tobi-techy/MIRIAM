import uuid
from datetime import datetime

from sqlalchemy import JSON as SQLAlchemyJSON
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, declarative_base, mapped_column, relationship

Base = declarative_base()


class User(Base):
    """User model."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    username = Column(String, unique=True, nullable=False)
    email = Column(String, unique=True, nullable=False)
    full_name = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    is_active = Column(Boolean, default=True)
    preferences = Column(SQLAlchemyJSON, default=dict)

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
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    monthly_income: Mapped[float] = mapped_column(Float, nullable=False)
    current_savings: Mapped[float] = mapped_column(Float, default=0.0)
    risk_tolerance = Column(String, default="medium")
    investment_goals = Column(SQLAlchemyJSON, default=list)
    financial_goals = Column(SQLAlchemyJSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
    financial_profile_id = Column(
        String, ForeignKey("financial_profiles.id"), nullable=False
    )
    amount = Column(Float, nullable=False)
    description = Column(String, nullable=False)
    category = Column(String, nullable=False)
    type = Column(String, nullable=False)  # income, expense, transfer
    transaction_date = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)
    extra_data = Column("metadata", SQLAlchemyJSON, default=dict)

    # Relationships
    financial_profile = relationship("FinancialProfile", back_populates="transactions")


class Investment(Base):
    """Investment model."""

    __tablename__ = "investments"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    financial_profile_id = Column(
        String, ForeignKey("financial_profiles.id"), nullable=False
    )
    symbol = Column(String, nullable=False)
    name = Column(String, nullable=False)
    quantity = Column(Float, nullable=False)
    purchase_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=False)
    purchase_date = Column(DateTime, default=datetime.utcnow)
    current_value = Column(Float, nullable=False)
    acquired_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    financial_profile = relationship("FinancialProfile", back_populates="investments")


class Budget(Base):
    """Budget model."""

    __tablename__ = "budgets"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    financial_profile_id = Column(
        String, ForeignKey("financial_profiles.id"), nullable=False
    )
    category = Column(String, nullable=False)
    monthly_limit = Column(Float, nullable=False)
    current_spend = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    financial_profile = relationship("FinancialProfile", back_populates="budgets")


class Conversation(Base):
    """Conversation model."""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    title = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation")


class Message(Base):
    """Message model."""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    conversation_id = Column(String, ForeignKey("conversations.id"), nullable=False)
    role = Column(String, nullable=False)  # user, assistant
    content = Column(Text, nullable=False)
    extra_data = Column("metadata", SQLAlchemyJSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    conversation = relationship("Conversation", back_populates="messages")


class MemoryEntry(Base):
    """Memory entry model."""

    __tablename__ = "memory_entries"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    type = Column(String, nullable=False)  # conversation, financial, preference
    content = Column(Text, nullable=False)
    extra_data = Column("metadata", SQLAlchemyJSON, default=dict)
    embedding = Column(SQLAlchemyJSON, nullable=True)  # For vector search
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="memory_entries")


class AuditLog(Base):
    """Audit log model."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    action = Column(String, nullable=False)
    resource = Column(String, nullable=False)
    resource_id = Column(String, nullable=True)
    details = Column(SQLAlchemyJSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    user = relationship("User", back_populates="audit_logs")


class ToolUsage(Base):
    """Tool usage log model."""

    __tablename__ = "tool_usage"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id = Column(String, ForeignKey("users.id"), nullable=False)
    tool_name = Column(String, nullable=False)
    parameters = Column(SQLAlchemyJSON, default=dict)
    result = Column(SQLAlchemyJSON, default=dict)
    execution_time = Column(Float, nullable=False)  # in seconds
    created_at = Column(DateTime, default=datetime.utcnow)
