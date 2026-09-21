"""Shared fakes for the three-layer money tests.

Everything here is hermetic: no network, no JEV, no provider, no database. The
judge is injected as a callable returning typed answers, the model is a stub
that records the tools it was offered, and the ledger is built directly.

Mirroring the rest of ``tests/``, the fakes are hand-rolled classes rather than
mocks so a test can assert on what actually happened rather than on what it
expected to be called.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from typesafe_sdk import ChoiceAnswer, NoulAnswer, ScoreAnswer

from miriam_agent.agents.llm import LLMResponse
from miriam_agent.hands.ledger import Ledger, RentFirst, Track, money
from miriam_agent.hands.limits import Policy
from miriam_agent.judgment.schema import MoneyJudgment

# A deterministic policy: 2,000 may be acted on, 100,000 is the absolute ceiling.
POLICY = Policy(
    max_auto=money(2000),
    max_with_confirm=money(100000),
    reversible_under=money(2000),
)

TRACK_70_30 = Track(name="70/30", spend=Decimal("70"), save=Decimal("30"))


def choice(label: str, confidence: float = 0.9) -> ChoiceAnswer:
    return ChoiceAnswer.model_construct(choice=label, confidence=confidence)


def noul(value: float) -> NoulAnswer:
    return NoulAnswer.model_construct(noul=value)


def score(value: float) -> ScoreAnswer:
    return ScoreAnswer.model_construct(score=value)


def jev(
    *,
    inflow: str = "salary",
    intent: str = "order",
    afford: float = 0.9,
    violation: float = 0.0,
    reversible: float = 0.9,
    mode: str = "act",
    action: str = "allow",
    conf: float = 0.9,
) -> MoneyJudgment:
    """One complete set of JEV answers, with sane defaults for everything else."""
    return MoneyJudgment.model_construct(
        inflow_class=choice(inflow, conf),
        intent_type=choice(intent, conf),
        affordability=score(afford),
        policy_violation=noul(violation),
        reversibility=noul(reversible),
        next_mode=choice(mode, conf),
        action_choice=choice(action, conf),
    )


def judge_of(judgment: MoneyJudgment | None):
    """A judge callable that always returns ``judgment``."""

    async def _judge(state, utterance):  # noqa: ANN001, ANN202
        return judgment

    return _judge


def broken_judge(exc: Exception | None = None):
    """A judge that always fails, for the fail-closed paths."""

    async def _judge(state, utterance):  # noqa: ANN001, ANN202
        raise exc or RuntimeError("JEV timed out")

    return _judge


class FakeProvider:
    """A model that returns fixed text and records what it was offered."""

    def __init__(self, content: str = "Noted.", *, fail: bool = False) -> None:
        self.content = content
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def complete(
        self,
        messages,  # noqa: ANN001
        tools=None,  # noqa: ANN001
        temperature=None,  # noqa: ANN001
        max_tokens=None,  # noqa: ANN001
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self.fail:
            raise RuntimeError("the model is dead")
        return LLMResponse(
            content=self.content,
            tool_calls=[],
            model="fake-model",
            usage={},
            finish_reason="stop",
        )

    def offered_tools(self) -> list[Any]:
        return [call["tools"] for call in self.calls]


def ledger_with(
    user_id: str = "u1",
    *,
    spendable: float | str = 0,
    savings: float | str = 0,
    yield_: float | str = 0,
    locked: float | str = 0,
    rent_required: float | str = 0,
    rent_reserved: float | str = 0,
    due_in_days: int | None = 6,
) -> Ledger:
    """A ledger with exactly the balances a test names."""
    ledger = Ledger(user_id=user_id, track=TRACK_70_30)
    ledger.sleeves = {
        "spendable": money(spendable),
        "savings": money(savings),
        "yield": money(yield_),
        "locked": money(locked),
    }
    ledger.rent_first = RentFirst(
        required=money(rent_required),
        reserved=money(rent_reserved),
        due_in_days=due_in_days,
    )
    return ledger
