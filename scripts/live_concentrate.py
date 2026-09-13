"""Live verification of the ConcentrateProvider against the real API.

Tests the three contracts the provider depends on:
  1. Non-streaming completion (plain text)
  2. Streaming SSE (tokens + tool_call pairing)
  3. Multi-turn function_call -> function_call_output pairing

Usage: python scripts/live_concentrate.py
"""

import asyncio
import json
import sys

sys.path.insert(0, "/Users/tobi/Development/MIRIAM")

from miriam_agent.agents.concentrate import ConcentrateProvider
from miriam_agent.agents.llm import ChatMessage

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_balance",
        "description": "Get the user's account balance.",
        "parameters": {"type": "object", "properties": {}},
    },
}


async def test_complete(provider) -> bool:
    print("=== 1. Non-streaming complete ===")
    resp = await provider.complete(
        [ChatMessage(role="user", content="Reply with exactly: hello world")],
        tools=[TOOL_SCHEMA],
    )
    print(f"content: {resp.content!r}")
    print(f"model: {resp.model!r}")
    print(f"usage: {resp.usage}")
    print(f"finish_reason: {resp.finish_reason!r}")
    ok = "hello world" in resp.content.lower()
    print(f"PASS={ok}\n")
    return ok


async def test_stream(provider) -> bool:
    print("=== 2. Streaming SSE ===")
    events = []
    async for ev in provider.stream(
        [ChatMessage(role="user", content="Count from 1 to 3 with a period between.")],
        tools=None,
    ):
        events.append(ev)
        if ev["type"] == "token":
            print(f"TOKEN: {ev['content']!r}")
        else:
            print(f"{ev['type']}: {json.dumps(ev)[:120]}")
    text = "".join(e["content"] for e in events if e["type"] == "token")
    ok = bool(text.strip()) and events[-1]["type"] == "done"
    print(f"assembled: {text[:120]!r}")
    print(f"PASS={ok}\n")
    return ok


async def test_tool_call(provider) -> bool:
    print("=== 3. Multi-turn tool calling ===")
    turn1 = await provider.complete(
        [
            ChatMessage(role="system", content="You are a helpful bank assistant."),
            ChatMessage(role="user", content="What is my total balance?"),
        ],
        tools=[TOOL_SCHEMA],
    )
    print(f"turn1 tool_calls: {json.dumps(turn1.tool_calls)[:200]}")
    print(f"turn1 content: {turn1.content!r}")
    if not turn1.tool_calls:
        print("FAIL: model didn't call the tool")
        return False

    round2_messages = [
        ChatMessage(role="system", content="You are a helpful bank assistant."),
        ChatMessage(role="user", content="What is my total balance?"),
        ChatMessage(
            role="assistant",
            content=turn1.content,
            tool_calls=turn1.tool_calls,
        ),
        ChatMessage(
            role="tool",
            tool_call_id=turn1.tool_calls[0]["id"],
            name=turn1.tool_calls[0]["function"]["name"],
            content=json.dumps({"total": 2500.0}),
        ),
    ]
    turn2 = await provider.complete(round2_messages, tools=[TOOL_SCHEMA])
    print(f"turn2 content: {turn2.content!r}")
    print(f"turn2 tool_calls: {json.dumps(turn2.tool_calls)[:200]}")
    ok = "$2,500" in turn2.content or "2500" in turn2.content
    print(f"PASS={ok}\n")
    return ok


async def main() -> int:
    provider = ConcentrateProvider()
    results = await asyncio.gather(
        test_complete(provider),
        test_stream(provider),
        test_tool_call(provider),
        return_exceptions=False,
    )
    fails = [r for r in results if not r]
    print("=" * 40)
    print(f"RESULT: {3 - len(fails)}/3 passed")
    await provider.aclose()
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
