"""Pydantic v2 models for Go document contract v1.

Mirrors ``internal/domain/services/document/contract.go`` (APIResult).
Money stays a string on the wire (shopspring/decimal) and is validated
with ``Decimal`` here — never float. Unknown major versions are rejected
by the validator so future Go changes fail loudly, not silently.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

SUPPORTED_MAJOR_VERSION = 1
CONTRACT_VERSION = "1.0"

DocumentStatus = Literal["processing", "completed", "failed"]


class DocumentCheck(BaseModel):
    name: str
    passed: bool
    message: str = ""


class DocumentEvidence(BaseModel):
    field: str
    page: int | None = None
    region: list[float] = Field(default_factory=list)
    engine: str = ""
    confidence: float = 0.0


class DocumentValidation(BaseModel):
    status: str
    reconciled: bool = False
    difference: str = "0.00"
    checks: list[DocumentCheck] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @field_validator("difference")
    @classmethod
    def _difference_is_decimal(cls, v: str) -> str:
        try:
            Decimal(v)
        except (InvalidOperation, ValueError, TypeError) as e:
            raise ValueError(f"difference must be a decimal string, got {v!r}") from e
        return v


class DocumentData(BaseModel):
    merchant: str | None = None
    amount: str | None = None
    currency: str | None = None
    document_date: str | None = None
    account_name: str | None = None
    opening_balance: str | None = None
    closing_balance: str | None = None
    raw: dict[str, Any] | None = None

    @field_validator("amount", "opening_balance", "closing_balance", mode="before")
    @classmethod
    def _money_is_decimal_string(cls, v: Any) -> str | None:
        if v is None:
            return None
        # Go serializes shopspring/decimal as JSON strings, but coerce ints
        # and floats defensively rather than rejecting a valid money value.
        if isinstance(v, bool):
            raise ValueError(f"money field must be a decimal string, got {v!r}")
        try:
            Decimal(str(v))
        except (InvalidOperation, ValueError, TypeError) as e:
            raise ValueError(f"money field must be a decimal string, got {v!r}") from e
        return str(v)


class DocumentResult(BaseModel):
    schema_version: str
    document_id: str
    document_type: str
    status: str
    confidence: float = 0.0
    data: DocumentData = Field(default_factory=DocumentData)
    validation: DocumentValidation | None = None
    evidence: list[DocumentEvidence] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _supported_major(cls, v: str) -> str:
        major = v.split(".")[0] if v else ""
        if major != str(SUPPORTED_MAJOR_VERSION):
            raise ValueError(
                f"unsupported document contract version {v!r}; "
                f"this client supports major version {SUPPORTED_MAJOR_VERSION}"
            )
        return v

    @field_validator("confidence")
    @classmethod
    def _confidence_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"confidence {v!r} out of range [0,1]")
        return v

    @model_validator(mode="after")
    def _required_identity(self) -> DocumentResult:
        if not self.document_id:
            raise ValueError("document_id is required")
        if not self.document_type:
            raise ValueError("document_type is required")
        return self
