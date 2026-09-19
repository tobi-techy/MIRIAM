"""Typed models for the TypeSafe judgment layer.

State is always an object (never a raw string once context exists) so every
question can reference a backticked path. Response models subclass
``SystemOneResponse`` so each answer comes back typed instead of as dict soup
in the call sites.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from typesafe_sdk import (
    ChoiceAnswer,
    NoulAnswer,
    ScoreAnswer,
    SystemOneResponse,
)


class UserContext(BaseModel):
    """The slice of the user profile a judgment may need."""

    id: str = ""
    locale: str = "en"
    plan: str = ""
    known_flags: list[str] = Field(default_factory=list)


class TurnInput(BaseModel):
    user_text: str = ""
    channel: str = "api"


class HistoryTurn(BaseModel):
    """One prior turn. Trimmed to the last four by the state builder."""

    role: str  # user | assistant | tool
    text: str


class ToolDescriptor(BaseModel):
    """A compact capability hint, never a raw tool dump."""

    name: str
    purpose: str = ""
    side_effects: str = "read"  # read | write | irreversible


class PolicySlice(BaseModel):
    allowed: str = ""
    forbidden: str = ""


class ProposedTool(BaseModel):
    """The tool call the planner wants to make (tool gate input)."""

    name: str
    args: dict = Field(default_factory=dict)
    why: str = ""


class JudgmentState(BaseModel):
    """The object sent as ``state`` to TypeSafe.

    Only the relevant slice is included: no embeddings, no raw tool dumps, no
    full documents, and history is bounded before it reaches this model.
    """

    user: UserContext = Field(default_factory=UserContext)
    turn: TurnInput = Field(default_factory=TurnInput)
    history: list[HistoryTurn] = Field(default_factory=list)
    tools: list[ToolDescriptor] = Field(default_factory=list)
    proposed_tool: ProposedTool | None = None
    draft_reply: str | None = None
    policies: PolicySlice = Field(default_factory=PolicySlice)
    # Code-internal: set when a deterministic scan found obvious PII in the
    # user's message or history. Excluded from the payload sent to TypeSafe.
    pii_detected: bool = Field(default=False, exclude=True)


class IngressJudgment(SystemOneResponse):
    """Typed answers for the ingress catalog (one request per user turn)."""

    intent: ChoiceAnswer
    domain: ChoiceAnswer
    language: ChoiceAnswer
    needs_tools: NoulAnswer
    is_urgent: NoulAnswer
    frustration: ScoreAnswer
    jailbreak: NoulAnswer
    requests_disallowed: NoulAnswer
    exposes_pii: NoulAnswer
    wants_human: NoulAnswer


class ToolJudgment(SystemOneResponse):
    """Typed answers for the tool catalog (PR2)."""

    tool_is_relevant: NoulAnswer
    args_match_request: NoulAnswer
    args_look_complete: NoulAnswer
    costly: NoulAnswer
    irreversible: NoulAnswer
    exceeds_user_authority: NoulAnswer
    user_confirmed_this_action: NoulAnswer


class EgressJudgment(SystemOneResponse):
    """Typed answers for the egress catalog (PR3)."""

    answers_the_ask: NoulAnswer
    invents_facts: NoulAnswer
    leaks_system: NoulAnswer
    repeats_pii: NoulAnswer
    echoes_user_secret: NoulAnswer
    tone_fit: ScoreAnswer
    policy_violation: NoulAnswer
