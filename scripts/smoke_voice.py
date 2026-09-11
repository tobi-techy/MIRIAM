"""Voice smoke-test for Miriam.

Runs a scripted conversation through the real agent loop (real tool
registry + real LLM provider) so the personality can be judged against
the new prompt without standing up the full chat API.

Usage:
    OPENAI_API_KEY=... python scripts/smoke_voice.py

Scenarios intentionally cover the personality beats distilled from the
Ramit Sethi study: connection before numbers, confidence through
competence, systems over willpower, money dials, rich life vs. number,
$3 vs. $30,000 questions, scripts not lectures.

The Go backend is only needed if Miriam actually calls a data tool. If
it is down, she should say "nothing came back" per TRUTH RULES - that
is also worth watching in the output.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

SCENARIOS = [
    "hey",
    "honestly? I feel behind on everything. everyone my age already owns a house",
    "I got a $10k bonus from work. should I invest all of it?",
    "Miriam be real with me. I keep eating out, like $600 a month on it."
    " my friends say I need a budget",
    "idk what I even want in life. I just save money because everyone"
    " says to. isn't it kind of pointless?",
    "I have to ask my boss for a raise next week and I'm terrified",
]

USER_CONTEXT = {
    "name": "Tobi",
    "currency": "USD",
    "risk_tolerance": "moderate",
    "monthly_income": 8000,
    "balances": {"spend": 1200.00, "stash": 38000.00},
    "goals": ["build a 6-month emergency fund", "buy a house in 3 years"],
    "roles": ["user"],
}

MEMORY_FACTS = [
    {
        "type": "goal",
        "content": "wants a 6-month emergency fund; currently at 4 months",
    },
    {"type": "preference", "content": "low tolerance for guilt-trip budgeting"},
    {
        "type": "pattern",
        "content": "gets anxious whenever savings talk comes up;"
        " responds well to specific numbers",
    },
]


async def run_turn(agent, message: str, history: list[dict[str, str]]) -> str:
    result = await agent.run(
        user_id="smoke_user",
        token="smoke-token",
        message=message,
        conversation_id="smoke_conv",
        history=history,
        user_context=USER_CONTEXT,
        memory_facts=MEMORY_FACTS,
    )
    return result.response


async def main() -> int:
    from miriam_agent.agents.agent_loop import Agent
    from miriam_agent.tools.definitions import registry  # ensures tools registered

    provider_class = _load_provider()
    agent = Agent(registry=registry, provider=provider_class())

    history: list[dict[str, str]] = []
    print("\n" + "=" * 72)
    print("MIRIAM VOICE SMOKE TEST")
    print("=" * 72)
    for i, msg in enumerate(SCENARIOS, 1):
        print(f"\n{'─' * 72}\n[{i}] USER: {msg}\n{'─' * 72}")
        try:
            reply = await run_turn(agent, msg, history)
        except Exception as exc:  # noqa: BLE001 - smoke harness reports and continues
            print(f"TOOL/LOOP ERROR (still part of the test): {exc}")
            reply = "(agent raised an error - see above)"
        print(f"MIRIAM: {reply}")
        history.append({"role": "user", "content": msg})
        history.append({"role": "assistant", "content": reply})

    print("\n" + "=" * 72)
    print("DONE. Check: does Miriam sound like one person across turns?")
    print("  - Connection before numbers?  - Confidence through competence?")
    print("  - $3 vs $30,000 framing?      - Scripts when courage is the blocker?")
    print("  - No slop, no filler, no em dashes?  - Honest on failed tools?")
    print("=" * 72 + "\n")
    return 0


def _load_provider():
    """Return a real provider class only if a key is available."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY is not set. Configure it in .env first.")
        sys.exit(1)
    from miriam_agent.agents.llm import OpenAIProvider

    return OpenAIProvider


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
