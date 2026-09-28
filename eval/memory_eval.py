"""Memory-quality golden set: retrieval stays correct, numbers stay out.

Offline by design so CI runs it without a SUPERMEMORY_API_KEY. Each case
pins one load-bearing invariant from docs/MEMORY-ARCHITECTURE.md:

1. Money ingest carries no numbers (user text scrubbed, assistant side is
   a de-identified action/status summary, never the narration).
2. Query-scoped recall is channel-scoped and precision-tuned on the money
   path (threshold + rerank) instead of append-order.
3. Correction is bound: forget-apply deletes exactly the previewed ids.
4. The review queue and erasure paths exist and fail open when disabled.

Exit non-zero on any failure so CI can gate on it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f" — {detail}" if detail and not cond else ""))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def main() -> int:
    from miriam_agent.api import chat as chatmod

    # 1a. Scrub golden pairs: no digits may survive a money turn.
    pairs = [
        ("send 200k to Femi", "send [amount] to Femi"),
        ("put 30 into stocks", "put [amount] into stocks"),
        ("Your balance is 1,234,567.89 NGN", None),
    ]
    for raw, expected in pairs:
        got = chatmod._scrub_money_numbers(raw)
        has_digit = any(ch.isdigit() for ch in got)
        check(f"scrub:no-digits:{raw[:24]}", not has_digit, got)
        if expected is not None:
            check(f"scrub:exact:{raw[:24]}", got == expected, got)
    # Confirm-id taps are opaque tokens, not amounts: the tap text stays
    # linkable while the assistant side still carries only the summary.
    check(
        "scrub:confirm-id-preserved",
        chatmod._scrub_money_numbers("confirm c1 yes") == "confirm c1 yes",
        chatmod._scrub_money_numbers("confirm c1 yes"),
    )

    # 1b. Summary golden: action + status + counterparty, never numbers.
    receipt = SimpleNamespace(action="transfer", status="executed", counterparty="Femi")
    result = SimpleNamespace(receipt=receipt, confirm_id="")
    summary = chatmod._money_memory_summary(result)
    check("summary:exact", summary == "money turn: transfer with Femi (executed)", summary)
    check("summary:no-digits", not any(ch.isdigit() for ch in summary), summary)
    pending = SimpleNamespace(receipt=None, confirm_id="c1")
    check(
        "summary:pending-no-movement",
        "no movement" in chatmod._money_memory_summary(pending),
        chatmod._money_memory_summary(pending),
    )

    # 1c. Money finalize ingests the summary, not the narration.
    seen: dict = {}

    class _Rec:
        def __init__(self) -> None:
            import datetime
            from decimal import Decimal

            self.id = "r1"
            self.at = datetime.datetime.now(datetime.UTC)
            self.status = "executed"
            self.action = "transfer"
            self.currency = "NGN"
            self.amount = Decimal("200000")
            self.counterparty = "Femi"
            self.sleeve = "spend"
            self.reasons: list[str] = []
            self.detail = "Sent 200000 NGN, balance now 720000 NGN"

        def model_dump(self, mode: str = "json"):  # noqa: ANN040 - test double
            return {"id": self.id, "status": self.status}

    class _Res:
        narration = "Sent ₦200,000 to Femi. Balance ₦720,000."
        decision = {"id": "d"}
        receipt = _Rec()
        confirm_id = ""

    class _FakeSM:
        enabled = True

        async def ingest_turn(self, **kwargs):  # noqa: ANN040 - test double
            seen.update(kwargs)
            return {"id": "d"}

        async def ensure_container(self, *a, **k):  # noqa: ANN040 - test double
            return True

    class _FakeStore:
        async def store_interaction(self, **k):  # noqa: ANN040 - test double
            return "c"

        async def get_conversation_history(self, *a):  # noqa: ANN040 - test double
            return []

    from miriam_agent.database.models import User

    u = User()
    u.id = "u1"
    u.username = "t"
    u.full_name = "T"

    async def _go():
        return await chatmod._finalize_money_turn(
            memory_store=_FakeStore(),
            supermemory_memory=_FakeSM(),
            user=u,
            message="send 200000 to Femi",
            result=_Res(),
            conversation_id="c1",
        )

    payload = _run(_go())
    check("finalize:keeps-full-reply-locally", "200,000" in payload["response"], payload["response"][:60])
    blob = (seen.get("user_message") or "") + " " + (seen.get("assistant_message") or "")
    check("finalize:graph-gets-no-digits", not any(ch.isdigit() for ch in blob), blob)
    check("finalize:graph-names-action", "transfer" in (seen.get("assistant_message") or ""), seen.get("assistant_message"))

    # 2. Read path: web filter on chat, threshold+rerank on voice.
    from miriam_agent.conversational.supermemory_memory import SupermemoryMemory

    recorded: dict = {}

    class _FakeClient:
        enabled = True

        async def profile(self, tag, query=None, filters=None, **k):  # noqa: ANN040 - test double
            recorded["profile_filters"] = filters
            return {"profile": {"static": [], "dynamic": [], "buckets": {}}, "search_results": None}

        async def search(self, tag, q, **k):  # noqa: ANN040 - test double
            recorded.update(k)
            return {"results": [], "total": 0}

    sm = SupermemoryMemory(_FakeClient())  # type: ignore[arg-type]
    _run(sm.build_memory_facts("u", query="rent", filters={"channel": "web"}))
    check("read:profile-gets-filters", recorded.get("profile_filters") == {"channel": "web"})

    recorded.clear()
    _run(sm.build_memory_facts("u", query="rent", threshold=0.35, rerank=True))
    check("read:threshold-passthrough", recorded.get("threshold") == 0.35)
    check("read:rerank-passthrough", recorded.get("rerank") is True)

    # 3. Bound forget: apply carries ids, never re-runs the match.
    posted: list = []

    class _ForgetClient:
        enabled = True

        async def forget_matching(self, tag, **k):  # noqa: ANN040 - test double
            posted.append(k)
            if k.get("dry_run"):
                return {"candidates": [{"id": "a"}, {"id": "b"}]}
            return {"deleted": 2}

    sm2 = SupermemoryMemory(_ForgetClient())  # type: ignore[arg-type]
    out = _run(sm2.forget_exact("u", "old bank"))
    check("forget:preview-first", posted and posted[0].get("dry_run") is True)
    check("forget:apply-bound-to-ids", posted[-1].get("ids") == ["a", "b"], str(posted[-1]))
    check("forget:applied", out == {"deleted": 2})

    # 4. Correction endpoints fail open when disabled.
    from miriam_agent.api import memory as memapi

    class _Off:
        enabled = False

    async def _off_calls():
        r = await memapi.remember_fact(memapi.RememberRequest(content="x"), u, _Off())
        f = await memapi.forget_fact(memapi.ForgetRequest(query="x"), u, _Off())
        li = await memapi.list_inferred(u, _Off())
        return r, f, li

    r, f, li = _run(_off_calls())
    check("endpoints:disabled-fail-open", r["enabled"] is False and f["enabled"] is False and li["memories"] == [])

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
