"""Append-only trace of every LLM-led onboarding turn, and the drift tooling.

The deterministic machinery is easy to test, but Miriam's voice is an LLM and
the prompt gets edited. A prompt edit that changes her behavior -- generic
praise, invented numbers, bullet lists -- must be visible before it ships. So
every conductor/present turn is appended to a capped, append-only trace tagged
with the *prompt version hash* that produced it. Behavior drift can then be
pinned to the exact prompt that caused it:

    python -m miriam_agent.onboarding.trace list      # recent turns
    python -m miriam_agent.onboarding.trace report    # violation rates by prompt
    python -m miriam_agent.onboarding.trace compare OLD NEW   # rule-by-rule diff

The store is Redis-backed with an in-process fallback, exactly like the state
store (fail-open: a missing Redis can never break a reply). Trace appends are
also fail-open in the service -- observability never blocks the conversation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

from miriam_agent.config.settings import get_settings
from miriam_agent.observability.correlation import current_trace_id

logger = logging.getLogger(__name__)

_KEY_PREFIX = "miriam:onboarding:trace"
MAX_TRACE_ENTRIES = 2000


@dataclass
class TraceRecord:
    """One LLM-led turn, as written to the append-only trace.

    ``violations`` holds the spec rules the linter found for this reply
    (computed with the same grounding the service used); ``clamped`` marks a
    plan presentation that was refused because it invented a number.
    ``trace_id`` ties the turn to the request that carried it, so an onboarded
    plan can be read alongside the tool calls and audit rows behind it.
    """

    user_id: str
    mode: str  # "conductor" | "present"
    stage: str
    intent: str
    reply: str
    prompt_version: str
    violations: list[str] = field(default_factory=list)
    clamped: bool = False
    facts: dict[str, str] = field(default_factory=dict)
    taps: list[str] = field(default_factory=list)
    adjustment: str = ""
    grounded: str = ""
    ts: float = field(default_factory=time.time)
    # Defaulted from the bound request context: every service call site gets the
    # request's id without having to pass it down.
    trace_id: str = field(default_factory=current_trace_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TraceRecord:
        return cls(
            user_id=str(data.get("user_id") or ""),
            mode=str(data.get("mode") or "conductor"),
            stage=str(data.get("stage") or ""),
            intent=str(data.get("intent") or ""),
            reply=str(data.get("reply") or ""),
            prompt_version=str(data.get("prompt_version") or ""),
            violations=list(data.get("violations") or []),
            clamped=bool(data.get("clamped")),
            facts=dict(data.get("facts") or {}),
            taps=list(data.get("taps") or []),
            adjustment=str(data.get("adjustment") or ""),
            grounded=str(data.get("grounded") or ""),
            ts=float(data.get("ts") or time.time()),
            trace_id=str(data.get("trace_id") or ""),
        )


class OnboardingTraceStore:
    """Append-only, capped trace of onboarding turns.

    Implemented as a Redis list (RPUSH + LTRIM) so it is cheap to append,
    naturally ordered, and bounded. The in-process deque is always kept as a
    fallback so Redis being down never drops the trace silently.
    """

    def __init__(
        self,
        redis_url: str | None = None,
        max_entries: int | None = None,
        use_redis: bool = True,
    ):
        settings = get_settings()
        self._max = max_entries or MAX_TRACE_ENTRIES
        self._url = redis_url
        if self._url is None and use_redis:
            self._url = settings.REDIS_URL or None
        self._redis: Any = None
        if self._url:
            try:
                from redis import asyncio as aioredis

                self._redis = aioredis.from_url(self._url, decode_responses=True)
            except Exception as e:  # pragma: no cover - import/env issues
                logger.warning(
                    "Onboarding trace Redis unavailable, using in-process trace: %s",
                    e,
                )
                self._redis = None
        self._local: deque[str] = deque(maxlen=self._max)

    def _key(self) -> str:
        return _KEY_PREFIX

    async def append(self, record: TraceRecord) -> None:
        raw = json.dumps(record.to_dict())
        self._local.append(raw)
        try:
            if self._redis is not None:
                await self._redis.rpush(self._key(), raw)
                await self._redis.ltrim(self._key(), -self._max, -1)
        except Exception as e:
            logger.debug("Onboarding trace write failed (local kept): %s", e)

    async def list_entries(self, limit: int = 200) -> list[TraceRecord]:
        """Most-recent-first entries, at most ``limit``."""
        rows: list[str] = []
        try:
            if self._redis is not None:
                rows = await self._redis.lrange(self._key(), -limit, -1)
                rows = list(reversed(rows))
        except Exception as e:
            logger.debug("Onboarding trace read failed (local used): %s", e)
        if not rows:
            local = list(self._local)
            rows = list(reversed(local[-limit:]))
        records: list[TraceRecord] = []
        for raw in rows:
            try:
                records.append(TraceRecord.from_dict(json.loads(raw)))
            except (ValueError, TypeError):
                continue
        return records

    async def clear(self) -> None:
        self._local.clear()
        try:
            if self._redis is not None:
                await self._redis.delete(self._key())
        except Exception as e:
            logger.debug("Onboarding trace clear failed: %s", e)


_store: OnboardingTraceStore | None = None


def get_onboarding_trace_store() -> OnboardingTraceStore:
    """Process-wide trace store singleton."""
    global _store
    if _store is None:
        _store = OnboardingTraceStore()
    return _store


# ---------------------------------------------------------------------------
# Drift tooling
# ---------------------------------------------------------------------------


def summarize(entries: list[TraceRecord]) -> dict[str, Any]:
    """Violation rates per rule, per prompt version, plus turn tallies."""
    versions: dict[str, dict[str, Any]] = {}
    totals: dict[str, int] = {"turns": 0, "clean": 0, "violating": 0}
    for entry in entries:
        key = f"{entry.mode}/{entry.prompt_version}"
        bucket = versions.setdefault(
            key,
            {
                "mode": entry.mode,
                "prompt_version": entry.prompt_version,
                "turns": 0,
                "clean": 0,
                "violating": 0,
                "by_rule": {},
            },
        )
        bucket["turns"] += 1
        totals["turns"] += 1
        if entry.violations:
            bucket["violating"] += 1
            totals["violating"] += 1
            for rule in entry.violations:
                bucket["by_rule"][rule] = bucket["by_rule"].get(rule, 0) + 1
        else:
            bucket["clean"] += 1
            totals["clean"] += 1
    return {"totals": totals, "by_version": versions}


def _rule_rate(entries: list[TraceRecord], rule: str) -> float:
    if not entries:
        return 0.0
    return sum(1 for e in entries if rule in e.violations) / len(entries)


def drift_between(
    entries: list[TraceRecord], old_version: str, new_version: str
) -> dict[str, Any]:
    """Rule-by-rule rate diff between two prompt versions (new rate minus old).
    Old and new are ``<mode>/<hash>`` keys from ``summarize``."""
    by_key: dict[str, list[TraceRecord]] = {}
    for entry in entries:
        by_key.setdefault(f"{entry.mode}/{entry.prompt_version}", []).append(entry)
    old = by_key.get(old_version, [])
    new = by_key.get(new_version, [])
    rules = sorted({r for e in entries for r in e.violations})
    per_rule: dict[str, Any] = {}
    for rule in rules:
        old_rate = _rule_rate(old, rule)
        new_rate = _rule_rate(new, rule)
        per_rule[rule] = {
            "old_rate": round(old_rate, 4),
            "new_rate": round(new_rate, 4),
            "delta": round(new_rate - old_rate, 4),
        }
    return {
        "old": {"key": old_version, "entries": len(old)},
        "new": {"key": new_version, "entries": len(new)},
        "by_rule": per_rule,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_report(summary: dict[str, Any]) -> None:
    print("prompt version          turns  clean  violated  top rules")
    for key, bucket in sorted(summary["by_version"].items()):
        top = ",".join(
            r for r, _ in sorted(bucket["by_rule"].items(), key=lambda kv: -kv[1])[:3]
        )
        print(
            f"{key:<22} {bucket['turns']:>5}  {bucket['clean']:>5}"
            f"  {bucket['violating']:>8}  {top}"
        )
    t = summary["totals"]
    print(f"totals: {t['turns']} turns, {t['clean']} clean, {t['violating']} violating")


def _print_compare(diff: dict[str, Any]) -> None:
    print(
        f"{diff['old']['key']} ({diff['old']['entries']} turns) -> "
        f"{diff['new']['key']} ({diff['new']['entries']} turns)"
    )
    rows = sorted(diff["by_rule"].items(), key=lambda kv: -abs(kv[1]["delta"]))
    if not rows:
        print("no rule deltas")
        return
    print("rule  old_rate  new_rate  delta")
    for rule, stat in rows:
        marker = "  <-- UP" if stat["delta"] > 0 else ""
        print(
            f"{rule:<7} {stat['old_rate']:>8.2%}  {stat['new_rate']:>8.2%}"
            f"  {stat['delta']:>+.2%}{marker}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="trace",
        description="Onboarding trace + prompt-drift tooling.",
    )
    sub = parser.add_subparsers(dest="cmd")
    list_parser = sub.add_parser("list", help="show the most recent traced turns")
    list_parser.add_argument("--limit", type=int, default=20)
    sub.add_parser("report", help="violation rates grouped by prompt version")
    compare = sub.add_parser(
        "compare", help="diff violation rates between two prompt versions"
    )
    compare.add_argument("old", help="<mode>/<hash> key of the old version")
    compare.add_argument("new", help="<mode>/<hash> key of the new version")
    args = parser.parse_args(argv)

    store = get_onboarding_trace_store()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    entries = loop.run_until_complete(store.list_entries(limit=MAX_TRACE_ENTRIES))

    if args.cmd == "report":
        _print_report(summarize(entries))
        return 0
    if args.cmd == "compare":
        diff = drift_between(entries, args.old, args.new)
        if diff["old"]["entries"] == 0:
            print(f"no traced turns for {args.old}")
            return 1
        if diff["new"]["entries"] == 0:
            print(f"no traced turns for {args.new}")
            return 1
        _print_compare(diff)
        return 0

    # default: list
    if not entries:
        print("no traced turns yet")
        return 0
    limit = getattr(args, "limit", 20)
    for entry in entries[:limit]:
        flag = (
            "CLAMPED"
            if entry.clamped
            else ("  " + ",".join(entry.violations) if entry.violations else "ok")
        )
        print(
            f"[{entry.ts:.0f}] {entry.mode}/{entry.prompt_version} "
            f"{entry.stage}/{entry.intent} {flag} {entry.reply[:60]!r}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
